"""wbvault 单元测试：用假登录态文件验证核心逻辑，不碰真实文件。

运行：python3 -m unittest discover -s tests -v
"""

from __future__ import annotations

import json
import os
import stat
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import wbvault_lib.switch as sw  # noqa: E402
from wbvault_lib.authfile import (  # noqa: E402
    AuthFileError,
    atomic_write,
    list_backups,
    parse_state,
    read_state,
)
from wbvault_lib.store import Vault, VaultError  # noqa: E402


def make_auth(uid: str, uin: str, domain: str = "www.workbuddy.ai", token: str = "aaa") -> bytes:
    payload = {
        "account": {
            "uid": uid,
            "uin": uin,
            "nickname": {"$wbEncrypted": 1, "envelope": "x" * 20},
            "type": "personal",
            "lastLogin": True,
        },
        "auth": {
            "accessToken": {"$wbEncrypted": 1, "envelope": token * 10},
            "refreshToken": {"$wbEncrypted": 1, "envelope": "r" * 40},
            "tokenType": "Bearer",
            "domain": domain,
            "expiresAt": int(time.time() * 1000) + 86_400_000,
            "refreshExpiresAt": int(time.time() * 1000) + 30 * 86_400_000,
            "lastRefreshTime": int(time.time() * 1000),
        },
        "accounts": [{"uid": uid, "uin": uin}],
        "allAccounts": [{"uid": uid, "uin": uin}],
    }
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


UID_A = "aaaaaaaa-1111-2222-3333-444444444444"
UID_B = "bbbbbbbb-1111-2222-3333-555555555555"


class BaseCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp(prefix="wbvault-test-"))
        self.auth_dir = self.tmp / "auth"
        self.auth_dir.mkdir()
        self.auth_file = self.auth_dir / "workbuddy-desktop-ai.info"
        self.auth_file.write_bytes(make_auth(UID_A, "1111111111", token="a"))
        os.chmod(self.auth_file, 0o600)
        self.vault = Vault(self.tmp / "vault")
        self.vault.ensure()

    def tearDown(self) -> None:
        import shutil

        shutil.rmtree(self.tmp, ignore_errors=True)

    def add_b_snapshot(self) -> None:
        snap = self.vault.snapshot_path(UID_B)
        snap.write_bytes(make_auth(UID_B, "2222222222", token="b"))
        os.chmod(snap, 0o600)
        self.vault.snapshot(parse_state(snap), alias="B号")
        # snapshot() 会把 activeUid 设成 B，这里改回来模拟「A 正在用」
        data = self.vault.load()
        data["activeUid"] = UID_A
        self.vault.save()


class TestParse(BaseCase):
    def test_parse_fields(self) -> None:
        state = read_state(self.auth_file)
        self.assertEqual(state.uid, UID_A)
        self.assertEqual(state.uin, "1111111111")
        self.assertEqual(state.domain, "www.workbuddy.ai")
        self.assertFalse(state.token_expired())
        self.assertGreater(state.days_left(), 20)

    def test_reject_bad_json(self) -> None:
        self.auth_file.write_bytes(b"{not json")
        with self.assertRaises(AuthFileError):
            read_state(self.auth_file)

    def test_reject_missing_uid(self) -> None:
        self.auth_file.write_bytes(json.dumps({"account": {}, "auth": {}}).encode())
        with self.assertRaises(AuthFileError):
            read_state(self.auth_file)


