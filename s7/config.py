"""配置：全部来自环境变量，无硬编码密钥。"""

from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("S7_DATA_DIR", "/data"))
DB_PATH = Path(os.environ.get("S7_DB_PATH", str(DATA_DIR / "s7.db")))

# 网络
INGEST_HOST = os.environ.get("S7_INGEST_HOST", "0.0.0.0")
INGEST_PORT = int(os.environ.get("S7_INGEST_PORT", "30101"))
WEB_HOST = os.environ.get("S7_WEB_HOST", "0.0.0.0")
WEB_PORT = int(os.environ.get("S7_WEB_PORT", "8080"))

# 认证
ADMIN_USERNAME = os.environ.get("S7_ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("S7_ADMIN_PASSWORD", "")
DEV_NOAUTH = os.environ.get("S7_DEV_NOAUTH", "0") == "1"
SECRET_KEY = os.environ.get("S7_SECRET_KEY", "")
if not SECRET_KEY:                              # 开发便利：未配置时用随机值（重启失效）
    import secrets as _secrets
    SECRET_KEY = _secrets.token_urlsafe(32)
SESSION_DAYS = int(os.environ.get("S7_SESSION_DAYS", "30"))

# 运行
DEBUG = os.environ.get("S7_DEBUG", "0") == "1"
TZ = os.environ.get("TZ", "Asia/Shanghai")
# HTTPS 反代后应设为 1，给会话/CSRF cookie 加 Secure 标志。
# 默认 0 以兼容直接以 http 访问的自建部署（Secure cookie 在 http 下不会写入）。
COOKIE_SECURE = os.environ.get("S7_COOKIE_SECURE", "0") == "1"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    (DATA_DIR / "backup").mkdir(parents=True, exist_ok=True)
