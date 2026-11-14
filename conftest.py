"""Every test gets its own report store and monitoring figures, so checks never write into the project folder."""
import pytest


@pytest.fixture(autouse=True)
def _private_report_store(tmp_path, monkeypatch):
    monkeypatch.setenv("REPORTS_DB", str(tmp_path / "reports.sqlite3"))
    monkeypatch.setenv("METRICS_DB", str(tmp_path / "metrics.sqlite3"))
