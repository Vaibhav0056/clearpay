"""Streamlit UI: streamlit run app.py"""
from __future__ import annotations
import json
import os
from pathlib import Path
import streamlit as st

# Streamlit Community Cloud: expose root-level secrets as environment variables before the
# backend reads its configuration. Locally there is usually no secrets file and .env is used.
try:
    for _key, _value in st.secrets.to_dict().items():
        if isinstance(_value, (str, int, float, bool)):
            os.environ.setdefault(_key, str(_value))
except Exception:
    pass

from invoiceiq.database import Repository
import pymupdf
from invoiceiq.workflow import process_pdf, review_and_recheck
from invoiceiq.schema import Invoice, Decision
from invoiceiq.ui import show_invoice, show_decision

st.set_page_config(page_title="ClearPay — AP Automation",page_icon="🧾",layout="wide")
st.title("🧾 ClearPay · Explainable AP Automation")
st.caption("Live PDF-to-decision workflow • deterministic finance controls • conditional agentic retrieval")
ROOT=Path(__file__).parent


@st.cache_resource(show_spinner="Connecting to database...")
def open_repository(path: str) -> Repository:
    # Created once per server process: avoids re-running schema setup on every click.
    return Repository(path)


try:
    repo=open_repository(os.getenv("INVOICEIQ_DB",str(ROOT/"invoiceiq.sqlite")))
except Exception as exc:
    st.error(f"Database unavailable: {exc}")
    st.stop()
with st.sidebar:
    st.header("Settings")
    mode=st.selectbox("Extraction",["auto","fixture","vision"],help="Auto uses an OpenAI-compatible vision endpoint if configured; otherwise accepts labelled demo invoices only.")
    st.caption("The local fixture parser is designed for bundled synthetic PDFs and is not a universal invoice parser.")
    if st.button("Seed demo procurement database"):
        repo.seed();st.success("Demo vendors and POs are ready")
    # Local recording aid only: off unless explicitly enabled, so a public deployment can't be wiped.
    if os.getenv("DEMO_RESET_ENABLED")=="1" and st.button("Reset demo data",help="Deletes all runs and approvals, then re-seeds"):
        repo.reset_demo();st.session_state.pop("last_result",None);st.rerun()
    st.markdown("**Capabilities**")
    st.caption("Database: "+("Supabase Postgres (connected)" if repo.postgres else "local SQLite (temporary on cloud hosts)"))
    st.caption("Vision API: "+("configured" if os.getenv("OPENAI_COMPATIBLE_MODEL") else "not configured"))
    st.caption("OCR fallback: "+("enabled" if os.getenv("OCR_ENABLED")=="1" else "disabled"))
    st.caption("LangGraph: optional; falls back to Python routing")
    st.caption("Semantic retrieval: "+("enabled" if os.getenv("EMBEDDINGS_ENABLED")=="1" else "lexical only"))
    if os.getenv("EMBEDDINGS_ENABLED")=="1":
        st.caption("Embedding backend: "+os.getenv("EMBEDDING_BACKEND", "local"))
        if os.getenv("EMBEDDING_BACKEND")=="remote" and not os.getenv("EMBEDDING_API_URL"):
            st.warning("Set EMBEDDING_API_URL and EMBEDDING_API_KEY for hosted BGE-M3.")

