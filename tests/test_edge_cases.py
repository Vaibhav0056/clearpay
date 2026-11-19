"""Edge cases 08 (unreadable scan) and 09 (tax-inclusive line pricing). See README "Edge cases"."""
from decimal import Decimal
from pathlib import Path
import httpx
from invoiceiq import extraction
from invoiceiq.finance import evaluate_invoice
from invoiceiq.schema import Invoice
from invoiceiq.workflow import process_pdf, review_and_recheck

FIX = Path(__file__).resolve().parents[1] / "fixtures/invoices"
SCAN = FIX / "08_scanned_different_layout.pdf"
GST = FIX / "09_gst_inclusive_pricing.pdf"


def codes(decision):
    return [f.code for f in decision.findings]


def committed(repo):
    with repo.connect() as c:
        return c.execute("SELECT count(*) FROM invoices").fetchone()[0]


# --- 08: scanned, different layout -------------------------------------------------------------

def test_unreadable_scan_goes_to_review_not_block(repo, monkeypatch):
    monkeypatch.delenv("OCR_ENABLED", raising=False)
    result = process_pdf(SCAN.read_bytes(), SCAN.name, repo, mode="fixture")
    decision = result["decision"]
    assert decision.status == "NEEDS_REVIEW"
    assert "UNREADABLE_SCAN" in codes(decision) and "VENDOR_NOT_EXTRACTED" in codes(decision)
    # Not having read the vendor is not evidence of a bad vendor.
    assert "UNKNOWN_VENDOR" not in codes(decision)
    assert committed(repo) == 0


def test_reviewer_keys_in_scan_and_all_rules_rerun(repo, monkeypatch):
    monkeypatch.delenv("OCR_ENABLED", raising=False)
    run = process_pdf(SCAN.read_bytes(), SCAN.name, repo, mode="fixture")
    typed = dict(invoice_number="INV-008", invoice_date="2026-11-12", vendor_name="Acme Supplies Pvt Ltd",
                 vendor_tax_id="TAX-ACME", po_number="PO-1007", currency="INR", subtotal="10000.00",
                 tax_amount="1800.00", invoice_total="11800.00",
                 line_items=[dict(description="Ergonomic office chair mesh", sku="CHAIR-B24", quantity="4",
                                  unit_price="2500.00", line_total="10000.00")])
    result = review_and_recheck(repo, run["run_id"], typed, "tester")
    assert result["decision"].status == "APPROVED"
    # The reviewer's input is checked like any other: a second identical correction is a duplicate.
    again = process_pdf(SCAN.read_bytes(), "rescan.pdf", repo, mode="fixture")
    assert review_and_recheck(repo, again["run_id"], typed, "tester")["decision"].status == "BLOCKED"


