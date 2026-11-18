"""Cross-language reuse: finding text translated from a source in another language (evaluation/CROSSLANG.md).

The engine compares the manuscript's SENTENCES with the sentences of library texts in the OTHER languages, using
LaBSE (Language-agnostic BERT Sentence Embedding, Google, Apache-2.0), which places a sentence and its translation
close together. A library text is reported only when at least MIN_SENTENCES (2) manuscript sentences each match one
of its sentences with similarity >= CUTOFF (0.80): the rule measured in evaluation/CROSSLANG.md (about 95-100 % of
machine translations found, 84-99 % after editing, no false alarms on about 1,700 real abstracts that were not
translations). It is reported separately as "possible translated reuse — review", never added to the percentage.

Runs on the engine only, without PyTorch: the model's ONNX export (onnxruntime), its tokenizer (tokenizers) and the
final 768x768 layer (numpy), the same pipeline as sentence-transformers (CLS token -> Dense + tanh -> normalise).
The model files are downloaded once from Hugging Face at a pinned revision and checked by size and SHA-256.

Settings (engine):
  CROSSLANG=1            turn it on (off by default; measured: about 1.9 GB more memory while running, a peak of
                         about 3.4 GB while the model loads, and 1.9 GB of disk for the model)
  CROSSLANG_DIR          where the model and nothing else is kept (default <volume>/models/labse)
  CROSSLANG_CUTOFF=0.80  sentence similarity cut-off (0.75-0.90)
  CROSSLANG_SHORTLIST=20 library texts compared sentence by sentence per manuscript part (measured: 20 loses nothing)
  CROSSLANG_MAX_SENTENCES=2000  manuscript sentences compared per check (about 25 s on 4 CPU threads)
"""
import hashlib
import json
import logging
import os
import re
import threading
import urllib.request
from pathlib import Path

import numpy as np

log = logging.getLogger("plagiq.crosslang")

REPO = "sentence-transformers/LaBSE"
REVISION = "836121a0533e5664b21c7aacc5d22951f2b8b25b"        # pinned: the files below are checked against it
FILES = {                                                     # name -> (bytes, sha256 or None for small JSON files)
    "onnx/model.onnx": (1881623050, None),
    "tokenizer.json": (9621556, None),
    "2_Dense/model.safetensors": (2362528, None),
}
DIM = 768
MAX_TOKENS = 128
MIN_SENTENCES = 2
BOILERPLATE_SOURCES = 5        # a manuscript sentence matching this many different texts is boilerplate: ignored
_lock = threading.Lock()
_model = None


def enabled():
    return os.environ.get("CROSSLANG", "") == "1"


def _bounded(name, default, low, high, cast=float):
    try:
        return min(max(cast(os.environ.get(name, default)), low), high)
    except ValueError:
        return default


def cutoff():
    return _bounded("CROSSLANG_CUTOFF", 0.80, 0.75, 0.90)


def shortlist_size():
    return int(_bounded("CROSSLANG_SHORTLIST", 20, 5, 100, int))


def max_sentences():
    return int(_bounded("CROSSLANG_MAX_SENTENCES", 2000, 50, 10000, int))


def model_dir():
    configured = os.environ.get("CROSSLANG_DIR")
    if configured:
        return Path(configured)
    db = os.environ.get("CORPUS_DB", "corpus.sqlite3")
    return Path(os.path.dirname(os.path.abspath(db))) / "models" / "labse"


# ----------------------------------------------------------------- model files

def missing_files(folder=None):
    folder = Path(folder or model_dir())
    return [name for name, (size, _) in FILES.items() if not (folder / name).is_file() or (folder / name).stat().st_size != size]


