from __future__ import annotations
import sqlite3
import os
import threading
import time
import psycopg
from psycopg import pq
from psycopg.rows import dict_row
from dotenv import load_dotenv
from contextlib import contextmanager
from pathlib import Path

load_dotenv(Path(__file__).resolve().parents[1] / ".env", override=False)

SCHEMA = """
PRAGMA foreign_keys=ON;
CREATE TABLE IF NOT EXISTS vendors (
 id TEXT PRIMARY KEY, name TEXT NOT NULL, tax_id TEXT UNIQUE, approved INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS purchase_orders (
 id TEXT PRIMARY KEY, vendor_id TEXT NOT NULL REFERENCES vendors(id), currency TEXT NOT NULL,
 status TEXT NOT NULL DEFAULT 'OPEN', cap TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS po_lines (
 id TEXT PRIMARY KEY, po_id TEXT NOT NULL REFERENCES purchase_orders(id),
 sku TEXT, description TEXT NOT NULL, qty TEXT NOT NULL, unit_price TEXT NOT NULL, amount TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS invoices (
 id INTEGER PRIMARY KEY AUTOINCREMENT, vendor_id TEXT NOT NULL REFERENCES vendors(id),
 invoice_number TEXT NOT NULL, document_hash TEXT NOT NULL, invoice_total TEXT NOT NULL,
 po_id TEXT REFERENCES purchase_orders(id), status TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 UNIQUE(vendor_id, invoice_number)
);
CREATE TABLE IF NOT EXISTS allocations (
 invoice_id INTEGER NOT NULL REFERENCES invoices(id), po_line_id TEXT NOT NULL REFERENCES po_lines(id),
 qty TEXT NOT NULL, amount TEXT NOT NULL, PRIMARY KEY(invoice_id, po_line_id)
);
CREATE TABLE IF NOT EXISTS runs (
 id TEXT PRIMARY KEY, document_hash TEXT NOT NULL, filename TEXT NOT NULL, created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
 status TEXT NOT NULL, invoice_json TEXT, decision_json TEXT, events_json TEXT NOT NULL DEFAULT '[]', error TEXT
);
CREATE TABLE IF NOT EXISTS documents (digest TEXT PRIMARY KEY, pdf BLOB NOT NULL);
CREATE TABLE IF NOT EXISTS extraction_cache (
 cache_key TEXT PRIMARY KEY, invoice_json TEXT NOT NULL, backend TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS vector_cache (
 cache_key TEXT PRIMARY KEY, vector_json TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_runs_created ON runs(created_at);
CREATE INDEX IF NOT EXISTS idx_invoice_hash ON invoices(document_hash);
CREATE INDEX IF NOT EXISTS idx_po_vendor ON purchase_orders(vendor_id);
"""

# Separate schema keeps backend financial data outside the default Supabase Data API.
POSTGRES_SCHEMA = (SCHEMA.replace("PRAGMA foreign_keys=ON;", "")
                   .replace("INTEGER PRIMARY KEY AUTOINCREMENT", "BIGSERIAL PRIMARY KEY")
                   .replace("invoice_id INTEGER", "invoice_id BIGINT")
                   .replace("pdf BLOB", "pdf BYTEA")
                   .replace("created_at TEXT", "created_at TIMESTAMPTZ"))


SESSION_SETTINGS = (
    "SET search_path TO invoiceiq",
    "SET lock_timeout = '15s'",
    "SET statement_timeout = '30s'",
)


