#!/usr/bin/env python3
"""End-to-end tests: start the server on a free port and walk the reader's flow over HTTP.

Run: python3 -m unittest discover -s tests
"""
import base64
import json
import os
import socket
import subprocess
import sys
import tempfile
import time
import unittest
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
PASSWORD = 'test-password'
BOB_PASSWORD = 'bob-password'

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier id="id">test-book</dc:identifier><dc:title>{title}</dc:title>
  <dc:creator>Jane Author</dc:creator><dc:language>de</dc:language>
 </metadata>
 <manifest><item id="c" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>
 </manifest><spine/></package>"""
CONTAINER = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
 <rootfiles><rootfile full-path="OEBPS/content.opf"
  media-type="application/oebps-package+xml"/></rootfiles></container>"""


def make_epub_without_cover(path, title, first_page_image=False):
    """No cover-image property; optionally an image referenced from the first page."""
    opf = OPF.replace('{title}', title).replace(
        '<item id="c" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>',
        '<item id="p1" href="text/p1.xhtml" media-type="application/xhtml+xml"/>' +
        ('<item id="pic" href="img/front.png" media-type="image/png"/>' if first_page_image
         else '')).replace('<spine/>', '<spine><itemref idref="p1"/></spine>')
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', CONTAINER)
        z.writestr('OEBPS/content.opf', opf)
        z.writestr('OEBPS/text/p1.xhtml', '<html><body>%s<p>Hello</p></body></html>' % (
            '<img src="../img/front.png"/>' if first_page_image else ''))
        if first_page_image:
            z.writestr('OEBPS/img/front.png', b'\x89PNGfake')


def make_epub(path, title='Test Book'):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', CONTAINER)
        z.writestr('OEBPS/content.opf', OPF.replace('{title}', title))
        z.writestr('OEBPS/cover.jpg', b'\xff\xd8fakejpeg')


def make_series_epub(path, title, series, index, epub3=False):
    if epub3:
        meta = ('<meta property="belongs-to-collection" id="s">%s</meta>'
                '<meta refines="#s" property="collection-type">series</meta>'
                '<meta refines="#s" property="group-position">%s</meta>' % (series, index))
    else:
        meta = ('<meta name="calibre:series" content="%s"/>'
                '<meta name="calibre:series_index" content="%s"/>' % (series, index))
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', CONTAINER)
        z.writestr('OEBPS/content.opf', OPF.replace('{title}', title).replace(
            '</metadata>', meta + '</metadata>'))
        z.writestr('OEBPS/cover.jpg', b'\xff\xd8fakejpeg')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


def free_port():
    with socket.socket() as s:
        s.bind(('127.0.0.1', 0))
        return s.getsockname()[1]


