#!/usr/bin/env python3
"""onilot: self-hosted backend for Tolino e-readers running a patched firmware 5.x.

Serves your own DRM-free EPUBs and syncs reading positions with the Tolino reader app
(Kobo Nickel in Tolino mode) after its REST base URL has been patched to point here
(see tools/patch_libnickel.py and README.md). Protocol notes: docs/protocol.md.

Configuration (environment variables; command-line flags override them):
  ONILOT_PUBLIC_URL   required  public base URL the reader was patched to, e.g.
                                https://books.example.com/onilotapi  (TLS terminated in front)
  ONILOT_PASSWORD     required  password for the reader's login page
  ONILOT_LIBRARY      ./library directory with .epub files (uploads from the reader land here)
  ONILOT_DATA         ./data    state: reading positions, tokens, signing key
  ONILOT_PORT         8095
  ONILOT_BIND         127.0.0.1
  ONILOT_BRAND        onilot    name shown on the reader (shop, login page)
  ONILOT_COUNTRY      DE        country offered during reader setup (any ISO code)
  ONILOT_REQUEST_LOG  0         1 = append every request/response to DATA/requests.jsonl
                                (secrets redacted; useful for debugging new firmware)

Single user, stdlib only (Python 3.10+). Runs plain HTTP; put TLS in front of it.
"""
import argparse
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

RESELLER_ID = 1
USER_ID = 'user1'
CLIENT_ID = 'onilot'
LOG_BODY_LIMIT = 64 * 1024
SIGNED_URL_TTL = 24 * 3600
COVER_NEVER_EXPIRES = 4102444800  # 2100-01-01; covers are low-sensitivity
ACCESS_TOKEN_TTL = 3600
COLLECTIONS = ('tags', 'comments', 'dogears')
COUNTRY_NAMES = {'DE': 'Deutschland', 'AT': 'Österreich', 'CH': 'Schweiz'}


def b64url(data):
    return base64.urlsafe_b64encode(data).rstrip(b'=').decode()


SECRET_FIELDS = re.compile(
    r'((?:password|code|refresh_token|access_token|client_secret)["\']?\s*[=:]\s*["\']?)'
    r'[^&"\'\s,}]+')
BEARER = re.compile(r'[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}')


def redact(text):
    """Strip passwords, codes and tokens from text before it goes into the request log."""
    return BEARER.sub('<token>', SECRET_FIELDS.sub(r'\1<redacted>', text))


def make_access_token():
    # The device splits on '.', base64-decodes the middle part and reads "sub" as the
    # bookshelf id. It does not check the signature.
    header = b64url(json.dumps({'alg': 'none', 'typ': 'JWT'}).encode())
    payload = b64url(json.dumps({'sub': USER_ID, 'iat': int(time.time())}).encode())
    return '%s.%s.%s' % (header, payload, b64url(secrets.token_bytes(16)))


