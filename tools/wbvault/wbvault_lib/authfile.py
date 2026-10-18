"""登录态文件的读取、原子写入与备份。

设计原则：
1. 只读不解析密文。accessToken / refreshToken / nickname 都是
   {"$wbEncrypted": 1, "envelope": "..."} 的字段级密文，密钥在
   ~/.workbuddy-ai/keyblob，机器绑定。本工具只做字节级搬运。
2. 写入必须原子：同目录临时文件 + fsync + os.replace，避免 App 读到半截 JSON。
3. 任何写操作前都要有备份，失败要能还原。
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional


class AuthFileError(RuntimeError):
    pass


@dataclass
class AuthState:
    path: Path
    uid: str
    uin: str = ""
    account_type: str = ""
    domain: str = ""
    expires_at: int = 0
    refresh_expires_at: int = 0
    last_refresh_time: int = 0
    is_enterprise: bool = False
    raw: bytes = b""
    sha256: str = ""
    mtime: float = 0.0
    size: int = 0
    accounts_seen: List[str] = field(default_factory=list)

    @property
    def uid_short(self) -> str:
        return self.uid[:8]

    def token_expired(self, now_ms: Optional[int] = None) -> bool:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return bool(self.expires_at) and self.expires_at <= now_ms

    def refresh_expired(self, now_ms: Optional[int] = None) -> bool:
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return bool(self.refresh_expires_at) and self.refresh_expires_at <= now_ms

    def days_left(self, now_ms: Optional[int] = None) -> Optional[int]:
        if not self.refresh_expires_at:
            return None
        now_ms = now_ms if now_ms is not None else int(time.time() * 1000)
        return max(0, (self.refresh_expires_at - now_ms) // 86_400_000)

    def summary(self) -> Dict[str, Any]:
        return {
            "uid": self.uid,
            "uin": self.uin,
            "accountType": self.account_type,
            "domain": self.domain,
            "expiresAt": self.expires_at,
            "refreshExpiresAt": self.refresh_expires_at,
            "lastRefreshTime": self.last_refresh_time,
            "sha256": self.sha256,
            "size": self.size,
        }


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def read_raw(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError as exc:
        raise AuthFileError("读取登录态文件失败：%s（%s）" % (path, exc))


def parse_state(path: Path, raw: Optional[bytes] = None) -> AuthState:
    raw = read_raw(path) if raw is None else raw
    if not raw.strip():
        raise AuthFileError("登录态文件为空：%s" % path)
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise AuthFileError("登录态文件不是合法 JSON：%s（%s）" % (path, exc))

    account = data.get("account") or {}
    auth = data.get("auth") or {}
    if not isinstance(account, dict) or not isinstance(auth, dict):
        raise AuthFileError("登录态文件结构异常：%s" % path)

    uid = str(account.get("uid") or "").strip()
    if not uid:
        raise AuthFileError("登录态文件里没有 account.uid，可能未登录：%s" % path)

    seen: List[str] = []
    for key in ("accounts", "allAccounts"):
        for item in data.get(key) or []:
            if isinstance(item, dict) and item.get("uid"):
                seen.append(str(item["uid"]))

    stat = path.stat()
    return AuthState(
        path=path,
        uid=uid,
        uin=str(account.get("uin") or ""),
        account_type=str(account.get("type") or ""),
        domain=str(auth.get("domain") or ""),
        expires_at=int(auth.get("expiresAt") or 0),
        refresh_expires_at=int(auth.get("refreshExpiresAt") or 0),
        last_refresh_time=int(auth.get("lastRefreshTime") or 0),
        is_enterprise=bool(account.get("isCurrentOneIdEnterprise")),
        raw=raw,
        sha256=sha256_bytes(raw),
        mtime=stat.st_mtime,
        size=len(raw),
        accounts_seen=seen,
    )


def read_state(path: Path) -> AuthState:
    return parse_state(path)


def atomic_write(path: Path, data: bytes, mode: int = 0o600) -> None:
    """同目录临时文件 + fsync + os.replace，保证不出现半截文件。"""
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(prefix=".wbvault.", suffix=".tmp", dir=str(directory))
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
        try:
            dir_fd = os.open(str(directory), os.O_RDONLY)
            try:
                os.fsync(dir_fd)
            finally:
                os.close(dir_fd)
        except OSError:
            pass
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def copy_backup(src: Path, backups_dir: Path, keep: int = 12, uid_hint: str = "") -> Path:
    backups_dir.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(backups_dir, 0o700)
    except OSError:
        pass
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    suffix = uid_hint[:8] or "unknown"
    target = backups_dir / ("%s-%s%s" % (stamp, suffix, src.suffix or ".info"))
    shutil.copy2(src, target)
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    rotate_backups(backups_dir, keep)
    return target


def list_backups(backups_dir: Path) -> List[Path]:
    if not backups_dir.is_dir():
        return []
    files = [p for p in backups_dir.iterdir() if p.is_file() and p.suffix == ".info"]
    return sorted(files, key=lambda p: p.stat().st_mtime, reverse=True)


def rotate_backups(backups_dir: Path, keep: int = 12) -> None:
    for stale in list_backups(backups_dir)[max(keep, 1):]:
        try:
            stale.unlink()
        except OSError:
            pass


def latest_backup(backups_dir: Path) -> Optional[Path]:
    files = list_backups(backups_dir)
    return files[0] if files else None


def read_observation(path: Optional[Path]) -> Dict[str, Any]:
    """读取会话观测文件，失败返回空字典（它只是辅助信号）。"""
    if not path or not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return data if isinstance(data, dict) else {}
