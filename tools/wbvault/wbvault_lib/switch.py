"""切换状态机。

    IDLE -> PRECHECK -> SNAPSHOT_CURRENT -> BACKUP -> WRITE -> VERIFY
                                                       |-> COMMIT
                                                       |-> RETRY -> ROLLBACK

原则：写完必须校验；校验不过必须回滚；读不到就如实说「未确认」，不谎报成功。
"""

from __future__ import annotations

import os
import re
import signal
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

APP_BUNDLE_ID = "com.workbuddy.workbuddy-ai"
APP_NAME = "WorkBuddy AI"

# App 的登录态监听（fs.watch 目录）一旦静默失效，写文件它也不会知道。
# 唯一能确认「真的热重载了」的外部证据是它自己打的这行日志。
APP_LOG_ROOT = Path.home() / ".workbuddy-ai" / "logs"
APP_LOG_RELOAD_MARK = "Restored auth session from file, userId: "
APP_LOG_RECONCILE_MARK = "Auth storage changed externally"
_LOG_LINE_RE = re.compile(
    r"^\[(\d{2})/(\d{2})/(\d{4}), (\d{1,2}):(\d{2}):(\d{2}) (AM|PM)\.(\d{3})\]"
)
_LOG_TAIL_BYTES = 400_000

EXIT_OK = 0
EXIT_FAILED = 1
EXIT_USAGE = 2
EXIT_PENDING = 3


def _label(entry, fallback: str) -> str:
    """账号的人话名字：别名 > 邮箱 > uid 前 8 位。"""
    if not entry:
        return fallback
    return str(entry.get("alias") or entry.get("email") or fallback)


