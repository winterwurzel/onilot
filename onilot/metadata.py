"""Series lookup on Wikidata for books whose EPUB has no series metadata.

Results are kept in DATA/series.json, keyed by normalized title and authors (so the same book in
several libraries shares one entry). A match counts as confident when the title and an author
agree; those apply automatically, weaker ones wait in the admin page for review. EPUB files are
never modified.
"""
import json
import re
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

from onilot import __version__

API = 'https://www.wikidata.org/w/api.php'
USER_AGENT = 'onilot/%s (https://github.com/winterwurzel/onilot)' % __version__
# Statuses whose series is shown on readers.
APPLIED = ('auto', 'accepted', 'manual')
REQUEST_PAUSE = 0.5   # seconds between Wikidata requests
RETRY_ERRORS_AFTER = 3600
# "instance of" values that make an item without authors still count as a book.
BOOK_TYPES = {'Q7725634', 'Q47461344', 'Q8261', 'Q571', 'Q3331189', 'Q49084', 'Q149537',
              'Q8274', 'Q725377', 'Q1004', 'Q277759'}


def normalize(text):
    text = unicodedata.normalize('NFKD', text or '')
    text = ''.join(c for c in text if not unicodedata.combining(c)).lower()
    return ' '.join(re.sub(r'[^\w]+', ' ', text).split())


def book_key(title, authors):
    return '%s|%s' % (normalize(title), ','.join(sorted(normalize(a) for a in authors)))


def title_variants(title):
    """Full title plus the parts around a colon or dash ("Mistborn: The Final Empire")."""
    variants = {normalize(title)}
    for sep in (':', ' - ', ' – ', '('):
        if sep in title:
            head, _, tail = title.partition(sep)
            variants.update({normalize(head), normalize(tail.rstrip(')'))})
    return {v for v in variants if len(v) >= 3}


def name_tokens(names):
    tokens = set()
    for name in names:
        tokens.update(t for t in normalize(name.replace(',', ' ')).split() if len(t) >= 3)
    return tokens


