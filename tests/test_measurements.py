from datetime import datetime, timedelta

from s7 import db
from support import DatabaseTestCase


class MeasurementDedupTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.a = self.add_member("甲")
        self.b = self.add_member("乙")
        self.at = "2026-11-24T08:00:00"

    def test_local_records_with_same_time_and_weight_are_separate_for_each_member(self):
        for device in ("MANUAL", "IMPORT"):
            with self.subTest(device=device):
                self.assertEqual(self.add_measurement(self.a, self.at, device=device), 1)
                self.assertEqual(self.add_measurement(self.b, self.at, device=device), 1)
                self.assertEqual(self.add_measurement(self.a, self.at, device=device), 0)

    def test_unassigned_local_records_still_deduplicate(self):
        self.assertEqual(self.add_measurement(None, self.at, device="MANUAL"), 1)
        self.assertEqual(self.add_measurement(None, self.at, device="MANUAL"), 0)

    def test_hardware_retry_deduplicates_even_after_member_changes(self):
        self.assertEqual(self.add_measurement(self.a, self.at), 1)
        self.assertEqual(self.add_measurement(self.b, self.at), 0)

    def test_old_unique_index_is_upgraded_without_losing_existing_records(self):
        db.execute("DROP INDEX ux_m_local_dedup")
        db.execute("DROP INDEX ux_m_hardware_dedup")
        db.execute("CREATE UNIQUE INDEX ux_m_dedup ON measurements(device_mac,measured_at,weight_centi_kg)")
        self.add_measurement(self.a, self.at, device="MANUAL")
        db.ensure_schema()
        db.ensure_schema()                       # 迁移重跑仍保持幂等
        self.assertEqual(self.add_measurement(self.b, self.at, device="MANUAL"), 1)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM measurements")["n"], 2)


class SessionTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.mid = self.add_member()
        self.base = datetime(2026, 11, 24, 8)
        self.add_measurement(self.mid, (self.base - timedelta(days=1)).isoformat(), weight=7100)
        self.add_measurement(self.mid, (self.base - timedelta(days=2)).isoformat(), weight=7200)
        # 最完整的记录位于会话开头，固定截取最近 100 行会漏掉它。
        self.add_measurement(self.mid, self.base.isoformat(), weight=7000,
                             impedances="5000,5000,5000,5000,5000,5000", ohm=500)
        for i in range(1, 122):
            self.add_measurement(self.mid, (self.base + timedelta(seconds=4*i)).isoformat(), weight=6950)

    def test_limited_query_matches_full_sessions_in_both_orders(self):
        for reverse in (False, True):
            with self.subTest(newest_first=reverse):
                full = db.member_sessions(self.mid, newest_first=reverse)
                limited = db.member_sessions(self.mid, limit_sessions=2, newest_first=reverse)
                expected = full[:2] if reverse else full[-2:]
                self.assertEqual([r["id"] for r in limited], [r["id"] for r in expected])

    def test_best_representative_at_start_of_large_boundary_session_is_preserved(self):
        latest = db.member_sessions(self.mid, limit_sessions=1, newest_first=True)
        self.assertEqual(len(latest), 1)
        self.assertEqual(latest[0]["weight_centi_kg"], 7000)
        self.assertEqual(latest[0]["impedance_used_ohm"], 500)

    def test_reference_median_uses_complete_requested_sessions(self):
        self.assertEqual(db.recent_median_weights(2)[self.mid], 70.5)

    def test_insufficient_sessions_and_zero_limit(self):
        self.assertEqual(len(db.member_sessions(self.mid, limit_sessions=5)), 3)
        self.assertEqual(db.member_sessions(self.mid, limit_sessions=0), [])


class GoalStartTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.mid = self.add_member()
        self.gid = db.execute("INSERT INTO goals(member_id,kind,start_value,target_value,created_at) "
                              "VALUES(?,'weight',NULL,60,'2026-11-24T08:00:00+08:00')", (self.mid,))

    def goal(self):
        return db.one("SELECT * FROM goals WHERE id=?", (self.gid,))

    def test_goal_waits_for_first_measurement_then_keeps_start(self):
        self.assertIsNone(db.goal_start(self.goal()))
        self.add_measurement(self.mid, "2026-11-24T08:01:00", weight=7000)
        self.assertEqual(db.goal_start(self.goal()), 70)
        self.add_measurement(self.mid, "2026-11-25T08:01:00", weight=6500)
        self.assertEqual(db.goal_start(self.goal()), 70)
        self.assertEqual(self.goal()["start_value"], 70)

    def test_first_measurement_is_found_when_page_is_opened_after_multiple_weighings(self):
        self.add_measurement(self.mid, "2026-11-24T08:01:00", weight=7000)
        self.add_measurement(self.mid, "2026-11-25T08:01:00", weight=6500)
        self.add_measurement(self.mid, "2026-11-26T08:01:00", weight=5900)
        self.assertEqual(db.goal_start(self.goal()), 70)

    def test_existing_explicit_start_is_never_overwritten(self):
        db.execute("UPDATE goals SET start_value=75 WHERE id=?", (self.gid,))
        self.add_measurement(self.mid, "2026-11-24T08:01:00", weight=7000)
        self.assertEqual(db.goal_start(self.goal()), 75)

    def test_ignored_measurements_are_not_used_as_start(self):
        self.add_measurement(self.mid, "2026-11-24T08:01:00", weight=10000, excluded=1)
        self.add_measurement(self.mid, "2026-11-24T08:02:00", weight=7000)
        self.assertEqual(db.goal_start(self.goal()), 70)

    def test_backfilled_older_history_can_initialize_missing_start(self):
        self.add_measurement(self.mid, "2026-11-23T08:01:00", weight=7200)
        self.add_measurement(self.mid, "2026-11-23T09:01:00", weight=7100)
        self.assertEqual(db.goal_start(self.goal()), 71)
