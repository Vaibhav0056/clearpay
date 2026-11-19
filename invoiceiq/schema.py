from __future__ import annotations
from decimal import Decimal, InvalidOperation
from typing import Optional
from pydantic import BaseModel, ConfigDict, Field, field_validator


def money(value) -> Decimal:
    """Normalize a supplied decimal, rejecting ambiguous or non-finite amounts."""
    if isinstance(value, bool):
        raise ValueError("Boolean is not a monetary amount")
    if value is None or str(value).strip() == "":
        raise ValueError("Missing monetary amount")
    s = str(value).strip()
    for symbol in ("₹", "$", "€", " "):  # rupee, dollar, euro, spaces
        s = s.replace(symbol, "")
    if "," in s and "." in s:
        if s.rfind(",") > s.rfind("."):
            s = s.replace(".", "").replace(",", ".")
        else:
            s = s.replace(",", "")
    elif "," in s:
        pieces = s.split(",")
        if len(pieces) == 2 and len(pieces[1]) in (1, 2):
            s = s.replace(",", ".")
        elif all(len(p) == 3 for p in pieces[1:]):
            s = s.replace(",", "")
        else:
            raise ValueError("Ambiguous number locale")
    try:
        val = Decimal(s)
    except InvalidOperation as exc:
        raise ValueError(f"Invalid decimal: {value}") from exc
    if not val.is_finite():
        raise ValueError("Non-finite decimal")
    return val


class InvoiceLine(BaseModel):
    model_config = ConfigDict(extra="ignore")
    description: str = ""
    sku: Optional[str] = None
    quantity: Decimal = Decimal("0")
    unit_price: Decimal = Decimal("0")
    line_total: Decimal = Decimal("0")

    @field_validator("quantity", "unit_price", "line_total", mode="before")
    @classmethod
    def coerce_decimal(cls, value):
        return money(value)


class Invoice(BaseModel):
    model_config = ConfigDict(extra="ignore")
    invoice_number: Optional[str] = None
    invoice_date: Optional[str] = None
    vendor_name: Optional[str] = None
    vendor_tax_id: Optional[str] = None
    po_number: Optional[str] = None
    # None = not printed on the invoice (common on domestic invoices); never guessed by the model.
    currency: Optional[str] = "INR"
    subtotal: Optional[Decimal] = None
    tax_amount: Optional[Decimal] = None
    invoice_total: Optional[Decimal] = None
    line_items: list[InvoiceLine] = Field(default_factory=list)
    # subtotal is always the pre-tax amount and invoice_total = subtotal + tax.
    # When True, line unit prices/line totals are tax-inclusive (gross), e.g. GST-inclusive retail pricing.
    prices_include_tax: bool = False
    # Set by the pipeline (never by the model), e.g. SCANNED_NO_TEXT when nothing could be read.
    extraction_flags: list[str] = Field(default_factory=list)
    evidence: dict = Field(default_factory=dict)

    @field_validator("currency", mode="before")
    @classmethod
    def blank_currency(cls, value):
        return None if value is None or str(value).strip() == "" else str(value).strip().upper()

    @field_validator("subtotal", "tax_amount", "invoice_total", mode="before")
    @classmethod
    def coerce_optional(cls, value):
        return None if value is None or value == "" else money(value)


class Finding(BaseModel):
    code: str
    message: str
    severity: str  # BLOCK | REVIEW | INFO


class Decision(BaseModel):
    status: str
    findings: list[Finding] = Field(default_factory=list)
    po_id: Optional[str] = None
    vendor_id: Optional[str] = None
    evidence: dict = Field(default_factory=dict)