def test_model_cannot_clear_pipeline_flags(repo, monkeypatch):
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://inference.invalid/v1")
    monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL", "test-injected-model")
    body = '{"invoice_number":"X","extraction_flags":["ANYTHING"]}'
    respond = lambda request: httpx.Response(200, json={"choices": [{"message": {"content": body}}]})
    original = httpx.Client
    monkeypatch.setattr(extraction.httpx, "Client",
                        lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    invoice = extraction.extract_via_vision_api(SCAN.read_bytes(), "")
    assert invoice.extraction_flags == []


# --- 09: GST-inclusive line prices -------------------------------------------------------------

def gst_invoice(**overrides):
    data = dict(invoice_number="NOS-1", invoice_date="2026-11-12", vendor_name="Nova Office Systems",
                vendor_tax_id="TAX-NOVA", po_number="PO-2001", currency="INR", prices_include_tax=True,
                subtotal="12000.00", tax_amount="2160.00", invoice_total="14160.00",
                line_items=[dict(description="Adjustable workstation desk", sku="DESK-01", quantity="2",
                                 unit_price="7080.00", line_total="14160.00")])
    data.update(overrides)
    return Invoice.model_validate(data)


def test_gst_inclusive_fixture_approved_and_allocates_pre_tax(repo):
    result = process_pdf(GST.read_bytes(), GST.name, repo, mode="fixture")
    assert result["invoice"].prices_include_tax
    assert result["decision"].status == "APPROVED"
    assert codes(result["decision"]) == ["TAX_INCLUSIVE_NORMALISED"]
    with repo.connect() as c:
        allocated = c.execute("SELECT amount FROM allocations").fetchone()["amount"]
    # PO commitment is consumed at the pre-tax value, not the gross ₹14,160.
    assert Decimal(allocated) == Decimal("12000.00")


def test_naive_reading_of_same_invoice_is_price_variance(repo):
    decision = evaluate_invoice(repo, gst_invoice(prices_include_tax=False), "naive", commit=False)
    assert decision.status == "NEEDS_REVIEW"
    assert "PRICE_VARIANCE" in codes(decision)


def test_inclusive_overcharge_still_caught(repo):
    # ₹7,300 incl. 18% GST = ₹6,186.44 pre-tax: 3.1% over the ₹6,000 PO price.
    inv = gst_invoice(subtotal="12372.88", tax_amount="2227.12", invoice_total="14600.00",
                      line_items=[dict(description="Adjustable workstation desk", sku="DESK-01", quantity="2",
                                       unit_price="7300.00", line_total="14600.00")])
    assert "PRICE_VARIANCE" in codes(evaluate_invoice(repo, inv, "over", commit=False))


def test_inclusive_lines_must_reconcile_to_gross_total(repo):
    inv = gst_invoice(invoice_total="14000.00", subtotal="11840.00")
    decision = evaluate_invoice(repo, inv, "bad", commit=False)
    assert decision.status == "NEEDS_REVIEW" and "SUBTOTAL_MISMATCH" in codes(decision)


def test_unprinted_currency_takes_verified_po_currency(repo):
    inv = gst_invoice(currency=None)
    decision = evaluate_invoice(repo, inv, "nocur", commit=False)
    assert decision.status == "APPROVED" and "CURRENCY_FROM_PO" in codes(decision)


def test_unprinted_currency_cannot_hide_wrong_currency_amounts(repo):
    # Same desks billed in USD with no symbol printed: ±2% price check still catches it.
    inv = gst_invoice(currency=None, prices_include_tax=False, subtotal="144.00", tax_amount="0", invoice_total="144.00",
                      line_items=[dict(description="Adjustable workstation desk", sku="DESK-01", quantity="2",
                                       unit_price="72.00", line_total="144.00")])
    assert "PRICE_VARIANCE" in codes(evaluate_invoice(repo, inv, "usd", commit=False))


def _mock_model(monkeypatch, responses):
    monkeypatch.setenv("OPENAI_COMPATIBLE_BASE_URL", "https://inference.invalid/v1")
    monkeypatch.setenv("OPENAI_COMPATIBLE_MODEL", "test-injected-model")
    monkeypatch.setattr(extraction.time, "sleep", lambda seconds: None)
    calls = []
    def respond(request):
        calls.append(1)
        return responses[min(len(calls), len(responses)) - 1]
    original = httpx.Client
    monkeypatch.setattr(extraction.httpx, "Client",
                        lambda **kw: original(transport=httpx.MockTransport(respond), **kw))
    return calls


def test_transient_model_overload_is_retried(monkeypatch):
    ok = httpx.Response(200, json={"choices": [{"message": {"content": '{"invoice_number":"INV-9"}'}}]})
    calls = _mock_model(monkeypatch, [httpx.Response(503), httpx.Response(429), ok])
    assert extraction.extract_via_vision_api(SCAN.read_bytes(), "").invoice_number == "INV-9"
    assert len(calls) == 3


def test_persistent_model_outage_fails_without_endless_retries(monkeypatch):
    import pytest
    calls = _mock_model(monkeypatch, [httpx.Response(503)])
    with pytest.raises(httpx.HTTPStatusError):
        extraction.extract_via_vision_api(SCAN.read_bytes(), "")
    assert len(calls) == 3


def test_client_errors_are_not_retried(monkeypatch):
    import pytest
    calls = _mock_model(monkeypatch, [httpx.Response(401)])
    with pytest.raises(httpx.HTTPStatusError):
        extraction.extract_via_vision_api(SCAN.read_bytes(), "")
    assert len(calls) == 1
