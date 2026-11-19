"""Readable invoice results shared by the dashboard and saved history."""
import streamlit as st


def amount(value, currency):
    return "Missing" if value is None else f"{currency or ''} {value:,.2f}".strip()


def show_decision(decision):
    labels = {"APPROVED": "Approved", "NEEDS_REVIEW": "Needs review", "BLOCKED": "Blocked"}
    st.subheader("Decision: " + labels.get(decision.status, decision.status))
    if decision.status == "APPROVED":
        st.success("All configured finance controls passed. This does not execute a payment.")
    for finding in decision.findings:
        display = st.error if finding.severity == "BLOCK" else (
            st.warning if finding.severity == "REVIEW" else st.info)
        display(finding.message)


def show_evidence(evidence):
    entries = evidence.get("retrieval", [])
    if not entries:
        st.caption("No additional policy or PO suggestions were retrieved for this run.")
    for entry in entries:
        source = entry.get("source", "Supporting evidence")
        candidates = entry.get("candidates", [])
        if source == "po_candidates":
            st.markdown("**Suggested purchase orders**")
            if candidates:
                st.dataframe([{"PO": c.get("id"), "Currency": c.get("currency"),
                               "Original PO cap": c.get("cap")} for c in candidates],
                             hide_index=True, width="stretch")
                st.caption("Suggestions require verification. The original cap is not the remaining balance.")
            else:
                st.caption("No open PO candidates found for the verified vendor.")
        elif source == "semantic_po_line_candidates":
            if candidates:
                st.markdown("**Suggested PO lines**")
                st.dataframe([{"Invoice item": c.get("invoice_description"),
                               "PO": c.get("po_id"), "PO line": c.get("po_line_id"),
                               "Match": c.get("match_type", c.get("error", "Unavailable"))}
                              for c in candidates], hide_index=True, width="stretch")
        elif entry.get("text"):
            st.markdown("**" + entry.get("section", source) + "**")
            st.text(entry["text"])
            st.caption("Source: " + source)
    if evidence.get("parent_run_id"):
        st.caption(f"Corrected by {evidence.get('reviewer', 'reviewer')} · Original run: {evidence['parent_run_id']}")


def show_invoice(invoice, decision):
    a, b, c = st.columns(3)
    a.metric("Invoice", invoice.invoice_number or "Missing")
    b.metric("Invoice total", amount(invoice.invoice_total, invoice.currency))
    c.metric("Matched PO", decision.po_id or "Unverified")
    left, right = st.columns([3, 2])
    with left:
        st.markdown("#### Invoice details")
        st.dataframe([
            {"Field": label, "Value": value or "Missing"}
            for label, value in [("Vendor", invoice.vendor_name), ("Vendor tax ID", invoice.vendor_tax_id),
                                 ("Invoice date", invoice.invoice_date), ("PO reference", invoice.po_number),
                                 ("Currency", invoice.currency)]
        ], hide_index=True, width="stretch")
    with right:
        st.markdown("#### Amounts")
        st.dataframe([
            {"Amount": label, "Value": amount(value, invoice.currency)}
            for label, value in [("Subtotal", invoice.subtotal), ("Tax", invoice.tax_amount),
                                 ("Total", invoice.invoice_total)]
        ], hide_index=True, width="stretch")
    st.markdown("#### Line items")
    if invoice.line_items:
        st.dataframe([{"Description": line.description, "SKU": line.sku or "Missing",
                       "Quantity": format(line.quantity, "f"),
                       "Unit price": amount(line.unit_price, invoice.currency),
                       "Line total": amount(line.line_total, invoice.currency)}
                      for line in invoice.line_items], hide_index=True, width="stretch")
    else:
        st.info("No line items were extracted. Review the source PDF.")
    st.markdown("#### Supporting evidence")
    show_evidence(decision.evidence)
    with st.expander("Technical details · raw JSON", expanded=False):
        st.json({"invoice": invoice.model_dump(mode="json"), "decision": decision.model_dump(mode="json")})
