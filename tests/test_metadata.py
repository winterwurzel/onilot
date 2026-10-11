"""Series matching against a fake Wikidata (the network is the only thing replaced)."""
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from onilot import metadata  # noqa: E402


def item(qid, labels, claims=None, aliases=None):
    return {'id': qid, 'labels': {lang: {'value': v} for lang, v in labels.items()},
            'aliases': {lang: [{'value': v} for v in vs] for lang, vs in (aliases or {}).items()},
            'claims': claims or {}}


def ref(prop_value, ordinal=None):
    claim = {'mainsnak': {'datavalue': {'value': {'id': prop_value}}}}
    if ordinal is not None:
        claim['qualifiers'] = {'P1545': [{'datavalue': {'value': ordinal}}]}
    return claim


SANDERSON = item('Q1', {'en': 'Brandon Sanderson'})
OTHER = item('Q2', {'en': 'Someone Else'})
MISTBORN = item('Q10', {'mul': 'Mistborn', 'am': 'ሚስትቦርን'})
WAX = item('Q11', {'en': 'Wax and Wayne'}, {'P179': [ref('Q10')]})
COSMERE = item('Q12', {'en': 'Cosmere'})
ENTITIES = {e['id']: e for e in [
    SANDERSON, OTHER, MISTBORN, WAX, COSMERE,
    item('Q100', {'en': 'Mistborn: The Well of Ascension'},
         {'P50': [ref('Q1')], 'P31': [ref('Q7725634')], 'P179': [ref('Q10', '2'), ref('Q12')]}),
    item('Q101', {'en': 'Mistborn: The Alloy of Law'},
         {'P50': [ref('Q1')], 'P179': [ref('Q10', '4'), ref('Q11', '1')]}),
    item('Q102', {'en': 'The Well of Ascension'},            # same title, different author
         {'P50': [ref('Q2')], 'P179': [ref('Q12', '9')]}),
    item('Q103', {'de': 'Die Verwandlung'},                 # TV episode, no author
         {'P31': [ref('Q21191270')], 'P179': [ref('Q12', '24')]}),
    item('Q104', {'en': 'Lonely Book'}, {'P50': [ref('Q1')]}),  # no series
    item('Q105', {'en': 'Anonymous Tale'}, {'P31': [ref('Q8261')], 'P179': [ref('Q10', '7')]}),
    item('Q106', {'de': 'Ausgabe'}, {'P629': [ref('Q100')]}),   # an edition of Q100
]}
SEARCH = {'well of ascension': ['Q102', 'Q100'], 'the well of ascension': ['Q102', 'Q100'],
          'alloy of law': ['Q101'], 'the alloy of law': ['Q101'], 'die verwandlung': ['Q103'],
          'lonely book': ['Q104'], 'anonymous tale': ['Q105'], 'ausgabe': ['Q106']}


class FakeWikidata(metadata.Wikidata):
    def __init__(self):
        super().__init__(fetch=self.answer)
        self.calls = []

    def answer(self, params):
        self.calls.append(params)
        if params['action'] == 'wbsearchentities':
            return {'search': [{'id': i} for i in SEARCH.get(params['search'], [])]}
        if params['action'] == 'query':
            text = params['srsearch'].replace(' haswbstatement:P179', '')
            return {'query': {'search': [{'title': i} for i in SEARCH.get(text, [])]}}
        ids = params['ids'].split('|')
        return {'entities': {i: ENTITIES[i] for i in ids if i in ENTITIES}}


