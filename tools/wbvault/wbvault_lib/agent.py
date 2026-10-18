"""macOS LaunchAgent：让 watch 常驻，保证「登录新账号的瞬间」能留住旧账号。

不常驻的后果很具体：你手动登出 A、登录 B，A 的 token 当场被覆盖，
之后再运行 wbvault 也救不回来。所以常驻采集是功能的一部分，不是可选项。
"""

from __future__ import annotations

import os
import plistlib
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Tuple

LABEL = "ai.workbuddy.wbvault.watch"


def stage_runtime(source_script: Path, vault_root: Path) -> Path:
    """把运行时复制到本机磁盘上的稳定位置。

    launchd 往往无法执行外接卷上的脚本（bootstrap 会报 Input/output error），
    复制到 ~/Library/Application Support 下可以避免这个问题，
    也让常驻进程不依赖开发目录是否挂载。
    """
    source_dir = source_script.parent
    runtime = vault_root / "runtime"
    if runtime.exists():
        shutil.rmtree(runtime)
    shutil.copytree(
        source_dir,
        runtime,
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "tests", ".git"),
    )
    return runtime / source_script.name


def plist_path() -> Path:
    return Path.home() / "Library" / "LaunchAgents" / ("%s.plist" % LABEL)


def log_path() -> Path:
    return Path.home() / "Library" / "Logs" / "wbvault-watch.log"


def _launchctl(*args: str) -> Tuple[int, str]:
    try:
        proc = subprocess.run(["launchctl", *args], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or proc.stderr).strip()


def install(script: Path, interval: float = 1.0, vault_root: Path = None) -> Tuple[bool, str]:
    python = sys.executable or "/usr/bin/python3"
    if vault_root is not None:
        try:
            script = stage_runtime(script, vault_root)
        except OSError as exc:
            return False, "复制运行时不成功：%s" % exc
    plist = {
        "Label": LABEL,
        "ProgramArguments": [
            python,
            str(script),
            "watch",
            "--interval",
            str(interval),
        ],
        "RunAtLoad": True,
        "KeepAlive": True,
        "ProcessType": "Background",
        "StandardOutPath": str(log_path()),
        "StandardErrorPath": str(log_path()),
        "EnvironmentVariables": {"PYTHONUNBUFFERED": "1"},
    }
    target = plist_path()
    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "wb") as handle:
        plistlib.dump(plist, handle)
    os.chmod(target, 0o644)

    uid = str(os.getuid())
    _launchctl("bootout", "gui/%s/%s" % (uid, LABEL))
    code, out = _launchctl("bootstrap", "gui/%s" % uid, str(target))
    if code != 0:
        code, out = _launchctl("load", "-w", str(target))

    if "已加载" not in status():
        return False, (
            "plist 已写入：%s\n"
            "但当前环境无法注册 launchd 服务（%s）。\n"
            "请在本机终端里执行一次：\n"
            "  launchctl bootstrap gui/%s %s\n"
            "然后用 wbvault doctor 确认「常驻监听」为已加载。"
            % (target, out or "bootstrap 被拒绝", uid, target)
        )
    return True, "已安装并启动常驻监听（%s），日志：%s" % (target, log_path())


def uninstall() -> Tuple[bool, str]:
    target = plist_path()
    uid = str(os.getuid())
    _launchctl("bootout", "gui/%s/%s" % (uid, LABEL))
    _launchctl("unload", "-w", str(target))
    if target.exists():
        try:
            target.unlink()
        except OSError as exc:
            return False, "删除 plist 失败：%s" % exc
    return True, "已停止并移除常驻监听"


def status() -> str:
    uid = str(os.getuid())
    code, out = _launchctl("print", "gui/%s/%s" % (uid, LABEL))
    if code == 0:
        state = "running" if "state = running" in out else "loaded"
        return "已加载（%s）" % state
    if plist_path().exists():
        return "已安装但未运行"
    return "未安装"
