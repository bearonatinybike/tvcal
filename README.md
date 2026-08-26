# tvcal

One shared TV calendar. No accounts, no per-user state — whatever is on the VM is
the calendar. Search TVmaze for UK and US shows, add them, and their episodes fill
in a month grid.

US shows land on the calendar the day *after* they air. The offset is stored per
show (`shows.shift_days`) rather than computed at render time, so you can override
it for anything the default gets wrong.

## Data source

TVmaze public API — no key, no signup, rate limited to roughly 20 requests per 10
seconds per IP. Listings are CC BY-SA, so the attribution line in the footer needs
to stay. Endpoints used:

- `GET /search/shows?q=` — show search
- `GET /shows/{id}?embed=episodes` — show metadata plus full episode list in one call

## How the offset is decided

On add, the country comes from `network.country.code`, falling back to
`webChannel.country.code`. `US` gets `shift_days = 1`; everything else gets `0`.

Global streamers (Netflix, Apple TV+, Prime) report no country at all, so they
default to no shift. That is usually right — they drop at 00:00 UK time — but the
checkbox under **Shows** lets you change it per show either way. The `+1` badge on
a calendar chip tells you an entry has been moved, and the episode popover shows
the real air date next to the calendar date.

## Endpoints

| Method | Path | Purpose |
| --- | --- | --- |
| GET | `/api/search?q=` | TVmaze search, flagged with what you already follow |
| GET | `/api/shows` | Followed shows with episode counts |
| POST | `/api/shows` | `{"id": 12345}` — add and sync a show |
| PATCH | `/api/shows/<id>` | `{"shift_days": 0\|1}` |
| DELETE | `/api/shows/<id>` | Remove a show and its episodes |
| POST | `/api/shows/<id>/refresh` | Re-sync one show |
| POST | `/api/refresh` | Re-sync everything in the background |
| POST | `/api/sync-list` | Reconcile `~/.content_list.json` now |
| GET | `/api/calendar?from=&to=` | Episodes by shifted date |
| GET | `/calendar.ics` | Subscribable feed, −90 to +270 days |

## Shared show list

tvcal and `get_content` keep the same list of shows, in `~/.content_list.json`.
Adding a show in the tvcal UI writes it there; adding a line to the file by hand
imports it into tvcal on the next sync. Removals stick on either side, because
the set as of the last sync is recorded in a `synced_shows` table rather than
inferred from the two lists.

Entries are written back with the TVmaze id pinned:

```json
{
  "shows": [
    {"name": "Ghosts", "tvmaze_id": 30770},
    {"name": "Hacks", "tvmaze_id": 41448}
  ],
  "last_downloaded": {"Ghosts": "S05E22"}
}
```

Bare strings are still read, so an unmigrated file works untouched. `last_downloaded`
and any other top-level keys are preserved. Writes go through a temp file in the
same directory then `os.replace`, so `get_content` can never read a half-written list.

Names TVmaze can't match are left in the file verbatim rather than dropped, which
also means a network failure during a sync loses nothing.

Apply `get_content-tvmaze-id.patch` to `get_content.py` so it reads the id form and
looks shows up by id instead of re-running a name search each time:

```bash
cd ~/OneDrive/Dev/get_content && patch -p0 < /path/to/get_content-tvmaze-id.patch
```

Sync runs on startup, on every add and remove, on the refresh interval, and on
`POST /api/sync-list`. Set `TVCAL_CONTENT_LIST_SYNC=0` to turn it off.

If the file is missing or unreadable, tvcal exports its own list and never prunes -
a missing bind mount can't empty your database.

## Run with Docker (linuxvm)

The VM runs this repo as a git clone in `~/dev/tvcal`, the same place and pattern
as the other self-hosted apps there. Deploy by pulling, not by copying a working
tree over:

```bash
git clone git@github.com:bearonatinybike/tvcal.git ~/dev/tvcal    # first time only
cd ~/dev/tvcal
cp .env.example .env       # set TVCAL_UID / TVCAL_GID from `id -u` / `id -g`
mkdir -p data
docker compose up -d --build
docker compose logs -f
```

`.env` and `data/` are gitignored, so they survive a `git pull` and are the only
two things a fresh clone needs recreating.

The compose file binds the container to `127.0.0.1:8087` only — nginx fronts it with TLS
from the shared `linuxvm.bearonatinybike.com` cert lineage, same pattern as Nom De Plume and
Transmission (`/etc/nginx/sites-enabled/linuxvm-local.conf`, a `listen 8446 ssl` block proxying
to `127.0.0.1:8087`). Live at `https://tvcal.bearonatinybike.com:8446`, with a card on the
landing page (`/var/www/html/index.html`). Adding a new domain to that cert lineage needs
`--dns-cloudflare-propagation-seconds 45` or higher — the default 10s isn't enough and fails
every domain in the reissue, not just the new one.

Notes:

- `TZ=Europe/London` is set in the compose file and `tzdata` is installed in the
  image. The calendar works in local dates, so a container left on UTC puts BST
  evening broadcasts on the wrong day.
- The container runs as your uid, so the database and content list stay yours
  rather than root's. Create `./data` before the first `up` or Docker will make
  it root-owned.
- `$HOME` is mounted at `/hostdata` as a directory, not `.content_list.json` as a
  single file. A single-file bind mount stops tracking the file after the first
  atomic replace. Point `TVCAL_CONTENT_DIR` somewhere narrower if you'd rather not
  mount all of `$HOME`.
- Keep `--workers 1`. The refresh and sync threads live in the worker, so more
  than one means duplicate sweeps against TVmaze.

## Install on the VM (systemd, no Docker)

```bash
sudo useradd --system --home /opt/tvcal --shell /usr/sbin/nologin tvcal
sudo mkdir -p /opt/tvcal && sudo chown tvcal:tvcal /opt/tvcal

sudo -u tvcal cp -r app.py contentlist.py static requirements.txt /opt/tvcal/
sudo -u tvcal python3 -m venv /opt/tvcal/.venv
sudo -u tvcal /opt/tvcal/.venv/bin/pip install -r /opt/tvcal/requirements.txt

sudo cp tvcal.service /etc/systemd/system/
sudo systemctl enable --now tvcal
```

Then open `http://<vm>:8087`.

Run `--workers 1` under gunicorn. SQLite is in WAL mode and the refresh thread
lives inside the worker, so more than one worker means duplicate sync sweeps.
Threads are fine.

## Configuration

| Variable | Default | Notes |
| --- | --- | --- |
| `TVCAL_DB` | `./tvcal.db` | The unit points this at `/var/lib/tvcal/tvcal.db` |
| `TVCAL_REFRESH_HOURS` | `12` | Background re-sync interval |
| `TVCAL_AUTOREFRESH` | `1` | Set to `0` to disable the background thread |
| `TVCAL_PORT` | `8087` | Only used by the dev server |
| `TVCAL_CONTENT_LIST` | `~/.content_list.json` | Shared show list |
| `TVCAL_CONTENT_LIST_SYNC` | `1` | Set to `0` to leave the file alone |
| `TZ` | container default | Set to `Europe/London`; the calendar is date-based |

## Notes

- Episodes with no air date (unannounced specials) are skipped.
- A re-sync updates episodes in place and deletes ones TVmaze has dropped. Manual
  `shift_days` overrides survive it.
- TVmaze caches API responses at its load balancer for up to an hour, so a
  schedule change on their site takes that long to reach you.
- Everything on the calendar is a date, not a timestamp. Air times are shown in
  the episode popover as network-local, straight from TVmaze.
