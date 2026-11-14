"""Institution-approved template text (title pages, approval sheets, required declarations).

A university's admins (INSTITUTIONS, institutions.py) upload the wording their students must use. When a student
chooses that university on the check form, the matched words that also appear in one of these templates are
excluded as "template text", like an excluded quotation: still shown in the report, not counted in the percentage.
Template texts are the institution's own documents: they are never shown to students (only their titles), never
added to the library, and kept until an admin removes them.
"""
import os
import sqlite3
import threading
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path

_lock = threading.Lock()
MAX_TEMPLATES = 20               # per institution
MAX_WORDS = 20_000               # per template
MAX_TITLE = 160


class TemplateError(ValueError):
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


def _path():
    configured = os.environ.get("TEMPLATES_DB")
    if configured:
        return configured
    import auth
    return str(Path(auth._db_path()).with_name("plagiq-templates.sqlite3"))


def _connect():
    db = sqlite3.connect(_path(), timeout=10)
    db.execute("CREATE TABLE IF NOT EXISTS templates (id INTEGER PRIMARY KEY AUTOINCREMENT, institution TEXT NOT NULL, "
               "title TEXT NOT NULL, text TEXT NOT NULL, words INTEGER NOT NULL, created TEXT NOT NULL, added_by INTEGER)")
    db.execute("CREATE INDEX IF NOT EXISTS templates_institution ON templates (institution)")
    return db


def add(institution, title, text, user_id):
    words = len(text.split())
    if words < 7:
        raise TemplateError("template_too_short", "A template needs at least 7 words.")
    if words > MAX_WORDS:
        raise TemplateError("template_too_long", f"A template may have at most {MAX_WORDS:,} words.")
    with _lock, closing(_connect()) as db, db:
        if db.execute("SELECT COUNT(*) FROM templates WHERE institution=?", (institution,)).fetchone()[0] >= MAX_TEMPLATES:
            raise TemplateError("template_limit", f"At most {MAX_TEMPLATES} templates per institution.")
        cursor = db.execute("INSERT INTO templates (institution, title, text, words, created, added_by) VALUES (?, ?, ?, ?, ?, ?)",
                            (institution, " ".join(title.split())[:MAX_TITLE] or "Template", text, words,
                             datetime.now(timezone.utc).isoformat(timespec="seconds"), user_id))
        return cursor.lastrowid


def listing(institution):
    """Titles only (never the text)."""
    with _lock, closing(_connect()) as db:
        rows = db.execute("SELECT id, title, words, created FROM templates WHERE institution=? ORDER BY id",
                          (institution,)).fetchall()
    return [dict(zip(("id", "title", "words", "created"), r)) for r in rows]


def remove(institution, template_id):
    with _lock, closing(_connect()) as db, db:
        return db.execute("DELETE FROM templates WHERE institution=? AND id=?", (institution, template_id)).rowcount > 0


def texts(institution):
    with _lock, closing(_connect()) as db:
        return [t for (t,) in db.execute("SELECT text FROM templates WHERE institution=? ORDER BY id", (institution,))]


def institutions_with_templates(names):
    """[{"name", "templates"}] for configured institutions that have at least one template (for the check form)."""
    if not names:
        return []
    with _lock, closing(_connect()) as db:
        counts = dict(db.execute(f"SELECT institution, COUNT(*) FROM templates WHERE institution IN "
                                 f"({','.join('?' * len(names))}) GROUP BY institution", list(names)).fetchall())
    return [{"name": n, "templates": counts[n]} for n in sorted(names) if counts.get(n)]


def spans(manuscript, institution, language):
    """Character spans of the manuscript that match the institution's templates (exact or edited), merged."""
    import corpus
    sources = texts(institution)
    if not sources:
        return []
    _, evidence, _, intervals = corpus.compare_texts(manuscript, sources, language)
    return [list(i) for i in intervals]
