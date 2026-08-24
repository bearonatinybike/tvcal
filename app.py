#!/usr/bin/env python3
"""
tvcal - one shared TV airing calendar.

Data source: TVmaze public API (no key, ~20 req/10s per IP, CC BY-SA).
US shows are offset forward by one day so they land on the calendar the
morning you can actually watch them in the UK. The offset is per-show and
editable, because "US" is not a reliable proxy for global streaming drops.
"""

import datetime as dt
import os
import re
import sqlite3
import threading
import time

import requests
from flask import Flask, Response, jsonify, request, send_from_directory

import contentlist

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DB_PATH = os.environ.get("TVCAL_DB", os.path.join(BASE_DIR, "tvcal.db"))
TVMAZE = "https://api.tvmaze.com"
USER_AGENT = "tvcal/1.0 (self-hosted personal TV calendar)"
REFRESH_HOURS = float(os.environ.get("TVCAL_REFRESH_HOURS", "12"))
REQUEST_GAP = 0.6  # seconds between TVmaze calls, keeps us well under the rate limit

app = Flask(__name__, static_folder=None)
_session = requests.Session()
_session.headers["User-Agent"] = USER_AGENT

SCHEMA = """
CREATE TABLE IF NOT EXISTS shows (
    id          INTEGER PRIMARY KEY,        -- TVmaze show id
    name        TEXT NOT NULL,
    network     TEXT,
    country     TEXT,                       -- ISO code from network/webChannel, may be NULL
    status      TEXT,
    premiered   TEXT,
    image       TEXT,
    url         TEXT,
    shift_days  INTEGER NOT NULL DEFAULT 0, -- days to push the calendar entry forward
    added_at    TEXT NOT NULL,
    refreshed_at TEXT,
    archived    INTEGER NOT NULL DEFAULT 0, -- off the calendar and out of the refresh sweep, but kept
    is_broadcast INTEGER NOT NULL DEFAULT 0 -- country came from network, not webChannel - see default_shift()
);

CREATE TABLE IF NOT EXISTS episodes (
    id       INTEGER PRIMARY KEY,           -- TVmaze episode id
    show_id  INTEGER NOT NULL REFERENCES shows(id) ON DELETE CASCADE,
    season   INTEGER,
    number   INTEGER,
    name     TEXT,
    airdate  TEXT,                          -- YYYY-MM-DD, network local date
    airtime  TEXT,                          -- HH:MM, network local time
    runtime  INTEGER,
    url      TEXT,
    summary  TEXT                            -- TVmaze's HTML synopsis, may be NULL
);

CREATE INDEX IF NOT EXISTS idx_episodes_airdate ON episodes(airdate);
CREATE INDEX IF NOT EXISTS idx_episodes_show ON episodes(show_id);
"""


