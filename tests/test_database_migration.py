import tempfile
import unittest
from pathlib import Path

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.database.db import Base
from app.database.migration import MIGRATION_KEY, _migrate_sqlite, _migrations
from app.database.models import Addon, Job, Preference


class SQLiteMigrationTests(unittest.TestCase):
    def test_import_preserves_rows_is_idempotent_and_keeps_source_backup(self):
        with tempfile.TemporaryDirectory() as temporary_dir:
            root = Path(temporary_dir)
            source_path = root / "source.sqlite3"
            source_url = f"sqlite:///{source_path.as_posix()}"
            source_engine = create_engine(source_url)
            Base.metadata.create_all(source_engine)
            with Session(source_engine) as session:
                session.add(Addon(id="addon.test", name="Addon de teste", manifest_url="https://example.test/manifest.json"))
                session.add(Preference(id=1, preferred_quality="1080p", preferred_provider="automatic"))
                session.add(Job(
                    id="job-test", kind="sync", owner_id="admin", status="completed",
                    total=2, completed=2, failed=0, message="Concluído", result_json="{}",
                    created_at="2026-10-08T10:00:00Z", updated_at="2026-10-08T10:00:01Z",
                ))
                session.commit()

            target_engine = create_engine(f"sqlite:///{(root / 'target.sqlite3').as_posix()}")
            Base.metadata.create_all(target_engine)

            copied = _migrate_sqlite(source_url, target_engine)
            repeated = _migrate_sqlite(source_url, target_engine)

            self.assertEqual(copied["addons"], 1)
            self.assertEqual(copied["jobs"], 1)
            self.assertEqual(copied["preferences"], 1)
            self.assertIsNone(repeated)
            self.assertTrue(Path(f"{source_path}.pre-postgresql.bak").is_file())
            with Session(target_engine) as session:
                self.assertEqual(session.get(Addon, "addon.test").name, "Addon de teste")
                self.assertEqual(session.get(Preference, 1).preferred_quality, "1080p")
                self.assertEqual(session.get(Job, "job-test").completed, 2)
            with target_engine.connect() as connection:
                self.assertEqual(
                    connection.execute(select(_migrations.c.key)).scalar_one(),
                    MIGRATION_KEY,
                )
            with Session(source_engine) as session:
                self.assertIsNotNone(session.get(Addon, "addon.test"))

            source_engine.dispose()
            target_engine.dispose()


if __name__ == "__main__":
    unittest.main()
