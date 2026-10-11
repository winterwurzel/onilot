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

SERVER = Path(__file__).resolve().parent.parent / 'onilot' / 'server.py'
PASSWORD = 'test-password'

OPF = """<?xml version="1.0"?>
<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="id">
 <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:identifier id="id">test-book</dc:identifier><dc:title>Test Book</dc:title>
  <dc:creator>Jane Author</dc:creator><dc:language>de</dc:language>
 </metadata>
 <manifest><item id="c" href="cover.jpg" media-type="image/jpeg" properties="cover-image"/>
 </manifest><spine/></package>"""
CONTAINER = """<?xml version="1.0"?>
<container xmlns="urn:oasis:names:tc:opendocument:xmlns:container" version="1.0">
 <rootfiles><rootfile full-path="OEBPS/content.opf"
  media-type="application/oebps-package+xml"/></rootfiles></container>"""


def make_epub(path):
    with zipfile.ZipFile(path, 'w') as z:
        z.writestr('mimetype', 'application/epub+zip')
        z.writestr('META-INF/container.xml', CONTAINER)
        z.writestr('OEBPS/content.opf', OPF)
        z.writestr('OEBPS/cover.jpg', b'\xff\xd8fakejpeg')


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args):
        return None


class DeviceFlowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        lib = Path(cls.tmp.name, 'library')
        lib.mkdir()
        make_epub(lib / 'test.epub')
        with socket.socket() as s:
            s.bind(('127.0.0.1', 0))
            port = s.getsockname()[1]
        cls.base = 'http://127.0.0.1:%d/onilotapi' % port
        cls.proc = subprocess.Popen(
            [sys.executable, '-I', str(SERVER)],
            env={**os.environ, 'ONILOT_PASSWORD': PASSWORD, 'ONILOT_PUBLIC_URL': cls.base,
                 'ONILOT_LIBRARY': str(lib), 'ONILOT_DATA': str(Path(cls.tmp.name, 'data')),
                 'ONILOT_PORT': str(port), 'ONILOT_REQUEST_LOG': '1'},
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        for _ in range(50):
            try:
                urllib.request.urlopen(cls.base + '/online-check')
                break
            except OSError:
                time.sleep(0.1)
        cls.opener = urllib.request.build_opener(NoRedirect)

    @classmethod
    def tearDownClass(cls):
        cls.proc.terminate()
        cls.proc.wait()
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

    def login(self):
        status, headers, _ = self.request('POST', '/oauth/authorize',
                                          form={'password': PASSWORD})
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
        status, headers, body = self.request('POST', '/oauth/authorize',
                                             form={'password': 'nope'})
        self.assertEqual(status, 200)
        self.assertNotIn('Location', headers)
        self.assertIn(b'Wrong password', body)

    def test_access_token_payload_has_sub(self):
        tokens, _ = self.login()
        payload = tokens['access_token'].split('.')[1]
        payload += '=' * (-len(payload) % 4)
        self.assertEqual(json.loads(base64.urlsafe_b64decode(payload))['sub'], 'user1')

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
        item = inventory['page']['content'][0]
        self.assertEqual(inventory['page']['numberOfElements'], 1)
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
        doc = Path(self.tmp.name, 'data', 'reading-metadata.json').read_text()
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
        self.assertTrue(Path(self.tmp.name, 'library', 'my-book.epub').exists())
        # same upload again: no duplicate file, same uuid
        _, _, body2 = self.upload(tokens['access_token'], 'my-book.epub.epub', epub.read_bytes())
        self.assertEqual(json.loads(body2)['metadata']['inventoryUuid'], meta['inventoryUuid'])
        self.assertFalse(Path(self.tmp.name, 'library', 'my-book-2.epub').exists())
        _, _, inv = self.request('GET', '/v8/inventory', headers=self.auth(tokens['access_token']))
        items = {i['uuid']: i for i in json.loads(inv)['page']['content']}
        self.assertEqual(items[meta['inventoryUuid']]['contentSources'], ['USER_UPLOAD'])

    def test_upload_rejects_non_epub(self):
        tokens, _ = self.login()
        status, _, _ = self.upload(tokens['access_token'], 'x.epub', b'not a zip')
        self.assertEqual(status, 400)

    def test_recommendations_are_a_list_of_library_books(self):
        tokens, _ = self.login()
        self.assertEqual(self.request('GET', '/v2/recommendations')[0], 401)
        status, _, body = self.request('GET', '/v2/recommendations?count=12&type=ebook',
                                       headers=self.auth(tokens['access_token']))
        recos = json.loads(body)
        self.assertEqual(status, 200)
        self.assertIsInstance(recos, list)
        self.assertIn('Test Book', [r['title'] for r in recos])
        self.assertTrue(all(r['cover_url'] and r['shop_url'] for r in recos))

    def test_shop_accepts_concatenated_url(self):
        status, _, body = self.request('GET', '/shophttps://books.example.com/onilotapi/shop')
        self.assertEqual(status, 200)
        self.assertIn(b'<h1>onilot</h1>', body)

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
        return subprocess.run([sys.executable, '-I', str(SERVER)], env={**clean, **env},
                              capture_output=True, text=True, timeout=10)

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
