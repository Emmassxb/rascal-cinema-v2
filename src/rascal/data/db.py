"""Motor SQLAlchemy, fábrica de sesiones y unidad de trabajo.

* Un único ``Engine`` por proceso (pool de conexiones reutilizado). En Streamlit se
  obtiene vía ``st.cache_resource`` para que los *reruns* no abran conexiones nuevas.
* SQLite se configura en modo WAL + ``busy_timeout`` para que la UI y el microservicio
  de streaming puedan escribir concurrentemente sin ``database is locked``.
* ``session_scope()`` es la unidad de trabajo: todo lo que ocurre dentro se confirma
  atómicamente o se revierte por completo.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from functools import lru_cache
from pathlib import Path
from typing import Any

from sqlalchemy import Engine, create_engine, event
from sqlalchemy.engine import make_url
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from rascal.config import get_settings
from rascal.data.models import Base


def _configure_sqlite(engine: Engine) -> None:
    @event.listens_for(engine, "connect")
    def _set_pragmas(dbapi_connection: Any, _record: Any) -> None:
        # pysqlite emite BEGIN por su cuenta y rompe los SAVEPOINT (begin_nested).
        # Se desactiva y SQLAlchemy emite BEGIN explícitamente (receta oficial).
        dbapi_connection.isolation_level = None
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=30000")
        cursor.close()

    @event.listens_for(engine, "begin")
    def _begin(connection: Any) -> None:
        connection.exec_driver_sql("BEGIN")


def build_engine(url: str, *, echo: bool = False) -> Engine:
    """Crea un ``Engine`` con ajustes por dialecto."""
    parsed = make_url(url)
    if parsed.get_backend_name() == "sqlite":
        database = parsed.database
        in_memory = database in (None, "", ":memory:")
        kwargs: dict[str, Any] = {"connect_args": {"check_same_thread": False, "timeout": 30}}
        if in_memory:
            kwargs["poolclass"] = StaticPool  # una sola conexión compartida (tests)
        else:
            Path(database).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, echo=echo, **kwargs)
        _configure_sqlite(engine)
        return engine
    return create_engine(url, echo=echo, pool_pre_ping=True, pool_size=10, max_overflow=20)


@lru_cache(maxsize=1)
def get_engine() -> Engine:
    settings = get_settings()
    return build_engine(settings.database_url, echo=settings.sql_echo)


@lru_cache(maxsize=1)
def get_sessionmaker() -> sessionmaker[Session]:
    # expire_on_commit=False: los DTO se construyen dentro de la sesión, pero así
    # cualquier objeto devuelto sigue siendo legible tras el commit.
    return sessionmaker(bind=get_engine(), expire_on_commit=False, autoflush=True)


@contextmanager
def session_scope(factory: sessionmaker[Session] | None = None) -> Iterator[Session]:
    """Unidad de trabajo transaccional: commit al salir, rollback ante cualquier excepción."""
    session = (factory or get_sessionmaker())()
    try:
        yield session
        session.commit()
    except BaseException:
        session.rollback()
        raise
    finally:
        session.close()


def create_schema(engine: Engine | None = None) -> None:
    """Crea todas las tablas (desarrollo/tests). En producción usar ``alembic upgrade head``."""
    Base.metadata.create_all(engine or get_engine())
