"""备份恢复：先在临时库验证与升级，替换失败时还原现场。"""

from __future__ import annotations

import sqlite3
import tempfile
import threading
from contextlib import closing, contextmanager
from datetime import datetime
from pathlib import Path

from . import config, db

_RESTORE_LOCK = threading.Lock()


class InvalidBackup(ValueError):
    """文件不是完整可读的 SQLite 数据库。"""


class IncompatibleBackup(ValueError):
    """数据库结构或外键关系不兼容。"""


def _open(path: Path | str) -> sqlite3.Connection:
    c = sqlite3.connect(str(path), timeout=10.0, isolation_level=None)
    c.row_factory = sqlite3.Row
    return c


def _check_integrity(c: sqlite3.Connection) -> None:
    try:
        rows = c.execute("PRAGMA integrity_check").fetchall()
        if len(rows) != 1 or rows[0][0] != "ok":
            raise InvalidBackup("数据库完整性检查失败")
    except sqlite3.DatabaseError as exc:
        raise InvalidBackup("无法读取数据库") from exc


def _columns(c: sqlite3.Connection, table: str) -> dict[str, sqlite3.Row]:
    escaped = table.replace('"', '""')
    return {r["name"]: r for r in c.execute(f'PRAGMA table_info("{escaped}")')}


def _prepare(c: sqlite3.Connection) -> None:
    """只操作上传的临时库；缺失旧版本新增的表/列时先迁移再校验。"""
    _check_integrity(c)
    tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if not {"measurements", "members"} <= tables:
        raise IncompatibleBackup("缺少称重或成员表")
    try:
        db.ensure_schema(c)
        # 从当前迁移代码生成参照，覆盖业务查询用到的所有表和列。
        with closing(_open(":memory:")) as expected:
            db.ensure_schema(expected)
            for row in expected.execute("SELECT name FROM sqlite_master WHERE type='table'"):
                table = row[0]
                if table.startswith("sqlite_"):
                    continue
                required = _columns(expected, table)
                actual = _columns(c, table)
                for name, want in required.items():
                    got = actual.get(name)
                    if got is None or any(got[k] != want[k] for k in ("type", "notnull", "pk")):
                        raise IncompatibleBackup(f"不兼容的列：{table}.{name}")
                # 允许历史版本遗留的可选列，但不能有阻止当前 INSERT 的必填列。
                if any(r["notnull"] and r["dflt_value"] is None and not r["pk"]
                       for name, r in actual.items() if name not in required):
                    raise IncompatibleBackup(f"表 {table} 含有额外的必填列")
        if c.execute("PRAGMA foreign_key_check").fetchone() is not None:
            raise IncompatibleBackup("数据库含有无效的外键引用")
    except sqlite3.DatabaseError as exc:
        raise IncompatibleBackup("数据库无法升级到当前结构") from exc
    _check_integrity(c)


@contextmanager
def restore(raw: bytes):
    """恢复并保留当前库快照；调用方完成会话同步等操作前，异常均触发回滚。"""
    if not raw.startswith(b"SQLite format 3\x00"):
        raise InvalidBackup("文件不是 SQLite 数据库")
    with _RESTORE_LOCK:
        config.ensure_dirs()
        backup_dir = config.DATA_DIR / "backup"
        # 每次恢复独占目录，避免并发上传覆盖同名文件；连接关闭后再清理 WAL/SHM。
        with tempfile.TemporaryDirectory(prefix="restore-", dir=backup_dir) as tmp_dir:
            upload = Path(tmp_dir) / "upload.db"
            upload.write_bytes(raw)
            with closing(_open(upload)) as staged:
                _prepare(staged)
                stamp = datetime.now().strftime("%Y%m%d-%H%M%S-%f")
                snapshot = backup_dir / f"before-restore-{stamp}.db"
                with closing(_open(config.DB_PATH)) as live, closing(_open(snapshot)) as snap:
                    live.backup(snap)
                    try:
                        staged.backup(live)
                        yield snapshot
                    except BaseException:
                        # backup API 在原文件内回滚，其他进程的连接仍指向同一个库。
                        snap.backup(live)
                        raise
