"""Run hourly in deployment: python -m app.maintenance. Uses the configured database."""

import time

from sqlalchemy import update

from .cloud_models import GithubFlow
from .db import SessionLocal
from .feedback import purge_expired


def main():
    with SessionLocal() as db:
        purge_expired(db)
        db.execute(
            update(GithubFlow)
            .where(GithubFlow.expires_at <= time.time())
            .values(encrypted_verifier=None, status="expired")
        )
        db.commit()


if __name__ == "__main__":
    main()
