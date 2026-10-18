"""会话过继：把某个会话从 A 账号挂到 B 账号名下。

背景：
- 会话消息存在 `~/.workbuddy-ai/projects/<目录>/<sessionId>.jsonl`，**不带账号**；
- 只有 `workbuddy.db` 的 `sessions.user_id` 决定它出现在谁的侧边栏；
- 所以改一个字段，会话就能跟着你换账号继续，历史一条不少。

风险与对策：
- 改库前用 sqlite backup API 做一致性备份；
- 记录每次过继，支持 undo；
- 只允许 local 会话（云端会话改了归属可能对不上）；
- 清掉两侧的侧边栏快照缓存，让 App 重新从库里取。
"""

from __future__ import annotations

import json
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from .paths import ensure_private_dir
from .store import Vault

DB_PATH = Path.home() / ".workbuddy-ai" / "workbuddy.db"


class HandoverError(RuntimeError):
    pass


@dataclass
class HandoverResult:
    status: str  # ok | refused | error
    message: str
    session_id: str = ""
    title: str = ""
    from_uid: str = ""
    to_uid: str = ""
    record_id: str = ""
    backup: Optional[Path] = None

    def to_dict(self) -> Dict[str, object]:
        return {
            "status": self.status,
            "message": self.message,
            "sessionId": self.session_id,
            "title": self.title,
            "fromUid": self.from_uid,
            "toUid": self.to_uid,
            "recordId": self.record_id,
            "backup": str(self.backup) if self.backup else None,
        }


def db_path(explicit: Optional[str] = None) -> Path:
    if explicit:
        return Path(explicit).expanduser()
    return DB_PATH


