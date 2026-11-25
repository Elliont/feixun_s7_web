import sqlite3
from contextlib import closing
from pathlib import Path
from unittest.mock import patch

from s7 import backup, config, db
from support import DatabaseTestCase


class RestoreTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.add_member()

    def current_names(self):
        return [r["name"] for r in db.query("SELECT name FROM members ORDER BY id")]

    def assert_temp_removed(self):
        self.assertEqual(list((config.DATA_DIR / "backup").glob("restore-*")), [])

    def test_invalid_header_leaves_current_database_untouched(self):
        with self.assertRaises(backup.InvalidBackup):
            with backup.restore(b"not a database"):
                self.fail("Invalid file reached replacement")
        self.assertEqual(self.current_names(), ["当前成员"])

    def test_corrupt_database_leaves_current_database_untouched(self):
        with self.assertRaises(backup.InvalidBackup):
            with backup.restore(b"SQLite format 3\x00" + b"\xff" * 500):
                self.fail("Corrupt file reached replacement")
        self.assertEqual(self.current_names(), ["当前成员"])
        self.assert_temp_removed()

    def test_same_table_names_with_wrong_columns_are_rejected_before_replacement(self):
        path = self.root / "wrong.db"
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.executescript("CREATE TABLE members(id INTEGER); CREATE TABLE measurements(id INTEGER);")
        with self.assertRaises(backup.IncompatibleBackup):
            with backup.restore(path.read_bytes()):
                self.fail("Wrong schema reached replacement")
        self.assertEqual(self.current_names(), ["当前成员"])
        self.assertEqual(list((config.DATA_DIR / "backup").glob("before-restore-*.db")), [])
        self.assert_temp_removed()

    def test_missing_nonindexed_column_is_also_rejected(self):
        self.backup_bytes()
        path = self.root / "uploaded.db"
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.execute("ALTER TABLE measurements DROP COLUMN impedance_used_ohm")
        with self.assertRaises(backup.IncompatibleBackup):
            with backup.restore(path.read_bytes()):
                self.fail("Incomplete schema reached replacement")
        self.assertEqual(self.current_names(), ["当前成员"])

    def test_broken_foreign_keys_are_rejected(self):
        self.backup_bytes()
        path = self.root / "uploaded.db"
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.execute("INSERT INTO device_members(device_mac,member_id) VALUES('AA',999)")
        with self.assertRaises(backup.IncompatibleBackup):
            with backup.restore(path.read_bytes()):
                self.fail("Broken references reached replacement")
        self.assertEqual(self.current_names(), ["当前成员"])

    def test_valid_backup_restores_and_keeps_snapshot_for_existing_connections(self):
        raw = self.backup_bytes()
        with closing(sqlite3.connect(config.DB_PATH, isolation_level=None)) as observer:
            self.assertEqual(observer.execute("SELECT name FROM members").fetchone()[0], "当前成员")
            with backup.restore(raw) as snapshot:
                self.assertEqual(self.current_names(), ["备份成员"])
                self.assertEqual(observer.execute("SELECT name FROM members").fetchone()[0], "备份成员")
            with closing(sqlite3.connect(snapshot)) as old:
                self.assertEqual(old.execute("SELECT name FROM members").fetchone()[0], "当前成员")
        self.assert_temp_removed()

    def test_failure_after_replacement_restores_original_data_and_connections(self):
        raw = self.backup_bytes()
        with closing(sqlite3.connect(config.DB_PATH, isolation_level=None)) as observer:
            observer.execute("SELECT name FROM members").fetchall()
            with self.assertRaisesRegex(RuntimeError, "injected failure"):
                with backup.restore(raw):
                    self.assertEqual(self.current_names(), ["备份成员"])
                    db.set_setting("restore_marker", "must be rolled back")
                    raise RuntimeError("injected failure")
            self.assertEqual(observer.execute("SELECT name FROM members").fetchone()[0], "当前成员")
        self.assertEqual(self.current_names(), ["当前成员"])
        self.assertEqual(db.get_setting("restore_marker"), "")
        self.assert_temp_removed()

    def test_partial_replacement_failure_rolls_back(self):
        raw = self.backup_bytes()
        real_open = backup._open

        class FailingSource:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def backup(self, target):
                self.connection.backup(target)
                raise sqlite3.OperationalError("injected copy failure")

        def open_source(path):
            c = real_open(path)
            return FailingSource(c) if str(path).endswith("/upload.db") else c

        with patch.object(backup, "_open", side_effect=open_source):
            with self.assertRaisesRegex(sqlite3.OperationalError, "injected copy failure"):
                with backup.restore(raw):
                    self.fail("Failed replacement reached caller")
        self.assertEqual(self.current_names(), ["当前成员"])
        self.assert_temp_removed()

    def test_legacy_backup_is_migrated_before_replacement(self):
        path = self.root / "legacy.db"
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.executescript((Path(db.__file__).parent / "schema.sql").read_text())
            self.add_member("旧版成员", c)
            c.execute("ALTER TABLE devices DROP COLUMN hidden_at")
            c.execute("ALTER TABLE devices DROP COLUMN bind_mode")
        with backup.restore(path.read_bytes()):
            self.assertEqual(self.current_names(), ["旧版成员"])
            member = db.one("SELECT birth_date FROM members")
            self.assertEqual(member["birth_date"], "1990-01-01")
            self.assertIn("hidden_at", {r["name"] for r in db.query("PRAGMA table_info(devices)")})
            self.assertEqual(db.trash_count(), 0)
        self.assert_temp_removed()
