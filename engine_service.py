"""Authenticated private backend compatible with the PlagIQ Noplag adapter.

No public ingestion endpoint; no manuscript persistence; bounded transient reports.
"""
import asyncio
from contextlib import asynccontextmanager
import os
import secrets
import threading
from pathlib import Path
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
import sqlite3
from fastapi.responses import StreamingResponse
import corpus
from corpus import DEPOSITS, LANGUAGES, category, connect, corpus_version, deposits, ingest, inventory, remove_deposit, search
from security import SecurityMiddleware
import import_job
import staging
import wikipedia_import
import cyberleninka
import crosslang
import uz_journals

log = logging.getLogger("plagiq.import")
import_stop = threading.Event()


def library_room(db_path=None):
    """How many more library texts CORPUS_MAX_SOURCES allows (authors' and universities' deposits not counted)."""
    try:
        with closing(connect(db_path)) as db:
            marks = ",".join("?" * len(corpus.DEPOSITS))
            count = db.execute(f"SELECT COUNT(*) FROM sources WHERE json_extract(metadata, '$.rights_basis') NOT IN "
                               f"({marks})", corpus.DEPOSITS).fetchone()[0]           # as corpus._check_capacity counts
    except sqlite3.Error:
        return 10**9                                    # cannot tell: let the importers find out themselves
    return corpus.max_sources() - count


def library_full_note(db_path=None):
    """When the library is at CORPUS_MAX_SOURCES: delete downloaded files waiting to be added (they could not be
    added and only took disk space; they are fetched again once there is room) and return the note for /admin.
    None while there is room."""
    room = library_room(db_path)
    if room > 0:
        return None
    freed = staging.purge_leftovers(import_job.staging_root())
    return (f"Library full: {corpus.max_sources() - room:,} of {corpus.max_sources():,} texts (CORPUS_MAX_SOURCES). "
            "Nothing is downloaded until the limit is raised or texts are removed (e.g. Wikipedia pruned)"
            + (f"; {freed // (1024 * 1024)} MB of downloaded files that could not be added were deleted." if freed else "."))


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
        full = library_full_note()
        import_job.write_status(import_job.staging_root(), "limit", {"error": full})
        if full:                                        # nothing could be added: do not download (it only fills the disk)
            log.warning("imports skipped: %s", full)
            import_stop.wait(hours * 3600)
            continue
        for language in langs:
            if import_stop.is_set():
                return
            if library_room() <= 0:
                break
            try:
                status = import_job.run(language, min(batch, library_room()), cost)
                log.warning("import %s: %s imported, %s failed, corpus %s sources, error=%s", language,
                            status["imported"], status["failed"], status["corpus_sources"], status["error"])
            except Exception as exc:                     # never let the import stop the engine
                log.warning("import %s failed: %s: %s", language, type(exc).__name__, str(exc)[:300])
                import_job.write_status(import_job.staging_root(), language,
                                        {"language": language, "error": type(exc).__name__ + ": " + str(exc)[:200]})
        zen_langs, zen_batch, zen_cost = import_job.zenodo_settings()
        for language in zen_langs:
            if import_stop.is_set():
                return
            try:
                if library_room() <= 0:
                    break
                status = import_job.run_zenodo(language, min(zen_batch, library_room()), zen_cost)
                log.warning("import zenodo-%s: %s imported, %s failed, error=%s", language, status["imported"],
                            status["failed"], status["error"])
            except Exception as exc:
                log.warning("import zenodo-%s failed: %s: %s", language, type(exc).__name__, str(exc)[:300])
                import_job.write_status(import_job.staging_root(), f"zenodo-{language}",
                                        {"language": language, "error": type(exc).__name__ + ": " + str(exc)[:200]})
        relabel, rl_batch, rl_cost = import_job.uz_from_ru_settings()
        if relabel and not import_stop.is_set() and library_room() > 0:
            try:
                status = import_job.run_uz_from_ru(min(rl_batch, library_room()), rl_cost)
                log.warning("import uz-from-ru: %s imported (%s relabelled to uz), %s failed, error=%s", status["imported"],
                            status.get("relabelled"), status["failed"], status["error"])
            except Exception as exc:
                log.warning("import uz-from-ru failed: %s: %s", type(exc).__name__, str(exc)[:300])
                import_job.write_status(import_job.staging_root(), "uz-from-ru",
                                        {"language": "uz", "error": type(exc).__name__ + ": " + str(exc)[:200]})
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
        if enabled and not import_stop.is_set() and library_room() > 0:
            try:
                status = cyberleninka.run(min(cl_batch, library_room()), cl_set, cl_from)
                log.warning("cyberleninka: %s imported, %s staged, error=%s", status["imported"], status["staged"], status["error"])
            except Exception as exc:
                log.warning("cyberleninka failed: %s: %s", type(exc).__name__, str(exc)[:300])
                import_job.write_status(import_job.staging_root(), "cyberleninka",
                                        {"error": type(exc).__name__ + ": " + str(exc)[:200]})
        uz_enabled, uz_batch = uz_journals.settings()
        if uz_enabled and not import_stop.is_set() and library_room() > 0:
            try:
                status = uz_journals.run(min(uz_batch, library_room()))
                log.warning("uz journals: %s imported from %s journals, error=%s", status.get("imported"),
                            status.get("journals"), status["error"])
            except Exception as exc:
                log.warning("uz journals failed: %s: %s", type(exc).__name__, str(exc)[:300])
                import_job.write_status(import_job.staging_root(), "uz-journals",
                                        {"error": type(exc).__name__ + ": " + str(exc)[:200]})
        import_stop.wait(hours * 3600)