def parse_index(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def language_of(code):
    code = (code or '').lower()
    return {'ger': 'de', 'deu': 'de', 'eng': 'en', 'fre': 'fr', 'fra': 'fr'}.get(
        code[:3], code[:2]) or 'en'


class Wikidata:
    """The few Wikidata API calls the matcher needs; fetch is replaceable in tests."""

    def __init__(self, fetch=None):
        self.fetch = fetch or self._fetch
        self.last_request = 0

    def _fetch(self, params):
        # maxlag asks to back off while replication lags; 5 s (the bot default) is exceeded for
        # long stretches by query-service lag alone, which plain reads do not add to.
        params = dict(params, format='json', maxlag='30')
        url = API + '?' + urllib.parse.urlencode(params)
        for attempt in range(4):
            wait = self.last_request + REQUEST_PAUSE - time.time()
            if wait > 0:
                time.sleep(wait)
            self.last_request = time.time()
            request = urllib.request.Request(url, headers={'User-Agent': USER_AGENT})
            try:
                with urllib.request.urlopen(request, timeout=30) as resp:
                    data = json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                if exc.code not in (429, 503) or attempt == 3:
                    raise
                time.sleep(int(exc.headers.get('Retry-After') or 5 * (attempt + 1)))
                continue
            if data.get('error', {}).get('code') == 'maxlag' and attempt < 3:
                time.sleep(5 * (attempt + 1))
                continue
            if 'error' in data:
                raise RuntimeError('Wikidata: %s' % data['error'].get('info'))
            return data
        raise RuntimeError('Wikidata did not answer')

    def search(self, text, language):
        data = self.fetch({'action': 'wbsearchentities', 'search': text, 'language': language,
                           'uselang': language, 'type': 'item', 'limit': '7'})
        return [r['id'] for r in data.get('search', [])]

    def fulltext(self, text):
        """Items with a series whose text contains the words (finds "Mistborn: The …")."""
        data = self.fetch({'action': 'query', 'list': 'search', 'srnamespace': '0',
                           'srlimit': '7', 'srsearch': '%s haswbstatement:P179' % text})
        return [r['title'] for r in data.get('query', {}).get('search', [])]

    def entities(self, ids, props='labels|aliases|claims'):
        result = {}
        ids = list(dict.fromkeys(ids))
        for start in range(0, len(ids), 50):
            data = self.fetch({'action': 'wbgetentities', 'ids': '|'.join(ids[start:start + 50]),
                               'props': props})
            result.update(data.get('entities', {}))
        return result


def claim_ids(entity, prop):
    ids = []
    for claim in entity.get('claims', {}).get(prop, []):
        value = claim.get('mainsnak', {}).get('datavalue', {}).get('value')
        if isinstance(value, dict) and 'id' in value:
            ids.append(value['id'])
    return ids


def series_claims(entity):
    """[(series id, position or None)] from "part of the series" with "series ordinal"."""
    result = []
    for claim in entity.get('claims', {}).get('P179', []):
        value = claim.get('mainsnak', {}).get('datavalue', {}).get('value')
        if not isinstance(value, dict):
            continue
        ordinals = [q.get('datavalue', {}).get('value')
                    for q in claim.get('qualifiers', {}).get('P1545', [])]
        result.append((value['id'], parse_index(ordinals[0]) if ordinals else None))
    return result


def names_of(entity):
    names = [v['value'] for v in entity.get('labels', {}).values()]
    names += [a['value'] for values in entity.get('aliases', {}).values() for a in values]
    return names


def label(entity, language):
    labels = entity.get('labels', {})
    for lang in (language, 'mul', 'en'):
        if lang in labels:
            return labels[lang]['value']
    return next(iter(labels.values()))['value'] if labels else None


def lookup(wikidata, title, authors, language):
    """Best series match for a book: dict with status "auto", "suggested" or "none"."""
    language = language_of(language)
    candidates = find_works(wikidata, title, language)
    best = best_match(wikidata, candidates, title, authors)
    if best is None:
        return {'status': 'none'}
    work, author_hit = best
    series_id, index = choose_series(wikidata, series_claims(work))
    series = wikidata.entities([series_id], props='labels')[series_id]
    return {'status': 'auto' if author_hit else 'suggested', 'series': label(series, language),
            'index': index, 'qid': work['id'], 'match': label(work, language)}


def find_works(wikidata, title, language):
    """[(found item, work)] for search hits that belong to a series; an edition found by the
    search leads to its work."""
    queries = sorted(title_variants(title), key=len, reverse=True)[:2] or [normalize(title)]
    ids = wikidata.fulltext(queries[0])
    for lang in dict.fromkeys((language, 'en')):
        for query in queries:
            ids += wikidata.search(query, lang)
    if not ids:
        return []
    works = wikidata.entities(ids)
    editions = {w: claim_ids(e, 'P629') for w, e in works.items()}  # edition -> work
    extra = [w for targets in editions.values() for w in targets if w not in works]
    if extra:
        works.update(wikidata.entities(extra))
    candidates = []
    for qid, entity in list(works.items()):
        work = next((works[t] for t in editions.get(qid, []) if t in works), entity)
        if series_claims(work):
            candidates.append((entity, work))
    return candidates


def best_match(wikidata, candidates, title, authors):
    """(work, author matched) for the best candidate whose title matches, else None."""
    if not candidates:
        return None
    people = wikidata.entities([a for _, w in candidates for a in claim_ids(w, 'P50')],
                               props='labels|aliases')
    wanted_title, wanted_authors = title_variants(title), name_tokens(authors)
    best = None
    for entity, work in candidates:
        titles = set().union(*[title_variants(n) for n in names_of(entity) + names_of(work)])
        if not wanted_title & titles:
            continue
        work_authors = claim_ids(work, 'P50')
        author_hit = bool(wanted_authors & name_tokens(
            [n for a in work_authors for n in names_of(people.get(a, {}))]))
        if not author_hit and (work_authors and wanted_authors
                               or not set(claim_ids(work, 'P31')) & BOOK_TYPES):
            continue  # other authors, or not a book at all (film, TV episode, ...)
        if best is None or author_hit and not best[1]:
            best = (work, author_hit)
    return best


def choose_series(wikidata, claims):
    """Prefer series with a position; among those the broadest one (Mistborn #4 over Wax and
    Wayne #1), so one collection covers a whole saga in reading order."""
    numbered = [c for c in claims if c[1] is not None] or claims
    if len(numbered) == 1:
        return numbered[0]
    entities = wikidata.entities([s for s, _ in claims], props='claims')
    parents = {p for s, _ in claims for p in claim_ids(entities.get(s, {}), 'P179')}
    return next((c for c in numbered if c[0] in parents), numbered[0])


class SeriesStore:
    """DATA/series.json: lookup results and manual edits, keyed by book_key()."""

    def __init__(self, data_dir):
        self.path = Path(data_dir) / 'series.json'
        self.lock = threading.Lock()
        self.entries = json.loads(self.path.read_text()) if self.path.exists() else {}

    def get(self, key):
        with self.lock:
            entry = self.entries.get(key)
            return dict(entry) if entry else None

    def put(self, key, **entry):
        with self.lock:
            self.entries[key] = dict(entry, checked=int(time.time()))
            self._save()

    def update(self, key, **fields):
        with self.lock:
            self.entries.setdefault(key, {}).update(fields)
            self._save()

    def remove(self, key):
        with self.lock:
            if self.entries.pop(key, None) is not None:
                self._save()

    def forget(self, statuses):
        """Drop entries with these statuses so they are looked up again."""
        with self.lock:
            self.entries = {k: v for k, v in self.entries.items()
                            if v.get('status') not in statuses}
            self._save()

    def _save(self):
        tmp = self.path.with_suffix('.tmp')
        tmp.write_text(json.dumps(self.entries, indent=1, ensure_ascii=False))
        tmp.replace(self.path)

    def apply(self, meta):
        """Book metadata with an applied override (manual edit or accepted match)."""
        entry = self.get(book_key(meta['title'], meta['authors']))
        if entry and entry.get('status') in APPLIED and (
                entry['status'] == 'manual' or not meta['series']):
            return dict(meta, series=entry.get('series'), series_index=entry.get('index'),
                        series_source=entry['status'])
        return dict(meta, series_source='file' if meta['series'] else None)


class Lookup(threading.Thread):
    """Background worker: looks up books without series, one at a time."""

    def __init__(self, store, books, wikidata=None, interval=600):
        super().__init__(daemon=True, name='series-lookup')
        self.store, self.books, self.interval = store, books, interval
        self.wikidata = wikidata or Wikidata()
        self.wake = threading.Event()
        self.status = {'running': False, 'pending': 0, 'error': None}

    def pending(self):
        now, seen, todo = time.time(), set(), []
        for book in self.books():
            if book['series'] and book.get('series_source') == 'file':
                continue
            key = book_key(book['title'], book['authors'])
            if key in seen:
                continue
            seen.add(key)
            entry = self.store.get(key)
            if entry is None or (entry.get('status') == 'error'
                                 and now - entry.get('checked', 0) > RETRY_ERRORS_AFTER):
                todo.append((key, book))
        return todo

    def run_once(self):
        todo = self.pending()
        self.status.update(running=True, pending=len(todo))
        try:
            for key, book in todo:
                self.status['pending'] -= 1
                try:
                    result = lookup(self.wikidata, book['title'], book['authors'],
                                    book.get('language'))
                    result['series'] = self._existing_name(result.get('series'), book)
                except (OSError, RuntimeError, ValueError, KeyError) as exc:
                    self.status['error'] = '%s: %s' % (book['title'], exc)
                    print('warning: series lookup for %r failed: %s' % (book['title'], exc),
                          flush=True)
                    result = {'status': 'error'}
                self.store.put(key, **result)
        finally:
            self.status.update(running=False, pending=0)

    def _existing_name(self, series, book):
        """Reuse the spelling of a series already in the library by the same author."""
        if not series:
            return series
        authors = name_tokens(book['authors'])
        wanted = normalize(series)
        for other in self.books():
            name = other['series']
            if name and authors & name_tokens(other['authors']) and (
                    normalize(name) == wanted or wanted in normalize(name)
                    or normalize(name) in wanted):
                return name
        return series

    def run(self):
        while True:
            try:
                self.run_once()
            except Exception as exc:  # keep the worker alive; the admin page shows the error
                self.status['error'] = str(exc)
                print('warning: series lookup stopped: %s' % exc, flush=True)
            self.wake.wait(self.interval)
            self.wake.clear()
