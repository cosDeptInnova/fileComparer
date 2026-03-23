from __future__ import annotations

import os
from typing import Generator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, declarative_base, sessionmaker

_DEFAULT_DEV_DATABASE_URL = "postgresql://admin:adminC0smo5.GDC@localhost:5432/cosmos_control"
_LOCAL_ENVIRONMENTS = {"", "dev", "development", "local", "test", "testing"}


def _load_database_url() -> str:
    database_url = (os.getenv("COMPARE_DATABASE_URL") or os.getenv("DATABASE_URL") or "").strip()
    env_name = (os.getenv("ENV", os.getenv("ENVIRONMENT", "development")) or "development").strip().lower()
    is_local = env_name in _LOCAL_ENVIRONMENTS
    if database_url:
        return database_url
    if is_local:
        return _DEFAULT_DEV_DATABASE_URL
    raise RuntimeError(
        "COMPARE_DATABASE_URL o DATABASE_URL debe configurarse explícitamente para comp_docs en producción."
    )


def _build_engine(database_url: str) -> Engine:
    engine_kwargs = {
        "pool_pre_ping": True,
        "pool_recycle": int(os.getenv("COMPARE_DB_POOL_RECYCLE_SECONDS", "1800")),
        "future": True,
    }
    if database_url.startswith("sqlite"):
        engine_kwargs.setdefault("connect_args", {"check_same_thread": False})
    return create_engine(database_url, **engine_kwargs)


DATABASE_URL = _load_database_url()
engine = _build_engine(DATABASE_URL)
Base = declarative_base()
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine, future=True)


def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