UPGRADES = (("ligature upgrade", corpus.ligature_upgrade, "all stored texts use expanded ligatures"),
            ("edited-copy index", corpus.seeds_upgrade, "every stored text can be found for edited copying"),
            ("Uzbek loanword fingerprints", corpus.uz_fold_upgrade, "Uzbek texts match loanwords across scripts"))


def index_upgrade_loop():
    """Once, after start-up: bring texts indexed by an older version up to date, a batch at a time under the
    search lock, so searches keep working (they only wait for one short batch). Ligatures (ﬁ, ﬂ ...) are
    re-fingerprinted first, then the edited-copy seeds are added. Until the seeds are complete, edited copies
    are found only in texts that already have them; exact matching is unaffected."""
    for name, step, done in UPGRADES:
        while not import_stop.is_set():
            try:
                if not lock.acquire(timeout=queue_seconds()):
                    continue
                try:
                    with closing(connect()) as db:
                        remaining = step(db)
                finally:
                    lock.release()
            except Exception as exc:                 # never stop the engine for this
                log.warning("%s failed: %s: %s", name, type(exc).__name__, str(exc)[:300])
                break
            if not remaining:
                log.warning("%s: %s", name, done)
                break
            log.warning("%s: %s texts left", name, remaining)
            import_stop.wait(1)


CROSSLANG_STATE = {"state": "off", "error": ""}


def crosslang_loop():
    """CROSSLANG=1: download the model once (to the volume), load it, then index the library a few texts at a time,
    only while no check is running (the search lock is taken without waiting). Never stops the engine."""
    try:
        if crosslang.missing_files():
            CROSSLANG_STATE.update(state="downloading")
            crosslang.download()
        if crosslang.needs_shrink():
            CROSSLANG_STATE.update(state="shrinking")
            crosslang.shrink()
        CROSSLANG_STATE.update(state="loading")
        encoder = crosslang.model()
        CROSSLANG_STATE.update(state="indexing")
    except Exception as exc:
        CROSSLANG_STATE.update(state="error", error=f"{type(exc).__name__}: {str(exc)[:200]}")
        log.warning("crosslang unavailable: %s", CROSSLANG_STATE["error"])
        return
    while not import_stop.is_set():
        done = 0
        if lock.acquire(blocking=False):
            try:
                with closing(connect()) as db:
                    done = crosslang.convert_some(db)      # rows in the earlier float16 format first: frees space
                    paused = crosslang_low_disk()
                    if not paused:
                        done += crosslang.index_some(db, encoder, limit=3)
                    status = crosslang.index_status(db)
                CROSSLANG_STATE.update(state="ready" if status["indexed"] >= status["total"] else
                                       "paused" if paused else "indexing", error="")
            except Exception as exc:
                CROSSLANG_STATE.update(error=f"{type(exc).__name__}: {str(exc)[:200]}")
                log.warning("crosslang indexing failed: %s", CROSSLANG_STATE["error"])
            finally:
                lock.release()
        import_stop.wait(0.2 if done else 30)


