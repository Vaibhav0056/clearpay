import pytest
from invoiceiq.database import Repository
from invoiceiq.schema import Invoice

@pytest.fixture
def repo(tmp_path):
    r=Repository(str(tmp_path/"test.sqlite"))
    r.seed()
    return r

@pytest.fixture
def invoice_data():
    return dict(invoice_number="INV-1",invoice_date="2026-11-10",vendor_name="Acme Supplies Pvt Ltd",vendor_tax_id="TAX-ACME",po_number="PO-1007",currency="INR",subtotal="25000",tax_amount="0",invoice_total="25000",line_items=[{"description":"Mesh chairs","sku":"CHAIR-B24","quantity":"10","unit_price":"2500","line_total":"25000"}])

@pytest.fixture
def invoice(invoice_data):
    return Invoice.model_validate(invoice_data)


@pytest.fixture(autouse=True)
def isolate_database_backend(monkeypatch):
    # Ordinary tests must never modify a configured remote project.
    monkeypatch.delenv("SUPABASE_DB_URL", raising=False)
    monkeypatch.setenv("EMBEDDINGS_ENABLED", "0")
    monkeypatch.setenv("EMBEDDING_BACKEND", "local")
    monkeypatch.delenv("OPENAI_COMPATIBLE_MODEL", raising=False)
    monkeypatch.delenv("RERANKER_MODEL", raising=False)