class PostgresConnection:
    """Small DB-API bridge for this repository's fixed, parameterized SQL.
    `with` commits on success and rolls back on error, then hands the connection back via `release`."""
    def __init__(self, connection, release=None):
        self.raw = connection
        self._release = release
        self._open = True

    def execute(self, sql, parameters=()):
        return self.raw.execute(sql.replace("?", "%s"), parameters)

    def executemany(self, sql, parameters):
        with self.raw.cursor() as cursor:
            cursor.executemany(sql.replace("?", "%s"), parameters)

    def __enter__(self):
        return self

    def __exit__(self, exc_type, *rest):
        try:
            if exc_type is None:
                self.raw.commit()
            else:
                self.raw.rollback()
        finally:
            self.close()
        return False

    def commit(self):
        self.raw.commit()

    def rollback(self):
        self.raw.rollback()

    def close(self):
        if not self._open:
            return
        self._open = False
        if self._release:
            self._release(self.raw)
        else:
            self.raw.close()


class PostgresPool:
    """Reuses open connections: a new TLS connection to Supabase costs ~1-2 s, a query on an open
    one ~0.15 s, and one invoice run makes ~15 database calls. Thread-safe; broken or
    mid-transaction connections are discarded, never reused."""
    MAX_IDLE = 4
    RECHECK_AFTER = 60  # seconds idle before a liveness check (poolers drop idle clients)

    def __init__(self, open_connection):
        self._open_connection = open_connection
        self._idle = []
        self._lock = threading.Lock()

    def borrow(self):
        while True:
            with self._lock:
                item = self._idle.pop() if self._idle else None
            if item is None:
                return self._open_connection()
            raw, since = item
            if time.monotonic() - since < self.RECHECK_AFTER:
                return raw
            try:
                raw.execute("SELECT 1")
                raw.rollback()
                return raw
            except Exception:
                self._discard(raw)

    def release(self, raw):
        try:
            healthy = (not raw.closed and not raw.broken
                       and raw.info.transaction_status == pq.TransactionStatus.IDLE)
        except Exception:
            healthy = False
        if healthy:
            with self._lock:
                if len(self._idle) < self.MAX_IDLE:
                    self._idle.append((raw, time.monotonic()))
                    return
        self._discard(raw)

    @staticmethod
    def _discard(raw):
        try:
            raw.close()
        except Exception:
            pass


class ClosingConnection(sqlite3.Connection):
    def __exit__(self, *args):
        try:
            return super().__exit__(*args)
        finally:
            self.close()