def db():
    conn = sqlite3.connect(DB_PATH, timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db():
    with db() as conn:
        conn.execute("PRAGMA journal_mode = WAL")
        conn.executescript(SCHEMA)
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(shows)")}
        if "archived" not in cols:
            conn.execute("ALTER TABLE shows ADD COLUMN archived INTEGER NOT NULL DEFAULT 0")
        if "is_broadcast" not in cols:
            conn.execute("ALTER TABLE shows ADD COLUMN is_broadcast INTEGER NOT NULL DEFAULT 0")
        ep_cols = {r["name"] for r in conn.execute("PRAGMA table_info(episodes)")}
        if "summary" not in ep_cols:
            conn.execute("ALTER TABLE episodes ADD COLUMN summary TEXT")


# --------------------------------------------------------------------------
# TVmaze
# --------------------------------------------------------------------------

def tvmaze_get(path, **params):
    resp = _session.get(f"{TVMAZE}{path}", params=params, timeout=20)
    if resp.status_code == 429:
        time.sleep(2)
        resp = _session.get(f"{TVMAZE}{path}", params=params, timeout=20)
    resp.raise_for_status()
    return resp.json()


def _https(url):
    return url.replace("http://", "https://") if url else None


def network_country_code(show):
    """Country code from TVmaze's network field specifically, or None.

    Distinct from show_country() below, which also falls back to webChannel
    for display purposes. This one is the real broadcast/streaming signal:
    a webChannel-sourced country (Hulu, Peacock) doesn't mean the show airs
    on a single evening schedule the way a network-sourced one does.
    """
    return (show.get("network") or {}).get("country", {}).get("code")


def show_country(show):
    """Network country if broadcast, web channel country if streaming, else None.

    Global streamers (Netflix, Prime) report no country at all, which is the
    honest answer - there is no single air date to offset from. This is for
    display only - see network_country_code() for the broadcast/streaming
    distinction that actually drives the shift default and calendar colour.
    """
    for key in ("network", "webChannel"):
        block = show.get(key) or {}
        country = block.get("country") or {}
        if country.get("code"):
            return country["code"]
    return None


def show_network(show):
    for key in ("network", "webChannel"):
        block = show.get(key) or {}
        if block.get("name"):
            return block["name"]
    return None


def summarise_show(show):
    return {
        "id": show["id"],
        "name": show.get("name"),
        "network": show_network(show),
        "country": show_country(show),
        "is_broadcast": bool(network_country_code(show)),
        "status": show.get("status"),
        "premiered": show.get("premiered"),
        "image": _https((show.get("image") or {}).get("medium")),
        "url": show.get("url"),
    }


def default_shift(show):
    """+1 only for a genuine broadcast network reporting US.

    A US streaming webChannel (Hulu, Peacock) isn't the same evening-
    primetime pattern the shift exists for, even though TVmaze does report
    a country for some of them - unlike network, where a US country really
    does mean "airs in US primetime, lands a UK day later." Global
    streamers reporting no country at all (Netflix, Prime) get no shift
    either, on the assumption they drop simultaneously at UK midnight.
    """
    return 1 if network_country_code(show) == "US" else 0


def tvmaze_search_id(name):
    """Best-matching TVmaze show id for a name, or None. Used by the list sync."""
    try:
        results = tvmaze_get("/search/shows", q=name)
    except requests.RequestException:
        return None
    return results[0]["show"]["id"] if results else None


# --------------------------------------------------------------------------
# Sync
# --------------------------------------------------------------------------

def sync_show(show_id, conn, set_shift=None):
    """Fetch a show and its full episode list, write both to the database."""
    data = tvmaze_get(f"/shows/{show_id}", embed="episodes")
    meta = summarise_show(data)
    now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")

    existing = conn.execute("SELECT shift_days FROM shows WHERE id = ?", (show_id,)).fetchone()
    if set_shift is not None:
        shift = int(set_shift)
    elif existing:
        shift = existing["shift_days"]
    else:
        shift = default_shift(data)

    conn.execute(
        """INSERT INTO shows (id, name, network, country, is_broadcast, status, premiered,
                              image, url, shift_days, added_at, refreshed_at)
           VALUES (:id, :name, :network, :country, :is_broadcast, :status, :premiered,
                   :image, :url, :shift, :now, :now)
           ON CONFLICT(id) DO UPDATE SET
               name = excluded.name, network = excluded.network, country = excluded.country,
               is_broadcast = excluded.is_broadcast,
               status = excluded.status, premiered = excluded.premiered,
               image = excluded.image, url = excluded.url, refreshed_at = excluded.refreshed_at""",
        {**meta, "shift": shift, "now": now},
    )

    episodes = (data.get("_embedded") or {}).get("episodes") or []
    keep = []
    for ep in episodes:
        if not ep.get("airdate"):
            continue  # unscheduled specials
        keep.append(ep["id"])
        conn.execute(
            """INSERT INTO episodes (id, show_id, season, number, name, airdate, airtime, runtime, url, summary)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(id) DO UPDATE SET
                   season = excluded.season, number = excluded.number, name = excluded.name,
                   airdate = excluded.airdate, airtime = excluded.airtime,
                   runtime = excluded.runtime, url = excluded.url, summary = excluded.summary""",
            (ep["id"], show_id, ep.get("season"), ep.get("number"), ep.get("name"),
             ep.get("airdate"), ep.get("airtime") or None, ep.get("runtime"), ep.get("url"),
             ep.get("summary")),
        )

    if keep:
        placeholders = ",".join("?" * len(keep))
        conn.execute(
            f"DELETE FROM episodes WHERE show_id = ? AND id NOT IN ({placeholders})",
            (show_id, *keep),
        )
    else:
        conn.execute("DELETE FROM episodes WHERE show_id = ?", (show_id,))

    return conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone()


def run_list_sync():
    """Reconcile ~/.content_list.json with the database."""
    if not contentlist.ENABLED:
        return None
    try:
        with db() as conn:
            return contentlist.sync(conn, sync_show, tvmaze_search_id,
                                    log=app.logger.info)
    except Exception as exc:
        app.logger.warning("content list sync failed: %s", exc)
        return None


def refresh_all():
    with db() as conn:
        ids = [r["id"] for r in conn.execute(
            "SELECT id FROM shows WHERE archived = 0 ORDER BY id")]
    for show_id in ids:
        try:
            with db() as conn:
                sync_show(show_id, conn)
        except Exception as exc:  # a single bad show must not stop the sweep
            app.logger.warning("refresh failed for show %s: %s", show_id, exc)
        time.sleep(REQUEST_GAP)
    return len(ids)


def refresh_loop():
    time.sleep(20)  # let the service settle before the first sweep
    while True:
        try:
            run_list_sync()
            count = refresh_all()
            app.logger.info("refreshed %d shows", count)
        except Exception as exc:
            app.logger.warning("refresh sweep failed: %s", exc)
        time.sleep(REFRESH_HOURS * 3600)


# --------------------------------------------------------------------------
# API
# --------------------------------------------------------------------------

DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")


def parse_date(value, fallback):
    if value and DATE_RE.match(value):
        return dt.date.fromisoformat(value)
    return fallback


@app.get("/api/search")
def api_search():
    query = (request.args.get("q") or "").strip()
    if not query:
        return jsonify([])
    try:
        results = tvmaze_get("/search/shows", q=query)
    except requests.RequestException as exc:
        return jsonify({"error": f"TVmaze search failed: {exc}"}), 502

    with db() as conn:
        followed = {r["id"] for r in conn.execute("SELECT id FROM shows")}

    out = []
    for item in results[:20]:
        show = summarise_show(item["show"])
        show["followed"] = show["id"] in followed
        show["default_shift"] = default_shift(item["show"])
        out.append(show)
    return jsonify(out)


@app.get("/api/shows")
def api_shows():
    with db() as conn:
        rows = conn.execute(
            """SELECT s.*, COUNT(e.id) AS episode_count,
                      MAX(e.airdate) AS last_airdate
               FROM shows s LEFT JOIN episodes e ON e.show_id = s.id
               GROUP BY s.id ORDER BY s.name COLLATE NOCASE"""
        ).fetchall()
    return jsonify([dict(r) for r in rows])


@app.post("/api/shows")
def api_add_show():
    payload = request.get_json(silent=True) or {}
    show_id = payload.get("id")
    if not show_id:
        return jsonify({"error": "id is required"}), 400
    try:
        with db() as conn:
            row = sync_show(int(show_id), conn)
    except requests.RequestException as exc:
        return jsonify({"error": f"TVmaze lookup failed: {exc}"}), 502
    run_list_sync()
    return jsonify(dict(row)), 201


@app.patch("/api/shows/<int:show_id>")
def api_update_show(show_id):
    payload = request.get_json(silent=True) or {}
    if "shift_days" not in payload and "archived" not in payload:
        return jsonify({"error": "shift_days or archived is required"}), 400
    sets, params = [], []
    if "shift_days" in payload:
        sets.append("shift_days = ?")
        params.append(max(0, min(7, int(payload["shift_days"]))))
    if "archived" in payload:
        sets.append("archived = ?")
        params.append(1 if payload["archived"] else 0)
    with db() as conn:
        cur = conn.execute(f"UPDATE shows SET {', '.join(sets)} WHERE id = ?", (*params, show_id))
        if cur.rowcount == 0:
            return jsonify({"error": "show not found"}), 404
        row = conn.execute("SELECT * FROM shows WHERE id = ?", (show_id,)).fetchone()
    return jsonify(dict(row))


@app.delete("/api/shows/<int:show_id>")
def api_remove_show(show_id):
    with db() as conn:
        conn.execute("DELETE FROM episodes WHERE show_id = ?", (show_id,))
        cur = conn.execute("DELETE FROM shows WHERE id = ?", (show_id,))
    if cur.rowcount == 0:
        return jsonify({"error": "show not found"}), 404
    run_list_sync()
    return jsonify({"removed": show_id})


@app.post("/api/shows/<int:show_id>/refresh")
def api_refresh_show(show_id):
    try:
        with db() as conn:
            row = sync_show(show_id, conn)
    except requests.RequestException as exc:
        return jsonify({"error": f"TVmaze lookup failed: {exc}"}), 502
    return jsonify(dict(row))


@app.post("/api/sync-list")
def api_sync_list():
    if not contentlist.ENABLED:
        return jsonify({"error": "content list sync is disabled"}), 400
    result = run_list_sync()
    if result is None:
        return jsonify({"error": "sync failed, see logs"}), 500
    return jsonify(result)


@app.post("/api/refresh")
def api_refresh_all():
    threading.Thread(target=refresh_all, daemon=True).start()
    return jsonify({"started": True})


def calendar_rows(start, end):
    """Episodes whose *shifted* date falls in [start, end]."""
    with db() as conn:
        rows = conn.execute(
            """SELECT e.id, e.season, e.number, e.name AS episode, e.airdate, e.airtime,
                      e.runtime, e.url, e.summary,
                      s.id AS show_id, s.name AS show, s.network, s.country, s.is_broadcast,
                      s.shift_days, s.image,
                      date(e.airdate, '+' || s.shift_days || ' days') AS display_date
               FROM episodes e JOIN shows s ON s.id = e.show_id
               WHERE s.archived = 0
                 AND date(e.airdate, '+' || s.shift_days || ' days') BETWEEN ? AND ?
               ORDER BY display_date, COALESCE(e.airtime, '99:99'), s.name COLLATE NOCASE""",
            (start.isoformat(), end.isoformat()),
        ).fetchall()
    return [dict(r) for r in rows]


@app.get("/api/calendar")
def api_calendar():
    today = dt.date.today()
    start = parse_date(request.args.get("from"), today.replace(day=1))
    end = parse_date(request.args.get("to"), start + dt.timedelta(days=41))
    if end < start:
        start, end = end, start
    if (end - start).days > 400:
        return jsonify({"error": "range must be 400 days or fewer"}), 400
    return jsonify({"from": start.isoformat(), "to": end.isoformat(),
                    "episodes": calendar_rows(start, end)})


# --------------------------------------------------------------------------
# ICS feed - subscribe from a phone or desktop calendar
# --------------------------------------------------------------------------

def ics_escape(text):
    return (text or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


@app.get("/calendar.ics")
def calendar_feed():
    today = dt.date.today()
    rows = calendar_rows(today - dt.timedelta(days=90), today + dt.timedelta(days=270))
    lines = [
        "BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//tvcal//EN",
        "CALSCALE:GREGORIAN", "METHOD:PUBLISH", "X-WR-CALNAME:TV",
    ]
    for row in rows:
        day = dt.date.fromisoformat(row["display_date"])
        code = f"S{row['season']:02d}E{row['number']:02d}" if row["season"] and row["number"] else "Special"
        title = f"{row['show']} {code}"
        if row["episode"]:
            title += f" - {row['episode']}"
        desc = f"{row['network'] or 'Unknown network'}"
        if row["shift_days"]:
            desc += f" - aired {row['airdate']}, shifted +{row['shift_days']}d"
        lines += [
            "BEGIN:VEVENT",
            f"UID:tvcal-{row['id']}@tvcal",
            f"DTSTAMP:{dt.datetime.now(dt.timezone.utc).strftime('%Y%m%dT%H%M%SZ')}",
            f"DTSTART;VALUE=DATE:{day.strftime('%Y%m%d')}",
            f"DTEND;VALUE=DATE:{(day + dt.timedelta(days=1)).strftime('%Y%m%d')}",
            f"SUMMARY:{ics_escape(title)}",
            f"DESCRIPTION:{ics_escape(desc)}",
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    lines.append("END:VCALENDAR")
    body = "\r\n".join(lines) + "\r\n"
    return Response(body, mimetype="text/calendar",
                    headers={"Content-Disposition": "inline; filename=tv.ics"})


# --------------------------------------------------------------------------
# Static
# --------------------------------------------------------------------------

@app.get("/")
def index():
    return send_from_directory(os.path.join(BASE_DIR, "static"), "index.html")


@app.get("/<path:filename>")
def static_files(filename):
    return send_from_directory(os.path.join(BASE_DIR, "static"), filename)


init_db()

with db() as _conn:
    _conn.executescript(contentlist.SYNC_SCHEMA)

if os.environ.get("TVCAL_AUTOREFRESH", "1") == "1":
    threading.Thread(target=refresh_loop, daemon=True).start()

if __name__ == "__main__":
    app.run(host=os.environ.get("TVCAL_HOST", "0.0.0.0"),
            port=int(os.environ.get("TVCAL_PORT", "8087")))
