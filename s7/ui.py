"""共享 UI 辅助：模板实例、成员/测量视图模型、指标卡与目标视图、CSRF。

web.py（总览/成员/详情）与 pages.py（所有记录/补录/设置/设备/管理）都从这里取，
避免循环依赖。
"""

from __future__ import annotations

import secrets
from pathlib import Path

from fastapi import Request
from fastapi.templating import Jinja2Templates

from . import __version__, bodycomp, config, db, grading, notify

BASE = Path(__file__).parent
templates = Jinja2Templates(directory=str(BASE / "templates"))

PALETTE = ["#00A98F", "#2563EB", "#F59E0B", "#7C3AED",
           "#EC4899", "#0EA5E9", "#84CC16", "#F97316"]

def base_dir() -> Path:
    return BASE


templates.env.globals["version"] = __version__
templates.env.globals["dev_noauth"] = config.DEV_NOAUTH


def csrf_token(request: Request) -> str:
    """双提交 CSRF：取值必须与 cookie 一致（由中间件统一生成）。"""
    tok = getattr(request.state, "csrf", "") or request.cookies.get("csrf", "")
    if not tok:
        tok = secrets.token_urlsafe(24)
        request.state.csrf = tok
    return tok



def _row(m) -> dict:
    """sqlite3.Row → dict（Row 没有 .get）"""
    return dict(m) if m is not None else {}


def _age(m) -> int:
    """成员周岁：优先按出生日期，其次按出生年。"""
    d = _row(m)
    return bodycomp.age_of_date(d.get("birth_date"), d.get("birth_year"))


def _asset_ver() -> str:
    """静态资源指纹：取 app.css / app.js 的 mtime，改动后 URL 变化即自动失效缓存。"""
    stamp = 0
    for name in ("app.css", "app.js"):
        p = BASE / "static" / name
        try:
            stamp = max(stamp, int(p.stat().st_mtime))
        except OSError:
            pass
    return f"{__version__}-{stamp}"


def ctx(request: Request, **kw) -> dict:
    """模板上下文统一入口（自动带版本、调试标志、CSRF）。"""
    base = {"request": request, "version": __version__, "asset_ver": _asset_ver(),
            "dev_noauth": config.DEV_NOAUTH, "csrf": csrf_token(request),
            "user": getattr(request.state, "user", "")}
    base.update(kw)
    return base


# grading 等级 → CSS 类
_LV_CLASS = {"ok": "v-ok", "low": "v-low", "warn": "v-warn", "bad": "v-bad", "info": "v-info"}


def _member_card(m: dict) -> dict:
    latest = db.one(
        "SELECT * FROM measurements WHERE member_id=? AND excluded=0 "
        "ORDER BY measured_at DESC LIMIT 1", (m["id"],))
    card = {
        "id": m["id"], "name": m["name"], "sex": m["sex"],
        "age": _age(m),
        "height_cm": m["height_cm"], "color": m["color"],
        "weight_kg": None, "measured_at": None, "comp": {},
    }
    if latest:
        weight = latest["weight_centi_kg"] / 100.0
        md = _row(m)
        male = md["sex"] == "male"
        at_date = bodycomp.date_of_iso(latest["measured_at"])
        comp = bodycomp.build(weight, md["height_cm"], md.get("birth_year"),
                              md["sex"], latest["impedance_used_ohm"] or 0,
                              birth_date=md.get("birth_date"), at=at_date)
        comp_age = comp.get("age", card["age"])
        card.update({
            "weight_kg": round(weight, 2),
            "measured_at": latest["measured_at"],
            "comp": comp,
            "match_state": latest["match_state"],
            "last_human": _human_since(latest["measured_at"]),
        })
        prev = db.one(
            "SELECT weight_centi_kg FROM measurements WHERE member_id=? AND excluded=0 "
            "ORDER BY measured_at DESC LIMIT 1 OFFSET 1", (m["id"],))
        if prev:
            card["delta"] = round(weight - prev["weight_centi_kg"] / 100.0, 1)
        # 三项指标的等级色（v-ok/v-low/v-warn/v-bad），由 grading 判定
        lv = {}
        if comp.get("bmi"):
            lv["bmi"] = _LV_CLASS.get(grading.bmi_level(comp["bmi"])[0], "")
        if comp.get("has_body_fat"):
            lv["bfr"] = _LV_CLASS.get(
                grading.fat_level(comp["body_fat_percent"], male, comp_age)[0], "")
            lv["vfr"] = _LV_CLASS.get(grading.visceral_level(comp["visceral_level"])[0], "")
            lv["score"] = _LV_CLASS.get(grading.score_level(comp["body_score"])[0], "")
            lv["bage"] = _LV_CLASS.get(
                grading.metabolic_age_level(comp["metabolic_age"], comp_age)[0], "")
        card["lv"] = lv
    goal = db.one(
        "SELECT * FROM goals WHERE member_id=? AND is_active=1 AND kind='weight' "
        "ORDER BY id DESC LIMIT 1", (m["id"],))
    if goal and card["weight_kg"] is not None:
        cur_w = card["weight_kg"]
        start = goal["start_value"] if goal["start_value"] is not None else cur_w
        target = goal["target_value"]
        gain = target > start
        done = (cur_w >= target) if gain else (cur_w <= target)
        span = target - start
        if done:
            prog = 100.0
        elif span == 0:
            prog = 0.0
        else:
            prog = max(0.0, min(100.0, (cur_w - start) / span * 100.0))
        remain = round((target - cur_w) if gain else (cur_w - target), 1)
        card["goal"] = {
            "target": target,
            "delta": remain,
            "done": done,
            "gain": gain,
            "progress": prog,
            "deadline": goal["deadline"],
        }
    return card

