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
    assert {"model_providers", "model_routes", "model_keys", "model_calls"}.issubset(inspect(engine).get_table_names())
    engine.dispose()
