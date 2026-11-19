"""Policy RAG: heading-aware chunks + BM25-like keyword scoring with optional HF dense embeddings.
For small prototype corpora, cosine over all chunks avoids vector DB complexity.
"""
from __future__ import annotations
import hashlib
import json
import math
import os
import re
import httpx
from pathlib import Path
from functools import lru_cache


def tokens(text):
    return re.findall(r"[\w-]+",text.casefold())


def chunk_policy(text: str, source: str, max_words: int=180) -> list[dict]:
    if max_words < 1:
        raise ValueError("max_words must be positive")
    chunks=[]
    heading="Overview"
    buf=[]
    def flush():
        if buf:
            chunks.append({"source":source,"section":heading,"text":heading+"\n"+" ".join(buf)})
    for line in text.splitlines():
        line=line.strip()
        if not line:
            continue
        if line.startswith("#"):
            flush();buf=[];heading=line.lstrip("# ").strip()
        else:
            words=line.split()
            while words:
                room=max_words-len(buf)
                if room<=0:
                    flush();buf=[];room=max_words
                buf.extend(words[:room]);words=words[room:]
    flush()
    return chunks


@lru_cache(maxsize=2)
def embedding_model(name):
    from sentence_transformers import SentenceTransformer
    return SentenceTransformer(name)


def remote_embeddings(texts, model):
    """Hosted OpenAI-compatible dense embeddings, without local model weights."""
    url = os.getenv("EMBEDDING_API_URL", "").strip()
    key = os.getenv("EMBEDDING_API_KEY", "").strip()
    if not url.startswith("https://"):
        raise RuntimeError("Set EMBEDDING_API_URL to the hosted HTTPS /v1/embeddings endpoint")
    if not key:
        raise RuntimeError("Set EMBEDDING_API_KEY for hosted embeddings")
    results = []
    with httpx.Client(timeout=60) as client:
        for start in range(0, len(texts), 32):
            batch = texts[start:start+32]
            response = client.post(url, headers={"Authorization": f"Bearer {key}"},
                                   json={"model": model, "input": batch, "encoding_format": "float"})
            response.raise_for_status()
            rows = response.json().get("data", [])
            if (not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows)
                    or len(rows) != len(batch) or any(type(row.get("index")) is not int for row in rows)
                    or sorted(row["index"] for row in rows) != list(range(len(batch)))):
                raise ValueError("Embedding response has missing or invalid input indices")
            for row in sorted(rows, key=lambda row: row["index"]):
                vector = row.get("embedding")
                if not isinstance(vector, list) or not vector:
                    raise ValueError("Embedding response must contain dense vectors")
                if model == "BAAI/bge-m3" and len(vector) != 1024:
                    raise ValueError("BGE-M3 endpoint must return 1024-dimensional vectors")
                if any(type(value) not in (float, int) or not math.isfinite(value) for value in vector):
                    raise ValueError("Embedding vector contains invalid values")
                norm = math.sqrt(sum(value*value for value in vector))
                if not math.isfinite(norm) or norm == 0:
                    raise ValueError("Embedding vector has invalid norm")
                results.append([value / norm for value in vector])
    return results


def encoded(texts):
    """Persistent content-addressed embeddings; encode only new content in one batch."""
    from .database import Repository
    repo = Repository(os.getenv("INVOICEIQ_DB", "invoiceiq.sqlite"))
    name = os.getenv("EMBEDDING_MODEL", "BAAI/bge-m3")
    backend = os.getenv("EMBEDDING_BACKEND", "local")
    if backend not in {"local", "remote"}:
        raise ValueError("EMBEDDING_BACKEND must be local or remote")
    identity = json.dumps(["embedding-v2", backend, name,
                           os.getenv("EMBEDDING_API_URL", "") if backend == "remote" else ""])
    keys = [hashlib.sha256((identity + "\0" + text).encode()).hexdigest() for text in texts]
    vectors = {}
    with repo.connect() as c:
        for key in set(keys):
            row = c.execute("SELECT vector_json FROM vector_cache WHERE cache_key=?", (key,)).fetchone()
            if row:
                vectors[key] = json.loads(row["vector_json"])
    missing = list(dict.fromkeys(key for key in keys if key not in vectors))
    if missing:
        lookup = dict(zip(keys, texts))
        pending = [lookup[key] for key in missing]
        values = (remote_embeddings(pending, name) if backend == "remote" else
                  embedding_model(name).encode(pending, normalize_embeddings=True))
        with repo.connect() as c:
            for key, value in zip(missing, values):
                vectors[key] = [float(item) for item in value]
                c.execute("INSERT INTO vector_cache VALUES(?,?) ON CONFLICT DO NOTHING", (key, json.dumps(vectors[key])))
    return [vectors[key] for key in keys]


