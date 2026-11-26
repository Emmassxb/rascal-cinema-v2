"""Entorno Alembic: usa la URL de ``rascal.config`` y los metadatos del ORM."""

from __future__ import annotations

import warnings
from logging.config import fileConfig

from alembic import context

from rascal.config import get_settings
from rascal.data.db import build_engine
from rascal.data.models import Base

# SQLite no refleja índices por expresión (lower(username)); en modo batch es sólo ruido.
warnings.filterwarnings("ignore", message="Skipped unsupported reflection of expression-based index")
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata
DATABASE_URL = get_settings().database_url


def run_migrations_offline() -> None:
    context.configure(
        url=DATABASE_URL, target_metadata=target_metadata, literal_binds=True,
        render_as_batch=DATABASE_URL.startswith("sqlite"), compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _sqlite_foreign_keys(connection, enabled: bool) -> None:  # type: ignore[no-untyped-def]
    """PRAGMA fuera de transacción (en pysqlite autocommit es la única forma de que surta efecto).

    Las migraciones *batch* de SQLite recrean tablas (copiar → DROP → RENAME). Con
    ``foreign_keys=ON``, el DROP de una tabla referenciada ejecuta un DELETE implícito que
    dispararía los ``ON DELETE CASCADE`` (p. ej. borraría todas las reseñas al recrear
    ``movies``). Es el procedimiento oficial de SQLite: desactivar, migrar y verificar con
    ``PRAGMA foreign_key_check``.
    """
    raw = connection.connection.driver_connection
    raw.execute(f"PRAGMA foreign_keys={'ON' if enabled else 'OFF'}")


def run_migrations_online() -> None:
    engine = build_engine(DATABASE_URL)
    with engine.connect() as connection:
        is_sqlite = connection.dialect.name == "sqlite"
        if is_sqlite:
            _sqlite_foreign_keys(connection, False)
        context.configure(
            connection=connection, target_metadata=target_metadata,
            render_as_batch=is_sqlite,  # ALTER TABLE vía copia en SQLite
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()
        if is_sqlite:
            problems = connection.connection.driver_connection.execute("PRAGMA foreign_key_check").fetchall()
            _sqlite_foreign_keys(connection, True)
            if problems:
                raise RuntimeError(f"La migración dejó claves foráneas rotas: {problems[:5]}")


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
