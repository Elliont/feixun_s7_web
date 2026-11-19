"""MQTT / HomeAssistant 接口（本轮只留接口，不实现）。

启用时只需实现 publish()，数据模型与前端无需改动。
"""

from __future__ import annotations

import logging

from . import db

log = logging.getLogger("s7.mqtt")


def enabled() -> bool:
    return db.get_setting("mqtt_enabled", "0") == "1"


def publish(topic_suffix: str, payload: dict) -> None:
    """占位实现：当前不做任何事。"""
    if not enabled():
        return
    log.debug("MQTT 未实现，忽略 %s %s", topic_suffix, payload)
