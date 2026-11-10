import asyncio
import io
import time

import httpx
import pytest
from fastapi.testclient import TestClient
import app as web
import engine_client
import engine_service
from corpus import connect, ingest, search


@pytest.fixture(autouse=True)
def clear_cache():
    web.results.clear()
    web.RATE.clear()
    engine_service.reports.clear()


def metadata(language='en'):
    return dict(title='Synthetic test fixture', url='https://example.org/test', language=language,
                authors='Test author', year=2026, rights_basis='written_permission',
                permission_reference='Synthetic test owned by the test author')


def upload(client):
    text='one two three four five six seven eight nine'
    return client.post('/api/check', data={'mode':'reference', 'language':'en', 'reference_text':text},
                       files={'submission':('sample.txt',text.encode(),'text/plain')})


def test_report_ownership_expiry_delete():
    owner, stranger = TestClient(web.app), TestClient(web.app)
    response = upload(owner)
    identifier = response.json()['report_id']
    url = f'/api/report/{identifier}.pdf'
    assert stranger.get(url).status_code == 404
    assert stranger.delete(f'/api/report/{identifier}').status_code == 404
    assert owner.get(url).status_code == 200
    assert response.headers['cache-control'] == 'no-store'
    assert 'HttpOnly' in response.headers['set-cookie']
    assert owner.delete(f'/api/report/{identifier}').status_code == 200
    assert owner.get(url).status_code == 404
    identifier = upload(owner).json()['report_id']
    web.results[identifier]['expires'] = time.monotonic()-1
    assert owner.get(f'/api/report/{identifier}.pdf').status_code == 404
    assert identifier not in web.results


def test_cross_site_and_large_request():
    client = TestClient(web.app)
    assert client.post('/api/check', headers={'Origin':'https://evil.example'}).status_code == 403
    assert client.post('/api/check', content=b'x'*(5*1024*1024+1)).status_code == 413


def test_rights_required_and_idempotent(tmp_path):
    with connect(tmp_path/'corpus.db') as db:
        record=metadata()
        record.pop('permission_reference')
        with pytest.raises(ValueError):
            ingest(db,record,'one two three four five six seven')
        for _ in range(2):
            ingest(db,metadata(),'one two three four five six seven eight')
        assert db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1
        result=search(db,'one two three four five six seven eight','en')
        assert result['unique_passages'] == [[0,39]]
        assert result['language_counts'] == {'uz':0,'ru':0,'en':1}
        assert search(db,'different text with no copied material here','en')['sources'] == []
        assert db.execute('SELECT count(*) FROM sources').fetchone()[0] == 1


@pytest.mark.parametrize('language,text,query',[
    ('uz','Олий таълим муассасаларида сифат муҳим ва долзарб масала',"Oliy ta'lim muassasalarida sifat muhim va dolzarb masala"),
    ('ru','Научный текст содержит семь слов для проверки совпадений','Научный текст содержит семь слов для проверки совпадений'),
    ('en','one two three four five six seven eight','one two three four five six seven eight')])
def test_multilingual_index(tmp_path,language,text,query):
    with connect(tmp_path/'index.db') as db:
        ingest(db,metadata(language),text)
        result=search(db,query,language)
        assert result['unique_passages'] == [[0,len(query)]]
        assert result['sources'][0]['authors'] == 'Test author'


def test_summary_uses_union_not_provider_score():
    report={'query_text_length':20, 'unique_passages':[[0,10],[5,15]],
            'overall_similarity_pct':99, 'total_matched_chars':999}
    assert engine_client.summarize(report)['percent'] == 75
    report['unique_passages']=[[0,21]]
    with pytest.raises(engine_client.EngineError):
        engine_client.summarize(report)


@pytest.mark.parametrize('path',['/v1/checks/../admin','//evil.test/x','/v1/checks/%2e%2e',
                                 '/v1/checks/x?foo=bar','https://evil.test'])
def test_engine_paths_reject_traversal(path):
    with pytest.raises(engine_client.EngineError):
        engine_client.safe_path(path)


def test_private_service_requires_auth(monkeypatch,tmp_path):
    monkeypatch.setenv('ENGINE_API_KEY','x'*32)
    monkeypatch.setenv('CORPUS_DB',str(tmp_path/'index.db'))
    client=TestClient(engine_service.app)
    assert client.post('/v1/checks',json={'query_text':'hello','language':'en'}).status_code == 401
    assert client.get('/docs').status_code == 404


