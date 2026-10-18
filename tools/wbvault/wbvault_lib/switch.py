"""切换状态机。

    IDLE -> PRECHECK -> SNAPSHOT_CURRENT -> BACKUP -> WRITE -> VERIFY
                                                       |-> COMMIT
                                                       |-> RETRY -> ROLLBACK

原则：写完必须校验；校验不过必须回滚；读不到就如实说「未确认」，不谎报成功。
"""

from __future__ import annotations

import os
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from .authfile import (
    AuthFileError,
    AuthState,
    atomic_write,
    copy_backup,
    parse_state,
    read_observation,
    read_state,
)
from .paths import observation_for
from .store import Vault

APP_PROCESS_PATTERNS = (
    "WorkBuddy AI.app/Contents/MacOS/Electron",
    "WorkBuddy AI.app/Contents/Resources/app.asar",
)

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PENDING = 3


@dataclass
class SwitchResult:
    status: str  # ok | noop | pending | rolled_back | refused | error
    message: str
    exit_code: int = EXIT_OK
    target_uid: str = ""
    previous_uid: str = ""
    backup: Optional[Path] = None
    verified_by: str = ""

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "exitCode": self.exit_code,
            "targetUid": self.target_uid,
            "previousUid": self.previous_uid,
            "backup": str(self.backup) if self.backup else None,
            "verifiedBy": self.verified_by,
        }


