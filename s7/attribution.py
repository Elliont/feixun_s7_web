"""归属算法：把一次测量分配给某个家庭成员。

规则（阈值可在「设置」里调 `match_auto_kg`）：
    参照值 = 该成员最近 N 次有效体重的中位数（抗单次异常）。
    候选池里按体重差值排序，取最小 d1、次小 d2：
        d1 ≤ 阈值 且 d2 > 2×阈值  → auto        唯一命中，直接归属
        否则                      → unassigned  进「待分配」等人认领

多设备二级归档：
    若这台秤绑定了若干成员（设备页「这台秤上通常是谁」），先在绑定池里按上面的
    规则找；找不到再退回全家人一起找——这样别人偶尔用这台秤也能被认出来。
"""

from __future__ import annotations

from . import db

AUTO = "auto"
PENDING = "pending"          # 历史数据里还有，新数据不再产生
UNASSIGNED = "unassigned"


def reference_weights() -> dict[int, float]:
    n = int(db.get_setting("match_ref_n", "5") or 5)
    return db.recent_median_weights(n)


def _auto_kg_setting() -> float:
    try:
        val = float(db.get_setting("match_auto_kg", "1"))
        if val > 0:
            return val
    except (TypeError, ValueError):
        pass
    return 1.0


def _pick(pool, weight_kg: float, refs: dict[int, float], auto_kg: float):
    """在候选池里挑一个足够确定的成员；返回 (member_id | None, score)。"""
    cands = []
    has_uncalibrated = False
    for m in pool:
        ref = refs.get(m["id"])
        if ref is None:
            has_uncalibrated = True
            continue
        cands.append((abs(ref - weight_kg), m["id"]))
    if not cands:
        return None, None
    cands.sort()
    d1, m1 = cands[0]
    if has_uncalibrated:
        # 候选池存在尚无历史体重的新成员：保守进待分配，避免把新成员首次称重误判给已有成员
        return None, round(d1, 3)
    d2 = cands[1][0] if len(cands) > 1 else 9e9
    if d1 <= auto_kg and d2 > 2 * auto_kg:
        return m1, round(d1, 3)
    return None, round(d1, 3)


def attribute(weight_kg: float, members, refs: dict[int, float],
              auto_kg: float | None = None,
              device_mac: str | None = None,
              bindings: dict[str, set[int]] | None = None) -> tuple[int | None, str, float | None]:
    """返回 (member_id | None, state, score)。"""
    auto_kg = auto_kg if auto_kg is not None else _auto_kg_setting()

    active = [m for m in members if m["is_active"]]
    if not active:
        return None, UNASSIGNED, None
    if len(active) == 1:
        d = refs.get(active[0]["id"])
        if d is None:
            return active[0]["id"], AUTO, None
        diff = round(abs(d - weight_kg), 3)
        # 单成员防呆上限：偏离历史中位数过大（如访客/宠物/重物）时进待分配，避免污染趋势
        if diff > max(5.0, 5.0 * auto_kg):
            return None, UNASSIGNED, diff
        return active[0]["id"], AUTO, diff

    # 1) 先看这台秤绑定的人
    bound = set((bindings or {}).get((device_mac or "").upper(), ()))
    if bound:
        mid, score = _pick([m for m in active if m["id"] in bound], weight_kg, refs, auto_kg)
        if mid:
            return mid, AUTO, score

    # 2) 退回全家人
    mid, score = _pick(active, weight_kg, refs, auto_kg)
    if mid:
        return mid, AUTO, score
    return None, UNASSIGNED, score
