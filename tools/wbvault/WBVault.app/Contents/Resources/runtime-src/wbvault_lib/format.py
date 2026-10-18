"""终端输出格式化（含中文宽度对齐）。"""

from __future__ import annotations

import time
import unicodedata
from typing import Iterable, List, Sequence

RESET = "\033[0m"
DIM = "\033[2m"
BOLD = "\033[1m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"

_USE_COLOR = True


def set_color(enabled: bool) -> None:
    global _USE_COLOR
    _USE_COLOR = enabled


def paint(text: str, color: str) -> str:
    if not _USE_COLOR:
        return text
    return "%s%s%s" % (color, text, RESET)


def width(text: str) -> int:
    total = 0
    for char in text:
        if unicodedata.combining(char):
            continue
        total += 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
    return total


def pad(text: str, target: int, align: str = "left") -> str:
    gap = max(0, target - width(text))
    if align == "right":
        return " " * gap + text
    return text + " " * gap


def truncate(text: str, limit: int) -> str:
    if width(text) <= limit:
        return text
    out = ""
    used = 0
    for char in text:
        w = 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1
        if used + w > limit - 1:
            break
        out += char
        used += w
    return out + "…"


def table(headers: Sequence[str], rows: Iterable[Sequence[str]], aligns: Sequence[str] = None) -> str:
    rows = [list(map(str, row)) for row in rows]
    aligns = list(aligns or ["left"] * len(headers))
    widths = [width(h) for h in headers]
    for row in rows:
        for i, cell in enumerate(row):
            widths[i] = max(widths[i], width(cell))
    lines = ["  ".join(pad(h, widths[i]) for i, h in enumerate(headers))]
    lines.append("  ".join("─" * max(widths[i], 1) for i in range(len(headers))))
    for row in rows:
        lines.append("  ".join(pad(cell, widths[i], aligns[i]) for i, cell in enumerate(row)))
    return "\n".join(lines)


def ago(ms: int) -> str:
    if not ms:
        return "-"
    delta = int(time.time() - ms / 1000)
    if delta < 0:
        return "刚刚"
    if delta < 60:
        return "%d 秒前" % delta
    if delta < 3600:
        return "%d 分钟前" % (delta // 60)
    if delta < 86400:
        return "%d 小时前" % (delta // 3600)
    return "%d 天前" % (delta // 86400)


def in_days(ms: int) -> str:
    if not ms:
        return "-"
    left = int((ms - time.time() * 1000) // 86400000)
    if left < 0:
        return "已过期"
    if left == 0:
        return "今天"
    return "%d 天" % left


def short_uid(uid: str) -> str:
    return (uid or "")[:8]


def mask_uin(uin: str) -> str:
    if not uin:
        return "-"
    if len(uin) <= 4:
        return uin
    return "…" + uin[-4:]
