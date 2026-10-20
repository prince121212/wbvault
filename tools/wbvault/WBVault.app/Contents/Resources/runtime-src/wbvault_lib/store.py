"""本地账号库：索引 + 快照 + 备份。

目录结构：
<vault>/
  accounts.json
  snapshots/<uid>.info
  snapshots/<uid>.meta.json
  backups/<timestamp>-<uid8>.info

并发约定（重要）：
- 索引是**派生数据**，快照文件 + 同目录 meta.json 才是事实；
- 常驻的 watch 进程会活很久，如果把 accounts.json 缓存在内存里一直不重读，
  它每次 save() 都会把别人写进去的更新整份覆盖掉（last-writer-wins）。
  所以 load() 会检查文件指纹，变了就重新读；
- 所有「读-改-写」都在 .index.lock 的 flock 里做，避免两个进程交错。
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .authfile import (
    AuthState,
    atomic_write,
    list_backups,
    rotate_backups,
    sha256_bytes,
)
from .paths import ensure_private_dir
from .profile import nickname_for

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
        self._index_stamp: Optional[tuple] = None

    # ---------- 基础 ----------

    def ensure(self) -> None:
        ensure_private_dir(self.root)
        ensure_private_dir(self.snapshots_dir)
        ensure_private_dir(self.backups_dir)

    def _stamp(self) -> Optional[tuple]:
        """索引文件的指纹；用来判断磁盘上的内容是不是被别人改过。"""
        try:
            st = self.index_path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def load(self, force: bool = False) -> Dict[str, Any]:
        """读索引。

        默认会先看文件指纹：磁盘变了就重新读，绝不拿旧缓存覆盖别人的更新。
        force=True 强制重读（事务里用）。
        """
        stamp = self._stamp()
        if self._index is not None and not force and stamp == self._index_stamp:
            return self._index
        if stamp is None:
            # 文件还不存在：保留内存里已经改过的内容，别把调用方的改动冲掉
            if self._index is None:
                self._index = {"version": INDEX_VERSION, "activeUid": None, "updatedAt": 0, "accounts": []}
            self._index_stamp = None
            return self._index
        try:
            data = json.loads(self.index_path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise VaultError("账号库索引损坏：%s（%s）" % (self.index_path, exc))
        if not isinstance(data, dict) or not isinstance(data.get("accounts"), list):
            raise VaultError("账号库索引结构异常：%s" % self.index_path)
        self._index = data
        self._index_stamp = stamp
        return data

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """跨进程互斥：常驻 watch 与菜单栏 / 命令行会同时写索引。"""
        self.ensure()
        fd = os.open(str(self.root / ".index.lock"), os.O_CREAT | os.O_RDWR, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @contextlib.contextmanager
    def transaction(self) -> Iterator[Dict[str, Any]]:
        """锁内「重读 → 改 → 写」，杜绝读-改-写竞态。"""
        with self.locked():
            data = self.load(force=True)
            yield data
            self.save()

    def save(self) -> None:
        """写索引。调用方应已持有 locked()/transaction()，且内存是最新的一份。"""
        self.ensure()
        data = self.load()
        data["version"] = INDEX_VERSION
        data["updatedAt"] = int(time.time() * 1000)
        payload = json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8")
        atomic_write(self.index_path, payload, mode=0o600)
        self._index_stamp = self._stamp()

    # ---------- 查询 ----------

    def entries(self) -> List[Dict[str, Any]]:
        return list(self.load().get("accounts") or [])

    def active_uid(self) -> Optional[str]:
        return self.load().get("activeUid")

    def snapshot_path(self, uid: str) -> Path:
        return self.snapshots_dir / ("%s.info" % uid)

    def find(self, selector: str) -> Optional[Dict[str, Any]]:
        """支持：完整 uid / uid 前缀（>=4 位）/ 别名 / 邮箱 / uin（都忽略大小写）。"""
        return self._match(self.load(), selector)

    def _find_in(self, data: Dict[str, Any], selector: str) -> Dict[str, Any]:
        """事务内查找：找不到直接抛错，返回的是索引里那一份（改它就会落盘）。"""
        entry = self._match(data, selector)
        if not entry:
            raise VaultError("找不到账号：%s" % selector)
        return entry

    def _match(self, data: Dict[str, Any], selector: str) -> Optional[Dict[str, Any]]:
        needle = (selector or "").strip()
        if not needle:
            return None
        entries = list(data.get("accounts") or [])
        lowered = needle.lower()
        for entry in entries:
            if str(entry.get("alias", "")).lower() == lowered:
                return entry
        for entry in entries:
            if str(entry.get("email") or "").lower() == lowered:
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

    def meta_path(self, uid: str) -> Path:
        return self.snapshots_dir / ("%s.meta.json" % uid)

    def read_meta(self, uid: str) -> Dict[str, Any]:
        try:
            data = json.loads(self.meta_path(uid).read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def verify_snapshot(self, entry: Dict[str, Any], repair: bool = True) -> bool:
        """快照是否可信。

        判定顺序：文件 → 同目录 meta → 索引里的 sha。
        索引是派生数据，常驻进程可能把旧索引写回来；这时文件与 meta 一致就说明
        快照本身没坏，只是索引过期了 —— 修好索引继续用，不误报「快照损坏」。
        """
        uid = str(entry.get("uid", ""))
        path = self.snapshot_path(uid)
        if not path.is_file():
            return False
        try:
            raw = path.read_bytes()
        except OSError:
            return False
        actual = sha256_bytes(raw)
        expected = str(entry.get("sha256") or "")
        if not expected or actual == expected:
            return True

        meta = self.read_meta(uid)
        if str(meta.get("sha256") or "") != actual:
            # 文件既不是索引说的那份、也不是 meta 说的那份：真的坏了，拒绝
            return False
        if repair:
            self.repair_entry(uid, actual, len(raw), meta)
        return True

    def repair_entry(self, uid: str, sha: str, size: int, meta: Optional[Dict[str, Any]] = None) -> bool:
        """把索引里过期的 sha / size 改回文件真实值。"""
        meta = meta or {}
        try:
            with self.transaction() as data:
                for item in data["accounts"]:
                    if item.get("uid") != uid:
                        continue
                    item["sha256"] = sha
                    item["size"] = size
                    if meta.get("snapshotAt"):
                        item["snapshotAt"] = meta["snapshotAt"]
                    return True
        except (OSError, VaultError):
            return False
        return False

    def repair(self) -> List[Dict[str, Any]]:
        """体检并修复索引：返回每个账号的处理结果（只动 accounts.json，不碰登录态）。"""
        report: List[Dict[str, Any]] = []
        for entry in self.entries():
            uid = str(entry.get("uid", ""))
            path = self.snapshot_path(uid)
            item = {"uid": uid, "alias": entry.get("alias") or "", "status": "ok", "detail": ""}
            if not path.is_file():
                item["status"] = "missing"
                item["detail"] = "快照文件不存在"
                report.append(item)
                continue
            raw = path.read_bytes()
            actual = sha256_bytes(raw)
            if actual == entry.get("sha256"):
                report.append(item)
                continue
            meta = self.read_meta(uid)
            if str(meta.get("sha256") or "") == actual:
                fixed = self.repair_entry(uid, actual, len(raw), meta)
                item["status"] = "repaired" if fixed else "error"
                item["detail"] = "索引里的 sha256 过期，已按文件修正" if fixed else "索引写入失败"
            else:
                item["status"] = "corrupt"
                item["detail"] = "文件与索引、meta 都不一致（快照确实坏了）"
            report.append(item)
        return report

    # ---------- 写入 ----------

    def snapshot(self, state: AuthState, alias: Optional[str] = None, note: str = "") -> Dict[str, Any]:
        """把当前登录态整份字节存进快照库，并更新索引。

        文件、meta、索引三者在同一把锁里写：否则两个进程（常驻 watch + 切换）
        交错时会写出「文件是新内容、索引还是旧 sha」这种自相矛盾的库。
        """
        self.ensure()
        path = self.snapshot_path(state.uid)
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
        with self.transaction() as data:
            if not path.is_file() or sha256_bytes(path.read_bytes()) != state.sha256:
                atomic_write(path, state.raw, mode=0o600)
            atomic_write(
                self.meta_path(state.uid),
                json.dumps(meta, indent=2, ensure_ascii=False).encode("utf-8"),
                mode=0o600,
            )

            existing = next((e for e in data["accounts"] if e.get("uid") == state.uid), None)
            now = int(time.time() * 1000)
            if existing is None:
                existing = {"uid": state.uid, "alias": alias or "", "lastSeenActiveAt": now}
                data["accounts"].append(existing)
            for key, value in meta.items():
                existing[key] = value
            if alias is not None:
                existing["alias"] = alias
            email = nickname_for(state.uid)
            if email:
                existing["email"] = email
            existing["lastSeenActiveAt"] = now
            data["activeUid"] = state.uid
        return existing

    def set_active(self, uid: Optional[str]) -> None:
        """只改 activeUid（登出时用），锁内重读，别整份覆盖。"""
        with self.transaction() as data:
            data["activeUid"] = uid

    def refresh_email(self, uid: str) -> Optional[str]:
        """登录态没变、但本机账号档案里补上了邮箱时，把邮箱写进账号库。"""
        email = nickname_for(uid)
        if not email:
            return None
        with self.transaction() as data:
            entry = next((e for e in data["accounts"] if e.get("uid") == uid), None)
            if not entry or entry.get("email") == email:
                return None
            entry["email"] = email
            return email

    def set_email(self, selector: str, email: str) -> Dict[str, Any]:
        value = email.strip()
        with self.transaction() as data:
            entry = self._find_in(data, selector)
            if value:
                entry["email"] = value
            else:
                entry.pop("email", None)
            return entry

    def set_alias(self, selector: str, alias: str) -> Dict[str, Any]:
        with self.transaction() as data:
            entry = self._find_in(data, selector)
            entry["alias"] = alias.strip()
            return entry

    def remove(self, selector: str) -> Dict[str, Any]:
        with self.transaction() as data:
            entry = self._find_in(data, selector)
            uid = str(entry["uid"])
            data["accounts"] = [e for e in data["accounts"] if e.get("uid") != uid]
            if data.get("activeUid") == uid:
                data["activeUid"] = None
        for path in (self.snapshot_path(uid), self.meta_path(uid)):
            try:
                path.unlink()
            except FileNotFoundError:
                pass
        return entry

    def prune_backups(self, keep: int = 12) -> int:
        before = len(list_backups(self.backups_dir))
        rotate_backups(self.backups_dir, keep)
        return before - len(list_backups(self.backups_dir))

    def backups(self) -> List[Path]:
        return list_backups(self.backups_dir)

    def touch_note(self, uid: str, note: str) -> None:
        with self.transaction() as data:
            for entry in data["accounts"]:
                if entry.get("uid") == uid:
                    entry["note"] = note
                    return

    # ---------- 设置与运行时状态 ----------

    def settings(self) -> Dict[str, Any]:
        """用户偏好。

        - autoRestart：热重载失败时自动重启 App，不用手动点（默认开）；
        - share：切账号时把本机所有会话都带过去，侧边栏不换一批（默认开）；
        - keepRoute：切完还停在同一页会话（默认开）。
        """
        defaults = {"autoRestart": True, "share": True, "keepRoute": True}
        path = self.root / "settings.json"
        if not path.is_file():
            return dict(defaults)
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return dict(defaults)
        if not isinstance(data, dict):
            return dict(defaults)
        for key, value in defaults.items():
            data.setdefault(key, value)
        return data

    def set_setting(self, key: str, value: Any) -> Dict[str, Any]:
        data = self.settings()
        data[key] = value
        self.ensure()
        atomic_write(
            self.root / "settings.json",
            json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
            mode=0o600,
        )
        return data

    def auto_restart(self) -> bool:
        return bool(self.settings().get("autoRestart", True))

    def share_enabled(self) -> bool:
        return bool(self.settings().get("share", True))

    def keep_route(self) -> bool:
        return bool(self.settings().get("keepRoute", True))

    def reload_state(self) -> Dict[str, Any]:
        """上一次切换时 App 的热重载结果（按 App 进程 pid 记）。"""
        path = self.root / "reload-state.json"
        if not path.is_file():
            return {}
        try:
            data = json.loads(path.read_text("utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}
        return data if isinstance(data, dict) else {}

    def record_reload(self, app_pid: Optional[int], healthy: bool, uid: str = "") -> None:
        payload = {
            "appPid": app_pid,
            "healthy": healthy,
            "uid": uid,
            "at": int(time.time() * 1000),
        }
        try:
            self.ensure()
            atomic_write(
                self.root / "reload-state.json",
                json.dumps(payload, indent=2, ensure_ascii=False).encode("utf-8"),
                mode=0o600,
            )
        except OSError:
            pass
