"""会话共享：切账号时把「本机所有会话」都带过去，而不是只带当前那一条。

依据（App 自己的 SQL，反编译自 app.asar）：

    WHERE transport = 'local' AND deleted_at IS NULL
      AND (user_id = ? OR user_id = '')

也就是说，决定「谁看得见」的只有 `sessions.user_id` 一个字段；消息本体在
`projects/<目录名>/<sessionId>.jsonl`，里面**不带账号**。所以：

- 把整张表的本地会话归到目标账号 → 切过去「会话全在」，而不是换一批；
- 会话在哪个空间，由 `sessions.cwd` 决定；同一个本地目录天然是同一个空间，
  两个账号打开过同一个文件夹，会话就会并排出现，不需要额外做什么；
- 云端会话（transport != 'local'）跟着服务端走，一律不碰。

为什么在写登录态**之前**归位：App 收到登录态变更后会立刻重读侧边栏，
写晚了这一轮就看不到新会话（得再触发一次刷新）。写失败再归位回原账号。

原始归属记在 `<vault>/ownership.json`，`wbvault unshare` 可以还原。
"""

from __future__ import annotations

import json
import os
import re
import shutil
import sqlite3
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from .authfile import atomic_write
from .handover import backup_db, connect, db_path  # noqa: F401  (db_path 供 CLI 复用)
from .paths import ensure_private_dir
from .store import Vault

WORKBUDDY_HOME = Path.home() / ".workbuddy-ai"
PROJECTS_DIR = WORKBUDDY_HOME / "projects"
SIDEBAR_SNAPSHOT = "sidebar-list-snapshot.json"

# 保留多少条归属记录（够用即可，避免文件无限长）
OWNERSHIP_LIMIT = 5000


class ShareError(RuntimeError):
    pass


# ---------------- 归属记录 ----------------


def ownership_path(vault: Vault) -> Path:
    return vault.root / "ownership.json"


def load_ownership(vault: Vault) -> Dict[str, object]:
    path = ownership_path(vault)
    if not path.is_file():
        return {"version": 1, "sessions": {}, "updatedAt": 0}
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return {"version": 1, "sessions": {}, "updatedAt": 0}
    if not isinstance(data, dict) or not isinstance(data.get("sessions"), dict):
        return {"version": 1, "sessions": {}, "updatedAt": 0}
    return data


def save_ownership(vault: Vault, data: Dict[str, object]) -> None:
    sessions = data.get("sessions") or {}
    if isinstance(sessions, dict) and len(sessions) > OWNERSHIP_LIMIT:
        # 只留最近的一批，避免老会话把文件撑大
        ordered = sorted(
            sessions.items(),
            key=lambda kv: int((kv[1] or {}).get("at") or 0) if isinstance(kv[1], dict) else 0,
            reverse=True,
        )
        data["sessions"] = dict(ordered[:OWNERSHIP_LIMIT])
    data["version"] = 1
    data["updatedAt"] = int(time.time() * 1000)
    ensure_private_dir(vault.root)
    atomic_write(
        ownership_path(vault),
        json.dumps(data, indent=2, ensure_ascii=False).encode("utf-8"),
        mode=0o600,
    )


def owner_of(vault: Vault, session_id: str, fallback: str = "") -> str:
    entry = (load_ownership(vault).get("sessions") or {}).get(session_id)
    if isinstance(entry, dict) and entry.get("uid"):
        return str(entry["uid"])
    return fallback


# ---------------- 结果类型 ----------------


@dataclass
class ShareResult:
    status: str  # ok | noop | error
    message: str
    total: int = 0
    changed: int = 0
    owners: Dict[str, int] = field(default_factory=dict)
    backup: Optional[Path] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "message": self.message,
            "total": self.total,
            "changed": self.changed,
            "owners": self.owners,
            "backup": str(self.backup) if self.backup else None,
        }


@dataclass
class MergeResult:
    status: str  # ok | noop | error
    message: str
    plan: List[Dict[str, object]] = field(default_factory=list)
    moved_sessions: int = 0
    moved_files: int = 0
    backup: Optional[Path] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "message": self.message,
            "plan": self.plan,
            "movedSessions": self.moved_sessions,
            "movedFiles": self.moved_files,
            "backup": str(self.backup) if self.backup else None,
        }