def _recent_rows(limit: int = 8) -> list[dict]:
    """总览「最近称重」表：跨成员的最近 N 条已归属测量（含指标等级色）。"""
    rows = db.query(
        "SELECT w.id, w.measured_at, w.weight_centi_kg, w.impedance_used_ohm, "
        "m.id AS mid, m.name, m.color, m.sex, m.birth_year, m.birth_date, m.height_cm "
        "FROM measurements w JOIN members m ON m.id = w.member_id "
        "WHERE w.excluded = 0 ORDER BY w.measured_at DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        w_kg = r["weight_centi_kg"] / 100.0
        at_date = bodycomp.date_of_iso(r["measured_at"])
        comp = bodycomp.build(w_kg, r["height_cm"], r["birth_year"], r["sex"],
                              r["impedance_used_ohm"] or 0, birth_date=r["birth_date"],
                              at=at_date)
        age = comp.get("age") or bodycomp.age_of_date(r["birth_date"], r["birth_year"], today=at_date)
        fat = bool(comp.get("has_body_fat"))
        out.append({
            "id": r["id"], "mid": r["mid"], "name": r["name"], "color": r["color"],
            "weight": round(w_kg, 1),
            "at": r["measured_at"][:16].replace("T", " "),
            "bfr": comp.get("body_fat_percent") if fat else None,
            "bmi": comp.get("bmi"),
            "vfr": comp.get("visceral_level") if fat else None,
            "lv_bfr": _LV_CLASS.get(grading.fat_level(comp["body_fat_percent"],
                          r["sex"] == "male", age)[0], "") if fat else "",
            "lv_bmi": _LV_CLASS.get(grading.bmi_level(comp["bmi"])[0], "")
                      if comp.get("bmi") else "",
            "lv_vfr": _LV_CLASS.get(grading.visceral_level(comp["visceral_level"])[0], "")
                      if fat else "",
            "score": comp.get("body_score") if fat else None,
            "lv_score": _LV_CLASS.get(grading.score_level(comp["body_score"])[0], "")
                        if fat and comp.get("body_score") is not None else "",
        })
    return out


def _pending_count() -> int:
    row = db.one("SELECT COUNT(*) AS n FROM measurements "
                 "WHERE excluded=0 AND match_state IN ('pending','unassigned')")
    return row["n"] if row else 0


def _human_since(iso: str | None) -> str:
    if not iso:
        return "暂无数据"
    from datetime import datetime
    try:
        t = datetime.fromisoformat(iso)
        now = datetime.now().astimezone()
        if t.tzinfo is None:          # 历史数据/手工补录可能是朴素时间，按本地时区处理
            t = t.astimezone()
        delta = (now - t).total_seconds()
    except ValueError:
        return "—"
    if delta < 60:
        return "刚刚"
    if delta < 3600:
        return f"{int(delta // 60)} 分钟前"
    if delta < 86400:
        return f"{int(delta // 3600)} 小时前"
    if delta < 86400 * 7:
        return f"{int(delta // 86400)} 天前"
    return iso[:10]


def _today_stats() -> dict:
    today = db.one("SELECT COUNT(*) AS n FROM measurements "
                   "WHERE date(measured_at)=date('now','localtime')")
    total = db.one("SELECT COUNT(*) AS n FROM measurements")
    last = db.one("SELECT measured_at, weight_centi_kg, member_id FROM measurements "
                  "WHERE excluded=0 ORDER BY measured_at DESC LIMIT 1")
    return {"today_count": today["n"] if today else 0,
            "total_count": total["n"] if total else 0,
            "pending": _pending_count(),
            "member_count": len(db.active_members()),
            "last_seen": ("最近称重 " + _human_since(last["measured_at"])) if last else "还没有数据",
            "last_weight": round(last["weight_centi_kg"] / 100.0, 1) if last else None,
            "last_weight_at": _human_since(last["measured_at"]) if last else None,
            "last_member_id": last["member_id"] if last else None}


def _member_stats(m) -> dict:
    latest = db.one(
        "SELECT weight_centi_kg FROM measurements WHERE member_id=? AND excluded=0 "
        "ORDER BY measured_at DESC LIMIT 1", (m["id"],))
    cnt = db.one("SELECT COUNT(*) AS n FROM measurements WHERE member_id=?", (m["id"],))
    last = db.one("SELECT measured_at FROM measurements WHERE member_id=? "
                  "ORDER BY measured_at DESC LIMIT 1", (m["id"],))
    return {
        **dict(m),
        "age": _age(m),
        "weight_kg": round(latest["weight_centi_kg"] / 100.0, 1) if latest else None,
        "count": cnt["n"] if cnt else 0,
        "last_at": (last["measured_at"][:16].replace("T", " ")) if last else None,
        "last_human": _human_since(last["measured_at"]) if last else "—",
    }


def _sparkline(values: list[float], w: int = 300, h: int = 46, pad: int = 4) -> str | None:
    """把一串体重画成 SVG polyline 的 points 字符串。"""
    if len(values) < 2:
        return None
    lo, hi = min(values), max(values)
    span = (hi - lo) or 1.0
    n = len(values)
    pts = []
    for i, v in enumerate(values):
        x = pad + (w - 2 * pad) * i / (n - 1)
        y = h - pad - (h - 2 * pad) * (v - lo) / span
        pts.append(f"{x:.1f},{y:.1f}")
    return " ".join(pts)


def _slope_per_day(rows) -> float | None:
    """最近 N 条的最小二乘斜率（kg/天）。"""
    if len(rows) < 3:
        return None
    from datetime import datetime
    xs, ys = [], []
    for r in rows:
        try:
            t = datetime.fromisoformat(r["measured_at"])
        except ValueError:
            continue
        xs.append(t.timestamp() / 86400.0)
        ys.append(r["weight_centi_kg"] / 100.0)
    if len(xs) < 3:
        return None
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    den = sum((x - mx) ** 2 for x in xs)
    if den == 0:
        return None
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / den


def _goal_view(member_id: int, current: float | None) -> dict | None:
    g = db.one("SELECT * FROM goals WHERE member_id=? AND is_active=1 "
               "ORDER BY id DESC LIMIT 1", (member_id,))
    if not g:
        return None
    target = g["target_value"]
    start = g["start_value"] if g["start_value"] is not None else current
    view = {"kind": g["kind"], "target": target, "start": start,
            "deadline": g["deadline"], "progress": 0.0, "remain": None,
            "eta": None, "done": False}
    if current is None:
        return view
    gain = (start is not None and target > start)
    done = (current >= target) if gain else (current <= target)
    span = (target - start) if start is not None else 0.0
    if done:
        view["progress"] = 1.0
    elif span != 0:
        view["progress"] = max(0.0, min(1.0, (current - start) / span))
    view["remain"] = round((target - current) if gain else (current - target), 1)
    view["done"] = done
    view["gain"] = gain

    if not view["done"]:
        rows = db.query("SELECT measured_at, weight_centi_kg FROM measurements "
                        "WHERE member_id=? AND excluded=0 "
                        "AND measured_at >= ? "
                        "ORDER BY measured_at", (member_id, db.cutoff_iso(30)))
        view["eta_note"] = None
        if len(rows) < 4:
            view["eta_note"] = "数据不足，暂不预测"
        else:
            from datetime import datetime
            try:
                t0 = datetime.fromisoformat(rows[0]["measured_at"]).replace(tzinfo=None)
                t1 = datetime.fromisoformat(rows[-1]["measured_at"]).replace(tzinfo=None)
                span_days = (t1 - t0).total_seconds() / 86400.0
            except ValueError:
                span_days = 0
            slope = _slope_per_day(rows)
            if span_days < 5:
                view["eta_note"] = "观察期不足 5 天，暂不预测"
            elif gain and (not slope or slope < 0.01):
                view["eta_note"] = "近 30 天无明显上升趋势"
            elif (not gain) and (not slope or slope > -0.01):
                view["eta_note"] = "近 30 天无明显下降趋势"
            else:
                days = ((target - current) / slope) if gain else ((current - target) / (-slope))
                if days > 730:
                    view["eta_note"] = "按当前速度超过 2 年，建议加强"
                else:
                    view["eta"] = max(1, int(round(days)))
    return view
