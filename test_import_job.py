"""Unattended OpenAlex import, engine library status and the public library summary."""
import json

import pytest
from fastapi.testclient import TestClient

import app as web
import engine_service
import import_job
from corpus import connect, inventory
from test_oa_import import fetcher, services  # noqa: F401  (pytest fixture)


def test_batches_resume_import_and_clean_up(services, tmp_path, monkeypatch):
    base, _ = services
    db, root = tmp_path / 'corpus.db', tmp_path / 'staging'
    monkeypatch.delenv('UNPAYWALL_EMAIL', raising=False)
    first = import_job.run('uz', batch=1, db_path=db, root=root, fetcher=fetcher(), api=base + '/oa')
    assert first['imported'] >= 1 and first['listing_complete'] is False and first['error'] is None
    assert not list((root / 'openalex-uz' / 'files').glob('*.pdf'))          # PDFs removed after indexing
    second = import_job.run('uz', batch=1, db_path=db, root=root, fetcher=fetcher(), api=base + '/oa')
    assert second['listing_complete'] is True                               # resumed on page 2, then finished
    with connect(db) as conn:
        titles = {m['title']: m for m in inventory(conn)}
    # Works 1 and 8 carry the identical PDF: indexed once (duplicate consolidation).
    assert len({'Work 1', 'Work 8'} & set(titles)) == 1 and len(titles) == 2
    assert 'Work 7' in titles and titles['Work 7']['language'] == 'ru'
    assert all('OpenAlex' in m['permission_reference'] for m in titles.values())
    state = json.loads((root / 'openalex-uz' / 'oa-state.json').read_text())
    assert any(v == 'imported' for v in state['imported'].values())
    status = import_job.read_status(root)['uz']
    assert status['corpus_sources'] == len(titles) and status['estimated_cost_usd'] >= 0


def test_failed_download_is_recorded_not_fatal(services, tmp_path):
    base, _ = services
    status = import_job.run('uz', batch=5, db_path=tmp_path / 'c.db', root=tmp_path / 's', fetcher=fetcher(), api=base + '/oa')
    with open(tmp_path / 's' / 'openalex-uz' / 'review.csv', encoding='utf-8-sig') as handle:
        review = handle.read()
    assert 'not a PDF' in review and status['imported'] >= 1


def test_settings_are_bounded(monkeypatch):
    monkeypatch.setenv('AUTO_IMPORT', 'uz, xx,ru')
    monkeypatch.setenv('AUTO_IMPORT_HOURS', '0')
    monkeypatch.setenv('AUTO_IMPORT_BATCH', '999999')
    monkeypatch.setenv('AUTO_IMPORT_MAX_COST', 'lots')
    assert import_job.settings() == (['uz', 'ru'], 5000, 1, 0.20)


def test_engine_library_requires_key_and_reports_counts(monkeypatch, tmp_path):
    monkeypatch.setenv('ENGINE_API_KEY', 'k' * 32)
    monkeypatch.setenv('CORPUS_DB', str(tmp_path / 'c.db'))
    monkeypatch.setenv('STAGING_DIR', str(tmp_path / 'staging'))
    monkeypatch.delenv('AUTO_IMPORT', raising=False)
    from corpus import ingest
    with connect() as db:
        ingest(db, dict(title='T', url='https://e.org', language='uz', authors='A', year=2025,
                        rights_basis='open_license', permission_reference='private note'),
               "Oliy ta'lim muassasalarida sifat muhim va dolzarb masala hisoblanadi")
    import_job.write_status(tmp_path / 'staging', 'uz', {'finished': '2026-11-11T00:00:00+00:00', 'imported': 1, 'secret': 'x'})
    client = TestClient(engine_service.app)
    assert client.get('/v1/library').status_code == 401
    data = client.get('/v1/library', headers={'Authorization': 'Bearer ' + 'k' * 32}).json()
    assert data['language_counts'] == {'uz': 1, 'ru': 0, 'en': 0} and data['total'] == 1
    assert 'secret' not in json.dumps(data) and 'private note' not in json.dumps(data)


def test_engine_starts_import_only_when_enabled(monkeypatch, tmp_path):
    import staging
    monkeypatch.setattr(staging, 'low_disk', lambda db_path=None: None)   # independent of the test machine's free disk
    calls = []
    monkeypatch.setenv('CORPUS_DB', str(tmp_path / 'c.db'))
    monkeypatch.setenv('STAGING_DIR', str(tmp_path / 's'))
    monkeypatch.setattr(import_job, 'run', lambda *a, **k: calls.append(a) or {
        'imported': 0, 'failed': 0, 'corpus_sources': 0, 'error': None})
    monkeypatch.delenv('AUTO_IMPORT', raising=False)
    engine_service.import_stop.clear()
    with TestClient(engine_service.app):
        pass
    assert calls == []
    monkeypatch.setenv('AUTO_IMPORT', 'uz')
    engine_service.import_stop.clear()
    with TestClient(engine_service.app):
        for _ in range(50):
            if calls:
                break
            import time; time.sleep(0.05)
    assert calls and calls[0][0] == 'uz'
    engine_service.import_stop.clear()


def test_public_library_summary(monkeypatch):
    web.LIBRARY_CACHE.update(at=-1e9, data=None)
    monkeypatch.delenv('ENGINE_URL', raising=False)
    assert TestClient(web.app).get('/api/library').json() == {'configured': False, 'available': False}
    async def fake():
        return {'language_counts': {'uz': 3, 'ru': 1, 'en': 0}, 'total': 4, 'last_import': '2026-11-11', 'auto_import': ['uz']}
    monkeypatch.setattr(web, 'engine_library', fake)
    web.LIBRARY_CACHE.update(at=-1e9, data=None)
    data = TestClient(web.app).get('/api/library').json()
    assert data['available'] is True and data['total'] == 4 and data['language_counts']['uz'] == 3