@dataclass
class SwitchResult:
    status: str  # ok | noop | pending | rolled_back | refused | error
    message: str
    exit_code: int = EXIT_OK
    target_uid: str = ""
    previous_uid: str = ""
    backup: Optional[Path] = None
    verified_by: str = ""
    needs_restart: bool = False

    def to_dict(self) -> dict:
        return {
            "status": self.status,
            "message": self.message,
            "exitCode": self.exit_code,
            "targetUid": self.target_uid,
            "previousUid": self.previous_uid,
            "backup": str(self.backup) if self.backup else None,
            "verifiedBy": self.verified_by,
            "needsRestart": self.needs_restart,
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


def running_inside_app() -> bool:
    """调用方是不是 WorkBuddy 自己拉起的进程（App 内置终端 / Agent 的 shell）。

    这种时候绝不能自动重启：会把正在跑命令的 App 一起掐掉。
    macOS 会把宿主 App 的 bundle id 塞进子进程环境变量。
    """
    return os.environ.get("__CFBundleIdentifier", "") == APP_BUNDLE_ID


def app_pid() -> Optional[int]:
    """App 主进程 pid；用来判断「热重载失效」是不是同一个进程还在坏着。"""
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
        if proc.returncode != 0:
            continue
        for token in proc.stdout.split():
            if token.isdigit():
                return int(token)
    return None


def _parse_log_time(line: str) -> Optional[float]:
    match = _LOG_LINE_RE.match(line)
    if not match:
        return None
    month, day, year, hour, minute, second, ampm, millis = match.groups()
    hour = int(hour) % 12 + (12 if ampm == "PM" else 0)
    try:
        stamp = time.strptime(
            "%s-%s-%s %02d:%s:%s" % (year, month, day, hour, minute, second),
            "%Y-%m-%d %H:%M:%S",
        )
    except ValueError:
        return None
    return time.mktime(stamp) + int(millis) / 1000.0


def _app_log_files():
    day_dir = APP_LOG_ROOT / time.strftime("%Y-%m-%d")
    if not day_dir.is_dir():
        return day_dir, []
    files = [p for p in day_dir.glob("workbuddyMainThread*.log") if p.is_file()]
    if not files:
        files = [p for p in day_dir.glob("*.log") if p.is_file()]
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    return day_dir, files


def _read_tail(path: Path, limit: int = _LOG_TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with open(path, "rb") as handle:
            if size > limit:
                handle.seek(size - limit)
            return handle.read().decode("utf-8", "replace")
    except OSError:
        return ""


def scan_app_logs(since: float) -> tuple:
    """返回 (日志目录存在, 最近一条日志时间, 已重载的 uid 集合, 是否收到过外部变更事件)。"""
    day_dir, files = _app_log_files()
    if not files:
        return day_dir.is_dir(), 0.0, set(), False
    latest = 0.0
    reloaded = set()
    reconciled = False
    for path in files:
        for line in _read_tail(path).splitlines():
            stamp = _parse_log_time(line)
            if stamp is None:
                continue
            if stamp > latest:
                latest = stamp
            if stamp < since - 1.0:
                continue
            if APP_LOG_RELOAD_MARK in line:
                reloaded.add(line.split(APP_LOG_RELOAD_MARK, 1)[1].strip()[:36])
            elif APP_LOG_RECONCILE_MARK in line:
                reconciled = True
    return True, latest, reloaded, reconciled


def probe_app_reload(target_uid: str, since: float, timeout: float = 2.5, interval: float = 0.3) -> str:
    """App 是否真的热重载到了目标账号。

    返回 reloaded / not_reloaded / unknown：
    - reloaded：日志里出现了「Restored auth session from file, userId: <目标>」；
    - not_reloaded：有日志可读，但等到超时也没出现这行 —— 它的监听失效了，只能重启；
    - unknown：连日志目录都找不到，不下结论（不谎报，也不冤枉）。
    """
    deadline = time.time() + timeout
    has_logs = False
    while True:
        has_logs, _latest, reloaded, _reconciled = scan_app_logs(since)
        if any(uid.startswith(target_uid[:8]) or target_uid.startswith(uid[:8]) for uid in reloaded):
            return "reloaded"
        if time.time() >= deadline:
            break
        time.sleep(interval)
    return "not_reloaded" if has_logs else "unknown"


def probe_watcher_alive(auth_file: Path, timeout: float = 3.0) -> str:
    """主动探活：只动 mtime（内容一字不改），看 App 有没有收到变更事件。

    alive / dead / unknown。健康时 App 会 re-read 同一份内容，等于空操作。
    """
    try:
        os.utime(auth_file, None)
    except OSError:
        return "unknown"
    since = time.time()
    deadline = since + timeout
    has_logs = False
    while True:
        has_logs, _latest, _reloaded, reconciled = scan_app_logs(since)
        if reconciled:
            return "alive"
        if time.time() >= deadline:
            break
        time.sleep(0.3)
    return "dead" if has_logs else "unknown"


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
    reload_wait: float = 2.5,
    auto_restart: Optional[bool] = None,
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

    if auto_restart is None:
        auto_restart = vault.auto_restart()

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
                _label(entry, target.uid_short),
                target.uid[:8],
            )
        else:
            message = "[dry-run] 将切换到 %s（%s），当前 %s" % (
                _label(entry, target.uid_short),
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
            "当前已经是这个账号（%s），无需切换" % (_label(entry, current.uid_short)),
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
    write_started = time.time() - 1.0
    try:
        atomic_write(auth_file, target.raw, mode=0o600)
        log("已原子替换为 %s（%s）" % (_label(entry, target.uid_short), target.uid[:8]))
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
    pid = app_pid() if running else None

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

    # ---------- 确认 App 真的热重载了 ----------
    # 文件写对 ≠ 画面变了。App 靠自己的目录监听发现外部改动，这个监听会静默失效，
    # 失效时它照样「什么都没发生」，只有重启才会重新 restore()。
    # 同一个 App 进程上次就失效过 → 不用再白等一轮。
    known_dead = False
    if pid is not None:
        last = vault.reload_state()
        known_dead = last.get("appPid") == pid and last.get("healthy") is False

    reload_state = "not_reloaded" if known_dead else probe_app_reload(target.uid, write_started, timeout=reload_wait)
    if reload_state == "reloaded":
        if verified_by == "file":
            verified_by = "file+app-log"
        vault.record_reload(pid, True, target.uid)
        return SwitchResult(
            "ok",
            "已切换到 %s（%s），App 已热重载（%s）"
            % (_label(entry, target.uid_short), target.uid[:8], verified_by),
            EXIT_OK,
            target_uid=target.uid,
            previous_uid=current.uid,
            backup=backup,
            verified_by=verified_by,
        )

    if reload_state == "not_reloaded":
        vault.record_reload(pid, False, target.uid)
        if auto_restart and running_inside_app():
            # 从 App 内部发起（Agent / 内置终端）：重启会掐断调用者，只能如实报告
            return SwitchResult(
                "pending",
                "登录态已写入 %s（%s），但 App 没有热重载。当前调用来自 App 内部，"
                "自动重启会中断这次操作——请在菜单栏点「重启 WorkBuddy 生效」或手动重启"
                % (_label(entry, target.uid_short), target.uid[:8]),
                EXIT_PENDING,
                target_uid=target.uid,
                previous_uid=current.uid,
                backup=backup,
                verified_by=verified_by,
                needs_restart=True,
            )
        if auto_restart:
            log("App 没有热重载（监听已失效），自动重启让它生效…")
            restarted = restart_app(log=log, expect_uid=target.uid)
            if restarted.status == "ok":
                vault.record_reload(app_pid(), True, target.uid)
                return SwitchResult(
                    "ok",
                    "已切换到 %s（%s），并自动重启 App 使其生效"
                    % (_label(entry, target.uid_short), target.uid[:8]),
                    EXIT_OK,
                    target_uid=target.uid,
                    previous_uid=current.uid,
                    backup=backup,
                    verified_by="restart",
                )
            return SwitchResult(
                "pending",
                "登录态已写入 %s（%s），但 App 的监听失效且自动重启失败：%s"
                % (_label(entry, target.uid_short), target.uid[:8], restarted.message),
                EXIT_PENDING,
                target_uid=target.uid,
                previous_uid=current.uid,
                backup=backup,
                verified_by=verified_by,
                needs_restart=True,
            )
        return SwitchResult(
            "pending",
            "登录态已写入 %s（%s），但 App 没有热重载：它的登录态监听已经失效，"
            "当前画面还是旧账号。重启 App 才会生效：wbvault restart-app"
            % (_label(entry, target.uid_short), target.uid[:8]),
            EXIT_PENDING,
            target_uid=target.uid,
            previous_uid=current.uid,
            backup=backup,
            verified_by=verified_by,
            needs_restart=True,
        )

    return SwitchResult(
        "ok",
        "已切换到 %s（%s），校验通过（%s）"
        % (_label(entry, target.uid_short), target.uid[:8], verified_by),
        EXIT_OK,
        target_uid=target.uid,
        previous_uid=current.uid,
        backup=backup,
        verified_by=verified_by,
    )


def _quit_app(timeout: float = 8.0, pid: Optional[int] = None) -> tuple:
    """先礼后兵：AppleScript 请它自己退，超时就 SIGTERM 主进程。"""
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application id "%s" to quit' % APP_BUNDLE_ID],
            capture_output=True,
            text=True,
            timeout=8,
        )
    except (OSError, subprocess.SubprocessError):
        pass

    started = time.time()
    killed = False
    while time.time() - started < timeout:
        if not app_is_running():
            return True, ""
        if not killed and time.time() - started > 3.0 and pid:
            try:
                os.kill(pid, signal.SIGTERM)
                killed = True
            except OSError:
                pass
        time.sleep(0.3)
    return False, "App 没有退出（可能有弹窗拦着），请手动退出后重试"


def _launch_app(timeout: float = 25.0) -> tuple:
    for args in (["open", "-b", APP_BUNDLE_ID], ["open", "-a", APP_NAME]):
        try:
            proc = subprocess.run(args, capture_output=True, text=True, timeout=20)
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        deadline = time.time() + timeout
        while time.time() < deadline:
            if app_is_running():
                return True, ""
            time.sleep(0.4)
        return False, "已发出启动命令，但没等到进程起来"
    return False, "找不到 %s，请手动打开" % APP_NAME


def restart_app(
    log: Callable[[str], None] = print,
    expect_uid: str = "",
    quit_timeout: float = 8.0,
    launch_timeout: float = 25.0,
) -> SwitchResult:
    """重启 WorkBuddy AI，让写在文件里的登录态生效。

    热重载失效时唯一可靠的办法：App 只在「启动」和「文件事件」两个时机读登录态，
    事件通道死了就没有第三个入口。会结束当前 App 会话（未发送的输入会丢），
    所以只在用户显式要求或 settings.autoRestart 打开时调用。
    """
    pid = app_pid()
    if app_is_running():
        log("正在退出 %s（会结束当前会话）…" % APP_NAME)
        ok, message = _quit_app(quit_timeout, pid)
        if not ok:
            return SwitchResult("error", message, EXIT_FAILED)

    launched_at = time.time()
    log("正在启动 %s…" % APP_NAME)
    ok, message = _launch_app(launch_timeout)
    if not ok:
        return SwitchResult("error", message, EXIT_FAILED)

    if expect_uid:
        log("等待 App 加载目标账号…")
        state = probe_app_reload(expect_uid, launched_at, timeout=launch_timeout, interval=0.8)
        if state != "reloaded":
            return SwitchResult(
                "error",
                "App 已重启，但没等到它加载目标账号（%s），请打开 App 确认" % expect_uid[:8],
                EXIT_FAILED,
                target_uid=expect_uid,
                verified_by="restart",
                needs_restart=True,
            )
    return SwitchResult(
        "ok",
        "已重启 %s，登录态已按文件生效" % APP_NAME,
        EXIT_OK,
        target_uid=expect_uid,
        verified_by="restart",
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
