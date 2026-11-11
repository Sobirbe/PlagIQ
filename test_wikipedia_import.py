"""Wikipedia dump import: markup cleaning, filtering, attribution, updates and resume."""
import bz2
import json

from fastapi.testclient import TestClient

import engine_service
import wikipedia_import as wiki
from corpus import connect, inventory, search

NS = 'http://www.mediawiki.org/xml/export-0.11/'
LONG_UZ = ("Oʻzbekiston Respublikasi Markaziy Osiyoda joylashgan davlat boʻlib uning poytaxti Toshkent shahri hisoblanadi "
           "va mamlakat iqtisodiyoti qishloq xoʻjaligi sanoat hamda xizmatlar sohasiga asoslangan. ") * 6


def page(pid, title, text, ns=0, redirect=False, rev=100, stamp='2025-06-01T00:00:00Z'):
    red = '<redirect title="X" />' if redirect else ''
    return (f'<page><title>{title}</title><ns>{ns}</ns><id>{pid}</id>{red}<revision><id>{rev}</id>'
            f'<timestamp>{stamp}</timestamp><contributor><username>u</username><id>9</id></contributor>'
            f'<text xml:space="preserve">{text}</text></revision></page>')


def dump(tmp_path, pages, name='uzwiki.xml.bz2'):
    xml = f'<mediawiki xmlns="{NS}"><siteinfo><sitename>Vikipediya</sitename></siteinfo>{"".join(pages)}</mediawiki>'
    path = tmp_path / name
    path.write_bytes(bz2.compress(xml.encode('utf-8')))
    return path


def test_markup_becomes_plain_prose():
    markup = ("{{Infobox country|name=X|capital={{flag|Tashkent}}}}'''Oʻzbekiston''' — [[Markaziy Osiyo|Markaziy Osiyodagi]] "
              "[[davlat]].<ref>{{cite web|url=x}}</ref> [[Fayl:Flag.svg|thumb|Bayroq]] [https://e.org rasmiy sayt]\n"
              "== Tarixi ==\n* birinchi\n{| class=wikitable\n|-\n| a || b\n|}\n[[Turkum:Davlatlar]]<!-- izoh -->&amp;")
    text = wiki.wikitext_to_text(markup)
    assert text.startswith('Oʻzbekiston — Markaziy Osiyodagi davlat.')
    for gone in ('Infobox', 'cite', 'Fayl', 'thumb', 'Turkum', 'wikitable', 'izoh', '[[', '{{', "'''", '<ref'):
        assert gone not in text, gone
    assert 'rasmiy sayt' in text and 'Tarixi' in text and 'birinchi' in text and text.endswith('&')


def test_real_world_markup_leftovers():
    """Cases seen in the live Uzbek dump (Nov 2026)."""
    markup = ('{| style="font-family:Arial Unicode MS; font-size:90%"\n|-\n! Ustun\n| qiymat\n'
              ' == Qoʻqon ==\n'
              '[[Fayl:Taxol.svg|right|thumb|250x250px| Molekulyar [[struktura]] modeli]] Taksol dori.\n'
              'Berlin (nemischa talaffuzi: [[Nemis tili|[bɛʁˈliːn]]]) Germaniya poytaxti.\n'
              'Mirzo Ulugʻbek, [[Zahiriddin Muhammad Bobur asarlari\nSayt: https://qashqadaryo.uz/oz rasmiy.')
    text = wiki.wikitext_to_text(markup)
    for gone in ('{|', 'style=', 'Ustun', '==', 'Fayl', 'thumb', '[[', ']]', 'https://'):
        assert gone not in text, (gone, text)
    assert 'Qoʻqon' in text and 'Taksol dori.' in text and 'Germaniya poytaxti' in text and 'Zahiriddin' in text


