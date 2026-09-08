import pytest
from argon2 import PasswordHasher
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from app.main import app
from app.db import Base, get_db
from app.models import Admin
from app.config import settings
from app.security import limiter


@pytest.fixture
def env(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'platform.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine)
    with factory() as db:
        db.add(Admin(username="test", password_hash=PasswordHasher().hash("test-password-only")))
        db.commit()

    def database():
        with factory() as db:
            yield db

    app.dependency_overrides[get_db] = database
    monkeypatch.setattr(settings(), "knowledge_service_token", "t" * 40)
    limiter.entries.clear()
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()
    engine.dispose()


def auth(client):
    result = client.post("/api/admin/v1/session", json={"username": "test", "password": "test-password-only"})
    assert result.status_code == 200, result.text
    assert "httponly" in result.headers["set-cookie"].lower()
    return {"X-CSRF-Token": result.json()["csrf"]}


def test_login_csrf_and_logout(env):
    assert env.get("/api/admin/v1/config").status_code == 401
    headers = auth(env)
    assert env.get("/api/admin/v1/config").status_code == 200
    assert env.delete("/api/admin/v1/session").status_code == 403
    assert env.delete("/api/admin/v1/session", headers={**headers, "Origin": "https://evil.example"}).status_code == 403
    assert env.delete("/api/admin/v1/session", headers=headers).status_code == 200
    assert env.get("/api/admin/v1/session").status_code == 401


def test_knowledge_management_has_moved(env):
    auth(env)
    response = env.get("/api/admin/v1/knowledge/records")
    assert response.status_code == 410
    assert response.json()["error"]["code"] == "KNOWLEDGE_MOVED"
    assert env.get("/api/admin/v1/session").status_code == 200
    assert "knowledge_service_token" not in env.get("/api/admin/v1/config").text


def test_platform_owns_no_knowledge_tables():
    assert set(Base.metadata.tables) == {
        "admins",
        "sessions",
        "model_providers",
        "model_routes",
        "model_keys",
        "model_calls",
    }