def crosslang_low_disk(db_path=None):
    """Indexing for translated / paraphrased reuse writes into the library file: it stops while the engine volume has
    less than CROSSLANG_MIN_FREE_PERCENT (5) of its size free, at least 150 MB, so the volume never fills up (imports
    stop earlier, at 10 %; the remaining room is kept for authors' archive deposits and the database's own work).
    Checks are not affected; texts not yet indexed are only left out of translated / paraphrased matching."""
    free, total = staging.disk(db_path or os.environ.get("CORPUS_DB", "corpus.sqlite3"))
    if free is None or not total:
        return False
    try:
        share = min(max(float(os.environ.get("CROSSLANG_MIN_FREE_PERCENT", "5")), 1), 50) / 100
    except ValueError:
        share = 0.05
    return free < max(150 * 1024 * 1024, total * share)


def paraphrase_enabled():
    """PARAPHRASE=1 (needs CROSSLANG=1 and its model): same-language meaning matching (evaluation/PARAPHRASE.md)."""
    return os.environ.get("PARAPHRASE", "0") == "1" and crosslang.enabled()


def translated(db, text, language, exclude_ids=(), same_language=False, skip=()):
    """Possible translated reuse for /v1/checks, when the model is loaded (never waits for it). same_language=True:
    possible paraphrased reuse instead, leaving out manuscript spans in `skip` (already found by word matching)."""
    if not crosslang.enabled() or crosslang._model is None:
        return None
    exclude = {row[0] for row in db.execute(
        f"SELECT num FROM sources WHERE id IN ({','.join('?' * len(exclude_ids))})", list(exclude_ids))} if exclude_ids else set()
    out = []
    for item in crosslang.search(db, text, language, crosslang._model, exclude=exclude,
                                 same_language=same_language, skip=skip)[:10]:
        identifier, raw, source_text = db.execute("SELECT id, metadata, text FROM sources WHERE num=?", (item["num"],)).fetchone()
        m = json.loads(raw)
        deposit = m.get("rights_basis") in DEPOSITS
        out.append({"title": m.get("title", ""), "authors": m.get("authors", ""), "year": m.get("year"),
                    "url": "" if deposit else m.get("url", ""), "language": m.get("language"),
                    "rights_basis": m.get("rights_basis"), "owner": m.get("owner", "") if deposit else "",
                    "sentences": item["sentences"],
                    "pairs": [{"query_start": qs, "query_end": qe, "similarity": sim,
                               "source_excerpt": "" if deposit else source_text[ss:se][:500]}
                              for qs, qe, ss, se, sim in item["pairs"]]})
    return out


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
    threading.Thread(target=index_upgrade_loop, name="index-upgrade", daemon=True).start()
    if crosslang.enabled():
        threading.Thread(target=crosslang_loop, name="crosslang", daemon=True).start()
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


def queue_seconds():
    try:
        return max(0.0, min(float(os.environ.get('ENGINE_QUEUE_SECONDS', '60')), 150.0))
    except ValueError:
        return 60.0


@app.get('/health')
def health():
    return {'status': 'ok'}


