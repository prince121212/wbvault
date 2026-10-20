"""常驻采集：登录态一变就进账号库。

为什么必须常驻：用户手动登录另一个账号的瞬间，上一个账号的 token 就被覆盖了。
只有一直在看，才留得住「切回去」的可能。
"""

from __future__ import annotations

import time
from pathlib import Path
from typing import Callable, Dict, Optional

from .authfile import AuthFileError, read_state, sha256_bytes
from .store import Vault

Log = Callable[[str], None]


def fingerprint(path: Path) -> Optional[tuple]:
    try:
        stat = path.stat()
        raw = path.read_bytes()
    except OSError:
        return None
    return (stat.st_mtime_ns, stat.st_size, sha256_bytes(raw))


def scan_once(vault: Vault, auth_file: Path, log: Log = print) -> Dict[str, object]:
    """检查一次；返回 {'event': ..., 'uid': ...}。"""
    if not auth_file.is_file():
        if vault.active_uid() is not None:
            # 锁内只改 activeUid：常驻进程不能整份覆盖别人写进去的索引
            vault.set_active(None)
            log("检测到登出（登录态文件不存在），快照保留")
            return {"event": "logout"}
        return {"event": "idle"}

    try:
        state = read_state(auth_file)
    except AuthFileError:
        return {"event": "unreadable"}

    entry = next((e for e in vault.entries() if e.get("uid") == state.uid), None)
    if entry is None:
        vault.snapshot(state)
        log("发现新账号 %s（uin %s），已入库" % (state.uid[:8], state.uin or "-"))
        return {"event": "new", "uid": state.uid}

    if entry.get("sha256") != state.sha256:
        vault.snapshot(state)
        log("已更新账号 %s 的登录态快照" % state.uid[:8])
        return {"event": "refreshed", "uid": state.uid}

    # 登录态没变，但 App 可能刚写完账号档案（含邮箱），补一下
    email = vault.refresh_email(state.uid)
    if email:
        log("已记录账号 %s 的邮箱 %s" % (state.uid[:8], email))
        return {"event": "profile", "uid": state.uid, "email": email}

    return {"event": "idle", "uid": state.uid}


def watch(
    vault: Vault,
    auth_file: Path,
    interval: float = 1.0,
    once: bool = False,
    log: Log = print,
    max_seconds: Optional[float] = None,
) -> int:
    vault.ensure()
    if once:
        scan_once(vault, auth_file, log)
        return 0

    # 启动时先采一次，否则第一次变更前的账号会漏掉
    scan_once(vault, auth_file, log)
    last = fingerprint(auth_file)
    log("开始监听 %s（每 %.1fs 检查一次，Ctrl-C 退出）" % (auth_file, interval))
    started = time.time()
    try:
        while True:
            current = fingerprint(auth_file)
            if current != last:
                # 稍等一下再读，避免读到 daemon 写一半的文件
                time.sleep(0.2)
                scan_once(vault, auth_file, log)
                last = fingerprint(auth_file)
            if max_seconds is not None and time.time() - started >= max_seconds:
                return 0
            time.sleep(interval)
    except KeyboardInterrupt:
        log("已停止监听")
        return 0
