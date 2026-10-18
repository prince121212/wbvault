"""本地账号库：索引 + 快照 + 备份。

目录结构：
<vault>/
  accounts.json
  snapshots/<uid>.info
  snapshots/<uid>.meta.json
  backups/<timestamp>-<uid8>.info
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .authfile import (
    AuthState,
    atomic_write,
    list_backups,
    rotate_backups,
    sha256_bytes,
)
from .paths import ensure_private_dir

INDEX_VERSION = 1


class VaultError(RuntimeError):
    pass


class Vault:
    def __init__(self, root: Path):
        self.root = Path(root)
        self.index_path = self.root / "accounts.json"
        self.snapshots_dir = self.root / "snapshots"
        self.backups_dir = self.root / "backups"
        self._index: Optional[Dict[str, Any]] = None

    # ---------- 基础 ----------

    def ensure(self) -> None:
        ensure_private_dir(self.root)
        ensure_private_dir(self.snapshots_dir)
        ensure_private_dir(self.backups_dir)

    def load(self) -> Dict[str, Any]:
        if self._index is not None:
            return self._index
        if not self.index_path.is_file():
            self._index = {"version": INDEX_VERSION, "activeUid": None, "updatedAt": 0, "accounts": []}
            return self._index
        try:
            data = json.loads(self.index_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VaultError("账号库索引损坏：%s（%s）" % (self.index_path, exc))
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            raise VaultError("账号库索引结构异常：%s" % self.index_path)
        self._index = data
        return data

    def save(self) -> None:
        self.ensure()
        data = self.load()
        data["version"] = INDEX_VERSION
        data["updatedAt"] = int(time.time() * 1000)
        payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        atomic_write(self.index_path, payload, mode=0o600)

    # ---------- 查询 ----------

    def entries(self) -> List[Dict[str, Any]]:
        return list(self.load().get("accounts") or [])

    def active_uid(self) -> Optional[str]:
        return self.load().get("activeUid")

    def snapshot_path(self, uid: str) -> Path:
        return self.snapshots_dir / ("%s.info" % uid)

    def find(self, selector: str) -> Optional[Dict[str, Any]]:
        """支持：完整 uid / uid 前缀（>=4 位）/ 别名（忽略大小写）/ uin。"""
        needle = (selector or "").strip()
        if not needle:
            return None
        entries = self.entries()
        lowered = needle.lower()
        for entry in entries:
            if entry.get("alias", "").lower() == lowered:
                return entry
        for entry in entries:
            if entry.get("uid") == needle:
                return entry
        for entry in entries:
            if entry.get("uin") and entry["uin"] == needle:
                return entry
        if len(needle) >= 4:
            matches = [e for e in entries if str(e.get("uid", "")).startswith(lowered)]
            if len(matches) == 1:
                return matches[0]
            if len(matches) > 1:
                raise VaultError("前缀 %s 匹配到多个账号，请写得更长一些" % needle)
        return None

    def verify_snapshot(self, entry: Dict[str, Any]) -> bool:
        path = self.snapshot_path(str(entry.get("uid", "")))
        if not path.is_file():
            return False
        expected = entry.get("sha256")
        if not expected:
            return True
        return sha256_bytes(path.read_bytes()) == expected

    # ---------- 写入 ----------

    def snapshot(self, state: AuthState, alias: Optional[str] = None, note: str = "") -> Dict[str, Any]:
        """把当前登录态整份字节存进快照库，并更新索引。"""
        self.ensure()
        path = self.snapshot_path(state.uid)
        if not path.is_file() or sha256_bytes(path.read_bytes()) != state.sha256:
            atomic_write(path, state.raw, mode=0o600)

        meta = {
            "uid": state.uid,
            "uin": state.uin,
            "domain": state.domain,
            "authId": state.path.stem,
            "accountType": state.account_type,
            "snapshotAt": int(time.time() * 1000),
            "tokenExpiresAt": state.expires_at,
            "refreshExpiresAt": state.refresh_expires_at,
            "sha256": state.sha256,
            "size": state.size,
            "note": note,
        }
        atomic_write(
            self.snapshots_dir / ("%s.meta.json" % state.uid),
            json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"),
            mode=0o600,
        )

        data = self.load()
        existing = next((e for e in data["accounts"] if e.get("uid") == state.uid), None)
        now = int(time.time() * 1000)
        if existing is None:
            existing = {"uid": state.uid, "alias": alias or "", "lastSeenActiveAt": now}
            data["accounts"].append(existing)
        for key, value in meta.items():
            existing[key] = value
        if alias is not None:
            existing["alias"] = alias
        existing["lastSeenActiveAt"] = now
        data["activeUid"] = state.uid
        self.save()
        return existing

    def set_alias(self, selector: str, alias: str) -> Dict[str, Any]:
        entry = self.find(selector)
        if not entry:
            raise VaultError("找不到账号：%s" % selector)
        entry["alias"] = alias.strip()
        self.save()
        return entry

    def remove(self, selector: str) -> Dict[str, Any]:
        entry = self.find(selector)
        if not entry:
            raise VaultError("找不到账号：%s" % selector)
        uid = str(entry["uid"])
        for path in (self.snapshot_path(uid), self.snapshots_dir / ("%s.meta.json" % uid)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        data = self.load()
        data["accounts"] = [e for e in data["accounts"] if e.get("uid") != uid]
        if data.get("activeUid") == uid:
            data["activeUid"] = None
        self.save()
        return entry

    def prune_backups(self, keep: int = 12) -> int:
        before = len(list_backups(self.backups_dir))
        rotate_backups(self.backups_dir, keep)
        return before - len(list_backups(self.backups_dir))

    def backups(self) -> List[Path]:
        return list_backups(self.backups_dir)

    def touch_note(self, uid: str, note: str) -> None:
        for entry in self.entries():
            if entry.get("uid") == uid:
                entry["note"] = note
                self.save()
                return