class Library:
    """EPUB files in a directory; the uuid is derived from the file name so it stays stable."""

    OPF_NS = {'opf': 'http://www.idpf.org/2007/opf', 'dc': 'http://purl.org/dc/elements/1.1/',
              'c': 'urn:oasis:names:tc:opendocument:xmlns:container'}

    def __init__(self, root):
        self.root = Path(root)

    def books(self):
        result = []
        for path in sorted(self.root.glob('*.epub')):
            uuid = hashlib.sha1(path.name.encode()).hexdigest()
            meta = self._metadata(path)
            result.append({'uuid': uuid, 'path': path, 'size': path.stat().st_size,
                           'mtime': int(path.stat().st_mtime * 1000), **meta})
        return result

    def find(self, uuid):
        return next((b for b in self.books() if b['uuid'] == uuid), None)

    def _metadata(self, path):
        meta = {'title': path.stem, 'authors': [], 'language': None, 'publisher': None,
                'cover': None}
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
                meta['cover'] = self._cover_href(opf, opf_path)
        except (KeyError, ET.ParseError, zipfile.BadZipFile, AttributeError) as exc:
            print('warning: cannot read metadata of %s: %s' % (path.name, exc))
        return meta

    def _cover_href(self, opf, opf_path):
        manifest = opf.find('opf:manifest', self.OPF_NS)
        if manifest is None:
            return None
        items = manifest.findall('opf:item', self.OPF_NS)
        cover = next((i for i in items if 'cover-image' in (i.get('properties') or '')), None)
        if cover is None:
            meta = opf.find(".//opf:meta[@name='cover']", self.OPF_NS)
            if meta is not None:
                cover = next((i for i in items if i.get('id') == meta.get('content')), None)
        if cover is None:
            return None
        base = opf_path.rsplit('/', 1)[0] + '/' if '/' in opf_path else ''
        return urllib.parse.unquote(base + cover.get('href'))

    def cover_bytes(self, book):
        if not book['cover']:
            return None, None
        with zipfile.ZipFile(book['path']) as z:
            data = z.read(book['cover'])
        mime = 'image/png' if book['cover'].lower().endswith('.png') else 'image/jpeg'
        return data, mime


class State:
    """Reading metadata (bookmarks, dogears, comments, tags) as JSON-patch documents.

    Protocol as observed on the device (firmware 5.18):
    - The device sends {"revision": <last seen>, "patches": [...]}. A new local document is an
      "add" on e.g. /publications/<uuid>/bookmark with a local "transientId" in the value.
    - The server must echo each add with the server id appended to the path
      (/publications/<uuid>/bookmark/<id>) and "transientId" + "revision" in the value; the
      device then stores backendId/revision (TolinoDbHelper::addBackendIdAndRevision) and
      sends later changes as "replace" on the path with the id. Without the echo it re-sends
      the add on every sync.
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
        doc = {'id': self.next_id, 'revision': self.revision, 'value': value, 'deleted': False}
        self.next_id += 1
        return doc

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

    def sync(self, client_revision, patches):
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
                if doc['deleted']:
                    outgoing.append({'op': 'remove', 'path': full})
                else:
                    outgoing.append({'op': 'replace', 'path': full,
                                     'value': dict(doc['value'], revision=str(doc['revision']))})
            if changed or not self.path.exists():
                self.path.write_text(json.dumps({'revision': self.revision,
                                                 'next_id': self.next_id,
                                                 'docs': self.docs}, indent=1))
            return str(self.revision), echoes + outgoing


class Handler(BaseHTTPRequestHandler):
    server_version = 'tolino-selfhosted/0.1'
    routes = []

    # ---- plumbing -----------------------------------------------------------------------

    def _dispatch(self):
        cfg = self.server.cfg
        parsed = urllib.parse.urlsplit(self.path)
        path = parsed.path
        if path.startswith(cfg.prefix):
            path = path[len(cfg.prefix):] or '/'
        self.query = urllib.parse.parse_qs(parsed.query)
        length = int(self.headers.get('Content-Length') or 0)
        self.body = self.rfile.read(length) if length else b''
        self.response_log = None
        if self.headers.get('Hardware-Id') and self._has_access_token():
            self.server.seen_device(self.headers)
        for method, pattern, func, guard in self.routes:
            m = pattern.fullmatch(path)
            if m and method == self.command:
                try:
                    if guard == 'token' and not self._has_access_token():
                        self._send_json({'error': 'unauthorized'}, 401)
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
        expires = COVER_NEVER_EXPIRES if kind == 'covers' else int(time.time()) + SIGNED_URL_TTL
        sig = self.server.sign('%s:%s:%d' % (kind, uuid, expires))
        return self._url('/%s/%s/%d/%s' % (kind, uuid, expires, sig))

    def _has_valid_signature(self, kind, uuid, expires, sig):
        expected = self.server.sign('%s:%s:%d' % (kind, uuid, int(expires)))
        return int(expires) >= time.time() and hmac.compare_digest(expected, sig)

    def _has_access_token(self):
        # The exact auth header pairing is unverified ("%1 %2" in Authorization, or
        # t_auth_token on v1 calls), so accept an issued token in any header value.
        jwt_like = re.compile(r'[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+')
        now = time.time()
        return any(self.server.access_tokens.get(tok, 0) > now
                   for value in self.headers.values() for tok in jwt_like.findall(value))

    def log_message(self, fmt, *args):
        pass  # replaced by _log

    def _form(self):
        return {k: v[0] for k, v in urllib.parse.parse_qs(self.body.decode()).items()}


def route(method, regex, guard=None):
    """guard: None (public), 'token' (issued access token), 'signed' (signed URL)."""
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
<p>Password<br><input type=password name=password style="font-size:1em"></p>
<p><button style="font-size:1em">Sign in</button></p></form>{message}</body></html>"""