class LookupTest(unittest.TestCase):
    def lookup(self, title, authors, language='en'):
        return metadata.lookup(FakeWikidata(), title, authors, language)

    def test_title_and_author_match_applies_automatically(self):
        result = self.lookup('The Well of Ascension', ['Sanderson, Brandon'])
        self.assertEqual(result, {'status': 'auto', 'series': 'Mistborn', 'index': 2.0,
                                  'qid': 'Q100', 'match': 'Mistborn: The Well of Ascension'})

    def test_broadest_numbered_series_wins(self):
        result = self.lookup('Mistborn: The Alloy of Law', ['Brandon Sanderson'])
        self.assertEqual((result['series'], result['index']), ('Mistborn', 4.0))

    def test_other_authors_and_non_books_are_not_matched(self):
        self.assertEqual(self.lookup('The Well of Ascension', ['Jane Doe']),
                         {'status': 'none'})
        self.assertEqual(self.lookup('Die Verwandlung', ['Franz Kafka'], 'de'),
                         {'status': 'none'})
        self.assertEqual(self.lookup('Lonely Book', ['Brandon Sanderson']), {'status': 'none'})
        self.assertEqual(self.lookup('Nothing Like This', ['Nobody']), {'status': 'none'})

    def test_book_without_author_data_is_only_suggested(self):
        result = self.lookup('Anonymous Tale', ['Some Writer'])
        self.assertEqual((result['status'], result['series'], result['index']),
                         ('suggested', 'Mistborn', 7.0))

    def test_edition_leads_to_the_work(self):
        result = self.lookup('Ausgabe', ['Brandon Sanderson'], 'de')
        self.assertEqual((result['status'], result['qid'], result['index']),
                         ('auto', 'Q100', 2.0))

    def test_helpers(self):
        self.assertEqual(metadata.language_of('ger'), 'de')
        self.assertEqual(metadata.language_of('de-AT'), 'de')
        self.assertEqual(metadata.language_of(None), 'en')
        self.assertEqual(metadata.book_key('Über Bücher!', ['B', 'A']), 'uber bucher|a,b')
        self.assertIn('the final empire', metadata.title_variants('Mistborn: The Final Empire'))


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = metadata.SeriesStore(self.tmp.name)
        self.book = {'title': 'T', 'authors': ['A'], 'series': None, 'series_index': None}
        self.key = metadata.book_key('T', ['A'])

    def tearDown(self):
        self.tmp.cleanup()

    def test_applied_statuses_and_file_priority(self):
        for status, applied in (('auto', True), ('accepted', True), ('suggested', False),
                                ('rejected', False), ('none', False)):
            self.store.put(self.key, status=status, series='S', index=1.0)
            self.assertEqual(self.store.apply(self.book)['series'], 'S' if applied else None,
                             status)
        from_file = dict(self.book, series='File', series_index=3.0)
        self.store.put(self.key, status='auto', series='S', index=1.0)
        self.assertEqual(self.store.apply(from_file)['series'], 'File')
        self.store.put(self.key, status='manual', series=None, index=None)
        self.assertIsNone(self.store.apply(from_file)['series'])   # a manual edit wins

    def test_persists_and_forgets(self):
        self.store.put(self.key, status='none')
        self.store.put('other', status='auto', series='S', index=None)
        again = metadata.SeriesStore(self.tmp.name)
        self.assertEqual(again.get(self.key)['status'], 'none')
        again.forget(('none',))
        self.assertIsNone(metadata.SeriesStore(self.tmp.name).get(self.key))
        self.assertIsNotNone(metadata.SeriesStore(self.tmp.name).get('other'))


class WorkerTest(unittest.TestCase):
    def test_run_once_looks_up_each_book_once_and_reuses_library_spelling(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        store = metadata.SeriesStore(tmp.name)
        books = [
            {'title': 'The Final Empire', 'authors': ['Brandon Sanderson'], 'series':
             'Mistborn Saga', 'series_index': 1.0, 'series_source': 'file'},
            {'title': 'The Well of Ascension', 'authors': ['Brandon Sanderson'], 'series': None,
             'series_index': None, 'series_source': None},
            {'title': 'The Well of Ascension', 'authors': ['Brandon Sanderson'], 'series': None,
             'series_index': None, 'series_source': None},   # same book, other library
            {'title': 'Broken', 'authors': ['X'], 'series': None, 'series_index': None,
             'series_source': None},
        ]
        fake = FakeWikidata()
        fake.fetch = lambda params: (_ for _ in ()).throw(OSError('offline')) \
            if params.get('search') == 'broken' else fake.answer(params)
        worker = metadata.Lookup(store, lambda: books, wikidata=fake)
        worker.run_once()
        lookups = [c for c in fake.calls if c['action'] == 'query']
        self.assertEqual(len(lookups), 2)   # the duplicate is looked up once, the file one never
        well = store.get(metadata.book_key('The Well of Ascension', ['Brandon Sanderson']))
        self.assertEqual((well['status'], well['series']), ('auto', 'Mistborn Saga'))
        self.assertIsNone(store.get(metadata.book_key('The Final Empire',
                                                      ['Brandon Sanderson'])))
        self.assertEqual(store.get(metadata.book_key('Broken', ['X']))['status'], 'error')
        self.assertIn('offline', worker.status['error'])
        calls = len(fake.calls)
        worker.run_once()
        self.assertEqual(len(fake.calls), calls)   # nothing left to look up


if __name__ == '__main__':
    unittest.main()