def app_is_running() -> bool:
    for pattern in APP_PROCESS_PATTERNS:
        try:
            proc = subprocess.run(
                ["pgrep", "-f", pattern],
                capture_output=True,
                text=True,
                timeout=3,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode == 0 and proc.stdout.strip():
            return True
    return False


def _wait_file_settle(path: Path, quiet_seconds: float = 2.0, timeout: float = 4.0) -> bool:
    """等文件安静下来，避免和 daemon 的 token 刷新抢写。"""
    deadline = time.time() + timeout
    last = None
    quiet_since = time.time()
    while time.time() < deadline:
        try:
            mtime = path.stat().st_mtime
        except OSError:
            return False
        if last is None or abs(mtime - last) > 1e-6:
            last = mtime
            quiet_since = time.time()
        elif time.time() - quiet_since >= quiet_seconds:
            return True
        time.sleep(0.2)
    return time.time() - quiet_since >= quiet_seconds


def _verify(
    auth_file: Path,
    target_uid: str,
    seconds: float,
    log: Callable[[str], None],
) -> tuple:
    """返回 (ok, reverted, last_state)。持续观察文件里的 uid 是否稳定为目标。"""
    deadline = time.time() + seconds
    last_state: Optional[AuthState] = None
    while time.time() < deadline:
        time.sleep(0.4)
        try:
            last_state = read_state(auth_file)
        except AuthFileError:
            continue
        if last_state.uid != target_uid:
            log("检测到登录态被改回 %s" % last_state.uid[:8])
            return False, True, last_state
    return True, False, last_state


def switch(
    vault: Vault,
    auth_file: Path,
    selector: str,
    *,
    verify_seconds: float = 4.0,
    settle_seconds: float = 2.0,
    force: bool = False,
    dry_run: bool = False,
    allow_domain_mismatch: bool = False,
    log: Callable[[str], None] = print,
) -> SwitchResult:
    # ---------- 读取当前 ----------
    try:
        current = read_state(auth_file)
    except AuthFileError as exc:
        return SwitchResult("error", str(exc), EXIT_FAILED)

    # ---------- 解析目标 ----------
    entry = vault.find(selector)
    if not entry:
        return SwitchResult(
            "refused",
            "账号库里没有这个账号：%s（先登录一次，或用 wbvault snapshot 采集）" % selector,
            EXIT_USAGE,
        )
    target_uid = str(entry.get("uid", ""))
    snap_path = vault.snapshot_path(target_uid)
    if not snap_path.is_file():
        return SwitchResult("refused", "目标快照文件缺失：%s" % snap_path, EXIT_USAGE)
    if not vault.verify_snapshot(entry):
        return SwitchResult("refused", "目标快照校验失败（sha256 不一致），已拒绝切换", EXIT_FAILED)

    try:
        target = read_state(snap_path)
    except AuthFileError as exc:
        return SwitchResult("refused", "目标快照无法解析：%s" % exc, EXIT_FAILED)
    if target.uid != target_uid:
        return SwitchResult("refused", "目标快照内部 uid 与索引不一致，已拒绝切换", EXIT_FAILED)

    # ---------- 环境一致性 ----------
    if target.domain and current.domain and target.domain != current.domain:
        if not allow_domain_mismatch:
            return SwitchResult(
                "refused",
                "环境不一致：当前 %s，目标 %s。跨环境切换会导致登录态失效，"
                "如确认要切请加 --allow-domain-mismatch" % (current.domain, target.domain),
                EXIT_USAGE,
            )

    # ---------- 空操作 ----------
    same_account = target.uid == current.uid and not force

    if dry_run:
        if same_account:
            message = "[dry-run] 当前已是 %s（%s），不会执行写入" % (
                entry.get("alias") or target.uid_short,
                target.uid[:8],
            )
        else:
            message = "[dry-run] 将切换到 %s（%s），当前 %s" % (
                entry.get("alias") or target.uid_short,
                target.uid[:8],
                current.uid[:8],
            )
        return SwitchResult(
            "ok",
            message,
            EXIT_OK,
            target_uid=target.uid,
            previous_uid=current.uid,
            verified_by="dry-run",
        )

    if same_account:
        vault.snapshot(current)
        return SwitchResult(
            "noop",
            "当前已经是这个账号（%s），无需切换" % (entry.get("alias") or current.uid_short),
            EXIT_OK,
            target_uid=target.uid,
            previous_uid=current.uid,
            verified_by="file",
        )

    # ---------- 竞态预检 ----------
    if not _wait_file_settle(auth_file, quiet_seconds=settle_seconds, timeout=max(settle_seconds + 2.0, 2.0)):
        return SwitchResult(
            "refused",
            "登录态文件正在被频繁写入（可能在刷新 token），请稍后重试",
            EXIT_FAILED,
        )

    # ---------- 采集当前 + 备份 ----------
    try:
        vault.snapshot(current)
    except Exception as exc:  # noqa: BLE001 - 采集失败不应阻断回滚能力，但要提示
        log("警告：切换前采集当前账号失败：%s" % exc)

    backup = copy_backup(auth_file, vault.backups_dir, keep=12, uid_hint=current.uid)
    log("已备份当前登录态：%s" % backup.name)

    # ---------- 写入 ----------
    try:
        atomic_write(auth_file, target.raw, mode=0o600)
        log("已原子替换为 %s（%s）" % (entry.get("alias") or target.uid_short, target.uid[:8]))
    except OSError as exc:
        return SwitchResult("error", "写入失败，未改动登录态：%s" % exc, EXIT_FAILED,
                            target_uid=target.uid, previous_uid=current.uid, backup=backup)

    # ---------- 校验 ----------
    ok, reverted, _ = _verify(auth_file, target.uid, verify_seconds, log)

    if not ok:
        log("第一次校验未通过，重试一次…")
        time.sleep(0.8)
        try:
            atomic_write(auth_file, target.raw, mode=0o600)
        except OSError as exc:
            log("重试写入失败：%s" % exc)
        ok, reverted, _ = _verify(auth_file, target.uid, max(verify_seconds, 2.0), log)

    if not ok:
        # ---------- 回滚 ----------
        log("校验失败，开始回滚…")
        try:
            atomic_write(auth_file, backup.read_bytes(), mode=0o600)
            restored = read_state(auth_file)
            if restored.uid != current.uid:
                return SwitchResult(
                    "error",
                    "回滚后账号仍不是原账号（%s -> %s），请手动处理，备份：%s"
                    % (current.uid[:8], restored.uid[:8], backup),
                    EXIT_FAILED,
                    target_uid=target.uid,
                    previous_uid=current.uid,
                    backup=backup,
                )
        except (OSError, AuthFileError) as exc:
            return SwitchResult(
                "error",
                "回滚失败：%s。备份文件：%s" % (exc, backup),
                EXIT_FAILED,
                target_uid=target.uid,
                previous_uid=current.uid,
                backup=backup,
            )
        return SwitchResult(
            "rolled_back",
            "切换未生效（可能被 App 的 token 刷新覆盖），已还原为原账号。备份：%s" % backup.name,
            EXIT_FAILED,
            target_uid=target.uid,
            previous_uid=current.uid,
            backup=backup,
            verified_by="rollback",
        )

    # ---------- 校验通过 ----------
    verified_by = "file"
    obs = read_observation(observation_for(auth_file))
    identity = (obs.get("identity") or {}) if isinstance(obs, dict) else {}
    updated_at = int(obs.get("updatedAt") or 0) if isinstance(obs, dict) else 0
    fresh = bool(updated_at) and (time.time() * 1000 - updated_at) < 60_000
    if identity.get("userId") == target.uid and obs.get("active") and fresh:
        verified_by = "file+observation"

    running = app_is_running()

    # 把切换后可能被刷新过的 token 再采一次（失败不影响结果）
    try:
        vault.snapshot(read_state(auth_file))
    except Exception:  # noqa: BLE001
        pass

    if not running:
        return SwitchResult(
            "pending",
            "已写入，但 App 未运行，无法确认热重载；下次启动时生效",
            EXIT_PENDING,
            target_uid=target.uid,
            previous_uid=current.uid,
            backup=backup,
            verified_by=verified_by,
        )

    return SwitchResult(
        "ok",
        "已切换到 %s（%s），校验通过（%s）"
        % (entry.get("alias") or target.uid_short, target.uid[:8], verified_by),
        EXIT_OK,
        target_uid=target.uid,
        previous_uid=current.uid,
        backup=backup,
        verified_by=verified_by,
    )


def restore_latest(vault: Vault, auth_file: Path, log: Callable[[str], None] = print) -> SwitchResult:
    """把最近一次备份还原回去（手动兜底）。"""
    backups = vault.backups()
    if not backups:
        return SwitchResult("refused", "没有可用的备份", EXIT_USAGE)
    backup = backups[0]
    try:
        current = read_state(auth_file)
        previous_uid = current.uid
    except AuthFileError:
        previous_uid = ""
    try:
        raw = backup.read_bytes()
        restored = parse_state(backup, raw)
        atomic_write(auth_file, raw, mode=0o600)
    except (OSError, AuthFileError) as exc:
        return SwitchResult("error", "还原失败：%s" % exc, EXIT_FAILED, previous_uid=previous_uid)
    log("已用 %s 还原为 %s" % (backup.name, restored.uid[:8]))
    return SwitchResult(
        "ok",
        "已还原为 %s" % restored.uid[:8],
        EXIT_OK,
        target_uid=restored.uid,
        previous_uid=previous_uid,
        backup=backup,
        verified_by="file",
    )
