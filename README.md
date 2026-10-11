# onilot

Self-hosted backend for **Tolino e-readers**. Keep the stock Tolino firmware and its reader
app, but sync your own DRM-free EPUBs and your reading progress with a server you run
yourself instead of the vendor cloud.

*onilot* is "tolino" backwards.

> **Status: experimental, single user.** Verified on a Tolino Vision Color with firmware
> 5.18.264769. Other 5.x devices (Shine 5 / Shine Color) run the same software and will
> probably work, but are untested.

## What works

| Feature | |
|---|---|
| Setup and sign-in on the reader (password page on your server) | ✓ |
| Library sync, covers, book download | ✓ |
| Reading position sync, both directions, last write wins | ✓ |
| Books copied to the reader over USB are uploaded to the server automatically | ✓ |
| Home screen recommendations, shop tab (lists the server's books) | ✓ |
| Collections sync | ✓ |
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
      ONILOT_PASSWORD: choose-a-password
    volumes:
      - /srv/books:/library        # your EPUBs; uploads from the reader land here too
      - /srv/onilot-data:/data     # reading positions, tokens, signing key
```

The container runs as UID 1000; make the volumes writable for it.

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
   the country, and your login page appears. Enter `ONILOT_PASSWORD`.

Your library syncs, books download on tap, and positions sync from then on.

### Undo

Copy `out/Kobo-restore.tgz` to `.kobo/` as `Kobo.tgz` (or just set
`pref_is_prod_env_enabled=true`) and restart. A firmware update also restores the original.

## Adding books

Put DRM-free EPUBs into the library volume. They show up on the reader at its next sync.
Books you copy onto the reader over USB are uploaded to the server automatically.

## Configuration

| Variable | Default | |
|---|---|---|
| `ONILOT_PUBLIC_URL` | (required) | the exact URL the reader was patched to |
| `ONILOT_PASSWORD` | (required) | password for the login page on the reader |
| `ONILOT_LIBRARY` | `/library` (image) | EPUB directory |
| `ONILOT_DATA` | `/data` (image) | state directory |
| `ONILOT_PORT` / `ONILOT_BIND` | `8095` / `0.0.0.0` (image) | listen address |
| `ONILOT_BRAND` | `onilot` | name shown on the reader |
| `ONILOT_COUNTRY` | `DE` | country offered during setup |
| `ONILOT_REQUEST_LOG` | `0` | `1` = log every request/response to `DATA/requests.jsonl` (secrets redacted); useful when trying new firmware |

Without Docker: `python3 -m onilot` (Python 3.10+, no dependencies).

## Security notes

- Single user: everyone who knows `ONILOT_PASSWORD` gets the whole library. Use a strong one.
- Library and progress endpoints require an access token issued after login. Book and
  cover URLs are HMAC-signed capability URLs (the reader sends no token for downloads);
  book links expire after 24 h.
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
