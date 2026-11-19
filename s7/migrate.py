"""把旧版日志式数据库（单表 measurement）迁移到新结构。

用法：
    python -m s7.migrate --from /data/legacy.db --member <成员ID>

行为：
    - 旧表每行 → 新 measurements（member_id 指向指定成员；不指定则留空=待认领）
    - 幂等：已存在（mac + 时间 + 体重）的行会跳过
    - 旧行没有 user 概念，全部归到 --member 指定的成员（或待认领）
"""

from __future__ import annotations

import argparse
import sqlite3
import sys

from . import db, protocol


def migrate(src: str, member_name: str | None = None) -> dict:
    db.init_db()
    member_id = None
    if member_name:
        row = db.one("SELECT id FROM members WHERE name=?", (member_name,))
        if row:
            member_id = row["id"]
        else:
            member_id = db.execute(
                "INSERT INTO members(name,sex,birth_year,height_cm,created_at) "
                "VALUES(?,?,?,?,?)",
                (member_name, "male", 1980, 170, db.now_iso()))

    old = sqlite3.connect(src)
    old.row_factory = sqlite3.Row
    try:
        rows = old.execute(
            "SELECT measured_at, weight_kg, impedance_ohm, impedances, "
            "       data_type, mac FROM measurement ORDER BY id").fetchall()
    except sqlite3.Error as exc:
        print("读取旧库失败：", exc, file=sys.stderr)
        return {"imported": 0, "skipped": 0}

    imported = skipped = 0
    for r in rows:
        centi = int(round((r["weight_kg"] or 0) * 100))
        if centi <= 0 or not r["measured_at"]:
            skipped += 1
            continue
        imps = r["impedances"] or ""
        imp = 0.0
        if imps:
            try:
                vals = [int(x) for x in imps.split(",") if x.strip()]
                if len(vals) > protocol.FOOT_CHANNEL_INDEX and \
                        protocol.valid_impedance(vals[protocol.FOOT_CHANNEL_INDEX]):
                    imp = vals[protocol.FOOT_CHANNEL_INDEX] / 10.0
            except ValueError:
                pass
        try:
            db.execute(
                """INSERT INTO measurements(member_id, device_mac, measured_at,
                       weight_centi_kg, impedances, impedance_used_ohm, data_type,
                       source, match_state, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (member_id, r["mac"] or "UNKNOWN", r["measured_at"], centi, imps,
                 imp, r["data_type"] or 0, "history",
                 "confirmed" if member_id else "unassigned", db.now_iso()))
            imported += 1
        except Exception as exc:
            if "UNIQUE" in str(exc).upper():
                skipped += 1
            else:
                raise
    db.audit("migrate_legacy", src, f"imported={imported} skipped={skipped}")
    return {"imported": imported, "skipped": skipped}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from", dest="src", required=True, help="旧数据库路径")
    ap.add_argument("--member", default=None, help="归入的成员名（不填=待认领）")
    args = ap.parse_args()
    print(migrate(args.src, args.member))


if __name__ == "__main__":
    main()
