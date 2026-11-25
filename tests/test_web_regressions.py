"""安装运行期依赖后执行真实路由和 UI 函数回归；不需要额外测试依赖。"""

import asyncio
import io
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

try:
    import fastapi
except ModuleNotFoundError:
    raise unittest.SkipTest("需要安装 requirements.txt 中的 Web 运行期依赖")

from fastapi import UploadFile

from s7 import config, db, fatstandards, pages, ui, web
from support import DatabaseTestCase


def route(path):
    return next(r.endpoint for r in web.app.routes
                if getattr(r, "path", None) == path and "POST" in getattr(r, "methods", set()))


class WebRegressions(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.add_member("甲")
        self.b = self.add_member("乙")
        fatstandards.invalidate()
        self.secret = config.SECRET_KEY
        self.addCleanup(setattr, config, "SECRET_KEY", self.secret)
        self.addCleanup(fatstandards.invalidate)

    def upload(self, raw):
        return UploadFile(filename="backup.db", file=io.BytesIO(raw))

    def test_manual_route_accepts_two_members_with_matching_time_and_weight(self):
        for member in (self.a, self.b):
            response = route("/records/new")(request=None, member_id=member,
                                            measured_at="2026-11-24T08:00", weight="70", impedance="")
            self.assertEqual(response.headers["location"], "/records?ok=1")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM measurements")["n"], 2)

    def test_import_route_accepts_two_members_and_reimport_still_deduplicates(self):
        raw = "时间,成员,体重kg\n2026-11-24 08:00,甲,70\n2026-11-24 08:00,乙,70\n".encode()
        response = asyncio.run(route("/records/import")(request=None, file=self.upload(raw)))
        self.assertEqual(response.headers["location"], "/records?imp=2")
        response = asyncio.run(route("/records/import")(request=None, file=self.upload(raw)))
        self.assertEqual(response.headers["location"], "/records?imp=0")
        self.assertEqual(db.one("SELECT COUNT(*) n FROM measurements")["n"], 2)

    def test_goal_progress_on_both_views_and_resave_preserves_start(self):
        web._save_goal(self.a, "60", "")
        self.assertIsNone(ui._goal_view(self.a, None)["start"])
        base = datetime.now().replace(microsecond=0) + timedelta(seconds=1)
        for i, (weight, progress) in enumerate(((70, 0), (65, .5), (59, 1))):
            self.add_measurement(self.a, (base + timedelta(days=i)).isoformat(), weight=weight*100)
            view = ui._goal_view(self.a, weight)
            card = ui._member_card(dict(db.member_by_id(self.a)))
            self.assertEqual(view["progress"], progress)
            self.assertEqual(card["goal"]["progress"], progress*100)
            self.assertFalse(view["gain"])
        self.assertTrue(view["done"])
        goal_id = db.one("SELECT id FROM goals WHERE is_active=1")["id"]
        web._save_goal(self.a, "60", "")
        goal = db.one("SELECT * FROM goals WHERE is_active=1")
        self.assertEqual(goal["id"], goal_id)
        self.assertEqual(goal["start_value"], 70)

    def test_explicit_start_can_replace_a_pending_start(self):
        web._save_goal(self.a, "60", "")
        web._save_goal(self.a, "60", "75")
        self.assertEqual(db.one("SELECT start_value FROM goals WHERE is_active=1")["start_value"], 75)

    def test_restore_route_rolls_back_database_and_secret_after_postprocessing_failure(self):
        raw = self.backup_bytes()
        with patch.dict("os.environ", {"S7_SECRET_KEY": ""}):
            with patch.object(db, "audit", side_effect=RuntimeError("injected audit failure")):
                with self.assertLogs("s7.pages", level="ERROR"):
                    response = asyncio.run(route("/admin/restore")(file=self.upload(raw)))
        self.assertEqual(response.headers["location"], "/settings?err=6")
        self.assertEqual(config.SECRET_KEY, self.secret)
        self.assertEqual([r["name"] for r in db.active_members()], ["甲", "乙"])

    def test_restore_route_rejects_invalid_file_and_accepts_valid_backup(self):
        response = asyncio.run(route("/admin/restore")(file=self.upload(b"bad")))
        self.assertEqual(response.headers["location"], "/settings?err=3")
        response = asyncio.run(route("/admin/restore")(file=self.upload(self.backup_bytes())))
        self.assertEqual(response.headers["location"], "/settings?ok=5")
        self.assertEqual([r["name"] for r in db.active_members()], ["备份成员"])
