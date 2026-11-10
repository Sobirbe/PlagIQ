from app import compare, tokens

def test_uzbek_latin_cyrillic():
    assert tokens("Олий таълим муассасаларида сифат муҳим", "uz") == tokens("Oliy ta'lim muassasalarida sifat muhim", "uz")

def test_exact_seven_token_match():
    result = compare("one two three four five six seven eight nine", "zero one two three four five six seven extra", "en")
    assert result["matched_tokens"] == 7

def test_short_text_returns_zero():
    assert compare("hello", "hello", "en")["percent"] == 0

def test_russian_cyrillic_maintained():
    assert tokens("Школа", "ru") == ["школа"]

from app import compare_sources
from discovery import reconstruct_abstract, suggest_queries
from fastapi.testclient import TestClient
from app import app
import io

def test_openalex_inverted_abstract():
    assert reconstruct_abstract({"Hello":[0],"world":[1]}) == "Hello world"

def test_academic_verified_only_from_abstract():
    sources=[
        {"title":"Article A","url":"https://example.org/a","year":2025,
         "coverage":"abstract","abstract":"one two three four five six seven alpha"},
        {"title":"Article B","url":"https://example.org/b","year":2024,
         "coverage":"metadata_only","abstract":""}]
    result=compare_sources("one two three four five six seven eight", sources, "en")
    assert result["matched_tokens"]==7
    assert result["sources"][1]["matched_tokens"]==0

def test_academic_requires_explicit_consent():
    client=TestClient(app)
    data=io.BytesIO(b"one two three four five six seven eight")
    response=client.post("/api/check",data={"language":"en","mode":"academic"},
                         files={"submission":("sample.txt",data,"text/plain")})
    assert response.status_code==400
    assert "consent" in response.json()["detail"].lower()

def test_user_reference_endpoint():
    client=TestClient(app)
    text=b"one two three four five six seven eight nine"
    response=client.post("/api/check",
       data={"language":"en","mode":"reference","reference_text":text.decode()},
       files={"submission":("sample.txt",io.BytesIO(text),"text/plain")})
    assert response.status_code==200
    result=response.json()
    assert result["percent"]==100.0
    assert result["self_comparison"] is True
    pdf=client.get("/api/report/"+result["report_id"]+".pdf")
    assert pdf.status_code==200 and pdf.content[:4]==b"%PDF"

from textfold import fold_with_map, to_original_span
from engine_client import highlight_html, engine_base_url, EngineError

def test_fold_keeps_original_offsets():
    normalized, starts, ends = fold_with_map("Ўзбекистон")
    assert normalized
    left, right = to_original_span(0, 1, starts, ends)
    assert "Ўзбекистон"[left:right] == "Ў"

def test_markup_escapes_user_text():
    text = "<script>alert(1)</script> plain"
    output = highlight_html(text, [[0,7]])
    assert "<script>" not in output
    assert "&lt;" in output
    assert "<mark>" in output

def test_corpus_mode_unconfigured(monkeypatch):
    monkeypatch.delenv("ENGINE_URL",raising=False)
    client=TestClient(app)
    content=io.BytesIO(b"one two three four five six seven eight nine")
    response=client.post("/api/check",data={"mode":"corpus","language":"en","consent":"true"},
                         files={"submission":("sample.txt",content,"text/plain")})
    assert response.status_code == 503