def download(folder=None, opener=urllib.request.urlopen):
    """Fetch the pinned model files that are missing (about 1.9 GB). Each file is written to a temporary name and
    renamed only when complete and of the expected size."""
    folder = Path(folder or model_dir())
    for name in missing_files(folder):
        size = FILES[name][0]
        target = folder / name
        target.parent.mkdir(parents=True, exist_ok=True)
        part = target.with_name(target.name + ".part")
        url = f"https://huggingface.co/{REPO}/resolve/{REVISION}/{name}"
        log.warning("crosslang: downloading %s (%.0f MB)", name, size / 1e6)
        with opener(urllib.request.Request(url, headers={"User-Agent": "PlagIQ-engine"}), timeout=60) as response, \
                open(part, "wb") as out:
            while True:
                chunk = response.read(1 << 20)
                if not chunk:
                    break
                out.write(chunk)
        if part.stat().st_size != size:
            part.unlink()
            raise OSError(f"{name}: incomplete download")
        os.replace(part, target)


class Encoder:
    """LaBSE sentence vectors (unit length, float32) without PyTorch."""

    def __init__(self, folder=None, threads=None):
        import onnxruntime
        from safetensors.numpy import load_file
        from tokenizers import Tokenizer
        folder = Path(folder or model_dir())
        self.tokenizer = Tokenizer.from_file(str(folder / "tokenizer.json"))
        self.tokenizer.enable_truncation(MAX_TOKENS)
        self.tokenizer.enable_padding(pad_id=0, pad_token="[PAD]")
        options = onnxruntime.SessionOptions()
        options.intra_op_num_threads = threads or int(_bounded("CROSSLANG_THREADS", 4, 1, 16, int))
        options.enable_cpu_mem_arena = False                      # measured: 1.9 GB resident instead of 2.1 GB
        options.add_session_config_entry("session.disable_prepacking", "1")
        self.session = onnxruntime.InferenceSession(str(folder / "onnx" / "model.onnx"), options,
                                                    providers=["CPUExecutionProvider"])
        self.inputs = {i.name for i in self.session.get_inputs()}
        dense = load_file(str(folder / "2_Dense" / "model.safetensors"))
        self.weight = dense["linear.weight"].astype(np.float32)         # (out, in)
        self.bias = dense["linear.bias"].astype(np.float32)

    def encode(self, sentences, batch=32):
        out = np.zeros((len(sentences), DIM), dtype=np.float32)
        for start in range(0, len(sentences), batch):
            enc = self.tokenizer.encode_batch(list(sentences[start:start + batch]))
            feed = {"input_ids": np.array([e.ids for e in enc], dtype=np.int64),
                    "attention_mask": np.array([e.attention_mask for e in enc], dtype=np.int64)}
            if "token_type_ids" in self.inputs:
                feed["token_type_ids"] = np.array([e.type_ids for e in enc], dtype=np.int64)
            hidden = self.session.run(None, feed)[0]                     # (batch, tokens, 768)
            cls = hidden[:, 0, :]
            vec = np.tanh(cls @ self.weight.T + self.bias)
            out[start:start + len(enc)] = vec / np.maximum(np.linalg.norm(vec, axis=1, keepdims=True), 1e-12)
        return out


def model():
    """The loaded encoder (loaded once, after the files are present), or None."""
    global _model
    with _lock:
        if _model is None and not missing_files():
            _model = Encoder()
        return _model


# ----------------------------------------------------------------- sentences

SENTENCE_END = re.compile(r"(?<=[.!?…])[\"»”’)\]]*\s+(?=[\"«“(\[]?[A-ZА-ЯЁЎҚҒҲ0-9])")
MIN_WORDS, MAX_WORDS = 6, 80


def sentences(text):
    """[(start, end)] character spans of sentences long enough to judge (6-80 words); longer ones are cut."""
    out = []
    for paragraph in re.finditer(r"[^\n]+", text):
        start = paragraph.start()
        bounds = [m.end() for m in SENTENCE_END.finditer(paragraph.group())] + [len(paragraph.group())]
        for end in bounds:
            s, e = start, paragraph.start() + end
            piece = text[s:e]
            stripped = len(piece) - len(piece.lstrip())
            s += stripped
            e = s + len(text[s:e].rstrip())
            words = text[s:e].split()
            if MIN_WORDS <= len(words) <= MAX_WORDS:
                out.append((s, e))
            elif len(words) > MAX_WORDS:                    # a run-on "sentence" (tables, lists): 40-word pieces
                spans = [m.span() for m in re.finditer(r"\S+", text[s:e])]
                for k in range(0, len(spans), 40):
                    group = spans[k:k + 40]
                    if len(group) >= MIN_WORDS:
                        out.append((s + group[0][0], s + group[-1][1]))
            start = paragraph.start() + end
    return out


