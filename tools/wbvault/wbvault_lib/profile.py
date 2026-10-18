"""读本机账号档案里的昵称（WorkBuddy 把登录邮箱存在这里）。

`~/.workbuddy-ai/storage/skeleton/account-snapshot.json` 只保存**当前**登录账号，
结构是 `{"primary": {"uid": ..., "nickname": "xxx@gmail.com"}}`。
登录态文件里的 nickname 是密文（`$wbEncrypted`），这里这份是明文，够用了。

采集时机：常驻监听每次发现登录态变化、以及 App 切号后重写这份档案时，
把邮箱补进账号库；切到某个账号一次，它的邮箱就记住了。
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Optional


def snapshot_path() -> Path:
    return Path.home() / ".workbuddy-ai" / "storage" / "skeleton" / "account-snapshot.json"


def nickname_for(uid: str) -> Optional[str]:
    """返回该 uid 的昵称；只有像邮箱的才返回（纯昵称/手机号不当邮箱用）。"""
    try:
        data = json.loads(snapshot_path().read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict):
        return None
    for key in ("primary", "current", "active"):
        item = data.get(key)
        if isinstance(item, dict) and str(item.get("uid") or "") == uid:
            name = str(item.get("nickname") or "").strip()
            if "@" in name and " " not in name:
                return name
            return None
    return None