@route('GET', r'/oauth/authorize')
def authorize_page(h):
    h._send(200, LOGIN_PAGE.format(brand=html.escape(h.server.cfg.brand), message='').encode(), 'text/html; charset=UTF-8')


@route('POST', r'/oauth/authorize')
def authorize_submit(h):
    if h._form().get('password') != h.server.cfg.password:
        h._send(200, LOGIN_PAGE.format(brand=html.escape(h.server.cfg.brand),
                                  message='<p>Wrong password.</p>').encode(),
                'text/html; charset=UTF-8')
        return
    code = secrets.token_urlsafe(16)
    h.server.codes.add(code)
    h._send(302, b'', 'text/plain', {'Location': 'epublishing://login?code=' + code})


@route('POST', r'/oauth/token')
def token(h):
    form = h._form()
    grant = form.get('grant_type')
    if grant == 'authorization_code' and form.get('code') not in h.server.codes:
        h._send_json({'error': 'invalid_grant'}, 400)
        return
    if grant == 'refresh_token' and form.get('refresh_token') not in h.server.refresh_tokens:
        h._send_json({'error': 'invalid_grant'}, 400)
        return
    h.server.codes.discard(form.get('code'))
    if grant == 'refresh_token':
        h.server.refresh_tokens.discard(form.get('refresh_token'))
    refresh = secrets.token_urlsafe(24)
    access = make_access_token()
    h.server.refresh_tokens.add(refresh)
    h.server.access_tokens[access] = int(time.time()) + ACCESS_TOKEN_TTL
    h.server.save_tokens()
    h._send_json({'access_token': access, 'refresh_token': refresh,
                  'expires_in': ACCESS_TOKEN_TTL, 'token_type': 'Bearer'})


@route('POST', r'/oauth/revoke')
def revoke(h):
    h._send(200, b'', 'text/plain')


@route('POST', r'/v1/devices')
def register_device(h):
    h._send_json({'initAppResponse': {'userId': USER_ID, 'deviceKey': CLIENT_ID + '-device'}})


@route('GET', r'/v1/devices/([^/]+)/userid')
def device_user_id(h, hardware_id):
    # Format not confirmed; the reader accepts this during setup.
    h._send_json({'userId': USER_ID})


# ---- library -------------------------------------------------------------------------------

def content_item(h, book):
    return {
        'uuid': book['uuid'],
        'publicationId': book['uuid'],
        'transactionId': book['uuid'],
        'title': book['title'],
        # Must be a MIME type: the reader uses it as the book's file type. With "EPUB" it
        # never recognized the book as EPUB and never saved reading positions.
        'contentFormat': 'application/epub+zip',
        'contentType': 'EBOOK',
        'contentSources': ['USER_UPLOAD' if book['path'].name in h.server.uploads
                           else 'PURCHASED'],
        'contentIsPreview': False,
        'language': book['language'],
        'publisher': book['publisher'],
        'purchasedDate': book['mtime'],
        'resellerId': RESELLER_ID,
        'resellerName': h.server.cfg.brand,
        'authors': [{'name': a, 'type': 'AUTHOR'} for a in book['authors']],
        'fileResources': [{'type': 'COVER_IMAGE',
                           'resource': h._signed_url('covers', book['uuid'])}],
        'readableContentInfo': {'renderingEngine': 'READIUM_1'},
    }


