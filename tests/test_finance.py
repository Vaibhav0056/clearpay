from invoiceiq.finance import evaluate_invoice
from invoiceiq.schema import Invoice


def test_happy_path_commits(repo,invoice):
    result=evaluate_invoice(repo,invoice,"hash1")
    assert result.status=="APPROVED"
    with repo.connect() as c:
        assert c.execute("SELECT count(*) FROM allocations").fetchone()[0]==1


def test_duplicate_normalized_number(repo,invoice):
    assert evaluate_invoice(repo,invoice,"hash1").status=="APPROVED"
    revised=invoice.model_copy(update={"invoice_number":"inv 1"})
    result=evaluate_invoice(repo,revised,"another-hash")
    assert result.status=="BLOCKED"
    assert any(f.code=="DUPLICATE_INVOICE" for f in result.findings)


def test_partial_po_capacity(repo,invoice_data):
    first=dict(invoice_data,invoice_number="PARTIAL",subtotal="65000",invoice_total="65000",line_items=[dict(invoice_data["line_items"][0],quantity="26",line_total="65000")])
    second=dict(invoice_data,invoice_number="SECOND",subtotal="40000",invoice_total="40000",line_items=[dict(invoice_data["line_items"][0],quantity="16",line_total="40000")])
    assert evaluate_invoice(repo,Invoice.model_validate(first),"hash1").status=="APPROVED"
    result=evaluate_invoice(repo,Invoice.model_validate(second),"hash2")
    assert result.status=="NEEDS_REVIEW"
    assert "PO_CAPACITY_EXCEEDED" in [f.code for f in result.findings]
    assert "PO_QUANTITY_EXCEEDED" in [f.code for f in result.findings]


def test_missing_po_review(repo,invoice):
    result=evaluate_invoice(repo,invoice.model_copy(update={"po_number":None}),"hash1")
    assert result.status=="NEEDS_REVIEW"


def test_unapproved_vendor_block(repo,invoice):
    changed=invoice.model_copy(update={"vendor_name":"Unknown Vendor","vendor_tax_id":"TAX-UNKNOWN"})
    result=evaluate_invoice(repo,changed,"hash1")
    assert result.status=="BLOCKED"


def test_unknown_sku_review(repo,invoice):
    invoice.line_items[0].sku="OTHER"
    result=evaluate_invoice(repo,invoice,"hash1")
    assert result.status=="NEEDS_REVIEW"


def test_bad_arithmetic_review(repo,invoice):
    invoice.invoice_total+=1
    assert evaluate_invoice(repo,invoice,"hash1").status=="NEEDS_REVIEW"


def test_conflicting_name_and_tax_id(repo,invoice):
    invoice.vendor_name="Nova Office Systems"
    assert evaluate_invoice(repo,invoice,"hash1").status=="NEEDS_REVIEW"


def test_no_commit_on_dry_run(repo,invoice):
    assert evaluate_invoice(repo,invoice,"hash1",commit=False).status=="APPROVED"
    with repo.connect() as c:
        assert c.execute("SELECT count(*) FROM invoices").fetchone()[0]==0


def test_concurrent_duplicate_approvals(repo,invoice):
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=2) as ex:
        results=list(ex.map(lambda x: evaluate_invoice(repo,invoice,f'hash{x}').status,range(2)))
    assert sorted(results)==['APPROVED','BLOCKED']
    with repo.connect() as c:
        assert c.execute('SELECT count(*) FROM invoices').fetchone()[0]==1