@lru_cache(maxsize=1)
def reranker(name):
    from sentence_transformers import CrossEncoder
    return CrossEncoder(name)


class PolicySearch:
    def __init__(self, policy_dir: str|Path, use_embeddings: bool=False):
        self.chunks=[]
        for f in sorted(Path(policy_dir).glob("*.md")):
            self.chunks.extend(chunk_policy(f.read_text(encoding="utf-8"),f.name))
        self.use_embeddings=use_embeddings
        self.embeddings=None
        if use_embeddings and self.chunks:
            self.embeddings=encoded([c["text"] for c in self.chunks])

    def search(self, query: str, k: int=3) -> list[dict]:
        q=set(tokens(query))
        lexical=[]
        for c in self.chunks:
            term=tokens(c["text"])
            tf=sum(min(term.count(w),3) for w in q)
            lexical.append(tf / math.sqrt(max(1,len(term))))
        dense=[0.0]*len(self.chunks)
        if self.use_embeddings and self.embeddings is not None:
            v=encoded([query])[0]
            dense=[sum(a*b for a,b in zip(row,v)) for row in self.embeddings]
        # rank fusion, rather than adding scores in incompatible units.
        def ranks(values):
            return {idx:rank for rank,idx in enumerate(sorted(range(len(values)),key=lambda i:values[i],reverse=True))}
        lrank=ranks(lexical)
        drank=ranks(dense)
        scores=[1/(60+lrank[i]+1)+(1/(60+drank[i]+1) if self.use_embeddings else 0) for i in range(len(self.chunks))]
        order=sorted(range(len(scores)),key=lambda i:scores[i],reverse=True)
        results = [dict(self.chunks[i],score=round(scores[i],5)) for i in order[:max(k,10)] if lexical[i]>0 or (self.use_embeddings and dense[i]>0)]
        name = os.getenv("RERANKER_MODEL")
        if name and results:
            values = reranker(name).predict([(query, c["text"]) for c in results])
            results = [dict(c, rerank_score=float(score)) for c,score in sorted(zip(results,values), key=lambda pair:float(pair[1]), reverse=True)]
        return results[:k]


def match_po_line(invoice_line, po_lines: list[dict], use_embeddings=False):
    """Exact SKU match wins. Descriptions only produce suggestions, never proof of identity."""
    if invoice_line.sku:
        matches=[l for l in po_lines if l["sku"] and l["sku"].casefold()==invoice_line.sku.casefold()]
        if len(matches)==1:
            return matches[0], "sku_verified"
    words=set(tokens(invoice_line.description))
    if not words:
        return None,"unmatched"
    scored=[]
    for line in po_lines:
        other=set(tokens(line["description"]))
        scored.append((len(words&other)/max(1,len(words|other)),line))
    exact = [line for line in po_lines if line["description"].strip().casefold() == invoice_line.description.strip().casefold()]
    if len(exact) == 1:
        return exact[0], "description_candidate"
    if use_embeddings and po_lines:
        em=encoded([invoice_line.description]+[l["description"] for l in po_lines])
        sims=[sum(a*b for a,b in zip(row,em[0])) for row in em[1:]]
        scored=[(float(sim),line) for sim,line in zip(sims,po_lines)]
    scored.sort(key=lambda x:x[0],reverse=True)
    # Semantics alone must be confirmed by a human.
    return (scored[0][1], "semantic_candidate") if scored and scored[0][0]>0.1 else (None,"unmatched")
