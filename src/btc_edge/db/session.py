"""Session management for the SQLite database."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from .models import Base


def _ensure_dir(db_path: Path) -> None:
    db_path = db_path.resolve()
    db_path.parent.mkdir(parents=True, exist_ok=True)


def create_engine_for(path: str | Path) -> Engine:
    """Build a SQLite engine, creating parent directories as needed."""
    p = Path(path)
    _ensure_dir(p)
    url = f"sqlite:///{p.resolve()}"
    return create_engine(url, future=True, connect_args={"check_same_thread": False})


def init_db(path: str | Path) -> Engine:
    """Create the engine and all tables if they do not exist."""
    engine = create_engine_for(path)
    Base.metadata.create_all(engine)
    return engine


class Database:
    """Thin session factory wrapper used across the codebase."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.engine = init_db(self.path)
        self._factory = sessionmaker(
            bind=self.engine, class_=Session, expire_on_commit=False
        )

    def session(self) -> Session:
        return self._factory()

    def close(self) -> None:
        self.engine.dispose()