class TestStore(BaseCase):
    def test_snapshot_and_resolve(self) -> None:
        entry = self.vault.snapshot(read_state(self.auth_file), alias="主号")
        self.assertEqual(entry["uid"], UID_A)
        self.assertTrue(self.vault.verify_snapshot(entry))
        self.assertEqual(self.vault.find("主号")["uid"], UID_A)
        self.assertEqual(self.vault.find(UID_A)["uid"], UID_A)
        self.assertEqual(self.vault.find("aaaa")["uid"], UID_A)
        self.assertEqual(self.vault.find("1111111111")["uid"], UID_A)
        self.assertIsNone(self.vault.find("cccc"))

    def test_snapshot_permissions(self) -> None:
        self.vault.snapshot(read_state(self.auth_file))
        mode = stat.S_IMODE(self.vault.snapshot_path(UID_A).stat().st_mode)
        self.assertEqual(mode, 0o600)

    def test_snapshot_sha_detects_tamper(self) -> None:
        entry = self.vault.snapshot(read_state(self.auth_file))
        self.vault.snapshot_path(UID_A).write_bytes(b"tampered")
        self.assertFalse(self.vault.verify_snapshot(entry))

    def test_remove(self) -> None:
        self.vault.snapshot(read_state(self.auth_file))
        self.vault.remove(UID_A)
        self.assertEqual(self.vault.entries(), [])
        self.assertFalse(self.vault.snapshot_path(UID_A).exists())

    def test_unknown_prefix_ambiguous(self) -> None:
        self.vault.snapshot(read_state(self.auth_file))
        twin = "aaaaaaaa-9999-8888-7777-666666666666"
        snap = self.vault.snapshot_path(twin)
        snap.write_bytes(make_auth(twin, "3"))
        self.vault.snapshot(parse_state(snap))
        with self.assertRaises(VaultError):
            self.vault.find("aaaa")


