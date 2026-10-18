#!/usr/bin/env python3
"""wbvault - WorkBuddy AI 本地账号管家

把登录过的账号留在本机，想切哪个切哪个。
只搬运字节，不解密、不上传、不跨机迁移。
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from wbvault_lib import agent as agent_mod  # noqa: E402
from wbvault_lib import format as fmt  # noqa: E402
from wbvault_lib import handover as handover_mod  # noqa: E402
from wbvault_lib.authfile import (  # noqa: E402
    AuthFileError,
    list_backups,
    read_observation,
    read_state,
)
from wbvault_lib.paths import (  # noqa: E402
    PathError,
    auth_dir,
    observation_for,
    resolve_auth_file,
    vault_root,
)
from wbvault_lib.store import Vault, VaultError  # noqa: E402
from wbvault_lib.switch import (  # noqa: E402
    EXIT_FAILED,
    EXIT_OK,
    EXIT_PENDING,
    EXIT_USAGE,
    app_is_running,
    restore_latest,
    switch,
)
from wbvault_lib.watcher import scan_once, watch  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wbvault",
        description="WorkBuddy AI 本地账号管家：登录过的账号留在本机，一键切换。",
    )
    parser.add_argument("--vault", help="账号库目录（默认 ~/Library/Application Support/WorkBuddyAccountVault）")
    parser.add_argument("--auth-file", help="指定登录态文件（默认自动发现）")
    parser.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本/Skill 调用")
    parser.add_argument("--no-color", action="store_true", help="关闭颜色")

    sub = parser.add_subparsers(dest="command")

    sub.add_parser("current", help="显示当前登录账号")
    sub.add_parser("list", help="列出账号库里的所有账号")

    snap = sub.add_parser("snapshot", help="把当前登录态存进账号库")
    snap.add_argument("--alias", help="给这个账号起个名字")
    snap.add_argument("--note", default="", help="备注")

    sw = sub.add_parser("switch", help="切换到指定账号")
    sw.add_argument("selector", help="uid / uid 前缀 / 别名 / uin")
    sw.add_argument("--wait", type=float, default=4.0, help="校验观察秒数（默认 4）")
    sw.add_argument("--force", action="store_true", help="当前已是该账号时也强制重写")
    sw.add_argument("--dry-run", action="store_true", help="只演练，不写文件")
    sw.add_argument("--allow-domain-mismatch", action="store_true", help="允许跨环境切换（危险）")
    sw.add_argument(
        "--with-current",
        action="store_true",
        help="把当前打开的会话一起过继到目标账号，切过去可继续",
    )
    sw.add_argument("--db", help="会话数据库路径（默认 ~/.workbuddy-ai/workbuddy.db）")

    ho = sub.add_parser("handover", help="把某个会话过继到另一个账号（不切换登录态）")
    ho.add_argument("session", help="会话 id / id 前缀 / current")
    ho.add_argument("--to", required=True, help="目标账号：uid / 别名 / uin")
    ho.add_argument("--dry-run", action="store_true", help="只演练，不写库")
    ho.add_argument("--db", help="会话数据库路径")

    hs = sub.add_parser("handovers", help="查看/撤销会话过继记录")
    hs.add_argument("--undo", nargs="?", const="latest", help="撤销最近一次，或指定记录 id")
    hs.add_argument("--db", help="会话数据库路径")

    rs = sub.add_parser("restore", help="用最近一次备份还原")
    rs.add_argument("--yes", action="store_true", help="跳过确认")

    al = sub.add_parser("alias", help="给账号起别名")
    al.add_argument("selector")
    al.add_argument("name")

    rm = sub.add_parser("remove", help="从账号库删除某个账号快照")
    rm.add_argument("selector")
    rm.add_argument("--yes", action="store_true", help="跳过确认")

    wt = sub.add_parser("watch", help="常驻监听登录态变化并自动采集")
    wt.add_argument("--interval", type=float, default=1.0, help="检查间隔秒（默认 1）")
    wt.add_argument("--once", action="store_true", help="只检查一次")
    wt.add_argument("--seconds", type=float, default=None, help="运行指定秒数后退出")

    sub.add_parser("doctor", help="自检：路径、权限、进程、账号库状态")

    se = sub.add_parser("sessions", help="只读查看各账号的会话与空间归属")
    se.add_argument("--all", action="store_true", help="列出每条会话")
    se.add_argument("--db", help="指定 workbuddy.db（默认 ~/.workbuddy-ai/workbuddy.db）")

    ins = sub.add_parser("install", help="安装常驻监听（LaunchAgent），自动采集登录态变化")
    ins.add_argument("--interval", type=float, default=1.0, help="检查间隔秒（默认 1）")
    sub.add_parser("uninstall", help="卸载常驻监听")

    pr = sub.add_parser("prune", help="清理多余备份")
    pr.add_argument("--keep", type=int, default=12)

    return parser


# ---------------- 输出 ----------------


def print_json(payload: dict) -> None:
    print(json.dumps(payload, indent=2, ensure_ascii=False))


def cmd_current(vault: Vault, auth_file: Path, as_json: bool) -> int:
    try:
        state = read_state(auth_file)
    except AuthFileError as exc:
        if as_json:
            print_json({"ok": False, "error": str(exc)})
        else:
            print(fmt.paint("读取失败：%s" % exc, fmt.RED))
        return EXIT_FAILED

    entry = next((e for e in vault.entries() if e.get("uid") == state.uid), None)
    obs = read_observation(observation_for(auth_file))

    payload = {
        "ok": True,
        "uid": state.uid,
        "uin": state.uin,
        "alias": (entry or {}).get("alias", ""),
        "domain": state.domain,
        "accountType": state.account_type,
        "expiresAt": state.expires_at,
        "refreshExpiresAt": state.refresh_expires_at,
        "daysLeft": state.days_left(),
        "inVault": entry is not None,
        "appRunning": app_is_running(),
        "observation": obs,
        "authFile": str(auth_file),
    }
    if as_json:
        print_json(payload)
        return EXIT_OK

    print(fmt.paint("当前账号", fmt.BOLD))
    print("  别名      %s" % (entry.get("alias") or fmt.paint("(未命名)", fmt.DIM)))
    print("  uid       %s" % state.uid)
    print("  uin       %s" % (state.uin or "-"))
    print("  环境      %s" % (state.domain or "-"))
    print("  类型      %s" % (state.account_type or "-"))
    print("  登录态到期 %s" % fmt.in_days(state.refresh_expires_at))
    print("  已入库    %s" % ("是" if entry else fmt.paint("否（运行 wbvault snapshot）", fmt.YELLOW)))
    print("  App 运行  %s" % ("是" if app_is_running() else fmt.paint("否", fmt.YELLOW)))
    print("  文件      %s" % auth_file)
    return EXIT_OK


def cmd_list(vault: Vault, auth_file: Path, as_json: bool) -> int:
    entries = vault.entries()
    active = None
    try:
        active = read_state(auth_file).uid
    except AuthFileError:
        active = vault.active_uid()

    entries.sort(key=lambda e: (e.get("uid") != active, e.get("alias") or "zzz"))
    if as_json:
        print_json({"ok": True, "activeUid": active, "accounts": entries})
        return EXIT_OK

    if not entries:
        print("账号库还是空的。先运行：wbvault snapshot")
        return EXIT_OK

    rows = []
    for entry in entries:
        uid = str(entry.get("uid", ""))
        ok = vault.verify_snapshot(entry)
        marker = "*" if uid == active else " "
        rows.append(
            [
                marker,
                entry.get("alias") or "-",
                fmt.short_uid(uid),
                fmt.mask_uin(entry.get("uin", "")),
                entry.get("domain") or "-",
                fmt.ago(int(entry.get("lastSeenActiveAt") or 0)),
                fmt.in_days(int(entry.get("refreshExpiresAt") or 0)),
                "正常" if ok else "快照损坏",
            ]
        )
    print(fmt.table(["", "别名", "uid", "uin", "环境", "上次在线", "剩余", "快照"], rows))
    print(fmt.paint("\n* 为当前登录账号；切换：wbvault switch <别名|uid|uin>", fmt.DIM))
    return EXIT_OK


def cmd_snapshot(vault: Vault, auth_file: Path, args) -> int:
    try:
        state = read_state(auth_file)
    except AuthFileError as exc:
        print(fmt.paint("读取失败：%s" % exc, fmt.RED))
        return EXIT_FAILED
    entry = vault.snapshot(state, alias=args.alias, note=args.note)
    if args.json:
        print_json({"ok": True, "account": entry})
    else:
        print("已采集账号 %s（%s）" % (entry.get("alias") or state.uid[:8], state.uid))
        print(fmt.paint("快照：%s" % vault.snapshot_path(state.uid), fmt.DIM))
    return EXIT_OK


def cmd_switch(vault: Vault, auth_file: Path, args) -> int:
    def log(message: str) -> None:
        if not args.json:
            print(fmt.paint("  · %s" % message, fmt.DIM))

    # 先过继当前会话，再切登录态：切过去就能接着干
    if getattr(args, "with_current", False):
        entry = vault.find(args.selector)
        if entry:
            moved = handover_mod.handover(
                vault,
                handover_mod.db_path(getattr(args, "db", None)),
                "current",
                str(entry["uid"]),
                dry_run=args.dry_run,
            )
            if not args.json:
                print(
                    fmt.paint(
                        "  · " + moved.message,
                        fmt.DIM if moved.status == "ok" else fmt.YELLOW,
                    )
                )
            if moved.status != "ok" and not args.force:
                return EXIT_FAILED if moved.status == "error" else EXIT_USAGE

    result = switch(
        vault,
        auth_file,
        args.selector,
        verify_seconds=args.wait,
        force=args.force,
        dry_run=args.dry_run,
        allow_domain_mismatch=args.allow_domain_mismatch,
        log=log,
    )
    if args.json:
        print_json(result.to_dict())
    else:
        color = {
            "ok": fmt.GREEN,
            "noop": fmt.DIM,
            "pending": fmt.YELLOW,
            "rolled_back": fmt.RED,
            "refused": fmt.YELLOW,
            "error": fmt.RED,
        }.get(result.status, fmt.RESET)
        print(fmt.paint(result.message, color))
    return result.exit_code


def cmd_restore(vault: Vault, auth_file: Path, args) -> int:
    backups = list_backups(vault.backups_dir)
    if not backups:
        print(fmt.paint("没有可用的备份", fmt.YELLOW))
        return EXIT_USAGE
    if not args.yes:
        print("将用最近一次备份还原：%s" % backups[0].name)
        try:
            answer = input("确认？[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("已取消")
            return EXIT_USAGE
    result = restore_latest(vault, auth_file)
    if args.json:
        print_json(result.to_dict())
    else:
        print(fmt.paint(result.message, fmt.GREEN if result.status == "ok" else fmt.RED))
    return result.exit_code


def cmd_alias(vault: Vault, args) -> int:
    entry = vault.set_alias(args.selector, args.name)
    if args.json:
        print_json({"ok": True, "account": entry})
    else:
        print("已把 %s 命名为「%s」" % (fmt.short_uid(str(entry["uid"])), entry["alias"]))
    return EXIT_OK


def cmd_remove(vault: Vault, args) -> int:
    entry = vault.find(args.selector)
    if not entry:
        print(fmt.paint("找不到账号：%s" % args.selector, fmt.YELLOW))
        return EXIT_USAGE
    if not args.yes:
        print("将从账号库删除 %s（%s）的快照，不影响当前登录。" % (entry.get("alias") or "-", entry["uid"]))
        try:
            answer = input("确认？[y/N] ").strip().lower()
        except EOFError:
            answer = ""
        if answer not in ("y", "yes"):
            print("已取消")
            return EXIT_USAGE
    vault.remove(args.selector)
    print("已删除")
    return EXIT_OK


def cmd_watch(vault: Vault, auth_file: Path, args) -> int:
    if args.once:
        result = scan_once(vault, auth_file)
        if args.json:
            print_json({"ok": True, **result})
        else:
            print("检查完成：%s" % result.get("event"))
        return EXIT_OK
    code = watch(vault, auth_file, interval=args.interval, max_seconds=args.seconds)
    return code


def cmd_prune(vault: Vault, args) -> int:
    removed = vault.prune_backups(args.keep)
    print("已清理 %d 个旧备份（保留 %d 个）" % (removed, args.keep))
    return EXIT_OK


def cmd_sessions(vault: Vault, args) -> int:
    """只读查询：会话按 user_id 归属，切换账号不会删除任何记录。"""
    import sqlite3

    db = Path(args.db).expanduser() if args.db else Path.home() / ".workbuddy-ai" / "workbuddy.db"
    if not db.is_file():
        print(fmt.paint("找不到数据库：%s" % db, fmt.YELLOW))
        return EXIT_USAGE
    try:
        conn = sqlite3.connect("file:%s?mode=ro" % db, uri=True, timeout=3)
    except sqlite3.Error as exc:
        print(fmt.paint("打不开数据库（先关掉 WorkBuddy 再试）：%s" % exc, fmt.RED))
        return EXIT_FAILED

    aliases = {str(e.get("uid")): (e.get("alias") or "") for e in vault.entries()}
    try:
        if args.all:
            rows = conn.execute(
                "select user_id, coalesce(title,'-'), cwd, coalesce(last_activity_at, created_at) "
                "from sessions where deleted_at is null order by coalesce(last_activity_at, created_at) desc"
            ).fetchall()
            table_rows = []
            for uid, title, cwd, ts in rows:
                table_rows.append(
                    [
                        aliases.get(uid) or fmt.short_uid(uid),
                        fmt.truncate(title, 28),
                        fmt.truncate(Path(cwd or "-").name or "-", 24),
                        fmt.ago(int(ts or 0)),
                    ]
                )
            print(fmt.table(["账号", "会话", "空间目录", "最近活动"], table_rows))
            return EXIT_OK

        rows = conn.execute(
            "select user_id, count(*), count(distinct cwd), max(coalesce(last_activity_at, created_at)) "
            "from sessions where deleted_at is null group by user_id order by 4 desc"
        ).fetchall()
        if not rows:
            print("数据库里还没有会话")
            return EXIT_OK
        table_rows = []
        for uid, count, spaces, ts in rows:
            table_rows.append(
                [
                    aliases.get(uid) or fmt.short_uid(uid),
                    str(count),
                    str(spaces),
                    fmt.ago(int(ts or 0)),
                ]
            )
        print(fmt.table(["账号", "会话数", "空间数", "最近活动"], table_rows))
        print(fmt.paint("\n会话按 user_id 归属；切换账号不会删除任何记录，只是侧边栏换一批。", fmt.DIM))
        return EXIT_OK
    except sqlite3.Error as exc:
        print(fmt.paint("查询失败：%s" % exc, fmt.RED))
        return EXIT_FAILED
    finally:
        conn.close()


def cmd_handover(vault: Vault, args) -> int:
    entry = vault.find(args.to)
    if not entry:
        print(fmt.paint("账号库里没有这个账号：%s" % args.to, fmt.YELLOW))
        return EXIT_USAGE
    result = handover_mod.handover(
        vault,
        handover_mod.db_path(getattr(args, "db", None)),
        args.session,
        str(entry["uid"]),
        dry_run=args.dry_run,
    )
    if args.json:
        print_json(result.to_dict())
    else:
        color = fmt.GREEN if result.status == "ok" else (fmt.YELLOW if result.status == "refused" else fmt.RED)
        print(fmt.paint(result.message, color))
        if result.backup:
            print(fmt.paint("  数据库备份：%s" % result.backup, fmt.DIM))
    return EXIT_OK if result.status == "ok" else EXIT_FAILED


def cmd_handovers(vault: Vault, args) -> int:
    db = handover_mod.db_path(getattr(args, "db", None))
    if args.undo:
        record = None if args.undo in ("latest", "") else args.undo
        result = handover_mod.undo(vault, db, record)
        if args.json:
            print_json(result.to_dict())
        else:
            print(fmt.paint(result.message, fmt.GREEN if result.status == "ok" else fmt.YELLOW))
        return EXIT_OK if result.status == "ok" else EXIT_FAILED

    items = handover_mod.load_handovers(vault)
    if args.json:
        print_json({"ok": True, "handovers": items})
        return EXIT_OK
    if not items:
        print("还没有过继记录")
        return EXIT_OK
    rows = []
    for item in reversed(items[-15:]):
        rows.append(
            [
                "已撤销" if item.get("undone") else "生效中",
                fmt.short_uid(str(item.get("fromUid", ""))),
                "→ " + fmt.short_uid(str(item.get("toUid", ""))),
                fmt.truncate(str(item.get("title") or "-"), 26),
                fmt.ago(int(item.get("at") or 0)),
            ]
        )
    print(fmt.table(["状态", "从", "到", "会话", "时间"], rows))
    print(fmt.paint("\n撤销最近一次：wbvault handovers --undo", fmt.DIM))
    return EXIT_OK


def cmd_install(args, vault: Vault) -> int:
    script = Path(__file__).resolve()
    ok, message = agent_mod.install(script, interval=args.interval, vault_root=vault.root)
    print(fmt.paint(message, fmt.GREEN if ok else fmt.RED))
    if not ok:
        print(fmt.paint("手动兜底：wbvault watch --interval 1（前台运行）", fmt.DIM))
    return EXIT_OK if ok else EXIT_FAILED


def cmd_uninstall(args) -> int:
    ok, message = agent_mod.uninstall()
    print(fmt.paint(message, fmt.GREEN if ok else fmt.RED))
    return EXIT_OK if ok else EXIT_FAILED


def cmd_doctor(vault: Vault, auth_file: Path, as_json: bool) -> int:
    checks = []

    def add(name: str, ok: bool, detail: str, critical: bool = True) -> None:
        checks.append({"name": name, "ok": ok, "detail": detail, "critical": critical})

    exists = auth_file.is_file()
    add("登录态文件存在", exists, str(auth_file))
    state = None
    if exists:
        try:
            state = read_state(auth_file)
            add("登录态可解析", True, "uid %s" % state.uid[:8])
        except AuthFileError as exc:
            add("登录态可解析", False, str(exc))
        mode = stat.S_IMODE(auth_file.stat().st_mode)
        add("文件权限 0600", mode == 0o600, "0%o" % mode, critical=False)

    keyblob = Path.home() / ".workbuddy-ai" / "keyblob"
    add("本机密钥存在", keyblob.is_file(), str(keyblob), critical=False)

    running = app_is_running()
    add("App 正在运行", running, "运行中" if running else "未运行（切换仍会写入，重启后生效）", critical=False)

    obs_path = observation_for(auth_file)
    add("观测文件可用", bool(obs_path), str(obs_path or "未找到"), critical=False)

    vault.ensure()
    entries = vault.entries()
    add("账号库可读", True, "%d 个账号" % len(entries), critical=False)
    if state:
        in_vault = any(e.get("uid") == state.uid for e in entries)
        add("当前账号已入库", in_vault, "已入库" if in_vault else "未入库，运行 wbvault snapshot")
        if in_vault:
            entry = next(e for e in entries if e.get("uid") == state.uid)
            add("快照校验", vault.verify_snapshot(entry), "sha256 一致" if vault.verify_snapshot(entry) else "不一致")

    backups = list_backups(vault.backups_dir)
    add("备份可用", bool(backups), "%d 个备份" % len(backups), critical=False)

    agent_state = agent_mod.status()
    add("常驻监听", "已加载" in agent_state, agent_state, critical=False)

    if as_json:
        print_json({"ok": all(c["ok"] or not c["critical"] for c in checks), "checks": checks})
        return EXIT_OK if all(c["ok"] or not c["critical"] for c in checks) else EXIT_FAILED

    print(fmt.paint("wbvault 自检", fmt.BOLD))
    for check in checks:
        mark = fmt.paint("✓", fmt.GREEN) if check["ok"] else fmt.paint("✗", fmt.RED if check["critical"] else fmt.YELLOW)
        print("  %s %s  %s" % (mark, fmt.pad(check["name"], 16), fmt.paint(check["detail"], fmt.DIM)))

    failed = [c for c in checks if not c["ok"] and c["critical"]]
    if failed:
        print(fmt.paint("\n有 %d 项关键检查未通过" % len(failed), fmt.RED))
        return EXIT_FAILED
    print(fmt.paint("\n自检通过", fmt.GREEN))
    return EXIT_OK


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    # zsh 默认不把 # 当注释（interactive_comments 关闭），
    # 从文档里复制带注释的命令时，注释会被当成参数传进来。这里显式忽略并提示。
    for index, item in enumerate(argv):
        if item.startswith("#"):
            sys.stderr.write(
                "提示：已忽略 # 之后的注释（zsh 默认不把 # 当注释，可执行 setopt interactive_comments）\n"
            )
            argv = argv[:index]
            break

    parser = build_parser()
    args = parser.parse_args(argv)
    fmt.set_color(not args.no_color and sys.stdout.isatty())

    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    try:
        vault = Vault(vault_root(args.vault))
        vault.ensure()
        auth_file = resolve_auth_file(args.auth_file, auth_dir())
    except (PathError, VaultError) as exc:
        print(fmt.paint(str(exc), fmt.RED))
        return EXIT_USAGE

    handlers = {
        "current": lambda: cmd_current(vault, auth_file, args.json),
        "list": lambda: cmd_list(vault, auth_file, args.json),
        "snapshot": lambda: cmd_snapshot(vault, auth_file, args),
        "switch": lambda: cmd_switch(vault, auth_file, args),
        "restore": lambda: cmd_restore(vault, auth_file, args),
        "alias": lambda: cmd_alias(vault, args),
        "remove": lambda: cmd_remove(vault, args),
        "watch": lambda: cmd_watch(vault, auth_file, args),
        "prune": lambda: cmd_prune(vault, args),
        "doctor": lambda: cmd_doctor(vault, auth_file, args.json),
        "sessions": lambda: cmd_sessions(vault, args),
        "handover": lambda: cmd_handover(vault, args),
        "handovers": lambda: cmd_handovers(vault, args),
        "install": lambda: cmd_install(args, vault),
        "uninstall": lambda: cmd_uninstall(args),
    }
    try:
        return handlers[args.command]()
    except VaultError as exc:
        print(fmt.paint("账号库错误：%s" % exc, fmt.RED))
        return EXIT_FAILED
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
