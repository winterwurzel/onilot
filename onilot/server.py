#!/usr/bin/env python3
"""onilot: self-hosted backend for Tolino e-readers running a patched firmware 5.x.

Serves your own DRM-free EPUBs and syncs reading positions with the Tolino reader app
(Kobo Nickel in Tolino mode) after its REST base URL has been patched to point here
(see tools/patch_libnickel.py and README.md). Protocol notes: docs/protocol.md.

Configuration (environment variables; command-line flags override them):
  ONILOT_PUBLIC_URL   required  public base URL the reader was patched to, e.g.
                                https://books.example.com/onilotapi  (TLS terminated in front)
  ONILOT_ADMIN_USER   admin     first admin account, created on first start together with
  ONILOT_PASSWORD               its password (existing single-user data is moved to it)
  ONILOT_LIBRARY      ./library books: LIBRARY/<user>/ is private (uploads from that user's reader
                                land here), LIBRARY/shared/ is visible to every user
  ONILOT_DATA         ./data    state: reading positions, tokens, signing key
  ONILOT_PORT         8095
  ONILOT_BIND         127.0.0.1
  ONILOT_BRAND        onilot    name shown on the reader (shop, login page)
  ONILOT_COUNTRY      DE        country offered during reader setup (any ISO code)
  ONILOT_REQUEST_LOG  0         1 = append every request/response to DATA/requests.jsonl
                                (secrets redacted; useful for debugging new firmware)

Users: `python -m onilot user add NAME [--admin]` or the /admin page in a browser.
Stdlib only (Python 3.10+). Runs plain HTTP; put TLS in front of it.
"""
import argparse
import shutil
import sys
import base64
import datetime
import email.parser
import email.policy
import email.utils
import hashlib
import hmac
import html
import json
import os
import re
import secrets
import threading
import time
import traceback
import urllib.parse
import xml.etree.ElementTree as ET
import zipfile
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from onilot import __version__
from onilot.metadata import Lookup, SeriesStore
from onilot.users import UserError, UserStore, check_name, cli as user_cli

RESELLER_ID = 1
CLIENT_ID = 'onilot'
LOG_BODY_LIMIT = 64 * 1024
SIGNED_URL_TTL = 24 * 3600
COVER_NEVER_EXPIRES = 4102444800  # 2100-01-01; covers are low-sensitivity
ACCESS_TOKEN_TTL = 3600
ADMIN_SESSION_TTL = 8 * 3600
CODE_TTL = 300
MAX_BODY = 256 * 1024 * 1024  # largest accepted request (EPUB uploads)
COLLECTIONS = ('tags', 'comments', 'dogears')
# "Mark as finished" on the reader adds this tag (category "system") to the book.
FINISHED_TAG = 'collection_finished_readings_name'
COUNTRY_NAMES = {'DE': 'Deutschland', 'AT': 'Österreich', 'CH': 'Schweiz'}


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


SECRET_FIELDS = re.compile(
    r'((?:password|code|refresh_token|access_token|client_secret|csrf)["\']?\s*[=:]\s*["\']?)'
    r'[^&"\'\s,}]+')
BEARER = re.compile(r'[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}')


def redact(text):
    """Strip passwords, codes and tokens from text before it goes into the request log."""
    return BEARER.sub('<token>', SECRET_FIELDS.sub(r'\1<redacted>', text))


def make_access_token(user):
    # The device splits on '.', base64-decodes the middle part and reads "sub" as the
    # bookshelf id. It does not check the signature; onilot looks tokens up server-side.
    header = b64url(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())
    payload = b64url(json.dumps({'sub': user, 'iat': int(time.time())}).encode())
    return '%s.%s.%s' % (header, payload, b64url(secrets.token_bytes(16)))


class Library:
    """EPUB files in one or more directories.

    roots: list of (directory, uuid prefix, shared flag). The uuid is sha1(prefix + file name),
    so it stays stable; a user's own directory has prefix "" (compatible with single-user
    installs), the shared directory "shared/".
    """

    OPF_NS = {'opf': 'http://www.idpf.org/2007/opf', 'dc': 'http://purl.org/dc/elements/1.1/',
              'c': 'urn:oasis:names:tc:opendocument:xmlns:container'}

    _meta_cache = {}
    _meta_lock = threading.Lock()
    series_store = None   # SeriesStore: series found on Wikidata or set in the admin page

    def __init__(self, roots, shelf=None):
        self.roots = [(Path(d), prefix, shared) for d, prefix, shared in roots]
        self.shelf = shelf  # if given: only shared books whose uuid is on it

    def books(self):
        result = []
        for root, prefix, shared in self.roots:
            for path in sorted(root.glob('*.epub')):
                stat = path.stat()
                uuid = hashlib.sha1((prefix + path.name).encode()).hexdigest()
                if shared and self.shelf is not None and uuid not in self.shelf:
                    continue
                meta = self._cached_metadata(path, stat)
                if self.series_store is not None:
                    meta = self.series_store.apply(meta)
                result.append({'uuid': uuid, 'path': path, 'size': stat.st_size,
                               'mtime': int(stat.st_mtime * 1000), 'shared': shared, **meta})
        return result

    def _cached_metadata(self, path, stat):
        key = (str(path), stat.st_mtime_ns, stat.st_size)
        with self._meta_lock:
            meta = self._meta_cache.get(key)
        if meta is None:
            meta = self._metadata(path)
            with self._meta_lock:
                self._meta_cache[key] = meta
        return meta

    def find(self, uuid):
        return next((b for b in self.books() if b['uuid'] == uuid), None)

    def _metadata(self, path):
        meta = {'title': path.stem, 'authors': [], 'language': None, 'publisher': None,
                'cover': None, 'series': None, 'series_index': None}
        try:
            with zipfile.ZipFile(path) as z:
                container = ET.fromstring(z.read('META-INF/container.xml'))
                opf_path = container.find('.//c:rootfile', self.OPF_NS).get('full-path')
                opf = ET.fromstring(z.read(opf_path))
                md = opf.find('opf:metadata', self.OPF_NS)
                title = md.findtext('dc:title', default=None, namespaces=self.OPF_NS)
                meta['title'] = title or meta['title']
                meta['authors'] = [e.text for e in md.findall('dc:creator', self.OPF_NS) if e.text]
                meta['language'] = md.findtext('dc:language', default=None,
                                               namespaces=self.OPF_NS)
                meta['publisher'] = md.findtext('dc:publisher', default=None,
                                                namespaces=self.OPF_NS)
                meta['cover'] = self._cover_href(opf, opf_path, z)
                meta['series'], meta['series_index'] = self._series(md)
        except (KeyError, ET.ParseError, zipfile.BadZipFile, AttributeError) as exc:
            print('warning: cannot read metadata of %s: %s' % (path.name, exc))
        return meta

    def _series(self, md):
        """Series name and position from Calibre or EPUB 3 metadata, else (None, None)."""
        name = index = None
        for meta in md.findall('opf:meta', self.OPF_NS):
            if meta.get('name') == 'calibre:series':
                name = (meta.get('content') or '').strip() or None
            elif meta.get('name') == 'calibre:series_index':
                index = meta.get('content')
        if name is None:
            for meta in md.findall('opf:meta', self.OPF_NS):
                if meta.get('property') != 'belongs-to-collection' or not (meta.text or '').strip():
                    continue
                ref = '#' + (meta.get('id') or '')
                refines = {m.get('property'): (m.text or '').strip()
                           for m in md.findall('opf:meta', self.OPF_NS) if m.get('refines') == ref}
                if refines.get('collection-type', 'series') == 'series':
                    name, index = meta.text.strip(), refines.get('group-position')
                    break
        try:
            index = float(index) if index not in (None, '') else None
        except ValueError:
            index = None
        return name, index if name else None

    IMAGE_TYPES = ('image/jpeg', 'image/png', 'image/gif', 'image/webp')

    def _cover_href(self, opf, opf_path, z):
        """Find the cover image: tagged cover, an image called "cover", the image on the first
        page, or the first image in the book. None if the book has no images at all."""
        manifest = opf.find('opf:manifest', self.OPF_NS)
        if manifest is None:
            return None
        base = opf_path.rsplit('/', 1)[0] + '/' if '/' in opf_path else ''
        items = manifest.findall('opf:item', self.OPF_NS)
        images = [i for i in items if (i.get('media-type') or '') in self.IMAGE_TYPES]
        cover = next((i for i in items if 'cover-image' in (i.get('properties') or '')), None)
        if cover is None:
            meta = opf.find(".//opf:meta[@name='cover']", self.OPF_NS)
            if meta is not None:
                cover = next((i for i in items if i.get('id') == meta.get('content')), None)
        if cover is None:
            cover = next((i for i in images if 'cover' in (i.get('id', '') + i.get('href', ''))
                          .lower()), None)
        if cover is not None:
            return urllib.parse.unquote(base + cover.get('href'))
        first_page = self._first_page_image(opf, items, base, z)
        if first_page:
            return first_page
        return urllib.parse.unquote(base + images[0].get('href')) if images else None

    def _first_page_image(self, opf, items, base, z):
        spine = opf.find('opf:spine', self.OPF_NS)
        first = spine.find('opf:itemref', self.OPF_NS) if spine is not None else None
        page = next((i for i in items if first is not None
                     and i.get('id') == first.get('idref')), None)
        if page is None:
            return None
        page_path = urllib.parse.unquote(base + page.get('href'))
        try:
            text = z.read(page_path).decode('utf-8', 'replace')
        except KeyError:
            return None
        match = re.search(r'<(?:img[^>]+src|image[^>]+(?:xlink:)?href)\s*=\s*["\']([^"\']+)',
                          text, re.I)
        if not match:
            return None
        folder = page_path.rsplit('/', 1)[0] + '/' if '/' in page_path else ''
        target = os.path.normpath(folder + urllib.parse.unquote(match.group(1))).replace('\\', '/')
        return target if target in z.namelist() else None

    def cover_bytes(self, book):
        """Cover image bytes and type; books without any image get a generated SVG cover."""
        if book['cover']:
            with zipfile.ZipFile(book['path']) as z:
                data = z.read(book['cover'])
            lower = book['cover'].lower()
            mime = ('image/png' if lower.endswith('.png') else 'image/gif' if
                    lower.endswith('.gif') else 'image/webp' if lower.endswith('.webp') else
                    'image/jpeg')
            return data, mime
        return generated_cover(book['title'], ', '.join(book['authors'])), 'image/svg+xml'


