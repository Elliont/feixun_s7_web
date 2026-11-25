"""临时 SQLite 测试环境，不连接实际业务数据库。"""

import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path

from s7 import config, db


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.old_config = config.DATA_DIR, config.DB_PATH
        self.temp = tempfile.TemporaryDirectory(prefix="s7-test-")
        self.addCleanup(self._cleanup)
        self.root = Path(self.temp.name)
        config.DATA_DIR = self.root / "data"
        config.DB_PATH = config.DATA_DIR / "s7.db"
        db._local.conn = None
        db._initialized = False
        db.init_db()

    def _cleanup(self):
        c = getattr(db._local, "conn", None)
        if c is not None:
            c.close()
        db._local.conn = None
        db._initialized = False
        config.DATA_DIR, config.DB_PATH = self.old_config
        self.temp.cleanup()

    def add_member(self, name="当前成员", connection=None):
        c = connection if connection is not None else db.conn()
        return c.execute("INSERT INTO members(name,sex,birth_year,height_cm,created_at) "
                         "VALUES(?,?,?,?,?)", (name, "male", 1990, 175, db.now_iso())).lastrowid

    def backup_bytes(self):
        path = self.root / "uploaded.db"
        with closing(sqlite3.connect(path, isolation_level=None)) as c:
            c.row_factory = sqlite3.Row
            db.ensure_schema(c)
            self.add_member("备份成员", c)
        return path.read_bytes()

    def add_measurement(self, member_id, at, weight=7000, device="AA:BB:CC:DD:EE:FF",
                        impedances="", ohm=0, excluded=0):
        cur = db.conn().execute(
            "INSERT OR IGNORE INTO measurements(member_id,device_mac,measured_at,weight_centi_kg,"
            "impedances,impedance_used_ohm,excluded,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (member_id, device, at, weight, impedances, ohm, excluded, db.now_iso()))
        return cur.rowcount
