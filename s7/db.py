"""SQLite 访问层。

要点：
- 每线程独立连接（sqlite3 默认不允许跨线程共享）
- WAL + busy_timeout，读写并发友好
- 所有 SQL 参数化
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path

from . import config, protocol

SESSION_WINDOW_MINUTES = 10

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


def cutoff_iso(days: int) -> str:
    """N 天前的本地朴素时间，格式与 measured_at(isoformat) 一致。

    measured_at 存的是 datetime.now().isoformat()（形如 2026-11-19T10:00:00，带 T、
    无时区）。SQL 里用 datetime('now','-N day') 生成的是「2026-11-19 10:00:00」（空格），
    与带 T 的值做字符串比较会错判边界日。这里在 Python 侧生成同格式截止值，SQL 用
    measured_at >= ? 传参，彻底避免格式/时区不一致。
    """
    return (datetime.now() - timedelta(days=days)).replace(microsecond=0).isoformat()


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


def ensure_schema(c: sqlite3.Connection | None = None) -> None:
    """重跑建表与迁移；可传临时库连接，在恢复前完成旧备份升级。"""
    schema = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")
    c = c if c is not None else conn()
    c.executescript(schema)
    _migrate_v2(c)          # 老备份缺 birth_date 列；函数本身幂等
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
    # INSERT 返回新行 id；UPDATE/DELETE 返回受影响行数。
    # 不能用 lastrowid or rowcount：非 INSERT 的 lastrowid 是上次 INSERT 的残留值。
    if cur.lastrowid and sql.lstrip().upper().startswith("INSERT"):
        return cur.lastrowid
    return cur.rowcount


@contextmanager
def transaction():
    """显式事务包装：isolation_level=None（autocommit）下保证多步写操作原子性。"""
    c = conn()
    c.execute("BEGIN IMMEDIATE")
    try:
        yield c
        c.execute("COMMIT")
    except BaseException:
        if c.in_transaction:
            c.execute("ROLLBACK")
        raise


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
    with transaction():
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
    with transaction():
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


def all_members() -> list[sqlite3.Row]:
    """返回全部成员（含已停用），供历史记录展示与导出按原档案算体成分。"""
    return query("SELECT * FROM members ORDER BY id")


def member_by_id(member_id: int) -> sqlite3.Row | None:
    return one("SELECT * FROM members WHERE id=?", (member_id,))


def _valid_imp_channels(row) -> int:
    """统计一条测量中有效阻抗通道数（6 路全电极 > 仅双脚 1 路 > 无阻抗 0 路）。"""
    keys = row.keys()
    raw = row["impedances"] if "impedances" in keys else ""
    cnt = 0
    if raw:
        for tok in str(raw).split(","):
            tok = tok.strip()
            if not tok:
                continue
            try:
                if protocol.valid_impedance(int(tok)):
                    cnt += 1
            except ValueError:
                pass
    if cnt == 0:
        imp = row["impedance_used_ohm"] if "impedance_used_ohm" in keys else 0
        if imp and float(imp) > 0:
            cnt = 1
    return cnt


def dedup_sessions(rows: list[sqlite3.Row],
                   window_minutes: int = SESSION_WINDOW_MINUTES,
                   newest_first: bool = False) -> list[sqlite3.Row]:
    """把同一成员短时间内的多次称重聚合成会话，每个会话仅选 1 条代表记录。

    规则：
      1) 按时间升序，相邻两条间隔 ≤ window_minutes 归入同一称重会话；
      2) 会话内按「有效阻抗通道数最多 → 时间最新 → id 最大」挑出代表记录。
    """
    if len(rows) <= 1:
        return list(rows)
    ordered = sorted(
        rows,
        key=lambda r: (str(r["measured_at"]), int(r["id"]) if "id" in r.keys() else 0),
    )
    window_sec = window_minutes * 60
    groups: list[list[sqlite3.Row]] = []
    prev_t: datetime | None = None

    for r in ordered:
        try:
            t = datetime.fromisoformat(str(r["measured_at"])).replace(tzinfo=None)
        except ValueError:
            t = None
        if groups and prev_t is not None and t is not None \
                and (t - prev_t).total_seconds() <= window_sec:
            groups[-1].append(r)
        else:
            groups.append([r])
        prev_t = t

    picked = [
        max(
            g,
            key=lambda r: (
                _valid_imp_channels(r),
                str(r["measured_at"]),
                int(r["id"]) if "id" in r.keys() else 0,
            ),
        )
        for g in groups
    ]
    if newest_first:
        picked.reverse()
    return picked


def member_sessions(member_id: int, since_iso: str | None = None,
                    limit_sessions: int | None = None,
                    newest_first: bool = False,
                    window_minutes: int = SESSION_WINDOW_MINUTES) -> list[sqlite3.Row]:
    """查询成员有效测量并按称重会话去重（不影响原始记录存储）。"""
    if limit_sessions is not None and limit_sessions <= 0:
        return []
    if since_iso is not None:
        rows = query(
            "SELECT * FROM measurements WHERE member_id=? AND excluded=0 "
            "AND measured_at >= ? ORDER BY measured_at ASC, id ASC",
            (member_id, since_iso))
        sess = dedup_sessions(rows, window_minutes=window_minutes, newest_first=newest_first)
        return sess[:limit_sessions] if (limit_sessions and newest_first) else (
            sess[-limit_sessions:] if limit_sessions else sess)

    if limit_sessions is not None:
        fetch_n = max(limit_sessions * 20, 100)
        while True:
            rows = query(
                "SELECT * FROM measurements WHERE member_id=? AND excluded=0 "
                "ORDER BY measured_at DESC, id DESC LIMIT ?",
                (member_id, fetch_n))
            sess = dedup_sessions(rows, window_minutes=window_minutes, newest_first=newest_first)
            # 多读到一个更早会话才说明所需的边界会话完整；否则扩大范围继续读取。
            if len(sess) > limit_sessions or len(rows) < fetch_n:
                return sess[:limit_sessions] if newest_first else sess[-limit_sessions:]
            fetch_n *= 2

    rows = query(
        "SELECT * FROM measurements WHERE member_id=? AND excluded=0 "
        "ORDER BY measured_at ASC, id ASC", (member_id,))
    return dedup_sessions(rows, window_minutes=window_minutes, newest_first=newest_first)


def recent_median_weights(n: int = 5) -> dict[int, float]:
    """每个成员最近 n 次独立称重会话的中位数，用于归属参照。"""
    out: dict[int, float] = {}
    for m in active_members():
        rows = member_sessions(m["id"], limit_sessions=n, newest_first=True)
        vals = sorted(r["weight_centi_kg"] / 100.0 for r in rows)
        if vals:
            k = len(vals)
            out[m["id"]] = vals[k // 2] if k % 2 else (vals[k // 2 - 1] + vals[k // 2]) / 2
    return out


def goal_start(goal) -> float | None:
    """固定空起点：优先取目标创建后的首次有效称重，否则取创建前最后一次。"""
    if goal["start_value"] is not None:
        return float(goal["start_value"])
    # created_at 带本地偏移、设备测量通常不带偏移；按保存的本地钟面时间比较。
    created = str(goal["created_at"])[:19].replace("T", " ")
    row = one("SELECT weight_centi_kg FROM measurements WHERE member_id=? AND excluded=0 "
              "AND substr(replace(measured_at,'T',' '),1,19)>=? "
              "ORDER BY measured_at ASC,id ASC LIMIT 1", (goal["member_id"], created))
    if row is None:
        row = one("SELECT weight_centi_kg FROM measurements WHERE member_id=? AND excluded=0 "
                  "ORDER BY measured_at DESC,id DESC LIMIT 1", (goal["member_id"],))
    if row is None:
        return None
    start = round(row["weight_centi_kg"] / 100.0, 2)
    execute("UPDATE goals SET start_value=? WHERE id=? AND start_value IS NULL", (start, goal["id"]))
    # 并发首次读取只允许一个起点生效，所有页面使用最终保存值。
    saved = one("SELECT start_value FROM goals WHERE id=?", (goal["id"],))
    return float(saved["start_value"]) if saved and saved["start_value"] is not None else start


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
    """把一条测量移进回收站（软删除，事务保证原子性）。"""
    with transaction():
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
    """从回收站还原；原 id 或去重键被占用时退化为新 id（事务保证原子性）。"""
    with transaction():
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