def start_server(base, port, lib, data, extra_env=None):
    clean = {k: v for k, v in os.environ.items() if not k.startswith('ONILOT_')}
    proc = subprocess.Popen(
        [sys.executable, '-m', 'onilot'], cwd=str(REPO),
        env={**clean, 'ONILOT_PUBLIC_URL': base, 'ONILOT_LIBRARY': str(lib),
             'ONILOT_DATA': str(data), 'ONILOT_PORT': str(port), 'ONILOT_REQUEST_LOG': '1',
             **(extra_env or {})},
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    for _ in range(100):
        try:
            urllib.request.urlopen(base + '/online-check')
            return proc
        except OSError:
            time.sleep(0.1)
    proc.terminate()
    raise RuntimeError('server did not start: %s' % proc.communicate()[0].decode())


def user_cli(data, *args, stdin=None):
    clean = {k: v for k, v in os.environ.items() if not k.startswith('ONILOT_')}
    return subprocess.run([sys.executable, '-m', 'onilot', 'user', *args], cwd=str(REPO),
                          env={**clean, 'ONILOT_DATA': str(data)}, input=stdin,
                          capture_output=True, text=True, timeout=60)


class DeviceFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.lib = Path(cls.tmp.name, 'library')
        cls.data = Path(cls.tmp.name, 'data')
        (cls.lib / 'shared').mkdir(parents=True)
        make_epub(cls.lib / 'test.epub')                     # first start moves it to alice
        make_epub(cls.lib / 'shared' / 'shared.epub', 'Shared Book')
        port = free_port()
        cls.base = 'http://127.0.0.1:%d/onilotapi' % port
        cls.proc = start_server(cls.base, port, cls.lib, cls.data, {
            'ONILOT_ADMIN_USER': 'alice', 'ONILOT_PASSWORD': PASSWORD})
        result = user_cli(cls.data, 'add', 'bob', '--password-stdin', stdin=BOB_PASSWORD + '\n')
        assert result.returncode == 0, result.stderr
        cls.opener = urllib.request.build_opener(NoRedirect)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()
        cls.proc.stdout.close()
        cls.tmp.cleanup()

    def request(self, method, path, body=None, headers=None, form=None):
        url = path if path.startswith('http') else self.base + path
        data = urllib.parse.urlencode(form).encode() if form is not None else body
        req = urllib.request.Request(url, data=data, method=method, headers=headers or {})
        try:
            with self.opener.open(req) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as err:
            return err.code, dict(err.headers), err.read()

    def login(self, user='alice', password=PASSWORD):
        status, headers, _ = self.request('POST', '/oauth/authorize',
                                          form={'username': user, 'password': password})
        self.assertEqual(status, 302)
        code = urllib.parse.parse_qs(urllib.parse.urlsplit(headers['Location']).query)['code'][0]
        self.assertTrue(headers['Location'].startswith('epublishing://login?code='))
        status, _, body = self.request('POST', '/oauth/token', form={
            'code': code, 'grant_type': 'authorization_code',
            'redirect_uri': 'epublishing://login', 'client_id': 'onilot'})
        self.assertEqual(status, 200)
        return json.loads(body), code

    def auth(self, token):
        return {'Authorization': 'Bearer ' + token}

    def test_reseller_config_points_oauth_at_this_server(self):
        status, _, body = self.request('GET', '/v1/resellers/1/client-types/x/config')
        config = json.loads(body)['config']
        self.assertEqual(status, 200)
        self.assertTrue(config['URL_OAUTH_AUTHORIZATION'].startswith(self.base))
        self.assertIn('?', config['URL_OAUTH_AUTHORIZATION'])  # device appends &client_id=

    def test_wrong_password_gets_no_code(self):
        for form in ({'username': 'alice', 'password': 'nope'},
                     {'username': 'nobody', 'password': PASSWORD},
                     {'password': PASSWORD}):
            status, headers, body = self.request('POST', '/oauth/authorize', form=form)
            self.assertEqual(status, 200)
            self.assertNotIn('Location', headers)
            self.assertIn(b'Wrong user or password', body)

    def test_access_token_payload_has_sub(self):
        tokens, _ = self.login()
        payload = tokens['access_token'].split('.')[1]
        payload += '=' * (-len(payload) % 4)
        self.assertEqual(json.loads(base64.urlsafe_b64decode(payload))['sub'], 'alice')

    def test_login_code_is_single_use(self):
        _, code = self.login()
        status, _, _ = self.request('POST', '/oauth/token', form={
            'code': code, 'grant_type': 'authorization_code'})
        self.assertEqual(status, 400)

    def test_refresh_rotates_and_old_refresh_token_is_rejected(self):
        tokens, _ = self.login()
        form = {'refresh_token': tokens['refresh_token'], 'grant_type': 'refresh_token'}
        self.assertEqual(self.request('POST', '/oauth/token', form=form)[0], 200)
        self.assertEqual(self.request('POST', '/oauth/token', form=form)[0], 400)

    def test_inventory_requires_token(self):
        self.assertEqual(self.request('GET', '/v8/inventory')[0], 401)
        self.assertEqual(self.request('GET', '/v8/inventory',
                                      headers=self.auth('a.b.c'))[0], 401)

    def test_inventory_download_and_cover(self):
        tokens, _ = self.login()
        status, _, body = self.request('GET', '/v8/inventory?size=10&page=0',
                                       headers=self.auth(tokens['access_token']))
        self.assertEqual(status, 200)
        inventory = json.loads(body)
        item = next(i for i in inventory['page']['content'] if i['title'] == 'Test Book')
        self.assertEqual(inventory['page']['numberOfElements'],
                         len(inventory['page']['content']))
        self.assertEqual(item['title'], 'Test Book')
        self.assertEqual(item['authors'][0]['name'], 'Jane Author')

        status, _, body = self.request('GET', '/v1/inventory/%s/downloadinfo' % item['uuid'],
                                       headers=self.auth(tokens['access_token']))
        info = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(info['format'], 'application/epub+zip')
        status, headers, epub = self.request('GET', info['contentUrl'])
        self.assertEqual(status, 200)
        self.assertEqual(len(epub), info['fileSize'])
        self.assertTrue(epub.startswith(b'PK'))

        cover_url = item['fileResources'][0]['resource'] + '?size=WS-B04'  # as the device does
        status, headers, cover = self.request('GET', cover_url)
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/jpeg')

    def test_inventory_answers_304_when_unchanged(self):
        tokens, _ = self.login()
        _, _, body = self.request('GET', '/v8/inventory', headers=self.auth(tokens['access_token']))
        last = json.loads(body)['lastModifiedDate'] // 1000
        since = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(last))
        status, _, _ = self.request('GET', '/v8/inventory', headers={
            **self.auth(tokens['access_token']), 'If-Modified-Since': since})
        self.assertEqual(status, 304)
        older = time.strftime('%a, %d %b %Y %H:%M:%S GMT', time.gmtime(last - 60))
        status, _, _ = self.request('GET', '/v8/inventory', headers={
            **self.auth(tokens['access_token']), 'If-Modified-Since': older})
        self.assertEqual(status, 200)

    def test_content_rejects_tampered_or_missing_signature(self):
        tokens, _ = self.login()
        _, _, body = self.request('GET', '/v8/inventory',
                                  headers=self.auth(tokens['access_token']))
        uuid = json.loads(body)['page']['content'][0]['uuid']
        self.assertEqual(self.request('GET', '/content/' + uuid)[0], 404)  # no signature
        _, _, body = self.request('GET', '/v1/inventory/%s/downloadinfo' % uuid,
                                  headers=self.auth(tokens['access_token']))
        url = json.loads(body)['contentUrl']
        self.assertEqual(self.request('GET', url[:-1] + ('0' if url[-1] != '0' else '1'))[0],
                         403)
        parts = url.split('/')
        parts[-2] = '1000'  # expiry in the past, signature no longer matches either
        self.assertEqual(self.request('GET', '/'.join(parts))[0], 403)

    def sync(self, token, revision, patches):
        status, _, body = self.request(
            'PATCH', '/v4/reading-metadata?paths=publications,audiobooks',
            body=json.dumps({'revision': revision, 'patches': patches}).encode(),
            headers={**self.auth(token), 'Content-Type': 'application/json'})
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_reading_metadata_add_is_echoed_with_server_id(self):
        tokens, _ = self.login()
        path = '/publications/echo-book/bookmark'
        add = {'op': 'add', 'path': path, 'value': {
            'position': 'p1', 'progress': 0.1, 'modified': 100, 'transientId': 7}}
        resp = self.sync(tokens['access_token'], '0', [add])
        echo = [p for p in resp['patches'] if p['path'].startswith(path + '/')]
        self.assertEqual(len(echo), 1)
        self.assertEqual(echo[0]['op'], 'add')
        self.assertEqual(echo[0]['value']['transientId'], '7')  # string, see server comment
        self.assertTrue(echo[0]['path'].rsplit('/', 1)[1].isdigit())
        self.assertEqual(echo[0]['value']['revision'], resp['revision'])

    def test_reading_metadata_confirmation_always_bumps_revision(self):
        # The device drops responses whose revision equals its own ("already up-to-date").
        tokens, _ = self.login()
        add = {'op': 'add', 'path': '/publications/bump-book/bookmark', 'value': {
            'position': 'p', 'modified': 100, 'transientId': 4}}
        first = self.sync(tokens['access_token'], '0', [add])
        # device did not process the confirmation and re-sends the same add
        second = self.sync(tokens['access_token'], first['revision'], [add])
        self.assertGreater(int(second['revision']), int(first['revision']))
        self.assertTrue([p for p in second['patches'] if p['value'].get('transientId') == '4'])

    def test_reading_metadata_unchanged_replace_still_bumps_revision(self):
        tokens, _ = self.login()
        path = '/publications/resend-book/bookmark'
        resp = self.sync(tokens['access_token'], '0', [{'op': 'add', 'path': path, 'value': {
            'position': 'p', 'modified': 100, 'transientId': 1}}])
        full = [p for p in resp['patches'] if p['path'].startswith(path + '/')][0]['path']
        replace = {'op': 'replace', 'path': full, 'value': {'position': 'p', 'modified': 100}}
        first = self.sync(tokens['access_token'], resp['revision'], [replace])
        second = self.sync(tokens['access_token'], first['revision'], [replace] * 3)
        self.assertGreater(int(second['revision']), int(first['revision']))
        echoed = [p for p in second['patches'] if p['path'] == full]
        self.assertEqual(len(echoed), 1)  # duplicates of the same replace are echoed once
        self.assertEqual(echoed[0]['op'], 'replace')
        self.assertEqual(echoed[0]['value']['revision'], second['revision'])
        idle = self.sync(tokens['access_token'], second['revision'], [])
        self.assertEqual(idle['revision'], second['revision'])
        self.assertEqual(idle['patches'], [])

    def test_reading_metadata_replace_and_last_write_wins(self):
        tokens, _ = self.login()
        path = '/publications/lww-book/bookmark'
        resp = self.sync(tokens['access_token'], '0', [{'op': 'add', 'path': path, 'value': {
            'position': 'old', 'modified': 100, 'transientId': 1}}])
        full = [p for p in resp['patches'] if p['path'].startswith(path + '/')][0]['path']
        rev = resp['revision']
        resp = self.sync(tokens['access_token'], rev, [{'op': 'replace', 'path': full,
                         'value': {'position': 'new', 'modified': 200}}])
        rev = resp['revision']
        # an older write from another client must not overwrite the newer position
        other = self.sync(tokens['access_token'], '0', [{'op': 'replace', 'path': full,
                          'value': {'position': 'stale', 'modified': 150}}])
        doc = Path(self.tmp.name, 'data', 'users', 'alice', 'reading-metadata.json').read_text()
        self.assertIn('"new"', doc)
        self.assertNotIn('"stale"', doc)
        del other

    def test_reading_metadata_sends_changes_from_other_clients(self):
        tokens, _ = self.login()
        path = '/publications/shared-book/bookmark'
        resp = self.sync(tokens['access_token'], '0', [{'op': 'add', 'path': path, 'value': {
            'position': 'device', 'modified': 100, 'transientId': 1}}])
        full = [p for p in resp['patches'] if p['path'].startswith(path + '/')][0]['path']
        device_rev = resp['revision']
        # another client (e.g. phone) moves on
        self.sync(tokens['access_token'], device_rev, [{'op': 'replace', 'path': full,
                  'value': {'position': 'phone', 'modified': 300}}])
        # the device syncs with its old revision and gets the phone position
        resp = self.sync(tokens['access_token'], device_rev, [])
        incoming = [p for p in resp['patches'] if p['path'] == full]
        self.assertEqual(len(incoming), 1)
        self.assertEqual(incoming[0]['op'], 'replace')
        self.assertEqual(incoming[0]['value']['position'], 'phone')
        # and nothing again once it is up to date
        resp = self.sync(tokens['access_token'], resp['revision'], [])
        self.assertFalse([p for p in resp['patches'] if p['path'] == full])

    def upload(self, token, filename, data):
        boundary = 'boundary_.oOo._test'
        body = (('--%s\r\nContent-Disposition: form-data; name="file"; filename="%s"\r\n'
                 'Content-Type: application/epub+zip\r\n\r\n' % (boundary, filename)).encode()
                + data + ('\r\n--%s--\r\n' % boundary).encode())
        return self.request('POST', '/v1/inventory/user-uploads', body=body, headers={
            **self.auth(token),
            'Content-Type': 'multipart/form-data; boundary="%s"' % boundary})

    def test_healthz_outside_prefix(self):
        url = self.base.rsplit('/', 1)[0] + '/healthz'
        status, _, body = self.request('GET', url)
        self.assertEqual((status, body), (200, b'ok'))

    def test_upload_requires_token(self):
        self.assertEqual(self.request('POST', '/v1/inventory/user-uploads', body=b'x')[0], 401)

    def test_upload_adds_book_to_library_as_user_upload(self):
        tokens, _ = self.login()
        epub = Path(self.tmp.name, 'up.epub')
        make_epub(epub)
        status, _, body = self.upload(tokens['access_token'], 'my-book.epub.epub',
                                      epub.read_bytes())
        self.assertEqual(status, 200)
        meta = json.loads(body)['metadata']
        self.assertEqual(meta['title'], 'Test Book')
        self.assertEqual(meta['format'], 'application/epub+zip')
        self.assertEqual(meta['authors'], 'Jane Author')
        self.assertTrue(Path(self.tmp.name, 'library', 'alice', 'my-book.epub').exists())
        # same upload again: no duplicate file, same uuid
        _, _, body2 = self.upload(tokens['access_token'], 'my-book.epub.epub', epub.read_bytes())
        self.assertEqual(json.loads(body2)['metadata']['inventoryUuid'], meta['inventoryUuid'])
        self.assertFalse(Path(self.tmp.name, 'library', 'alice', 'my-book-2.epub').exists())
        _, _, inv = self.request('GET', '/v8/inventory', headers=self.auth(tokens['access_token']))
        items = {i['uuid']: i for i in json.loads(inv)['page']['content']}
        self.assertEqual(items[meta['inventoryUuid']]['contentSources'], ['USER_UPLOAD'])

    def test_upload_rejects_non_epub(self):
        tokens, _ = self.login()
        status, _, _ = self.upload(tokens['access_token'], 'x.epub', b'not a zip')
        self.assertEqual(status, 400)

    def test_recommendations_suggest_shared_books_not_yet_added(self):
        tokens, _ = self.login()
        self.assertEqual(self.request('GET', '/v2/recommendations')[0], 401)

        def recos():
            status, _, body = self.request('GET', '/v2/recommendations?count=12&type=ebook',
                                           headers=self.auth(tokens['access_token']))
            self.assertEqual(status, 200)
            return json.loads(body)
        first = recos()
        self.assertIsInstance(first, list)
        self.assertIn('Shared Book', [r['title'] for r in first])
        self.assertNotIn('Test Book', [r['title'] for r in first])  # own books are not suggested
        shared = next(r for r in first if r['title'] == 'Shared Book')
        self.assertIn('/shop?id=' + shared['publicationId'], shared['shop_url'])
        self.store_add(tokens['access_token'], shared['publicationId'])
        self.assertNotIn('Shared Book', [r['title'] for r in recos()])
        self.store_add(tokens['access_token'], shared['publicationId'], 'remove')

    def test_shop_accepts_concatenated_url(self):
        status, _, body = self.request('GET', '/shophttps://books.example.com/onilotapi/shop')
        self.assertEqual(status, 200)
        self.assertIn(b'onilot store', body)

    def test_tags_are_a_list_per_publication(self):
        tokens, _ = self.login()
        path = '/publications/tag-book/tags'
        resp = self.sync(tokens['access_token'], '0', [
            {'op': 'add', 'path': path, 'value': {'name': 'A', 'modified': 1, 'transientId': 1}},
            {'op': 'add', 'path': path, 'value': {'name': 'B', 'modified': 1, 'transientId': 2}}])
        echoes = [p for p in resp['patches'] if p['path'].startswith(path + '/')]
        self.assertEqual(sorted(p['value']['transientId'] for p in echoes), ['1', '2'])
        self.assertEqual(len({p['path'] for p in echoes}), 2)  # two separate documents
        first = echoes[0]['path']
        resp = self.sync(tokens['access_token'], resp['revision'],
                         [{'op': 'remove', 'path': first}])
        doc = json.loads(Path(self.tmp.name, 'data', 'users', 'alice', 'reading-metadata.json').read_text())
        self.assertTrue(doc['docs'][first]['deleted'])
        self.assertEqual(sum(1 for k, v in doc['docs'].items()
                             if k.startswith(path + '/') and not v['deleted']), 1)

    def test_reseller_config_points_account_pages_at_server(self):
        _, _, body = self.request('GET', '/v1/resellers/1/client-types/x/config')
        config = json.loads(body)['config']
        self.assertEqual(config['URL_HANDSHAKE'], self.base + '/manage/library')
        self.assertEqual(config['URL_DEVICE_MANAGEMENT'], self.base + '/manage/devices')

    def test_manage_pages_render_and_use_handshake_config(self):
        for page in ('/manage/library', '/manage/devices?action=logindevicelimit&lang=de'):
            status, _, body = self.request('GET', page)
            self.assertEqual(status, 200)
            self.assertIn(b'handshake_config', body)
            self.assertIn(b'Bearer', body)

    def test_manage_api_requires_token(self):
        self.assertEqual(self.request('GET', '/onilot/library')[0], 401)
        self.assertEqual(self.request('GET', '/onilot/devices')[0], 401)
        self.assertEqual(self.request('POST', '/onilot/devices/sign-out', body=b'')[0], 401)

    def test_devices_are_tracked_and_sign_out_revokes_tokens(self):
        tokens, _ = self.login()
        headers = {**self.auth(tokens['access_token']), 'Hardware-Id': 'hw-test-1',
                   'client_type': 'TOLINO_VISION_7', 'client_version': '5.18.264769'}
        self.request('GET', '/v8/inventory', headers=headers)
        # a later request without client headers must not erase them
        self.request('GET', '/v2/recommendations', headers={
            **self.auth(tokens['access_token']), 'Hardware-Id': 'hw-test-1'})
        status, _, body = self.request('GET', '/onilot/devices', headers=headers)
        devices = {d['id']: d for d in json.loads(body)['devices']}
        self.assertEqual(devices['hw-test-1']['client_type'], 'TOLINO_VISION_7')
        _, _, body = self.request('GET', '/onilot/library', headers=headers)
        self.assertIn('Test Book', [b['title'] for b in json.loads(body)['books']])
        status, _, _ = self.request('POST', '/onilot/devices/sign-out', body=b'', headers=headers)
        self.assertEqual(status, 200)
        self.assertEqual(self.request('GET', '/v8/inventory', headers=headers)[0], 401)
        form = {'refresh_token': tokens['refresh_token'], 'grant_type': 'refresh_token'}
        self.assertEqual(self.request('POST', '/oauth/token', form=form)[0], 400)


    # ---- multi-user ------------------------------------------------------------------------

    def inventory_titles(self, token):
        _, _, body = self.request('GET', '/v8/inventory', headers=self.auth(token))
        return {i['title']: i for i in json.loads(body)['page']['content']}

    def shared_uuid(self, title):
        import hashlib
        name = {'Shared Book': 'shared.epub', 'Admin Upload': 'admin-up.epub'}[title]
        return hashlib.sha1(('shared/' + name).encode()).hexdigest()

    def store_add(self, token, uuid, verb='add', header=True, form_token=False):
        form = {'q': ''}
        if form_token:
            form['t'] = token
        return self.request('POST', '/shop/%s/%s' % (verb, uuid), form=form,
                            headers={'X-Access-Token': token} if header else {})

    def test_users_see_only_own_books_until_they_add_shared_ones(self):
        alice, _ = self.login()
        bob, _ = self.login('bob', BOB_PASSWORD)
        self.assertIn('Test Book', self.inventory_titles(alice['access_token']))
        self.assertNotIn('Test Book', self.inventory_titles(bob['access_token']))
        self.assertNotIn('Shared Book', self.inventory_titles(bob['access_token']))
        status, headers, _ = self.store_add(bob['access_token'], self.shared_uuid('Shared Book'))
        self.assertEqual(status, 303)
        self.assertIn('Added', urllib.parse.unquote_plus(headers['Location']))
        self.assertIn('Shared Book', self.inventory_titles(bob['access_token']))
        self.assertNotIn('Shared Book', self.inventory_titles(alice['access_token']))
        self.store_add(bob['access_token'], self.shared_uuid('Shared Book'), 'remove')

    def test_access_token_names_the_user(self):
        bob, _ = self.login('bob', BOB_PASSWORD)
        payload = bob['access_token'].split('.')[1]
        payload += '=' * (-len(payload) % 4)
        self.assertEqual(json.loads(base64.urlsafe_b64decode(payload))['sub'], 'bob')

    def test_reading_progress_is_per_user(self):
        alice, _ = self.login()
        bob, _ = self.login('bob', BOB_PASSWORD)
        path = '/publications/per-user-book/bookmark'
        self.sync(alice['access_token'], '0', [{'op': 'add', 'path': path, 'value': {
            'position': 'alice-pos', 'modified': 10, 'transientId': 1}}])
        resp = self.sync(bob['access_token'], '0', [])
        self.assertFalse([p for p in resp['patches'] if p['path'].startswith(path)])
        bob_file = self.data / 'users' / 'bob' / 'reading-metadata.json'
        self.assertNotIn('alice-pos', bob_file.read_text() if bob_file.exists() else '')

    def test_book_links_are_bound_to_the_user(self):
        alice, _ = self.login()
        bob, _ = self.login('bob', BOB_PASSWORD)
        uuid = self.inventory_titles(alice['access_token'])['Test Book']['uuid']
        _, _, body = self.request('GET', '/v1/inventory/%s/downloadinfo' % uuid,
                                  headers=self.auth(alice['access_token']))
        url = json.loads(body)['contentUrl']
        self.assertIn('/content/alice/', url)
        self.assertEqual(self.request('GET', url)[0], 200)
        self.assertEqual(self.request('GET', url.replace('/content/alice/', '/content/bob/'))[0],
                         403)
        # bob cannot fetch alice's book through his own token either
        self.assertEqual(self.request('GET', '/v1/inventory/%s/downloadinfo' % uuid,
                                      headers=self.auth(bob['access_token']))[0], 404)

    def test_uploads_go_to_the_uploaders_directory(self):
        bob, _ = self.login('bob', BOB_PASSWORD)
        epub = Path(self.tmp.name, 'bob-up.epub')
        make_epub(epub, 'Bob Upload')
        status, _, _ = self.upload(bob['access_token'], 'bobs.epub', epub.read_bytes())
        self.assertEqual(status, 200)
        self.assertTrue((self.lib / 'bob' / 'bobs.epub').exists())
        alice, _ = self.login()
        self.assertNotIn('Bob Upload', self.inventory_titles(alice['access_token']))


    def test_store_shows_add_buttons_only_with_token(self):
        bob, _ = self.login('bob', BOB_PASSWORD)
        _, _, anonymous = self.request('GET', '/shop')
        self.assertIn(b'Shared Book', anonymous)
        self.assertNotIn(b'>Add</button>', anonymous)
        _, _, page = self.request('GET', '/shop', headers={'X-Access-Token': bob['access_token']})
        self.assertIn(b'>Add</button>', page)
        self.assertIn(b'/covers/bob/', page)

    def test_store_actions_need_a_token(self):
        uuid = self.shared_uuid('Shared Book')
        status, _, _ = self.request('POST', '/shop/add/' + uuid, form={'q': ''})
        self.assertEqual(status, 401)
        status, _, _ = self.request('POST', '/shop/add/' + uuid, form={'t': 'a.b.c'})
        self.assertEqual(status, 401)

    def test_store_form_token_fallback(self):
        bob, _ = self.login('bob', BOB_PASSWORD)
        uuid = self.shared_uuid('Shared Book')
        status, _, _ = self.store_add(bob['access_token'], uuid, header=False, form_token=True)
        self.assertEqual(status, 303)
        self.assertIn('Shared Book', self.inventory_titles(bob['access_token']))
        self.store_add(bob['access_token'], uuid, 'remove')

    def test_shared_download_needs_adding_and_removal_reaches_reader(self):
        bob, _ = self.login('bob', BOB_PASSWORD)
        token = bob['access_token']
        uuid = self.shared_uuid('Shared Book')
        info = '/v1/inventory/%s/downloadinfo' % uuid
        self.assertEqual(self.request('GET', info, headers=self.auth(token))[0], 404)
        _, _, body = self.request('GET', '/v8/inventory', headers=self.auth(token))
        before = json.loads(body)['lastModifiedDate']
        time.sleep(0.01)
        self.store_add(token, uuid)
        _, _, body = self.request('GET', '/v8/inventory', headers=self.auth(token))
        self.assertGreater(json.loads(body)['lastModifiedDate'], before)  # no 304 after adding
        self.assertEqual(self.request('GET', info, headers=self.auth(token))[0], 200)
        self.store_add(token, uuid, 'remove')
        _, _, body = self.request('GET', '/v8/inventory', headers=self.auth(token))
        inv = json.loads(body)
        self.assertIn({'uuid': uuid}, inv['deletedContent'])
        self.assertEqual(self.request('GET', info, headers=self.auth(token))[0], 404)
        # adding again takes it off the deleted list
        self.store_add(token, uuid)
        _, _, body = self.request('GET', '/v8/inventory', headers=self.auth(token))
        self.assertNotIn({'uuid': uuid}, json.loads(body)['deletedContent'])
        self.store_add(token, uuid, 'remove')

    def test_cover_fallbacks(self):
        alice, _ = self.login()
        make_epub_without_cover(self.lib / 'alice' / 'nocover.epub', 'A Book Without Any Cover')
        make_epub_without_cover(self.lib / 'alice' / 'firstpage.epub', 'First Page Picture',
                                first_page_image=True)
        items = self.inventory_titles(alice['access_token'])
        status, headers, body = self.request(
            'GET', items['A Book Without Any Cover']['fileResources'][0]['resource'])
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Type'], 'image/svg+xml')
        self.assertIn(b'A Book Without', body)
        self.assertIn(b'<svg', body)
        status, headers, body = self.request(
            'GET', items['First Page Picture']['fileResources'][0]['resource'])
        self.assertEqual((status, headers['Content-Type'], body), (200, 'image/png',
                                                                   b'\x89PNGfake'))
        (self.lib / 'alice' / 'nocover.epub').unlink()
        (self.lib / 'alice' / 'firstpage.epub').unlink()

    def test_generated_cover_escapes_title(self):
        import importlib
        sys.path.insert(0, str(REPO))
        server = importlib.import_module('onilot.server')
        svg = server.generated_cover('<script>alert(1)</script> & more', 'A & B')
        self.assertNotIn(b'<script>', svg)
        self.assertIn(b'&amp;', svg)


    # ---- series, collections, e-paper store ------------------------------------------------

    def new_user(self, name, books=()):
        """A fresh user with its own library: [(title, series, index)]."""
        result = user_cli(self.data, 'add', name, '--password-stdin', stdin='pw-%s-123\n' % name)
        self.assertEqual(result.returncode, 0, result.stderr)
        (self.lib / name).mkdir(exist_ok=True)
        for i, (title, series, index) in enumerate(books):
            make_series_epub(self.lib / name / ('%d.epub' % i), title, series, index,
                             epub3=i % 2 == 1)
        tokens, _ = self.login(name, 'pw-%s-123' % name)
        return tokens['access_token'], {t: v['uuid'] for t, v in
                                        self.inventory_titles(tokens['access_token']).items()}

    @staticmethod
    def tag_patches(resp, op=None):
        return [p for p in resp['patches'] if '/tags/' in p['path'] and (op is None or
                                                                          p['op'] == op)]

    def test_series_metadata(self):
        sys.path.insert(0, str(REPO))
        import importlib
        server = importlib.import_module('onilot.server')
        self.assertEqual(server.series_label({'series': 'Saga', 'series_index': 3.5}), 'Saga #3.5')
        self.assertEqual(server.series_label({'series': 'Saga', 'series_index': 2.0}), 'Saga #2')
        self.assertEqual(server.series_label({'series': None, 'series_index': None}), '')
        books = {}
        for i, epub3 in enumerate((False, True)):
            path = Path(self.tmp.name, 'series%d.epub' % i)
            make_series_epub(path, 'Book %d' % i, 'Saga', '3.5', epub3=epub3)
            books[epub3] = server.Library([])._metadata(path)
        for book in books.values():
            self.assertEqual((book['series'], book['series_index']), ('Saga', 3.5))

    def test_series_become_collections_on_the_reader(self):
        token, books = self.new_user('dora', [('Saga One', 'Saga', '1'),
                                              ('Saga Two', 'Saga', '2')])
        resp = self.sync(token, '0', [])
        adds = self.tag_patches(resp, 'add')
        self.assertEqual({p['path'].split('/')[2] for p in adds}, set(books.values()))
        self.assertTrue(all(p['value']['name'] == 'Saga' and p['value']['category'] ==
                            'collection' and 'transientId' not in p['value'] for p in adds))
        # nothing new on the next sync
        self.assertEqual(self.tag_patches(self.sync(token, resp['revision'], [])), [])
        # a book removed from the collection on the reader stays removed
        removed = adds[0]['path']
        resp = self.sync(token, resp['revision'], [{'op': 'remove', 'path': removed}])
        resp = self.sync(token, resp['revision'], [])
        self.assertEqual(self.tag_patches(resp), [])
        # a second reader never gets the removed tag, only the remaining one, as an "add"
        fresh = self.sync(token, '0', [])
        self.assertEqual([p['path'] for p in self.tag_patches(fresh)], [adds[1]['path']])
        self.assertEqual(self.tag_patches(fresh)[0]['op'], 'add')
        # after deleting the whole collection, a new book of the series does not bring it back
        resp = self.sync(token, resp['revision'], [{'op': 'remove', 'path': adds[1]['path']}])
        make_series_epub(self.lib / 'dora' / 'three.epub', 'Saga Three', 'Saga', '3')
        resp = self.sync(token, resp['revision'], [])
        self.assertEqual(self.tag_patches(resp), [])

    def test_series_collection_survives_a_book_leaving_and_returning(self):
        token, books = self.new_user('lea', [('Loop One', 'Loop', '1'),
                                             ('Loop Two', 'Loop', '2')])
        resp = self.sync(token, '0', [])
        moved = self.lib / 'lea' / '0.epub'
        parked = Path(self.tmp.name, 'parked.epub')
        moved.rename(parked)
        resp = self.sync(token, resp['revision'], [])
        self.assertEqual([p['op'] for p in self.tag_patches(resp)], ['remove'])
        parked.rename(moved)
        resp = self.sync(token, resp['revision'], [])
        self.assertEqual([(p['op'], p['path'].split('/')[2]) for p in self.tag_patches(resp)],
                         [('add', books['Loop One'])])

    def test_series_collection_follows_a_rename(self):
        token, _ = self.new_user('ren', [('Cycle One', 'Cycle', '1')])
        resp = self.sync(token, '0', [])
        path = self.tag_patches(resp, 'add')[0]['path']
        resp = self.sync(token, resp['revision'], [{'op': 'replace', 'path': path, 'value': {
            'name': 'My Cycle', 'category': 'collection', 'modified': 10 ** 13}}])
        make_series_epub(self.lib / 'ren' / 'two.epub', 'Cycle Two', 'Cycle', '2')
        resp = self.sync(token, resp['revision'], [])
        self.assertEqual([p['value']['name'] for p in self.tag_patches(resp, 'add')],
                         ['My Cycle'])

    def test_bookmarks_from_other_readers_stay_replace(self):
        token, _ = self.new_user('bea')
        self.sync(token, '0', [{'op': 'add', 'path': '/publications/x/bookmark', 'value': {
            'progress': 0.3, 'modified': 5, 'transientId': 1}}])
        fresh = self.sync(token, '0', [])
        ops = [p['op'] for p in fresh['patches'] if '/bookmark/' in p['path']]
        self.assertEqual(ops, ['replace'])

    def admin_collections(self, user, cookie, csrf, **form):
        status, headers, _ = self.request(
            'POST', self.origin + '/admin/users/%s/collections' % user,
            headers={'Cookie': cookie}, form={'csrf': csrf, **form})
        self.assertEqual(status, 303)
        return urllib.parse.unquote(headers['Location'])

    def test_admin_collections_reach_the_reader(self):
        token, books = self.new_user('cleo', [('Alpha', '', ''), ('Beta', '', '')])
        start = self.sync(token, '0', [])['revision']
        _, cookie, csrf = self.admin_login()
        location = self.admin_collections('cleo', cookie, csrf, action='create',
                                          name='Favourites', uuid=books['Alpha'])
        self.assertNotIn('err=1', location)
        self.admin_collections('cleo', cookie, csrf, action='add', name='Favourites',
                               uuid=books['Beta'])
        resp = self.sync(token, start, [])
        adds = self.tag_patches(resp, 'add')
        self.assertEqual(sorted(p['path'].split('/')[2] for p in adds), sorted(books.values()))
        self.assertTrue(all(p['value']['name'] == 'Favourites' for p in adds))
        _, _, page = self.request('GET', self.origin + '/admin/users/cleo/collections',
                                  headers={'Cookie': cookie})
        self.assertIn(b'Favourites', page)
        self.assertIn(b'Beta', page)

        self.admin_collections('cleo', cookie, csrf, action='rename', name='Favourites',
                               new='Best')
        resp = self.sync(token, resp['revision'], [])
        self.assertEqual({(p['op'], p['value']['name']) for p in self.tag_patches(resp)},
                         {('replace', 'Best')})
        alpha_tag = next(p['path'] for p in adds if books['Alpha'] in p['path'])
        self.admin_collections('cleo', cookie, csrf, action='remove', key=alpha_tag)
        resp = self.sync(token, resp['revision'], [])
        self.assertEqual([(p['op'], p['path']) for p in self.tag_patches(resp)],
                         [('remove', alpha_tag)])
        self.admin_collections('cleo', cookie, csrf, action='delete', name='Best')
        resp = self.sync(token, resp['revision'], [])
        self.assertEqual([p['op'] for p in self.tag_patches(resp)], ['remove'])
        _, _, page = self.request('GET', self.origin + '/admin/users/cleo/collections',
                                  headers={'Cookie': cookie})
        self.assertIn(b'No collections yet', page)

    def test_admin_shows_series_collections_before_a_sync(self):
        token, _ = self.new_user('sid', [('Arc One', 'Arc', '1')])
        _, cookie, _ = self.admin_login()
        _, _, page = self.request('GET', self.origin + '/admin/users/sid/collections',
                                  headers={'Cookie': cookie})
        self.assertIn(b'Arc <span class="badge">series</span>', page)
        self.assertEqual([p['value']['name'] for p in self.tag_patches(self.sync(token, '0', []))],
                         ['Arc'])

    def test_admin_shows_books_marked_finished(self):
        token, books = self.new_user('fin', [('Done Book', '', '')])
        self.sync(token, '0', [{'op': 'add', 'path': '/publications/%s/tags' % books['Done Book'],
                                'value': {'name': 'collection_finished_readings_name',
                                          'category': 'system', 'modified': 5,
                                          'transientId': 3}}])
        _, cookie, _ = self.admin_login()
        _, _, page = self.request('GET', self.origin + '/admin/users/fin',
                                  headers={'Cookie': cookie})
        self.assertIn(b'width:100%', page)
        self.assertIn(b'>finished<', page)
        _, _, page = self.request('GET', self.origin + '/admin/users/fin/collections',
                                  headers={'Cookie': cookie})
        self.assertNotIn(b'collection_finished_readings_name', page)  # not a user collection

    def test_admin_collections_reject_bad_input(self):
        token, books = self.new_user('vic', [('Gamma', '', '')])
        _, cookie, csrf = self.admin_login()
        shared = self.shared_uuid('Shared Book')  # not on vic's shelf
        for form in ({'action': 'create', 'name': 'X', 'uuid': shared},
                     {'action': 'create', 'name': '', 'uuid': books['Gamma']},
                     {'action': 'create', 'name': 'x' * 101, 'uuid': books['Gamma']},
                     {'action': 'remove', 'key': '/publications/%s/bookmark' % books['Gamma']},
                     {'action': 'explode'}):
            self.assertIn('err=1', self.admin_collections('vic', cookie, csrf, **form), form)
        status, headers, _ = self.request(
            'POST', self.origin + '/admin/users/vic/collections', headers={'Cookie': cookie},
            form={'csrf': 'bad', 'action': 'create', 'name': 'X', 'uuid': books['Gamma']})
        self.assertIn('err=1', headers['Location'])
        status, headers, _ = self.request(
            'POST', self.origin + '/admin/users/vic/collections',
            form={'csrf': csrf, 'action': 'create', 'name': 'X', 'uuid': books['Gamma']})
        self.assertNotIn('collections', headers.get('Location', ''))  # no session: login page
        self.assertEqual(self.tag_patches(self.sync(token, '0', [])), [])

    def test_store_pages_and_finds_series(self):
        make_series_epub(self.lib / 'shared' / 'tale1.epub', 'Tale Begins', 'Long Tale', '1')
        make_series_epub(self.lib / 'shared' / 'tale2.epub', 'Tale Ends', 'Long Tale', '2')
        try:
            _, _, page = self.request('GET', '/shop?q=long+tale')
            self.assertIn(b'Long Tale #1', page)
            self.assertIn(b'id="next"', page)
            self.assertLess(page.index(b'Tale Begins'), page.index(b'Tale Ends'))  # reading order
            self.assertNotIn(b'Shared Book', page)
        finally:
            (self.lib / 'shared' / 'tale1.epub').unlink()
            (self.lib / 'shared' / 'tale2.epub').unlink()

    def test_shop_shows_only_shared_books(self):
        status, _, body = self.request('GET', '/shop')
        self.assertEqual(status, 200)
        self.assertIn(b'Shared Book', body)
        self.assertNotIn(b'Test Book', body)

    def test_sign_out_only_affects_own_readers(self):
        alice, _ = self.login()
        bob, _ = self.login('bob', BOB_PASSWORD)
        self.request('POST', '/onilot/devices/sign-out', body=b'',
                     headers=self.auth(bob['access_token']))
        self.assertEqual(self.request('GET', '/v8/inventory',
                                      headers=self.auth(bob['access_token']))[0], 401)
        self.assertEqual(self.request('GET', '/v8/inventory',
                                      headers=self.auth(alice['access_token']))[0], 200)

    def test_user_removed_via_cli_loses_access_immediately(self):
        self.assertEqual(user_cli(self.data, 'add', 'gone', '--password-stdin',
                                  stdin='gone-password\n').returncode, 0)
        tokens, _ = self.login('gone', 'gone-password')
        self.assertEqual(self.request('GET', '/v8/inventory',
                                      headers=self.auth(tokens['access_token']))[0], 200)
        self.assertEqual(user_cli(self.data, 'remove', 'gone').returncode, 0)
        self.assertEqual(self.request('GET', '/v8/inventory',
                                      headers=self.auth(tokens['access_token']))[0], 401)
        form = {'refresh_token': tokens['refresh_token'], 'grant_type': 'refresh_token'}
        self.assertEqual(self.request('POST', '/oauth/token', form=form)[0], 400)

    # ---- admin page --------------------------------------------------------------------------

    @property
    def origin(self):
        return self.base.rsplit('/', 1)[0]

    def test_admin_page_lives_at_the_origin(self):
        status, _, body = self.request('GET', self.origin + '/admin')
        self.assertEqual(status, 200)
        self.assertIn(('action="%s/admin/login"' % self.origin).encode(), body)
        status, headers, _ = self.request('GET', '/admin')  # old address under the API prefix
        self.assertEqual((status, headers['Location']), (301, self.origin + '/admin'))
        _, headers, _ = self.request('POST', self.origin + '/admin/login',
                                     form={'username': 'alice', 'password': PASSWORD})
        self.assertIn('Path=/admin;', headers['Set-Cookie'])
        self.assertEqual(headers['Location'], self.origin + '/admin')

    def admin_login(self, user='alice', password=PASSWORD):
        status, headers, _ = self.request('POST', self.origin + '/admin/login',
                                          form={'username': user, 'password': password})
        if status != 303:
            return status, None, None
        cookie = headers['Set-Cookie'].split(';')[0]
        _, _, page = self.request('GET', self.origin + '/admin', headers={'Cookie': cookie})
        csrf = page.decode().split('name=csrf value="')[1].split('"')[0]
        return status, cookie, csrf

    def test_admin_login_requires_admin(self):
        status, _, _ = self.admin_login('bob', BOB_PASSWORD)
        self.assertEqual(status, 403)
        status, _, _ = self.admin_login('alice', 'wrong')
        self.assertEqual(status, 403)
        status, cookie, csrf = self.admin_login()
        self.assertEqual(status, 303)
        self.assertTrue(cookie.startswith('onilot_admin='))

    def test_admin_cookie_flags(self):
        status, headers, _ = self.request('POST', self.origin + '/admin/login',
                                          form={'username': 'alice', 'password': PASSWORD})
        flags = headers['Set-Cookie']
        self.assertIn('HttpOnly', flags)
        self.assertIn('SameSite=Strict', flags)

    def test_admin_actions_need_session_and_csrf(self):
        form = {'action': 'add', 'name': 'mallory', 'password': 'mallory-pw'}
        status, headers, _ = self.request('POST', self.origin + '/admin/users', form=form)
        self.assertEqual(status, 303)  # redirected to the login page
        self.assertNotIn('mallory', user_cli(self.data, 'list').stdout)
        _, cookie, csrf = self.admin_login()
        status, headers, _ = self.request('POST', self.origin + '/admin/users',
                                          form={**form, 'csrf': 'bad'},
                                          headers={'Cookie': cookie})
        self.assertEqual(status, 303)
        self.assertIn('err=1', headers['Location'])
        self.assertNotIn('mallory', user_cli(self.data, 'list').stdout)

    def test_admin_add_reset_and_delete_user(self):
        _, cookie, csrf = self.admin_login()
        h = {'Cookie': cookie}
        self.request('POST', self.origin + '/admin/users', headers=h, form={
            'csrf': csrf, 'action': 'add', 'name': 'carol', 'password': 'carol-pass'})
        carol, _ = self.login('carol', 'carol-pass')
        self.request('POST', self.origin + '/admin/users/carol', headers=h, form={
            'csrf': csrf, 'action': 'passwd', 'password': 'carol-new-pass'})
        # password change signs carol's readers out
        self.assertEqual(self.request('GET', '/v8/inventory',
                                      headers=self.auth(carol['access_token']))[0], 401)
        self.login('carol', 'carol-new-pass')
        status, headers, _ = self.request('POST', self.origin + '/admin/users/carol', headers=h,
                                          form={'csrf': csrf, 'action': 'delete'})
        self.assertIn('Deleted', urllib.parse.unquote(headers['Location']))
        status, _, body = self.request('POST', '/oauth/authorize',
                                       form={'username': 'carol', 'password': 'carol-new-pass'})
        self.assertIn(b'Wrong user or password', body)
        # an admin cannot delete themselves
        _, headers, _ = self.request('POST', self.origin + '/admin/users/alice', headers=h,
                                     form={'csrf': csrf, 'action': 'delete'})
        self.assertIn('err=1', headers['Location'])


    def test_admin_user_page_shows_reading_progress(self):
        alice, _ = self.login()
        uuid = self.inventory_titles(alice['access_token'])['Test Book']['uuid']
        self.sync(alice['access_token'], '0', [{'op': 'add',
                  'path': '/publications/%s/bookmark' % uuid,
                  'value': {'position': 'p', 'progress': 0.42, 'modified': 5, 'transientId': 9}}])
        _, cookie, _ = self.admin_login()
        status, _, body = self.request('GET', self.origin + '/admin/users/alice',
                                       headers={'Cookie': cookie})
        self.assertEqual(status, 200)
        self.assertIn(b'Test Book', body)
        self.assertIn(b'42%', body)

    def test_admin_pages_and_covers_need_session(self):
        for path in ('/admin/users/alice', '/admin/library?owner=shared',
                     '/admin/cover/shared/' + '0' * 40):
            status, headers, _ = self.request('GET', self.origin + path)
            self.assertEqual(status, 303, path)
            self.assertTrue(headers['Location'].endswith('/admin'), path)

    def test_admin_upload_and_delete_shared_book_reaches_readers(self):
        _, cookie, csrf = self.admin_login()
        epub = Path(self.tmp.name, 'admin-up.epub')
        make_epub(epub, 'Admin Upload')
        boundary = 'b0undary'
        body = (('--%s\r\nContent-Disposition: form-data; name="file"; filename="admin-up.epub"'
                 '\r\nContent-Type: application/epub+zip\r\n\r\n' % boundary).encode()
                + epub.read_bytes() + ('\r\n--%s--\r\n' % boundary).encode())
        headers = {'Cookie': cookie, 'X-CSRF-Token': csrf,
                   'Content-Type': 'multipart/form-data; boundary=%s' % boundary}
        status, _, _ = self.request('POST', self.origin + '/admin/library/upload?owner=shared',
                                    body=body, headers={**headers, 'X-CSRF-Token': 'bad'})
        self.assertEqual(status, 403)
        status, _, _ = self.request('POST', self.origin + '/admin/library/upload?owner=shared',
                                    body=body, headers=headers)
        self.assertEqual(status, 200)
        bob, _ = self.login('bob', BOB_PASSWORD)
        self.assertNotIn('Admin Upload', self.inventory_titles(bob['access_token']))
        self.store_add(bob['access_token'], self.shared_uuid('Admin Upload'))
        item = self.inventory_titles(bob['access_token'])['Admin Upload']
        status, _, page = self.request('GET', self.origin + '/admin/library?owner=shared',
                                       headers={'Cookie': cookie})
        self.assertIn(b'Admin Upload', page)
        status, _, _ = self.request('POST', self.origin + '/admin/library/shared/' + item['uuid'],
                                    headers={'Cookie': cookie},
                                    form={'csrf': csrf, 'action': 'delete'})
        self.assertEqual(status, 303)
        self.assertFalse((self.lib / 'shared' / 'admin-up.epub').exists())
        _, _, inv = self.request('GET', '/v8/inventory', headers=self.auth(bob['access_token']))
        inv = json.loads(inv)
        self.assertIn({'uuid': item['uuid']}, inv['deletedContent'])
        self.assertNotIn('Admin Upload', [i['title'] for i in inv['page']['content']])

    def test_oversized_request_is_rejected_before_reading(self):
        import http.client
        host, port = urllib.parse.urlsplit(self.base).netloc.split(':')
        conn = http.client.HTTPConnection(host, int(port), timeout=10)
        conn.putrequest('POST', '/onilotapi/v1/inventory/user-uploads')
        conn.putheader('Content-Length', str(10 * 1024 ** 3))
        conn.endheaders()
        self.assertEqual(conn.getresponse().status, 413)
        conn.close()

    def test_admin_rejects_bad_user_names(self):
        _, cookie, csrf = self.admin_login()
        for name in ('../etc', 'shared', 'with space', 'a/b', ''):
            _, headers, _ = self.request('POST', self.origin + '/admin/users', headers={'Cookie': cookie},
                                         form={'csrf': csrf, 'action': 'add', 'name': name,
                                               'password': 'long-enough'})
            self.assertIn('err=1', headers['Location'], name)
        self.assertFalse(Path(self.tmp.name, 'etc').exists())

    def test_log_contains_no_password_or_tokens(self):
        tokens, _ = self.login()
        log = Path(self.tmp.name, 'data', 'requests.jsonl').read_text()
        self.assertNotIn(PASSWORD, log)
        self.assertNotIn(tokens['access_token'], log)
        self.assertNotIn(tokens['refresh_token'], log)

    def test_unknown_path_is_404_and_logged(self):
        self.assertEqual(self.request('GET', '/v9/does-not-exist')[0], 404)
        log = Path(self.tmp.name, 'data', 'requests.jsonl').read_text().splitlines()
        self.assertTrue(any(json.loads(l)['path'] == '/v9/does-not-exist' for l in log))


