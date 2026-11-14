"""Zenodo works found through OpenAlex (Zenodo's robots.txt disallows its search API), downloaded from
Zenodo file links, licence rules unchanged."""
from corpus import category, connect, inventory
import import_job
from test_database_access import local_fetcher, openalex  # noqa: F401  (fixture: simulated OpenAlex + PDFs)


def test_zenodo_uz_lists_works_with_a_copy_on_zenodo(openalex, tmp_path, monkeypatch):    # noqa: F811
    base, seen = openalex
    monkeypatch.delenv("UNPAYWALL_EMAIL", raising=False)
    status = import_job.run_zenodo("uz", batch=10, db_path=tmp_path / "c.db", root=tmp_path / "s",
                                   fetcher=local_fetcher(), api=base + "/oa")
    listing = next(f for f in seen if f.startswith("best_oa_location"))
    assert "locations.source.id:S4306400562" in listing and "language:uz" in listing
    assert "country_code" not in listing                                    # Uzbek: from any country
    assert import_job.read_status(tmp_path / "s")["zenodo-uz"]["last_success"] and status["error"] is None
    review = (tmp_path / "s" / "openalex-zenodo-uz" / "review.csv").read_text("utf-8-sig")
    assert "cc-by-nc" in review                                              # NC still goes to review, never imported


def test_zenodo_ru_limited_to_uzbek_institutions(openalex, tmp_path, monkeypatch):    # noqa: F811
    base, seen = openalex
    monkeypatch.delenv("ZENODO_RU_ANY_COUNTRY", raising=False)
    import_job.run_zenodo("ru", batch=5, db_path=tmp_path / "c.db", root=tmp_path / "s", fetcher=local_fetcher(), api=base + "/oa")
    listing = next(f for f in seen if f.startswith("best_oa_location"))
    assert "language:ru" in listing and "authorships.institutions.country_code:UZ" in listing
    with connect(tmp_path / "c.db") as db:
        assert inventory(db)                                                # imported (texts are detected per file)


def test_zenodo_settings_and_category(monkeypatch):
    monkeypatch.setenv("AUTO_IMPORT_ZENODO", "uz, ru,xx")
    monkeypatch.setenv("ZENODO_BATCH", "50000")
    assert import_job.zenodo_settings() == (["uz", "ru"], 2000, 0.05)
    monkeypatch.delenv("AUTO_IMPORT_ZENODO")
    assert import_job.zenodo_settings()[0] == []
    assert category({"url": "https://doi.org/10.5281/zenodo.8019652"}) == "zenodo"
    assert category({"url": "https://zenodo.org/records/8019653"}) == "zenodo"