class TestSwitch(BaseCase):
    def test_switch_success(self) -> None:
        self.vault.snapshot(read_state(self.auth_file), alias="A号")
        self.add_b_snapshot()
        with mock.patch.object(sw, "app_is_running", return_value=True), mock.patch.object(
            sw, "probe_app_reload", return_value="reloaded"
        ):
            result = sw.switch(
                self.vault,
                self.auth_file,
                "B号",
                verify_seconds=0.5,
                settle_seconds=0.0,
                log=lambda _: None,
            )
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(read_state(self.auth_file).uid, UID_B)
        self.assertEqual(self.vault.active_uid(), UID_B)
        self.assertTrue(list_backups(self.vault.backups_dir))

    def test_pending_when_app_did_not_reload(self) -> None:
        """文件写对了、App 还活着但没热重载：必须报 pending，不能报成功。"""
        self.vault.snapshot(read_state(self.auth_file), alias="A号")
        self.add_b_snapshot()
        with mock.patch.object(sw, "app_is_running", return_value=True), mock.patch.object(
            sw, "probe_app_reload", return_value="not_reloaded"
        ):
            result = sw.switch(
                self.vault,
                self.auth_file,
                "B号",
                verify_seconds=0.5,
                settle_seconds=0.0,
                auto_restart=False,
                log=lambda _: None,
            )
        self.assertEqual(result.status, "pending", result.message)
        self.assertTrue(result.needs_restart)
        self.assertEqual(result.exit_code, 3)
        self.assertEqual(read_state(self.auth_file).uid, UID_B)

    def test_auto_restart_when_hot_reload_failed(self) -> None:
        """默认自动重启：热重载失败时自己恢复，用户不用动手。"""
        self.vault.snapshot(read_state(self.auth_file), alias="A号")
        self.add_b_snapshot()
        fake = sw.SwitchResult("ok", "已重启", sw.EXIT_OK, verified_by="restart")
        with mock.patch.object(sw, "app_is_running", return_value=True), mock.patch.object(
            sw, "probe_app_reload", return_value="not_reloaded"
        ), mock.patch.object(sw, "app_pid", return_value=999), mock.patch.object(
            sw, "running_inside_app", return_value=False
        ), mock.patch.object(sw, "restart_app", return_value=fake) as restarted:
            result = sw.switch(
                self.vault,
                self.auth_file,
                "B号",
                verify_seconds=0.5,
                settle_seconds=0.0,
                log=lambda _: None,
            )
        self.assertEqual(result.status, "ok", result.message)
        self.assertEqual(result.verified_by, "restart")
        restarted.assert_called_once()

    def test_no_auto_restart_inside_app(self) -> None:
        """从 App 内部调用时不能自杀式重启。"""
        self.vault.snapshot(read_state(self.auth_file), alias="A号")
        self.add_b_snapshot()
        with mock.patch.object(sw, "app_is_running", return_value=True), mock.patch.object(
            sw, "probe_app_reload", return_value="not_reloaded"
        ), mock.patch.object(sw, "app_pid", return_value=999), mock.patch.object(
            sw, "running_inside_app", return_value=True
        ), mock.patch.object(sw, "restart_app") as restarted:
            result = sw.switch(
                self.vault,
                self.auth_file,
                "B号",
                verify_seconds=0.5,
                settle_seconds=0.0,
                log=lambda _: None,
            )
        self.assertEqual(result.status, "pending")
        self.assertTrue(result.needs_restart)
        restarted.assert_not_called()

    def test_known_dead_skips_probe(self) -> None:
        """同一 App 进程上次就失效 → 不再白等，直接走恢复。"""
        self.vault.snapshot(read_state(self.auth_file), alias="A号")
        self.add_b_snapshot()
        self.vault.record_reload(999, False, UID_A)
        fake = sw.SwitchResult("ok", "已重启", sw.EXIT_OK, verified_by="restart")
        with mock.patch.object(sw, "app_is_running", return_value=True), mock.patch.object(
            sw, "app_pid", return_value=999
        ), mock.patch.object(sw, "running_inside_app", return_value=False), mock.patch.object(
            sw, "restart_app", return_value=fake
        ), mock.patch.object(sw, "probe_app_reload") as probe:
            result = sw.switch(
                self.vault,
                self.auth_file,
                "B号",
                verify_seconds=0.5,
                settle_seconds=0.0,
                log=lambda _: None,
            )
        probe.assert_not_called()
        self.assertEqual(result.status, "ok")

    def test_switch_noop(self) -> None:
        self.vault.snapshot(read_state(self.auth_file), alias="A号")
        result = sw.switch(
            self.vault, self.auth_file, "A号", verify_seconds=0.3, settle_seconds=0.0, log=lambda _: None
        )
        self.assertEqual(result.status, "noop")

    def test_switch_unknown_account(self) -> None:
        result = sw.switch(self.vault, self.auth_file, "不存在", log=lambda _: None)
        self.assertEqual(result.status, "refused")
        self.assertEqual(result.exit_code, 2)

    def test_refuse_domain_mismatch(self) -> None:
        snap = self.vault.snapshot_path(UID_B)
        snap.write_bytes(make_auth(UID_B, "2", domain="copilot.tencent.com"))
        self.vault.snapshot(parse_state(snap), alias="企业号")
        result = sw.switch(
            self.vault, self.auth_file, "企业号", verify_seconds=0.3, settle_seconds=0.0, log=lambda _: None
        )
        self.assertEqual(result.status, "refused")
        self.assertIn("环境不一致", result.message)
        self.assertEqual(read_state(self.auth_file).uid, UID_A)

    def test_rollback_when_reverted(self) -> None:
        self.vault.snapshot(read_state(self.auth_file), alias="A号")
        self.add_b_snapshot()
        original = read_state(self.auth_file)

        real_read = sw.read_state
        seen = {"count": 0}

        def fake_read(path):
            if path == self.auth_file:
                seen["count"] += 1
                if seen["count"] > 1:
                    return original
            return real_read(path)

        with mock.patch.object(sw, "read_state", side_effect=fake_read):
            result = sw.switch(
                self.vault,
                self.auth_file,
                "B号",
                verify_seconds=0.5,
                settle_seconds=0.0,
                log=lambda _: None,
            )
        self.assertEqual(result.status, "rolled_back", result.message)
        self.assertEqual(read_state(self.auth_file).uid, UID_A)
        self.assertEqual(result.exit_code, 1)

    def test_dry_run_does_not_write(self) -> None:
        self.vault.snapshot(read_state(self.auth_file), alias="A号")
        self.add_b_snapshot()
        before = self.auth_file.read_bytes()
        result = sw.switch(
            self.vault, self.auth_file, "B号", dry_run=True, log=lambda _: None
        )
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.auth_file.read_bytes(), before)

    def test_restore_latest(self) -> None:
        self.vault.snapshot(read_state(self.auth_file), alias="A号")
        self.add_b_snapshot()
        with mock.patch.object(sw, "app_is_running", return_value=True), mock.patch.object(
            sw, "probe_app_reload", return_value="reloaded"
        ):
            sw.switch(self.vault, self.auth_file, "B号", verify_seconds=0.5, settle_seconds=0.0, log=lambda _: None)
        self.assertEqual(read_state(self.auth_file).uid, UID_B)
        result = sw.restore_latest(self.vault, self.auth_file, log=lambda _: None)
        self.assertEqual(result.status, "ok")
        self.assertEqual(read_state(self.auth_file).uid, UID_A)


