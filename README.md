# onilot

Self-hosted backend for **Tolino e-readers**. Keep the stock Tolino firmware and its reader
app, but sync your own DRM-free EPUBs and your reading progress with a server you run
yourself instead of the vendor cloud.

*onilot* is "tolino" backwards.

> **Status: no longer maintained (archived 2026-10-12).** The author moved to KOReader on
> the device. The code worked on a Tolino Vision Color with firmware 5.18.264769 at that
> point; other 5.x devices (Shine 5 / Shine Color) run the same software but were never
> tested. Feel free to fork.

## What works

| Feature | |
|---|---|
| Setup and sign-in on the reader (login page on your server) | ✓ |
| Multiple users: own books, reading progress, collections and readers per user; shared books for everyone | ✓ |
| Library sync, covers, book download | ✓ |
| Reading position sync, both directions, last write wins | ✓ |
| Books copied to the reader over USB are uploaded to the server automatically | ✓ |
| Shop tab as a store for shared books (add/remove), home screen recommendations | ✓ |
| Collections sync | ✓ |
| Collections edited in the admin page; one collection per book series (from EPUB metadata) | needs testing on a reader |
| Settings > account: library page with **Sync now**, device list with sign-out | ✓ |
| Adobe DRM / vendor DRM books, audiobooks, highlights/notes sync | ✗ |

## How it works

Since firmware 5.x, Tolino readers run Kobo's reader app ("Nickel") in a Tolino mode. Its
cloud address is hardcoded, but a developer setting switches between the production server
and a test server (`https://api.prodref.pageplace.de`, 32 characters).
`tools/patch_libnickel.py` replaces that test-server address in your own copy of
`libnickel.so` with your onilot URL (same length). You install the patched file with the
firmware's own update mechanism (`Kobo.tgz`) and select the "test server" setting. The
production setting still points to the vendor cloud, and `Kobo-restore.tgz` undoes the patch.

The protocol the reader speaks is documented in [docs/protocol.md](docs/protocol.md).

## Setup

### 1. Run the server

```sh
docker run -d --name onilot --restart unless-stopped \
  -p 8095:8095 \
  -e ONILOT_PUBLIC_URL=https://books.example.com/onilot \
  -e ONILOT_ADMIN_USER=anna \
  -e ONILOT_PASSWORD='choose-a-password' \
  -v /srv/books:/library \
  -v /srv/onilot-data:/data \
  ghcr.io/winterwurzel/onilot:latest
```

or with Compose:

```yaml
services:
  onilot:
    image: ghcr.io/winterwurzel/onilot:latest
    restart: unless-stopped
    ports: ["8095:8095"]
    environment:
      ONILOT_PUBLIC_URL: https://books.example.com/onilot
      ONILOT_ADMIN_USER: anna          # first admin, created on first start
      ONILOT_PASSWORD: choose-a-password
    volumes:
      - /srv/books:/library        # books: /srv/books/<user>/ and /srv/books/shared/
      - /srv/onilot-data:/data     # users, reading positions, tokens, signing key
```

The container runs as UID 1000; make the volumes writable for it.

Image tags: `latest` (main branch), `1.0.0` (exact release), `1.0` (latest patch of 1.0).
Pin `1.0` if you prefer updates you choose yourself. Images exist for amd64, arm64 and
arm/v7.

Put HTTPS in front of it (Caddy, nginx, Traefik, a Cloudflare tunnel, …) so that
`ONILOT_PUBLIC_URL` reaches port 8095 **with the path unchanged**. The reader trusts the
usual public CAs (Let's Encrypt, Google Trust Services, …), so no certificate tweaks are
needed on the device. If you use Cloudflare, exempt the host from bot protection and
challenges: the reader cannot solve them.

`ONILOT_PUBLIC_URL` must be **exactly 32 characters** (it replaces a 32-character string).
Let the patch tool pick a valid one for your host:

```sh
python3 tools/patch_libnickel.py --suggest https://books.example.com
# https://books.example.com/onilot
```

Hosts longer than 30 characters do not fit.

### 2. Get your reader's firmware and patch it

The patch has to match the firmware on your reader exactly. Download the same version's
`update.tar` from Kobo's file server (Vision Color 5.18.264769:
`https://ereaderfiles.kobo.com/firmwares/kobo11/Jul2026/kobo-update-5.18.264769.zip`), unzip
it, then:

