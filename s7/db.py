"""SQLite 访问层。

要点：
- 每线程独立连接（sqlite3 默认不允许跨线程共享）
- WAL + busy_timeout，读写并发友好
- 所有 SQL 参数化
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config

_local = threading.local()
_init_lock = threading.Lock()
_initialized = False


def _connect(path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(path, timeout=10.0, isolation_level=None)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def conn() -> sqlite3.Connection:
    c = getattr(_local, "conn", None)
    if c is None:
        config.ensure_dirs()
        c = _connect(str(config.DB_PATH))
        _local.conn = c
    return c


def now_iso() -> str:
    return datetime.now().astimezone().replace(microsecond=0).isoformat()


def init_db() -> None:
    """建表（幂等）。"""
    global _initialized
    with _init_lock:
        if _initialized:
            return
        config.ensure_dirs()
        schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
        c = conn()
        c.executescript(schema)
        c.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(1, ?)",
                  (now_iso(),))
        _migrate_v2(c)
        _ensure_trash(c)
        _ensure_device_mode(c)
        _initialized = True


def ensure_schema() -> None:
    """重跑建表与迁移（恢复备份后调用，老备份可能缺新列）。"""
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    c = conn()
    c.executescript(schema)
    _ensure_trash(c)
    _ensure_device_mode(c)


def _ensure_device_mode(c: sqlite3.Connection) -> None:
    """devices 表补 bind_mode 列（老库升级用）。"""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(devices)").fetchall()}
    if cols and "bind_mode" not in cols:
        c.execute("ALTER TABLE devices ADD COLUMN bind_mode TEXT NOT NULL DEFAULT 'all'")
        c.commit()
    cols = {r["name"] for r in c.execute("PRAGMA table_info(devices)").fetchall()}
    if cols and "hidden_at" not in cols:
        c.execute("ALTER TABLE devices ADD COLUMN hidden_at TEXT")
        c.commit()


def _migrate_v2(c: sqlite3.Connection) -> None:
    """v2：出生年 → 出生日期（精确到日）。旧数据按 当年-01-01 补齐。"""
    cols = {r["name"] for r in c.execute("PRAGMA table_info(members)")}
    if "birth_date" not in cols:
        c.execute("ALTER TABLE members ADD COLUMN birth_date TEXT")
        c.execute("UPDATE members SET birth_date = printf('%04d-01-01', birth_year) "
                  "WHERE birth_date IS NULL")
        c.execute("INSERT OR IGNORE INTO schema_migrations(version, applied_at) VALUES(2, ?)",
                  (now_iso(),))


def query(sql: str, params: tuple = ()) -> list[sqlite3.Row]:
    return conn().execute(sql, params).fetchall()


def one(sql: str, params: tuple = ()) -> sqlite3.Row | None:
    return conn().execute(sql, params).fetchone()


def execute(sql: str, params: tuple = ()) -> int:
    cur = conn().execute(sql, params)
    return cur.lastrowid or cur.rowcount


# ---------------------------------------------------------------- 设置

def get_setting(key: str, default: str = "") -> str:
    row = one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


def set_setting(key: str, value: str) -> None:
    execute("INSERT INTO settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (key, str(value)))


def get_settings(keys: list[str]) -> dict[str, str]:
    placeholders = ",".join("?" * len(keys))
    rows = query(f"SELECT key,value FROM settings WHERE key IN ({placeholders})", tuple(keys))
    return {r["key"]: r["value"] for r in rows}


# ---------------------------------------------------------------- 审计

AUDIT_KEEP = 5000        # 审计只留最近 N 条，避免无限膨胀（静默存储，页面不再展示）


def audit(action: str, target: str | None = None, detail: str | None = None) -> None:
    execute("INSERT INTO audit_log(at,action,target,detail) VALUES(?,?,?,?)",
            (now_iso(), action, target, detail))
    execute("DELETE FROM audit_log WHERE id <= "
            "(SELECT MAX(id) FROM audit_log) - ?", (AUDIT_KEEP,))


# ---------------------------------------------------------------- 设备绑定

def device_bindings() -> dict[str, set[int]]:
    """{MAC: {member_id, ...}}，MAC 统一大写。"""
    out: dict[str, set[int]] = {}
    for r in query("SELECT device_mac, member_id FROM device_members"):
        out.setdefault(str(r["device_mac"]).upper(), set()).add(r["member_id"])
    return out


def device_names() -> dict[str, str]:
    return {str(r["mac"]).upper(): (r["name"] or "") for r in query("SELECT mac, name FROM devices")}


def set_device_name(mac: str, name: str) -> None:
    mac = mac.strip().upper()
    execute("INSERT INTO devices(mac, name, created_at) VALUES(?,?,?) "
            "ON CONFLICT(mac) DO UPDATE SET name=excluded.name",
            (mac, name, now_iso()))


def device_hidden() -> dict[str, str]:
    return {str(r["mac"]).upper(): str(r["hidden_at"] or "")
            for r in query("SELECT mac, hidden_at FROM devices WHERE hidden_at IS NOT NULL")}


def delete_device(mac: str, purge_data: bool = False) -> int:
    """移除设备；purge_data=True 时连带删掉这台秤的称重记录与连接事件。"""
    mac = mac.strip().upper()
    removed = 0
    if purge_data:
        removed = execute("DELETE FROM measurements WHERE device_mac=?", (mac,))
        execute("DELETE FROM device_events WHERE mac=?", (mac,))
        execute("DELETE FROM device_members WHERE device_mac=?", (mac,))
        execute("DELETE FROM devices WHERE mac=?", (mac,))
    else:
        execute("DELETE FROM device_events WHERE mac=?", (mac,))
        execute("DELETE FROM device_members WHERE device_mac=?", (mac,))
        execute("INSERT INTO devices(mac, name, bind_mode, hidden_at, created_at) "
                "VALUES(?,?, 'all', ?, ?) "
                "ON CONFLICT(mac) DO UPDATE SET hidden_at=excluded.hidden_at",
                (mac, "", now_iso(), now_iso()))
    return removed


def device_created() -> dict[str, str]:
    return {str(r["mac"]).upper(): str(r["created_at"] or "")
            for r in query("SELECT mac, created_at FROM devices")}


def device_modes() -> dict[str, str]:
    return {str(r["mac"]).upper(): (r["bind_mode"] or "all")
            for r in query("SELECT mac, bind_mode FROM devices")}


def set_device_bindings(mac: str, member_ids: list[int], mode: str = "pick") -> None:
    mac = mac.strip().upper()
    execute("INSERT INTO devices(mac, name, bind_mode, created_at) VALUES(?,?,?,?) "
            "ON CONFLICT(mac) DO UPDATE SET bind_mode=excluded.bind_mode",
            (mac, "", mode, now_iso()))
    execute("DELETE FROM device_members WHERE device_mac=?", (mac,))
    for mid in member_ids:
        execute("INSERT OR IGNORE INTO device_members(device_mac, member_id) VALUES(?,?)",
                (mac, mid))


# ---------------------------------------------------------------- 成员 / 测量

def active_members() -> list[sqlite3.Row]:
    return query("SELECT * FROM members WHERE is_active=1 ORDER BY id")


def member_by_id(member_id: int) -> sqlite3.Row | None:
    return one("SELECT * FROM members WHERE id=?", (member_id,))


def recent_median_weights(n: int = 5) -> dict[int, float]:
    """每个成员最近 n 次有效体重的中位数，用于归属参照。"""
    out: dict[int, float] = {}
    for m in active_members():
        rows = query(
            "SELECT weight_centi_kg FROM measurements "
            "WHERE member_id=? AND excluded=0 ORDER BY measured_at DESC LIMIT ?",
            (m["id"], n))
        vals = sorted(r["weight_centi_kg"] / 100.0 for r in rows)
        if vals:
            k = len(vals)
            out[m["id"]] = vals[k // 2] if k % 2 else (vals[k // 2 - 1] + vals[k // 2]) / 2
    return out


# ---------------------------------------------------------------- 回收站

TRASH_DAYS = 14


def _ensure_trash(c: sqlite3.Connection) -> None:
    """建回收站表：measurements 的全部列 + 自身主键 + 删除时间。"""
    defs = []
    for r in c.execute("PRAGMA table_info(measurements)"):
        if r["name"] == "id":
            defs.append("id INTEGER")
        else:
            defs.append(f'"{r["name"]}" {r["type"]}')
    if not defs:
        return
    c.execute("CREATE TABLE IF NOT EXISTS measurements_trash ("
              "trash_id INTEGER PRIMARY KEY AUTOINCREMENT, "
              + ", ".join(defs) + ", deleted_at TEXT NOT NULL)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_trash_at ON measurements_trash(deleted_at)")


def trash_move(measurement_id: int) -> bool:
    """把一条测量移进回收站（软删除）。"""
    row = one("SELECT * FROM measurements WHERE id=?", (measurement_id,))
    if not row:
        return False
    keys = list(row.keys())
    collist = ",".join(f'"{k}"' for k in keys)
    ph = ",".join("?" * len(keys))
    execute(f"INSERT INTO measurements_trash({collist},deleted_at) VALUES({ph},?)",
            tuple(row[k] for k in keys) + (now_iso(),))
    execute("DELETE FROM measurements WHERE id=?", (measurement_id,))
    return True


def trash_rows() -> list[sqlite3.Row]:
    return query("SELECT * FROM measurements_trash ORDER BY deleted_at DESC, trash_id DESC")


def trash_count() -> int:
    row = one("SELECT COUNT(*) AS n FROM measurements_trash")
    return row["n"] if row else 0


def trash_purge(days: int = TRASH_DAYS) -> int:
    """清掉超过保留期的条目（按本地时间比较，兼容带/不带时区的历史值）。"""
    cut = datetime.now().astimezone() - timedelta(days=days)
    dead = []
    for r in query("SELECT trash_id, deleted_at FROM measurements_trash"):
        try:
            t = datetime.fromisoformat(str(r["deleted_at"]))
        except ValueError:
            continue
        if t.tzinfo is None:
            t = t.replace(tzinfo=cut.tzinfo)
        if t < cut:
            dead.append(r["trash_id"])
    for tid in dead:
        execute("DELETE FROM measurements_trash WHERE trash_id=?", (tid,))
    return len(dead)


def trash_restore(trash_id: int) -> bool:
    """从回收站还原；原 id 或去重键被占用时退化为新 id。"""
    row = one("SELECT * FROM measurements_trash WHERE trash_id=?", (trash_id,))
    if not row:
        return False
    keys = [k for k in row.keys() if k not in ("trash_id", "deleted_at")]
    ok = False
    for cols in (keys, [k for k in keys if k != "id"]):
        sql = ("INSERT INTO measurements(" + ",".join(f'"{k}"' for k in cols)
               + ") VALUES(" + ",".join("?" * len(cols)) + ")")
        try:
            conn().execute(sql, tuple(row[k] for k in cols))
            ok = True
            break
        except sqlite3.IntegrityError:
            continue
    if ok:
        execute("DELETE FROM measurements_trash WHERE trash_id=?", (trash_id,))
    return ok


def trash_delete(trash_id: int) -> bool:
    """彻底删除一条。"""
    row = one("SELECT trash_id FROM measurements_trash WHERE trash_id=?", (trash_id,))
    if not row:
        return False
    execute("DELETE FROM measurements_trash WHERE trash_id=?", (trash_id,))
    return True


def trash_empty() -> int:
    return conn().execute("DELETE FROM measurements_trash").rowcount
