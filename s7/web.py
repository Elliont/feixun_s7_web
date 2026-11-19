"""Web 服务：总览、成员、成员详情。

记录/趋势/目标/设置等页面在 pages.py，通过 register(app) 挂载。
"""

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles

from . import __version__, bodycomp, config, db, grading, notify
from .ui import (PALETTE, _age, _goal_view, _member_card, _member_stats,
                 _recent_rows, _row, _sparkline, _today_stats,
                 base_dir, ctx, templates)

log = logging.getLogger("s7.web")


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时初始化数据库与管理员账号（替代已废弃的 @app.on_event）。"""
    db.init_db()
    db.trash_purge()                     # 回收站到期清理（14 天）
    from .pages import ensure_admin, ensure_secret
    ensure_secret()                      # 会话密钥落库，重启不掉登录
    ensure_admin()
    yield


app = FastAPI(title="韭菜称重", version=__version__, docs_url=None, redoc_url=None,
              lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(base_dir() / "static")), name="static")


# ---------------------------------------------------------------- 健康检查 / API

@app.get("/healthz")
def healthz():
    try:
        db.one("SELECT 1")
        return {"ok": True, "version": __version__}
    except Exception as exc:  # pragma: no cover
        return JSONResponse({"ok": False, "error": {"code": "DB", "message": str(exc)}},
                            status_code=500)


@app.get("/api/overview")
def api_overview():
    members = [dict(m) for m in db.active_members()]
    cards = [_member_card(m) for m in members]
    pending = [dict(r) for r in db.query(
        "SELECT id, member_id, measured_at, weight_centi_kg, match_state, match_score "
        "FROM measurements WHERE excluded=0 AND match_state IN ('pending','unassigned') "
        "ORDER BY measured_at DESC LIMIT 20")]
    for p in pending:
        p["weight_kg"] = round(p["weight_centi_kg"] / 100.0, 2)
    return {"ok": True, "data": {
        "stats": _today_stats(), "members": cards, "pending": pending,
        "debug": config.DEV_NOAUTH, "version": __version__,
        "notify": notify.enabled(),
    }}


# ---------------------------------------------------------------- 总览

@app.get("/", response_class=HTMLResponse)
def index(request: Request, ok: int = 0):
    members = [dict(m) for m in db.active_members()]
    cards = [_member_card(m) for m in members]
    pending = [dict(r) for r in db.query(
        "SELECT id, measured_at, weight_centi_kg, match_state, match_score "
        "FROM measurements WHERE excluded=0 AND match_state IN ('pending','unassigned') "
        "ORDER BY measured_at DESC LIMIT 20")]
    for p in pending:
        p["weight_kg"] = round(p["weight_centi_kg"] / 100.0, 2)
    from datetime import date
    today = date.today().isoformat()
    return templates.TemplateResponse("index.html", ctx(
        request, stats=_today_stats(), members=cards, pending=pending,
        recent=_recent_rows(8), ok=ok, today=today))


# ---------------------------------------------------------------- 成员

@app.get("/members", response_class=HTMLResponse)
def members_page(request: Request, ok: int = 0, deleted: int = 0):
    rows = db.query("SELECT * FROM members ORDER BY is_active DESC, id")
    active = [_member_stats(m) for m in rows if m["is_active"]]
    inactive = [_member_stats(m) for m in rows if not m["is_active"]]
    return templates.TemplateResponse("members.html", ctx(
        request, members=active, inactive=inactive, ok=ok, deleted=deleted))


def _back_url(back: str, default: str = "/") -> str:
    """只允许站内相对路径，防开放重定向。"""
    b = (back or "").strip()
    if b.startswith("/") and not b.startswith("//") and "\\" not in b:
        return b
    return default


def _redirect_ok(path: str) -> RedirectResponse:
    """带成功标记跳转（已有查询串则用 & 拼接）。"""
    sep = "&" if "?" in path else "?"
    return RedirectResponse(f"{path}{sep}ok=1", status_code=303)

def _form_ctx(member=None, form=None, error=None, claim=0, back="/", goal=None):
    from datetime import date
    max_year = date.today().year
    if form is None:
        if member:
            form = {"name": member["name"], "sex": member["sex"],
                    "birth_date": member["birth_date"] or f"{member['birth_year']}-01-01",
                    "height_cm": member["height_cm"]}
        else:
            form = {"name": "", "sex": "male", "birth_date": f"{max_year - 35}-01-01",
                    "height_cm": 170}
    return {"member": member, "form": form, "error": error, "max_year": max_year,
            "today": date.today().isoformat(), "claim": claim, "back": back,
            "goal": goal}


@app.get("/members/new", response_class=HTMLResponse)
def member_new(request: Request, claim: int = 0, back: str = "/"):
    """claim = 待认领测量 ID（保存后自动认领）；back = 保存后回跳的站内路径。"""
    return templates.TemplateResponse(
        "member_form.html", ctx(request, **_form_ctx(claim=claim, back=_back_url(back))))


def _goal_for(member_id):
    """成员当前目标视图（没设定目标时返回 None）。"""
    if not member_id:
        return None
    from .ui import _goal_view
    latest = db.one("SELECT weight_centi_kg FROM measurements WHERE member_id=? AND excluded=0 "
                    "ORDER BY measured_at DESC LIMIT 1", (member_id,))
    cur = round(latest["weight_centi_kg"] / 100.0, 2) if latest else None
    return _goal_view(member_id, cur)


@app.get("/members/{member_id}", response_class=HTMLResponse)
def member_edit(request: Request, member_id: int):
    m = db.member_by_id(member_id)
    if not m:
        return RedirectResponse("/members", status_code=303)
    return templates.TemplateResponse("member_form.html", ctx(
        request, **_form_ctx(member=m, goal=_goal_for(member_id))))


def _validate(name, sex, birth_date, height_cm, member_id):
    from datetime import date
    name = (name or "").strip()
    if not (1 <= len(name) <= 20):
        return "姓名需要 1–20 个字"
    dup = db.one("SELECT id FROM members WHERE name=? AND id<>?", (name, member_id or 0))
    if dup:
        return f"「{name}」已存在，请换一个名字"
    if sex not in ("male", "female"):
        return "性别无效"
    today = date.today()
    try:
        bd = date.fromisoformat(birth_date)
    except (ValueError, TypeError):
        return "出生日期格式不正确"
    if not (date(1900, 1, 1) <= bd <= today):
        return "出生日期需在 1900-01-01 与今天之间"
    if not (100 <= height_cm <= 220):
        return "身高需在 100–220 cm"
    return None


def _save_goal(member_id: int, target: str, start: str = "") -> None:
    """成员表单里的目标：留空即取消，填了才建立/更新。"""
    active = db.one("SELECT id FROM goals WHERE member_id=? AND kind='weight' "
                    "AND is_active=1", (member_id,))
    raw = (target or "").strip()
    if not raw:
        if active:                               # 留空 = 不要目标
            db.execute("UPDATE goals SET is_active=0 WHERE member_id=? AND kind='weight' "
                       "AND is_active=1", (member_id,))
            db.audit("goal.clear", str(member_id))
        return
    try:
        val = float(raw)
        if not (20 <= val <= 300):
            return
    except ValueError:
        return
    start_val = None
    raw_start = (start or "").strip()
    if raw_start:
        try:
            s = float(raw_start)
            if 20 <= s <= 300:
                start_val = round(s, 1)
        except ValueError:
            start_val = None
    if start_val is None:                        # 没填就用当前体重当起点
        latest = db.one("SELECT weight_centi_kg FROM measurements WHERE member_id=? "
                        "AND excluded=0 ORDER BY measured_at DESC LIMIT 1", (member_id,))
        start_val = round(latest["weight_centi_kg"] / 100.0, 2) if latest else None
    cur = db.one("SELECT id, start_value, target_value, deadline FROM goals WHERE member_id=? "
                 "AND kind='weight' AND is_active=1 ORDER BY id DESC LIMIT 1", (member_id,))
    if cur and abs(float(cur["target_value"]) - val) < 0.001 \
            and (start_val is None or cur["start_value"] is None
                 or abs(float(cur["start_value"]) - start_val) < 0.001):
        if cur["deadline"]:                      # 期限功能已下线，把历史值清掉
            db.execute("UPDATE goals SET deadline=NULL WHERE id=?", (cur["id"],))
        return                                   # 没有变化，不新建
    db.execute("UPDATE goals SET is_active=0 WHERE member_id=? AND kind='weight' "
               "AND is_active=1", (member_id,))
    db.execute("INSERT INTO goals(member_id,kind,start_value,target_value,deadline,created_at) "
               "VALUES(?,?,?,?,?,?)",
               (member_id, "weight", start_val, val, None, db.now_iso()))
    db.audit("goal.update", str(member_id), f"-> {val}")


@app.post("/members/save")
def member_save(
    request: Request,
    name: str = Form(""), sex: str = Form("male"),
    birth_date: str = Form(""), height_cm: int = Form(0),
    id: str = Form(""), claim: str = Form(""), back: str = Form("/"),
    goal_target: str = Form(""), goal_start: str = Form(""),
):
    member_id = int(id) if id else None
    err = _validate(name, sex, birth_date, height_cm, member_id)
    if err:
        member = db.member_by_id(member_id) if member_id else None
        form = {"name": name, "sex": sex, "birth_date": birth_date,
                "height_cm": height_cm}
        return templates.TemplateResponse(
            "member_form.html",
            ctx(request, **_form_ctx(member, form, err, claim=claim,
                                     back=_back_url(back), goal=_goal_for(member_id))),
            status_code=422)

    if member_id:
        db.execute("UPDATE members SET name=?,sex=?,birth_date=?,birth_year=?,height_cm=? "
                   "WHERE id=?",
                   (name.strip(), sex, birth_date, int(birth_date[:4]),
                    height_cm, member_id))
        db.audit("member.update", str(member_id), name)
        _save_goal(member_id, goal_target, goal_start)
        return _redirect_ok("/members")

    used = {r["color"] for r in db.query("SELECT color FROM members")}
    color = next((c for c in PALETTE if c not in used), PALETTE[0])
    mid = db.execute(
        "INSERT INTO members(name,sex,birth_year,birth_date,height_cm,"
        "activity_level,waist_cm,color,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (name.strip(), sex, int(birth_date[:4]), birth_date, height_cm,
         2, None, color, db.now_iso()))
    db.audit("member.create", str(mid), name)
    _save_goal(mid, goal_target, goal_start)
    # 待认领闭环：新增成员后把来源测量自动认领给新成员
    if claim.isdigit() and int(claim) > 0:
        db.execute(
            "UPDATE measurements SET member_id=?, match_state='confirmed' "
            "WHERE id=? AND member_id IS NULL AND excluded=0",
            (mid, int(claim)))
        db.audit("measure.assign", claim, str(mid))
    return _redirect_ok(_back_url(back))


@app.post("/members/{member_id}/delete")
def member_delete(member_id: int):
    db.execute("UPDATE members SET is_active=0 WHERE id=?", (member_id,))
    db.audit("member.disable", str(member_id))
    return RedirectResponse("/members?deleted=1", status_code=303)


@app.post("/members/{member_id}/restore")
def member_restore(member_id: int):
    db.execute("UPDATE members SET is_active=1 WHERE id=?", (member_id,))
    db.audit("member.restore", str(member_id))
    return RedirectResponse("/members?ok=1", status_code=303)


# ---------------------------------------------------------------- 成员详情

@app.get("/m/{member_id}", response_class=HTMLResponse)
def member_detail(request: Request, member_id: int, rp: int = 1, rs: int = 10,
                  sort: str = "time", dir: str = "desc"):
    m = db.member_by_id(member_id)
    if not m:
        return RedirectResponse("/members", status_code=303)

    md = _row(m)
    age = bodycomp.age_of_date(md.get("birth_date"), md.get("birth_year"))
    male = md["sex"] == "male"

    latest = db.one("SELECT * FROM measurements WHERE member_id=? AND excluded=0 "
                    "ORDER BY measured_at DESC LIMIT 1", (member_id,))
    prev = db.one("SELECT * FROM measurements WHERE member_id=? AND excluded=0 "
                  "ORDER BY measured_at DESC LIMIT 1 OFFSET 1", (member_id,))

    weight = round(latest["weight_centi_kg"] / 100.0, 2) if latest else None

    # 体成分取最近一次「测到阻抗」的称重：用户穿袜称重时仍能看到上次体成分
    comp_src = latest
    if latest and not (latest["impedance_used_ohm"] or 0):
        comp_src = db.one(
            "SELECT * FROM measurements WHERE member_id=? AND excluded=0 "
            "AND impedance_used_ohm > 0 ORDER BY measured_at DESC LIMIT 1", (member_id,))
    comp, comp_at = {}, None
    if comp_src:
        comp_w = comp_src["weight_centi_kg"] / 100.0
        comp = bodycomp.build(comp_w, md["height_cm"], md.get("birth_year"), md["sex"],
                              comp_src["impedance_used_ohm"] or 0,
                              birth_date=md.get("birth_date"))
        comp["height_cm"] = md["height_cm"]
        comp_at = comp_src["measured_at"]
    comp_is_latest = bool(latest and comp_at == latest["measured_at"])

    delta_last = None
    if latest and prev:
        delta_last = round((latest["weight_centi_kg"] - prev["weight_centi_kg"]) / 100.0, 2)

    week = db.query("SELECT weight_centi_kg FROM measurements WHERE member_id=? "
                    "AND excluded=0 AND measured_at >= ? "
                    "ORDER BY measured_at DESC", (member_id, db.cutoff_iso(7)))
    avg7 = round(sum(r["weight_centi_kg"] for r in week) / len(week) / 100.0, 2) if week else None
    delta_avg = round(weight - avg7, 2) if (weight is not None and avg7 is not None) else None

    series = db.query("SELECT measured_at, weight_centi_kg FROM measurements "
                      "WHERE member_id=? AND excluded=0 "
                      "AND measured_at >= ? ORDER BY measured_at",
                      (member_id, db.cutoff_iso(30)))
    spark = _sparkline([r["weight_centi_kg"] / 100.0 for r in series])

    # ---- 体重记录：分页 + 排序 + 按需显示列 + 每行可点进单次详情
    col_spec = [("weight", "体重", "kg"), ("bfr", "体脂率", "%"), ("bmi", "BMI", ""),
                ("vfr", "内脏脂肪", ""), ("muscle", "肌肉量", "kg"),
                ("water", "水分率", "%"), ("score", "身体评分", "分")]
    valid = [k for k, _, _ in col_spec]
    chosen = [c for c in request.query_params.getlist("rc") if c in valid] or ["weight", "bfr", "bmi"]
    chosen = [k for k in valid if k in chosen]          # 固定顺序，避免来回跳
    sort = sort if sort in ("time",) + tuple(valid) else "time"
    dir = "asc" if dir == "asc" else "desc"

    total_rec = db.one("SELECT COUNT(*) n FROM measurements "
                       "WHERE member_id=? AND excluded=0", (member_id,))["n"]
    rs = rs if rs in (10, 20, 50) else 10
    pages_rec = max(1, (total_rec + rs - 1) // rs)
    rp = max(1, min(pages_rec, rp))

    def _calc(r) -> dict:
        w = r["weight_centi_kg"] / 100.0
        c = bodycomp.build(w, md["height_cm"], md.get("birth_year"), md["sex"],
                           r["impedance_used_ohm"] or 0, birth_date=md.get("birth_date"))
        return {"id": r["id"],
                "measured_at": r["measured_at"][:16].replace("T", " "),
                "time": r["measured_at"],
                "weight": round(w, 1),
                "bfr": c.get("body_fat_percent"), "bmi": c.get("bmi"),
                "vfr": c.get("visceral_level"), "muscle": c.get("muscle_kg"),
                "water": c.get("water_percent"), "score": c.get("body_score"),
                "has_fat": bool(c.get("has_body_fat"))}

    order_sql = "measured_at" if sort == "time" else "weight_centi_kg"
    if sort in ("time", "weight"):
        # 存储列直接交给 SQLite 排序，分页仍在库内完成
        raw = db.query(
            f"SELECT * FROM measurements WHERE member_id=? AND excluded=0 "
            f"ORDER BY {order_sql} {dir.upper()}, id {dir.upper()} LIMIT ? OFFSET ?",
            (member_id, rs, (rp - 1) * rs))
        rec_rows = [_calc(r) for r in raw]
    else:
        # 体成分指标是算出来的，整段取出后在内存里排序再切页
        rec_rows = [_calc(r) for r in db.query(
            "SELECT * FROM measurements WHERE member_id=? AND excluded=0",
            (member_id,))]
        rec_rows.sort(key=lambda x: (x["measured_at"], x["id"]))
        filled = [x for x in rec_rows if x[sort] is not None]
        empty = [x for x in rec_rows if x[sort] is None]
        filled.sort(key=lambda x: x[sort], reverse=(dir == "desc"))
        rec_rows = (filled + empty)[(rp - 1) * rs: rp * rs]     # 空值永远排在最后

    def _url(**over):
        q = {"rs": rs, "sort": sort, "dir": dir, "rc": chosen}
        q.update(over)
        out = [f"rs={q['rs']}", f"sort={q['sort']}", f"dir={q['dir']}"]
        out += [f"rc={c}" for c in q["rc"]]
        return f"/m/{member_id}?" + "&".join(out)

    cols = [{"key": k, "label": label, "unit": unit, "on": k in chosen,
             "url": _url(rc=([c for c in chosen if c != k] if k in chosen else chosen + [k]))}
            for k, label, unit in col_spec]
    sort_heads = [{"key": "time", "label": "时间"}]
    sort_heads += [{"key": c["key"], "label": c["label"]} for c in cols if c["on"]]
    for h in sort_heads:
        nxt = "asc" if (h["key"] == sort and dir == "desc") else \
              ("desc" if h["key"] == sort else "desc")
        h["url"] = _url(sort=h["key"], dir=nxt)
        h["ind"] = "↓" if (h["key"] == sort and dir == "desc") else \
                   ("↑" if h["key"] == sort else "")
    size_links = [{"n": n, "url": _url(rs=n)} for n in (10, 20, 50)]

    return templates.TemplateResponse("member_detail.html", ctx(
        request,
        m={**md, "age": age}, male=male, latest=latest, weight=weight, comp=comp,
        member_tabs=[{"id": r["id"], "name": r["name"], "color": r["color"]}
                     for r in db.active_members()],
        cards=grading.cards(comp, age, male),
        delta_last=delta_last, avg7=avg7, delta_avg=delta_avg,
        spark=spark,
        spark_min=round(min(r["weight_centi_kg"] for r in series) / 100.0, 1) if series else None,
        spark_max=round(max(r["weight_centi_kg"] for r in series) / 100.0, 1) if series else None,
        goal=_goal_view(member_id, weight),
        rec_rows=rec_rows, rec_total=total_rec, rec_page=rp, rec_pages=pages_rec,
        rec_size=rs, rec_cols=chosen, cols=cols,
        sort_heads=sort_heads, sort_key=sort, sort_dir=dir, size_links=size_links,
        rec_qs="&".join([f"rc={c}" for c in chosen] + [f"sort={sort}", f"dir={dir}"])))


@app.get("/m/{member_id}/series.json")
def member_series(member_id: int, days: int = 365):
    """成员各项指标的时序数据（给详情页趋势图用）。读时计算，只回近 N 天。"""
    m = db.member_by_id(member_id)
    if not m:
        return JSONResponse({"ok": False, "error": "member not found"}, status_code=404)
    md = _row(m)
    days = days if days in (30, 90, 180, 365) else 365
    rows = db.query(
        "SELECT id, measured_at, weight_centi_kg, impedance_used_ohm FROM measurements "
        "WHERE member_id=? AND excluded=0 "
        "AND measured_at >= ? ORDER BY measured_at",
        (member_id, db.cutoff_iso(days)))
    points = []
    for r in rows:
        w = r["weight_centi_kg"] / 100.0
        c = bodycomp.build(w, md["height_cm"], md.get("birth_year"), md["sex"],
                           r["impedance_used_ohm"] or 0, birth_date=md.get("birth_date"))
        points.append({
            "t": r["measured_at"][:16].replace("T", " "),
            "d": r["measured_at"][:10],
            "weight": round(w, 1),
            "rid": r["id"],
            "bmi": c.get("bmi"),
            "bfr": c.get("body_fat_percent"),
            "score": c.get("body_score"),
            "bage": c.get("metabolic_age"),
            "muscle": c.get("muscle_kg"),
            "smm": c.get("skeletal_muscle_kg"),
            "vfr": c.get("visceral_level"),
            "water": c.get("water_percent"),
            "protein": c.get("protein_kg"),
            "fat_mass": c.get("fat_mass_kg"),
            "lbm": c.get("lbm_kg"),
        })
    return JSONResponse({"ok": True, "member": md["name"], "days": days, "points": points})



@app.get("/m/{member_id}/r/{rid}", response_class=HTMLResponse)
def measurement_detail(request: Request, member_id: int, rid: int):
    """单次称重详情：那一次的完整体成分。"""
    m = db.member_by_id(member_id)
    row = db.one("SELECT * FROM measurements WHERE id=? AND member_id=?", (rid, member_id))
    if not m or not row:
        return RedirectResponse(f"/m/{member_id}", status_code=303)

    md = _row(m)
    age = bodycomp.age_of_date(md.get("birth_date"), md.get("birth_year"))
    male = md["sex"] == "male"
    w = row["weight_centi_kg"] / 100.0
    comp = bodycomp.build(w, md["height_cm"], md.get("birth_year"), md["sex"],
                          row["impedance_used_ohm"] or 0, birth_date=md.get("birth_date"))
    comp["height_cm"] = md["height_cm"]

    nxt = db.one("SELECT id, measured_at FROM measurements WHERE member_id=? AND excluded=0 "
                 "AND measured_at < ? ORDER BY measured_at DESC LIMIT 1",
                 (member_id, row["measured_at"]))
    prv = db.one("SELECT id, measured_at FROM measurements WHERE member_id=? AND excluded=0 "
                 "AND measured_at > ? ORDER BY measured_at ASC LIMIT 1",
                 (member_id, row["measured_at"]))
    delta = None
    if nxt:
        prev_w = db.one("SELECT weight_centi_kg FROM measurements WHERE id=?",
                        (nxt["id"],))["weight_centi_kg"] / 100.0
        delta = round(w - prev_w, 2)

    return templates.TemplateResponse("measurement_detail.html", ctx(
        request, m={**md, "age": age}, male=male, rec=row, weight=round(w, 2), comp=comp,
        cards=grading.cards(comp, age, male), delta=delta,
        at=row["measured_at"][:16].replace("T", " "),
        newer=prv, older=nxt))


# ---------------------------------------------------------------- 启动

from . import pages  # noqa: E402  记录/趋势/目标/设置/登录

pages.register(app)


def main() -> None:
    import os
    import uvicorn
    logging.basicConfig(level=logging.DEBUG if config.DEBUG else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s %(message)s")
    db.init_db()
    reload = os.environ.get("S7_DEV_RELOAD", "0") == "1"
    uvicorn.run("s7.web:app" if reload else app,
                host=config.WEB_HOST, port=config.WEB_PORT,
                reload=reload, reload_dirs=["/app/s7"] if reload else None,
                log_level="debug" if config.DEBUG else "info")


if __name__ == "__main__":
    main()
