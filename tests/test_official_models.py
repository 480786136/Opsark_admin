from sqlalchemy import func, select
from test_accounts import configure
from test_gateway import setup as source_setup
from test_platform import env as source_env

from app.cloud_models import ClientPolicy
from app.db import get_db
from app.main import app
from app.models import ModelRoute, Provider
from app.user_models import UserAccount, UserSession

env = source_env
setup = source_setup


def test_public_catalogue_needs_no_account_but_does_not_authorize_model_calls(setup):
    c, staff, _, _, _ = setup
    configure(c, staff, models=["public-model"])
    for db in app.dependency_overrides[get_db]():
        db.scalar(select(ModelRoute)).display_name = "官方体验模型"
        db.add(ClientPolicy(id=1, min_cloud_version="99.0.0"))
        db.commit()
    c.cookies.clear()
    result = c.get("/api/core/v1/official-models")
    assert result.status_code == 200
    assert result.json() == {"models": [{"id": "public-model", "name": "官方体验模型"}]}
    assert "set-cookie" not in result.headers
    assert c.get("/v1/models").status_code == 401
    assert c.post("/v1/chat/completions", json={"model": "public-model", "messages": []}).status_code == 401
    for db in app.dependency_overrides[get_db]():
        assert db.scalar(select(func.count()).select_from(UserAccount)) == 0
        assert db.scalar(select(func.count()).select_from(UserSession)) == 0


def test_catalogue_tracks_admin_model_scope_route_and_provider_enablement(setup):
    c, staff, _, _, _ = setup
    assert c.get("/api/core/v1/official-models").json() == {"models": []}
    configure(c, staff, models=["public-model"])
    assert c.get("/api/core/v1/official-models").json() == {"models": [{"id": "public-model", "name": "public-model"}]}
    for db in app.dependency_overrides[get_db]():
        route = db.scalar(select(ModelRoute))
        route.enabled = False
        db.commit()
    assert c.get("/api/core/v1/official-models").json() == {"models": []}
    for db in app.dependency_overrides[get_db]():
        db.scalar(select(ModelRoute)).enabled = True
        db.scalar(select(Provider)).enabled = False
        db.commit()
    assert c.get("/api/core/v1/official-models").json() == {"models": []}
    for db in app.dependency_overrides[get_db]():
        db.scalar(select(Provider)).enabled = True
        db.commit()
    configure(c, staff, models=[])
    assert c.get("/api/core/v1/official-models").json() == {"models": []}
