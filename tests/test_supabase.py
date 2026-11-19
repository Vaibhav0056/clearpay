"""Adapter tests use an explicit connection spy; they are not live Supabase tests."""
from unittest.mock import MagicMock
import pytest
from psycopg import pq
from invoiceiq.database import Repository, PostgresConnection, PostgresPool

URL = "postgresql://user:secret@example.invalid/postgres"


def healthy_connection():
    raw = MagicMock()
    raw.closed = False
    raw.broken = False
    raw.info.transaction_status = pq.TransactionStatus.IDLE
    return raw


def postgres_repo(monkeypatch, raw):
    from invoiceiq import database
    connect = MagicMock(return_value=raw)
    monkeypatch.setenv("SUPABASE_DB_URL", URL)
    monkeypatch.setattr(database.psycopg, "connect", connect)
    return Repository(), connect


def test_postgres_parameter_binding():
    raw = MagicMock()
    connection = PostgresConnection(raw)
    value = "untrusted ? ' text"
    connection.execute("SELECT * FROM vendors WHERE name=?", (value,))
    raw.execute.assert_called_once_with("SELECT * FROM vendors WHERE name=%s", (value,))


def test_rejects_project_https_url(monkeypatch):
    monkeypatch.setenv("SUPABASE_DB_URL", "https://example.supabase.co")
    with pytest.raises(ValueError, match="Session pooler"):
        Repository()


def test_supabase_initialization_and_transaction_lock(monkeypatch):
    raw = healthy_connection()
    repo, connect = postgres_repo(monkeypatch, raw)
    assert repo.postgres
    assert connect.call_args.kwargs["sslmode"] == "require"
    statements = [call.args[0] for call in raw.execute.call_args_list]
    assert "CREATE SCHEMA IF NOT EXISTS invoiceiq" in statements
    assert any("BIGSERIAL PRIMARY KEY" in sql for sql in statements)
    assert any("pdf BYTEA" in sql for sql in statements)
    assert not any("AUTOINCREMENT" in sql or "PRAGMA" in sql for sql in statements)
    raw.reset_mock()
    with repo.transaction() as connection:
        connection.execute("SELECT id FROM vendors WHERE id=?", ("V001",))
    statements = [call.args[0] for call in raw.execute.call_args_list]
    assert statements[0] == "SET TRANSACTION ISOLATION LEVEL READ COMMITTED"
    assert statements[1] == "SELECT pg_advisory_xact_lock(83749102)"
    raw.commit.assert_called_once()
    raw.close.assert_not_called()  # returned to the pool, not closed


def test_schema_is_set_by_sql_not_startup_options(monkeypatch):
    # Supabase's pooler ignores the `options` startup parameter; relying on it put tables in `public`.
    raw = healthy_connection()
    _, connect = postgres_repo(monkeypatch, raw)
    assert "options" not in connect.call_args.kwargs
    statements = [call.args[0] for call in raw.execute.call_args_list]
    assert statements[0] == "SET search_path TO invoiceiq"
    assert statements.index("SET search_path TO invoiceiq") < statements.index("CREATE SCHEMA IF NOT EXISTS invoiceiq")


def test_connections_are_reused(monkeypatch):
    raw = healthy_connection()
    repo, connect = postgres_repo(monkeypatch, raw)
    for _ in range(5):
        with repo.connect() as connection:
            connection.execute("SELECT 1")
    with repo.transaction() as connection:
        connection.execute("SELECT 1")
    assert connect.call_count == 1


def test_supabase_transaction_rolls_back(monkeypatch):
    raw = healthy_connection()
    repo, _ = postgres_repo(monkeypatch, raw)
    raw.reset_mock()
    with pytest.raises(RuntimeError):
        with repo.transaction():
            raise RuntimeError("injected failure")
    raw.rollback.assert_called_once()
    raw.commit.assert_not_called()


def test_connect_context_rolls_back_on_error(monkeypatch):
    raw = healthy_connection()
    repo, _ = postgres_repo(monkeypatch, raw)
    raw.reset_mock()
    with pytest.raises(RuntimeError):
        with repo.connect():
            raise RuntimeError("injected failure")
    raw.rollback.assert_called_once()
    raw.commit.assert_not_called()


def test_broken_or_mid_transaction_connections_are_discarded():
    opened = []
    def open_connection():
        opened.append(healthy_connection())
        return opened[-1]
    pool = PostgresPool(open_connection)
    first = pool.borrow()
    first.broken = True
    pool.release(first)
    first.close.assert_called_once()
    second = pool.borrow()
    assert second is not first
    second.info.transaction_status = pq.TransactionStatus.INTRANS
    pool.release(second)
    second.close.assert_called_once()
    assert len(opened) == 2


def test_stale_idle_connection_is_rechecked_and_replaced(monkeypatch):
    from invoiceiq import database
    clock = [1000.0]
    monkeypatch.setattr(database.time, "monotonic", lambda: clock[0])
    opened = []
    def open_connection():
        opened.append(healthy_connection())
        return opened[-1]
    pool = PostgresPool(open_connection)
    stale = pool.borrow()
    pool.release(stale)
    clock[0] += PostgresPool.RECHECK_AFTER + 1
    stale.execute.side_effect = OSError("server closed the connection")
    fresh = pool.borrow()
    assert fresh is not stale and len(opened) == 2
    stale.close.assert_called_once()