```sh
tools/extract-firmware.sh update.tar fw/          # needs zstd and debugfs (e2fsprogs)
python3 tools/patch_libnickel.py fw/libnickel.so.1.0.0 https://books.example.com/onilot out/
```

`out/` now has `Kobo.tgz` (patched) and `Kobo-restore.tgz` (original). Don't share the
patched file; it contains the vendor's software.

(`extract-firmware.sh` unpacks a 1 GB ext4 image to `$TMPDIR`; set `TMPDIR` to a disk-backed
directory if `/tmp` is a small tmpfs.)

### 3. Install on the reader

The reader must run firmware 5.x in **Tolino mode** (Tolino branding).

1. Connect the reader over USB and copy `out/Kobo.tgz` to the hidden `.kobo/` folder. Eject;
   the reader applies it on the next boot.
2. Connect again and add to `.kobo/Kobo/Kobo eReader.conf`:
   ```ini
   [TolinoServices]
   pref_is_prod_env_enabled=false

   [DeveloperSettings]
   DisableOTA=true
   ```
   The first setting selects the test server, i.e. your onilot server. The second stops
   automatic firmware updates, which would replace the patched file.
3. Eject and restart the reader.
4. Sign in to your Tolino account on the reader (setup wizard or account settings). Pick
   the country, and your login page appears. Enter your onilot user name and password.

Your library syncs, books download on tap, and positions sync from then on.

### Undo

Copy `out/Kobo-restore.tgz` to `.kobo/` as `Kobo.tgz` (or just set
`pref_is_prod_env_enabled=true`) and restart. A firmware update also restores the original.

## Users

`ONILOT_ADMIN_USER` + `ONILOT_PASSWORD` create the first admin on the first start (after that
`ONILOT_PASSWORD` is ignored). Add more users on the command line:

```sh
docker exec -it onilot python -m onilot user add bob          # asks for the password
docker exec -it onilot python -m onilot user add carol --admin
docker exec onilot python -m onilot user list
docker exec -it onilot python -m onilot user passwd bob
docker exec onilot python -m onilot user remove bob             # books and progress are kept
```

or in the browser at `https://<your host>/admin` (admins only): overview, per-user reading
progress and readers, add users, set passwords, grant admin, sign readers out, delete; and a
library view per user and for `shared` with drag-and-drop upload and delete (deleted books
disappear from readers at their next sync). Changing a password signs that user's readers out.

Each user has their own books, reading progress, collections and list of readers; "sign out"
on a reader only affects that user. Usernames are lowercase `a-z 0-9 . _ -`.

## Adding books

| Directory (in the library volume) | Visible to |
|---|---|
| `<user>/` | that user only; books copied onto their reader over USB are uploaded here |
| `shared/` | everyone, via the store: each user adds the shared books they want |

DRM-free EPUBs show up on the reader at its next sync (or "Sync now", see below). Shared
books are not added to anyone's library automatically: the **shop tab** on the reader is a
store for them (Add / Remove), paged to fit the screen without scrolling, and the home screen
recommends shared books the user has not added yet.

Admins can create, rename and delete each user's collections and add or remove books under
**Users > (user) > Collections**; changes reach the readers at their next sync.

## Series

Each series becomes a collection on the reader, and the store and admin page show
"Series #n". The series comes from, in this order of priority:

