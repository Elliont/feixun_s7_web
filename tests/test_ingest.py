import sqlite3
from contextlib import closing
from datetime import datetime
from unittest.mock import patch

from s7 import db, ingest, protocol
from support import DatabaseTestCase


class IngestAcknowledgementTests(DatabaseTestCase):
    def setUp(self):
        super().setUp()
        self.handler = ingest.Handler.__new__(ingest.Handler)
        self.sent = []
        self.notify = patch.object(ingest, "_notify_async").start()
        self.addCleanup(patch.stopall)

    def frame(self, command, weight=7000):
        plain = bytearray(64)
        plain[:4] = bytes((0x55, 0xAA, 0, 64))
        plain[12:18] = bytes.fromhex("aabbccddeeff")
        plain[26:28] = command.to_bytes(2, "big")
        offset = 43 if command == protocol.CMD_REPORT else 37
        plain[offset:offset + 2] = weight.to_bytes(2, "big")
        if command == protocol.CMD_RECORD:
            at = datetime.now().replace(microsecond=0)
            plain[30:36] = bytes((at.year - 2000, at.month, at.day, at.hour, at.minute, at.second))
        return protocol.seal(plain)

    def sendall(self, raw):
        self.sent.append(raw)

    def test_both_commands_commit_before_ack(self):
        class CommittedSocket:
            def __init__(self, test):
                self.test = test

            def sendall(self, raw):
                # 独立连接只能看到已提交的记录。
                from s7 import config
                with closing(sqlite3.connect(config.DB_PATH)) as observer:
                    count = observer.execute("SELECT COUNT(*) FROM measurements").fetchone()[0]
                self.test.assertEqual(count, len(self.test.sent) + 1)
                self.test.sent.append(raw)

        for i, command in enumerate((protocol.CMD_REPORT, protocol.CMD_RECORD)):
            self.handler._dispatch(CommittedSocket(self), self.frame(command, 7000 + i), "test")
            ack = protocol.xor(self.sent[-1])
            self.assertTrue(protocol.frame_is_valid(ack))
            self.assertEqual(protocol.cmd_of(ack), command | 0x4000)

    def test_failed_persistence_sends_no_ack_for_either_command(self):
        for command in (protocol.CMD_REPORT, protocol.CMD_RECORD):
            with self.subTest(command=command):
                with patch.object(db, "execute", side_effect=sqlite3.OperationalError("database is locked")):
                    with self.assertRaises(sqlite3.OperationalError):
                        self.handler._dispatch(self, self.frame(command), "test")
                self.assertEqual(self.sent, [])
                self.assertEqual(db.one("SELECT COUNT(*) n FROM measurements")["n"], 0)

    def test_duplicate_history_receives_ack_without_another_insert_or_notification(self):
        raw = self.frame(protocol.CMD_RECORD)
        self.handler._dispatch(self, raw, "test")
        self.handler._dispatch(self, raw, "test")
        self.assertEqual(len(self.sent), 2)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM measurements")["n"], 1)
        self.notify.assert_called_once()

    def test_notification_queue_failure_does_not_prevent_ack_after_commit(self):
        self.notify.side_effect = RuntimeError("executor unavailable")
        with self.assertLogs("s7.ingest", level="ERROR"):
            self.handler._dispatch(self, self.frame(protocol.CMD_REPORT), "test")
        self.assertEqual(len(self.sent), 1)
        self.assertEqual(db.one("SELECT COUNT(*) n FROM measurements")["n"], 1)

    def test_event_logging_happens_after_ack(self):
        def event(mac, kind):
            self.assertEqual(kind, "report")
            self.assertEqual(len(self.sent), 1)

        with patch.object(ingest, "_event", side_effect=event):
            self.handler._dispatch(self, self.frame(protocol.CMD_REPORT), "test")

    def test_invalid_measurement_is_not_acknowledged(self):
        with self.assertLogs("s7.ingest", level="WARNING"):
            self.handler._dispatch(self, self.frame(protocol.CMD_REPORT, 0), "test")
        self.assertEqual(self.sent, [])
        self.assertEqual(db.one("SELECT COUNT(*) n FROM measurements")["n"], 0)
