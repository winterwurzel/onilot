"""Admin web interface at https://<host>/admin (browser only; readers never use it).

Session cookie (HttpOnly, SameSite=Strict, Secure on https) for admin users, plus a CSRF token
on every state-changing request (form field "csrf" or header X-CSRF-Token).
"""
import hashlib
import hmac
import html
import time
import urllib.parse
from pathlib import Path

from onilot.server import (ADMIN_SESSION_TTL, Library, parse_multipart_file, route,
                           series_label, series_of, store_upload)
from onilot import __version__
from onilot.metadata import book_key
from onilot.users import UserError

SHARED = 'shared'

CSS = """
:root { --bg:#f5f6f8; --card:#fff; --text:#1d2330; --muted:#6b7280; --line:#e5e7eb;
  --accent:#3b6fd8; --accent-soft:#e8effc; --danger:#c43d3d; --danger-soft:#fbeaea;
  --ok:#2f8f5b; --ok-soft:#e6f4ec; --radius:12px; --shadow:0 1px 2px rgba(0,0,0,.06),
  0 4px 16px rgba(0,0,0,.05); }
@media (prefers-color-scheme: dark) { :root { --bg:#14171d; --card:#1d222b; --text:#e6e8ec;
  --muted:#9aa3af; --line:#2c333e; --accent:#7aa2f7; --accent-soft:#233150;
  --danger:#f07a7a; --danger-soft:#3a2224; --ok:#6fcf97; --ok-soft:#1f3328;
  --shadow:none; } }
* { box-sizing:border-box; }
body { margin:0; background:var(--bg); color:var(--text);
  font:15px/1.5 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif; }
a { color:var(--accent); text-decoration:none; } a:hover { text-decoration:underline; }
header { background:var(--card); border-bottom:1px solid var(--line); position:sticky; top:0;
  z-index:5; }
.bar { max-width:1100px; margin:0 auto; padding:.7rem 1.2rem; display:flex; align-items:center;
  gap:1.5rem; }
.brand { font-weight:700; font-size:1.1rem; display:flex; align-items:center; gap:.5rem; }
.brand svg { width:22px; height:22px; }
nav { display:flex; gap:.3rem; flex:1; }
nav a { padding:.4rem .8rem; border-radius:8px; color:var(--muted); font-weight:500; }
nav a.on, nav a:hover { background:var(--accent-soft); color:var(--accent);
  text-decoration:none; }
main { max-width:1100px; margin:0 auto; padding:1.5rem 1.2rem 4rem; }
h1 { font-size:1.5rem; margin:.2rem 0 1.2rem; } h2 { font-size:1.1rem; margin:0 0 .8rem; }
.card { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
  box-shadow:var(--shadow); padding:1.2rem; margin-bottom:1.2rem; }
.stats { display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:1rem;
  margin-bottom:1.2rem; }
.stat { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
  box-shadow:var(--shadow); padding:1rem 1.2rem; }
.stat b { display:block; font-size:1.8rem; line-height:1.2; }
.stat span { color:var(--muted); font-size:.85rem; }
.muted { color:var(--muted); } .small { font-size:.85rem; }
table { width:100%; border-collapse:collapse; }
th { text-align:left; font-size:.75rem; text-transform:uppercase; letter-spacing:.04em;
  color:var(--muted); font-weight:600; padding:.5rem; border-bottom:1px solid var(--line); }
td { padding:.7rem .5rem; border-bottom:1px solid var(--line); vertical-align:middle; }
tr:last-child td { border-bottom:0; }
.who { display:flex; align-items:center; gap:.7rem; }
.avatar { width:34px; height:34px; border-radius:50%; display:grid; place-items:center;
  font-weight:700; color:#fff; flex:none; text-transform:uppercase; }
.badge { display:inline-block; font-size:.72rem; font-weight:600; padding:.1rem .5rem;
  border-radius:99px; background:var(--accent-soft); color:var(--accent); margin-left:.4rem;
  vertical-align:middle; }
.badge.ok { background:var(--ok-soft); color:var(--ok); }
button, .btn { font:inherit; font-weight:500; border:1px solid var(--line);
  background:var(--card); color:var(--text); padding:.45rem .9rem; border-radius:8px;
  cursor:pointer; display:inline-flex; align-items:center; gap:.4rem; }
button:hover, .btn:hover { border-color:var(--accent); text-decoration:none; }
button.primary { background:var(--accent); border-color:var(--accent); color:#fff; }
button.danger { color:var(--danger); } button.danger:hover { border-color:var(--danger);
  background:var(--danger-soft); }
button.link { border:0; background:none; color:var(--accent); padding:0; }
input[type=text], input[type=password] { font:inherit; padding:.5rem .7rem; border-radius:8px;
  border:1px solid var(--line); background:var(--bg); color:var(--text); width:100%; }
input:focus { outline:2px solid var(--accent-soft); border-color:var(--accent); }
label { display:block; font-size:.85rem; font-weight:500; margin-bottom:.8rem; }
label input { margin-top:.3rem; } label.check { display:flex; gap:.5rem; align-items:center; }
.row { display:flex; gap:.6rem; flex-wrap:wrap; align-items:center; }
.spread { display:flex; justify-content:space-between; align-items:center; gap:1rem;
  flex-wrap:wrap; margin-bottom:1rem; }
.flash { padding:.7rem 1rem; border-radius:10px; margin-bottom:1.2rem; background:var(--ok-soft);
  color:var(--ok); font-weight:500; }
.flash.err { background:var(--danger-soft); color:var(--danger); }
.progress { height:6px; border-radius:99px; background:var(--line); overflow:hidden;
  width:140px; }
.progress i { display:block; height:100%; background:var(--accent); }
.books { display:grid; grid-template-columns:repeat(auto-fill,minmax(150px,1fr)); gap:1rem; }
.book { background:var(--card); border:1px solid var(--line); border-radius:var(--radius);
  overflow:hidden; display:flex; flex-direction:column; box-shadow:var(--shadow); }
.cover { aspect-ratio:2/3; background:var(--accent-soft); display:grid; place-items:center;
  color:var(--accent); font-weight:700; font-size:1.6rem; overflow:hidden; }
.cover img { width:100%; height:100%; object-fit:cover; display:block; }
.book .meta { padding:.6rem .7rem .7rem; flex:1; display:flex; flex-direction:column; gap:.2rem; }
.book .title { font-weight:600; font-size:.9rem; line-height:1.3; }
.book .actions { margin-top:auto; padding-top:.4rem; }
.drop { border:2px dashed var(--line); border-radius:var(--radius); padding:1.6rem;
  text-align:center; color:var(--muted); margin-bottom:1.2rem; transition:.15s; cursor:pointer; }
.drop.over { border-color:var(--accent); background:var(--accent-soft); color:var(--accent); }
.tabs { display:flex; gap:.3rem; flex-wrap:wrap; margin-bottom:1.2rem; }
.tabs a { padding:.35rem .8rem; border-radius:99px; border:1px solid var(--line);
  color:var(--muted); font-size:.9rem; }
.tabs a.on { background:var(--accent); border-color:var(--accent); color:#fff; }
dialog { border:1px solid var(--line); border-radius:var(--radius); background:var(--card);
  color:var(--text); padding:1.4rem; width:min(420px,92vw); box-shadow:0 20px 60px rgba(0,0,0,.25); }
dialog::backdrop { background:rgba(10,14,22,.45); }
.login { max-width:380px; margin:12vh auto; }
.empty { text-align:center; padding:2rem; color:var(--muted); }
select { font:inherit; padding:.45rem .6rem; border-radius:8px; border:1px solid var(--line);
  background:var(--bg); color:var(--text); max-width:100%; }
.members { list-style:none; margin:0 0 .8rem; padding:0; }
.members li { display:flex; justify-content:space-between; align-items:center; gap:.6rem;
  padding:.45rem 0; border-bottom:1px solid var(--line); }
.members li:last-child { border-bottom:0; }
.inline { display:flex; gap:.5rem; align-items:center; flex-wrap:wrap; }
.inline input[type=text] { width:auto; flex:1; min-width:10rem; }
.inline input.num { flex:none; width:4.5em; min-width:0; }
@media (max-width:640px) { .hide-sm { display:none; } nav a { padding:.4rem .5rem; } }
"""

