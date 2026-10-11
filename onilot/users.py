"""User accounts: storage, password hashing and the `onilot user ...` command line."""
import argparse
import base64
import getpass
import hashlib
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
from pathlib import Path

NAME_RE = re.compile(r'^[a-z0-9][a-z0-9._-]{0,31}$')
RESERVED = {'shared'}
PBKDF2_ITERATIONS = 600_000
MIN_PASSWORD = 8


class UserError(ValueError):
    pass


def hash_password(password, iterations=PBKDF2_ITERATIONS):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, iterations)
    return 'pbkdf2_sha256$%d$%s$%s' % (iterations, base64.b64encode(salt).decode(),
                                       base64.b64encode(digest).decode())


def verify_password(password, stored):
    try:
        scheme, iterations, salt, digest = stored.split('$')
    except (AttributeError, ValueError):
        return False
    if scheme != 'pbkdf2_sha256':
        return False
    actual = hashlib.pbkdf2_hmac('sha256', password.encode(), base64.b64decode(salt),
                                 int(iterations))
    return hmac.compare_digest(actual, base64.b64decode(digest))


def check_name(name):
    if not NAME_RE.match(name or '') or name in RESERVED:
        raise UserError('user names are 1-32 characters a-z, 0-9, ".", "_", "-" '
                        '(starting with a letter or digit); "shared" is reserved')


def check_password(password):
    if len(password or '') < MIN_PASSWORD:
        raise UserError('passwords need at least %d characters' % MIN_PASSWORD)


class UserStore:
    """data/users.json. Re-read when the file changes, so the CLI and the server can share it."""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'users.json'
        self.lock = threading.RLock()
        self._mtime = None
        self.data = {'users': {}}
        self.reload()

    def exists(self):
        return self.path.exists()

    def reload(self):
        with self.lock:
            try:
                mtime = self.path.stat().st_mtime_ns
            except FileNotFoundError:
                return
            if mtime != self._mtime:
                self.data = json.loads(self.path.read_text())
                self.data.setdefault('users', {})
                self._mtime = mtime

    def _save(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.data, indent=1, sort_keys=True))
        tmp.chmod(0o600)
        os.replace(tmp, self.path)
        self._mtime = self.path.stat().st_mtime_ns

    def names(self):
        self.reload()
        return sorted(self.data['users'])

    def get(self, name):
        self.reload()
        return self.data['users'].get(name)

    def is_admin(self, name):
        user = self.get(name)
        return bool(user and user.get('admin'))

    def authenticate(self, name, password):
        user = self.get((name or '').strip().lower())
        if user is None:
            verify_password(password or '', hash_password('x', 1000))  # similar timing
            return None
        return name.strip().lower() if verify_password(password or '', user['password']) \
            else None

    def add(self, name, password, admin=False):
        check_name(name)
        check_password(password)
        with self.lock:
            self.reload()
            if name in self.data['users']:
                raise UserError('user %r already exists' % name)
            self.data['users'][name] = {'password': hash_password(password), 'admin': admin,
                                        'created': int(time.time())}
            self._save()

    def set_password(self, name, password):
        check_password(password)
        with self.lock:
            self.reload()
            if name not in self.data['users']:
                raise UserError('no user %r' % name)
            self.data['users'][name]['password'] = hash_password(password)
            self._save()

    def set_admin(self, name, admin):
        with self.lock:
            self.reload()
            if name not in self.data['users']:
                raise UserError('no user %r' % name)
            if not admin and self._other_admins(name) == 0:
                raise UserError('cannot remove the last admin')
            self.data['users'][name]['admin'] = admin
            self._save()

    def remove(self, name):
        with self.lock:
            self.reload()
            if name not in self.data['users']:
                raise UserError('no user %r' % name)
            if self.data['users'][name].get('admin') and self._other_admins(name) == 0:
                raise UserError('cannot remove the last admin')
            del self.data['users'][name]
            self._save()

    def _other_admins(self, name):
        return sum(1 for n, u in self.data['users'].items() if u.get('admin') and n != name)

    def legacy_user(self):
        self.reload()
        return self.data.get('legacy_user')

    def set_legacy_user(self, name):
        with self.lock:
            self.data['legacy_user'] = name
            self._save()


# ---- command line: python -m onilot user ... ---------------------------------------------------

def read_password(args, prompt='Password: '):
    if args.password_stdin:
        return sys.stdin.readline().rstrip('\n')
    first = getpass.getpass(prompt)
    if getpass.getpass('Repeat: ') != first:
        raise UserError('passwords do not match')
    return first


def cli(argv):
    p = argparse.ArgumentParser(prog='onilot user', description='Manage onilot users.')
    p.add_argument('--data', default=os.environ.get('ONILOT_DATA', 'data'),
                   help='state directory (ONILOT_DATA)')
    sub = p.add_subparsers(dest='command', required=True)
    add = sub.add_parser('add', help='create a user')
    add.add_argument('name')
    add.add_argument('--admin', action='store_true', help='may use the /admin page')
    add.add_argument('--password-stdin', action='store_true')
    pw = sub.add_parser('passwd', help='set a new password')
    pw.add_argument('name')
    pw.add_argument('--password-stdin', action='store_true')
    rm = sub.add_parser('remove', help='delete the account (books and progress are kept)')
    rm.add_argument('name')
    adm = sub.add_parser('admin', help='grant or revoke admin rights')
    adm.add_argument('name')
    adm.add_argument('state', choices=['on', 'off'])
    sub.add_parser('list', help='list users')
    args = p.parse_args(argv)

    Path(args.data).mkdir(parents=True, exist_ok=True)
    store = UserStore(args.data)
    try:
        if args.command == 'add':
            store.add(args.name, read_password(args), admin=args.admin)
            print('created %s%s' % (args.name, ' (admin)' if args.admin else ''))
        elif args.command == 'passwd':
            if store.get(args.name) is None:
                raise UserError('no user %r' % args.name)
            store.set_password(args.name, read_password(args, 'New password: '))
            print('password changed for %s' % args.name)
        elif args.command == 'remove':
            store.remove(args.name)
            print('removed %s (books and reading data were kept)' % args.name)
        elif args.command == 'admin':
            store.set_admin(args.name, args.state == 'on')
            print('%s admin: %s' % (args.name, args.state))
        else:
            for name in store.names():
                print('%s%s' % (name, ' (admin)' if store.is_admin(name) else ''))
    except UserError as exc:
        sys.exit('error: %s' % exc)