# ---------------- 共享（全量归到目标账号） ----------------


def _local_rows(conn: sqlite3.Connection) -> List[sqlite3.Row]:
    return conn.execute(
        "select id, user_id, coalesce(title, '') as title, coalesce(cwd, '') as cwd "
        "from sessions where transport = 'local' and deleted_at is null"
    ).fetchall()


def claim_all(
    vault: Vault,
    db: Path,
    to_uid: str,
    *,
    dry_run: bool = False,
    keep_backup: bool = True,
) -> ShareResult:
    """把本机所有本地会话归到 to_uid 名下，并把原始归属记下来。"""
    if not to_uid:
        return ShareResult("error", "目标账号为空，已跳过")
    try:
        conn = connect(db)
    except Exception as exc:  # noqa: BLE001 - 统一转成结果对象
        return ShareResult("error", str(exc))

    try:
        rows = _local_rows(conn)
        total = len(rows)
        pending = [row for row in rows if row["user_id"] != to_uid]
        owners: Dict[str, int] = {}
        for row in rows:
            owners[row["user_id"]] = owners.get(row["user_id"], 0) + 1

        if not pending:
            return ShareResult(
                "noop",
                "本机 %d 条会话都已在 %s 名下，无需共享" % (total, to_uid[:8]),
                total=total,
                changed=0,
                owners=owners,
            )

        if dry_run:
            return ShareResult(
                "ok",
                "[dry-run] 将把 %d 条本地会话归到 %s 名下（本机共 %d 条）"
                % (len(pending), to_uid[:8], total),
                total=total,
                changed=len(pending),
                owners=owners,
            )

        backup = backup_db(db, vault) if keep_backup else None

        # 原始归属：先见先得，之后无论怎么切都按第一任主人还原
        data = load_ownership(vault)
        store = data.get("sessions")
        if not isinstance(store, dict):
            store = {}
            data["sessions"] = store
        now = int(time.time() * 1000)
        for row in rows:
            sid = str(row["id"])
            if sid in store or not row["user_id"]:
                continue
            store[sid] = {
                "uid": str(row["user_id"]),
                "title": str(row["title"] or "")[:120],
                "cwd": str(row["cwd"] or ""),
                "at": now,
            }
        save_ownership(vault, data)

        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute(
                "update sessions set user_id = ? "
                "where transport = 'local' and deleted_at is null and user_id != ?",
                (to_uid, to_uid),
            )
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            return ShareResult("error", "写库失败，已回滚：%s" % exc, total=total, backup=backup)

        invalidate_sidebars(set(owners) | {to_uid})
        return ShareResult(
            "ok",
            "已把 %d 条会话归到 %s 名下（本机共 %d 条），切过去全都能看到"
            % (len(pending), to_uid[:8], total),
            total=total,
            changed=len(pending),
            owners=owners,
            backup=backup,
        )
    finally:
        conn.close()


def capture_owners(db: Path) -> Dict[str, str]:
    """记下此刻每条本地会话归谁。

    切换失败要回滚时按这份快照精确还原。用「整表归到上一个账号」兜底是不行的：
    本机有第三个账号时，它的会话会被一起搬走。
    """
    conn = connect(db)
    try:
        return {str(row["id"]): str(row["user_id"]) for row in _local_rows(conn)}
    finally:
        conn.close()


def restore_exact(vault: Vault, db: Path, owners: Dict[str, str]) -> int:
    """把会话归属还原成 capture_owners 记下的样子，只改真的变了的那些。"""
    if not owners:
        return 0
    conn = connect(db)
    try:
        current = {str(row["id"]): str(row["user_id"]) for row in _local_rows(conn)}
        moves = [
            (sid, uid)
            for sid, uid in owners.items()
            # 期间被删掉的会话（current 里没有）不去复活它
            if sid in current and current[sid] != uid
        ]
        if not moves:
            return 0
        try:
            conn.execute("BEGIN IMMEDIATE")
            for sid, uid in moves:
                conn.execute("update sessions set user_id = ? where id = ?", (uid, sid))
            conn.commit()
        except sqlite3.Error:
            conn.rollback()
            raise
    finally:
        conn.close()
    invalidate_sidebars({uid for _sid, uid in moves} | set(owners.values()))
    return len(moves)


