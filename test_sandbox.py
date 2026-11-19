"""Files are read in a separate, limited process: same results as before, and a file that exhausts memory, hangs or
crashes the reader costs only that helper process, never the website."""
import io
import json
import os
import sys
import textwrap

import pymupdf
import pytest
from docx import Document

import extract
import sandbox

TEXT = "Remittance inflows have reshaped rural household budgets across the Fergana valley. " * 20


def pdf(text=TEXT, title="A study of remittances"):
    doc = pymupdf.open()
    doc.new_page().insert_textbox(pymupdf.Rect(40, 40, 560, 800), text, fontsize=10)
    doc.set_metadata({"title": title})
    return doc.tobytes()


def scanned():
    out = pymupdf.open()
    src = pymupdf.open(); page = src.new_page()
    page.insert_textbox(pymupdf.Rect(60, 60, 535, 790), TEXT, fontsize=12)
    img = out.new_page(width=page.rect.width, height=page.rect.height)
    img.insert_image(img.rect, stream=page.get_pixmap(dpi=200).tobytes("png"))
    return out.tobytes()


def fake_worker(tmp_path, monkeypatch, body):
    path = tmp_path / "worker.py"
    path.write_text(textwrap.dedent(body))
    monkeypatch.setattr(sandbox, "WORKER", str(path))


def test_same_text_as_reading_in_process():
    d = Document(); d.add_paragraph(TEXT); b = io.BytesIO(); d.save(b)
    for name, data in (("a.txt", TEXT.encode()), ("a.pdf", pdf()), ("a.docx", b.getvalue())):
        info_in, info_out = {}, {}
        assert sandbox.extract_text(name, data, info=info_out) == extract.extract_text(name, data, info=info_in), name
        assert info_out == info_in
    with pytest.raises(extract.ExtractError) as exc:
        sandbox.extract_text("a.exe", b"MZ")
    assert exc.value.code == "unsupported_type"


def test_memory_exhaustion_is_contained(monkeypatch):
    if not extract.ocr_available():
        pytest.skip("Tesseract is not installed")
    monkeypatch.setenv("EXTRACT_MEMORY_MB", "256")              # OCR at 300 dpi needs about 440 MB
    with pytest.raises(extract.ExtractError) as exc:
        sandbox.extract_text("scan.pdf", scanned(), ocr=True)
    assert exc.value.code == "file_too_complex" and exc.value.status == 400
    monkeypatch.delenv("EXTRACT_MEMORY_MB")
    assert "Fergana" in sandbox.extract_text("scan.pdf", scanned(), ocr=True)  # the next file is read normally


def test_hanging_reader_is_killed(tmp_path, monkeypatch):
    fake_worker(tmp_path, monkeypatch, """
        import os, subprocess, sys, time
        child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"])
        open(os.environ.get("TMPDIR", "/tmp") + "/plagiq-test-child", "w").write(str(child.pid))
        time.sleep(60)
    """)
    monkeypatch.setenv("EXTRACT_SECONDS", "5")
    monkeypatch.setenv("TMPDIR", str(tmp_path))
    with pytest.raises(extract.ExtractError) as exc:
        sandbox.extract_text("a.pdf", pdf())
    assert exc.value.code == "file_too_complex"
    child = int((tmp_path / "plagiq-test-child").read_text())
    import time
    for _ in range(50):
        try:
            os.kill(child, 0)
            # killed but not yet collected by its new parent (a zombie, state Z): it no longer runs
            if open(f"/proc/{child}/stat").read().rsplit(")", 1)[1].split()[0] == "Z":
                break
            time.sleep(0.1)
        except (ProcessLookupError, FileNotFoundError):
            break
    else:
        raise AssertionError("the reader's child process was not stopped")


def test_crashing_reader_is_contained(tmp_path, monkeypatch):
    fake_worker(tmp_path, monkeypatch, "import os, signal; os.kill(os.getpid(), signal.SIGSEGV)")
    with pytest.raises(extract.ExtractError) as exc:
        sandbox.extract_text("a.pdf", pdf())
    assert exc.value.code == "file_too_complex"


def test_secrets_are_not_passed_to_the_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("PAYME_TEST_KEY", "secret-value")
    monkeypatch.setenv("ENGINE_API_KEY", "another-secret")
    fake_worker(tmp_path, monkeypatch, """
        import json, os, sys
        sys.stdin.buffer.read()
        print(json.dumps({"ok": True, "text": " ".join(sorted(os.environ)), "info": {}}))
    """)
    names = sandbox.extract_text("a.txt", b"x").split()
    assert "PAYME_TEST_KEY" not in names and "ENGINE_API_KEY" not in names and "PATH" in names


def test_ocr_is_one_at_a_time_across_processes(monkeypatch):
    if not extract.ocr_available():
        pytest.skip("Tesseract is not installed")
    monkeypatch.setenv("OCR_QUEUE_SECONDS", "0")
    assert extract._OCR_SLOT.acquire(timeout=5)                 # this process is reading a scan ...
    try:
        with pytest.raises(extract.ExtractError) as exc:       # ... so the helper process must wait / refuse
            sandbox.extract_text("scan.pdf", scanned(), ocr=True)
        assert exc.value.code == "ocr_busy"
    finally:
        extract._OCR_SLOT.release()


def test_ocr_progress_is_relayed_and_switch_off_works(monkeypatch):
    if not extract.ocr_available():
        pytest.skip("Tesseract is not installed")
    seen = []
    info = {"progress": lambda key, detail=None: seen.append((key, detail))}
    sandbox.extract_text("scan.pdf", scanned(), ocr=True, info=info)
    assert seen == [("ocr", "1/1")] and info["ocr_pages"] == 1
    monkeypatch.setenv("EXTRACT_SANDBOX", "0")
    assert "Fergana" in sandbox.extract_text("a.pdf", pdf())
