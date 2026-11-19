"""Adapter tests use an explicit connection spy; they are not live Supabase tests."""
from unittest.mock import MagicMock
from invoiceiq.database import Repository, PostgresConnection


def test_postgres_parameter_binding():
    raw = MagicMock()
    connection = PostgresConnection(raw)
    value = "untrusted ? ' text"
    connection.execute("SELECT * FROM vendors WHERE name=?", (value,))
    raw.execute.assert_called_once_with("SELECT * FROM vendors WHERE name=%s", (value,))


def test_supabase_initialization_and_transaction_lock(monkeypatch):
    from invoiceiq import database
    raw = MagicMock()
    connect = MagicMock(return_value=raw)
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://user:secret@example.invalid/postgres")
    monkeypatch.setattr(database.psycopg, "connect", connect)
    repo = Repository()
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
    raw.close.assert_called_once()


def test_supabase_transaction_rolls_back(monkeypatch):
    import pytest
    from invoiceiq import database
    raw = MagicMock()
    monkeypatch.setenv("SUPABASE_DB_URL", "postgresql://user:secret@example.invalid/postgres")
    monkeypatch.setattr(database.psycopg, "connect", lambda *args, **kwargs: raw)
    repo = Repository()
    raw.reset_mock()
    with pytest.raises(RuntimeError):
        with repo.transaction():
            raise RuntimeError("injected failure")
    raw.rollback.assert_called_once()
    raw.commit.assert_not_called()
    raw.close.assert_called_once()
