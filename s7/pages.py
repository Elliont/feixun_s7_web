"""记录 / 趋势 / 目标 / 设置 / 管理 / 登录 —— 通过 register(app) 挂载。"""

from __future__ import annotations
import base64
import csv
import hashlib
import hmac
import io
import json
import logging
import os
import secrets
import shutil
import sqlite3
import time
from datetime import date, datetime, timedelta
from pathlib import Path

from argon2 import PasswordHasher
from argon2.exceptions import VerifyMismatchError
from fastapi import FastAPI, File, Form, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, RedirectResponse, Response

from . import __version__, bodycomp, config, db, fatstandards, grading, notify
from .ui import (_age, _goal_view, _human_since, _row, _slope_per_day,
                 csrf_token, ctx, templates)

log = logging.getLogger("s7.pages")
HASHER = PasswordHasher()
PUBLIC = ("/login", "/setup", "/static", "/healthz", "/manifest.json", "/sw.js",
          "/favicon.ico")
LOGIN_MAX, LOGIN_WINDOW = 5, 300
_attempts: dict[str, list[float]] = {}


# ================================================================ 认证

def _sign(payload: dict) -> str:
    body = base64.urlsafe_b64encode(json.dumps(payload, separators=(",", ":")).encode()).decode()
    mac = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{body}.{mac}"


def _verify(token: str) -> dict | None:
    try:
        body, mac = token.rsplit(".", 1)
    except ValueError:
        return None
    want = hmac.new(config.SECRET_KEY.encode(), body.encode(), hashlib.sha256).hexdigest()[:32]
    if not hmac.compare_digest(mac, want):
        return None
    try:
        data = json.loads(base64.urlsafe_b64decode(body.encode()))
    except Exception:
        return None
    if data.get("exp", 0) < time.time():
        return None
    return data


def _current_user(request: Request) -> str | None:
    if config.DEV_NOAUTH:
        return "dev"
    token = request.cookies.get("sess")
    if not token:
        return None
    data = _verify(token)
    if not data:
        return None
    row = db.one("SELECT token_version FROM admin WHERE id=1")
    if not row or data.get("v") != row["token_version"]:
        return None
    return data.get("u")


def admin_exists() -> bool:
    return db.one("SELECT 1 FROM admin WHERE id=1") is not None


def ensure_admin() -> None:
    """建管理员账号：优先环境变量；都没有也不拦启动，首次访问跳 /setup。"""
    if admin_exists():
        return
    if config.ADMIN_PASSWORD:
        db.execute("INSERT INTO admin(id,username,password_hash,created_at) VALUES(1,?,?,?)",
                   (config.ADMIN_USERNAME, HASHER.hash(config.ADMIN_PASSWORD), db.now_iso()))
        log.info("已按环境变量创建管理员账号 %s", config.ADMIN_USERNAME)
        return
    log.info("还没有管理员账号：首次打开网页会跳到 /setup 让你设置用户名与密码")


def ensure_secret() -> None:
    """未配置 S7_SECRET_KEY 时，把随机密钥落到 DB，重启后不用重新登录。"""
    if os.environ.get("S7_SECRET_KEY"):
        return
    stored = db.get_setting("secret_key", "")
    if stored:
        config.SECRET_KEY = stored
        return
    value = secrets.token_urlsafe(32)
    db.set_setting("secret_key", value)
    config.SECRET_KEY = value


def check_login(username: str, password: str) -> bool:
    row = db.one("SELECT * FROM admin WHERE id=1")
    if not row or row["username"] != username:
        return False
    try:
        HASHER.verify(row["password_hash"], password)
        return True
    except (VerifyMismatchError, Exception):
        return False


def _rate_limited(ip: str) -> bool:
    now = time.time()
    hits = [t for t in _attempts.get(ip, []) if now - t < LOGIN_WINDOW]
    _attempts[ip] = hits
    return len(hits) >= LOGIN_MAX


# ================================================================ 记录页

# 记录页状态语义（简化后只有三种）：已分配 / 待分配 / 已忽略
STATE_FILTERS = {
    "auto": "excluded=0 AND match_state IN ('auto','confirmed')",
    "pending": "excluded=0 AND match_state IN ('pending','unassigned')",
    "ignored": "excluded=1",
}