class TestEmail(BaseCase):
    def test_set_and_find_by_email(self) -> None:
        self.vault.snapshot(read_state(self.auth_file))
        self.vault.set_email("aaaa", "someone@gmail.com")
        self.assertEqual(self.vault.find("someone@gmail.com")["uid"], UID_A)
        self.assertEqual(self.vault.find("SOMEONE@gmail.com")["uid"], UID_A)

    def test_clear_email(self) -> None:
        self.vault.snapshot(read_state(self.auth_file))
        self.vault.set_email(UID_A, "someone@gmail.com")
        self.vault.set_email(UID_A, "")
        self.assertNotIn("email", self.vault.find(UID_A))


class TestProfile(BaseCase):
    def _write_profile(self, uid: str, nickname: str) -> None:
        path = self.tmp / ".workbuddy-ai" / "storage" / "skeleton"
        path.mkdir(parents=True, exist_ok=True)
        (path / "account-snapshot.json").write_text(
            json.dumps({"primary": {"uid": uid, "nickname": nickname}}), "utf-8"
        )

    def test_nickname_for_matching_uid(self) -> None:
        import wbvault_lib.profile as pf

        self._write_profile(UID_A, "someone@gmail.com")
        with mock.patch.object(pf.Path, "home", return_value=self.tmp):
            self.assertEqual(pf.nickname_for(UID_A), "someone@gmail.com")
            self.assertIsNone(pf.nickname_for(UID_B))

    def test_nickname_without_at_is_ignored(self) -> None:
        import wbvault_lib.profile as pf

        self._write_profile(UID_A, "阿黄")
        with mock.patch.object(pf.Path, "home", return_value=self.tmp):
            self.assertIsNone(pf.nickname_for(UID_A))

    def test_refresh_email_only_when_changed(self) -> None:
        import wbvault_lib.profile as pf

        self.vault.snapshot(read_state(self.auth_file))
        self._write_profile(UID_A, "someone@gmail.com")
        with mock.patch.object(pf.Path, "home", return_value=self.tmp):
            self.assertEqual(self.vault.refresh_email(UID_A), "someone@gmail.com")
            self.assertIsNone(self.vault.refresh_email(UID_A))
        self.assertEqual(self.vault.find(UID_A)["email"], "someone@gmail.com")


class TestAtomicWrite(BaseCase):
    def test_permissions_and_content(self) -> None:
        data = make_auth(UID_B, "2")
        atomic_write(self.auth_file, data)
        self.assertEqual(self.auth_file.read_bytes(), data)
        self.assertEqual(stat.S_IMODE(self.auth_file.stat().st_mode), 0o600)

    def test_no_temp_left(self) -> None:
        atomic_write(self.auth_file, make_auth(UID_B, "2"))
        leftovers = [p.name for p in self.auth_dir.iterdir() if p.name.startswith(".wbvault.")]
        self.assertEqual(leftovers, [])


