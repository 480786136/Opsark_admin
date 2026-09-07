from pathlib import Path
from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker
from .config import settings


class Base(DeclarativeBase):
    pass


url = settings().database_url
if url.startswith("sqlite"):
    Path("data").mkdir(exist_ok=True)
engine = create_engine(url, **({"connect_args": {"check_same_thread": False}} if url.startswith("sqlite") else {}))
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def get_db():
    with SessionLocal() as db:
        yield db
