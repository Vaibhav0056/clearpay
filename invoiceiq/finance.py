"""Finance rules. Auto approval is restricted to exact verified PO line/SKU identity.
All approvals and allocations are committed atomically under BEGIN IMMEDIATE.
"""
from __future__ import annotations
import os
import re
from datetime import date
from decimal import Decimal
from .schema import Invoice, Decision, Finding
from .retrieval import match_po_line

CENT=Decimal("0.01")
PRICE_TOLERANCE=Decimal("0.02")


def key_invoice(num: str) -> str:
    return re.sub(r"[^A-Z0-9]", "",num.upper())


def validate_invoice(invoice: Invoice) -> list[Finding]:
    issues=[]
    def add(code,msg,sev="REVIEW"):
        issues.append(Finding(code=code,message=msg,severity=sev))
    if "SCANNED_NO_TEXT" in invoice.extraction_flags:
        add("UNREADABLE_SCAN","Scanned page has no text layer and no OCR or vision model read it; "
            "fields below are missing because they were not read, not because the invoice lacks them")
    for field in ("invoice_number","invoice_date","vendor_name","invoice_total","subtotal","tax_amount"):
        if getattr(invoice,field) is None or getattr(invoice,field)=="":
            add("MISSING_FIELD",f"Missing {field}")
    if invoice.invoice_date:
        try:
            d=date.fromisoformat(invoice.invoice_date)
            if d>date.today(): add("FUTURE_DATE","Invoice date is in the future")
        except ValueError:
            add("INVALID_DATE","Invoice date must be YYYY-MM-DD")
    if invoice.invoice_number and not key_invoice(invoice.invoice_number):
        add("INVALID_INVOICE_NUMBER", "Invoice number must contain letters or digits")
    if invoice.currency is not None and not re.fullmatch(r"[A-Z]{3}", invoice.currency):
        add("INVALID_CURRENCY", "Currency must be a three-letter uppercase code")
    if invoice.tax_amount is not None and invoice.tax_amount < 0:
        add("NEGATIVE_TAX", "Tax amount cannot be negative")
    if invoice.subtotal is not None and invoice.subtotal < 0:
        add("NEGATIVE_SUBTOTAL", "Subtotal cannot be negative")
    # Tax jurisdiction/rates must be explicitly configured; never inferred by an LLM.
    configured = os.getenv("ALLOWED_TAX_RATES")
    if configured and invoice.subtotal is not None and invoice.tax_amount is not None:
        rates = [Decimal(x.strip()) for x in configured.split(",")]
        if not any(abs(invoice.subtotal * rate / 100 - invoice.tax_amount) <= CENT for rate in rates):
            add("TAX_RATE_MISMATCH", "Tax does not match configured allowed percentage rates")
    if invoice.invoice_total is not None and invoice.invoice_total<0:
        add("NEGATIVE_TOTAL","Negative invoice total")
    if invoice.subtotal is not None and invoice.tax_amount is not None and invoice.invoice_total is not None:
        if abs(invoice.subtotal+invoice.tax_amount-invoice.invoice_total)>CENT:
            add("TOTAL_MISMATCH","Subtotal + tax does not match invoice total")
    if not invoice.line_items:
        add("MISSING_LINES","No line items available to verify")
    else:
        for n,line in enumerate(invoice.line_items,1):
            if not line.description or line.quantity<=0 or line.unit_price<0 or line.line_total<0:
                add("INVALID_LINE",f"Line {n} contains invalid values")
            elif abs(line.quantity*line.unit_price-line.line_total)>CENT:
                add("LINE_ARITHMETIC",f"Line {n} quantity × price mismatch")
        line_sum=sum((l.line_total for l in invoice.line_items),Decimal(0))
        if invoice.prices_include_tax:
            # Gross lines must add up to the gross total; subtotal is the pre-tax amount.
            if invoice.invoice_total is not None and abs(line_sum-invoice.invoice_total)>CENT:
                add("SUBTOTAL_MISMATCH","Sum of tax-inclusive line totals differs from invoice total")
            elif invoice.subtotal is not None and invoice.invoice_total:
                add("TAX_INCLUSIVE_NORMALISED",
                    f"Line prices include tax; compared to the pre-tax PO at {invoice.subtotal}/{invoice.invoice_total} of the stated price","INFO")
        elif invoice.subtotal is not None and abs(line_sum-invoice.subtotal)>CENT:
            add("SUBTOTAL_MISMATCH","Sum of invoice line totals differs from subtotal")
    return issues


