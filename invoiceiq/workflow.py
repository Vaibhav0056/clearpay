from __future__ import annotations
import hashlib
import json
import os
import time
import uuid
from functools import lru_cache
from pathlib import Path
from typing import TypedDict, Any
from .schema import Invoice
from .database import Repository
from .extraction import parse_document, extract_invoice
from .finance import evaluate_invoice
from .retrieval import PolicySearch, match_po_line

class FlowState(TypedDict, total=False):
    invoice: Any
    digest: str
    repo: Any
    policy_dir: str
    decision: Any
    evidence: list


def exception_node(state: FlowState):
    """Bounded tool-using exception investigator. It may retrieve policy or PO suggestions,
    but cannot mutate records or change approval eligibility.
    """
    invoice=state["invoice"]
    repo=state["repo"]
    evidence=[]
    try:
        search=PolicySearch(state["policy_dir"],use_embeddings=os.getenv("EMBEDDINGS_ENABLED")=="1")
        query=(invoice.po_number or "missing PO")+" invoice price tolerance duplicate vendor tax policy"
        evidence=search.search(query,k=3)
    except Exception as exc:
        evidence=[{"source":"retrieval","text":f"Policy search unavailable: {type(exc).__name__}","score":0}]
    suggestions=[]
    candidate_lines=[]
    with repo.connect() as c:
        if invoice.vendor_tax_id:
            vendor=c.execute("SELECT id FROM vendors WHERE UPPER(tax_id)=UPPER(?)",(invoice.vendor_tax_id,)).fetchone()
            if vendor:
                suggestions=[dict(r) for r in c.execute("SELECT id,cap,currency FROM purchase_orders WHERE vendor_id=? AND status='OPEN' LIMIT 5",(vendor["id"],))]
                candidate_lines=[dict(r) for r in c.execute("SELECT l.*,p.currency FROM po_lines l JOIN purchase_orders p ON p.id=l.po_id WHERE p.vendor_id=? AND p.status='OPEN' LIMIT 100",(vendor["id"],))]
    # Semantic candidates are recommendations, not verified matches or approvals.
    suggested_lines=[]
    for line in invoice.line_items[:20]:
        try:
            match,kind=match_po_line(line,candidate_lines,use_embeddings=os.getenv("EMBEDDINGS_ENABLED")=="1")
            if match:
                suggested_lines.append({"invoice_description":line.description,"po_id":match["po_id"],"po_line_id":match["id"],"match_type":kind})
        except Exception as exc:
            suggested_lines.append({"invoice_description":line.description,"error":type(exc).__name__})
    evidence.append({"source":"semantic_po_line_candidates","candidates":suggested_lines})
    evidence.append({"source":"po_candidates","candidates":suggestions,"text":"Suggestions are advisory only; an exact verified PO is required to approve."})
    return {"evidence":evidence}


def decision_node(state: FlowState):
    decision=evaluate_invoice(state["repo"],state["invoice"],state["digest"])
    decision.evidence={"retrieval":state.get("evidence",[])}
    return {"decision":decision}


def router(state: FlowState):
    # Precheck without committing; graph routes only uncertain cases to retrieval.
    pre=evaluate_invoice(state["repo"],state["invoice"],state["digest"],commit=False)
    interesting={"PO_MISSING","PO_NOT_FOUND","LINE_UNMATCHED","LINE_IDENTITY_UNVERIFIED","PRICE_VARIANCE","MISSING_FIELD"}
    if any(f.code in interesting for f in pre.findings):
        return "exception"
    return "decision"


@lru_cache(maxsize=1)
def build_graph():
    """Optional real LangGraph orchestration, graph is compiled when package installed."""
    from langgraph.graph import StateGraph, END
    graph=StateGraph(FlowState)
    graph.add_node("exception",exception_node)
    graph.add_node("decision",decision_node)
    graph.add_node("start",lambda state:{})
    graph.set_entry_point("start")
    graph.add_conditional_edges("start",router,{"exception":"exception","decision":"decision"})
    graph.add_edge("exception","decision")
    graph.add_edge("decision",END)
    return graph.compile()