upload,history,reference=st.tabs(["Run Invoice","History & Analytics","Demo Inputs & Policies"])
with upload:
    st.subheader("Submit an invoice PDF")
    pdf=st.file_uploader("Upload an invoice",type=["pdf"])
    if pdf:
        if pdf.size > 10 * 1024 * 1024:
            st.error("Maximum PDF size is 10 MB")
            st.stop()
        st.caption(f"Selected: {pdf.name} · {pdf.size:,} bytes")
        with st.expander("Original PDF",expanded=False):
            st.download_button("Download supplied document",pdf.getvalue(),file_name=pdf.name,mime="application/pdf")
            try:
                with pymupdf.open(stream=pdf.getvalue(), filetype="pdf") as preview:
                    for page in list(preview)[:10]:
                        st.image(page.get_pixmap(matrix=pymupdf.Matrix(1,1)).tobytes("png"))
            except Exception:
                st.warning("PDF preview unavailable")
        if st.button("▶ Process invoice",type="primary"):
            placeholder=st.empty()
            events=[]
            def display_event(e):
                events.append(e)
                placeholder.dataframe(events,width="stretch",hide_index=True)
            with st.status("Processing real invoice workflow...",expanded=True) as status:
                try:
                    result=process_pdf(pdf.getvalue(),pdf.name,repo,mode=mode,policy_dir=ROOT/"fixtures/policies",on_event=display_event)
                    status.update(label="Completed: "+result["decision"].status,state="complete")
                    st.session_state["last_result"]=result
                except Exception as exc:
                    status.update(label="Invoice run failed",state="error")
                    st.error(f"{type(exc).__name__}: {exc}")
    result=st.session_state.get("last_result")
    if result:
        invoice=result["invoice"];decision=result["decision"]
        st.divider()
        show_decision(decision)
        st.caption("Run ID: "+result["run_id"])
        show_invoice(invoice,decision)
        if decision.status=="NEEDS_REVIEW":
            with st.expander("Human review — correct fields and rerun all financial rules", expanded=False):
                st.caption("Demo reviewer: edits create a new auditable run, never bypassing financial controls.")
                corrected=st.text_area("Corrected invoice JSON",value=invoice.model_dump_json(indent=2),height=240,key="review_"+result["run_id"])
                if st.button("Recheck corrected invoice",key="submit_"+result["run_id"]):
                    try:
                        revised=review_and_recheck(repo,result["run_id"],json.loads(corrected))
                        st.session_state["last_result"]=revised
                        st.rerun()
                    except Exception as exc:
                        st.error(str(exc))
with history:
    runs=repo.runs()
    if not runs: st.info("No executions yet. Seed the demo database and process a PDF.")
    else:
        counts={x:sum(r["status"]==x for r in runs) for x in ["APPROVED","NEEDS_REVIEW","BLOCKED","ERROR"]}
        cols=st.columns(4)
        for col,(name,n) in zip(cols,counts.items()): col.metric(name,n)
        st.dataframe([{k:v for k,v in r.items() if k in ["id","filename","created_at","status","error"]} for r in runs],width="stretch",hide_index=True)
        ids=[r["id"] for r in runs]
        selected=st.selectbox("Inspect full audit history",ids)
        record=repo.get_run(selected)
        if record:
            st.markdown("#### Actual execution events")
            events = json.loads(record["events_json"] or "[]")
            if events:
                st.dataframe([{"Stage": e["stage"], "Detail": e["detail"], "Elapsed (ms)": e.get("elapsed_ms",0)}
                              for e in events],hide_index=True,width="stretch")
            if record["decision_json"]:
                saved_decision = Decision.model_validate_json(record["decision_json"])
                show_decision(saved_decision)
                if record["invoice_json"]:
                    show_invoice(Invoice.model_validate_json(record["invoice_json"]),saved_decision)
            elif record["error"]:
                st.error(record["error"])
            stored = repo.document(record["document_hash"])
            if stored:
                st.download_button("Download original PDF", stored, file_name="invoice.pdf", mime="application/pdf")
            if record["invoice_json"] and st.button("Open invoice for review"):
                st.session_state["last_result"] = {"run_id":selected,"invoice":Invoice.model_validate_json(record["invoice_json"]),"decision":Decision.model_validate_json(record["decision_json"])}
                st.rerun()
with reference:
    st.markdown("#### Sample invoices")
    st.caption("Synthetic test PDFs. Download one, then upload it in the Run Invoice tab.")
    samples=sorted((ROOT/"fixtures/invoices").glob("*.pdf"))
    for column,sample in zip(st.columns(3)*((len(samples)+2)//3),samples):
        column.download_button(sample.stem.replace("_"," "),sample.read_bytes(),file_name=sample.name,
                               mime="application/pdf",key="sample_"+sample.name,width="stretch")
    st.markdown("#### AP policy used for retrieval")
    st.code((ROOT/"fixtures/policies/ap_policy.md").read_text(encoding="utf-8"),language="markdown")
    st.info("Model access requires external inference credentials; all finance decisions are local and deterministic. This app never sends money or posts real invoices.")