1. your edit in the admin page (**Series**),
2. the EPUB's metadata (Calibre `calibre:series` or EPUB 3 `belongs-to-collection`),
3. a lookup on [Wikidata](https://www.wikidata.org) for books without series metadata.

The lookup sends each such book's title and authors to Wikidata (one book about every 2-3
seconds, in the background; results are kept in `DATA/series.json`). Matches where title and
author agree apply automatically; matches without author data wait under **Series > To
review**. Wikidata knows well-known series; niche books often have no entry. Set
`ONILOT_SERIES_LOOKUP=off` to disable the lookup. EPUB files are never modified.

A series collection you delete or rename on the reader stays that way.

The reader has no series sort, but it sorts by **Date added**, and that date comes from the
server. onilot gives the books of a series consecutive dates in reading order, so
**My Books > Sort: Date added** (or the series' collection sorted that way) lists each series
together, #1 first. The store has a **Series** tab and can add a whole series at once.

## On the reader

- **Settings > account > Link Library** shows your books and has a **Sync now** button.
- **Settings > account > Device Management** lists your readers and can sign them all out.

## Upgrading from a single-user version

Set `ONILOT_ADMIN_USER` (e.g. to your name) next to the existing `ONILOT_PASSWORD` and
restart. On that first start onilot creates the admin and moves the existing reading progress,
tokens, devices, uploads and books (`library/*.epub` to `library/<name>/`) to it. Readers stay
signed in, and cover URLs they stored keep working.

## Configuration

| Variable | Default | |
|---|---|---|
| `ONILOT_PUBLIC_URL` | (required) | the exact URL the reader was patched to |
| `ONILOT_ADMIN_USER` | `admin` | first admin, created on first start |
| `ONILOT_PASSWORD` | (first start) | password of that first admin |
| `ONILOT_LIBRARY` | `/library` (image) | books: `<user>/` and `shared/` |
| `ONILOT_DATA` | `/data` (image) | state directory |
| `ONILOT_PORT` / `ONILOT_BIND` | `8095` / `0.0.0.0` (image) | listen address |
| `ONILOT_BRAND` | `onilot` | name shown on the reader |
| `ONILOT_COUNTRY` | `DE` | country offered during setup |
| `ONILOT_SERIES_LOOKUP` | `wikidata` | `off` = never contact Wikidata (see [Series](#series)) |
| `ONILOT_REQUEST_LOG` | `0` | `1` = log every request/response to `DATA/requests.jsonl` (secrets redacted); useful when trying new firmware |

Without Docker: `python3 -m onilot` (Python 3.10+, no dependencies).

## Security notes

- Passwords are stored as PBKDF2-SHA256 hashes; failed logins are slowed down.
- The admin page uses an HttpOnly, SameSite=Strict session cookie (Secure on https) and a
  CSRF token in every form.
- Library and progress endpoints require an access token issued after login. Book and
  cover URLs are HMAC-signed capability URLs bound to the user (the reader sends no token
  for downloads); book links expire after 24 h.
- Developer mode on the reader can start telnet/FTP servers without a password. Don't
  leave it on longer than needed.

## Development

```sh
python3 -m unittest discover -s tests
```

## Legal

- onilot is an independent, interoperable implementation. It contains **no** vendor code,
  firmware, keys or credentials. The patch tool only modifies the user's own copy of the
  firmware, on the user's own device.
- onilot does **not** circumvent DRM. It serves files you already have without DRM.
- Modifying your reader's software is at your own risk and may affect warranty or support.
- Not affiliated with Tolino, Rakuten Kobo or any bookseller. "Tolino" and "Kobo" are
  trademarks of their respective owners and are used here only to describe compatibility.

Credits: firmware notes by [notmarek/KoboTolinoFindings](https://github.com/notmarek/KoboTolinoFindings),
older Tolino cloud API notes by [cweiske](https://cweiske.de/tolino-api-docs.htm).

## License

[MIT](LICENSE)