class Repository:
    def __init__(self, path: str = "invoiceiq.sqlite"):
        self.path = str(path)
        self.database_url = os.getenv("SUPABASE_DB_URL", "").strip()
        self.postgres = bool(self.database_url)
        if self.postgres and not self.database_url.startswith(("postgresql://", "postgres://")):
            raise ValueError("SUPABASE_DB_URL must be a PostgreSQL connection string (postgresql://...) "
                             "from Supabase > Connect > Session pooler, not the https:// project URL")
        self.integrity_errors = (sqlite3.IntegrityError, psycopg.IntegrityError)
        if self.postgres:
            self.pool = PostgresPool(self._open_postgres)
        else:
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.initialize()

    def _open_postgres(self):
        raw = psycopg.connect(self.database_url, sslmode="require", connect_timeout=15,
                              row_factory=dict_row, prepare_threshold=None)
        # Set per session with SQL, not the `options` startup parameter: Supabase's Supavisor
        # pooler silently drops startup options, which would put every table in `public`.
        for statement in SESSION_SETTINGS:
            raw.execute(statement)
        raw.commit()
        return raw

    def connect(self):
        if self.postgres:
            return PostgresConnection(self.pool.borrow(), release=self.pool.release)
        c = sqlite3.connect(self.path, timeout=15, factory=ClosingConnection)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        return c

    def initialize(self):
        with self.connect() as c:
            if self.postgres:
                c.execute("SELECT pg_advisory_xact_lock(83749102)")
                c.execute("CREATE SCHEMA IF NOT EXISTS invoiceiq")
                for statement in POSTGRES_SCHEMA.split(";"):
                    if statement.strip():
                        c.execute(statement)
            else:
                c.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        c = self.connect()
        try:
            if self.postgres:
                # Transaction lock spans fresh checks and allocations across all workers.
                # READ COMMITTED sees approvals committed by the previous lock holder.
                c.execute("SET TRANSACTION ISOLATION LEVEL READ COMMITTED")
                c.execute("SELECT pg_advisory_xact_lock(83749102)")
            else:
                c.execute("BEGIN IMMEDIATE")
            yield c
            c.commit()
        except Exception:
            c.rollback()
            raise
        finally:
            c.close()

    def seed(self):
        with self.transaction() as c:
            c.executemany("INSERT INTO vendors(id,name,tax_id,approved) VALUES(?,?,?,?) ON CONFLICT DO NOTHING", [
                ("V001", "Acme Supplies Pvt Ltd", "TAX-ACME", 1),
                ("V002", "Nova Office Systems", "TAX-NOVA", 1),
                ("V003", "Unknown Vendor", "TAX-UNKNOWN", 0)
            ])
            c.executemany("INSERT INTO purchase_orders(id,vendor_id,currency,status,cap) VALUES(?,?,?,?,?) ON CONFLICT DO NOTHING", [
                ("PO-1007", "V001", "INR", "OPEN", "100000.00"),
                ("PO-2001", "V002", "INR", "OPEN", "60000.00"),
                ("PO-2002", "V002", "INR", "OPEN", "61000.00")
            ])
            c.executemany("INSERT INTO po_lines(id,po_id,sku,description,qty,unit_price,amount) VALUES(?,?,?,?,?,?,?) ON CONFLICT DO NOTHING", [
                ("L1007A", "PO-1007", "CHAIR-B24", "Ergonomic office chair mesh", "40", "2500", "100000"),
                ("L2001A", "PO-2001", "DESK-01", "Adjustable workstation desk", "10", "6000", "60000"),
                ("L2002A", "PO-2002", "DESK-02", "Adjustable work desk", "10", "6100", "61000")
            ])

    def reset_demo(self):
        """Delete all processed invoices, allocations and runs, then re-seed. Synthetic demo data only."""
        with self.transaction() as c:
            for table in ("allocations", "invoices", "runs", "documents", "extraction_cache"):
                c.execute(f"DELETE FROM {table}")
        self.seed()

    def record_run(self, run_id, digest, filename, status="PROCESSING"):
        with self.connect() as c:
            c.execute("INSERT INTO runs(id,document_hash,filename,status) VALUES(?,?,?,?)", (run_id,digest,filename,status))

    def update_run(self, run_id, **fields):
        allowed = {"status", "invoice_json", "decision_json", "events_json", "error"}
        if not fields or not set(fields) <= allowed:
            raise ValueError("Unknown run update fields")
        sql = ",".join(f"{key}=?" for key in fields)
        with self.connect() as c:
            c.execute(f"UPDATE runs SET {sql} WHERE id=?", (*fields.values(),run_id))

    def runs(self, limit=100):
        with self.connect() as c:
            tie_breaker = "id" if self.postgres else "rowid"
            return [dict(r) for r in c.execute(f"SELECT * FROM runs ORDER BY created_at DESC, {tie_breaker} DESC LIMIT ?", (limit,))]

    def get_run(self, run_id):
        with self.connect() as c:
            row=c.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
            return dict(row) if row else None

    def store_document(self, digest, pdf):
        with self.connect() as c:
            c.execute("INSERT INTO documents VALUES(?,?) ON CONFLICT DO NOTHING", (digest, pdf))

    def document(self, digest):
        with self.connect() as c:
            row = c.execute("SELECT pdf FROM documents WHERE digest=?", (digest,)).fetchone()
            return bytes(row["pdf"]) if row else None

    def cached_extraction(self, key):
        with self.connect() as c:
            row = c.execute("SELECT * FROM extraction_cache WHERE cache_key=?", (key,)).fetchone()
            return dict(row) if row else None

    def cache_extraction(self, key, invoice, backend):
        with self.connect() as c:
            c.execute("INSERT INTO extraction_cache VALUES(?,?,?) ON CONFLICT DO NOTHING", (key, invoice.model_dump_json(), backend))