@route('GET', r'/v8/inventory', 'token')
def inventory(h):
    books = h.server.library.books()
    size = int(h.query.get('size', ['100'])[0])
    page = int(h.query.get('page', ['0'])[0])
    chunk = books[page * size:(page + 1) * size]
    last_modified = max((b['mtime'] for b in books), default=0)
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
        'page': {'content': [content_item(h, b) for b in chunk],
                 'numberOfElements': len(chunk), 'totalElements': len(books),
                 'number': page, 'size': size},
        'deletedContent': [],
        'lastModifiedDate': last_modified,
    })


@route('GET', r'/v1/inventory/([^/]+)/downloadinfo', 'token')
def download_info(h, uuid):
    book = h.server.library.find(uuid)
    if book is None:
        h._send_json({'error': 'unknown book'}, 404)
        return
    # "format" must be a MIME type: the device copies it into content.MimeType, and with
    # "EPUB" the reader engine never saved positions (verified on device, 2026-10-11).
    h._send_json({'contentUrl': h._signed_url('content', uuid), 'format': 'application/epub+zip',
                  'fileSize': book['size']})


@route('GET', r'/(content)/([0-9a-f]+)/(\d+)/([0-9a-f]{64})', 'signed')
def content(h, kind, uuid, expires, sig):
    book = h.server.library.find(uuid)
    if book is None:
        h._send_json({'error': 'unknown book'}, 404)
        return
    h._send(200, book['path'].read_bytes(), 'application/epub+zip')


@route('GET', r'/(covers)/([0-9a-f]+)/(\d+)/([0-9a-f]{64})', 'signed')
def cover(h, kind, uuid, expires, sig):
    book = h.server.library.find(uuid)
    data, mime = h.server.library.cover_bytes(book) if book else (None, None)
    if data is None:
        h._send_json({'error': 'no cover'}, 404)
        return
    h._send(200, data, mime)


# ---- shop tab (webview) -----------------------------------------------------------------

SHOP_PAGE = """<!doctype html><html><head><meta name=viewport content="width=device-width">
<title>{brand}</title></head><body style="font-family:sans-serif;font-size:1.3em">
<h1>{brand}</h1><p>{count} books on the server. They appear in your library after a
sync.</p><ul>{items}</ul></body></html>"""


# Some shop entry points concatenate SHOP_BASE + URL_SHOP_EBOOK_START_PAGE, producing
# /shophttps://<host>/<prefix>/shop, so accept anything after /shop.
@route('GET', r'/shop(?:https?://.*)?')
def shop(h):
    query = h.query.get('q', [''])[0].lower()
    books = [b for b in h.server.library.books()
             if query in b['title'].lower() or any(query in a.lower() for a in b['authors'])]
    items = ''.join('<li>%s%s</li>' % (html.escape(b['title']),
                                        ' &ndash; ' + html.escape(', '.join(b['authors']))
                                        if b['authors'] else '') for b in books)
    page = SHOP_PAGE.format(brand=html.escape(h.server.cfg.brand), count=len(books),
                            items=items)
    h._send(200, page.encode(), 'text/html; charset=UTF-8')


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
    path = store_upload(h.server.cfg.library, filename, data)
    h.server.mark_uploaded(path.name)
    book = h.server.library.find(hashlib.sha1(path.name.encode()).hexdigest())
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
    books = sorted(h.server.library.books(), key=lambda b: b['mtime'], reverse=True)[:count]
    # Top-level array; keys from TolinoRecommendation(const QJsonObject&). Tapping one opens
    # shop_url in the shop webview.
    h._send_json([{
        'publicationId': b['uuid'],
        'title': b['title'],
        'author': ', '.join(b['authors']),
        'cover_url': h._signed_url('covers', b['uuid']),
        'description': '',
        'publicationType': h.query.get('type', ['ebook'])[0],
        'shop_url': h._url('/shop?q=' + urllib.parse.quote(b['title'])),
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
      '</span>' : '') + (b.uploaded ? ' <span class="muted">(uploaded)</span>' : '') + '</li>';
  }).join('');
  document.getElementById('content').innerHTML = '<p>' + d.books.length +
    ' books on this server. "Sync now" downloads new ones into your library.</p><ul>' +
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
    document.getElementById('content').innerHTML = '<p>Readers signed in to this server:</p>' +
      '<ul>' + items + '</ul>';
  }, fail);
}
function signOut() {
  if (!confirm('Sign out every reader? Each has to sign in again with the password.')) return;
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
                '<button onclick="signOut()">Sign out all readers</button>')