def test_adapter_to_real_private_service(monkeypatch,tmp_path):
    monkeypatch.setenv('ENGINE_API_KEY','x'*32)
    monkeypatch.setenv('ENGINE_URL','http://engine')
    monkeypatch.setenv('CORPUS_DB',str(tmp_path/'index.db'))
    text='one two three four five six seven eight'
    with connect() as db:
        ingest(db,metadata(),text)
    real_client=httpx.AsyncClient
    def local_client(**kwargs):
        return real_client(**kwargs,transport=httpx.ASGITransport(app=engine_service.app))
    monkeypatch.setattr(engine_client.httpx,'AsyncClient',local_client)
    report=asyncio.run(engine_client.run_check(text,'en'))
    assert engine_client.summarize(report)['percent'] == 100
    assert not engine_service.reports  # retrieval deletes transient backend report


def test_unavailable_discovery_does_not_claim_zero(monkeypatch):
    monkeypatch.setattr(web,'discover',lambda text,*a:dict(sources=[],status='unavailable',message='Unavailable',queries=1))
    response=TestClient(web.app).post('/api/check',data={'mode':'academic','language':'en','consent':'true'},
                                    files={'submission':('test.txt',b'one two three four five six seven')})
    assert response.status_code == 200
    assert response.json()['percent'] is None


def test_query_excerpt_is_always_bounded():
    from discovery import suggest_queries
    assert len(suggest_queries('x'*300000)[0]) <= 220


def test_docx_expansion_rejected():
    import zipfile
    payload=io.BytesIO()
    with zipfile.ZipFile(payload,'w',zipfile.ZIP_DEFLATED) as z:
        z.writestr('large.xml',b'x'*(21*1024*1024))
    response=TestClient(web.app).post('/api/check',data={'mode':'reference','reference_text':'source'},
                                    files={'submission':('test.docx',payload.getvalue())})
    assert response.status_code == 413


def test_private_pilot_login(monkeypatch):
    client=TestClient(web.app)
    monkeypatch.setenv('PRIVATE_PILOT_USER','reviewer')
    monkeypatch.setenv('PRIVATE_PILOT_PASSWORD','safe-test-password-123')
    assert client.get('/').status_code == 401
    assert client.get('/health').status_code == 200
    assert client.get('/',auth=('reviewer','safe-test-password-123')).status_code == 200
    monkeypatch.setenv('PRIVATE_PILOT_PASSWORD','short')
    assert client.get('/',auth=('reviewer','short')).status_code == 401


def test_pdf_citation_escapes_unicode_and_long_text():
    import pymupdf as fitz
    client=TestClient(web.app)
    identifier=upload(client).json()['report_id']
    web.results[identifier]['result']['sources']=[{
        'title':'Ўзбекистон · Научная работа <test> & '+('LongWord'*80),
        'url':'https://example.org/article', 'authors':'Муаллиф', 'year':2026,
        'coverage':'indexed full-text source','matched_chars':20,
        'passages':[{'query_start':0,'query_end':20,'source_start':3,'source_end':23,
                     'source_excerpt':'Ўзбекистон <script> & текст'}]}]*20
    response=client.get(f'/api/report/{identifier}.pdf')
    assert response.status_code == 200
    with fitz.open(stream=response.content,filetype='pdf') as pdf:
        text=''.join(page.get_text() for page in pdf)
        assert 'Ўзбекистон' in text and 'Научная' in text
        assert 'Page 2' in text


@pytest.mark.parametrize('bad_response',[
    httpx.Response(503,json={'error':'unavailable'}),
    httpx.Response(202,json=[]),
    httpx.Response(202,json={'status_url':'/v1/checks/../admin','report_url':'/admin'})])
def test_adapter_failure_is_controlled(monkeypatch,bad_response):
    monkeypatch.setenv('ENGINE_API_KEY','x'*32)
    monkeypatch.setenv('ENGINE_URL','http://engine')
    real_client=httpx.AsyncClient
    def client(**kwargs):
        return real_client(**kwargs,transport=httpx.MockTransport(lambda request:bad_response))
    monkeypatch.setattr(engine_client.httpx,'AsyncClient',client)
    with pytest.raises(engine_client.EngineError):
        asyncio.run(engine_client.run_check('one two three four five six seven','en'))


def test_non_ascii_authentication_is_rejected(monkeypatch):
    monkeypatch.setenv('ENGINE_API_KEY','x'*32)
    assert TestClient(engine_service.app).get('/v1/checks/missing',headers=[(b'authorization',b'Bearer \xff')]).status_code == 401
    owner=TestClient(web.app)
    identifier=upload(owner).json()['report_id']
    assert TestClient(web.app).get(f'/api/report/{identifier}.pdf',headers=[(b'cookie',b'plagiq_session=\xff')]).status_code == 404