@app.post('/v1/checks', status_code=202, dependencies=[Depends(authorize)])
def check(query: Query):
    # One search at a time limits CPU and memory. Other requests wait their turn (up to
    # ENGINE_QUEUE_SECONDS, default 60) instead of failing at once; FastAPI runs this sync
    # endpoint in its thread pool, so waiting requests do not block the event loop.
    if not lock.acquire(timeout=queue_seconds()):
        raise HTTPException(429, 'Engine busy')
    try:
        now = time.monotonic()
        for key in list(reports):
            if reports[key][0] <= now:
                del reports[key]
        if len(reports) >= 10:
            raise HTTPException(429, 'Transient report capacity reached')
        with closing(connect()) as db:
            exclude = {x for x in query.exclude if re.fullmatch('[0-9a-f]{64}', x)}
            report = search(db, query.query_text, query.language, exclude=exclude)
            try:
                found = translated(db, query.query_text, query.language, exclude)
            except Exception as exc:                 # never fail a check because of this
                log.warning("crosslang search failed: %s: %s", type(exc).__name__, str(exc)[:200])
                found = None
            if found is not None:
                report['translated'] = found
            if paraphrase_enabled():
                try:
                    found = translated(db, query.query_text, query.language, exclude, same_language=True,
                                       skip=report.get('unique_passages') or ())
                except Exception as exc:             # never fail a check because of this
                    log.warning("paraphrase search failed: %s: %s", type(exc).__name__, str(exc)[:200])
                    found = None
                if found is not None:
                    report['paraphrased'] = found
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
    by_source, groups = {}, {lang: {} for lang in LANGUAGES}
    for m in rows:
        by_source[category(m)] = by_source.get(category(m), 0) + 1
        if m['language'] in groups:
            g = corpus.collection_group(m)
            groups[m['language']][g] = groups[m['language']].get(g, 0) + 1
    allowed = ({f'wikipedia-{lang}' for lang in LANGUAGES} | {f'zenodo-{lang}' for lang in LANGUAGES} | set(LANGUAGES)
               | {'cyberleninka', 'uz-journals', 'disk', 'uz-from-ru'})
    status = {name: {k: v for k, v in info.items() if k in (
                  'finished', 'imported', 'updated', 'failed', 'staged_total', 'failed_total', 'listing_complete',
                  'dump_complete', 'estimated_cost_usd', 'error', 'note', 'language_corrected', 'staged', 'skipped',
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
    cross = {"enabled": crosslang.enabled(), **CROSSLANG_STATE}
    if crosslang.enabled():
        try:
            with closing(connect()) as db:
                cross.update(crosslang.index_status(db))
        except Exception as exc:
            cross["error"] = type(exc).__name__
    return {'language_counts': counts, 'by_source': by_source, 'collection_counts': groups, 'total': len(rows), 'corpus_version': version, 'disk': disk, 'crosslang': cross,
            'auto_import': import_job.settings()[0], 'auto_import_wikipedia': wikipedia_import.settings()[0],
            'imports': status}


STATUS_FIELDS = ('started', 'finished', 'last_success', 'error_at', 'imported', 'updated', 'failed', 'staged_total',
                 'failed_total', 'listing_complete', 'dump_complete', 'estimated_cost_usd', 'error', 'note',
                 'language_corrected', 'relabelled', 'staged', 'skipped', 'journals', 'downloaded', 'surveyed',
                 'approved', 'failure_reasons')


@app.get('/v1/inventory', dependencies=[Depends(authorize)])
def inventory_stats():
    """Owner statistics for /admin: counts by language, collection, provider and rights basis, freshness,
    searchable full-text coverage, limits, import history with reasons, and current disk state.
    No texts, no permission records."""
    with closing(connect()) as db:
        data = corpus.stats(db)
    data['limits'] = {'corpus_max_sources': corpus.max_sources(), 'archive_max_sources': corpus.max_deposits(),
                      'wikipedia_max_articles': wikipedia_import.max_articles(), 'wikipedia_max_share': wikipedia_import.max_share()}
    data['imports'] = {str(name)[:40]: {k: v for k, v in info.items() if k in STATUS_FIELDS}
                       for name, info in import_job.read_status().items() if isinstance(info, dict)}
    data['disk_low_now'] = staging.low_disk()
    data['auto_import'] = import_job.settings()[0]
    data['auto_import_wikipedia'] = wikipedia_import.settings()[0]
    return data


class Diagnostic(BaseModel):
    doi: str = Field(default='', max_length=300)
    title: str = Field(default='', max_length=500)
    passage: str = Field(default='', max_length=5000)


@app.post('/v1/diagnostic', dependencies=[Depends(authorize)])
def diagnostic(item: Diagnostic):
    """Owner check of one known source: is it indexed (by DOI or title), are its fingerprints present,
    and is a passage from it found by a search? Returns facts only, never source text."""
    if not item.doi.strip() and len(item.title.strip()) < 8:
        raise HTTPException(422, 'Give a DOI or at least 8 characters of the title')
    if not lock.acquire(timeout=queue_seconds()):
        raise HTTPException(429, 'Engine busy')
    try:
        with closing(connect()) as db:
            matches = corpus.find_source(db, item.doi, item.title)
            results = [corpus.diagnose(db, num, item.passage) for num, _ in matches[:3]]
    finally:
        lock.release()
    return {'matches': len(matches), 'results': results}


# ------------------------------------------------------------- accuracy on the live library (owner, /admin)

EVALUATION = {"state": "never", "done": 0, "total": 0, "started": None, "finished": None, "error": ""}


def evaluation_path():
    return Path(os.environ.get("CORPUS_DB", "corpus.sqlite3")).resolve().parent / "accuracy-results.md"


def _evaluate(per_language):
    """evaluation/real_eval.py on the live library, read-only: real library passages copied into set-aside library
    texts in the ways students copy, checked as a real check is. Each search takes the engine's search lock, so
    real checks are never held up by more than one test search. The result is kept on the volume."""
    import importlib
    try:
        real_eval = importlib.import_module("evaluation.real_eval")

        def locked_search(*args, **kwargs):
            with lock:
                return corpus.search(*args, **kwargs)
        real_eval.SEARCH = locked_search
        started, seed = time.time(), int(time.time())
        results, cases, whole, size, held = real_eval.run(
            os.environ.get("CORPUS_DB", "corpus.sqlite3"), None, per_language, seed,
            progress=lambda done, total: EVALUATION.update(done=done, total=total))
        text = real_eval.markdown(results, cases, whole, size, held, seed, time.time() - started)
        text = text.replace("Generated by `python evaluation/real_eval.py`",
                            "Run from /admin on the live library on " + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())
                            + " with `evaluation/real_eval.py`", 1)
        evaluation_path().write_text(text, "utf-8")
        EVALUATION.update(state="done", finished=time.time(), error="")
    except Exception as exc:
        log.warning("accuracy evaluation failed: %s: %s", type(exc).__name__, str(exc)[:300])
        EVALUATION.update(state="error", finished=time.time(), error=f"{type(exc).__name__}: {str(exc)[:200]}")


class Evaluation(BaseModel):
    per_language: int = Field(30, ge=5, le=60)


@app.post('/v1/evaluation', dependencies=[Depends(authorize)])
def evaluation_start(item: Evaluation):
    """Owner-only: start the accuracy test in the background (one at a time)."""
    if EVALUATION["state"] == "running":
        raise HTTPException(409, 'An accuracy test is already running')
    EVALUATION.update(state="running", done=0, total=0, started=time.time(), finished=None, error="")
    threading.Thread(target=_evaluate, args=(item.per_language,), daemon=True).start()
    return evaluation_status()


@app.get('/v1/evaluation', dependencies=[Depends(authorize)])
def evaluation_status():
    """The test's progress and the last result (Markdown, kept on the volume)."""
    path = evaluation_path()
    return {**EVALUATION, "result": path.read_text("utf-8")[:200_000] if path.exists() else ""}


class Compact(BaseModel):
    confirm: bool = False


def compact_plan(db_path):
    """Sizes for compacting: the file, the space unused inside it (left by removed texts), the free disk space, and
    whether a compacted copy fits (it needs the live data plus a margin)."""
    db_bytes = os.path.getsize(db_path) if os.path.exists(db_path) else 0
    wal = db_path + '-wal'
    wal_bytes = os.path.getsize(wal) if os.path.exists(wal) else 0
    with closing(sqlite3.connect(db_path, timeout=60)) as db:
        page = db.execute('PRAGMA page_size').fetchone()[0]
        unused = db.execute('PRAGMA freelist_count').fetchone()[0] * page
    free = shutil.disk_usage(os.path.dirname(os.path.abspath(db_path))).free
    needed = max(0, db_bytes - unused) + 64 * 1024 * 1024
    return {'db_bytes': db_bytes, 'wal_bytes': wal_bytes, 'unused_bytes': unused, 'free_bytes': free,
            'needed_bytes': needed, 'possible': free >= needed}


@app.post('/v1/compact', dependencies=[Depends(authorize)])
def compact(item: Compact):
    """Owner-only: give the space left by removed texts back to the disk. Without confirm it only reports the sizes.
    With confirm, a compacted copy is written next to the library (VACUUM INTO), checked (integrity and the number of
    texts), and only then replaces the library file. Checks and imports wait meanwhile (the engine lock)."""
    db_path = os.environ.get('CORPUS_DB', 'corpus.sqlite3')
    if not os.path.exists(db_path):
        raise HTTPException(404, 'No library file')
    plan = compact_plan(db_path)
    if not item.confirm or not plan['possible']:
        return {**plan, 'compacted': False}
    if not lock.acquire(timeout=queue_seconds()):
        raise HTTPException(429, 'Engine busy')
    started = time.monotonic()
    tmp = db_path + '.compact'
    try:
        if os.path.exists(tmp):
            os.unlink(tmp)
        with closing(sqlite3.connect(db_path, timeout=60)) as db:
            db.execute('PRAGMA wal_checkpoint(TRUNCATE)')
            before = db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
            db.execute('VACUUM INTO ?', (tmp,))
        with closing(sqlite3.connect(tmp)) as new:
            ok = new.execute('PRAGMA quick_check').fetchone()[0] == 'ok'
            after = new.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
        if not ok or after != before:
            os.unlink(tmp)
            log.error('compact: copy failed its check (quick_check ok=%s, texts %s -> %s); library unchanged', ok, before, after)
            raise HTTPException(500, 'The compacted copy failed its check; the library was not changed.')
        os.replace(tmp, db_path)                      # atomic on the same volume
        for extra in (db_path + '-wal', db_path + '-shm'):
            if os.path.exists(extra):
                os.unlink(extra)
        with closing(connect()) as db:               # WAL mode and schema on the new file
            db.execute('SELECT COUNT(*) FROM sources').fetchone()
    except sqlite3.Error as exc:
        if os.path.exists(tmp):
            os.unlink(tmp)
        log.error('compact failed: %s; library unchanged', exc)
        raise HTTPException(500, f'Compacting failed ({type(exc).__name__}); the library was not changed.')
    finally:
        lock.release()
    new_bytes = os.path.getsize(db_path)
    log.warning('compacted library: %s -> %s bytes, %s texts, %.1fs', plan['db_bytes'], new_bytes, before, time.monotonic() - started)
    return {**plan, 'compacted': True, 'db_bytes_after': new_bytes, 'texts': before,
            'seconds': round(time.monotonic() - started, 1), 'free_bytes_after': shutil.disk_usage(
                os.path.dirname(os.path.abspath(db_path))).free}


class Prune(BaseModel):
    collection: str = Field(default='wikipedia', max_length=40)
    keep_per_language: int = Field(ge=0, le=100000)
    confirm: bool = False


@app.post('/v1/prune', dependencies=[Depends(authorize)])
def prune(item: Prune):
    """Owner-only removal of general-reference texts (Wikipedia), keeping the longest N per language.
    Without confirm it only reports what would be removed. With confirm, the list of removed articles
    (title, address, language, words) is written to the staging folder first, then they are deleted."""
    if not lock.acquire(timeout=queue_seconds()):
        raise HTTPException(429, 'Engine busy')
    try:
        with closing(connect()) as db:
            try:
                plan = corpus.prune_collection(db, item.collection, item.keep_per_language, dry_run=True)
            except ValueError as exc:
                raise HTTPException(422, str(exc))
            record = None
            if item.confirm and plan['removed']:
                stamp = time.strftime('%Y%m%d-%H%M%S', time.gmtime())
                root = import_job.staging_root()
                root.mkdir(parents=True, exist_ok=True)
                record = root / f'pruned-{item.collection}-{stamp}.json'
                record.write_text(json.dumps(plan['removed'], ensure_ascii=False), 'utf-8')
                plan = corpus.prune_collection(db, item.collection, item.keep_per_language, dry_run=False)
                log.warning('pruned %s: removed %s, kept %s; list in %s', item.collection, plan['remove'], plan['keep'], record)
            total = db.execute('SELECT COUNT(*) FROM sources').fetchone()[0]
            left = corpus.count_category(db, item.collection)
    finally:
        lock.release()
    return {'remove': plan['remove'], 'keep': plan['keep'], 'confirmed': bool(item.confirm and record),
            'record': record.name if record else None, 'total_after': total if item.confirm else total - sum(plan['remove'].values()),
            'collection_after': left if item.confirm else sum(plan['keep'].values())}


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
