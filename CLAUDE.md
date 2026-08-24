# CLAUDE.md

## What this is

`tvcal` — a self-hosted TV airing calendar. Search TVmaze for UK and US shows, add
them, see episodes on a month grid. US shows appear on the calendar the day *after*
they air, so they land on the morning you can watch them in the UK.

No user accounts. One shared calendar. Runs in Docker on `linuxvm`.

Flask + SQLite + vanilla JS. No build step, no frontend framework, no ORM.

## Layout

```
app.py              Flask app, TVmaze client, SQLite schema, API, ICS feed
contentlist.py      Two-way sync with get_content's ~/.content_list.json
static/index.html   Whole frontend — inline CSS and JS, one file
Dockerfile          python:3.12-slim, gunicorn
docker-compose.yml  The deploy on linuxvm
tvcal.service       systemd alternative if Docker is dropped
```

## Related projects

Both live in `~/OneDrive/Dev/`. Start sessions with:

```bash
cd ~/OneDrive/Dev/tvcal
claude --add-dir ../get_content ../organise_media
```

- `get_content` — CLI that finds episodes and hands them to Transmission. Shares
  `~/.content_list.json` with tvcal. Also uses TVmaze. Has separate Mac and Linux
  variants; only one is on GitHub.
- `organise_media` — bash scripts that sort downloads into `~/media/{Movies,TV}`.
  Uses TVmaze and OMDb. Keeps `~/.config/organise_media/title_corrections.tsv`.

## Rules

- **Do not add acquisition features to tvcal.** It is a calendar. Downloading,
  torrent search and Transmission stay in `get_content`. The shared surface between
  them is the show list and title metadata, nothing else.
- **Keep `--workers 1`.** The refresh and content-list sync threads live inside the
  gunicorn worker. More workers means duplicate sweeps against TVmaze.
- **TVmaze is rate limited** to about 20 requests per 10 seconds per IP, shared
  across tvcal and `get_content` on the same host. Use `?embed=episodes` rather than
  separate show and episode calls. Set a real User-Agent; TVmaze asks for one.
- **Data is CC BY-SA.** The attribution line in the page footer stays.
- **The calendar works in dates, not timestamps.** `TZ` must be set. On UTC, BST
  evening broadcasts land on the wrong day — which is exactly the boundary the
  whole +1 rule depends on.
- **Never let a missing content list prune the database.** `read_file()` returns
  `None` for unreadable, which is different from an empty list. Keep that distinction.
- No `localStorage` or `sessionStorage` in the frontend.
- British spelling in UI copy and comments.

## Commands

```bash
docker compose up -d --build      # deploy
docker compose logs -f tvcal
curl -X POST localhost:8087/api/sync-list      # reconcile the show list now
curl -X POST localhost:8087/api/refresh        # re-pull episodes for every show
```

## Key design decisions

**Per-show offset, not a render-time rule.** `shows.shift_days` is stored, defaulting
to 1 when the TVmaze country is `US` and 0 otherwise. Global streamers report no
country at all, so they default to no shift — usually right, since they drop at
00:00 UK. The UI has a per-show override. Shifted entries carry a `+1` badge and the
episode popover shows the real air date, so the shift is never silent.

**The content-list sync records state.** A `synced_shows` table holds the set as of
the last sync. Without it, a union re-adds anything deleted on either side. With it,
`(file | db) - ((last - file) | (last - db))` makes removals stick both ways.

**Writes to the content list are atomic.** Temp file in the same directory, then
`os.replace`, so `get_content` can never read a half-written list. This is why the
compose file mounts a *directory* at `/hostdata` — a single-file bind mount stops
tracking the file after the first inode swap.

**The canonical file lives on both machines, kept in step by `get_content` itself.**
`~/.content_list.json` exists independently on the Mac and on `linuxvm` (tvcal only ever
sees `linuxvm`'s copy). Rather than a background sync daemon on either side,
`get_content.py`'s list-driven mode (`sync_content_list_with_peer()`, both machines run an
identical copy of the script) does two one-directional `rsync -u` passes with its peer
before reading and after writing — whichever side has the newer mtime wins, propagated
both ways, no daemon required. tvcal's own writes (add/remove in the UI) only reach the
Mac the next time `get_content` runs there.

## Status

Deployed at `/opt/tvcal` on `linuxvm`, live at `https://tvcal.bearonatinybike.com:8446`
with a landing-page card. Verified against the real TVmaze API and real followed shows
(not just mocks) — search, add, per-show shift override, calendar month view, episode
popover, and the sub-760px agenda layout all checked in a browser against the live
deployment.

**Known gap:** `default_shift()` only looks at `network.country.code` /
`webChannel.country.code`. Services that don't report a country at all — Netflix, Prime
Video, Paramount+ — default to no shift, which is right, since they drop simultaneously
at 00:00 UK. But HBO Max also reports no country, and it *isn't* simultaneous — it's a
US-only service on US time, same as any broadcast network. Confirmed against two real
followed shows (`Hacks`, `The Pitt`): both get `shift_days = 0` by default and need the
per-show override ticked manually. If more HBO Max (or similarly US-only, no-country)
shows get added, worth hardcoding a short list of known US-only streaming networks as a
second check in `default_shift()`, rather than relying on the country code alone.

**Not done:** no tests in the repo; no `/api/sync-list` button in the Shows panel
(endpoint only); no local caching of TVmaze poster images; the ICS feed is
unauthenticated (fine on a LAN, worth revisiting if it's ever exposed); tvcal could
populate `title_corrections.tsv` from its own TVmaze names/premiere years once the
`db_save` fix has proven itself, so `organise_media` stops prompting for shows already
followed — not done yet.
