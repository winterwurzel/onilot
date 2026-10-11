#!/usr/bin/env python3
"""Point a Tolino reader (firmware 5.x, Tolino mode) at an onilot server.

The reader app builds its REST base from one of two hardcoded strings in
/usr/local/Kobo/libnickel.so.1.0.0, selected by a developer setting
(TolinoServices/pref_is_prod_env_enabled):
    prod    -> https://api.pageplace.de
    prodref -> https://api.prodref.pageplace.de   (32 characters)
This tool replaces the prodref string with your server URL of exactly the same length, so
"prodref" means "my server" and "prod" still means the vendor cloud. It patches your own
copy of the library; never redistribute the patched file.

Outputs (in <outdir>):
    libnickel.so.1.0.0   patched library
    Kobo.tgz             copy to .kobo/ on the reader; applied at the next boot
    Kobo-restore.tgz     original library, same mechanism, to undo the patch
    SHA256SUMS

Usage:
    patch_libnickel.py --suggest https://books.example.com
        print a valid 32-character URL for that host (path padding); use it as
        ONILOT_PUBLIC_URL on the server
    patch_libnickel.py <libnickel.so.1.0.0> <url> <outdir>
"""
import hashlib
import io
import os
import sys
import tarfile
import time
import urllib.parse

ORIGINAL = b'https://api.prodref.pageplace.de'
PROD = b'https://api.pageplace.de\0'
LIB_NAME = 'libnickel.so.1.0.0'
PADDING = 'onilotapi'


def fail(message):
    sys.exit('error: ' + message)


def suggest_url(base):
    """Pad https://host[:port] with a path so the result is exactly 32 characters."""
    base = base.rstrip('/')
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ('http', 'https') or not parts.netloc or parts.path:
        fail('give the bare origin, e.g. https://books.example.com')
    needed = len(ORIGINAL) - len(base)
    if needed == 0:
        return base
    if needed < 2:
        fail('%s is %d characters; it must be at most %d (or exactly %d) to fit'
             % (base, len(base), len(ORIGINAL) - 2, len(ORIGINAL)))
    path = (PADDING * (needed // len(PADDING) + 1))[:needed - 1]
    return '%s/%s' % (base, path)


def check_url(url):
    if len(url) != len(ORIGINAL):
        fail('URL must be exactly %d characters, got %d (%s); try --suggest'
             % (len(ORIGINAL), len(url), url.decode()))
    if not url.startswith((b'https://', b'http://')) or url.endswith(b'/'):
        fail('URL must start with http(s):// and must not end with "/" '
             '(API paths like /v1/... are appended directly)')


def build_tgz(path, payload):
    info = tarfile.TarInfo(LIB_NAME)
    info.size = len(payload)
    info.mode = 0o755
    info.mtime = int(time.time())
    info.uid = info.gid = 0
    info.uname = info.gname = 'root'
    with tarfile.open(path, 'w:gz', format=tarfile.GNU_FORMAT) as tar:
        tar.addfile(info, io.BytesIO(payload))


def patch(original, url):
    needle = ORIGINAL + b'\0'
    if original.count(needle) != 1:
        fail('expected exactly one "%s" in the library, found %d; unsupported firmware '
             'version or already patched' % (ORIGINAL.decode(), original.count(needle)))
    if original.count(PROD) != 1:
        fail('prod URL not found exactly once; unsupported firmware version')
    offset = original.index(needle)
    patched = original[:offset] + url + original[offset + len(ORIGINAL):]
    assert len(patched) == len(original)
    return offset, patched


def main(argv):
    if len(argv) == 2 and argv[0] == '--suggest':
        print(suggest_url(argv[1]))
        return
    if len(argv) != 3:
        sys.exit(__doc__)
    lib_path, url, outdir = argv[0], argv[1].encode('ascii'), argv[2]
    check_url(url)
    with open(lib_path, 'rb') as f:
        original = f.read()
    offset, patched = patch(original, url)

    os.makedirs(outdir, exist_ok=True)
    with open(os.path.join(outdir, LIB_NAME), 'wb') as f:
        f.write(patched)
    os.chmod(os.path.join(outdir, LIB_NAME), 0o755)
    build_tgz(os.path.join(outdir, 'Kobo.tgz'), patched)
    build_tgz(os.path.join(outdir, 'Kobo-restore.tgz'), original)

    sums = []
    for name in (LIB_NAME, 'Kobo.tgz', 'Kobo-restore.tgz'):
        with open(os.path.join(outdir, name), 'rb') as f:
            sums.append('%s  %s' % (hashlib.sha256(f.read()).hexdigest(), name))
    sums.append('%s  original %s' % (hashlib.sha256(original).hexdigest(), LIB_NAME))
    with open(os.path.join(outdir, 'SHA256SUMS'), 'w') as f:
        f.write('\n'.join(sums) + '\n')
    print('patched offset 0x%x: %s -> %s' % (offset, ORIGINAL.decode(), url.decode()))
    print('\n'.join(sums))


if __name__ == '__main__':
    main(sys.argv[1:])
