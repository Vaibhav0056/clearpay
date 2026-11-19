from pathlib import Path

import pytest
from streamlit.testing.v1 import AppTest

from invoiceiq.workflow import process_pdf

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("filename,expected", [
    ("01_happy_path.pdf", "Approved"),
    ("05_missing_po.pdf", "Needs review"),
    ("06_unapproved_vendor.pdf", "Blocked"),
])
def test_readable_results_and_history(repo, monkeypatch, filename, expected):
    monkeypatch.setenv("INVOICEIQ_DB", repo.path)
    path = ROOT / "fixtures/invoices" / filename
    result = process_pdf(path.read_bytes(), filename, repo, mode="fixture", graph_enabled=False)
    app = AppTest.from_file(str(ROOT / "app.py"))
    app.session_state["last_result"] = result
    app.run(timeout=30)
    assert not app.exception
    assert any(item.value == f"Decision: {expected}" for item in app.subheader)
    assert any("Invoice details" in item.value for item in app.markdown)
    line_tables = [table.value for table in app.dataframe if "Description" in table.value.columns]
    assert line_tables and line_tables[0].iloc[0]["Quantity"] == format(result["invoice"].line_items[0].quantity, "f")
    raw_sections = [item for item in app.expander if "raw JSON" in item.label]
    assert raw_sections and all(not item.proto.expanded for item in raw_sections)
    if expected == "Approved":
        assert any("configured finance controls passed" in item.value for item in app.success)
    elif expected == "Needs review":
        assert app.warning
    else:
        assert app.error
