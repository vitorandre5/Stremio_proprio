"""One-time, transactional import of the persisted SQLite database into PostgreSQL."""

from __future__ import annotations

import json
import logging
import sqlite3
from pathlib import Path
from urllib.parse import unquote

from sqlalchemy import Column, MetaData, String, Table, Text, create_engine, inspect, select, func
from sqlalchemy.engine import Engine, make_url

from app.database.db import Base


logger = logging.getLogger("media_library_manager.database")
MIGRATION_KEY = "sqlite-to-postgresql-v1"
_migration_metadata = MetaData()
_migrations = Table(
    "application_migrations",
    _migration_metadata,
    Column("key", String(80), primary_key=True),
    Column("completed_at", String(40), nullable=False),
    Column("details", Text, nullable=False),
)


def _sqlite_path(database_url: str) -> Path:
    url = make_url(database_url)
    if url.get_backend_name() != "sqlite" or not url.database:
        raise ValueError("A URL da origem da migração precisa apontar para um arquivo SQLite.")
    return Path(unquote(url.database))


def _backup_sqlite(source_engine: Engine, source_path: Path) -> Path:
    backup_path = Path(f"{source_path}.pre-postgresql.bak")
    if backup_path.exists():
        return backup_path
    backup_path.parent.mkdir(parents=True, exist_ok=True)
    destination = sqlite3.connect(backup_path)
    try:
        raw = source_engine.raw_connection()
        try:
            raw.driver_connection.backup(destination)
        finally:
            raw.close()
    finally:
        destination.close()
    return backup_path


def _migrate_sqlite(source_url: str, target_engine: Engine) -> dict[str, int] | None:
    source_path = _sqlite_path(source_url)
    if not source_path.is_file():
        logger.info("SQLite legado não encontrado; migração para PostgreSQL ignorada")
        return None

    _migration_metadata.create_all(target_engine, checkfirst=True)
    with target_engine.connect() as connection:
        if connection.execute(select(_migrations.c.key).where(_migrations.c.key == MIGRATION_KEY)).first():
            logger.info("Migração SQLite para PostgreSQL já concluída")
            return None

    source_engine = create_engine(source_url, connect_args={"check_same_thread": False})
    backup_path = _backup_sqlite(source_engine, source_path)
    summary: dict[str, int] = {}
    source_tables = set(inspect(source_engine).get_table_names())

    try:
        with source_engine.connect() as source_connection, target_engine.begin() as target_connection:
            if target_connection.execute(
                select(_migrations.c.key).where(_migrations.c.key == MIGRATION_KEY)
            ).first():
                return None

            for target_table in Base.metadata.sorted_tables:
                if target_table.name not in source_tables:
                    continue
                source_table = Table(
                    target_table.name,
                    MetaData(),
                    autoload_with=source_engine,
                )
                source_rows = source_connection.execute(select(source_table)).mappings().all()
                shared_columns = {column.name for column in target_table.columns} & {
                    column.name for column in source_table.columns
                }
                primary_key = [column.name for column in target_table.primary_key.columns]
                copied = 0
                for source_row in source_rows:
                    row = {name: source_row[name] for name in shared_columns}
                    existing_filter = [
                        target_table.c[name] == row[name]
                        for name in primary_key
                        if name in row
                    ]
                    if len(existing_filter) != len(primary_key):
                        raise RuntimeError(f"Chave primária incompleta em {target_table.name}")
                    exists = target_connection.execute(
                        select(target_table.c[primary_key[0]]).where(*existing_filter).limit(1)
                    ).first()
                    if not exists:
                        target_connection.execute(target_table.insert().values(**row))
                        copied += 1

                source_count = len(source_rows)
                target_count = target_connection.execute(
                    select(func.count()).select_from(target_table)
                ).scalar_one()
                if target_count < source_count:
                    raise RuntimeError(
                        f"Verificação falhou para {target_table.name}: "
                        f"origem={source_count}, destino={target_count}"
                    )
                summary[target_table.name] = copied

            from datetime import datetime, timezone

            target_connection.execute(
                _migrations.insert().values(
                    key=MIGRATION_KEY,
                    completed_at=datetime.now(timezone.utc).isoformat(),
                    details=json.dumps(summary, sort_keys=True),
                )
            )
    except Exception:
        logger.exception("Falha ao migrar SQLite para PostgreSQL; transação revertida")
        raise
    finally:
        source_engine.dispose()

    logger.info(
        "Migração SQLite para PostgreSQL concluída; cópia preservada em %s; linhas copiadas: %s",
        backup_path,
        summary,
    )
    return summary


def migrate_sqlite_to_postgresql(source_url: str, target_engine: Engine) -> dict[str, int] | None:
    """Copy legacy app data only when PostgreSQL is the configured destination."""
    if target_engine.dialect.name != "postgresql":
        return None
    return _migrate_sqlite(source_url, target_engine)