@route('GET', r'/manage/(handshakeFinish|handshakeCancel)')
def manage_leave(h, what):
    # Normally intercepted by the reader; reached only from a desktop browser.
    h._send(200, b'You can close this page.', 'text/plain; charset=UTF-8')


@route('GET', r'/onilot/library', 'token')
def api_library(h):
    h._send_json({'books': [{
        'uuid': b['uuid'], 'title': b['title'], 'authors': ', '.join(b['authors']),
        'uploaded': b['path'].name in h.server.uploads,
    } for b in sorted(h.server.library.books(), key=lambda b: b['title'].lower())]})


@route('GET', r'/onilot/devices', 'token')
def api_devices(h):
    devices = [dict(v, id=k) for k, v in h.server.devices.items()]
    h._send_json({'devices': sorted(devices, key=lambda d: -d.get('last_seen', 0))})


@route('POST', r'/onilot/devices/sign-out', 'token')
def api_sign_out(h):
    h.server.sign_out_everyone()
    h._send_json({'signed_out': True})


# ---- reading progress -----------------------------------------------------------------------

@route('PATCH', r'/v4/reading-metadata', 'token')
def reading_metadata(h):
    request = json.loads(h.body or b'{}')
    revision, patches = h.server.state.sync(request.get('revision'), request.get('patches', []))
    h._send_json({'revision': revision, 'patches': patches, 'conflicts': []})


class Server(ThreadingHTTPServer):
    def __init__(self, cfg):
        super().__init__((cfg.bind, cfg.port), Handler)
        self.cfg = cfg
        self.library = Library(cfg.library)
        self.state = State(cfg.data)
        self.log_lock = threading.Lock()
        self.codes = set()
        self.tokens_path = Path(cfg.data) / 'tokens.json'
        saved = json.loads(self.tokens_path.read_text()) if self.tokens_path.exists() else {}
        self.refresh_tokens = set(saved.get('refresh', []))
        now = time.time()
        self.access_tokens = {t: exp for t, exp in saved.get('access', {}).items() if exp > now}
        self.devices_path = Path(cfg.data) / 'devices.json'
        self.devices = json.loads(self.devices_path.read_text()) \
            if self.devices_path.exists() else {}
        self.devices_lock = threading.Lock()
        self.uploads_path = Path(cfg.data) / 'uploads.json'
        self.uploads = set(json.loads(self.uploads_path.read_text())) \
            if self.uploads_path.exists() else set()
        secret_path = Path(cfg.data) / 'url-signing.key'
        if not secret_path.exists():
            secret_path.write_bytes(secrets.token_bytes(32))
            secret_path.chmod(0o600)
        self.signing_key = secret_path.read_bytes()

    def save_tokens(self):
        now = time.time()
        self.access_tokens = {t: e for t, e in self.access_tokens.items() if e > now}
        self.tokens_path.write_text(json.dumps({'refresh': sorted(self.refresh_tokens),
                                                'access': self.access_tokens}))
        self.tokens_path.chmod(0o600)

    def seen_device(self, headers):
        hw = headers.get('Hardware-Id', '')[:64]
        now = int(time.time())
        with self.devices_lock:
            entry = self.devices.setdefault(hw, {'first_seen': now})
            previous = entry.get('last_seen', 0)
            entry['last_seen'] = now
            for field, limit in (('client_type', 64), ('client_version', 32)):
                if headers.get(field):  # not every request carries these headers
                    entry[field] = headers[field][:limit]
            if now - previous > 60:  # do not rewrite the file on every request
                self.devices_path.write_text(json.dumps(self.devices, indent=1))

    def sign_out_everyone(self):
        self.refresh_tokens.clear()
        self.access_tokens.clear()
        self.save_tokens()

    def mark_uploaded(self, filename):
        self.uploads.add(filename)
        self.uploads_path.write_text(json.dumps(sorted(self.uploads)))

    def sign(self, message):
        return hmac.new(self.signing_key, message.encode(), hashlib.sha256).hexdigest()


