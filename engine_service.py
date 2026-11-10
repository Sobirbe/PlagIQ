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
from corpus import connect, search
from security import SecurityMiddleware

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
    try:
        yield
    finally:
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
    if len(key) < 32 or not secrets.compare_digest(authorization, 'Bearer ' + key):
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
        with connect() as db:
            report = search(db, query.query_text, query.language)
        identifier = uuid.uuid4().hex
        reports[identifier] = (time.monotonic() + 180, report)
        return {'status_url': f'/v1/checks/{identifier}', 'report_url': f'/v1/checks/{identifier}/report'}
    finally:
        lock.release()


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
