"""The OAK journal list from ilmiz.uz as a second source for the Uzbek-journals importer (uz_journals.py)."""
import json

import uz_journals


class FakeIlmiz:
    """ilmiz.uz pages: two list pages, then an empty one; journal pages link the official site."""

    def __init__(self):
        self.calls = []
        journal = '<a href="/jurnal/{0}">x</a>'
        self.pages = {
            "https://ilmiz.uz/jurnallar": "".join(journal.format(s) for s in ("ilim", "fardu", "ilim")),
            "https://ilmiz.uz/jurnallar?sahifa=2": journal.format("socinov") + journal.format("fardu"),
            "https://ilmiz.uz/jurnallar?sahifa=3": "<p>no more</p>",
            "https://ilmiz.uz/jurnal/ilim": '<p>4671 ta maqola</p><p><a href="https://ilim.uz/index.php/ilim/" '
                                             'rel="noopener" target="_blank">Ilim ha&#8217;m ja&#8217;miyet — rasmiy sayti</a></p>',
            "https://ilmiz.uz/jurnal/fardu": '<tr><th>Maqolalar indeksda</th><td>7 471 (2015–2026)</td></tr><p><a href="https://journal.fdu.uz" '
                                              'rel="noopener" target="_blank">FarDU ilmiy xabarlari — rasmiy sayti</a></p>',
            "https://ilmiz.uz/jurnal/socinov": '<p>5991 ta maqola</p><p><a href="https://inscience.uz/index.php/socinov" '
                                                'rel="noopener">Жамият ва инновациялар — rasmiy sayti</a></p>',
        }

    def get(self, url, limit):
        self.calls.append(url)
        return self.pages[url].encode(), "text/html"


def test_ilmiz_list_gives_each_journals_official_site():
    fake = FakeIlmiz()
    journals = uz_journals.ilmiz_journals(fake)
    assert journals == [("Ilim ha’m ja’miyet", "https://ilim.uz/index.php/ilim", 4671),
                        ("FarDU ilmiy xabarlari", "https://journal.fdu.uz", 7471),
                        ("Жамият ва инновациялар", "https://inscience.uz/index.php/socinov", 5991)]
    assert fake.calls.count("https://ilmiz.uz/jurnal/fardu") == 1           # listed twice, fetched once
    assert "https://ilmiz.uz/jurnallar?sahifa=3" in fake.calls               # stops at the first page with nothing new


def test_sources_are_merged_one_entry_per_feed(monkeypatch):
    monkeypatch.setattr(uz_journals, "openalex_journals", lambda fetch=None: [
        ("Society and innovations", "https://inscience.uz/index.php/socinov/about", 6000)])
    merged = uz_journals.all_journals(["openalex", "ilmiz"], fetcher=FakeIlmiz())
    assert [(name, source) for name, _, _, source in merged] == [
        ("Society and innovations", "openalex"), ("Ilim ha’m ja’miyet", "ilmiz"), ("FarDU ilmiy xabarlari", "ilmiz")]
    monkeypatch.setenv("UZ_JOURNALS_SOURCES", "ilmiz, bogus")
    assert uz_journals.journal_sources() == ["ilmiz"]
    monkeypatch.setenv("UZ_JOURNALS_SOURCES", "")
    assert uz_journals.journal_sources() == ["openalex"]
    monkeypatch.delenv("UZ_JOURNALS_SOURCES")
    assert uz_journals.journal_sources() == ["openalex", "ilmiz"]
    assert uz_journals.endpoint_for("https://x.uz/journal/index.php/GED/index") == "https://x.uz/journal/index.php/GED/oai"
    assert uz_journals.endpoint_for("https://x.uz/journal/about") is None


def test_one_source_down_still_surveys_the_other(monkeypatch):
    def down(fetch=None):
        raise OSError("no network")
    monkeypatch.setattr(uz_journals, "openalex_journals", down)
    assert len(uz_journals.all_journals(["openalex", "ilmiz"], fetcher=FakeIlmiz())) == 3


def test_extend_adds_only_new_journals_and_keeps_owner_edits(tmp_path):
    # Non-OJS homepages need no network: they are recorded as no_oai.
    uz_journals.survey(tmp_path, [("Old journal", "https://old.uz/", 10, "openalex")])
    rows = uz_journals._read(tmp_path)
    rows[0]["status"] = "ok"                                                   # the owner's manual edit
    uz_journals._write(tmp_path, rows)
    new = uz_journals.extend(tmp_path, [("Old journal", "https://old.uz/", 10, "openalex"),
                                        ("New journal", "https://new.uz/", 20, "ilmiz")])
    assert [r["name"] for r in new] == ["New journal"]
    rows = uz_journals._read(tmp_path)
    assert [(r["name"], r["status"], r["source"]) for r in rows] == [("Old journal", "ok", "openalex"),
                                                                      ("New journal", "no_oai", "ilmiz")]
    assert uz_journals.extend(tmp_path, [("New journal", "https://new.uz/", 20, "ilmiz")]) == []


def test_engine_run_surveys_a_newly_added_source_once(tmp_path, monkeypatch):
    import import_job
    calls = []
    monkeypatch.setattr(import_job, "write_status", lambda root, name, status: None)
    monkeypatch.setattr(uz_journals, "harvest_all", lambda out, batch, do_import, db_path=None: {"journals": 0})
    monkeypatch.setattr(uz_journals, "all_journals", lambda sources=None: calls.append(list(sources)) or [])
    out = tmp_path / "uz-journals"
    out.mkdir()
    (out / "journals.csv").write_text("name,endpoint,status\nOld,https://x.uz/index.php/j/oai,ok\n", "utf-8")
    monkeypatch.setenv("UZ_JOURNALS_SOURCES", "openalex,ilmiz")
    uz_journals.run(root=tmp_path)                       # an engine surveyed before ilmiz existed: extended once
    assert calls == [["openalex", "ilmiz"]]
    assert json.loads((out / "sources.json").read_text("utf-8")) == ["ilmiz", "openalex"]
    assert [r["name"] for r in uz_journals.approved(out)] == ["Old"]        # existing journal kept
    uz_journals.run(root=tmp_path)                       # nothing new to survey on the next run
    assert calls == [["openalex", "ilmiz"]]
