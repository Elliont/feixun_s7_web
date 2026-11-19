"""通知接口（预留实现）。

当前实现 pushplus；未配置 token 时静默跳过。
事件：measure.pending / measure.unassigned / goal.achieved / device.offline
"""

from __future__ import annotations

import json
import logging
import urllib.request

from . import db

log = logging.getLogger("s7.notify")

ENDPOINT = "https://www.pushplus.plus/send"


def enabled() -> bool:
    return db.get_setting("notify_enabled", "0") == "1"


def _events() -> set[str]:
    raw = db.get_setting("notify_events", "unassigned")
    return {x.strip() for x in raw.split(",") if x.strip()}


def send(event: str, payload: dict, aliases: list[str] | None = None,
         force: bool = False) -> bool:
    """发送通知；失败只记日志，不影响主流程。

    aliases：命中的任一个订阅事件即可发送（例如一次称重同时匹配
    measure.any / measure.member.3 / measure.unassigned）。
    force：跳过启用开关与事件订阅过滤（用于设置页测试通知）。
    """
    keys = [event] + list(aliases or [])
    if not force and (not enabled() or not any(k in _events() for k in keys)):
        return False
    token = db.get_setting("pushplus_token", "")
    if not token:
        log.warning("通知已开启但未配置 pushplus token")
        return False

    title = payload.get("title") or {
        "measure.pending": "有称重数据待分配",
        "measure.unassigned": "有称重数据待分配",
        "measure": "新称重",
        "goal.achieved": "目标达成 🎉",
        "device.offline": "秤离线提醒",
    }.get(event, "韭菜称重")
    content = "%s<br>%s" % (title, json.dumps(payload, ensure_ascii=False))

    body = json.dumps({"token": token, "title": title,
                       "content": content, "template": "html"},
                      ensure_ascii=False).encode()
    for attempt in (1, 2):
        try:
            req = urllib.request.Request(
                ENDPOINT, data=body,
                headers={"Content-Type": "application/json"})
            with urllib.request.urlopen(req, timeout=8) as resp:
                if resp.status == 200:
                    return True
        except Exception:
            log.warning("pushplus 第 %d 次发送失败", attempt, exc_info=attempt == 2)
    return False