def connect(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise HandoverError("找不到会话数据库：%s" % path)
    try:
        conn = sqlite3.connect(str(path), timeout=8)
        conn.execute("PRAGMA busy_timeout = 8000")
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:
        raise HandoverError("打不开数据库：%s" % exc)


def backup_db(path: Path, vault: Vault, keep: int = 5) -> Path:
    """用 sqlite 在线备份 API 做一致性快照（比直接 copy 安全）。"""
    target_dir = ensure_private_dir(vault.root / "db-backups")
    stamp = time.strftime("%Y%m%dT%H%M%S", time.localtime())
    target = target_dir / ("workbuddy-%s.db" % stamp)
    src = connect(path)
    dst = sqlite3.connect(str(target))
    try:
        src.backup(dst)
        dst.commit()
    finally:
        dst.close()
        src.close()
    try:
        os.chmod(target, 0o600)
    except OSError:
        pass
    old = sorted(target_dir.glob("workbuddy-*.db"), key=lambda p: p.stat().st_mtime, reverse=True)
    for stale in old[max(keep, 1):]:
        try:
            stale.unlink()
        except OSError:
            pass
    return target


def current_session_id(uid: str) -> Optional[str]:
    """从 <uid>/startup-route.json 取当前打开的会话。"""
    route = Path.home() / ".workbuddy-ai" / uid / "startup-route.json"
    try:
        data = json.loads(route.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    path = str(data.get("path") or "")
    if "/conversation/" in path:
        return path.rsplit("/", 1)[-1] or None
    return None


def latest_session_id(conn: sqlite3.Connection, uid: str) -> Optional[str]:
    row = conn.execute(
        "select id from sessions where user_id = ? and deleted_at is null "
        "order by coalesce(last_activity_at, created_at) desc limit 1",
        (uid,),
    ).fetchone()
    return row["id"] if row else None


def resolve_session(
    conn: sqlite3.Connection, selector: str, uid: str
) -> sqlite3.Row:
    if selector in ("current", "@current"):
        sid = current_session_id(uid) or latest_session_id(conn, uid)
        if not sid:
            raise HandoverError("找不到当前会话")
    else:
        sid = selector
    row = conn.execute(
        "select id, user_id, title, cwd, status, transport from sessions where id = ? or id like ?",
        (sid, sid + "%"),
    ).fetchone()
    if not row:
        raise HandoverError("找不到会话：%s" % selector)
    return row


def invalidate_sidebar(uid: str) -> None:
    """删掉侧边栏快照缓存，让 App 重新从库里取（startup-route 不动）。"""
    candidate = Path.home() / ".workbuddy-ai" / uid / "sidebar-list-snapshot.json"
    try:
        candidate.unlink()
    except OSError:
        pass


def load_handovers(vault: Vault) -> List[Dict[str, object]]:
    path = vault.root / "handovers.json"
    if not path.is_file():
        return []
    try:
        data = json.loads(path.read_text("utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def save_handovers(vault: Vault, items: List[Dict[str, object]]) -> None:
    from .authfile import atomic_write

    atomic_write(
        vault.root / "handovers.json",
        json.dumps(items[-200:], indent=2, ensure_ascii=False).encode("utf-8"),
        mode=0o600,
    )


def handover(
    vault: Vault,
    db: Path,
    selector: str,
    to_uid: str,
    *,
    dry_run: bool = False,
    allow_cross_domain: bool = False,
) -> HandoverResult:
    conn = connect(db)
    try:
        row = resolve_session(conn, selector, vault.active_uid() or "")
        session_id = row["id"]
        from_uid = row["user_id"]
        title = row["title"] or "(无标题)"

        if row["transport"] != "local":
            return HandoverResult(
                "refused",
                "这是云端会话（transport=%s），改归属可能与服务端不一致，已拒绝" % row["transport"],
                session_id=session_id,
                title=title,
                from_uid=from_uid,
                to_uid=to_uid,
            )
        if from_uid == to_uid:
            return HandoverResult(
                "refused", "会话已经属于目标账号，无需过继", session_id=session_id, title=title,
                from_uid=from_uid, to_uid=to_uid,
            )

        if dry_run:
            return HandoverResult(
                "ok",
                "[dry-run] 将把「%s」从 %s 过继到 %s" % (title, from_uid[:8], to_uid[:8]),
                session_id=session_id, title=title, from_uid=from_uid, to_uid=to_uid,
            )

        backup = backup_db(db, vault)
        try:
            conn.execute("BEGIN IMMEDIATE")
            conn.execute("update sessions set user_id = ? where id = ?", (to_uid, session_id))
            conn.commit()
        except sqlite3.Error as exc:
            conn.rollback()
            return HandoverResult(
                "error", "写库失败，已回滚：%s" % exc, session_id=session_id, title=title,
                from_uid=from_uid, to_uid=to_uid, backup=backup,
            )

        record_id = "%d-%s" % (int(time.time() * 1000), session_id[:8])
        items = load_handovers(vault)
        items.append(
            {
                "id": record_id,
                "sessionId": session_id,
                "title": title,
                "fromUid": from_uid,
                "toUid": to_uid,
                "at": int(time.time() * 1000),
                "undone": False,
            }
        )
        save_handovers(vault, items)
        invalidate_sidebar(from_uid)
        invalidate_sidebar(to_uid)

        return HandoverResult(
            "ok",
            "已把「%s」过继到 %s，切换后可直接继续" % (title, to_uid[:8]),
            session_id=session_id, title=title, from_uid=from_uid, to_uid=to_uid,
            record_id=record_id, backup=backup,
        )
    finally:
        conn.close()


def undo(vault: Vault, db: Path, record_id: Optional[str] = None) -> HandoverResult:
    items = load_handovers(vault)
    target = None
    for item in reversed(items):
        if item.get("undone"):
            continue
        if record_id is None or item.get("id") == record_id:
            target = item
            break
    if target is None:
        return HandoverResult("refused", "没有可撤销的过继记录")

    conn = connect(db)
    try:
        backup = backup_db(db, vault)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute(
            "update sessions set user_id = ? where id = ?",
            (target["fromUid"], target["sessionId"]),
        )
        conn.commit()
    except sqlite3.Error as exc:
        conn.rollback()
        return HandoverResult("error", "撤销失败：%s" % exc, backup=backup)
    finally:
        conn.close()

    target["undone"] = True
    save_handovers(vault, items)
    invalidate_sidebar(str(target["fromUid"]))
    invalidate_sidebar(str(target["toUid"]))
    return HandoverResult(
        "ok",
        "已撤销：会话「%s」回到 %s" % (target.get("title"), str(target["fromUid"])[:8]),
        session_id=str(target["sessionId"]),
        title=str(target.get("title") or ""),
        from_uid=str(target["toUid"]),
        to_uid=str(target["fromUid"]),
        record_id=str(target["id"]),
        backup=backup,
    )
