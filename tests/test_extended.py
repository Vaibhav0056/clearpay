"""Failure injection uses explicitly mocked adapters; never substitutes production inference."""
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import pytest
import pymupdf
from invoiceiq.workflow import process_pdf, review_and_recheck
from invoiceiq.finance import evaluate_invoice
from invoiceiq.schema import Invoice, money
from invoiceiq.extraction import parse_document

ROOT = Path(__file__).resolve().parents[1]
PDF = ROOT / "fixtures/invoices/01_happy_path.pdf"

@pytest.mark.parametrize("value", ["NaN", "Infinity", True, "1,23,456"])
def test_invalid_money(value):
    with pytest.raises(ValueError): money(value)

def test_negative_tax(repo, invoice):
    invoice.tax_amount = money("-1")
    invoice.invoice_total = invoice.subtotal - 1
    assert "NEGATIVE_TAX" in [f.code for f in evaluate_invoice(repo, invoice, "tax").findings]

def test_tax_policy(repo, invoice, monkeypatch):
    monkeypatch.setenv("ALLOWED_TAX_RATES", "5,18")
    assert "TAX_RATE_MISMATCH" in [f.code for f in evaluate_invoice(repo, invoice, "tax").findings]
    invoice.tax_amount = invoice.subtotal * money("0.18")
    invoice.invoice_total = invoice.subtotal + invoice.tax_amount
    assert evaluate_invoice(repo, invoice, "tax").status == "APPROVED"

def test_concurrent_capacity(repo, invoice_data):
    def approve(n):
        data = dict(invoice_data, invoice_number=f"CAP-{n}", subtotal="65000", invoice_total="65000",
                    line_items=[dict(invoice_data["line_items"][0], quantity="26", line_total="65000")])
        return evaluate_invoice(repo, Invoice.model_validate(data), str(n)).status
    with ThreadPoolExecutor(max_workers=2) as pool:
        assert sorted(pool.map(approve, range(2))) == ["APPROVED", "NEEDS_REVIEW"]

def test_ambiguous_sku(repo, invoice):
    with repo.connect() as c:
        c.execute("INSERT INTO po_lines VALUES('AMB','PO-1007','CHAIR-B24','Mesh chairs','40','2500','100000')")
    assert evaluate_invoice(repo, invoice, "amb").status == "NEEDS_REVIEW"

def test_cache_revalidates_and_survives_restart(repo, monkeypatch):
    from invoiceiq.database import Repository
    first = process_pdf(PDF.read_bytes(), PDF.name, repo, mode="fixture", graph_enabled=True)
    assert first["decision"].status == "APPROVED"
    def forbidden(*a, **kw): raise AssertionError("Extraction should be cached")
    monkeypatch.setattr("invoiceiq.workflow.extract_invoice", forbidden)
    restarted = Repository(repo.path)
    again = process_pdf(PDF.read_bytes(), PDF.name, restarted, mode="fixture", graph_enabled=True)
    assert again["decision"].status == "BLOCKED"
    assert restarted.document(restarted.get_run(first["run_id"])["document_hash"]) == PDF.read_bytes()

def test_model_failure_is_persisted(repo, monkeypatch):
    def failure(*a, **kw): raise TimeoutError("injected model failure")
    monkeypatch.setattr("invoiceiq.extraction.extract_via_vision_api", failure)
    with pytest.raises(TimeoutError):
        process_pdf(PDF.read_bytes(), PDF.name, repo, mode="vision")
    assert repo.runs()[0]["status"] == "ERROR"
    with repo.connect() as c:
        assert c.execute("SELECT count(*) FROM invoices").fetchone()[0] == 0

def scanned_pdf():
    with pymupdf.open(PDF) as original:
        image = original[0].get_pixmap().tobytes("png")
    with pymupdf.open() as doc:
        doc.new_page().insert_image(pymupdf.Rect(0,0,595,842), stream=image)
        return doc.tobytes()

def test_scan_without_ocr_requires_review(repo, monkeypatch):
    monkeypatch.delenv("OCR_ENABLED", raising=False)
    data = scanned_pdf()
    assert parse_document(data).scanned
    result = process_pdf(data, "scan.pdf", repo, mode="fixture")
    assert result["decision"].status != "APPROVED"
    assert "MISSING_FIELD" in [f.code for f in result["decision"].findings]

