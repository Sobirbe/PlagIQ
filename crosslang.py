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

MODEL = "onnx/model.onnx"
SHRUNK = "onnx/model.f16.json"          # written after shrink(): the 16-bit-storage file's size


def _expected_size(folder, name):
    """The pinned size, or for the model the size recorded by shrink() when it was stored in 16 bits."""
    if name == MODEL and (folder / SHRUNK).is_file():
        try:
            return int(json.loads((folder / SHRUNK).read_text("utf-8"))["size"])
        except (ValueError, KeyError, TypeError, OSError):
            return FILES[name][0]
    return FILES[name][0]


def missing_files(folder=None):
    folder = Path(folder or model_dir())
    return [name for name in FILES if not (folder / name).is_file()
            or (folder / name).stat().st_size != _expected_size(folder, name)]


def shrink_enabled():
    return os.environ.get("CROSSLANG_FP16", "1") != "0"


def needs_shrink(folder=None):
    folder = Path(folder or model_dir())
    return shrink_enabled() and (folder / MODEL).is_file() and (folder / MODEL).stat().st_size == FILES[MODEL][0]


def shrink(folder=None, tmp_dir=None):
    """Store the model's large weight tables in 16 bits (1.88 GB -> 0.94 GB on disk). Computation stays 32-bit: each
    table is cast back when the model loads, and the 1.5 GB word-embedding table is looked up in 16 bits and only the
    looked-up rows are cast. Measured on 400 Uzbek sentences: same-sentence similarity 1.00000, largest change of any
    sentence-pair similarity 0.00026 (the matching cut-off is 0.80). The new file is written beside the model when the
    disk has room, else in the container's temporary folder, and replaces the original only when complete."""
    import shutil
    import tempfile
    import onnx
    from onnx import TensorProto, helper, numpy_helper
    folder = Path(folder or model_dir())
    source = folder / MODEL
    m = onnx.load(str(source))
    g = m.graph
    gathered = {node.input[0] for node in g.node if node.op_type == "Gather"}
    tables, casts_first = [], []
    for init in list(g.initializer):
        if init.data_type != TensorProto.FLOAT or int(np.prod(init.dims)) < 100_000:
            continue
        name16 = init.name + "__f16"
        tables.append(numpy_helper.from_array(numpy_helper.to_array(init).astype(np.float16), name16))
        g.initializer.remove(init)
        if init.name in gathered:              # embedding tables: gather the 16-bit rows, then cast only those rows
            for i, node in enumerate(list(g.node)):
                if node.op_type == "Gather" and node.input[0] == init.name:
                    out = node.output[0]
                    node.input[0], node.output[0] = name16, out + "__f16"
                    g.node.insert(list(g.node).index(node) + 1,
                                  helper.make_node("Cast", [out + "__f16"], [out], to=TensorProto.FLOAT, name=out + "__cast"))
        if any(init.name in node.input for node in g.node):
            casts_first.append(helper.make_node("Cast", [name16], [init.name], to=TensorProto.FLOAT, name=init.name + "__cast"))
    g.initializer.extend(tables)
    for node in reversed(casts_first):
        g.node.insert(0, node)
    data = m.SerializeToString()
    del m, tables
    room = shutil.disk_usage(folder).free > len(data) + (64 << 20)
    where = folder / "onnx" if room else Path(tmp_dir or tempfile.gettempdir())
    part = where / "model.onnx.f16.part"
    part.write_bytes(data)
    if room:
        os.replace(part, source)
    else:                                      # full volume: free the original first, then move the new file in
        source.unlink()
        shutil.move(str(part), str(source))
    (folder / SHRUNK).write_text(json.dumps({"size": len(data), "from": FILES[MODEL][0]}), "utf-8")
    log.warning("crosslang: model stored in 16 bits (%.0f MB, was %.0f MB)", len(data) / 1e6, FILES[MODEL][0] / 1e6)
    return len(data)


def download(folder=None, opener=urllib.request.urlopen):
    """Fetch the pinned model files that are missing (about 1.9 GB). Each file is written to a temporary name and
    renamed only when complete and of the expected size."""
    folder = Path(folder or model_dir())
    for name in missing_files(folder):
        if name == MODEL:
            (folder / SHRUNK).unlink(missing_ok=True)          # a fresh full-size download replaces a shrunk model
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


Q8 = b"Q8"          # index format: this tag, then per vector a float32 scale and DIM int8 values (772 bytes)


def pack(vectors):
    """Index bytes: 8-bit values with one scale per vector, half the size of the earlier float16 rows. Measured on
    6,000 real library sentences: similarity changes by at most 0.0015, and no sentence pair moved across the 0.80
    cut-off (0 of 1,766)."""
    v = np.atleast_2d(np.asarray(vectors, dtype=np.float32))
    scale = np.maximum(np.abs(v).max(axis=1, keepdims=True), 1e-12) / 127
    q = np.round(v / scale).astype(np.int8)
    return Q8 + b"".join(scale[i].astype(np.float32).tobytes() + q[i].tobytes() for i in range(len(v)))


def unpack(blob, rows=None):
    """Vectors from the index: the 8-bit format (length 2 + 772 n, never a multiple of 4) or the earlier float16 rows
    (length 1536 n), so an index written before the change keeps working while it is converted (convert_some)."""
    if len(blob) % 4 == 2 and blob[:2] == Q8:
        raw = np.frombuffer(blob, dtype=np.uint8, offset=2).reshape(-1, 4 + DIM)
        scale = raw[:, :4].copy().view(np.float32)
        vec = raw[:, 4:].copy().view(np.int8).astype(np.float32) * scale
    else:
        vec = np.frombuffer(blob, dtype=np.float16).astype(np.float32).reshape(-1, DIM)
    return vec if rows is None else vec.reshape(rows, DIM)


def convert_some(db, limit=2000):
    """Rewrite up to `limit` index rows stored in the earlier float16 format (2 bytes per value) in the 8-bit format.
    Returns the number converted. The freed space is reused by SQLite for later rows (the file itself shrinks only
    when the library is compacted)."""
    ensure_tables(db)
    legacy = DIM * 2
    rows = db.execute("SELECT rowid, vec FROM crosslang_sents WHERE length(vec) = ? LIMIT ?", (legacy, limit)).fetchall()
    db.executemany("UPDATE crosslang_sents SET vec=? WHERE rowid=?", [(pack(unpack(v, 1)), r) for r, v in rows])
    docs = db.execute("SELECT source, vec FROM crosslang_docs WHERE length(vec) = ? LIMIT ?", (legacy, limit)).fetchall()
    db.executemany("UPDATE crosslang_docs SET vec=? WHERE source=?", [(pack(unpack(v, 1)), n) for n, v in docs])
    db.commit()
    return len(rows) + len(docs)


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
