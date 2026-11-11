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
from corpus import LANGUAGES, connect, corpus_version, inventory, search
from security import SecurityMiddleware
import import_job
import wikipedia_import
import cyberleninka

log = logging.getLogger("plagiq.import")
import_stop = threading.Event()


def import_loop():
    """Background OpenAlex import when AUTO_IMPORT is set. Runs, then sleeps AUTO_IMPORT_HOURS."""
    while not import_stop.is_set():
        langs, batch, hours, cost = import_job.settings()
        for language in langs:
            if import_stop.is_set():
                return
            try:
                status = import_job.run(language, batch, cost)
                log.warning("import %s: %s imported, %s failed, corpus %s sources, error=%s", language,
                            status["imported"], status["failed"], status["corpus_sources"], status["error"])
            except Exception as exc:                     # never let the import stop the engine
                log.warning("import %s failed: %s", language, type(exc).__name__)
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
                log.warning("wikipedia %s failed: %s", language, type(exc).__name__)
        enabled, cl_batch, cl_set, cl_from = cyberleninka.settings()
        if enabled and not import_stop.is_set():
            try:
                status = cyberleninka.run(cl_batch, cl_set, cl_from)
                log.warning("cyberleninka: %s imported, %s staged, error=%s", status["imported"], status["staged"], status["error"])
            except Exception as exc:
                log.warning("cyberleninka failed: %s", type(exc).__name__)
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
    connect().close()                                    # run any index upgrade before serving or importing
    worker = None
    if import_job.settings()[0] or wikipedia_import.settings()[0] or cyberleninka.settings()[0]:
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
    query_text: str = Field(min_length=1, max_length=300000)
    language: str = Field(pattern='^(uz|ru|en)$')


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
            report = search(db, query.query_text, query.language)
        identifier = uuid.uuid4().hex
        reports[identifier] = (time.monotonic() + 180, report)
        return {'status_url': f'/v1/checks/{identifier}', 'report_url': f'/v1/checks/{identifier}/report'}
    finally:
        lock.release()


@app.get('/v1/library', dependencies=[Depends(authorize)])
def library():
    """Indexed sources per language and the latest import status (no texts, no permission records)."""
    with closing(connect()) as db:
        rows = inventory(db)
        version = corpus_version(db)
    counts = {lang: sum(m['language'] == lang for m in rows) for lang in LANGUAGES}
    allowed = {f'wikipedia-{lang}' for lang in LANGUAGES} | set(LANGUAGES) | {'cyberleninka'}
    status = {name: {k: v for k, v in info.items() if k in (
                  'finished', 'imported', 'updated', 'failed', 'staged_total', 'failed_total', 'listing_complete',
                  'dump_complete', 'estimated_cost_usd', 'error', 'language_corrected', 'staged', 'skipped')}
              for name, info in import_job.read_status().items() if name in allowed and isinstance(info, dict)}
    return {'language_counts': counts, 'total': len(rows), 'corpus_version': version,
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