def wrap_text(text, width, max_lines):
    lines, line = [], ''
    for word in text.split():
        if line and len(line) + 1 + len(word) > width:
            lines.append(line)
            line = word
        else:
            line = (line + ' ' + word).strip()
    if line:
        lines.append(line)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1][:width - 1].rstrip() + '…'
    return lines


def generated_cover(title, author):
    """A plain, high-contrast SVG cover (the reader's image loader handles SVG)."""
    shade = 30 + int(hashlib.sha1(title.encode()).hexdigest()[:2], 16) % 50
    title_lines = wrap_text(title, 14, 6)
    author_lines = wrap_text(author, 22, 2)
    y = 560 - len(title_lines) * 60
    texts = []
    for line in title_lines:
        texts.append('<text x="600" y="%d" font-size="110" font-weight="bold">%s</text>' % (
            y, html.escape(line)))
        y += 135
    y = max(y + 90, 1420)
    for line in author_lines:
        texts.append('<text x="600" y="%d" font-size="64">%s</text>' % (y, html.escape(line)))
        y += 85
    svg = ('<svg xmlns="http://www.w3.org/2000/svg" width="1200" height="1800" '
           'viewBox="0 0 1200 1800"><rect width="1200" height="1800" fill="hsl(0 0%% %d%%)"/>'
           '<rect x="60" y="60" width="1080" height="1680" fill="none" stroke="#fff" '
           'stroke-width="8"/><g fill="#fff" text-anchor="middle" '
           'font-family="serif">%s</g></svg>' % (shade, ''.join(texts)))
    return svg.encode()


class State:
    """Reading metadata (bookmarks, dogears, comments, tags) as JSON-patch documents.

    Protocol as observed on the device (firmware 5.18):
    - The device sends {"revision": <last seen>, "patches": [...]}. A new local document is an
      "add" on e.g. /publications/<uuid>/bookmark with a local "transientId" in the value.
    - The server must echo each add with the server id appended to the path
      (/publications/<uuid>/bookmark/<id>) and "transientId" + "revision" in the value; the
      device then records the server id and sends later changes as "replace" on the path
      with the id. Without the echo it re-sends the add on every sync.
    - The response also carries every document changed after the client's revision, so
      positions written by other clients reach the device.
    - Conflicts: last write wins by "modified".
    """

    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'reading-metadata.json'
        self.lock = threading.Lock()
        data = json.loads(self.path.read_text()) if self.path.exists() else {}
        self.revision = int(data.get('revision', 0))
        self.next_id = int(data.get('next_id', 1))
        # base path (without id) -> {"id", "revision", "value", "deleted"}
        self.docs = dict(data.get('docs', {}))
        for key in [k for k in self.docs if k.rpartition('/')[2] in COLLECTIONS]:
            doc = self.docs.pop(key)  # early versions stored list items under the list path
            self.docs['%s/%d' % (key, doc['id'])] = doc
        if 'documents' in data and not self.docs:  # migrate the first, id-less format
            for base, value in data['documents'].items():
                value = dict(value or {})
                value.pop('transientId', None)
                self.docs[base] = self._new_doc(value)

    def _new_doc(self, value):
        # "created": revision at creation; a reader that has not seen that revision gets the
        # document as an "add" (it does not know the id yet), later changes as "replace".
        doc = {'id': self.next_id, 'revision': self.revision, 'created': self.revision,
               'value': value, 'deleted': False}
        self.next_id += 1
        return doc

    def _save(self):
        self.path.write_text(json.dumps({'revision': self.revision, 'next_id': self.next_id,
                                         'docs': self.docs}, indent=1))

    def _create(self, list_path, value, **extra):
        self.revision += 1
        doc = self._new_doc(value)
        doc['created'] = doc['revision'] = self.revision
        doc.update(extra)
        key = '%s/%d' % (list_path, doc['id'])
        self.docs[key] = doc
        return key

    # ---- collections (tags with category "collection") -------------------------------------

    def collections(self):
        """{collection name: [(doc key, publication uuid)]} of live collection tags."""
        result = {}
        with self.lock:
            for key, doc in self.docs.items():
                value = doc.get('value') or {}
                parts = key.split('/')
                if (len(parts) == 5 and parts[3] == 'tags' and not doc.get('deleted')
                        and value.get('category') == 'collection' and value.get('name')):
                    result.setdefault(value['name'], []).append((key, parts[2]))
        return result

    def finished(self):
        """uuids of books marked as finished on a reader (a system tag, not the progress)."""
        with self.lock:
            return {key.split('/')[2] for key, doc in self.docs.items()
                    if '/tags/' in key and not doc.get('deleted')
                    and (doc.get('value') or {}).get('category') == 'system'
                    and doc['value'].get('name') == FINISHED_TAG}

    def auto_collections(self):
        """Names of live collections created from series metadata."""
        with self.lock:
            return {doc['value']['name'] for doc in self.docs.values()
                    if doc.get('auto_series') and not doc.get('deleted')}

    def _has_collection_tag(self, uuid, name):
        prefix = '/publications/%s/tags/' % uuid
        return any(key.startswith(prefix) and not doc.get('deleted')
                   and (doc.get('value') or {}).get('category') == 'collection'
                   and (doc.get('value') or {}).get('name') == name
                   for key, doc in self.docs.items())

    def add_to_collection(self, name, uuid):
        with self.lock:
            if not self._has_collection_tag(uuid, name):
                self._create('/publications/%s/tags' % uuid, {
                    'name': name, 'category': 'collection', 'modified': int(time.time() * 1000)})
                self._save()

    def remove_tag(self, key):
        with self.lock:
            doc = self.docs.get(key)
            if doc and not doc.get('deleted'):
                self.revision += 1
                doc.update(deleted=True, revision=self.revision)
                self._save()

    def rename_collection(self, old, new):
        now = int(time.time() * 1000)
        with self.lock:
            for doc in self.docs.values():
                value = doc.get('value') or {}
                if (not doc.get('deleted') and value.get('category') == 'collection'
                        and value.get('name') == old):
                    self.revision += 1
                    doc['value'] = dict(value, name=new, modified=now)
                    doc['revision'] = self.revision
            self._save()

    def update_series_collections(self, series_by_uuid):
        with self.lock:
            if self.ensure_series_collections(series_by_uuid):
                self._save()

    def ensure_series_collections(self, series_by_uuid):
        """Keep one auto collection tag per (book, series). Called with the lock held.

        A tag the user removed on the reader stays removed (its deleted doc is remembered).
        """
        now = int(time.time() * 1000)
        changed = False
        auto, live_name, kept = {}, {}, set()
        for key, doc in self.docs.items():
            series = doc.get('auto_series')
            if series:
                auto[(key.split('/')[2], series)] = doc
                if not doc.get('deleted'):
                    live_name[series] = doc['value']['name']
                if not doc.get('book_gone'):
                    kept.add(series)  # live, or deleted by the user
        for uuid, series in series_by_uuid.items():
            doc = auto.get((uuid, series))
            if doc is not None and not doc.get('book_gone'):
                continue
            if series in kept and series not in live_name:
                continue  # the user deleted the whole series collection: leave it deleted
            name = live_name.get(series, series)  # follow a rename
            if self._has_collection_tag(uuid, name):
                continue
            self.revision += 1
            if doc is not None:  # the book is back in the library
                doc.update(deleted=False, book_gone=False, revision=self.revision,
                           created=self.revision, value=dict(doc['value'], name=name,
                                                             modified=now))
            else:
                self._create('/publications/%s/tags' % uuid, {
                    'name': name, 'category': 'collection', 'modified': now}, auto_series=series)
            kept.add(series)
            live_name[series] = name
            changed = True
        for (uuid, series), doc in auto.items():
            if series_by_uuid.get(uuid) != series and not doc.get('deleted'):
                self.revision += 1
                doc.update(deleted=True, book_gone=True, revision=self.revision)
                changed = True
        return changed

    @staticmethod
    def doc_key(path):
        """Storage key for a patch path, or None for an add to a collection.

        The bookmark exists once per publication: /publications/<uuid>/bookmark[/<id>] maps
        to one key. tags, comments and dogears are lists: an add on .../tags creates a new
        document stored under .../tags/<id>.
        """
        head, _, last = path.rstrip('/').rpartition('/')
        if last.isdigit():
            parent = head.rpartition('/')[2]
            return path if parent in COLLECTIONS else head
        return None if last in COLLECTIONS else path

    @staticmethod
    def full_path(key, doc):
        return key if key.rpartition('/')[2].isdigit() else '%s/%d' % (key, doc['id'])

    def _apply(self, patch, echoes, touched):
        op, path, value = patch.get('op'), patch.get('path'), patch.get('value')
        if not path or op not in ('add', 'replace', 'remove'):
            print('warning: ignoring malformed patch %r' % (patch,))
            return False
        base = self.doc_key(path)
        if base is None and op != 'add':
            print('warning: %s on a collection without id: %r' % (op, path))
            return False
        doc = self.docs.get(base) if base else None
        if op == 'remove':
            touched.add(base)
            if doc is None or doc['deleted']:
                return False
            self.revision += 1
            doc.update(deleted=True, revision=self.revision)
            return True
        value = dict(value or {})
        transient = value.pop('transientId', None)
        changed = False
        if doc is None:
            self.revision += 1
            doc = self._new_doc(value)
            doc['revision'] = self.revision
            if base is None:  # add to a collection: the server id becomes part of the key
                base = '%s/%d' % (path.rstrip('/'), doc['id'])
            self.docs[base] = doc
            changed = True
        elif doc['deleted'] or value.get('modified', 0) > doc['value'].get('modified', 0):
            self.revision += 1
            doc.update(value=value, revision=self.revision, deleted=False)
            changed = True
        touched.add(base)
        full = self.full_path(base, doc)
        if op == 'add' and transient is not None:
            # transientId must be a JSON string: a number is read as 0 by the reader, which
            # then stores the echo as a new bookmark instead of confirming its own.
            echo = dict(doc['value'], transientId=str(transient))
            echoes[full] = {'op': 'add', 'path': full, 'value': echo}
        elif full not in echoes:
            # Echo the accepted (or, if the write lost, the newer server) state: the device only
            # clears its modifiedLocally flag when it gets the document back; otherwise it
            # re-sends the replace on every sync.
            echoes[full] = {'op': 'replace', 'path': full, 'value': dict(doc['value'])}
        return changed

    def sync(self, client_revision, patches, series_by_uuid=None):
        """Apply client patches; return (new revision, patches for the client)."""
        try:
            client_revision = int(client_revision)
        except (TypeError, ValueError):
            client_revision = 0
        with self.lock:
            echoes, touched = {}, set()
            changed = False
            for patch in patches:
                changed = self._apply(patch, echoes, touched) or changed
            if series_by_uuid is not None:
                changed = self.ensure_series_collections(series_by_uuid) or changed
            # The device ignores a response whose revision equals its own ("already up-to-date"):
            # id confirmations are then lost and accepted replaces are re-sent on every sync.
            # So any response to incoming patches carries a new revision.
            if patches and not changed:
                self.revision += 1
                changed = True
            if patches and self.revision <= client_revision:
                self.revision = client_revision + 1
                changed = True
            echoes = list(echoes.values())
            for echo in echoes:
                echo['value']['revision'] = str(self.revision)
            outgoing = []
            for base, doc in self.docs.items():
                if base in touched or doc['revision'] <= client_revision:
                    continue
                full = self.full_path(base, doc)
                # Bookmarks (one per book) go out as "replace" even when new: verified on the
                # device. List items (tags, ...) the reader has never seen must be an "add".
                new_to_client = (doc.get('created', 0) > client_revision
                                 and base.rpartition('/')[0].rpartition('/')[2] in COLLECTIONS)
                if doc['deleted']:
                    if not new_to_client:  # never sent to this reader: nothing to remove
                        outgoing.append({'op': 'remove', 'path': full})
                else:
                    outgoing.append({'op': 'add' if new_to_client else 'replace', 'path': full,
                                     'value': dict(doc['value'], revision=str(doc['revision']))})
            if changed or not self.path.exists():
                self._save()
            return str(self.revision), echoes + outgoing