LOGO = ('<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" '
        'stroke-linecap="round" stroke-linejoin="round"><path d="M4 19.5v-15A2.5 2.5 0 0 1 '
        '6.5 2H20v20H6.5a2.5 2.5 0 0 1 0-5H20"/></svg>')

JS = """
const csrf = document.body.dataset.csrf;
function post(url, data) {
  const body = new FormData();
  for (const [k, v] of Object.entries(data || {})) body.append(k, v);
  body.append('csrf', csrf);
  return fetch(url, {method: 'POST', body, headers: {'X-CSRF-Token': csrf}});
}
function confirmDialog(text, action) {
  const d = document.getElementById('confirm');
  d.querySelector('p').textContent = text;
  d.querySelector('.go').onclick = () => { d.close(); action(); };
  d.showModal();
}
document.querySelectorAll('[data-dialog]').forEach(b =>
  b.addEventListener('click', () => document.getElementById(b.dataset.dialog).showModal()));
document.querySelectorAll('dialog .cancel').forEach(b =>
  b.addEventListener('click', () => b.closest('dialog').close()));
document.querySelectorAll('[data-confirm]').forEach(f => f.addEventListener('submit', e => {
  e.preventDefault(); confirmDialog(f.dataset.confirm, () => f.submit()); }));
"""

UPLOAD_JS = """
const drop = document.getElementById('drop'), input = document.getElementById('files');
const status = document.getElementById('upload-status');
drop.addEventListener('click', () => input.click());
['dragenter', 'dragover'].forEach(e => drop.addEventListener(e, ev => {
  ev.preventDefault(); drop.classList.add('over'); }));
['dragleave', 'drop'].forEach(e => drop.addEventListener(e, ev => {
  ev.preventDefault(); drop.classList.remove('over'); }));
drop.addEventListener('drop', ev => upload(ev.dataTransfer.files));
input.addEventListener('change', () => upload(input.files));
async function upload(files) {
  let ok = 0, failed = [];
  for (const [i, f] of [...files].entries()) {
    status.textContent = 'Uploading ' + (i + 1) + ' of ' + files.length + ': ' + f.name;
    const body = new FormData(); body.append('file', f, f.name); body.append('csrf', csrf);
    const r = await fetch(drop.dataset.url, {method: 'POST', body,
                                             headers: {'X-CSRF-Token': csrf}});
    if (r.ok) ok++; else failed.push(f.name + ' (' + ((await r.json()).error || r.status) + ')');
  }
  location.href = location.pathname + location.search.replace(/&?msg=[^&]*|&?err=1/g, '') +
    (location.search ? '&' : '?') + (failed.length ? 'err=1&' : '') + 'msg=' +
    encodeURIComponent(ok + ' uploaded' + (failed.length ? '; failed: ' + failed.join(', ') : ''));
}
"""