if __name__ == '__main__':
    unittest.main()


class ConfigTest(unittest.TestCase):
    def run_server(self, env):
        clean = {k: v for k, v in os.environ.items() if not k.startswith('ONILOT_')}
        with tempfile.TemporaryDirectory() as tmp:
            return subprocess.run([sys.executable, '-m', 'onilot'], cwd=str(REPO),
                                  env={**clean, 'ONILOT_DATA': tmp + '/data',
                                       'ONILOT_LIBRARY': tmp + '/lib', **env},
                                  capture_output=True, text=True, timeout=30)

    def test_version_flag(self):
        result = subprocess.run([sys.executable, '-m', 'onilot', '--version'], cwd=str(REPO),
                                capture_output=True, text=True, timeout=30)
        sys.path.insert(0, str(REPO))
        from onilot import __version__
        self.assertEqual(result.stdout.strip(), 'onilot ' + __version__)

    def test_refuses_to_start_without_public_url_or_password(self):
        result = self.run_server({'ONILOT_PASSWORD': 'x'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ONILOT_PUBLIC_URL', result.stderr)
        result = self.run_server({'ONILOT_PUBLIC_URL': 'https://b.example/api'})
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('ONILOT_PASSWORD', result.stderr)

    def test_rejects_malformed_public_url(self):
        result = self.run_server({'ONILOT_PASSWORD': 'x', 'ONILOT_PUBLIC_URL': 'books.example/'})
        self.assertNotEqual(result.returncode, 0)


class UserCliTest(unittest.TestCase):
    def test_add_list_passwd_remove(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(user_cli(tmp, 'add', 'dora', '--admin', '--password-stdin',
                                      stdin='dora-password\n').returncode, 0)
            self.assertEqual(user_cli(tmp, 'add', 'ed', '--password-stdin',
                                      stdin='ed-password\n').returncode, 0)
            self.assertEqual(user_cli(tmp, 'list').stdout.split(), ['dora', '(admin)', 'ed'])
            short = user_cli(tmp, 'add', 'fay', '--password-stdin', stdin='short\n')
            self.assertNotEqual(short.returncode, 0)
            self.assertIn('at least', short.stderr)
            last = user_cli(tmp, 'remove', 'dora')
            self.assertNotEqual(last.returncode, 0)
            self.assertIn('last admin', last.stderr)
            self.assertEqual(user_cli(tmp, 'passwd', 'ed', '--password-stdin',
                                      stdin='ed-new-password\n').returncode, 0)
            self.assertEqual(user_cli(tmp, 'remove', 'ed').returncode, 0)
            stored = json.loads(Path(tmp, 'users.json').read_text())
            self.assertTrue(stored['users']['dora']['password'].startswith('pbkdf2_sha256$'))
            self.assertNotIn('dora-password', Path(tmp, 'users.json').read_text())


class MigrationTest(unittest.TestCase):
    """A single-user install (pre multi-user) keeps working after the upgrade."""

    def test_single_user_data_moves_to_first_admin(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib, data = Path(tmp, 'library'), Path(tmp, 'data')
            lib.mkdir()
            data.mkdir()
            make_epub(lib / 'old.epub', 'Old Book')
            (data / 'reading-metadata.json').write_text(json.dumps({
                'revision': 3, 'next_id': 2, 'docs': {'/publications/x/bookmark': {
                    'id': 1, 'revision': 3, 'value': {'position': 'old'}, 'deleted': False}}}))
            (data / 'uploads.json').write_text(json.dumps(['old.epub']))
            old_token = 'aaaaaaaaaa.bbbbbbbbbb.cccccccccc'
            (data / 'tokens.json').write_text(json.dumps({
                'refresh': ['old-refresh'], 'access': {old_token: time.time() + 3600}}))
            key = b'k' * 32
            (data / 'url-signing.key').write_bytes(key)
            import hashlib
            import hmac as hmac_mod
            uuid = hashlib.sha1(b'old.epub').hexdigest()
            sig = hmac_mod.new(key, ('covers:%s:4102444800' % uuid).encode(),
                               hashlib.sha256).hexdigest()
            port = free_port()
            base = 'http://127.0.0.1:%d/api' % port
            proc = start_server(base, port, lib, data, {'ONILOT_ADMIN_USER': 'winter',
                                                        'ONILOT_PASSWORD': PASSWORD})
            try:
                self.assertTrue((lib / 'winter' / 'old.epub').exists())
                self.assertTrue((data / 'users' / 'winter' / 'reading-metadata.json').exists())
                req = urllib.request.Request(base + '/v8/inventory',
                                             headers={'Authorization': 'Bearer ' + old_token})
                with urllib.request.urlopen(req) as resp:
                    items = json.loads(resp.read())['page']['content']
                self.assertEqual(items[0]['uuid'], uuid)          # same uuid as before
                self.assertEqual(items[0]['contentSources'], ['USER_UPLOAD'])
                # a cover URL stored on the reader before the upgrade still works
                with urllib.request.urlopen('%s/covers/%s/4102444800/%s' % (base, uuid, sig)) as r:
                    self.assertEqual(r.status, 200)
                form = urllib.parse.urlencode({'grant_type': 'refresh_token',
                                               'refresh_token': 'old-refresh'}).encode()
                with urllib.request.urlopen(base + '/oauth/token', data=form) as resp:
                    self.assertEqual(resp.status, 200)
            finally:
                proc.terminate()
                proc.wait()
                proc.stdout.close()