class TestHandover(BaseCase):
    def _make_db(self) -> Path:
        import sqlite3

        db = self.tmp / "workbuddy.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "create table sessions (id text primary key, user_id text, title text, cwd text, "
            "transport text default 'local', status text, deleted_at integer, "
            "last_activity_at integer, created_at integer)"
        )
        conn.execute(
            "insert into sessions (id,user_id,title,transport,status,created_at) "
            "values ('sess-0001', ?, '测试会话', 'local', 'completed', 1)",
            (UID_A,),
        )
        conn.commit()
        conn.close()
        return db

    def test_handover_and_undo(self) -> None:
        import sqlite3

        import wbvault_lib.handover as ho

        db = self._make_db()
        result = ho.handover(self.vault, db, "sess", UID_B)
        self.assertEqual(result.status, "ok", result.message)
        conn = sqlite3.connect(str(db))
        self.assertEqual(conn.execute("select user_id from sessions").fetchone()[0], UID_B)
        conn.close()

        undone = ho.undo(self.vault, db)
        self.assertEqual(undone.status, "ok")
        conn = sqlite3.connect(str(db))
        self.assertEqual(conn.execute("select user_id from sessions").fetchone()[0], UID_A)
        conn.close()

    def test_refuse_same_account(self) -> None:
        import wbvault_lib.handover as ho

        db = self._make_db()
        result = ho.handover(self.vault, db, "sess", UID_A)
        self.assertEqual(result.status, "refused")

    def test_refuse_cloud_session(self) -> None:
        import sqlite3

        import wbvault_lib.handover as ho

        db = self._make_db()
        conn = sqlite3.connect(str(db))
        conn.execute("update sessions set transport='cloud'")
        conn.commit()
        conn.close()
        result = ho.handover(self.vault, db, "sess", UID_B)
        self.assertEqual(result.status, "refused")
        self.assertIn("云端会话", result.message)

    def test_dry_run_does_not_write(self) -> None:
        import sqlite3

        import wbvault_lib.handover as ho

        db = self._make_db()
        result = ho.handover(self.vault, db, "sess", UID_B, dry_run=True)
        self.assertEqual(result.status, "ok")
        conn = sqlite3.connect(str(db))
        self.assertEqual(conn.execute("select user_id from sessions").fetchone()[0], UID_A)
        conn.close()

    def test_current_session_id_strips_query(self) -> None:
        """startup-route 里是 /conversation/<id>?startIntentToken=6，问号后不能算进 id。"""
        import wbvault_lib.handover as ho

        route_dir = self.tmp / ".workbuddy-ai" / UID_A
        route_dir.mkdir(parents=True)
        (route_dir / "startup-route.json").write_text(
            json.dumps({"path": "/conversation/abc-123?startIntentToken=6"}), "utf-8"
        )
        with mock.patch.object(ho.Path, "home", return_value=self.tmp):
            self.assertEqual(ho.current_session_id(UID_A), "abc-123")

    def test_resolve_session_accepts_route_fragment(self) -> None:
        import sqlite3

        import wbvault_lib.handover as ho

        db = self._make_db()
        conn = sqlite3.connect(str(db))
        conn.row_factory = sqlite3.Row
        row = ho.resolve_session(conn, "conversation/sess-0001?x=1", UID_A)
        self.assertEqual(row["id"], "sess-0001")
        conn.close()

    def test_handover_bumps_last_activity(self) -> None:
        import sqlite3

        import wbvault_lib.handover as ho

        db = self._make_db()
        started = int(time.time() * 1000)
        result = ho.handover(self.vault, db, "sess", UID_B)
        self.assertEqual(result.status, "ok", result.message)
        conn = sqlite3.connect(str(db))
        stamp = conn.execute("select last_activity_at from sessions").fetchone()[0]
        conn.close()
        self.assertGreaterEqual(stamp, started)

    def test_missing_session_returns_error_not_traceback(self) -> None:
        import wbvault_lib.handover as ho

        db = self._make_db()
        result = ho.handover(self.vault, db, "不存在的会话", UID_B)
        self.assertEqual(result.status, "error")
        self.assertIn("找不到会话", result.message)