def test_import_filters_attributes_and_updates(tmp_path):
    db, root = tmp_path / 'c.db', tmp_path / 'staging'
    pages = [page(1, 'Oʻzbekiston', LONG_UZ), page(2, 'Toshkent', 'qisqa maqola'),              # stub
             page(3, 'Yoʻnaltirish', LONG_UZ, redirect=True), page(4, 'Munozara:X', LONG_UZ, ns=1),
             page(5, 'Ruscha', 'Научный текст о качестве высшего образования в регионах страны. ' * 20)]
    status = wiki.run('uz', batch=10, min_words=50, source=dump(tmp_path, pages), db_path=db, root=root)
    assert (status['imported'], status['skipped_short'], status['skipped_language'], status['dump_complete']) == (1, 1, 1, True)
    with connect(db) as conn:
        (meta,) = inventory(conn)
        assert meta['title'] == 'Oʻzbekiston — Wikipedia' and meta['authors'] == 'Wikipedia contributors'
        assert meta['url'] == 'https://uz.wikipedia.org/wiki/O%CA%BBzbekiston' and meta['year'] == 2025
        assert 'CC BY-SA 4.0' in meta['permission_reference'] and 'revision 100' in meta['permission_reference']
        hit = search(conn, LONG_UZ[:200], 'uz')
        assert hit['sources'][0]['title'] == 'Oʻzbekiston — Wikipedia'
    # Unchanged on the next pass; a new revision replaces the old text instead of duplicating it.
    again = wiki.run('uz', batch=10, min_words=50, source=dump(tmp_path, pages), db_path=db, root=root)
    assert again['unchanged'] == 1 and again['imported'] == 0
    changed = [page(1, 'Oʻzbekiston', LONG_UZ + ' Yangi qoʻshimcha jumla matnga kiritildi.', rev=101)]
    third = wiki.run('uz', batch=10, min_words=50, source=dump(tmp_path, changed, 'v2.xml.bz2'), db_path=db, root=root)
    assert third['updated'] == 1
    with connect(db) as conn:
        rows = inventory(conn)
    assert len(rows) == 1 and 'revision 101' in rows[0]['permission_reference']


def test_batches_resume(tmp_path):
    pages = [page(i, f'Maqola {i}', LONG_UZ + f' Raqam {i} uchun alohida jumla.') for i in range(1, 6)]
    source, db, root = dump(tmp_path, pages), tmp_path / 'c.db', tmp_path / 's'
    first = wiki.run('uz', batch=2, min_words=50, source=source, db_path=db, root=root)
    second = wiki.run('uz', batch=2, min_words=50, source=source, db_path=db, root=root)
    third = wiki.run('uz', batch=2, min_words=50, source=source, db_path=db, root=root)
    assert [first['imported'], second['imported'], third['imported']] == [2, 2, 1]
    assert third['dump_complete'] and not first['dump_complete']
    with connect(db) as conn:
        assert len(inventory(conn)) == 5


def test_only_wikimedia_dump_urls(tmp_path):
    status = wiki.run('uz', source='https://example.org/dump.xml.bz2', db_path=tmp_path / 'c.db', root=tmp_path)
    assert 'wikimedia.org' in status['error'] and status['imported'] == 0


def test_settings_and_engine_status(monkeypatch, tmp_path):
    monkeypatch.setenv('AUTO_IMPORT_WIKIPEDIA', 'uz,fr')
    monkeypatch.setenv('WIKIPEDIA_BATCH', '0')
    assert wiki.settings() == (['uz'], 1, 300)
    monkeypatch.setenv('ENGINE_API_KEY', 'k' * 32)
    monkeypatch.setenv('CORPUS_DB', str(tmp_path / 'c.db'))
    monkeypatch.setenv('STAGING_DIR', str(tmp_path))
    monkeypatch.delenv('AUTO_IMPORT_WIKIPEDIA')
    (tmp_path / 'import-status.json').write_text(json.dumps({'wikipedia-uz': {'imported': 3, 'finished': 'x', 'junk': 1}}))
    data = TestClient(engine_service.app).get('/v1/library', headers={'Authorization': 'Bearer ' + 'k' * 32}).json()
    assert data['imports'] == {'wikipedia-uz': {'imported': 3, 'finished': 'x'}}
