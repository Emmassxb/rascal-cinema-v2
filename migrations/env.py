"""Entorno Alembic: usa la URL de ``rascal.config`` y los metadatos del ORM."""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context

from rascal.config import get_settings
from rascal.data.db import build_engine
from rascal.data.models import Base

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


def run_migrations_online() -> None:
    engine = build_engine(DATABASE_URL)
    with engine.connect() as connection:
        context.configure(
            connection=connection, target_metadata=target_metadata,
            render_as_batch=connection.dialect.name == "sqlite",  # ALTER TABLE vía copia en SQLite
            compare_type=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