class Handler(BaseHTTPRequestHandler):
    server_version = 'onilot/' + __version__
    routes = []

    # ---- plumbing -----------------------------------------------------------------------

    def _dispatch(self):
        cfg = self.server.cfg
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path.startswith(cfg.prefix):
            path = path[len(cfg.prefix):] or '/'
        self.query = urllib.parse.parse_qs(parsed.query)
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            length = -1
        if length < 0 or length > MAX_BODY:
            self.query, self.body, self.response_log = {}, b'', None
            self._send_json({'error': 'request too large (limit %d MB)' % (MAX_BODY >> 20)}, 413)
            self.close_connection = True
            self._log(path)
            return
        self.body = self.rfile.read(length) if length else b''
        self.response_log = None
        self.user = self._token_user()
        if self.user and self.headers.get('Hardware-Id'):
            self.ctx.seen_device(self.headers)
        for method, pattern, func, guard in self.routes:
            m = pattern.fullmatch(path)
            if m and method == self.command:
                try:
                    if guard == 'token' and not self.user:
                        self._send_json({'error': 'unauthorized'}, 401)
                    elif guard == 'admin' and not self._admin_session():
                        self._send(303, b'', 'text/plain', {'Location': self._admin_url()})
                    elif guard == 'signed' and not self._has_valid_signature(*m.groups()):
                        self._send_json({'error': 'bad or expired signature'}, 403)
                    else:
                        func(self, *m.groups())
                except Exception as exc:  # report and keep serving; this is a test server
                    traceback.print_exc()
                    self._send_json({'error': str(exc)}, 500)
                finally:
                    self._log(path)
                return
        self._send_json({'error': 'not implemented', 'path': path}, 404)
        self._log(path)

    do_GET = do_POST = do_PATCH = do_PUT = do_DELETE = _dispatch

    def _log(self, path):
        if self.headers.get('Content-Type', '').startswith('multipart/'):
            body = '<multipart, %d bytes>' % len(self.body)
        else:
            body = redact(self.body[:LOG_BODY_LIMIT].decode('utf-8', 'replace'))
        response = dict(self.response_log or {})
        if 'body' in response:
            response['body'] = redact(response['body'])
        entry = {
            'time': datetime.datetime.now().isoformat(timespec='seconds'),
            'client': self.client_address[0], 'method': self.command, 'path': path,
            'query': self.query,
            'headers': {k: redact(v) for k, v in self.headers.items()},
            'body': body,
            'response': response,
        }
        if self.server.cfg.request_log:
            with self.server.log_lock, open(self.server.cfg.log_file, 'a') as f:
                f.write(json.dumps(entry) + '\n')
        status = (self.response_log or {}).get('status')
        print('%s %s %s -> %s' % (entry['time'], self.command, path, status))

    def _send(self, status, body, content_type, extra_headers=None):
        self.send_response(status)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', str(len(body)))
        for k, v in (extra_headers or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if body:
            self.wfile.write(body)
        shown = body[:LOG_BODY_LIMIT].decode('utf-8', 'replace') \
            if content_type.startswith(('application/json', 'text/')) else '<%d bytes>' % len(body)
        self.response_log = {'status': status, 'content_type': content_type, 'body': shown}

    def _send_json(self, obj, status=200):
        self._send(status, json.dumps(obj).encode(), 'application/json; charset=UTF-8')

    def _url(self, suffix):
        return self.server.cfg.public_base + suffix

    def _signed_url(self, kind, uuid):
        # Content downloads carry no access token (only Hardware-Id/Reseller-Id/User-Agent),
        # so book and cover URLs are capability URLs: HMAC over uuid + expiry.
        # Signature in the path, not the query: the device appends its own query to cover
        # URLs ("?size=WS-B04"), which would corrupt a query-string signature.
        # Covers never expire: the device stores frontCoverUrl permanently and re-fetches it
        # later. Book downloads expire (fresh downloadinfo is requested per download).
        # The URL names the user and the signature covers it, so a link only opens that
        # user's books.
        expires = COVER_NEVER_EXPIRES if kind == 'covers' else int(time.time()) + SIGNED_URL_TTL
        sig = self.server.sign('%s:%s:%s:%d' % (kind, self.user, uuid, expires))
        return self._url('/%s/%s/%s/%d/%s' % (kind, self.user, uuid, expires, sig))

    def _has_valid_signature(self, kind, *rest):
        if len(rest) == 4:
            user, uuid, expires, sig = rest
            message = '%s:%s:%s:%d' % (kind, user, uuid, int(expires))
        else:  # single-user era URL, still stored on readers (covers never expire)
            uuid, expires, sig = rest
            user = self.server.users.legacy_user()
            message = '%s:%s:%d' % (kind, uuid, int(expires))
        if not user or self.server.users.get(user) is None:
            return False
        self.user = user
        return int(expires) >= time.time() and hmac.compare_digest(
            self.server.sign(message), sig)

    def _token_user(self):
        # Readers send the token as "Authorization: Bearer" or "t_auth_token", so accept an
        # issued token in any header value.
        jwt_like = re.compile(r'[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+')
        for value in self.headers.values():
            for tok in jwt_like.findall(value):
                user = self.server.access_user(tok)
                if user:
                    return user
        return None

    @property
    def ctx(self):
        return self.server.context(self.user)

    def _admin_url(self, suffix=''):
        return self.server.cfg.origin + '/admin' + suffix

    def _redirect(self, path):
        self._send(303, b'', 'text/plain', {'Location': self._url(path)})

    def _admin_session(self):
        cookie = self.headers.get('Cookie', '')
        sid = next((c.split('=', 1)[1] for c in cookie.split('; ')
                    if c.startswith('onilot_admin=')), None)
        session = self.server.admin_session(sid)
        if session:
            self.admin, self.csrf = session
        return bool(session)

    def log_message(self, fmt, *args):
        pass  # replaced by _log

    def _form(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.body.decode()).items()}

    def _html(self, page, status=200, extra_headers=None):
        self._send(status, page.encode(), 'text/html; charset=UTF-8', extra_headers)