def _filtered_records(member: str, date_from: str, date_to: str,
                      state: str, page: int, size: int = 20):
    where, params = ["1=1"], []
    if member and member.isdigit():
        where.append("member_id=?")
        params.append(int(member))
    if date_from:
        where.append("date(measured_at) >= date(?)")
        params.append(date_from)
    if date_to:
        where.append("date(measured_at) <= date(?)")
        params.append(date_to)
    where.append(STATE_FILTERS.get(state, "excluded=0"))
    clause = " AND ".join(where)
    total = db.one(f"SELECT COUNT(*) AS n FROM measurements WHERE {clause}", tuple(params))["n"]
    pages = max(1, (total + size - 1) // size)        # 页码在库内生页后再夹紧，越界页回落到末页
    page = max(1, min(pages, page))
    rows = db.query(
        f"SELECT * FROM measurements WHERE {clause} "
        f"ORDER BY measured_at DESC LIMIT ? OFFSET ?",
        tuple(params) + (size, (page - 1) * size))
    return rows, total, page, pages


def _record_rows(rows, members_by_id):
    out = []
    for r in rows:
        m = members_by_id.get(r["member_id"])
        w = r["weight_centi_kg"] / 100.0
        comp = {}
        if m:
            comp = bodycomp.build(w, m["height_cm"], m.get("birth_year"), m["sex"],
                                  r["impedance_used_ohm"] or 0,
                                  birth_date=m.get("birth_date"))
        out.append({
            "id": r["id"], "measured_at": r["measured_at"][:16].replace("T", " "),
            "weight": round(w, 1), "member": m["name"] if m else None,
            "member_id": r["member_id"], "imp": r["impedance_used_ohm"] or 0,
            "bfr": comp.get("body_fat_percent"), "bmi": comp.get("bmi"),
            "visceral": comp.get("visceral_level"), "score": comp.get("body_score"),
            "has_fat": bool(comp.get("has_body_fat")),
            "state": r["match_state"], "score": r["match_score"],
            "excluded": r["excluded"], "source": r["source"],
        })
    return out


# ================================================================ 趋势图（服务端 SVG）

def _chart(series, width=1160, height=420, pad_l=52, pad_r=18, pad_t=18, pad_b=34) -> dict | None:
    """series: [{name, color, points:[(ts, value)]}] → SVG 几何数据"""
    pts = [p for s in series for p in s["points"]]
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    x0, x1 = min(xs), max(xs)
    y0, y1 = min(ys), max(ys)
    if x1 == x0:
        x1 = x0 + 1
    pad = (y1 - y0) * 0.12 or 1
    y0, y1 = y0 - pad, y1 + pad
    iw, ih = width - pad_l - pad_r, height - pad_t - pad_b

    def sx(t):
        return pad_l + iw * (t - x0) / (x1 - x0)

    def sy(v):
        return pad_t + ih * (1 - (v - y0) / (y1 - y0))

    lines = []
    for s in series:
        if not s["points"]:
            continue
        coords = [(round(sx(t), 1), round(sy(v), 1)) for t, v in s["points"]]
        lines.append({"name": s["name"], "color": s["color"],
                      "points": " ".join(f"{x},{y}" for x, y in coords),
                      "dots": coords, "last": round(s["points"][-1][1], 1)})
    ticks = []
    for i in range(4):
        v = y0 + (y1 - y0) * i / 3
        ticks.append({"y": round(sy(v), 1), "label": round(v, 1)})
    d0 = datetime.fromtimestamp(x0).strftime("%m-%d")
    d1 = datetime.fromtimestamp(x1).strftime("%m-%d")
    return {"width": width, "height": height, "lines": lines, "ticks": ticks,
            "x0": pad_l, "x1": width - pad_r, "y0": pad_t, "y1": height - pad_b,
            "date0": d0, "date1": d1}



# ================================================================ 注册路由

def register(app: FastAPI) -> None:

    # ---------------- 中间件：认证 + 安全头 + CSRF cookie

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        # CSRF 令牌：整请求生命周期唯一
        csrf_tok = request.cookies.get("csrf") or secrets.token_urlsafe(24)
        request.state.csrf = csrf_tok
        # 认证
        user = "dev" if config.DEV_NOAUTH else (_current_user(request) or "")
        request.state.user = user
        if not config.DEV_NOAUTH and not path.startswith(PUBLIC):
            if not user:
                target = "/login" if admin_exists() else "/setup"
                return RedirectResponse(f"{target}?next={path}", status_code=303)
        # CSRF（写操作）
        if request.method in ("POST", "PUT", "PATCH", "DELETE") and not config.DEV_NOAUTH:
            cookie = csrf_tok
            form_tok = ""
            if request.headers.get("content-type", "").startswith(
                    ("application/x-www-form-urlencoded", "multipart/form-data")):
                body = await request.body()
                from urllib.parse import parse_qs
                form_tok = (parse_qs(body.decode(errors="ignore")).get("csrf") or [""])[0]
                request._body = body  # 供下游再次读取
            header_tok = request.headers.get("x-csrf-token", "")
            if not cookie or cookie not in (form_tok, header_tok):
                return Response("CSRF 校验失败，请刷新页面重试", status_code=403)

        response = await call_next(request)
        # CSRF cookie
        if request.cookies.get("csrf") != csrf_tok:
            response.set_cookie("csrf", csrf_tok, samesite="lax", max_age=86400 * 30)
        # 安全头
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data:; style-src 'self' 'unsafe-inline'; "
            "script-src 'self'; frame-ancestors 'none'")
        response.headers.setdefault("Permissions-Policy", "geolocation=(), camera=(), microphone=()")
        return response

    # ---------------- 首次建号 / 登录

    @app.get("/setup", response_class=HTMLResponse)
    def setup_page(request: Request, err: int = 0):
        if config.DEV_NOAUTH or _current_user(request):
            return RedirectResponse("/", status_code=303)
        if admin_exists():
            return RedirectResponse("/login", status_code=303)
        return templates.TemplateResponse("setup.html", ctx(request, err=err))

    @app.post("/setup")
    def setup_submit(request: Request, username: str = Form(""),
                     password: str = Form(""), again: str = Form("")):
        if admin_exists():
            return RedirectResponse("/login", status_code=303)
        name = username.strip()
        if not 2 <= len(name) <= 32:
            return RedirectResponse("/setup?err=1", status_code=303)
        if len(password) < 8:
            return RedirectResponse("/setup?err=2", status_code=303)
        if password != again:
            return RedirectResponse("/setup?err=3", status_code=303)
        db.execute("INSERT INTO admin(id,username,password_hash,created_at) VALUES(1,?,?,?)",
                   (name, HASHER.hash(password), db.now_iso()))
        db.audit("admin.create", None, name[:20])
        row = db.one("SELECT token_version FROM admin WHERE id=1")
        token = _sign({"u": name, "v": row["token_version"],
                       "exp": time.time() + config.SESSION_DAYS * 86400})
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie("sess", token, httponly=True, samesite="lax",
                        max_age=config.SESSION_DAYS * 86400)
        log.info("已创建管理员账号 %s", name)
        return resp

    @app.get("/login", response_class=HTMLResponse)
    def login_page(request: Request, next: str = "/", err: int = 0):
        if config.DEV_NOAUTH or _current_user(request):
            return RedirectResponse("/", status_code=303)
        if not admin_exists():
            return RedirectResponse("/setup", status_code=303)
        return templates.TemplateResponse("login.html", ctx(
            request, next=next, err=err))

    @app.post("/login")
    def login_submit(request: Request, username: str = Form(""),
                     password: str = Form(""), next: str = Form("/")):
        ip = request.client.host if request.client else "?"
        if _rate_limited(ip):
            return RedirectResponse(f"/login?err=2&next={next}", status_code=303)
        if not check_login(username.strip(), password):
            _attempts.setdefault(ip, []).append(time.time())
            db.audit("login.fail", None, username[:20])
            return RedirectResponse(f"/login?err=1&next={next}", status_code=303)
        row = db.one("SELECT token_version FROM admin WHERE id=1")
        token = _sign({"u": username.strip(), "v": row["token_version"],
                       "exp": time.time() + config.SESSION_DAYS * 86400})
        db.audit("login.ok", None, username[:20])
        resp = RedirectResponse(next or "/", status_code=303)
        resp.set_cookie("sess", token, httponly=True, samesite="lax",
                        max_age=config.SESSION_DAYS * 86400)
        return resp

    @app.post("/logout")
    def logout():
        db.audit("logout")
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("sess")
        return resp

    # ---------------- 记录

    # 称重记录的可选列（时间 / 成员 / 状态 / 操作 固定显示）
    REC_COL_SPEC = [("weight", "体重", "kg"), ("bfr", "体脂率", "%"),
                    ("bmi", "BMI", ""), ("visceral", "内脏脂肪", ""),
                    ("score", "身体评分", "分")]

    @app.get("/family")
    def family_page():
        """顶栏「家庭成员」入口：直接落到第一位成员的详情（详情页内用 tab 切换）。"""
        first = db.one("SELECT id FROM members WHERE is_active=1 ORDER BY id LIMIT 1")
        return RedirectResponse(f"/m/{first['id']}" if first else "/members",
                                status_code=303)

    @app.get("/records", response_class=HTMLResponse)
    def records_page(request: Request, member: str = "", date_from: str = "",
                     date_to: str = "", state: str = "", page: int = 1,
                     size: int = 20, ok: int = 0, imp: int = 0):
        size = size if size in (10, 20, 50) else 20
        valid = [k for k, _, _ in REC_COL_SPEC]
        picked = [c for c in request.query_params.getlist("rc") if c in valid]
        chosen = picked or ["weight", "bfr", "bmi", "visceral"]
        chosen = [k for k in valid if k in chosen]
        rows, total, page, pages = _filtered_records(
            member, date_from, date_to, state, page, size)
        members = [dict(m) for m in db.active_members()]
        by_id = {m["id"]: m for m in members}
        cols = [{"key": k, "label": label, "unit": unit, "on": k in chosen}
                for k, label, unit in REC_COL_SPEC]
        base_qs = (f"size={size}" + "".join(f"&rc={k}" for k in chosen)
                   + f"&member={member}&state={state}"
                     f"&date_from={date_from}&date_to={date_to}")
        return templates.TemplateResponse("records.html", ctx(
            request, rows=_record_rows(rows, by_id), members=members,
            total=total, page=page, pages=pages, ok=ok, imp=imp,
            size=size, size_links=[10, 20, 50], cols=cols, rec_cols=chosen,
            base_qs=base_qs, trash_n=db.trash_count(),
            q={"member": member, "date_from": date_from, "date_to": date_to,
               "state": state}))

    @app.get("/records/new", response_class=HTMLResponse)
    def record_new(request: Request, member: str = "", err: int = 0):
        members = [dict(m) for m in db.active_members()]
        now = datetime.now().strftime("%Y-%m-%dT%H:%M")
        return templates.TemplateResponse("record_form.html", ctx(
            request, members=members, err=err, now=now, sel=member))

    @app.post("/records/new")
    def record_create(request: Request, member_id: int = Form(0),
                      measured_at: str = Form(""), weight: str = Form(""),
                      impedance: str = Form("")):
        try:
            w = float(weight)
            if not (0 < w < 400):
                raise ValueError
        except ValueError:
            return RedirectResponse("/records/new?err=1", status_code=303)
        try:
            at = datetime.fromisoformat(measured_at).replace(microsecond=0)
        except ValueError:
            at = datetime.now().replace(microsecond=0)
        imp = 0.0
        try:
            if impedance.strip():
                imp = float(impedance)
                if not (100 <= imp <= 3000):
                    imp = 0.0
        except ValueError:
            imp = 0.0

        mid = member_id or None
        db.execute(
            """INSERT OR IGNORE INTO measurements(
                   member_id, device_mac, measured_at, weight_centi_kg, impedances,
                   impedance_used_ohm, data_type, source, match_state, created_at)
               VALUES(?,?,?,?,?,?,?,?,?,?)""",
            (mid, "MANUAL", at.isoformat(), int(round(w * 100)),
             str(int(imp * 10)) if imp else "", imp, 1 if imp else 0,
             "manual", "confirmed" if mid else "unassigned", db.now_iso()))
        db.audit("measure.create", str(mid or "-"), f"{w}kg @ {at:%Y-%m-%d %H:%M}")
        return RedirectResponse("/records?ok=1", status_code=303)

    @app.post("/measurements/{mid}/assign")
    def measure_assign(mid: int, member_id: str = Form(""), back: str = Form("/records")):
        # 下拉选「＋ 新增成员」→ 去新增页，建完自动认领这条（claim）
        if member_id == "new":
            return RedirectResponse(f"/members/new?claim={mid}&back={back}", status_code=303)
        target = int(member_id) if member_id.isdigit() else None
        db.execute("UPDATE measurements SET member_id=?, match_state=? WHERE id=?",
                   (target, "confirmed" if target else "unassigned", mid))
        db.audit("measure.assign", str(mid), str(target or "-"))
        return RedirectResponse(back, status_code=303)

    @app.post("/measurements/{mid}/exclude")
    def measure_exclude(mid: int, value: int = Form(1), back: str = Form("/records")):
        db.execute("UPDATE measurements SET excluded=? WHERE id=?", (1 if value else 0, mid))
        db.audit("measure.exclude", str(mid), str(value))
        return RedirectResponse(back, status_code=303)

    @app.post("/measurements/{mid}/delete")
    def measure_delete(mid: int, back: str = Form("/records")):
        # 软删除：先进回收站，14 天后自动清理，期间可还原
        moved = db.trash_move(mid)
        db.audit("measure.trash", str(mid), "ok" if moved else "missing")
        return RedirectResponse(back, status_code=303)

    # ---------------- 回收站

    def _trash_view(r) -> dict:
        now = datetime.now().astimezone()
        try:
            t = datetime.fromisoformat(str(r["deleted_at"]))
        except ValueError:
            t = now
        if t.tzinfo is None:
            t = t.replace(tzinfo=now.tzinfo)
        return {"trash_id": r["trash_id"], "orig_id": r["id"],
                "measured_at": str(r["measured_at"])[:16].replace("T", " "),
                "member_id": r["member_id"],
                "weight": round(r["weight_centi_kg"] / 100.0, 1),
                "deleted_at": t.strftime("%Y-%m-%d %H:%M"),
                "left": max(0, db.TRASH_DAYS - (now - t).days)}

    @app.get("/trash", response_class=HTMLResponse)
    def trash_page(request: Request, ok: int = -1):
        purged = db.trash_purge()
        names = {m["id"]: m["name"] for m in db.active_members()}
        rows = [_trash_view(r) for r in db.trash_rows()]
        for r in rows:
            r["member"] = names.get(r["member_id"]) or "待分配"
        return templates.TemplateResponse("trash.html", ctx(
            request, rows=rows, days=db.TRASH_DAYS, ok=ok, purged=purged))

    @app.post("/trash/{tid}/restore")
    def trash_restore(tid: int):
        done = db.trash_restore(tid)
        db.audit("measure.restore", str(tid), "ok" if done else "conflict")
        return RedirectResponse("/trash?ok=%d" % (1 if done else 0), status_code=303)

    @app.post("/trash/{tid}/delete")
    def trash_delete(tid: int):
        done = db.trash_delete(tid)
        db.audit("measure.purge", str(tid), "ok" if done else "missing")
        return RedirectResponse("/trash?ok=%d" % (1 if done else 0), status_code=303)

    @app.post("/trash/empty")
    def trash_empty():
        n = db.trash_empty()
        db.audit("measure.purge.all", None, str(n))
        return RedirectResponse("/trash?ok=1", status_code=303)


    # ---------------- 设置

    SETTING_KEYS = ("grading", "match_auto_kg", "match_pending_kg",
                    "notify_enabled", "notify_events", "pushplus_token",
                    "mqtt_enabled", "mqtt_host", "mqtt_port", "mqtt_user",
                    "mqtt_password", "mqtt_topic")

    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, ok: int = 0, err: int = 0):
        size = config.DB_PATH.stat().st_size if config.DB_PATH.exists() else 0
        return templates.TemplateResponse("settings.html", ctx(
            request, values={k: db.get_setting(k, "") for k in SETTING_KEYS},
            ok=ok, err=err, db_size=size // 1024,
            grading_choices=fatstandards.choices(),
            grading_tip=fatstandards.settings_tip(),
            members=[dict(m) for m in db.active_members()]))

    @app.post("/settings")
    async def settings_save(request: Request):
        """每张卡片各自提交，用 form_id 判断是哪一块。"""
        form = await request.form()
        fid = str(form.get("form_id", "calc"))
        if fid == "notify":
            db.set_setting("notify_enabled", "1" if form.getlist("notify_enabled") else "0")
            db.set_setting("pushplus_token", str(form.get("pushplus_token", "")).strip())
            db.set_setting("notify_events",
                           ",".join(str(v).strip() for v in form.getlist("notify_events")
                                    if str(v).strip()))
        elif fid == "mqtt":
            db.set_setting("mqtt_enabled", "1" if form.getlist("mqtt_enabled") else "0")
            for k in ("mqtt_host", "mqtt_port", "mqtt_user", "mqtt_password", "mqtt_topic"):
                if k in form:
                    db.set_setting(k, str(form[k]).strip())
        else:
            for k in ("match_auto_kg",):
                if k in form:
                    db.set_setting(k, str(form[k]).strip())
            if "grading" in form:
                std = str(form["grading"]).strip().lower()
                if std in fatstandards.STANDARDS:
                    db.set_setting("grading", std)
            fatstandards.invalidate()
        db.audit("settings.update", fid)
        return RedirectResponse("/settings?ok=1", status_code=303)

    @app.post("/settings/test-notify")
    def settings_test_notify():
        ok = notify.send("test", {"msg": "这是一条测试通知"})
        return RedirectResponse(f"/settings?ok={2 if ok else 3}", status_code=303)

    # ---------------- 管理

    @app.get("/admin")
    def admin_page():
        # 备份与安全管理已合并进设置页，这里只做兼容跳转
        return RedirectResponse("/settings", status_code=303)

    @app.post("/admin/backup")
    def admin_backup():
        """备份并直接下载；同时在 backup/ 里留一份存档。"""
        config.ensure_dirs()
        name = datetime.now().strftime("%Y%m%d-%H%M%S") + ".db"
        dst = config.DATA_DIR / "backup" / name
        src = sqlite3.connect(str(config.DB_PATH))
        out = sqlite3.connect(str(dst))
        with out:
            src.backup(out)
        out.close()
        src.close()
        db.audit("backup.create", name)
        return FileResponse(str(dst), filename=name, media_type="application/octet-stream")

    @app.post("/admin/restore")
    async def admin_restore(file: UploadFile = File(...)):
        """用上传的 .db 覆盖当前库；覆盖前先自动留一份现场。"""
        raw = await file.read()
        if not raw.startswith(b"SQLite format 3"):
            return RedirectResponse("/settings?err=3", status_code=303)
        config.ensure_dirs()
        tmp = config.DATA_DIR / "backup" / "_restore_upload.db"
        tmp.write_bytes(raw)
        try:
            probe = sqlite3.connect(str(tmp))
            tables = {r[0] for r in probe.execute(
                "SELECT name FROM sqlite_master WHERE type='table'")}
            probe.close()
            if not {"measurements", "members"} <= tables:
                return RedirectResponse("/settings?err=4", status_code=303)

            stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
            pre = config.DATA_DIR / "backup" / f"before-restore-{stamp}.db"
            live = sqlite3.connect(str(config.DB_PATH))
            snap = sqlite3.connect(str(pre))
            with snap:
                live.backup(snap)
            snap.close()

            src = sqlite3.connect(str(tmp))
            with live:
                src.backup(live)
            src.close()
            live.close()

            db.ensure_schema()          # 老备份可能缺新列，补一下
            db.audit("backup.restore", file.filename or "-", f"{len(raw)} bytes")
        finally:
            tmp.unlink(missing_ok=True)
        return RedirectResponse("/settings?ok=5", status_code=303)

    @app.get("/admin/backup/{name}")
    def admin_download(name: str):
        safe = Path(name).name
        path = config.DATA_DIR / "backup" / safe
        if not path.exists() or not safe.endswith(".db"):
            return RedirectResponse("/settings?err=1", status_code=303)
        return FileResponse(str(path), filename=safe, media_type="application/octet-stream")

    @app.post("/admin/password")
    def admin_password(old: str = Form(""), new: str = Form(""), again: str = Form("")):
        row = db.one("SELECT * FROM admin WHERE id=1")
        if not row or not check_login(row["username"], old) or len(new) < 8 or new != again:
            return RedirectResponse("/settings?err=2", status_code=303)
        db.execute("UPDATE admin SET password_hash=?, token_version=token_version+1 WHERE id=1",
                   (HASHER.hash(new),))
        db.audit("admin.password")
        resp = RedirectResponse("/login", status_code=303)
        resp.delete_cookie("sess")
        return resp

    # ---------------- 设备

    @app.get("/device", response_class=HTMLResponse)
    def device_page(request: Request, mac: str = "", ok: int = 0):
        whitelist = db.get_setting("device_mac_whitelist", "")
        last = db.one("SELECT mac, kind, at FROM device_events ORDER BY at DESC LIMIT 1")
        today = db.one("SELECT COUNT(*) AS n FROM measurements "
                       "WHERE date(measured_at)=date('now','localtime')")
        binds = db.device_bindings()
        modes = db.device_modes()
        renamed = db.device_names()
        members = [dict(m) for m in db.active_members()]
        names = {m["id"]: m["name"] for m in members}
        # 每台设备自己的「最后心跳」与「今日上报」，供选中设备的概述卡使用
        last_by_mac = {str(r["m"]).upper(): str(r["at"] or "") for r in db.query(
            "SELECT mac AS m, MAX(at) AS at FROM device_events GROUP BY mac")}
        today_by_mac = {str(r["m"]).upper(): r["n"] for r in db.query(
            "SELECT device_mac AS m, COUNT(*) AS n FROM measurements "
            "WHERE date(measured_at)=date('now','localtime') GROUP BY device_mac")}
        # 默认名「设备一/二」按首次接入时间固定，不随最近活跃度漂移
        first_by_mac = {str(r["m"]).upper(): str(r["at"] or "") for r in db.query(
            "SELECT device_mac AS m, MIN(measured_at) AS at FROM measurements GROUP BY device_mac")}
        created_by_mac = db.device_created()
        hidden = db.device_hidden()

        # 设备来源 = 连接过的心跳 ∪ 已有称重（只连过还没称的秤也要能绑定）
        merged: dict[str, dict] = {}
        for row in db.query("SELECT mac AS m, MAX(at) AS at, COUNT(*) AS n "
                            "FROM device_events GROUP BY mac"):
            key = str(row["m"]).upper()
            merged[key] = {"mac": key, "count": 0, "events": row["n"],
                           "at": str(row["at"] or "")}
        for row in db.query("SELECT device_mac AS m, MAX(measured_at) AS at, COUNT(*) AS n "
                            "FROM measurements GROUP BY device_mac"):
            key = str(row["m"]).upper()
            item = merged.setdefault(key, {"mac": key, "count": 0, "events": 0, "at": ""})
            item["count"] = row["n"]
            item["at"] = max(item["at"], str(row["at"] or ""))

        ordinal = "一二三四五六七八九十"
        devices = []
        order = sorted(merged.items(), key=lambda kv: (
            created_by_mac.get(kv[0]) or first_by_mac.get(kv[0]) or kv[1]["at"], kv[0]))
        for key, item in order:
            # 删过且之后没有新数据的设备不再列出（有新数据会自动回来）
            hidden_at = hidden.get(key)
            if hidden_at and (item["at"] or "") <= hidden_at:
                continue
            idx = len(devices) + 1
            bound = [i for i in sorted(binds.get(key, set())) if i in names]
            saved = (renamed.get(key) or "").strip()
            default = f"设备{ordinal[idx - 1]}" if idx <= len(ordinal) else f"设备 {idx}"
            seen = last_by_mac.get(key) or ""
            mode = modes.get(key) or ("pick" if bound else "all")
            if mode == "pick" and not bound:
                mode = "none"
            devices.append({"mac": key, "name": saved or default, "custom": saved,
                            "bound": bound, "mode": mode, "all": mode == "all",
                            "count": item["count"], "events": item["events"],
                            "at": item["at"][:16].replace("T", " "),
                            "today": today_by_mac.get(key, 0),
                            "last_seen": _human_since(seen) if seen else None})

        sel = next((d for d in devices if d["mac"] == mac.upper()), devices[0] if devices else None)
        return templates.TemplateResponse("device.html", ctx(
            request, devices=devices, sel=sel, members=members, ok=ok,
            whitelist=whitelist or "不限",
            last_event=_human_since(last["at"]) if last else None,
            today_reports=today["n"] if today else 0,
            self_ip=os.environ.get("S7_PUBLIC_ADDR", "127.0.0.1:30101")))

    @app.post("/device/rename")
    async def device_rename(request: Request):
        form = await request.form()
        mac = str(form.get("mac", "")).strip().upper()
        name = str(form.get("name", "")).strip()[:20]
        if mac:
            db.set_device_name(mac, name)
            db.audit("device.rename", mac, name or "(默认名)")
        return RedirectResponse(f"/device?mac={mac}", status_code=303)

    @app.post("/device/delete")
    async def device_delete(request: Request):
        form = await request.form()
        mac = str(form.get("mac", "")).strip().upper()
        mode = str(form.get("mode", "keep"))
        if mac:
            removed = db.delete_device(mac, purge_data=(mode == "purge"))
            db.audit("device.delete", mac, f"{mode}:{removed}")
        return RedirectResponse("/device", status_code=303)

    @app.post("/device/bind")
    async def device_bind(request: Request):
        form = await request.form()
        mac = str(form.get("mac", "")).strip().upper()
        picked = [int(v) for v in form.getlist("member_id") if str(v).isdigit()]
        # 三态：勾「全部成员」=all；一个都不勾 =none（不限定人）；否则 pick
        if form.getlist("all"):
            mode, picked = "all", []
        elif picked:
            mode = "pick"
        else:
            mode = "none"
        if mac:
            db.set_device_bindings(mac, picked, mode)
            db.audit("device.bind", mac, f"{mode}:{','.join(str(i) for i in picked)}")
        return RedirectResponse(f"/device?mac={mac}", status_code=303)

    # ---------------- 导出

    # ---------------- 导入（与导出的 CSV 表头一致）

    @app.post("/records/import")
    async def records_import(request: Request, file: UploadFile = File(...)):
        raw = await file.read()
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError:
            text = raw.decode("gbk", errors="replace")
        rows = list(csv.reader(io.StringIO(text)))
        if not rows:
            return RedirectResponse("/records?imp=0", status_code=303)

        head = [h.strip() for h in rows[0]]

        def col(*names):
            for n in names:
                if n in head:
                    return head.index(n)
            return -1

        ci_time = col("时间", "measured_at")
        ci_member = col("成员", "member")
        ci_weight = col("体重kg", "体重", "weight")
        ci_imp = col("阻抗Ω", "阻抗", "impedance")
        if min(ci_time, ci_member, ci_weight) < 0:
            return RedirectResponse("/records?imp=-1", status_code=303)

        members = {m["name"]: m["id"] for m in db.active_members()}
        added = skipped = 0
        for row in rows[1:]:
            if not row or len(row) <= max(ci_time, ci_member, ci_weight):
                continue
            name = row[ci_member].strip()
            if not name or name in ("待认领", "待分配"):
                continue
            mid = members.get(name)
            if not mid:
                skipped += 1
                continue
            try:
                w = float(row[ci_weight])
                if not (0 < w < 400):
                    skipped += 1
                    continue
            except ValueError:
                skipped += 1
                continue
            at_raw = row[ci_time].strip().replace("/", "-")
            at = None
            try:
                at = datetime.fromisoformat(at_raw).replace(microsecond=0)
            except ValueError:
                for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
                    try:
                        at = datetime.strptime(at_raw, fmt)
                        break
                    except ValueError:
                        at = None
            if at is None:
                skipped += 1
                continue
            # 去重：同一成员同一分钟已有记录就跳过（导出→导入往返不应产生重复）
            exists = db.one(
                "SELECT id FROM measurements WHERE member_id=? AND "
                "substr(replace(measured_at,'T',' '),1,16)=?",
                (mid, at.strftime("%Y-%m-%d %H:%M")))
            if exists:
                skipped += 1
                continue
            imp = 0.0
            if ci_imp >= 0 and len(row) > ci_imp and row[ci_imp].strip():
                try:
                    imp = float(row[ci_imp])
                    if not (100 <= imp <= 3000):
                        imp = 0.0
                except ValueError:
                    imp = 0.0
            cur = db.conn().execute(
                "INSERT OR IGNORE INTO measurements(member_id, device_mac, measured_at,"
                " weight_centi_kg, impedances, impedance_used_ohm, data_type, source,"
                " match_state, created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (mid, "IMPORT", at.isoformat(), int(round(w * 100)),
                 str(int(imp)) if imp else "", imp, 1 if imp else 0,
                 "import", "confirmed", db.now_iso()))
            if cur.rowcount:
                added += 1
            else:
                skipped += 1
        db.audit("measure.import", None, f"added={added} skipped={skipped}")
        return RedirectResponse(f"/records?imp={added}", status_code=303)

    @app.get("/api/export.csv")
    def export_csv(member: str = "", date_from: str = "", date_to: str = ""):
        rows = _filtered_records(member, date_from, date_to, "", 1, 100000)[0]
        members = {m["id"]: dict(m) for m in db.active_members()}
        head = ["时间", "成员", "体重kg", "阻抗Ω", "BMI", "体脂%", "脂肪量kg", "去脂体重kg",
                "肌肉量kg", "骨骼肌kg", "水分%", "蛋白质kg", "内脏脂肪",
                "代谢年龄", "身体评分", "体型", "来源", "状态"]
        lines = [",".join(head)]
        for r in rows:
            m = members.get(r["member_id"])
            w = r["weight_centi_kg"] / 100.0
            c = {}
            if m:
                c = bodycomp.build(w, m["height_cm"], m.get("birth_year"), m["sex"],
                                   r["impedance_used_ohm"] or 0,
                                   birth_date=m.get("birth_date"))
            lines.append(",".join(str(x) for x in [
                r["measured_at"][:16].replace("T", " "), m["name"] if m else "待分配",
                round(w, 1), r["impedance_used_ohm"] or "", c.get("bmi", ""),
                c.get("body_fat_percent", ""), c.get("fat_mass_kg", ""),
                c.get("lbm_kg", ""), c.get("muscle_kg", ""), c.get("skeletal_muscle_kg", ""),
                c.get("water_percent", ""), c.get("protein_kg", ""),
                c.get("visceral_level", ""),
                c.get("metabolic_age", ""), c.get("body_score", ""), c.get("body_type", ""),
                r["source"], r["match_state"]]))
        body = "\ufeff" + "\r\n".join(lines) + "\r\n"
        return Response(body, media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition":
                                 'attachment; filename="measurements.csv"'})

    @app.get("/api/export.json")
    def export_json(member: str = "", date_from: str = "", date_to: str = ""):
        rows = _filtered_records(member, date_from, date_to, "", 1, 100000)[0]
        members = {m["id"]: dict(m) for m in db.active_members()}
        out = []
        for r in rows:
            m = members.get(r["member_id"])
            w = r["weight_centi_kg"] / 100.0
            comp = {}
            if m:
                comp = bodycomp.build(w, m["height_cm"], m.get("birth_year"), m["sex"],
                                      r["impedance_used_ohm"] or 0,
                                      birth_date=m.get("birth_date"))
            out.append({"measured_at": r["measured_at"], "member": m["name"] if m else None,
                        "weight_kg": round(w, 2),
                        "impedance_ohm": r["impedance_used_ohm"] or 0,
                        "impedances": (r["impedances"] or "").split(",") if r["impedances"] else [],
                        "source": r["source"], "state": r["match_state"],
                        "excluded": bool(r["excluded"]), "comp": comp})
        return {"ok": True, "data": out}

    @app.get("/manifest.json")
    def manifest():
        return {
            "name": "韭菜称重", "short_name": "韭菜称重", "start_url": "/",
            "display": "standalone", "background_color": "#F6F8FA",
            "theme_color": "#00A98F", "lang": "zh-CN",
            "icons": [{"src": "/static/icon-192.png", "sizes": "192x192",
                       "type": "image/png"},
                      {"src": "/static/icon-512.png", "sizes": "512x512",
                       "type": "image/png"}],
        }

    @app.get("/sw.js")
    def service_worker():
        js = """
const CACHE = 's7-v1';
const ASSETS = ['/static/app.css', '/static/app.js', '/static/icon-192.png'];
self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then((c) => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});
self.addEventListener('activate', (e) => {
  e.waitUntil(caches.keys().then((ks) => Promise.all(
    ks.filter((k) => k !== CACHE).map((k) => caches.delete(k)))).then(() => self.clients.claim()));
});
self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || !url.pathname.startsWith('/static/')) return;
  // 网络优先，失败回落缓存（保证样式更新即时生效，断网仍可用）
  e.respondWith(fetch(e.request)
    .then((res) => {
      const copy = res.clone();
      caches.open(CACHE).then((c) => c.put(e.request, copy)).catch(() => {});
      return res;
    })
    .catch(() => caches.match(e.request)));
});
"""
        return Response(js, media_type="application/javascript")