def pack(vectors):
    """float16 bytes for the index (rounding changes similarities by about 0.001)."""
    return np.asarray(vectors, dtype=np.float16).tobytes()


def unpack(blob, rows=None):
    vec = np.frombuffer(blob, dtype=np.float16).astype(np.float32)
    return vec.reshape(-1, DIM) if rows is None else vec.reshape(rows, DIM)


def digest(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def status_line(indexed, total):
    return json.dumps({"enabled": enabled(), "model": not missing_files(), "indexed": indexed, "total": total})


# ----------------------------------------------------------------- library index (in the corpus database)

def ensure_tables(db):
    db.execute("CREATE TABLE IF NOT EXISTS crosslang_docs (source INTEGER PRIMARY KEY, language TEXT NOT NULL, "
               "sentences INTEGER NOT NULL, vec BLOB)")
    db.execute("CREATE TABLE IF NOT EXISTS crosslang_sents (source INTEGER NOT NULL, idx INTEGER NOT NULL, start INTEGER "
               "NOT NULL, end INTEGER NOT NULL, vec BLOB NOT NULL, PRIMARY KEY (source, idx))")


def index_status(db):
    ensure_tables(db)
    total = db.execute("SELECT COUNT(*) FROM sources").fetchone()[0]
    indexed = db.execute("SELECT COUNT(*) FROM crosslang_docs WHERE source IN (SELECT num FROM sources)").fetchone()[0]
    return {"enabled": enabled(), "model": not missing_files(), "indexed": indexed, "total": total}


def index_some(db, encoder, limit=5, max_per_text=5000):
    """Embed the sentences of up to `limit` library texts not indexed yet; drop index rows of removed texts.
    Returns the number of texts indexed."""
    ensure_tables(db)
    db.execute("DELETE FROM crosslang_sents WHERE source NOT IN (SELECT num FROM sources)")
    db.execute("DELETE FROM crosslang_docs WHERE source NOT IN (SELECT num FROM sources)")
    rows = db.execute("SELECT num, metadata, text FROM sources WHERE num NOT IN (SELECT source FROM crosslang_docs) "
                      "ORDER BY num LIMIT ?", (limit,)).fetchall()
    for num, raw, text in rows:
        language = json.loads(raw).get("language", "")
        spans = sentences(text)[:max_per_text]
        vectors = encoder.encode([text[s:e] for s, e in spans]) if spans else np.zeros((0, DIM), np.float32)
        db.executemany("INSERT OR REPLACE INTO crosslang_sents (source, idx, start, end, vec) VALUES (?, ?, ?, ?, ?)",
                       [(num, k, s, e, pack(vectors[k])) for k, (s, e) in enumerate(spans)])
        doc = None
        if len(vectors):
            mean = vectors.mean(axis=0)
            doc = pack(mean / max(np.linalg.norm(mean), 1e-12))
        db.execute("INSERT OR REPLACE INTO crosslang_docs (source, language, sentences, vec) VALUES (?, ?, ?, ?)",
                   (num, language, len(spans), doc))
    db.commit()
    return len(rows)


# ----------------------------------------------------------------- search

PART = 40                      # manuscript sentences per part for the shortlist (a few paragraphs)


TEMPLATE = re.compile(r"(?i)адабиётлар|adabiyotlar|список литературы|references|bibliograph|калит сўзлар|kalit so|"
                      r"таянч сўз|tayanch so|ключевые слова|keywords|аннотация|annotatsiya|abstract|удк|udc|issn|doi")


def template_like(sentence):
    """Reference entries, keyword and annotation lines, journal headers and footers: text every paper shares in some
    form. Measured in evaluation/PARAPHRASE.md as the only source of false paraphrase matches."""
    if TEMPLATE.search(sentence):
        return True
    cites = len(re.findall(r"(?:^|\s)\d{1,3}\.\s*[A-ZА-ЯЁЎҚҒҲ][a-zа-яёўқғҳʼ’']+\s+[A-ZА-ЯЁЎҚҒҲ]\.", sentence))
    marks = len(re.findall(r"(?:\b[ТT]\.\s?:|\bС\.\s?\d|\b[бБ]\.\s?\d|\bpp?\.\s?\d|//|№\s?\d|\b(?:19|20)\d\d\b)", sentence))
    return cites >= 1 or marks >= 2


def search(db, text, language, encoder, exclude=(), max_pairs=8, same_language=False, skip=()):
    """Library texts in OTHER languages that at least MIN_SENTENCES manuscript sentences appear to be translated
    from: [{"num", "pairs": [(q_start, q_end, s_start, s_end, similarity)], "sentences": n}], strongest first.
    same_language=True looks for paraphrase instead: library texts in the SAME language, leaving out manuscript
    sentences that overlap a span in `skip` (already found by word matching) and template lines (template_like)."""
    ensure_tables(db)
    spans = sentences(text)[:max_sentences()]
    if same_language:
        spans = [(s, e) for s, e in spans if not template_like(text[s:e])
                 and sum(max(0, min(e, b) - max(s, a)) for a, b in skip) < 0.5 * (e - s)]
    if len(spans) < MIN_SENTENCES:
        return []
    docs = db.execute("SELECT d.source, d.vec FROM crosslang_docs d JOIN sources s ON s.num = d.source "
                      f"WHERE d.language {'=' if same_language else '!='} ? AND d.vec IS NOT NULL", (language,)).fetchall()
    docs = [(num, vec) for num, vec in docs if num not in exclude]
    if not docs:
        return []
    nums = [num for num, _ in docs]
    doc_matrix = np.vstack([unpack(vec, 1) for _, vec in docs])
    vectors = encoder.encode([text[s:e] for s, e in spans])
    limit, k = cutoff(), shortlist_size()
    best = {}                                       # (manuscript sentence, source) -> (similarity, source sentence)
    cache = {}
    for start in range(0, len(spans), PART):
        part = vectors[start:start + PART]
        centre = part.mean(axis=0)
        centre /= max(np.linalg.norm(centre), 1e-12)
        order = np.argsort(-(doc_matrix @ centre))[:k]
        for d in order:
            num = nums[d]
            if num not in cache:
                rows = db.execute("SELECT start, end, vec FROM crosslang_sents WHERE source=? ORDER BY idx", (num,)).fetchall()
                cache[num] = ([(s, e) for s, e, _ in rows], np.vstack([unpack(v, 1) for *_, v in rows]) if rows else None)
            src_spans, src_vectors = cache[num]
            if src_vectors is None:
                continue
            sims = part @ src_vectors.T
            for i in np.nonzero(sims.max(axis=1) >= limit)[0]:
                j = int(sims[i].argmax())
                key = (start + int(i), num)
                if float(sims[i, j]) > best.get(key, (0,))[0]:
                    best[key] = (float(sims[i, j]), j)
    # boilerplate: a manuscript sentence that "matches" many different texts is a formula, not a translation
    holders = {}
    for i, num in best:
        holders.setdefault(i, set()).add(num)
    found = {}
    for (i, num), (sim, j) in best.items():
        if len(holders[i]) >= BOILERPLATE_SOURCES:
            continue
        s_start, s_end = cache[num][0][j]
        found.setdefault(num, []).append((spans[i][0], spans[i][1], s_start, s_end, round(sim, 3)))
    out = []
    for num, pairs in found.items():
        if len({p[0] for p in pairs}) >= MIN_SENTENCES:
            pairs.sort(key=lambda p: p[0])
            out.append({"num": num, "sentences": len(pairs), "pairs": pairs[:max_pairs]})
    return sorted(out, key=lambda x: -x["sentences"])
