"""
Two-way sync between tvcal's show list and get_content's ~/.content_list.json.

The file stays the shared source of truth for *which* shows are followed.
TVmaze ids and episode data stay in tvcal's SQLite database.

Membership changes are resolved against the set recorded at the last sync
(`synced_shows`), so a removal on either side sticks instead of being undone
by the other side on the next pass:

    added to the file      -> added to tvcal
    removed from the file  -> removed from tvcal
    added in the tvcal UI  -> written to the file
    removed in the tvcal UI-> removed from the file

Entries are written back as {"name": ..., "tvmaze_id": ...} so get_content can
look a show up by id instead of re-guessing it from a name search every run.
Bare strings are still read, so an unmigrated file works untouched.
"""

import json
import os
import re
import tempfile

CONTENT_LIST = os.path.expanduser(
    os.environ.get("TVCAL_CONTENT_LIST", "~/.content_list.json")
)
ENABLED = os.environ.get("TVCAL_CONTENT_LIST_SYNC", "1") == "1"

SYNC_SCHEMA = """
CREATE TABLE IF NOT EXISTS synced_shows (
    tvmaze_id INTEGER PRIMARY KEY,
    name      TEXT
);
"""


def normalise_key(title):
    """Same rule organise_media uses for title_corrections.tsv.

    Lowercase, drop a trailing (year), strip apostrophes and hyphens,
    collapse whitespace. Keeps "Bob's Burgers" and "Bobs Burgers (2011)"
    on the same key.
    """
    t = re.sub(r"\s*\(\d{4}\)\s*$", "", (title or "").strip())
    t = re.sub(r"['\u2018\u2019\u201c\u201d\"\u2013\u2014-]", "", t)
    return re.sub(r"\s+", " ", t.lower()).strip()


# ---------------------------------------------------------------------------
# File I/O
# ---------------------------------------------------------------------------

def read_file():
    """Return (entries, document) or (None, None) if the file is unusable.

    Returning None means "no opinion" - the caller must not treat that as an
    empty list, or a missing mount would wipe every show in the database.
    """
    if not os.path.exists(CONTENT_LIST):
        return None, None
    try:
        with open(CONTENT_LIST, "r", encoding="utf-8") as fh:
            raw = fh.read().strip()
    except OSError:
        return None, None
    if not raw:
        return [], {"shows": [], "last_downloaded": {}}

    try:
        doc = json.loads(raw)
    except json.JSONDecodeError:
        # Pre-JSON plain text format, one show per line.
        shows = [l.strip() for l in raw.splitlines()
                 if l.strip() and not l.startswith("#")]
        doc = {"shows": shows, "last_downloaded": {}}

    if not isinstance(doc, dict) or not isinstance(doc.get("shows"), list):
        return None, None

    entries = []
    for item in doc["shows"]:
        if isinstance(item, str):
            entries.append({"name": item, "tvmaze_id": None})
        elif isinstance(item, dict) and item.get("name"):
            tid = item.get("tvmaze_id")
            entries.append({"name": item["name"],
                            "tvmaze_id": int(tid) if tid else None})
    return entries, doc


def write_file(entries, doc, unresolved):
    """Rewrite the file, preserving last_downloaded and any other keys."""
    doc = dict(doc or {})
    doc["shows"] = [{"name": e["name"], "tvmaze_id": e["tvmaze_id"]} for e in entries]
    doc["shows"] += unresolved  # names TVmaze could not match, kept verbatim
    doc.setdefault("last_downloaded", {})

    directory = os.path.dirname(CONTENT_LIST) or "."
    os.makedirs(directory, exist_ok=True)
    # Same-directory temp file plus replace, so a crash mid-write can't leave
    # get_content reading a truncated list.
    fd, tmp = tempfile.mkstemp(dir=directory, prefix=".content_list.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(doc, fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        os.replace(tmp, CONTENT_LIST)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise


# ---------------------------------------------------------------------------
# Sync
# ---------------------------------------------------------------------------

def sync(conn, sync_show, tvmaze_search, log=None):
    """Reconcile the file with the database. Returns a summary dict.

    sync_show(show_id, conn)  - adds/refreshes a show, from app.py
    tvmaze_search(name)       - returns a TVmaze id or None, from app.py
    """
    def note(msg):
        if log:
            log(msg)

    if not ENABLED:
        return {"skipped": "disabled"}

    conn.executescript(SYNC_SCHEMA)

    file_entries, doc = read_file()
    db_rows = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM shows")}
    last_synced = {r["tvmaze_id"] for r in conn.execute("SELECT tvmaze_id FROM synced_shows")}

    if file_entries is None:
        # No readable file. Export what we have and record it, but never prune.
        note(f"content list not readable at {CONTENT_LIST}; exporting only")
        _record(conn, db_rows)
        write_file([{"name": n, "tvmaze_id": i} for i, n in sorted(db_rows.items(),
                                                                  key=lambda kv: kv[1].lower())],
                   {}, [])
        return {"exported": len(db_rows), "imported": 0, "removed": 0}

    # Resolve every file entry to a TVmaze id.
    by_key = {normalise_key(name): sid for sid, name in db_rows.items()}
    file_ids, unresolved = set(), []
    for entry in file_entries:
        sid = entry["tvmaze_id"] or by_key.get(normalise_key(entry["name"]))
        if sid is None:
            sid = tvmaze_search(entry["name"])
            if sid is None:
                note(f"could not resolve {entry['name']!r} on TVmaze; leaving it in place")
                unresolved.append({"name": entry["name"], "tvmaze_id": None})
                continue
        file_ids.add(sid)

    db_ids = set(db_rows)
    dropped = (last_synced - file_ids) | (last_synced - db_ids)
    final = (file_ids | db_ids) - dropped

    to_add = final - db_ids
    to_remove = db_ids - final

    for sid in sorted(to_add):
        try:
            row = sync_show(sid, conn)
            db_rows[sid] = row["name"]
            note(f"imported {row['name']} from content list")
        except Exception as exc:
            note(f"import failed for TVmaze id {sid}: {exc}")
            final.discard(sid)

    for sid in sorted(to_remove):
        conn.execute("DELETE FROM episodes WHERE show_id = ?", (sid,))
        conn.execute("DELETE FROM shows WHERE id = ?", (sid,))
        note(f"removed {db_rows.get(sid, sid)} - dropped from content list")

    names = {r["id"]: r["name"] for r in conn.execute("SELECT id, name FROM shows")}
    entries = sorted(({"name": names[i], "tvmaze_id": i} for i in final if i in names),
                     key=lambda e: e["name"].lower())
    write_file(entries, doc, unresolved)
    _record(conn, {e["tvmaze_id"]: e["name"] for e in entries})

    return {"imported": len(to_add), "removed": len(to_remove),
            "exported": len(entries), "unresolved": len(unresolved)}


def _record(conn, id_to_name):
    conn.execute("DELETE FROM synced_shows")
    conn.executemany("INSERT INTO synced_shows (tvmaze_id, name) VALUES (?, ?)",
                     list(id_to_name.items()))