def restore_ownership(vault: Vault, db: Path, *, dry_run: bool = False) -> ShareResult:
    """按 ownership.json 把会话还回各自的第一任账号。"""
    data = load_ownership(vault)
    store = data.get("sessions") or {}
    if not isinstance(store, dict) or not store:
        return ShareResult("noop", "没有归属记录（还没共享过，或记录被删了）")

    try:
        conn = connect(db)
    except Exception as exc:  # noqa: BLE001
        return ShareResult("error", str(exc))

    try:
        existing = {
            str(row["id"]): row
            for row in conn.execute(
                "select id, user_id from sessions where transport = 'local' and deleted_at is null"
            ).fetchall()
        }
        moves: List[Tuple[str, str]] = []
        for sid, entry in store.items():
            if not isinstance(entry, dict):
                continue
            uid = str(entry.get("uid") or "")
            row = existing.get(str(sid))
            if not uid or row is None or row["user_id"] == uid:
                continue
            moves.append((str(sid), uid))

        if not moves:
            return ShareResult("noop", "会话归属已经是原始状态", total=len(existing))

        if dry_run:
            return ShareResult(
                "ok",
                "[dry-run] 将把 %d 条会话还回原账号" % len(moves),
                total=len(existing),
                changed=len(moves),
            )

        backup = backup_db(db, vault)
        try:
            conn.execute("BEGIN IMMEDIATE")
            for sid, uid in moves:
                conn.execute("update sessions set user_id = ? where id = ?", (uid, sid))
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            return ShareResult("error", "写库失败，已回滚：%s" % exc, backup=backup)

        invalidate_sidebars({uid for _sid, uid in moves})
        return ShareResult(
            "ok",
            "已把 %d 条会话还回各自账号" % len(moves),
            total=len(existing),
            changed=len(moves),
            backup=backup,
        )
    finally:
        conn.close()


def status(vault: Vault, db: Path) -> Dict[str, object]:
    """共享状态：有多少条、分别是谁建的、当前账号能看多少。"""
    try:
        conn = connect(db)
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": str(exc)}
    try:
        rows = _local_rows(conn)
        by_user: Dict[str, int] = {}
        for row in rows:
            by_user[row["user_id"]] = by_user.get(row["user_id"], 0) + 1
        owners: Dict[str, int] = {}
        for row in rows:
            uid = owner_of(vault, str(row["id"]), str(row["user_id"]))
            owners[uid] = owners.get(uid, 0) + 1
        return {
            "ok": True,
            "total": len(rows),
            "byUser": by_user,
            "byOwner": owners,
            "ownershipRecorded": len(load_ownership(vault).get("sessions") or {}),
        }
    finally:
        conn.close()


# ---------------- 空间 ----------------


def spaces(db: Path, *, include_playground: bool = True) -> List[Dict[str, object]]:
    """按 cwd 聚合：一个 cwd 就是一个空间（App 就是这么分的）。"""
    conn = connect(db)
    try:
        sql = (
            "select coalesce(cwd, '') as cwd, count(*) as n, "
            "       max(coalesce(last_activity_at, created_at)) as ts, "
            "       max(coalesce(is_playground, 0)) as playground "
            "from sessions where transport = 'local' and deleted_at is null "
        )
        if not include_playground:
            sql += " and coalesce(is_playground, 0) = 0 "
        sql += " group by cwd order by ts desc"
        items = []
        for row in conn.execute(sql).fetchall():
            cwd = str(row["cwd"] or "")
            items.append(
                {
                    "cwd": cwd,
                    "name": Path(cwd).name or cwd or "(未知目录)",
                    "sessions": int(row["n"] or 0),
                    "lastActivityAt": int(row["ts"] or 0),
                    "playground": bool(row["playground"]),
                }
            )
        return items
    finally:
        conn.close()


def same_name_groups(items: Sequence[Dict[str, object]]) -> Dict[str, List[Dict[str, object]]]:
    """同名（目录名相同、路径不同）的空间分组；只有 >1 个才算需要合并。"""
    groups: Dict[str, List[Dict[str, object]]] = {}
    for item in items:
        name = str(item.get("name") or "")
        if not name:
            continue
        groups.setdefault(name, []).append(item)
    return {name: rows for name, rows in groups.items() if len(rows) > 1}


