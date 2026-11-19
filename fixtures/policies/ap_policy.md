# Invoice validation
Invoices require a verifiable invoice number, invoice date, approved vendor, total and matching PO. All invoice arithmetic must reconcile, and duplicate active invoice numbers must be blocked.
# Price tolerance
For this synthetic demo the individual line-unit price tolerance is 2 percent. Quantity cannot exceed remaining PO quantity. Subtotals are compared exclusive of tax. Invoice totals must equal subtotal plus tax within INR 0.01. These are demonstration policies, not Zamp policies.
# Exceptions
An invoice with uncertain PO identity, missing fields or insufficient PO balance must be sent to AP review. A human must confirm a suggested semantic PO match before approval. A reviewer cannot bypass hard duplicate constraints.
# Tax
Tax is validated arithmetically but not deducted from the tax-exclusive PO commitment. When a supplier quotes tax-inclusive line prices, each line is converted to its pre-tax value (line price × subtotal ÷ total) before it is compared with the PO price and before it consumes PO balance. Gross lines must add up to the invoice total. A policy for tax-inclusive POs would require explicit configuration.
# Unreadable documents
A scanned page that no OCR engine or vision model has read is sent to AP review, not blocked. Missing fields on such a page mean nothing was read, not that the supplier omitted them. A reviewer may key in the values; every finance rule is then re-applied.
