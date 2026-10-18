"""路径解析：WorkBuddy 登录态文件与本地账号库的位置。

注意：登录态不在 ~/.workbuddy-ai 下，而在
~/Library/Application Support/CodeBuddyExtension/Data/Public/auth/
这是 CodeBuddyExtension 的 sharedDataPath（basePath/Data/Public）。
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import List, Optional

APP_SUPPORT = Path.home() / "Library" / "Application Support"

DEFAULT_AUTH_DIR = APP_SUPPORT / "CodeBuddyExtension" / "Data" / "Public" / "auth"
DEFAULT_VAULT_ROOT = APP_SUPPORT / "WorkBuddyAccountVault"

AUTH_FILE_SUFFIX = ".info"
OBSERVATION_MARKER = ".observation.json"

# 优先使用新版的 authentication id；旧版 workbuddy-desktop 仍可能残留
PREFERRED_AUTH_IDS = ("workbuddy-desktop-ai", "workbuddy-desktop")


class PathError(RuntimeError):
    pass


def auth_dir(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("WBVAULT_AUTH_DIR")
    if env:
        return Path(env).expanduser()
    return DEFAULT_AUTH_DIR


def vault_root(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    env = os.environ.get("WBVAULT_HOME")
    if env:
        return Path(env).expanduser()
    return DEFAULT_VAULT_ROOT


def candidate_auth_files(directory: Path) -> List[Path]:
    if not directory.is_dir():
        return []
    files = [
        p
        for p in directory.iterdir()
        if p.is_file()
        and p.name.endswith(AUTH_FILE_SUFFIX)
        and OBSERVATION_MARKER not in p.name
    ]
    # 优先已知 id，其次按修改时间倒序
    def sort_key(p: Path):
        try:
            idx = PREFERRED_AUTH_IDS.index(p.stem)
        except ValueError:
            idx = len(PREFERRED_AUTH_IDS)
        try:
            mtime = p.stat().st_mtime
        except OSError:
            mtime = 0.0
        return (idx, -mtime)

    return sorted(files, key=sort_key)


def resolve_auth_file(explicit: Optional[str] = None, directory: Optional[Path] = None) -> Path:
    """定位当前登录态文件。

    规则：显式参数 > 环境变量 WBVAULT_AUTH_FILE > 已知 id > 目录内最新。
    """
    if explicit:
        p = Path(explicit).expanduser()
        if not p.is_file():
            raise PathError("指定的登录态文件不存在：%s" % p)
        return p

    env = os.environ.get("WBVAULT_AUTH_FILE")
    if env:
        p = Path(env).expanduser()
        if not p.is_file():
            raise PathError("WBVAULT_AUTH_FILE 指向的文件不存在：%s" % p)
        return p

    directory = directory or auth_dir()
    files = candidate_auth_files(directory)
    if not files:
        raise PathError(
            "没找到 WorkBuddy 登录态文件，目录：%s\n"
            "请确认 WorkBuddy AI 已登录，或用 --auth-file 指定。" % directory
        )
    return files[0]


def observation_for(auth_file: Path) -> Optional[Path]:
    """返回与 auth 文件配对的 observation 文件（会话观测信号）。"""
    directory = auth_file.parent
    prefix = auth_file.name + "."
    try:
        matches = sorted(
            p
            for p in directory.iterdir()
            if p.is_file() and p.name.startswith(prefix) and p.name.endswith(OBSERVATION_MARKER)
        )
    except OSError:
        return None
    return matches[0] if matches else None


def ensure_private_dir(path: Path) -> Path:
    path.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path, 0o700)
    except OSError:
        pass
    return path
