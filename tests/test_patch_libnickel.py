"""Tests for tools/patch_libnickel.py against a synthetic library (no firmware needed)."""
import importlib.util
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path

TOOL = Path(__file__).resolve().parent.parent / 'tools' / 'patch_libnickel.py'
spec = importlib.util.spec_from_file_location('patch_libnickel', TOOL)
tool = importlib.util.module_from_spec(spec)
spec.loader.exec_module(tool)

FAKE_LIB = (b'\x7fELF' + b'\0' * 100 + b'https://api.pageplace.de\0junk'
            + b'https://api.prodref.pageplace.de\0' + b'\0' * 100)


def run(*args):
    return subprocess.run([sys.executable, '-I', str(TOOL), *args],
                          capture_output=True, text=True)


class PatchToolTest(unittest.TestCase):
    def test_suggest_pads_to_32_characters(self):
        for origin in ('https://b.io', 'https://books.example.com', 'http://10.0.0.5:8095'):
            url = tool.suggest_url(origin)
            self.assertEqual(len(url), 32, url)
            self.assertTrue(url.startswith(origin + '/') or url == origin)
            self.assertFalse(url.endswith('/'))

    def test_suggest_rejects_too_long_origin(self):
        result = run('--suggest', 'https://a-very-long-hostname.example.org')
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('at most', result.stderr)

    def test_patch_replaces_prodref_only(self):
        url = tool.suggest_url('https://books.example.com').encode()
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp, 'libnickel.so.1.0.0')
            lib.write_bytes(FAKE_LIB)
            result = run(str(lib), url.decode(), str(Path(tmp, 'out')))
            self.assertEqual(result.returncode, 0, result.stderr)
            patched = Path(tmp, 'out', 'libnickel.so.1.0.0').read_bytes()
            self.assertEqual(len(patched), len(FAKE_LIB))
            self.assertIn(url + b'\0', patched)
            self.assertIn(b'https://api.pageplace.de\0', patched)
            self.assertNotIn(b'prodref', patched)
            with tarfile.open(Path(tmp, 'out', 'Kobo.tgz')) as tar:
                member = tar.getmember('libnickel.so.1.0.0')
                self.assertEqual((member.mode, member.uid), (0o755, 0))
            with tarfile.open(Path(tmp, 'out', 'Kobo-restore.tgz')) as tar:
                self.assertEqual(tar.extractfile('libnickel.so.1.0.0').read(), FAKE_LIB)

    def test_refuses_wrong_length_and_double_patch(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib = Path(tmp, 'libnickel.so.1.0.0')
            lib.write_bytes(FAKE_LIB)
            self.assertNotEqual(run(str(lib), 'https://short.example', tmp).returncode, 0)
            url = tool.suggest_url('https://books.example.com')
            self.assertEqual(run(str(lib), url, str(Path(tmp, 'o1'))).returncode, 0)
            again = run(str(Path(tmp, 'o1', 'libnickel.so.1.0.0')), url, str(Path(tmp, 'o2')))
            self.assertNotEqual(again.returncode, 0)
            self.assertIn('already patched', again.stderr)


if __name__ == '__main__':
    unittest.main()
