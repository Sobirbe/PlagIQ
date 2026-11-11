"""University contributions to the protected archive (theses, diploma and course works).

A university signs an agreement with the PlagIQ owner and then deposits its past works.
They are indexed as `institution_deposit`: matched like any source, but their text is
never shown to other users (title, author, year and the matching passage in the checked
document only), exactly like personal archive deposits.

Configuration (website service), JSON list:
  INSTITUTIONS=[{"name": "TDIU", "agreement": "No. 12/2026, 01.11.2026", "admins": [123456789]}]
    name       short name shown in reports ("University deposit: TDIU")
    agreement  agreement number and date, stored with every work as the permission reference
    admins     Telegram user IDs allowed to deposit and remove works for this institution

Two ways to deposit:
  * Web: signed-in admins see "University deposit" and upload files (one request per file).
    A file named "Author - Title.docx" gives author and title; otherwise the file name is the title.
  * Bulk (engine shell), for whole archives:
      python institutions.py --institution TDIU --agreement "No. 12/2026" --csv works.csv --folder ./files [--check]
    works.csv columns: file,title,authors,year[,language]   (language detected when empty)
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sys
from contextlib import closing
from pathlib import Path

MAX_NAME = 80


def configured():
    """{name: {"agreement": str, "admins": set of int}} from INSTITUTIONS; invalid entries ignored."""
    try:
        data = json.loads(os.environ.get("INSTITUTIONS", "") or "[]")
    except ValueError:
        return {}
    out = {}
    for item in data if isinstance(data, list) else []:
        if not isinstance(item, dict):
            continue
        name = " ".join(str(item.get("name", "")).split())[:MAX_NAME]
        agreement = " ".join(str(item.get("agreement", "")).split())[:200]
        admins = {a for a in item.get("admins", []) if type(a) is int and a > 0} if isinstance(item.get("admins"), list) else set()
        if name and agreement:
            out[name] = {"agreement": agreement, "admins": admins}
    return out


def owner_id(name):
    return "org:" + hashlib.sha256(b"plagiq-org|" + name.encode()).hexdigest()[:24]


def for_user(user):
    """Institutions the signed-in user may deposit for."""
    if not user:
        return []
    return [{"name": n, "agreement": c["agreement"]} for n, c in configured().items() if user["id"] in c["admins"]]


def allowed(user, name):
    config = configured().get(name)
    return bool(user and config and user["id"] in config["admins"])


def from_filename(filename):
    """("Author", "Title") from "Author - Title.ext"; ("", "name") otherwise."""
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", Path(filename or "").name).replace("_", " ").strip()
    stem = " ".join(stem.split())
    if " - " in stem:
        author, title = stem.split(" - ", 1)
        if author.strip() and title.strip():
            return author.strip()[:300], title.strip()[:300]
    return "", stem[:300] or "Document"


def record(name, agreement, title, authors, year, language):
    return {"title": title, "authors": authors or "—", "year": year, "language": language, "url": "",
            "rights_basis": "institution_deposit", "permission_reference": f"{name}: agreement {agreement}",
            "owner": owner_id(name), "collection": name}


# ----------------------------------------------------------------- bulk import (engine shell)

def build_manifest(csv_path, folder, name, agreement):
    """Rows of works.csv to manifest records (text_file relative to folder); language detected when empty."""
    import corpus
    from extract import extract_text
    rows = []
    with open(csv_path, newline="", encoding="utf-8-sig") as handle:
        for number, row in enumerate(csv.DictReader(handle), 2):
            file = (row.get("file") or "").strip()
            path = (Path(folder) / file).resolve()
            if not file or Path(folder).resolve() not in path.parents or not path.is_file():
                raise ValueError(f"line {number}: file not found in the folder: {file!r}")
            try:
                year = int((row.get("year") or "").strip())
            except ValueError:
                raise ValueError(f"line {number}: year must be a number") from None
            language = (row.get("language") or "").strip().lower()
            if not language:
                language = corpus.detect_language(extract_text(path.name, path.read_bytes(), max_pages=5000)) or ""
                if not language:
                    raise ValueError(f"line {number}: language could not be detected; add a language column (uz, ru or en)")
            title = (row.get("title") or "").strip() or from_filename(file)[1]
            rows.append({**record(name, agreement, title, (row.get("authors") or "").strip(), year, language),
                         "text_file": file})
    return rows


def main(argv=None):
    parser = argparse.ArgumentParser(description="Bulk deposit of a university's works into the protected archive.")
    parser.add_argument("--institution", required=True)
    parser.add_argument("--agreement", required=True, help="agreement number and date with this institution")
    parser.add_argument("--csv", required=True, type=Path, help="columns: file,title,authors,year[,language]")
    parser.add_argument("--folder", required=True, type=Path)
    parser.add_argument("--db", default=os.environ.get("CORPUS_DB", "corpus.sqlite3"))
    parser.add_argument("--check", action="store_true", help="validate everything; write nothing")
    args = parser.parse_args(argv)
    import corpus
    rows = build_manifest(args.csv, args.folder, args.institution, args.agreement)
    manifest = args.folder / ".plagiq-institution-manifest.json"
    manifest.write_text(json.dumps(rows, ensure_ascii=False), "utf-8")
    try:
        if args.check:
            records = corpus.load_manifest(manifest)
            print(f"{len(records)} work(s) valid for {args.institution}. Nothing was written.")
        else:
            with closing(corpus.connect(args.db)) as db:
                records = corpus.import_manifest(db, manifest)
            print(f"Deposited {len(records)} work(s) for {args.institution} (agreement {args.agreement}).")
    finally:
        manifest.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
