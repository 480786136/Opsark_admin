import os
from pathlib import Path
import subprocess
import sys
from sqlalchemy import create_engine, inspect, text


def test_upgrade_existing_platform_keeps_admins(tmp_path):
    url = "sqlite:///" + (tmp_path / "upgrade.db").as_posix()
    engine = create_engine(url)
    with engine.begin() as c:
        c.execute(
            text(
                "CREATE TABLE admins (id VARCHAR(64) PRIMARY KEY, username VARCHAR(100) UNIQUE NOT NULL, password_hash TEXT NOT NULL)"
            )
        )
        c.execute(
            text(
                "CREATE TABLE sessions (token_hash VARCHAR(64) PRIMARY KEY, admin_id VARCHAR(64) NOT NULL REFERENCES admins(id), csrf VARCHAR(100) NOT NULL, expires FLOAT NOT NULL)"
            )
        )
        c.execute(text("INSERT INTO admins VALUES ('existing','existing','synthetic-hash')"))
    environment = {**os.environ, "DATABASE_URL": url}
    root = Path(__file__).resolve().parents[1]
    for args in [("stamp", "0001"), ("upgrade", "head"), ("upgrade", "head")]:
        subprocess.run(
            [sys.executable, "-m", "alembic", *args], cwd=root, env=environment, check=True, capture_output=True
        )
    with engine.connect() as c:
        assert c.execute(text("SELECT password_hash FROM admins WHERE id='existing'")).scalar() == "synthetic-hash"
    assert {"model_providers", "model_routes", "model_keys", "model_calls", "model_call_details",
            "user_accounts", "user_sessions", "registration_policy", "credit_accounts", "credit_ledger",
            "model_reservations", "account_audit"}.issubset(
        inspect(engine).get_table_names()
    )
    assert "parameter_defaults" in {column["name"] for column in inspect(engine).get_columns("model_providers")}
    assert "parameter_overrides" in {column["name"] for column in inspect(engine).get_columns("model_routes")}
    engine.dispose()


def test_fresh_database_runs_every_migration_once(tmp_path):
    from app.models import Admin
    from alembic.config import Config
    from alembic.script import ScriptDirectory
    url = "sqlite:///" + (tmp_path / "fresh.db").as_posix()
    root = Path(__file__).resolve().parents[1]
    for _ in range(2):
        subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"],
            cwd=root, env={**os.environ, "DATABASE_URL": url}, check=True, capture_output=True)
    engine = create_engine(url)
    inspector = inspect(engine)
    assert set(Admin.metadata.tables).issubset(inspector.get_table_names())
    for name, table in Admin.metadata.tables.items():
        assert set(table.columns.keys()) == {column["name"] for column in inspector.get_columns(name)}
    with engine.connect() as connection:
        expected_head = ScriptDirectory.from_config(Config(str(root / "alembic.ini"))).get_current_head()
        assert connection.execute(text("SELECT version_num FROM alembic_version")).scalar() == expected_head
        assert connection.execute(text("SELECT count(*) FROM user_accounts")).scalar() == 0
    engine.dispose()


def test_call_correlation_upgrade_preserves_history_and_uses_reservations_for_identity(tmp_path):
    url = "sqlite:///" + (tmp_path / "call-correlation.db").as_posix()
    root = Path(__file__).resolve().parents[1]
    environment = {**os.environ, "DATABASE_URL": url}
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "0008"], cwd=root, env=environment, check=True, capture_output=True)
    engine = create_engine(url)
    with engine.begin() as connection:
        connection.execute(text("INSERT INTO user_accounts (id,email,password_hash,disabled,created_at) VALUES ('u','test@example.test','unused',0,1000)"))
        for cid in ("official-call", "key-call"):
            connection.execute(text("INSERT INTO model_calls (id,key_id,owner,provider_id,model,started_at,status) VALUES (:id,'s','u','p','m',1000,'succeeded')"), {"id": cid})
        connection.execute(text("INSERT INTO model_reservations (id,user_id,idempotency_key,request_hash,amount,actual,state,created_at) VALUES ('official-call','u','synthetic-operation','hash',10,10,'settled',1000)"))
    subprocess.run([sys.executable, "-m", "alembic", "upgrade", "head"], cwd=root, env=environment, check=True, capture_output=True)
    with engine.connect() as connection:
        records = connection.execute(text("SELECT id,user_id,task_id,status FROM model_calls ORDER BY id")).all()
        assert records == [("key-call", None, None, "succeeded"), ("official-call", "u", None, "succeeded")]
    engine.dispose()
