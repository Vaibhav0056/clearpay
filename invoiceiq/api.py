"""Optional REST API: uvicorn invoiceiq.api:app --host 0.0.0.0 --port 8000
Exposes a PDF byte-stream endpoint; protect with authentication before public deployment.
"""
from __future__ import annotations
import os
from functools import lru_cache
from pathlib import Path
from fastapi import FastAPI, Request, Header, HTTPException
from fastapi.responses import Response
from starlette.concurrency import run_in_threadpool
from .database import Repository
from .workflow import process_pdf, review_and_recheck

app=FastAPI(title="ClearPay API",version="0.1.0")
ROOT=Path(__file__).resolve().parent.parent

@lru_cache(maxsize=4)
def _repository(path: str, database_url: str) -> Repository:
    # One repository (and Postgres connection pool) per configured database, shared by requests.
    return Repository(path)


def database():
    return _repository(os.getenv("INVOICEIQ_DB",str(ROOT/"invoiceiq.sqlite")), os.getenv("SUPABASE_DB_URL",""))

@app.get("/health")
def health():
    return {"ok":True,"service":"invoiceiq"}

@app.get("/runs")
def runs(limit: int=50):
    return database().runs(min(100,max(1,limit)))

@app.get("/runs/{run_id}")
def run(run_id: str):
    result=database().get_run(run_id)
    if result is None:
        raise HTTPException(status_code=404,detail="Unknown run")
    return result

@app.post("/process")
async def process(request: Request, x_filename: str = Header(default="invoice.pdf")):
    if request.headers.get("content-type", "").split(";")[0] != "application/pdf":
        raise HTTPException(status_code=415,detail="Expected application/pdf binary body")
    payload=bytearray()
    async for chunk in request.stream():
        payload.extend(chunk)
        if len(payload)>10*1024*1024:
            raise HTTPException(status_code=413, detail="Maximum PDF size is 10 MB")
    payload=bytes(payload)
    try:
        result=await run_in_threadpool(process_pdf,payload,x_filename,database(),policy_dir=ROOT/"fixtures/policies")
        return {"run_id":result["run_id"],"invoice":result["invoice"].model_dump(mode="json"),"decision":result["decision"].model_dump(mode="json"),"events":result["events"],"backend":result["backend"]}
    except ValueError as exc:
        raise HTTPException(status_code=400,detail=str(exc)) from exc
    except Exception:
        # No stack traces or credentials leak to callers. Details are in run history.
        raise HTTPException(status_code=503,detail="Processing failed; inspect stored run history")


@app.get("/runs/{run_id}/pdf")
def pdf(run_id: str):
    repo = database()
    record = repo.get_run(run_id)
    data = repo.document(record["document_hash"]) if record else None
    if data is None:
        raise HTTPException(status_code=404, detail="PDF unavailable")
    return Response(data, media_type="application/pdf")


@app.post("/runs/{run_id}/review")
def review(run_id: str, corrected: dict, x_reviewer: str = Header(default="demo_reviewer")):
    try:
        result = review_and_recheck(database(), run_id, corrected, x_reviewer)
        return {"run_id":result["run_id"], "decision":result["decision"].model_dump(mode="json")}
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