def route(method, regex, guard=None):
    """guard: None (public), 'token' (access token), 'signed' (signed URL), 'admin'."""
    def wrap(func):
        Handler.routes.append((method, re.compile(regex), func, guard))
        return func
    return wrap


# ---- setup: region / reseller / config ----------------------------------------------------

@route('GET', r'/healthz')
def healthz(h):
    h._send(200, b'ok', 'text/plain')


@route('GET', r'/online-check')
def online_check(h):
    h._send(204, b'', 'text/plain')


@route('GET', r'/v1/reseller-group/devices/([^/]+)/countries')
def countries(h, hardware_id):
    # Country list for the reader's setup screen.
    lang = h.query.get('languageCode', ['de'])[0]
    code = h.server.cfg.country
    h._send_json({'resellerCountryResponse': {'resellerCountryGroups': [{
        'languageCode': lang,
        'resellerCountries': [{'country': COUNTRY_NAMES.get(code, code), 'countryCode': code}],
    }]}})


@route('GET', r'/v1/reseller-group/devices/([^/]+)/resellers')
def resellers(h, hardware_id):
    # Reseller (shop) list for setup. With a single entry the reader skips the selection
    # screen. Item keys beyond resellerId are not confirmed; extra keys are harmless.
    brand = h.server.cfg.brand
    item = {'resellerId': RESELLER_ID, 'resellerDisplayName': brand,
            'logoUrl': h._url('/static/logo.png'),
            'id': RESELLER_ID, 'name': brand, 'displayName': brand}
    h._send_json({'resellerSelectionResponse': {'resellerSelection': [item]}})


@route('GET', r'/v1/resellers/([^/]+)/client-types/([^/]+)/config')
def reseller_config(h, reseller_id, client_type):
    base = h.server.cfg.public_base
    h._send_json({
        'reseller_id': RESELLER_ID,
        'config': {
            # The device appends "&client_id=<OAUTH_CLIENT_ID>", so this URL needs a query.
            'URL_OAUTH_AUTHORIZATION': base + '/oauth/authorize?response_type=code',
            'URL_OAUTH_ACCESSTOKEN': base + '/oauth/token',
            'URL_OAUTH_REVOKETOKEN': base + '/oauth/revoke',
            'OAUTH_CLIENT_ID': CLIENT_ID,
            'STRING_BRAND_NAME': h.server.cfg.brand,
            'URL_RESELLER_LOGO': base + '/static/logo.png',
            # Settings > account: "Link Library" and "Device Management" webviews.
            'URL_HANDSHAKE': base + '/manage/library',
            'URL_DEVICE_MANAGEMENT': base + '/manage/devices',
            'SHOP_BASE': base + '/shop',
            'URL_SHOP_EBOOK_START_PAGE': base + '/shop',
            'URL_SHOP_EBOOK_SEARCH': base + '/shop?q=%s',
            'FAMILY_SHARING_ACTIVE': False,
            'KDRM_ACTIVATED': False,
            'LCP_ACTIVATED': False,
            'METRICS_ACTIVATED': False,
            'IS_ONLEIHE_LINK_ENABLED': False,
            'IS_EXTERNAL_LINK_ENABLED': False,
            'RECOMMENDATIONS_IS_SHUFFLE_ENABLED': False,
        },
    })


# ---- login --------------------------------------------------------------------------------