# ---- helpers ---------------------------------------------------------------------------------

def esc(text):
    return html.escape(str(text if text is not None else ''))


def avatar(name):
    hue = int(hashlib.sha1(name.encode()).hexdigest()[:4], 16) % 360
    return '<span class="avatar" style="background:hsl(%d 55%% 48%%)">%s</span>' % (
        hue, esc(name[:1]))


def ago(ts):
    if not ts:
        return 'never'
    delta = int(time.time() - ts)
    for unit, size in (('d', 86400), ('h', 3600), ('min', 60)):
        if delta >= size:
            return '%d %s ago' % (delta // size, unit)
    return 'just now'


def cookie(h, sid, max_age):
    flags = 'Path=/admin; HttpOnly; SameSite=Strict; Max-Age=%d' % max_age
    if h.server.cfg.secure_cookies:
        flags += '; Secure'
    return {'Set-Cookie': 'onilot_admin=%s; %s' % (sid, flags)}


def go(h, path, message=None, error=False):
    query = ''
    if message:
        query = ('&' if '?' in path else '?') + ('err=1&' if error else '') + 'msg=' + \
            urllib.parse.quote(message)
    h._send(303, b'', 'text/plain', {'Location': h._admin_url(path + query)})


def flash(h):
    text = h.query.get('msg', [''])[0]
    if not text:
        return ''
    return '<div class="flash%s">%s</div>' % (' err' if h.query.get('err') else '', esc(text))


def page(h, title, body, active='', status=200, script='', extra_headers=None):
    signed_in = getattr(h, 'admin', None)
    nav = ''
    if signed_in:
        links = [('', 'Overview'), ('/library', 'Library'), ('/series', 'Series')]
        nav = '<nav>%s</nav>' % ''.join(
            '<a href="%s"%s>%s</a>' % (esc(h._admin_url(p)), ' class="on"' if active == p else '',
                                       label) for p, label in links)
        nav += ('<form method=post action="%s"><input type=hidden name=csrf value="%s">'
                '<button class="link">Sign out %s</button></form>' % (
                    esc(h._admin_url('/logout')), esc(h.csrf), esc(signed_in)))
    brand = esc(h.server.cfg.brand)
    doc = """<!doctype html><html><head><meta charset="utf-8">
<meta name=viewport content="width=device-width,initial-scale=1"><title>%s · %s</title>
<style>%s</style></head><body data-csrf="%s">
<header><div class="bar"><span class="brand">%s %s</span>%s</div></header>
<main>%s</main>
<dialog id="confirm"><p></p><div class="row" style="justify-content:flex-end">
<button class="cancel">Cancel</button><button class="danger go">Confirm</button></div></dialog>
<script>%s%s</script></body></html>""" % (
        esc(title), brand, CSS, esc(getattr(h, 'csrf', '')), LOGO, brand, nav,
        flash(h) + body, JS if signed_in else '', script)
    h._html(doc, status, extra_headers)


def csrf_ok(h, form):
    token = form.get('csrf') or h.headers.get('X-CSRF-Token', '')
    return hmac.compare_digest(token, h.csrf)


def owners(h):
    return [SHARED] + h.server.users.names()


def owner_library(h, owner):
    if owner == SHARED:
        return h.server.shared_library(), h.server.shared_dir
    directory = Path(h.server.cfg.library) / owner
    directory.mkdir(parents=True, exist_ok=True)
    return Library([(directory, '', False)]), directory


def user_summary(h, name):
    ctx = h.server.context(name)
    own = [b for b in ctx.library.books() if not b['shared']]
    last = max([d.get('last_seen', 0) for d in ctx.devices.values()] or [0])
    return ctx, own, last


def progress_by_uuid(ctx):
    result = {}
    for key, doc in ctx.state.docs.items():
        parts = key.split('/')
        if len(parts) == 4 and parts[1] == 'publications' and parts[3] == 'bookmark' \
                and not doc.get('deleted'):
            value = doc.get('value', {})
            result[parts[2]] = (float(value.get('progress') or 0), value.get('modified', 0) / 1000)
    return result


# ---- login / logout ----------------------------------------------------------------------------

LOGIN = """<div class="login card"><h1>Sign in</h1>{message}
<form method=post action="{action}">
<label>User<input type=text name=username autocapitalize=none autocomplete=username required
autofocus></label>
<label>Password<input type=password name=password autocomplete=current-password required></label>
<button class="primary" style="width:100%;justify-content:center">Sign in</button></form>
<p class="muted small">Admins only. Readers sign in on the device.</p></div>"""


@route('GET', r'/admin')
def admin_index(h):
    if urllib.parse.urlsplit(h.path).path != '/admin':  # reached as <prefix>/admin
        h._send(301, b'', 'text/plain', {'Location': h._admin_url()})
        return
    if not h._admin_session():
        page(h, 'Sign in', LOGIN.format(message='', action=esc(h._admin_url('/login'))))
        return
    overview(h)


@route('POST', r'/admin/login')
def admin_login(h):
    form = h._form()
    user = h.server.users.authenticate(form.get('username'), form.get('password'))
    if user is None or not h.server.users.is_admin(user):
        time.sleep(1)
        page(h, 'Sign in', LOGIN.format(
            message='<div class="flash err">Wrong user or password, or not an admin.</div>',
            action=esc(h._admin_url('/login'))), status=403)
        return
    sid = h.server.new_admin_session(user)
    h._send(303, b'', 'text/plain', {'Location': h._admin_url(),
                                     **cookie(h, sid, ADMIN_SESSION_TTL)})


@route('POST', r'/admin/logout', 'admin')
def admin_logout(h):
    if csrf_ok(h, h._form()):
        h.server.admin_sessions = {k: v for k, v in h.server.admin_sessions.items()
                                   if v[0] != h.admin}
    h._send(303, b'', 'text/plain', {'Location': h._admin_url(), **cookie(h, '', 0)})


# ---- overview ----------------------------------------------------------------------------------

ADD_USER = """<dialog id="add-user"><h2>Add user</h2>
<form method=post action="{action}"><input type=hidden name=csrf value="{csrf}">
<label>Name<input type=text name=name pattern="[a-z0-9][a-z0-9._-]{{0,31}}" required
placeholder="lowercase, e.g. anna"></label>
<label>Password<input type=password name=password minlength=8 required
autocomplete=new-password></label>
<label class="check"><input type=checkbox name=admin value=1> Administrator</label>
<div class="row" style="justify-content:flex-end"><button type=button class="cancel">Cancel
</button><button class="primary">Create</button></div></form></dialog>"""


def overview(h):
    rows, total_own, readers, last_sync = [], 0, 0, 0
    for name in h.server.users.names():
        ctx, own, last = user_summary(h, name)
        total_own += len(own)
        readers += len(ctx.devices)
        last_sync = max(last_sync, last)
        rows.append(
            '<tr><td><a class="who" href="%s">%s<span><b>%s</b>%s</span></a></td>'
            '<td>%d</td><td class="hide-sm">%d</td><td class="muted">%s</td></tr>' % (
                esc(h._admin_url('/users/' + name)), avatar(name), esc(name),
                '<span class="badge">admin</span>' if h.server.users.is_admin(name) else '',
                len(own), len(ctx.devices), ago(last)))
    shared = len(h.server.shared_library().books())
    stats = ''.join('<div class="stat"><b>%s</b><span>%s</span></div>' % kv for kv in (
        (len(rows), 'users'), (total_own + shared, 'books (%d shared)' % shared),
        (readers, 'readers'), (ago(last_sync), 'last reader activity')))
    body = ('<h1>Overview</h1><div class="stats">%s</div>'
            '<div class="card"><div class="spread"><h2 style="margin:0">Users</h2>'
            '<button class="primary" data-dialog="add-user">+ Add user</button></div>'
            '<table><tr><th>User</th><th>Own books</th><th class="hide-sm">Readers</th>'
            '<th>Last activity</th></tr>%s</table></div>'
            '<p class="muted small">Reader endpoint: <code>%s</code> · onilot %s</p>' % (
                stats, ''.join(rows), esc(h.server.cfg.public_base), esc(__version__)))
    body += ADD_USER.format(action=esc(h._admin_url('/users')), csrf=esc(h.csrf))
    page(h, 'Overview', body, active='')


# ---- users ---------------------------------------------------------------------------------------

@route('POST', r'/admin/users', 'admin')
def admin_add_user(h):
    form = h._form()
    if not csrf_ok(h, form):
        go(h, '', 'Form expired, please try again.', error=True)
        return
    name = (form.get('name') or '').strip().lower()
    try:
        h.server.users.add(name, form.get('password', ''), admin=form.get('admin') == '1')
        go(h, '/users/' + name, 'Created %s. They can sign in on their reader now.' % name)
    except UserError as exc:
        go(h, '', str(exc), error=True)


USER_ACTIONS = """<div class="card"><h2>Account</h2><div class="row">
<button data-dialog="pw">Set password</button>
<form method=post action="{url}"><input type=hidden name=csrf value="{csrf}">
<input type=hidden name=action value=admin><input type=hidden name=state value="{admin_to}">
<button>{admin_label}</button></form>
<form method=post action="{url}" data-confirm="Sign out all readers of {name}? They have to sign in again.">
<input type=hidden name=csrf value="{csrf}"><input type=hidden name=action value=signout>
<button>Sign out readers</button></form>
<form method=post action="{url}" title="Brings back positions, finished marks and collections that
were removed by deleting books on a reader, for the books now in the library.">
<input type=hidden name=csrf value="{csrf}"><input type=hidden name=action value=restore>
<button>Restore reading data</button></form>
{delete}</div></div>
<dialog id="pw"><h2>New password for {name}</h2>
<form method=post action="{url}"><input type=hidden name=csrf value="{csrf}">
<input type=hidden name=action value=passwd>
<label>Password<input type=password name=password minlength=8 required
autocomplete=new-password></label>
<p class="muted small">Their readers are signed out and must sign in with the new password.</p>
<div class="row" style="justify-content:flex-end"><button type=button class="cancel">Cancel
</button><button class="primary">Save</button></div></form></dialog>"""


@route('GET', r'/admin/users/([a-z0-9._-]+)', 'admin')
def admin_user(h, name):
    if h.server.users.get(name) is None:
        go(h, '', 'No user %s.' % name, error=True)
        return
    ctx, own, last = user_summary(h, name)
    is_admin = h.server.users.is_admin(name)
    progress = progress_by_uuid(ctx)
    finished = ctx.state.finished()
    books = sorted(ctx.library.books(), key=lambda b: -progress.get(b['uuid'], (0, 0))[1])
    rows = []
    for b in books:
        pct, when = progress.get(b['uuid'], (0, 0))
        if b['uuid'] in finished:
            pct = 1.0
        rows.append('<tr><td><b>%s</b><div class="muted small">%s</div></td><td>%s</td>'
                    '<td><div class="row"><div class="progress"><i style="width:%d%%"></i></div>'
                    '<span class="small">%s</span></div></td><td class="muted hide-sm">%s</td>'
                    '</tr>' % (esc(b['title']), esc(', '.join(
                        b['authors'] + ([series_label(b)] if b['series'] else []))),
                               '<span class="badge ok">shared</span>' if b['shared'] else
                               ('<span class="badge">uploaded</span>' if ctx.is_upload(b) else ''),
                               round(pct * 100),
                               'finished' if b['uuid'] in finished else '%d%%' % round(pct * 100),
                               ago(when) if when else '–'))
    devices = ''.join(
        '<tr><td><b>%s</b> <span class="muted">%s</span><div class="muted small">%s</div></td>'
        '<td class="muted">%s</td></tr>' % (esc(d.get('client_type') or 'Reader'),
                                            esc(d.get('client_version', '')), esc(hw),
                                            ago(d.get('last_seen')))
        for hw, d in sorted(ctx.devices.items(), key=lambda kv: -kv[1].get('last_seen', 0)))
    url = esc(h._admin_url('/users/' + name))
    delete = '' if name == h.admin else (
        '<form method=post action="%s" data-confirm="Delete account %s? Books and reading data '
        'are kept and come back if the account is re-created."><input type=hidden name=csrf '
        'value="%s"><input type=hidden name=action value=delete><button class="danger">Delete '
        'account</button></form>' % (url, esc(name), esc(h.csrf)))
    body = ('<div class="spread"><div class="who">%s<h1 style="margin:0">%s%s</h1></div>'
            '<div class="row"><a class="btn" href="%s">Collections</a>'
            '<a class="btn" href="%s">Manage books</a></div></div>'
            '<div class="stats"><div class="stat"><b>%d</b><span>own books</span></div>'
            '<div class="stat"><b>%d</b><span>readers</span></div>'
            '<div class="stat"><b>%s</b><span>last activity</span></div></div>'
            '<div class="card"><h2>Reading</h2>%s</div>'
            '<div class="card"><h2>Readers</h2>%s</div>' % (
                avatar(name), esc(name), ' <span class="badge">admin</span>' if is_admin else '',
                esc(h._admin_url('/users/%s/collections' % name)),
                esc(h._admin_url('/library?owner=' + name)), len(own), len(ctx.devices), ago(last),
                '<table><tr><th>Book</th><th></th><th>Progress</th><th class="hide-sm">Read</th>'
                '</tr>%s</table>' % ''.join(rows) if rows else
                '<div class="empty">No books yet.</div>',
                '<table>%s</table>' % devices if devices else
                '<div class="empty">No reader has signed in yet.</div>'))
    body += USER_ACTIONS.format(url=url, csrf=esc(h.csrf), name=esc(name),
                                admin_to='off' if is_admin else 'on',
                                admin_label='Revoke admin' if is_admin else 'Make admin',
                                delete=delete)
    page(h, name, body)


@route('POST', r'/admin/users/([a-z0-9._-]+)', 'admin')
def admin_user_action(h, name):
    form = h._form()
    path = '/users/' + name
    if not csrf_ok(h, form):
        go(h, path, 'Form expired, please try again.', error=True)
        return
    users, action = h.server.users, form.get('action')
    try:
        if users.get(name) is None:
            raise UserError('no user %s' % name)
        if action == 'passwd':
            users.set_password(name, form.get('password', ''))
            h.server.sign_out(name)
            go(h, path, 'New password set; %s\'s readers must sign in again.' % name)
        elif action == 'admin':
            users.set_admin(name, form.get('state') == 'on')
            go(h, path, 'Updated %s.' % name)
        elif action == 'restore':
            ctx = h.server.context(name)
            count = ctx.state.restore([b['uuid'] for b in ctx.library.books()])
            go(h, path, 'Restored %d reading data entries; readers get them at their next '
                        'sync.' % count)
        elif action == 'signout':
            h.server.sign_out(name)
            go(h, path, 'Signed out all readers of %s.' % name)
        elif action == 'delete':
            if name == h.admin:
                raise UserError('you cannot delete yourself')
            users.remove(name)
            h.server.sign_out(name)
            go(h, '', 'Deleted %s (books and reading data kept).' % name)
        else:
            raise UserError('unknown action')
    except UserError as exc:
        go(h, path if users.get(name) else '', str(exc), error=True)


# ---- library -------------------------------------------------------------------------------------

@route('GET', r'/admin/library', 'admin')
def admin_library(h):
    owner = h.query.get('owner', [SHARED])[0]
    if owner not in owners(h):
        go(h, '/library', 'Unknown library %s.' % owner, error=True)
        return
    library, _ = owner_library(h, owner)
    tabs = ''.join('<a href="%s"%s>%s</a>' % (
        esc(h._admin_url('/library?owner=' + o)), ' class="on"' if o == owner else '',
        'Shared' if o == SHARED else esc(o)) for o in owners(h))
    cards = []
    for b in sorted(library.books(), key=lambda b: b['title'].lower()):
        cover_url = esc(h._admin_url('/cover/%s/%s' % (owner, b['uuid'])))
        initial = esc(b['title'][:1])
        cards.append(
            '<div class="book"><div class="cover">%s</div><div class="meta">'
            '<span class="title">%s</span><span class="muted small">%s</span>%s'
            '<span class="muted small">%.1f MB</span><div class="actions">'
            '<form method=post action="%s" data-confirm="Delete %s from the server? Readers '
            'remove it at their next sync."><input type=hidden name=csrf value="%s">'
            '<input type=hidden name=action value=delete><button class="danger small">Delete'
            '</button></form></div></div></div>' % (
                '<img src="%s" alt="%s" loading="lazy" '
                'onerror="this.parentNode.textContent=this.alt">' % (cover_url, initial),
                esc(b['title']), esc(', '.join(b['authors'])),
                '<span class="muted small"><i>%s</i></span>' % esc(series_label(b))
                if b['series'] else '', b['size'] / 1e6,
                esc(h._admin_url('/library/%s/%s' % (owner, b['uuid']))), esc(b['title']),
                esc(h.csrf)))
    who = 'everyone' if owner == SHARED else owner
    body = ('<h1>Library</h1><div class="tabs">%s</div>'
            '<div class="drop" id="drop" data-url="%s"><b>Drop EPUB files here</b> or click to '
            'choose<div class="small">They go to the %s library, visible to %s.</div>'
            '<div class="small" id="upload-status"></div></div>'
            '<input type="file" id="files" accept=".epub,application/epub+zip" multiple hidden>'
            '%s' % (tabs, esc(h._admin_url('/library/upload?owner=' + owner)),
                    'shared' if owner == SHARED else esc(owner) + "'s", esc(who),
                    '<div class="books">%s</div>' % ''.join(cards) if cards else
                    '<div class="card empty">No books in this library yet.</div>'))
    page(h, 'Library', body, active='/library', script=UPLOAD_JS)


@route('POST', r'/admin/library/upload', 'admin')
def admin_upload(h):
    owner = h.query.get('owner', [''])[0]
    if not csrf_ok(h, {}):
        h._send_json({'error': 'form expired, reload the page'}, 403)
        return
    if owner not in owners(h):
        h._send_json({'error': 'unknown library'}, 400)
        return
    filename, data = parse_multipart_file(h.headers.get('Content-Type', ''), h.body)
    if not data or not data.startswith(b'PK'):
        h._send_json({'error': 'not an EPUB'}, 400)
        return
    _, directory = owner_library(h, owner)
    path = store_upload(directory, filename, data)
    h._send_json({'stored': path.name})


@route('POST', r'/admin/library/([a-z0-9._-]+)/([0-9a-f]{40})', 'admin')
def admin_book_action(h, owner, uuid):
    form = h._form()
    back = '/library?owner=' + owner
    if not csrf_ok(h, form):
        go(h, back, 'Form expired, please try again.', error=True)
        return
    if owner not in owners(h) or form.get('action') != 'delete':
        go(h, back, 'Unknown request.', error=True)
        return
    library, _ = owner_library(h, owner)
    book = library.find(uuid)
    if book is None:
        go(h, back, 'Book not found.', error=True)
        return
    book['path'].unlink()
    h.server.record_deletion(owner, uuid)
    go(h, back, 'Deleted %s.' % book['title'])


@route('GET', r'/admin/cover/([a-z0-9._-]+)/([0-9a-f]{40})', 'admin')
def admin_cover(h, owner, uuid):
    if owner not in owners(h):
        h._send(404, b'', 'text/plain')
        return
    library, _ = owner_library(h, owner)
    book = library.find(uuid)
    data, mime = library.cover_bytes(book) if book else (None, None)
    if data is None:
        h._send(404, b'', 'text/plain')
        return
    h._send(200, data, mime, {'Cache-Control': 'private, max-age=3600'})



# ---- collections (per user; they reach the user's readers at the next sync) -------------------

NEW_COLLECTION = """<div class="card"><h2>New collection</h2>
<form method=post action="{url}" class="inline"><input type=hidden name=csrf value="{csrf}">
<input type=hidden name=action value=create>
<input type=text name=name maxlength=100 required placeholder="Name">
<select name=uuid required><option value="">First book…</option>{options}</select>
<button class="primary">Create</button></form>
<p class="muted small">A collection needs at least one book. Collections made on the reader
show up here after its next sync; books with series information are collected automatically.
</p></div>"""


def book_options(books, exclude=()):
    return ''.join('<option value="%s">%s</option>' % (
        esc(b['uuid']), esc(b['title'] + (' (%s)' % series_label(b) if b['series'] else '')))
        for b in books if b['uuid'] not in exclude)


def collection_card(h, url, name, members, books, auto):
    by_uuid = {b['uuid']: b for b in books}
    csrf = '<input type=hidden name=csrf value="%s">' % esc(h.csrf)
    hidden = csrf + '<input type=hidden name=name value="%s">' % esc(name)

    def order(member):
        book = by_uuid.get(member[1])
        if book is None:
            return (1, 0, '')
        return (0, book['series_index'] or 0, book['title'].lower())
    items = []
    for key, uuid in sorted(members, key=order):
        book = by_uuid.get(uuid)
        if book is None:
            title = '<span class="muted">Book no longer in the library</span>'
        elif book['series']:
            title = '%s <span class="muted small">%s</span>' % (esc(book['title']),
                                                                esc(series_label(book)))
        else:
            title = esc(book['title'])
        items.append('<li><span>%s</span><form method=post action="%s">%s'
                     '<input type=hidden name=action value=remove>'
                     '<input type=hidden name=key value="%s"><button class="danger small">'
                     'Remove</button></form></li>' % (title, url, csrf, esc(key)))
    present = {uuid for _, uuid in members}
    options = book_options(books, present)
    add = ('<form method=post action="%s" class="inline">%s<input type=hidden name=action '
           'value=add><select name=uuid required><option value="">Add a book…</option>%s'
           '</select><button>Add</button></form>' % (url, hidden, options)) if options else ''
    return ('<div class="card"><div class="spread"><h2 style="margin:0">%s%s '
            '<span class="muted small">%d</span></h2><div class="row">'
            '<form method=post action="%s" class="inline">%s<input type=hidden name=action '
            'value=rename><input type=text name=new maxlength=100 required value="%s" '
            'aria-label="New name"><button>Rename</button></form>'
            '<form method=post action="%s" data-confirm="Delete collection %s? The books stay '
            'in the library.">%s<input type=hidden name=action value=delete>'
            '<button class="danger">Delete</button></form></div></div>'
            '<ul class="members">%s</ul>%s</div>' % (
                esc(name), ' <span class="badge">series</span>' if auto else '', len(members),
                url, hidden, esc(name), url, esc(name), hidden, ''.join(items), add))


@route('GET', r'/admin/users/([a-z0-9._-]+)/collections', 'admin')
def admin_collections(h, name):
    if h.server.users.get(name) is None:
        go(h, '', 'No user %s.' % name, error=True)
        return
    ctx = h.server.context(name)
    books = sorted(ctx.library.books(), key=lambda b: (b['series'] or b['title'].lower(),
                                                        b['series_index'] or 0))
    ctx.state.update_series_collections(series_of(ctx.library))  # show them before a sync
    collections = ctx.state.collections()
    auto = ctx.state.auto_collections()
    url = esc(h._admin_url('/users/%s/collections' % name))
    cards = ''.join(collection_card(h, url, c, collections[c], books, c in auto)
                    for c in sorted(collections, key=str.lower))
    body = ('<div class="spread"><div class="who">%s<h1 style="margin:0">Collections of %s</h1>'
            '</div><a class="btn" href="%s">Back to %s</a></div>%s%s' % (
                avatar(name), esc(name), esc(h._admin_url('/users/' + name)), esc(name),
                NEW_COLLECTION.format(url=url, csrf=esc(h.csrf), options=book_options(books)),
                cards or '<div class="card empty">No collections yet.</div>'))
    page(h, 'Collections', body)


@route('POST', r'/admin/users/([a-z0-9._-]+)/collections', 'admin')
def admin_collection_action(h, name):
    form = h._form()
    path = '/users/%s/collections' % name
    if not csrf_ok(h, form):
        go(h, path, 'Form expired, please try again.', error=True)
        return
    if h.server.users.get(name) is None:
        go(h, '', 'No user %s.' % name, error=True)
        return
    handler = COLLECTION_ACTIONS.get(form.get('action'))
    if handler is None:
        go(h, path, 'Unknown request.', error=True)
        return
    try:
        message = handler(h.server.context(name), form)
    except UserError as exc:
        go(h, path, str(exc), error=True)
        return
    go(h, path, message)


def collection_name(form, field='name'):
    value = (form.get(field) or '').strip()
    if not 0 < len(value) <= 100:
        raise UserError('Collection names need 1 to 100 characters.')
    return value


def collection_add(ctx, form):
    collection = collection_name(form)
    book = ctx.library.find(form.get('uuid', ''))
    if book is None:
        raise UserError('Pick a book from %s\'s library.' % ctx.name)
    ctx.state.add_to_collection(collection, book['uuid'])
    return 'Added "%s" to %s.' % (book['title'], collection)


def collection_rename(ctx, form):
    old, new = collection_name(form), collection_name(form, 'new')
    if new != old and new in ctx.state.collections():
        raise UserError('There already is a collection called %s.' % new)
    ctx.state.rename_collection(old, new)
    return 'Renamed %s to %s.' % (old, new)


def collection_delete(ctx, form):
    collection = collection_name(form)
    for key, _ in ctx.state.collections().get(collection, []):
        ctx.state.remove_tag(key)
    return 'Deleted collection %s.' % collection


def collection_remove(ctx, form):
    key = form.get('key', '')
    if not any(key == k for members in ctx.state.collections().values() for k, _ in members):
        raise UserError('That book is not in a collection.')
    ctx.state.remove_tag(key)
    return 'Removed the book from the collection.'


COLLECTION_ACTIONS = {'create': collection_add, 'add': collection_add,
                      'rename': collection_rename, 'delete': collection_delete,
                      'remove': collection_remove}


# ---- series (EPUB metadata, Wikidata lookups, manual edits) ------------------------------------

SOURCE_BADGES = {'file': ('', 'from file'), 'auto': ('ok', 'Wikidata'),
                 'accepted': ('ok', 'accepted'), 'manual': ('', 'manual')}
SERIES_TABS = (('review', 'To review'), ('missing', 'No series'), ('all', 'All books'))


def unique_books(h):
    books = {}
    for b in h.server.all_books():
        books.setdefault(book_key(b['title'], b['authors']), b)
    return sorted(books.items(), key=lambda kv: (kv[1]['series'] or '~', kv[1]['series_index']
                                                 or 0, kv[1]['title'].lower()))


def lookup_status(h):
    worker = h.server.series_lookup
    if worker is None:
        return 'Wikidata lookup is off (ONILOT_SERIES_LOOKUP=off).'
    status = worker.status
    text = ('Looking up series on Wikidata: %d books left.' % status['pending']
            if status['running'] else 'Wikidata lookup idle; new books are checked every 10 min.')
    if status['error']:
        text += ' Last error: %s' % status['error']
    return text


def series_row(h, key, book, entry):
    hidden = ('<input type=hidden name=csrf value="%s"><input type=hidden name=key value="%s">'
              % (esc(h.csrf), esc(key)))
    url = esc(h._admin_url('/series'))
    badge = SOURCE_BADGES.get(book.get('series_source'))
    current = ('%s <span class="badge %s">%s</span>' % (esc(series_label(book)), badge[0],
                                                        badge[1]) if book['series'] else
               '<span class="muted">none</span>')
    suggestion = ''
    if entry and entry.get('status') == 'suggested':
        suggestion = (
            '<div class="small">Wikidata suggests <b>%s</b> (matched <a href="https://www.'
            'wikidata.org/wiki/%s" target="_blank" rel="noopener">%s</a>)</div>'
            '<div class="row"><form method=post action="%s">%s<input type=hidden name=action '
            'value=accept><button class="primary small">Accept</button></form>'
            '<form method=post action="%s">%s<input type=hidden name=action value=reject>'
            '<button class="small">Reject</button></form></div>' % (
                esc(series_label({'series': entry.get('series'), 'series_index':
                                  entry.get('index')})), esc(entry.get('qid', '')),
                esc(entry.get('match') or entry.get('qid')), url, hidden, url, hidden))
    clear = ('<form method=post action="%s">%s<input type=hidden name=action value=clear>'
             '<button class="link small">undo edit</button></form>' % (url, hidden)
             if entry and entry.get('status') in ('manual', 'accepted', 'rejected') else '')
    edit = ('<form method=post action="%s" class="inline">%s<input type=hidden name=action '
            'value=set><input type=text name=series maxlength=100 placeholder="Series" '
            'value="%s" aria-label="Series"><input type=text name=index maxlength=8 '
            'placeholder="#" value="%s" class="num" aria-label="Number">'
            '<button class="small">Save</button></form>' % (
                url, hidden, esc(book['series'] or ''),
                esc('%g' % book['series_index'] if book['series_index'] is not None else '')))
    return ('<tr><td><b>%s</b><div class="muted small">%s</div>%s</td><td>%s%s</td>'
            '<td>%s</td></tr>' % (esc(book['title']), esc(', '.join(book['authors'])),
                                  suggestion, current, clear, edit))


@route('GET', r'/admin/series', 'admin')
def admin_series(h):
    tab = h.query.get('tab', ['review'])[0]
    rows, counts = [], {'review': 0, 'missing': 0, 'all': 0}
    for key, book in unique_books(h):
        entry = h.server.series.get(key)
        kinds = {'all'}
        if entry and entry.get('status') == 'suggested' and not book['series']:
            kinds.add('review')
        if not book['series']:
            kinds.add('missing')
        for kind in kinds:
            counts[kind] = counts.get(kind, 0) + 1
        if tab in kinds:
            rows.append(series_row(h, key, book, entry))
    tabs = ''.join('<a href="%s"%s>%s (%d)</a>' % (
        esc(h._admin_url('/series?tab=' + t)), ' class="on"' if t == tab else '', label,
        counts.get(t, 0)) for t, label in SERIES_TABS)
    body = ('<div class="spread"><h1 style="margin:0">Series</h1>'
            '<form method=post action="%s"><input type=hidden name=csrf value="%s">'
            '<input type=hidden name=action value=lookup><button>Look up again</button></form>'
            '</div><p class="muted small">%s Series from the EPUB files win over Wikidata; your '
            'edits win over both. Each series is a collection on the reader.</p>'
            '<div class="tabs">%s</div><div class="card">%s</div>' % (
                esc(h._admin_url('/series')), esc(h.csrf), esc(lookup_status(h)), tabs,
                '<table><tr><th>Book</th><th>Series</th><th>Edit</th></tr>%s</table>'
                % ''.join(rows) if rows else '<div class="empty">Nothing here.</div>'))
    page(h, 'Series', body, active='/series')


@route('POST', r'/admin/series', 'admin')
def admin_series_action(h):
    form = h._form()
    back = h.headers.get('Referer', '')
    path = '/series' + (back[back.index('?'):] if '?tab=' in back else '')
    if not csrf_ok(h, form):
        go(h, path, 'Form expired, please try again.', error=True)
        return
    store, key, action = h.server.series, form.get('key', ''), form.get('action')
    entry = store.get(key)
    if action == 'lookup':
        store.forget(('none', 'error'))
        if h.server.series_lookup is not None:
            h.server.series_lookup.wake.set()
        go(h, path, 'Looking up books without series again.')
    elif action == 'accept' and entry and entry.get('status') == 'suggested':
        store.update(key, status='accepted')
        go(h, path, 'Accepted %s.' % entry.get('series'))
    elif action == 'reject' and entry and entry.get('status') == 'suggested':
        store.update(key, status='rejected')
        go(h, path, 'Rejected the suggestion.')
    elif action == 'set' and key in dict(unique_books(h)):
        series_edit(h, store, key, form, path)
    elif action == 'clear' and entry:
        store.remove(key)
        go(h, path, 'Edit undone.')
    else:
        go(h, path, 'Unknown request.', error=True)


def series_edit(h, store, key, form, path):
    series = (form.get('series') or '').strip()[:100] or None
    index = (form.get('index') or '').strip().replace(',', '.')
    try:
        index = float(index) if index and series else None
    except ValueError:
        go(h, path, 'The number must be a number, like 2 or 2.5.', error=True)
        return
    store.put(key, status='manual', series=series, index=index)
    go(h, path, 'Saved: %s.' % (series_label({'series': series, 'series_index': index})
                                or 'no series'))
