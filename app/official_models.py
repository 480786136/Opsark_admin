"""Public discovery is not a model credential or an execution entitlement."""

# ruff: noqa: B008
from fastapi import APIRouter, Depends
from sqlalchemy import select

from .accounts import policy
from .db import get_db
from .models import ModelRoute, Provider

router = APIRouter(prefix="/api/core/v1")


@router.get("/official-models")
def catalog(db=Depends(get_db)):
    rows = db.execute(
        select(ModelRoute.alias, ModelRoute.display_name)
        .join(Provider, Provider.id == ModelRoute.provider_id)
        .where(
            ModelRoute.enabled.is_(True), Provider.enabled.is_(True), ModelRoute.alias.in_(policy(db).allowed_models)
        )
        .order_by(ModelRoute.alias)
    )
    # Only the public alias/display name. No routes, defaults, keys or user state.
    return {"models": [{"id": alias, "name": name or alias} for alias, name in rows]}