def compress_workspace_name(path: str) -> str:
    """复刻 App 的目录名算法（node.js 里的 compressWorkspacePathName）。"""
    base = re.sub(r"[/\\:]", "-", path)
    base = re.sub(r"^-+", "", base)
    base = re.sub(r"-+$", "", base)
    base = re.sub(r"-+", "-", base)
    if len(base.encode("utf-8")) <= 255:
        return base
    truncated = ""
    used = 0
    for char in base:
        size = len(char.encode("utf-8"))
        if used + size > 180:
            break
        truncated += char
        used += size
    return "%s-%s" % (truncated, _djb2_base36(base))


def _djb2_base36(text: str) -> str:
    value = 5381
    for byte in text.encode("utf-8"):
        value = ((value * 33) & 0xFFFFFFFF) ^ byte
    digits = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    out = ""
    while value:
        value, rem = divmod(value, 36)
        out = digits[rem] + out
    return out


def _session_artifacts(projects: Path, cwd: str) -> List[Path]:
    directory = projects / compress_workspace_name(cwd)
    if not directory.is_dir():
        return []
    return list(directory.iterdir())


def merge_same_name(
    vault: Vault,
    db: Path,
    *,
    dry_run: bool = True,
    only: Optional[Iterable[str]] = None,
) -> MergeResult:
    """把「目录名相同、路径不同」的空间并到一个路径上（会话 + 记录一起搬）。

    这会改变会话绑定的文件夹：合并后这些会话都在保留的那个目录里干活。
    所以默认只演练，真要动手得显式 --yes。
    """
    items = spaces(db)
    groups = same_name_groups(items)
    if only:
        wanted = {str(x) for x in only}
        groups = {name: rows for name, rows in groups.items() if name in wanted}
    if not groups:
        return MergeResult("noop", "没有同名不同路径的空间，不需要合并")

    plan: List[Dict[str, object]] = []
    for name, rows in sorted(groups.items()):
        canonical = max(rows, key=lambda item: (int(item["sessions"]), int(item["lastActivityAt"])))
        for item in rows:
            if item["cwd"] == canonical["cwd"]:
                continue
            plan.append(
                {
                    "name": name,
                    "from": item["cwd"],
                    "to": canonical["cwd"],
                    "sessions": item["sessions"],
                }
            )
    if not plan:
        return MergeResult("noop", "没有需要合并的空间")

    if dry_run:
        return MergeResult(
            "ok",
            "[dry-run] 将合并 %d 个空间（会话会挂到保留目录上）" % len(plan),
            plan=plan,
        )

    try:
        conn = connect(db)
    except Exception as exc:  # noqa: BLE001
        return MergeResult("error", str(exc))

    backup = None
    moved_sessions = 0
    moved_files = 0
    try:
        backup = backup_db(db, vault)
        projects = PROJECTS_DIR
        undo: List[Dict[str, object]] = []
        conn.execute("BEGIN IMMEDIATE")
        for entry in plan:
            old, new = str(entry["from"]), str(entry["to"])
            ids = [
                str(row["id"])
                for row in conn.execute(
                    "select id from sessions where cwd = ? and transport = 'local'", (old,)
                ).fetchall()
            ]
            conn.execute(
                "update sessions set cwd = ? where cwd = ? and transport = 'local'", (new, old)
            )
            moved_sessions += len(ids)
            moved_files += _move_artifacts(projects, old, new, ids)
            undo.append({"from": old, "to": new, "sessions": ids})
        conn.execute(
            "insert into workspaces (path, last_opened_at) values (?, ?) "
            "on conflict(path) do update set last_opened_at = excluded.last_opened_at",
            (str(plan[0]["to"]), int(time.time() * 1000)),
        )
        conn.commit()
    except (sqlite3.Error, OSError) as exc:
        conn.rollback()
        return MergeResult("error", "合并失败，已回滚：%s" % exc, plan=plan, backup=backup)
    finally:
        conn.close()

    _append_merge_record(vault, undo)
    invalidate_sidebars(set())
    return MergeResult(
        "ok",
        "已合并 %d 个空间（%d 条会话、%d 个文件）" % (len(plan), moved_sessions, moved_files),
        plan=plan,
        moved_sessions=moved_sessions,
        moved_files=moved_files,
        backup=backup,
    )