LOGIN_PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width">
<title>{brand} login</title></head><body style="font-family:sans-serif;font-size:1.4em">
<h1>{brand}</h1><form method=post>
<p>User<br><input name=username autocapitalize=none autocorrect=off style="font-size:1em"></p>
<p>Password<br><input type=password name=password style="font-size:1em"></p>
<p><button style="font-size:1em">Sign in</button></p></form>{message}</body></html>"""


@route('GET', r'/oauth/authorize')
def authorize_page(h):
    h._send(200, LOGIN_PAGE.format(brand=html.escape(h.server.cfg.brand), message='').encode(), 'text/html; charset=UTF-8')


@route('POST', r'/oauth/authorize')
def authorize_submit(h):
    form = h._form()
    user = h.server.users.authenticate(form.get('username'), form.get('password'))
    if user is None:
        time.sleep(1)  # slow down guessing
        h._send(200, LOGIN_PAGE.format(brand=html.escape(h.server.cfg.brand),
                                       message='<p>Wrong user or password.</p>').encode(),
                'text/html; charset=UTF-8')
        return
    code = secrets.token_urlsafe(16)
    h.server.codes[code] = (user, time.time() + CODE_TTL)
    h._send(302, b'', 'text/plain', {'Location': 'epublishing://login?code=' + code})


@route('POST', r'/oauth/token')
def token(h):
    form = h._form()
    grant = form.get('grant_type')
    if grant == 'authorization_code':
        user, expires = h.server.codes.pop(form.get('code'), (None, 0))
        user = user if expires > time.time() else None
    elif grant == 'refresh_token':
        user = h.server.take_refresh_token(form.get('refresh_token'))
    else:
        user = None
    if not user or h.server.users.get(user) is None:
        h._send_json({'error': 'invalid_grant'}, 400)
        return
    access, refresh = h.server.issue_tokens(user)
    h._send_json({'access_token': access, 'refresh_token': refresh,
                  'expires_in': ACCESS_TOKEN_TTL, 'token_type': 'Bearer'})


@route('POST', r'/oauth/revoke')
def revoke(h):
    h._send(200, b'', 'text/plain')


@route('POST', r'/v1/devices')
def register_device(h):
    h._send_json({'initAppResponse': {'userId': h.user or '', 'deviceKey': CLIENT_ID + '-device'}})


@route('GET', r'/v1/devices/([^/]+)/userid')
def device_user_id(h, hardware_id):
    # Format not confirmed; the reader accepts this during setup.
    h._send_json({'userId': h.user or ''})


# ---- library -------------------------------------------------------------------------------

def added_dates(books):
    """uuid -> the "purchasedDate" sent to the reader, which stores it as the book's date added.

    The reader cannot sort by series, but it can sort by date added (newest first). So each
    series gets the date of its newest file, minus the book's position in seconds: sorted by
    date added, a series stays together and in reading order (#1 first).
    """
    newest = {}
    for b in books:
        if b['series']:
            newest[b['series']] = max(newest.get(b['series'], 0), b['mtime'])
    dates = {}
    for b in books:
        if b['series']:
            position = b['series_index'] if b['series_index'] is not None else 1000
            dates[b['uuid']] = newest[b['series']] - round(position * 1000)
        else:
            dates[b['uuid']] = b['mtime']
    return dates


def content_item(h, book, added):
    return {
        'uuid': book['uuid'],
        'publicationId': book['uuid'],
        'transactionId': book['uuid'],
        'title': book['title'],
        # Must be a MIME type: the reader uses it as the book's file type. With "EPUB" it
        # never recognized the book as EPUB and never saved reading positions.
        'contentFormat': 'application/epub+zip',
        'contentType': 'EBOOK',
        'contentSources': ['USER_UPLOAD' if h.ctx.is_upload(book) else 'PURCHASED'],
        'contentIsPreview': False,
        'language': book['language'],
        'publisher': book['publisher'],
        'purchasedDate': added,
        'resellerId': RESELLER_ID,
        'resellerName': h.server.cfg.brand,
        'authors': [{'name': a, 'type': 'AUTHOR'} for a in book['authors']],
        'fileResources': [{'type': 'COVER_IMAGE',
                           'resource': h._signed_url('covers', book['uuid'])}],
        'readableContentInfo': {'renderingEngine': 'READIUM_1'},
    }


@route('GET', r'/v8/inventory', 'token')
def inventory(h):
    books = h.ctx.library.books()
    size = int(h.query.get('size', ['100'])[0])
    page = int(h.query.get('page', ['0'])[0])
    chunk = books[page * size:(page + 1) * size]
    dates = added_dates(books)
    deleted = h.server.deletions_for(h.user)
    present = {b['uuid'] for b in books}
    deleted = {u: ts for u, ts in deleted.items() if u not in present}
    last_modified = max([b['mtime'] for b in books] + list(deleted.values())
                        + list(h.ctx.shelf.values()) + [h.server.series.modified()] or [0])
    # The device sends back lastModifiedDate as If-Modified-Since. Answering 200 every time
    # makes it re-import every book, which resets read status and position (seen on device).
    since = h.headers.get('If-Modified-Since')
    if since:
        try:
            since_s = email.utils.parsedate_to_datetime(since).timestamp()
        except (TypeError, ValueError):
            since_s = None
        if since_s is not None and last_modified // 1000 <= since_s:
            h._send(304, b'', 'application/json; charset=UTF-8')
            return
    h._send_json({
        'page': {'content': [content_item(h, b, dates[b['uuid']]) for b in chunk],
                 'numberOfElements': len(chunk), 'totalElements': len(books),
                 'number': page, 'size': size},
        # Books deleted on the server; the reader removes them from its library.
        'deletedContent': [{'uuid': u} for u in sorted(deleted)] if page == 0 else [],
        'lastModifiedDate': last_modified,
    })


@route('GET', r'/v1/inventory/([^/]+)/downloadinfo', 'token')
def download_info(h, uuid):
    book = h.ctx.library.find(uuid)
    if book is None:
        h._send_json({'error': 'unknown book'}, 404)
        return
    # "format" must be a MIME type: the device copies it into content.MimeType, and with
    # "EPUB" the reader engine never saved positions (verified on device, 2026-10-11).
    h._send_json({'contentUrl': h._signed_url('content', uuid), 'format': 'application/epub+zip',
                  'fileSize': book['size']})


@route('GET', r'/(content)/([a-z0-9._-]+)/([0-9a-f]{40})/(\d+)/([0-9a-f]{64})', 'signed')
@route('GET', r'/(content)/([0-9a-f]{40})/(\d+)/([0-9a-f]{64})', 'signed')
def content(h, kind, *rest):
    uuid = rest[-3]
    book = h.ctx.library.find(uuid)
    if book is None:
        h._send_json({'error': 'unknown book'}, 404)
        return
    h._send(200, book['path'].read_bytes(), 'application/epub+zip')


@route('GET', r'/(covers)/([a-z0-9._-]+)/([0-9a-f]{40})/(\d+)/([0-9a-f]{64})', 'signed')
@route('GET', r'/(covers)/([0-9a-f]{40})/(\d+)/([0-9a-f]{64})', 'signed')
def cover(h, kind, *rest):
    uuid = rest[-3]
    book = h.ctx.library.find(uuid) or h.server.shared_library().find(uuid)
    data, mime = h.ctx.library.cover_bytes(book) if book else (None, None)
    if data is None:
        h._send_json({'error': 'no cover'}, 404)
        return
    h._send(200, data, mime)


# ---- shop tab (webview) -----------------------------------------------------------------

# E-paper friendly: no scrolling. The script measures how many books fit on the screen and
# pages through them with large buttons; without the script the list simply scrolls.
STORE_CSS = """
* { box-sizing:border-box; }
html, body { height:100%; }
body { margin:0; font:18px/1.35 sans-serif; color:#000; background:#fff; display:flex;
  flex-direction:column; }
header { padding:.6em .8em; border-bottom:2px solid #000; display:flex; gap:.8em;
  align-items:center; flex-wrap:wrap; flex:none; }
header h1 { font-size:1.25em; margin:0; flex:1; white-space:nowrap; }
form.search { display:flex; gap:.4em; } input[type=search] { font:inherit; padding:.3em .5em;
  border:2px solid #000; border-radius:6px; width:10em; }
.note { border:2px solid #000; border-radius:8px; padding:.5em .7em; margin:.6em .8em 0;
  flex:none; }
main { flex:1; padding:0 .8em; display:grid; align-content:start;
  grid-template-columns:repeat(auto-fill, minmax(15em, 1fr)); column-gap:1em; }
.paged main { overflow:hidden; }
.book { display:flex; gap:.8em; padding:.7em 0; border-bottom:1px solid #000; height:8.8em;
  overflow:hidden; }
.paged .book.off { display:none; }
.book img, .book .ph { width:4.8em; height:7.2em; object-fit:cover; border:1px solid #000;
  flex:none; display:grid; place-items:center; font-size:1em; font-weight:700; }
.book .ph { font-size:2em; }
.book div { min-width:0; display:flex; flex-direction:column; }
.book h2 { font-size:1em; margin:0; max-height:2.7em; overflow:hidden; }
.by, .series { margin:.1em 0 0; max-height:1.35em; overflow:hidden; } .series { font-style:italic; }
.book form { margin-top:auto; display:flex; align-items:center; gap:.5em; flex-wrap:wrap; }
button { font:inherit; padding:.3em .8em; border:2px solid #000; border-radius:8px;
  background:#fff; color:#000; } button.add { background:#000; color:#fff; }
button:disabled { border-style:dashed; color:#777; }
.owned { font-weight:700; } .empty { padding:2em 0; text-align:center; grid-column:1/-1; }
nav { display:none; flex:none; border-top:2px solid #000; padding:.5em .8em;
  align-items:center; gap:.8em; }
.paged nav { display:flex; }
nav button { flex:1; padding:.6em; font-size:1.1em; font-weight:700; }
nav span { min-width:7em; text-align:center; }
a { color:#000; }
.tabs { display:flex; gap:.4em; } .tabs a { padding:.25em .7em; border:2px solid #000;
  border-radius:99px; text-decoration:none; } .tabs a.on { background:#000; color:#fff; }
.heading { display:flex; align-items:center; gap:.8em; margin:.6em .8em 0; flex:none; }
.heading h2 { font-size:1.15em; margin:0; flex:1; }
"""

STORE_JS = """
(function () {
  var main = document.querySelector('main');
  var books = [].slice.call(main.querySelectorAll('.book'));
  if (!books.length) return;
  document.body.className = 'paged';
  var cols = books.filter(function (b) { return b.offsetTop === books[0].offsetTop; }).length;
  var rows = Math.max(1, Math.floor(main.clientHeight / books[0].offsetHeight));
  var per = cols * rows, pages = Math.ceil(books.length / per), page = 0;
  var focus = document.getElementById('focus');
  if (focus) page = Math.floor(books.indexOf(focus) / per);
  var prev = document.getElementById('prev'), next = document.getElementById('next');
  function show() {
    books.forEach(function (b, i) {
      b.className = Math.floor(i / per) === page ? 'book' : 'book off';
    });
    document.getElementById('pageno').textContent = 'Page ' + (page + 1) + ' of ' + pages;
    prev.disabled = page === 0;
    next.disabled = page >= pages - 1;
  }
  prev.onclick = function () { if (page > 0) { page--; show(); } };
  next.onclick = function () { if (page < pages - 1) { page++; show(); } };
  show();
})();
"""


def series_of(library):
    return {b['uuid']: b['series'] for b in library.books() if b['series']}


def series_label(book):
    if not book.get('series'):
        return ''
    index = book.get('series_index')
    if index is None:
        return book['series']
    return '%s #%s' % (book['series'], ('%g' % index))


STORE_VIEWS = (('', 'Books'), ('series', 'Series'))


def store_state(source):
    """The store page a request came from or goes back to: query, view, series."""
    def get(key):
        value = source.get(key, '')
        return value[0] if isinstance(value, list) else value
    return {k: get(k).strip()[:200] for k in ('q', 'view', 'series')}


def store_token(h):
    return next((v for k, v in h.headers.items() if k.lower() == 'x-access-token'), '')


def store_form(h, path, state, label, css=''):
    hidden = ''.join('<input type=hidden name=%s value="%s">' % (k, html.escape(v))
                     for k, v in state.items() if v)
    return ('<form method=post action="%s"><input type=hidden name=t value="%s">%s'
            '<button class="%s">%s</button></form>' % (
                html.escape(h._url(path)), html.escape(store_token(h)), hidden, css, label))


def store_cover(h, book):
    if h.user:
        return '<img src="%s" alt="">' % html.escape(h._signed_url('covers', book['uuid']))
    return '<span class="ph">%s</span>' % html.escape(book['title'][:1])


def book_card(h, b, state, focus):
    action = ''
    if h.user:
        owned = b['uuid'] in h.ctx.shelf
        action = store_form(h, '/shop/%s/%s' % ('remove' if owned else 'add', b['uuid']), state,
                            'Remove' if owned else 'Add', '' if owned else 'add')
        if owned:
            action = action.replace('<button', '<span class="owned">In your library</span>'
                                    '<button', 1)
    series = series_label(b)
    if series and not state['series']:
        series = '<a href="%s">%s</a>' % (html.escape(h._url('/shop?' + urllib.parse.urlencode(
            {'series': b['series']}))), html.escape(series))
    else:
        series = html.escape(series)
    return ('<div class="book"%s>%s<div><h2>%s</h2><p class="by">%s</p>%s%s</div></div>' % (
        ' id="focus"' if b['uuid'] == focus else '', store_cover(h, b), html.escape(b['title']),
        html.escape(', '.join(b['authors'])),
        '<p class="series">%s</p>' % series if series else '', action))


def series_card(h, name, books):
    owned = sum(1 for b in books if h.user and b['uuid'] in h.ctx.shelf)
    link = html.escape(h._url('/shop?' + urllib.parse.urlencode({'series': name})))
    authors = sorted({a for b in books for a in b['authors']})
    return ('<div class="book">%s<div><h2><a href="%s">%s</a></h2><p class="by">%s</p>'
            '<p class="series">%d book%s%s</p><form method=get action="%s">'
            '<input type=hidden name=series value="%s"><button>Open</button></form></div></div>'
            % (store_cover(h, books[0]), link, html.escape(name),
               html.escape(', '.join(authors)), len(books), '' if len(books) == 1 else 's',
               ', %d in your library' % owned if owned else '', html.escape(h._url('/shop')),
               html.escape(name)))


def store_page(h, cards, state, title=None, actions=''):
    brand = html.escape(h.server.cfg.brand)
    tabs = ''.join('<a href="%s"%s>%s</a>' % (
        html.escape(h._url('/shop' + ('?view=' + view if view else ''))),
        ' class="on"' if view == state['view'] and not state['series'] else '', label)
        for view, label in STORE_VIEWS)
    message = h.query.get('msg', [''])[0]
    notes = ('<div class="note">%s</div>' % html.escape(message)) if message else ''
    if not h.user:
        notes += '<div class="note">Open the store on your reader to add books.</div>'
    if title:
        notes += '<div class="heading"><h2>%s</h2>%s</div>' % (html.escape(title), actions)
    doc = """<!doctype html><html><head><meta charset="utf-8">
<meta name=viewport content="width=device-width"><title>{brand}</title><style>{css}</style>
</head><body><header><h1>{brand} store</h1><div class="tabs">{tabs}</div>
<form class="search" action="{action}"><input type=hidden name=view value="{view}">
<input type=search name=q value="{q}" placeholder="Title, author, series"><button>Search</button>
</form></header>{notes}<main>{cards}</main>
<nav><button id="prev">&lsaquo; Previous</button><span id="pageno"></span>
<button id="next">Next &rsaquo;</button></nav>
<script>{js}</script>
</body></html>""".format(brand=brand, css=STORE_CSS, js=STORE_JS, tabs=tabs,
                         action=html.escape(h._url('/shop')), q=html.escape(state['q']),
                         view=html.escape(state['view']), notes=notes,
                         cards=''.join(cards) or '<div class="empty">Nothing found.</div>')
    h._send(200, doc.encode(), 'text/html; charset=UTF-8')


# The shop tab is the store for the shared library: each user adds the shared books they want;
# only those (plus their own books) appear in their library. The reader's shop webview sends
# the access token as X-Access-Token, so the store knows the user.
# Some shop entry points concatenate SHOP_BASE + URL_SHOP_EBOOK_START_PAGE, producing
# /shophttps://<host>/<prefix>/shop, so accept anything after /shop.
def store_order(books):
    """Newest first; a series stays together, in reading order, at its newest book."""
    newest = {}
    for b in books:
        if b['series']:
            newest[b['series']] = max(newest.get(b['series'], 0), b['mtime'])

    def key(b):
        if b['series']:
            return (-newest[b['series']], b['series'], b['series_index'] or 0, b['title'])
        return (-b['mtime'], '', 0, b['title'])
    return sorted(books, key=key)


def matches(book, needle):
    return (not needle or needle in book['title'].lower()
            or any(needle in a.lower() for a in book['authors'])
            or needle in (book['series'] or '').lower())


@route('GET', r'/shop(?:https?://.*)?')
def shop(h):
    state = store_state(h.query)
    focus = h.query.get('id', [''])[0]
    needle = state['q'].lower()
    books = [b for b in store_order(h.server.shared_library().books()) if matches(b, needle)]
    if state['series']:
        books = sorted((b for b in books if b['series'] == state['series']),
                       key=lambda b: (b['series_index'] is None, b['series_index'] or 0,
                                      b['title'].lower()))
        missing = [b for b in books if h.user and b['uuid'] not in h.ctx.shelf]
        actions = store_form(h, '/shop/add-series', state, 'Add all %d' % len(missing),
                             'add') if missing else ''
        store_page(h, [book_card(h, b, state, focus) for b in books], state,
                   title=state['series'], actions=actions)
    elif state['view'] == 'series':
        groups = {}
        for b in books:
            if b['series']:
                groups.setdefault(b['series'], []).append(b)
        cards = [series_card(h, name, sorted(group, key=lambda b: b['series_index'] or 0))
                 for name, group in sorted(groups.items(), key=lambda kv: kv[0].lower())]
        store_page(h, cards, state)
    else:
        store_page(h, [book_card(h, b, state, focus) for b in books], state)


def store_user(h, form):
    # The token arrives as a header on the reader; the form copy is a fallback in case the
    # webview does not add it to form posts.
    user = h.user or h.server.access_user(form.get('t', ''))
    if user:
        h.user = user
    return user


def back_to_store(h, form, message, uuid=''):
    state = {k: v for k, v in store_state(form).items() if v}
    state.update({'msg': message, **({'id': uuid} if uuid else {})})
    h._send(303, b'', 'text/plain', {'Location': h._url('/shop?' + urllib.parse.urlencode(state))})


SYNC_HINT = 'after the next sync (Settings > account > Link Library > Sync now)'


@route('POST', r'/shop/(add|remove)/([0-9a-f]{40})')
def shop_action(h, verb, uuid):
    form = h._form()
    if not store_user(h, form):
        h._send_json({'error': 'unauthorized'}, 401)
        return
    book = h.server.shared_library().find(uuid)
    if book is None:
        message = 'That book is no longer in the store.'
    elif verb == 'add':
        h.ctx.add_to_shelf(uuid)
        message = 'Added "%s". It appears in your library %s.' % (book['title'], SYNC_HINT)
    else:
        h.ctx.remove_from_shelf(uuid)
        message = 'Removed "%s"; it leaves your reader at the next sync.' % book['title']
    back_to_store(h, form, message, uuid)


@route('POST', r'/shop/add-series')
def shop_add_series(h):
    form = h._form()
    if not store_user(h, form):
        h._send_json({'error': 'unauthorized'}, 401)
        return
    name = form.get('series', '')
    books = [b for b in h.server.shared_library().books()
             if b['series'] == name and b['uuid'] not in h.ctx.shelf]
    for b in books:
        h.ctx.add_to_shelf(b['uuid'])
    message = ('Added %d books of %s. They appear in your library %s.' % (len(books), name,
                                                                          SYNC_HINT)
               if books else 'You already have every book of %s.' % name)
    back_to_store(h, form, message)


# ---- uploads of sideloaded books (device pushes them automatically) ----------------------

def parse_multipart_file(content_type, body, field='file'):
    """Return (filename, bytes) of a multipart/form-data field, or (None, None)."""
    msg = email.parser.BytesParser(policy=email.policy.HTTP).parsebytes(
        b'Content-Type: ' + content_type.encode() + b'\r\n\r\n' + body)
    if not msg.is_multipart():
        return None, None
    for part in msg.iter_parts():
        if part.get_param('name', header='content-disposition') == field:
            return part.get_filename(), part.get_payload(decode=True)
    return None, None


def store_upload(library_dir, filename, data):
    """Save into the library; same content under the same name is reused. Returns the Path."""
    name = Path(filename or 'upload.epub').name
    while name.lower().endswith('.epub.epub'):  # the device doubles the extension
        name = name[:-5]
    if not name.lower().endswith('.epub'):
        name += '.epub'
    stem, n = name[:-5], 1
    target = Path(library_dir) / name
    while target.exists() and target.read_bytes() != data:
        n += 1
        target = Path(library_dir) / ('%s-%d.epub' % (stem, n))
    if not target.exists():
        target.write_bytes(data)
    return target


@route('POST', r'/v1/inventory/user-uploads', 'token')
def user_upload(h):
    filename, data = parse_multipart_file(h.headers.get('Content-Type', ''), h.body)
    if not data or not data.startswith(b'PK'):
        h._send_json({'error': 'expected an EPUB in multipart field "file"'}, 400)
        return
    path = store_upload(h.ctx.library_dir, filename, data)
    h.ctx.mark_uploaded(path.name)
    book = h.ctx.library.find(hashlib.sha1(path.name.encode()).hexdigest())
    # Upload confirmation; "format" becomes the book's file type on the reader (MIME type).
    h._send_json({'metadata': {
        'inventoryUuid': book['uuid'],
        'title': book['title'],
        'subtitle': '',
        'language': book['language'] or '',
        'isbn': '',
        'publisher': book['publisher'] or '',
        'issued': 0,
        'format': 'application/epub+zip',
        'authors': ', '.join(book['authors']),
    }})


@route('POST', r'/v1/inventory/user-uploads/([^/]+)/cover', 'token')
def user_upload_cover(h, uuid):
    # Covers are taken from the EPUB itself; accept and ignore the uploaded image.
    h._send_json({})


# ---- home screen recommendations ---------------------------------------------------------

@route('GET', r'/v2/recommendations', 'token')
def recommendations(h):
    count = int(h.query.get('count', ['12'])[0])
    # Suggest shared books the user has not added yet; tapping one opens it in the store.
    books = [b for b in sorted(h.server.shared_library().books(), key=lambda b: -b['mtime'])
             if b['uuid'] not in h.ctx.shelf][:count]
    # Top-level array; tapping an entry opens shop_url in the shop webview.
    h._send_json([{
        'publicationId': b['uuid'],
        'title': b['title'],
        'author': ', '.join(b['authors']),
        'cover_url': h._signed_url('covers', b['uuid']),
        'description': '',
        'publicationType': h.query.get('type', ['ebook'])[0],
        'shop_url': h._url('/shop?id=' + b['uuid']),
    } for b in books])


# ---- account pages (Settings > account) -----------------------------------------------------
#
# The reader opens URL_HANDSHAKE ("Link Library") and URL_DEVICE_MANAGEMENT ("Device
# Management") in a webview and injects
#   window.handshake_config = {token, wdycf: "handshakeCancel", libjump: "handshakeFinish",
#                              deviceid}
# Navigating to any URL containing "handshakeFinish" closes the dialog and starts a sync;
# "handshakeCancel" just closes it. The pages call the JSON API below with the token.

MANAGE_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name=viewport content="width=device-width"><title>{brand}</title>
<style>
body {{ font-family: sans-serif; font-size: 1.3em; margin: 1em; }}
button {{ font-size: 1em; padding: .4em 1em; margin: .3em .3em .3em 0; }}
li {{ margin: .4em 0; }} .muted {{ color: #555; font-size: .85em; }}
</style></head><body>
<h1>{title}</h1><div id="content"><p class="muted">Loading…</p></div>
<p>{buttons}<button onclick="leave('handshakeCancel')">Close</button></p>
<script>
var cfg = window.handshake_config || {{}};
var api = {api};
function leave(what) {{ location.href = api + '/manage/' + (cfg[what === 'handshakeFinish'
  ? 'libjump' : 'wdycf'] || what); }}
function esc(t) {{ var d = document.createElement('div'); d.textContent = t; return d.innerHTML; }}
function call(method, path) {{
  return fetch(api + path, {{method: method, headers: {{'Authorization': 'Bearer ' + cfg.token}}}})
    .then(function (r) {{ if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); }});
}}
function fail(e) {{ document.getElementById('content').innerHTML =
  '<p>Could not load (' + esc(e.message) + '). Open this page from the reader settings.</p>'; }}
{script}
</script></body></html>"""

LIBRARY_SCRIPT = """
call('GET', '/onilot/library').then(function (d) {
  var items = d.books.map(function (b) {
    return '<li>' + esc(b.title) + (b.authors ? ' <span class="muted">' + esc(b.authors) +
      '</span>' : '') + (b.uploaded ? ' <span class="muted">(uploaded)</span>' : '') +
      (b.shared ? ' <span class="muted">(shared)</span>' : '') + '</li>';
  }).join('');
  document.getElementById('content').innerHTML = '<p>Signed in as <b>' + esc(d.user) +
    '</b>. ' + d.books.length + ' books (yours and shared). "Sync now" brings new ones into ' +
    'your library.</p><ul>' +
    items + '</ul>';
}, fail);
"""

DEVICES_SCRIPT = """
function fmt(t) { return new Date(t * 1000).toLocaleString(); }
function load() {
  call('GET', '/onilot/devices').then(function (d) {
    var items = d.devices.map(function (x) {
      return '<li>' + esc(x.client_type || 'unknown device') + ' ' + esc(x.client_version) +
        (x.id === cfg.deviceid ? ' <b>(this reader)</b>' : '') +
        '<br><span class="muted">' + esc(x.id) + ' · last seen ' + fmt(x.last_seen) +
        '</span></li>';
    }).join('');
    document.getElementById('content').innerHTML = '<p>Your readers:</p>' +
      '<ul>' + items + '</ul>';
  }, fail);
}
function signOut() {
  if (!confirm('Sign out all your readers? Each has to sign in again.')) return;
  call('POST', '/onilot/devices/sign-out').then(function () { leave('handshakeCancel'); }, fail);
}
load();
"""


def manage_page(h, title, script, buttons=''):
    page = MANAGE_PAGE.format(brand=html.escape(h.server.cfg.brand), title=html.escape(title),
                              api=json.dumps(h.server.cfg.public_base), script=script,
                              buttons=buttons)
    h._send(200, page.encode(), 'text/html; charset=UTF-8')


@route('GET', r'/manage/library')
def manage_library(h):
    manage_page(h, 'Library', LIBRARY_SCRIPT,
                '<button onclick="leave(\'handshakeFinish\')">Sync now</button>')


@route('GET', r'/manage/devices')
def manage_devices(h):
    manage_page(h, 'Devices', DEVICES_SCRIPT,
                '<button onclick="signOut()">Sign out my readers</button>')


@route('GET', r'/manage/(handshakeFinish|handshakeCancel)')
def manage_leave(h, what):
    # Normally intercepted by the reader; reached only from a desktop browser.
    h._send(200, b'You can close this page.', 'text/plain; charset=UTF-8')


@route('GET', r'/onilot/library', 'token')
def api_library(h):
    h._send_json({'user': h.user, 'books': [{
        'uuid': b['uuid'], 'title': b['title'], 'authors': ', '.join(b['authors']),
        'uploaded': h.ctx.is_upload(b), 'shared': b['shared'],
    } for b in sorted(h.ctx.library.books(), key=lambda b: b['title'].lower())]})


@route('GET', r'/onilot/devices', 'token')
def api_devices(h):
    devices = [dict(v, id=k) for k, v in h.ctx.devices.items()]
    h._send_json({'devices': sorted(devices, key=lambda d: -d.get('last_seen', 0))})


@route('POST', r'/onilot/devices/sign-out', 'token')
def api_sign_out(h):
    h.server.sign_out(h.user)
    h._send_json({'signed_out': True})


# ---- reading progress -----------------------------------------------------------------------

@route('PATCH', r'/v4/reading-metadata', 'token')
def reading_metadata(h):
    request = json.loads(h.body or b'{}')
    revision, patches = h.ctx.state.sync(request.get('revision'), request.get('patches', []),
                                         series_of(h.ctx.library))
    h._send_json({'revision': revision, 'patches': patches, 'conflicts': []})


class UserContext:
    """Per-user state: reading metadata, devices, uploads and the library view."""

    def __init__(self, server, name):
        self.name = name
        self.dir = Path(server.cfg.data) / 'users' / name
        self.dir.mkdir(parents=True, exist_ok=True)
        self.library_dir = Path(server.cfg.library) / name
        self.library_dir.mkdir(parents=True, exist_ok=True)
        self.server = server
        # Shared books the user added from the store: uuid -> added at (ms).
        self.shelf_path = self.dir / 'shelf.json'
        self.shelf = json.loads(self.shelf_path.read_text()) if self.shelf_path.exists() else {}
        self.library = Library([(self.library_dir, '', False),
                                (server.shared_dir, 'shared/', True)], shelf=self.shelf)
        self.state = State(self.dir)
        self.lock = threading.Lock()
        self.devices_path = self.dir / 'devices.json'
        self.devices = json.loads(self.devices_path.read_text()) \
            if self.devices_path.exists() else {}
        self.uploads_path = self.dir / 'uploads.json'
        self.uploads = set(json.loads(self.uploads_path.read_text())) \
            if self.uploads_path.exists() else set()

    def seen_device(self, headers):
        hw = headers.get('Hardware-Id', '')[:64]
        now = int(time.time())
        with self.lock:
            entry = self.devices.setdefault(hw, {'first_seen': now})
            previous = entry.get('last_seen', 0)
            entry['last_seen'] = now
            for field, limit in (('client_type', 64), ('client_version', 32)):
                if headers.get(field):  # not every request carries these headers
                    entry[field] = headers[field][:limit]
            if now - previous > 60:  # do not rewrite the file on every request
                self.devices_path.write_text(json.dumps(self.devices, indent=1))

    def add_to_shelf(self, uuid):
        with self.lock:
            self.shelf[uuid] = int(time.time() * 1000)
            self.shelf_path.write_text(json.dumps(self.shelf, indent=1))
        self.server.clear_deletion(self.name, uuid)

    def remove_from_shelf(self, uuid):
        with self.lock:
            if self.shelf.pop(uuid, None) is None:
                return
            self.shelf_path.write_text(json.dumps(self.shelf, indent=1))
        self.server.record_deletion(self.name, uuid)  # the reader removes it at its next sync

    def is_upload(self, book):
        return not book['shared'] and book['path'].name in self.uploads

    def mark_uploaded(self, filename):
        with self.lock:
            self.uploads.add(filename)
            self.uploads_path.write_text(json.dumps(sorted(self.uploads)))


class Server(ThreadingHTTPServer):
    def __init__(self, cfg, users):
        super().__init__((cfg.bind, cfg.port), Handler)
        self.cfg = cfg
        self.users = users
        self.shared_dir = Path(cfg.library) / 'shared'
        self.shared_dir.mkdir(parents=True, exist_ok=True)
        self.contexts = {}
        self.contexts_lock = threading.Lock()
        self.deletions_path = Path(cfg.data) / 'deleted.json'
        self.deletions = json.loads(self.deletions_path.read_text()) \
            if self.deletions_path.exists() else {}   # owner -> {uuid: deleted at (ms)}
        self.deletions_lock = threading.Lock()
        self.log_lock = threading.Lock()
        self.tokens_lock = threading.Lock()
        self.codes = {}            # code -> (user, expires)
        self.admin_sessions = {}   # sid -> (user, expires, csrf)
        self.tokens_path = Path(cfg.data) / 'tokens.json'
        saved = json.loads(self.tokens_path.read_text()) if self.tokens_path.exists() else {}
        now = time.time()
        self.refresh_tokens = dict(saved.get('refresh', {}))                  # token -> user
        self.access_tokens = {t: tuple(v) for t, v in saved.get('access', {}).items()
                              if v[1] > now}                                  # token -> (user, exp)
        secret_path = Path(cfg.data) / 'url-signing.key'
        if not secret_path.exists():
            secret_path.write_bytes(secrets.token_bytes(32))
            secret_path.chmod(0o600)
        self.signing_key = secret_path.read_bytes()
        self.series = SeriesStore(cfg.data)
        Library.series_store = self.series
        self.series_lookup = None   # Lookup thread, unless ONILOT_SERIES_LOOKUP=off

    def context(self, user):
        with self.contexts_lock:
            if user not in self.contexts:
                self.contexts[user] = UserContext(self, user)
            return self.contexts[user]

    def shared_library(self):
        return Library([(self.shared_dir, 'shared/', True)])

    def all_books(self):
        """Every book on the server: all users' libraries and the whole shared library."""
        roots = [(Path(self.cfg.library) / name, '', False) for name in self.users.names()]
        return Library(roots + [(self.shared_dir, 'shared/', True)]).books()

    def record_deletion(self, owner, uuid):
        with self.deletions_lock:
            self.deletions.setdefault(owner, {})[uuid] = int(time.time() * 1000)
            self.deletions_path.write_text(json.dumps(self.deletions, indent=1))

    def clear_deletion(self, owner, uuid):
        with self.deletions_lock:
            if self.deletions.get(owner, {}).pop(uuid, None) is not None:
                self.deletions_path.write_text(json.dumps(self.deletions, indent=1))

    def deletions_for(self, user):
        result = dict(self.deletions.get('shared', {}))
        result.update(self.deletions.get(user, {}))
        return result

    # ---- tokens ------------------------------------------------------------------------------

    def issue_tokens(self, user):
        access, refresh = make_access_token(user), secrets.token_urlsafe(24)
        with self.tokens_lock:
            self.access_tokens[access] = (user, int(time.time()) + ACCESS_TOKEN_TTL)
            self.refresh_tokens[refresh] = user
            self._save_tokens()
        return access, refresh

    def access_user(self, token):
        entry = self.access_tokens.get(token)
        if not entry or entry[1] <= time.time() or self.users.get(entry[0]) is None:
            return None
        return entry[0]

    def take_refresh_token(self, token):
        with self.tokens_lock:
            return self.refresh_tokens.pop(token, None)  # rotated: single use

    def sign_out(self, user):
        with self.tokens_lock:
            self.refresh_tokens = {t: u for t, u in self.refresh_tokens.items() if u != user}
            self.access_tokens = {t: v for t, v in self.access_tokens.items() if v[0] != user}
            self._save_tokens()

    def _save_tokens(self):
        now = time.time()
        self.access_tokens = {t: v for t, v in self.access_tokens.items() if v[1] > now}
        self.tokens_path.write_text(json.dumps({'refresh': self.refresh_tokens,
                                                'access': self.access_tokens}))
        self.tokens_path.chmod(0o600)

    # ---- admin sessions ----------------------------------------------------------------------

    def new_admin_session(self, user):
        sid, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(16)
        self.admin_sessions[sid] = (user, time.time() + ADMIN_SESSION_TTL, csrf)
        return sid

    def admin_session(self, sid):
        entry = self.admin_sessions.get(sid or '')
        if not entry or entry[1] <= time.time() or not self.users.is_admin(entry[0]):
            self.admin_sessions.pop(sid or '', None)
            return None
        return entry[0], entry[2]

    def sign(self, message):
        return hmac.new(self.signing_key, message.encode(), hashlib.sha256).hexdigest()


def migrate_single_user(cfg, users, name, password):
    """First start: create the admin and move single-user data (if any) to them."""
    users.add(name, password, admin=True)
    data, library = Path(cfg.data), Path(cfg.library)
    user_dir = data / 'users' / name
    user_dir.mkdir(parents=True, exist_ok=True)
    moved = []
    for filename in ('reading-metadata.json', 'devices.json', 'uploads.json'):
        if (data / filename).exists():
            shutil.move(str(data / filename), str(user_dir / filename))
            moved.append(filename)
    tokens_path = data / 'tokens.json'
    if tokens_path.exists():
        old = json.loads(tokens_path.read_text())
        if isinstance(old.get('refresh'), list):  # single-user format
            tokens_path.write_text(json.dumps({
                'refresh': {t: name for t in old['refresh']},
                'access': {t: [name, exp] for t, exp in old.get('access', {}).items()}}))
            moved.append('tokens.json')
    books = [p for p in library.glob('*.epub')]
    if books:
        (library / name).mkdir(parents=True, exist_ok=True)
        for book in books:
            shutil.move(str(book), str(library / name / book.name))
        moved.append('%d books' % len(books))
    if moved:
        users.set_legacy_user(name)
    print('onilot: created admin %r%s' % (
        name, '; moved single-user data to it: ' + ', '.join(moved) if moved else ''), flush=True)


def env_flag(name, default='0'):
    return os.environ.get(name, default).strip().lower() in ('1', 'true', 'yes', 'on')


def main(argv=None):
    p = argparse.ArgumentParser(prog='onilot', description=__doc__.split('\n')[0])
    env = os.environ.get
    p.add_argument('--version', action='version', version='onilot ' + __version__)
    p.add_argument('--public-url', default=env('ONILOT_PUBLIC_URL'),
                   help='base URL the reader was patched to (ONILOT_PUBLIC_URL)')
    p.add_argument('--library', default=env('ONILOT_LIBRARY', 'library'),
                   help='book directory (ONILOT_LIBRARY)')
    p.add_argument('--data', default=env('ONILOT_DATA', 'data'),
                   help='state directory (ONILOT_DATA)')
    p.add_argument('--port', type=int, default=int(env('ONILOT_PORT', '8095')))
    p.add_argument('--bind', default=env('ONILOT_BIND', '127.0.0.1'))
    p.add_argument('--brand', default=env('ONILOT_BRAND', 'onilot'))
    p.add_argument('--country', default=env('ONILOT_COUNTRY', 'DE'),
                   help='country code offered during reader setup (ONILOT_COUNTRY)')
    p.add_argument('--series-lookup', choices=('wikidata', 'off'),
                   default=env('ONILOT_SERIES_LOOKUP', 'wikidata'),
                   help='look up series of books without series metadata (ONILOT_SERIES_LOOKUP)')
    p.add_argument('--request-log', action='store_true', default=env_flag('ONILOT_REQUEST_LOG'),
                   help='log every request to DATA/requests.jsonl (ONILOT_REQUEST_LOG=1)')
    cfg = p.parse_args(argv)
    if not cfg.public_url:
        p.error('set ONILOT_PUBLIC_URL (or --public-url) to the URL the reader was patched to')
    parts = urllib.parse.urlsplit(cfg.public_url)
    if parts.scheme not in ('http', 'https') or not parts.netloc or cfg.public_url.endswith('/'):
        p.error('ONILOT_PUBLIC_URL must look like https://host/path and must not end with "/"')
    cfg.public_base = cfg.public_url
    cfg.prefix = parts.path.rstrip('/')
    cfg.secure_cookies = parts.scheme == 'https'
    # The admin page lives at the origin (https://host/admin), not under the API prefix: only the
    # reader is bound to the 32-character URL, and it never uses the admin page.
    cfg.origin = '%s://%s' % (parts.scheme, parts.netloc)
    Path(cfg.data).mkdir(parents=True, exist_ok=True)
    Path(cfg.library).mkdir(parents=True, exist_ok=True)
    cfg.log_file = str(Path(cfg.data) / 'requests.jsonl')

    users = UserStore(cfg.data)
    if not users.exists():
        password = env('ONILOT_PASSWORD')
        name = env('ONILOT_ADMIN_USER', 'admin').strip().lower()
        if not password:
            p.error('no users yet: set ONILOT_ADMIN_USER and ONILOT_PASSWORD for the first admin, '
                    'or run "python -m onilot user add NAME --admin"')
        try:
            check_name(name)
            migrate_single_user(cfg, users, name, password)
        except UserError as exc:
            p.error(str(exc))
    elif env('ONILOT_PASSWORD'):
        print('onilot: users exist; ONILOT_PASSWORD is ignored (manage users with '
              '"python -m onilot user" or /admin)', flush=True)

    server = Server(cfg, users)
    if cfg.series_lookup == 'wikidata':
        server.series_lookup = Lookup(server.series, server.all_books)
        server.series_lookup.start()
    print('onilot %s: %d users, listening on %s:%d, API prefix %r, request log %s, '
          'series lookup %s' % (__version__, len(users.names()), cfg.bind, cfg.port, cfg.prefix,
                                cfg.log_file if cfg.request_log else 'off', cfg.series_lookup),
          flush=True)
    server.serve_forever()


def entry(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ['user']:
        user_cli(argv[1:])
    else:
        main(argv)


from onilot import admin  # noqa: E402,F401  (registers the /admin routes)


if __name__ == '__main__':
    entry()