def env_flag(name, default='0'):
    return os.environ.get(name, default).strip().lower() in ('1', 'true', 'yes', 'on')


def main(argv=None):
    p = argparse.ArgumentParser(prog='onilot', description=__doc__.split('\n')[0])
    env = os.environ.get
    p.add_argument('--public-url', default=env('ONILOT_PUBLIC_URL'),
                   help='base URL the reader was patched to (ONILOT_PUBLIC_URL)')
    p.add_argument('--library', default=env('ONILOT_LIBRARY', 'library'),
                   help='directory with .epub files (ONILOT_LIBRARY)')
    p.add_argument('--data', default=env('ONILOT_DATA', 'data'),
                   help='state directory (ONILOT_DATA)')
    p.add_argument('--port', type=int, default=int(env('ONILOT_PORT', '8095')))
    p.add_argument('--bind', default=env('ONILOT_BIND', '127.0.0.1'))
    p.add_argument('--brand', default=env('ONILOT_BRAND', 'onilot'))
    p.add_argument('--country', default=env('ONILOT_COUNTRY', 'DE'),
                   help='country code offered during reader setup (ONILOT_COUNTRY)')
    p.add_argument('--request-log', action='store_true', default=env_flag('ONILOT_REQUEST_LOG'),
                   help='log every request to DATA/requests.jsonl (ONILOT_REQUEST_LOG=1)')
    cfg = p.parse_args(argv)
    cfg.password = env('ONILOT_PASSWORD')
    if not cfg.public_url:
        p.error('set ONILOT_PUBLIC_URL (or --public-url) to the URL the reader was patched to')
    if not cfg.password:
        p.error('set ONILOT_PASSWORD (password for the login page on the reader)')
    parts = urllib.parse.urlsplit(cfg.public_url)
    if parts.scheme not in ('http', 'https') or not parts.netloc or cfg.public_url.endswith('/'):
        p.error('ONILOT_PUBLIC_URL must look like https://host/path and must not end with "/"')
    cfg.public_base = cfg.public_url
    cfg.prefix = parts.path.rstrip('/')
    Path(cfg.data).mkdir(parents=True, exist_ok=True)
    Path(cfg.library).mkdir(parents=True, exist_ok=True)
    cfg.log_file = str(Path(cfg.data) / 'requests.jsonl')
    server = Server(cfg)
    print('onilot: %d books, listening on %s:%d, API prefix %r, request log %s' % (
        len(server.library.books()), cfg.bind, cfg.port, cfg.prefix,
        cfg.log_file if cfg.request_log else 'off'), flush=True)
    server.serve_forever()


if __name__ == '__main__':
    main()