def net_factor(invoice: Invoice) -> Decimal:
    """Multiplier that turns a line's stated price into its pre-tax price (1 for tax-exclusive invoices)."""
    if invoice.prices_include_tax and invoice.subtotal is not None and invoice.invoice_total:
        return invoice.subtotal/invoice.invoice_total
    return Decimal(1)


def _rowdict(rows):
    return [dict(r) for r in rows]


def _verify(conn, invoice: Invoice, digest: str):
    findings=validate_invoice(invoice)
    allocations=[]
    vendor=None;po=None
    def add(code,msg,sev="REVIEW"):
        findings.append(Finding(code=code,message=msg,severity=sev))
    # Verified ID must match submitted vendor name if both exist.
    if invoice.vendor_tax_id:
        vendor=conn.execute("SELECT * FROM vendors WHERE UPPER(tax_id)=UPPER(?)",(invoice.vendor_tax_id,)).fetchone()
        if vendor is not None and invoice.vendor_name and vendor["name"].casefold()!=invoice.vendor_name.casefold():
            add("VENDOR_NAME_MISMATCH","Tax ID and submitted vendor name disagree")
    elif invoice.vendor_name:
        vendor=conn.execute("SELECT * FROM vendors WHERE LOWER(name)=LOWER(?)",(invoice.vendor_name,)).fetchone()
    if vendor is None and not (invoice.vendor_tax_id or invoice.vendor_name):
        # Nothing was read, so there is no evidence the vendor is bad: review, don't block.
        add("VENDOR_NOT_EXTRACTED","No vendor name or tax ID could be read from the document")
    elif vendor is None:
        add("UNKNOWN_VENDOR","Vendor not verified", "BLOCK")
    elif not vendor["approved"]:
        add("UNAPPROVED_VENDOR","Vendor is not approved", "BLOCK")
    if vendor and invoice.invoice_number:
        canonical=key_invoice(invoice.invoice_number)
        rows=conn.execute("SELECT invoice_number FROM invoices WHERE vendor_id=? AND status='APPROVED'",(vendor["id"],)).fetchall()
        if any(key_invoice(r["invoice_number"])==canonical for r in rows):
            add("DUPLICATE_INVOICE","This vendor invoice number has already been approved", "BLOCK")
        samehash=conn.execute("SELECT id FROM invoices WHERE vendor_id=? AND document_hash=? AND status='APPROVED'",(vendor["id"],digest)).fetchone()
        if samehash:
            add("DUPLICATE_DOCUMENT","Same vendor PDF was previously approved", "BLOCK")
    if not invoice.po_number:
        add("PO_MISSING","PO number missing; semantic suggestions require human verification")
    else:
        po=conn.execute("SELECT * FROM purchase_orders WHERE id=?",(invoice.po_number,)).fetchone()
        if po is None:
            add("PO_NOT_FOUND","PO reference not found")
        else:
            if po["status"]!="OPEN": add("PO_CLOSED","PO is not open")
            if vendor and po["vendor_id"]!=vendor["id"]:
                add("PO_VENDOR_MISMATCH","PO belongs to another vendor", "BLOCK")
            if invoice.currency is None:
                # Safe only because the PO is an exact verified match and the ±2% price check would
                # catch amounts denominated in a different currency.
                add("CURRENCY_FROM_PO",f"No currency printed on invoice; amounts read in PO currency {po['currency']}","INFO")
            elif invoice.currency.upper()!=po["currency"].upper():
                add("CURRENCY_MISMATCH","PO and invoice currency disagree")
            po_lines=_rowdict(conn.execute("SELECT * FROM po_lines WHERE po_id=?",(po["id"],)).fetchall())
            allocations_by_line={}
            for row in conn.execute("SELECT a.po_line_id,a.qty,a.amount FROM allocations a JOIN invoices i ON i.id=a.invoice_id WHERE i.status='APPROVED' AND i.po_id=?",(po["id"],)):
                slot=allocations_by_line.setdefault(row["po_line_id"],{"qty":Decimal(0),"amount":Decimal(0)})
                slot["qty"]+=Decimal(row["qty"]);slot["amount"]+=Decimal(row["amount"])
            allocated_total=sum((x["amount"] for x in allocations_by_line.values()),Decimal(0))
            if invoice.subtotal is not None and invoice.subtotal > Decimal(po["cap"])-allocated_total+CENT:
                add("PO_CAPACITY_EXCEEDED",f"Invoice subtotal {invoice.subtotal} exceeds remaining tax-exclusive PO balance {Decimal(po['cap'])-allocated_total}")
            new_usage={}
            factor=net_factor(invoice)
            for idx,line in enumerate(invoice.line_items,1):
                net_unit=line.unit_price*factor
                net_total=(line.line_total*factor).quantize(CENT)
                match,kind=match_po_line(line,po_lines,use_embeddings=False)
                if not match:
                    add("LINE_UNMATCHED",f"Line {idx} has no plausible PO line")
                    continue
                if kind!="sku_verified":
                    add("LINE_IDENTITY_UNVERIFIED",f"Line {idx} matched description only; requires confirmation")
                    continue
                old=allocations_by_line.get(match["id"],{"qty":Decimal(0),"amount":Decimal(0)})
                current=new_usage.get(match["id"],{"qty":Decimal(0),"amount":Decimal(0)})
                allowed=Decimal(match["qty"])-old["qty"]-current["qty"]
                if line.quantity>allowed:
                    add("PO_QUANTITY_EXCEEDED",f"Line {idx} quantity {line.quantity} exceeds remaining {allowed}")
                remaining_amount = Decimal(match["amount"]) - old["amount"] - current["amount"]
                if net_total > remaining_amount + CENT:
                    add("PO_LINE_BALANCE_EXCEEDED", f"Line {idx} pre-tax amount {net_total} exceeds remaining PO line balance {remaining_amount}")
                expected=Decimal(match["unit_price"])
                if expected<=0 or abs(net_unit-expected)>expected*PRICE_TOLERANCE:
                    add("PRICE_VARIANCE",f"Line {idx} pre-tax unit price {net_unit.quantize(CENT)} differs from PO price {expected} by more than 2%")
                next_usage={"qty":current["qty"]+line.quantity,"amount":current["amount"]+net_total}
                new_usage[match["id"]]=next_usage
            allocations=[(lineid,str(use["qty"]),str(use["amount"])) for lineid,use in new_usage.items()]
    status="BLOCKED" if any(f.severity=="BLOCK" for f in findings) else (
        "NEEDS_REVIEW" if any(f.severity=="REVIEW" for f in findings) else "APPROVED")
    return Decision(status=status,findings=findings,po_id=po["id"] if po else None,vendor_id=vendor["id"] if vendor else None),allocations


def evaluate_invoice(repo, invoice: Invoice, document_hash: str, commit: bool=True) -> Decision:
    """Must evaluate fresh inside same transaction used for authorization. No agent can override."""
    with repo.transaction() as conn:
        result,allocations=_verify(conn,invoice,document_hash)
        if result.status=="APPROVED" and commit:
            try:
                cursor=conn.execute("INSERT INTO invoices(vendor_id,invoice_number,document_hash,invoice_total,po_id,status) VALUES(?,?,?,?,?,'APPROVED') RETURNING id",
                    (result.vendor_id, invoice.invoice_number,document_hash,str(invoice.invoice_total),result.po_id))
                invoice_id = cursor.fetchone()["id"]
            except repo.integrity_errors:
                return Decision(status="BLOCKED",findings=[Finding(code="DUPLICATE_INVOICE",message="Concurrent duplicate invoice",severity="BLOCK")],po_id=result.po_id,vendor_id=result.vendor_id)
            for lineid,qty,amount in allocations:
                conn.execute("INSERT INTO allocations(invoice_id,po_line_id,qty,amount) VALUES(?,?,?,?)",(invoice_id,lineid,qty,amount))
        return result