def process_pdf(pdf: bytes, filename: str, repo: Repository, mode="auto", policy_dir="fixtures/policies", on_event=None, graph_enabled=None):
    """Real synchronous processing. Persists every terminal result and streams actual stage events."""
    if len(pdf)>10*1024*1024:
        raise ValueError("Maximum PDF size is 10 MB")
    run_id=str(uuid.uuid4())
    digest=hashlib.sha256(pdf).hexdigest()
    repo.record_run(run_id,digest,Path(filename).name[:160])
    repo.store_document(digest,pdf)
    events=[]
    begin=time.perf_counter()
    def event(stage,detail):
        x={"stage":stage,"detail":detail,"elapsed_ms":int((time.perf_counter()-begin)*1000)}
        events.append(x)
        repo.update_run(run_id,events_json=json.dumps(events))
        if on_event: on_event(x)
    try:
        event("intake","PDF received; hash computed")
        # Version and backend identity prevent mixing fixture and model results.
        identity = ["v3", digest, mode, os.getenv("OPENAI_COMPATIBLE_BASE_URL", ""),
                    os.getenv("OPENAI_COMPATIBLE_MODEL", ""), os.getenv("OCR_ENABLED", "0")]
        cache_key = hashlib.sha256(json.dumps(identity).encode()).hexdigest()
        cached = repo.cached_extraction(cache_key)
        if cached:
            invoice = Invoice.model_validate_json(cached["invoice_json"])
            backend = cached["backend"]
            event("extract", f"Immutable extraction cache hit ({backend}); finance checks run fresh")
        else:
            doc=parse_document(pdf,enable_ocr=os.getenv("OCR_ENABLED")=="1")
            event("parse",f"Parsed {doc.page_count} page(s), scanned={doc.scanned}")
            invoice,backend=extract_invoice(pdf,doc,mode=mode)
            repo.cache_extraction(cache_key,invoice,backend)
            event("extract",f"Invoice extracted using {backend}")
        repo.update_run(run_id, invoice_json=invoice.model_dump_json())
        state={"invoice":invoice,"digest":digest,"repo":repo,"policy_dir":str(policy_dir),"evidence":[]}
        if graph_enabled is None:
            graph_enabled=os.getenv("AGENT_ENABLED","1")=="1"
        if graph_enabled:
            try:
                graph=build_graph()
            except ImportError:
                graph=None
        else:
            graph=None
        if graph is not None:
            event("orchestrate","Executing LangGraph routed workflow")
            output=graph.invoke(state, {"recursion_limit": 8})
            decision=output["decision"]
            if output.get("evidence"): event("retrieve","Retrieved policy and PO candidate evidence")
        else:
            branch=router(state)
            event("route",f"Routed to {branch} path (Python fallback)")
            if branch=="exception":
                state.update(exception_node(state))
                event("retrieve","Policy retrieval and PO suggestions completed")
            decision=decision_node(state)["decision"]
        event("decision",f"Final: {decision.status}; {len(decision.findings)} finding(s)")
        repo.update_run(run_id,status=decision.status,invoice_json=invoice.model_dump_json(),decision_json=decision.model_dump_json(),events_json=json.dumps(events))
        return {"run_id":run_id,"invoice":invoice,"decision":decision,"events":events,"backend":backend}
    except Exception as exc:
        event("failure",f"{type(exc).__name__}: {exc}")
        repo.update_run(run_id,status="ERROR",error=f"{type(exc).__name__}: {exc}",events_json=json.dumps(events))
        raise


def review_and_recheck(repo: Repository, prior_run_id: str, corrected: dict, reviewer: str="demo_reviewer"):
    """Human-edited correction creates a separate auditable run and reapplies ALL rules.
    Never overwrites source run, and never grants a policy bypass. Demo only: no auth.
    """
    old=repo.get_run(prior_run_id)
    if not old or old['status']!='NEEDS_REVIEW':
        raise ValueError('Only an existing NEEDS_REVIEW run can be corrected')
    if not reviewer or len(reviewer)>100:
        raise ValueError('Reviewer identifier required')
    invoice=Invoice.model_validate(corrected)
    new_id=str(uuid.uuid4())
    repo.record_run(new_id,old['document_hash'],f"review-of-{prior_run_id}")
    try:
        decision=evaluate_invoice(repo,invoice,old['document_hash'])
        decision.evidence={"parent_run_id":prior_run_id,"reviewer":reviewer,"correction":"Manually edited; full finance policy re-evaluated"}
        events=[{"stage":"human_correction","detail":f"Reviewer {reviewer} edited {prior_run_id}; all rules rechecked","elapsed_ms":0},
                {"stage":"decision","detail":f"Final: {decision.status}","elapsed_ms":0}]
        repo.update_run(new_id,status=decision.status,invoice_json=invoice.model_dump_json(),decision_json=decision.model_dump_json(),events_json=json.dumps(events))
        return {"run_id":new_id,"invoice":invoice,"decision":decision,"events":events,"backend":"human_correction"}
    except Exception as exc:
        repo.update_run(new_id,status='ERROR',error=f'{type(exc).__name__}: {exc}')
        raise