def test_adaptive_ocr_adapter(monkeypatch):
    # Mock only Tesseract boundary to test routing when the executable is absent.
    calls = []
    def ocr(page, **kwargs):
        calls.append(page.number)
        return page.get_textpage()
    monkeypatch.setattr(pymupdf.Page, "get_textpage_ocr", ocr)
    parse_document(PDF.read_bytes(), enable_ocr=True)
    assert calls == []
    parse_document(scanned_pdf(), enable_ocr=True)
    assert calls == [0]

def test_graph_review_after_restart(repo):
    from invoiceiq.database import Repository
    path = ROOT / "fixtures/invoices/05_missing_po.pdf"
    run = process_pdf(path.read_bytes(), path.name, repo, mode="fixture", graph_enabled=True)
    assert run["decision"].status == "NEEDS_REVIEW"
    corrected = run["invoice"].model_dump(mode="json")
    corrected["po_number"] = "PO-1007"
    result = review_and_recheck(Repository(repo.path), run["run_id"], corrected, "tester")
    assert result["decision"].status == "APPROVED"

def test_api_pdf_and_review(repo, monkeypatch):
    from fastapi.testclient import TestClient
    from invoiceiq.api import app
    monkeypatch.setenv("INVOICEIQ_DB", repo.path)
    client = TestClient(app)
    path = ROOT / "fixtures/invoices/05_missing_po.pdf"
    response = client.post("/process", content=path.read_bytes(), headers={"content-type":"application/pdf"})
    assert response.status_code == 200
    data = response.json()
    assert client.get(f"/runs/{data['run_id']}/pdf").content == path.read_bytes()
    corrected = data["invoice"]
    corrected["po_number"] = "PO-1007"
    response = client.post(f"/runs/{data['run_id']}/review", json=corrected)
    assert response.status_code == 200
    assert response.json()["decision"]["status"] == "APPROVED"


@pytest.mark.parametrize("name,status", [
    ("01_happy_path.pdf", "APPROVED"),
    ("02_partial_po_65k.pdf", "APPROVED"),
    ("03_po_over_capacity.pdf", "APPROVED"),
    ("05_missing_po.pdf", "NEEDS_REVIEW"),
    ("06_unapproved_vendor.pdf", "BLOCKED"),
    ("07_wrong_sku.pdf", "NEEDS_REVIEW"),
    ("08_scanned_different_layout.pdf", "NEEDS_REVIEW"),
    ("09_gst_inclusive_pricing.pdf", "APPROVED"),
])
def test_fixture_decisions(repo, name, status):
    path = ROOT / "fixtures/invoices" / name
    result = process_pdf(path.read_bytes(), name, repo, mode="fixture", graph_enabled=True)
    assert result["decision"].status == status


def test_model_invalid_schema_never_commits(repo, monkeypatch):
    import httpx
    from invoiceiq import extraction
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://inference.invalid/v1")
    monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL", "test-injected-model")
    def respond(request):
        return httpx.Response(200, json={"choices":[{"message":{"content":'{"invoice_total":"NaN"}'}}]})
    original = httpx.Client
    monkeypatch.setattr(extraction.httpx, "Client", lambda **kwargs: original(transport=httpx.MockTransport(respond), **kwargs))
    with pytest.raises(ValueError):
        process_pdf(PDF.read_bytes(), PDF.name, repo, mode="vision")
    assert repo.runs()[0]["status"] == "ERROR"


def test_embedding_cache_adapter(repo, monkeypatch):
    import numpy as np
    from invoiceiq import retrieval
    monkeypatch.setenv("INVOICEIQ_DB", repo.path)
    calls = []
    class FakeEmbedding:
        def encode(self, texts, **kwargs):
            calls.append(texts)
            return np.asarray([[1., 0.] for _ in texts])
    monkeypatch.setattr(retrieval, "embedding_model", lambda name: FakeEmbedding())
    assert retrieval.encoded(["a", "a", "b"]) == [[1.,0.]] * 3
    retrieval.encoded(["a", "c"])
    assert calls == [["a", "b"], ["c"]]


def test_streamlit_initial_render(monkeypatch, tmp_path):
    from streamlit.testing.v1 import AppTest
    monkeypatch.setenv("INVOICEIQ_DB", str(tmp_path / "ui.sqlite"))
    app = AppTest.from_file(str(ROOT / "app.py")).run(timeout=30)
    assert not app.exception
