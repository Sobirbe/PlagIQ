"""Authenticated private backend compatible with the PlagIQ Noplag adapter.

No public ingestion endpoint; no manuscript persistence; bounded transient reports.
"""
import asyncio
from contextlib import asynccontextmanager
import os
import secrets
import threading
import time
import uuid
from fastapi import Depends, FastAPI, Header, HTTPException
from pydantic import BaseModel, Field
from contextlib import closing
import logging
import gzip
import json
import re
import shutil
from fastapi.responses import StreamingResponse
from corpus import DEPOSITS, LANGUAGES, category, connect, corpus_version, deposits, ingest, inventory, remove_deposit, search
from security import SecurityMiddleware
import import_job
import staging
import wikipedia_import
import cyberleninka
import uz_journals

log = logging.getLogger("plagiq.import")
import_stop = threading.Event()


def import_loop():
    """Background OpenAlex import when AUTO_IMPORT is set. Runs, then sleeps AUTO_IMPORT_HOURS."""
    while not import_stop.is_set():
        langs, batch, hours, cost = import_job.settings()
        if staging.low_disk():
            freed = staging.purge_leftovers(import_job.staging_root())
            if freed:
                log.warning("low disk: removed %s MB of downloaded files that were not yet indexed", freed // (1024 * 1024))
        paused = staging.low_disk()
        if paused:
            log.warning("imports paused: %s", paused)
            import_job.write_status(import_job.staging_root(), "disk", {"error": paused})
            import_stop.wait(min(hours, 6) * 3600)
            continue
        import_job.write_status(import_job.staging_root(), "disk", {"error": None})
        for language in langs:
            if import_stop.is_set():
                return
            try:
                status = import_job.run(language, batch, cost)
                log.warning("import %s: %s imported, %s failed, corpus %s sources, error=%s", language,
                            status["imported"], status["failed"], status["corpus_sources"], status["error"])
            except Exception as exc:                     # never let the import stop the engine
                log.warning("import %s failed: %s: %s", language, type(exc).__name__, str(exc)[:300])
                import_job.write_status(import_job.staging_root(), language,
                                        {"language": language, "error": type(exc).__name__ + ": " + str(exc)[:200]})
        wiki_langs, wiki_batch, min_words = wikipedia_import.settings()
        for language in wiki_langs:
            if import_stop.is_set():
                return
            try:
                status = wikipedia_import.run(language, wiki_batch, min_words)
                log.warning("wikipedia %s: %s new, %s updated, corpus %s sources, error=%s", language,
                            status["imported"], status["updated"], status["corpus_sources"], status["error"])
            except Exception as exc:
                log.warning("wikipedia %s failed: %s: %s", language, type(exc).__name__, str(exc)[:300])
                import_job.write_status(import_job.staging_root(), f"wikipedia-{language}",
                                        {"language": language, "error": type(exc).__name__ + ": " + str(exc)[:200]})
        enabled, cl_batch, cl_set, cl_from = cyberleninka.settings()
        if enabled and not import_stop.is_set():
            try:
                status = cyberleninka.run(cl_batch, cl_set, cl_from)
                log.warning("cyberleninka: %s imported, %s staged, error=%s", status["imported"], status["staged"], status["error"])
            except Exception as exc:
                log.warning("cyberleninka failed: %s: %s", type(exc).__name__, str(exc)[:300])
                import_job.write_status(import_job.staging_root(), "cyberleninka",
                                        {"error": type(exc).__name__ + ": " + str(exc)[:200]})
        uz_enabled, uz_batch = uz_journals.settings()
        if uz_enabled and not import_stop.is_set():
            try:
                status = uz_journals.run(uz_batch)
                log.warning("uz journals: %s imported from %s journals, error=%s", status.get("imported"),
                            status.get("journals"), status["error"])
            except Exception as exc:
                log.warning("uz journals failed: %s: %s", type(exc).__name__, str(exc)[:300])
                import_job.write_status(import_job.staging_root(), "uz-journals",
                                        {"error": type(exc).__name__ + ": " + str(exc)[:200]})
        import_stop.wait(hours * 3600)

@asynccontextmanager
async def lifespan(app):
    async def cleanup():
        while True:
            await asyncio.sleep(30)
            now = time.monotonic()
            if not lock.acquire(blocking=False):
                continue
            try:
                for key in list(reports):
                    if reports[key][0] <= now:
                        del reports[key]
            finally:
                lock.release()
    task = asyncio.create_task(cleanup())
    # A full volume must not stop the engine from starting: free re-downloadable files first,
    # and never let the startup check crash the service (searches still work on a full disk).
    if staging.low_disk():
        try:
            freed = staging.purge_leftovers(import_job.staging_root())
            log.warning("startup on low disk: removed %s MB of downloaded files not yet indexed", freed // (1024 * 1024))
        except OSError as exc:
            log.warning("startup cleanup failed: %s", exc)
    try:
        connect().close()                                # run any index upgrade before serving or importing
    except Exception as exc:
        log.warning("startup database check failed: %s: %s", type(exc).__name__, str(exc)[:300])
    worker = None
    if (import_job.settings()[0] or wikipedia_import.settings()[0] or cyberleninka.settings()[0]
            or uz_journals.settings()[0]):
        worker = threading.Thread(target=import_loop, name="auto-import", daemon=True)
        worker.start()
    try:
        yield
    finally:
        import_stop.set()
        task.cancel()
        try:
            await task
        except asyncio.CancelledError:
            pass


app = FastAPI(title='PlagIQ private corpus', docs_url=None, redoc_url=None, openapi_url=None, lifespan=lifespan)
app.add_middleware(SecurityMiddleware)
reports = {}
lock = threading.Lock()


def authorize(authorization: str = Header('')):
    key = os.environ.get('ENGINE_API_KEY', '')
    if len(key) < 32 or not secrets.compare_digest(authorization.encode(), ('Bearer ' + key).encode()):
        raise HTTPException(401, 'Private service authentication required')


class Query(BaseModel):
    query_text: str = Field(min_length=1, max_length=1_200_000)
    language: str = Field(pattern='^(uz|ru|en)$')
    exclude: list[str] = Field(default_factory=list, max_length=5)


OWNER = r'^(user|org):[0-9a-f]{24}$'


class Deposit(BaseModel):
    text: str = Field(min_length=1, max_length=1_200_000)
    language: str = Field(pattern='^(uz|ru|en)$')
    title: str = Field(min_length=1, max_length=300)
    authors: str = Field(min_length=1, max_length=300)
    year: int = Field(ge=1900, le=2200)
    owner: str = Field(pattern=OWNER)
    rights_basis: str = Field(pattern='^(author_deposit|institution_deposit)$')
    permission_reference: str = Field(min_length=1, max_length=300)
    collection: str = Field(default='', max_length=120)


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.post('/v1/checks', status_code=202, dependencies=[Depends(authorize)])
def check(query: Query):
    # Serialize bounded pilot jobs to limit CPU/memory; deploy a queue before scaling.
    if not lock.acquire(blocking=False):
        raise HTTPException(429, 'Engine busy')
    try:
        now = time.monotonic()
        for key in list(reports):
            if reports[key][0] <= now:
                del reports[key]
        if len(reports) >= 10:
            raise HTTPException(429, 'Transient report capacity reached')
        with closing(connect()) as db:
            report = search(db, query.query_text, query.language,
                            exclude={x for x in query.exclude if re.fullmatch('[0-9a-f]{64}', x)})
        identifier = uuid.uuid4().hex
        reports[identifier] = (time.monotonic() + 180, report)
        return {'status_url': f'/v1/checks/{identifier}', 'report_url': f'/v1/checks/{identifier}/report'}
    finally:
        lock.release()


@app.post('/v1/archive', status_code=201, dependencies=[Depends(authorize)])
def deposit(item: Deposit):
    """Add a deposited (unpublished) work to the protected archive. Only the website calls this,
    after the author's or institution's consent; the text is indexed but never shown to others."""
    with closing(connect()) as db:
        try:
            identifier = ingest(db, item.model_dump(), item.text.replace('\x00', ''))
        except ValueError as exc:
            raise HTTPException(422, str(exc)[:200])
    return {'id': identifier}


@app.get('/v1/archive', dependencies=[Depends(authorize)])
def list_deposits(owner: str):
    if not re.fullmatch(OWNER, owner):
        raise HTTPException(422, 'Invalid owner')
    with closing(connect()) as db:
        rows = deposits(db, owner)
    return {'items': [{'id': i, 'title': m.get('title', ''), 'authors': m.get('authors', ''), 'year': m.get('year'),
                       'language': m.get('language'), 'words': m.get('words'), 'deposited': m.get('imported_at'),
                       'collection': m.get('collection', '')} for i, m in rows[:500]]}


@app.delete('/v1/archive/{identifier}', dependencies=[Depends(authorize)])
def delete_deposit(identifier: str, owner: str):
    if not re.fullmatch('[0-9a-f]{64}', identifier) or not re.fullmatch(OWNER, owner):
        raise HTTPException(422, 'Invalid request')
    with closing(connect()) as db:
        if not remove_deposit(db, identifier, owner):
            raise HTTPException(404, 'Not found')
    return {'deleted': True}


@app.get('/v1/archive/export', dependencies=[Depends(authorize)])
def export_deposits():
    """Backup of every deposit (metadata and text) as gzip-compressed JSON lines. Deposits are the
    only library content that cannot be downloaded again from its source."""
    def lines():
        with closing(connect()) as db:
            for identifier, raw, text in db.execute('SELECT id, metadata, text FROM sources'):
                metadata = json.loads(raw)
                if metadata.get('rights_basis') in DEPOSITS:
                    yield gzip.compress((json.dumps({'id': identifier, 'metadata': metadata, 'text': text},
                                                    ensure_ascii=False) + '\n').encode(), mtime=0)
    return StreamingResponse(lines(), media_type='application/gzip',
                             headers={'Content-Disposition': 'attachment; filename="plagiq-archive.jsonl.gz"'})


@app.get('/v1/library', dependencies=[Depends(authorize)])
def library():
    """Indexed sources per language and the latest import status (no texts, no permission records)."""
    with closing(connect()) as db:
        rows = inventory(db)
        version = corpus_version(db)
    counts = {lang: sum(m['language'] == lang for m in rows) for lang in LANGUAGES}
    by_source = {}
    for m in rows:
        by_source[category(m)] = by_source.get(category(m), 0) + 1
    allowed = {f'wikipedia-{lang}' for lang in LANGUAGES} | set(LANGUAGES) | {'cyberleninka', 'uz-journals', 'disk'}
    status = {name: {k: v for k, v in info.items() if k in (
                  'finished', 'imported', 'updated', 'failed', 'staged_total', 'failed_total', 'listing_complete',
                  'dump_complete', 'estimated_cost_usd', 'error', 'language_corrected', 'staged', 'skipped',
                  'journals', 'downloaded', 'surveyed', 'approved')}
              for name, info in import_job.read_status().items() if name in allowed and isinstance(info, dict)}
    db_path = os.environ.get('CORPUS_DB', 'corpus.sqlite3')
    try:
        usage = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path)))
        disk = {'db_bytes': os.path.getsize(db_path) if os.path.exists(db_path) else 0,
                'staging_bytes': staging.size(import_job.staging_root()),
                'free_bytes': usage.free, 'total_bytes': usage.total}
    except OSError:
        disk = None
    return {'language_counts': counts, 'by_source': by_source, 'total': len(rows), 'corpus_version': version, 'disk': disk,
            'auto_import': import_job.settings()[0], 'auto_import_wikipedia': wikipedia_import.settings()[0],
            'imports': status}


@app.get('/v1/checks/{identifier}', dependencies=[Depends(authorize)])
def status(identifier: str):
    entry = reports.get(identifier)
    if entry is None or entry[0] <= time.monotonic():
        reports.pop(identifier, None)
        raise HTTPException(404, 'Expired')
    return {'status': 'complete'}


@app.get('/v1/checks/{identifier}/report', dependencies=[Depends(authorize)])
def report(identifier: str):
    status(identifier)
    # One-time retrieval removes backend results immediately.
    entry = reports.pop(identifier, None)
    if entry is None:
        raise HTTPException(404, 'Expired')
    return entry[1]