def _move_artifacts(projects: Path, old: str, new: str, ids: Sequence[str]) -> int:
    """把 <projects>/<旧目录>/ 里属于这些会话的文件搬到 <projects>/<新目录>/。"""
    src = projects / compress_workspace_name(old)
    dst = projects / compress_workspace_name(new)
    if not src.is_dir() or src == dst:
        return 0
    dst.mkdir(parents=True, exist_ok=True)
    moved = 0
    for path in _session_artifacts(projects, old):
        if not any(path.name.startswith(sid) for sid in ids):
            continue
        target = dst / path.name
        if target.exists():
            continue
        try:
            shutil.move(str(path), str(target))
            moved += 1
        except OSError:
            continue
    return moved


def _append_merge_record(vault: Vault, undo: List[Dict[str, object]]) -> None:
    path = vault.root / "space-merges.json"
    try:
        items = json.loads(path.read_text("utf-8")) if path.is_file() else []
    except (OSError, json.JSONDecodeError):
        items = []
    if not isinstance(items, list):
        items = []
    items.append({"at": int(time.time() * 1000), "moves": undo, "undone": False})
    ensure_private_dir(vault.root)
    atomic_write(
        path,
        json.dumps(items[-50:], indent=2, ensure_ascii=False).encode("utf-8"),
        mode=0o600,
    )


def undo_merge(vault: Vault, db: Path) -> MergeResult:
    path = vault.root / "space-merges.json"
    if not path.is_file():
        return MergeResult("noop", "没有可撤销的空间合并")
    try:
        items = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return MergeResult("noop", "没有可撤销的空间合并")
    if not isinstance(items, list):
        return MergeResult("noop", "没有可撤销的空间合并")
    target = None
    for item in reversed(items):
        if isinstance(item, dict) and not item.get("undone"):
            target = item
            break
    if target is None:
        return MergeResult("noop", "没有可撤销的空间合并")

    conn = connect(db)
    backup = backup_db(db, vault)
    try:
        conn.execute("BEGIN IMMEDIATE")
        for move in target.get("moves") or []:
            old, new = str(move.get("from")), str(move.get("to"))
            conn.execute(
                "update sessions set cwd = ? where cwd = ? and transport = 'local'", (old, new)
            )
            _move_artifacts(PROJECTS_DIR, new, old, [str(x) for x in move.get("sessions") or []])
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        return MergeResult("error", "撤销失败：%s" % exc, backup=backup)
    finally:
        conn.close()

    target["undone"] = True
    atomic_write(
        path, json.dumps(items, indent=2, ensure_ascii=False).encode("utf-8"), mode=0o600
    )
    invalidate_sidebars(set())
    return MergeResult("ok", "已撤销最近一次空间合并", backup=backup)


# ---------------- 其它小工具 ----------------


def invalidate_sidebars(uids: Iterable[str]) -> None:
    """删掉侧边栏快照缓存，让 App 重新从库里取。"""
    candidates = {uid for uid in uids if uid}
    try:
        for path in WORKBUDDY_HOME.iterdir():
            if path.is_dir() and re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", path.name):
                candidates.add(path.name)
    except OSError:
        pass
    for uid in candidates:
        try:
            (WORKBUDDY_HOME / uid / SIDEBAR_SNAPSHOT).unlink()
        except OSError:
            pass


def carry_route(from_uid: str, to_uid: str) -> bool:
    """把「当前打开的是哪个会话」抄给目标账号，切完还停在同一页。"""
    if not from_uid or not to_uid or from_uid == to_uid:
        return False
    source = WORKBUDDY_HOME / from_uid / "startup-route.json"
    if not source.is_file():
        return False
    try:
        data = json.loads(source.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    path = str(data.get("path") or "")
    if "/conversation/" not in path:
        return False
    target_dir = WORKBUDDY_HOME / to_uid
    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        payload = json.dumps(
            {"path": path, "savedAt": int(time.time() * 1000)}, ensure_ascii=False
        ).encode("utf-8")
        atomic_write(target_dir / "startup-route.json", payload, mode=0o600)
    except OSError:
        return False
    return True