class TestIndexConsistency(BaseCase):
    """索引是派生数据：常驻 watch 进程不能拿旧缓存覆盖别人的更新。"""

    def _refresh_snapshot_file(self, uid: str, raw: bytes) -> None:
        """模拟另一个进程写入了新快照（文件 + meta 都新，索引还没跟上）。"""
        import hashlib

        self.vault.snapshot_path(uid).write_bytes(raw)
        meta = self.vault.read_meta(uid)
        meta["sha256"] = hashlib.sha256(raw).hexdigest()
        meta["size"] = len(raw)
        self.vault.meta_path(uid).write_text(json.dumps(meta), "utf-8")

    def test_stale_index_is_repaired_not_rejected(self) -> None:
        import hashlib

        entry = self.vault.snapshot(read_state(self.auth_file))
        new_raw = make_auth(UID_A, "1111111111", token="refreshed")
        self._refresh_snapshot_file(UID_A, new_raw)
        # 索引里还是旧 sha —— 这就是报「sha256 不一致」的那种状态
        self.assertNotEqual(entry["sha256"], hashlib.sha256(new_raw).hexdigest())

        self.assertTrue(self.vault.verify_snapshot(self.vault.find(UID_A)))
        self.assertEqual(
            self.vault.find(UID_A)["sha256"], hashlib.sha256(new_raw).hexdigest()
        )

    def test_real_corruption_still_rejected(self) -> None:
        self.vault.snapshot(read_state(self.auth_file))
        self.vault.snapshot_path(UID_A).write_bytes(b"tampered")
        self.assertFalse(self.vault.verify_snapshot(self.vault.find(UID_A)))

    def test_long_lived_instance_keeps_other_writes(self) -> None:
        """常驻进程先加载索引、之后别的进程加了账号：再保存时不能把它抹掉。"""
        daemon = Vault(self.vault.root)
        daemon.snapshot(read_state(self.auth_file))

        other = Vault(self.vault.root)
        snap_b = other.snapshot_path(UID_B)
        snap_b.write_bytes(make_auth(UID_B, "2222222222", token="b"))
        other.snapshot(parse_state(snap_b))

        refreshed = make_auth(UID_A, "1111111111", token="a2")
        self.auth_file.write_bytes(refreshed)
        daemon.snapshot(read_state(self.auth_file))

        uids = {e["uid"] for e in Vault(self.vault.root).entries()}
        self.assertEqual(uids, {UID_A, UID_B})
        fresh = Vault(self.vault.root)
        self.assertTrue(fresh.verify_snapshot(fresh.find(UID_A)))
        self.assertTrue(fresh.verify_snapshot(fresh.find(UID_B)))

    def test_resolve_target_refuses_corrupt_snapshot(self) -> None:
        self.vault.snapshot(read_state(self.auth_file))
        self.vault.snapshot_path(UID_A).write_bytes(b"tampered")
        entry, target, problem = sw.resolve_target(self.vault, self.auth_file, UID_A)
        self.assertIsNone(entry)
        self.assertIsNone(target)
        self.assertIsNotNone(problem)
        self.assertEqual(problem.status, "refused")
        self.assertEqual(problem.exit_code, sw.EXIT_FAILED)


class TestSwitchWithCurrent(BaseCase):
    """过继会话必须排在目标校验之后：切不过去就别动会话归属。"""

    def _make_db(self) -> Path:
        import sqlite3

        db = self.tmp / "workbuddy.db"
        conn = sqlite3.connect(str(db))
        conn.execute(
            "create table sessions (id text primary key, user_id text, title text, cwd text, "
            "transport text default 'local', status text, deleted_at integer, "
            "last_activity_at integer, created_at integer)"
        )
        conn.execute(
            "insert into sessions (id,user_id,title,transport,status,created_at) "
            "values ('sess-0001', ?, '开始新对话', 'local', 'completed', 1)",
            (UID_A,),
        )
        conn.commit()
        conn.close()
        return db

    def test_broken_target_does_not_handover_session(self) -> None:
        import sqlite3

        import wbvault

        self.vault.snapshot(read_state(self.auth_file), alias="A号")
        self.add_b_snapshot()
        self.vault.snapshot_path(UID_B).write_bytes(b"tampered")
        db = self._make_db()

        code = wbvault.main(
            [
                "--vault",
                str(self.vault.root),
                "--auth-file",
                str(self.auth_file),
                "switch",
                UID_B,
                "--with-current",
                "--db",
                str(db),
                "--wait",
                "0.2",
            ]
        )
        self.assertEqual(code, 1)
        conn = sqlite3.connect(str(db))
        owner = conn.execute("select user_id from sessions").fetchone()[0]
        conn.close()
        self.assertEqual(owner, UID_A, "会话不该被过继到切不过去的账号")
        self.assertEqual(read_state(self.auth_file).uid, UID_A)


if __name__ == "__main__":
    unittest.main(verbosity=2)
