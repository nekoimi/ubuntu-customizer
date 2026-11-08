"""Release integrity, proxy preset compatibility and build isolation checks."""
from contextlib import closing
import importlib.util
import io
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]


def module(name, path):
    spec = importlib.util.spec_from_file_location(name, ROOT / path)
    result = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(result)
    return result


builder = module("v2raya_builder", "scripts/build.py")
guest = module("v2raya_guest", "scripts/check-v2raya.py")


class ReleaseDownload(unittest.TestCase):
    def release(self):
        return {"tag_name": "v2.5.13", "draft": False, "prerelease": False, "assets": [{
            "name": "installer_debian_x64_2.5.13.deb", "digest": "sha256:" + "a" * 64,
            "browser_download_url": "https://github.com/v2rayA/v2rayA/releases/download/v2.5.13/installer_debian_x64_2.5.13.deb"}]}

    def test_latest_stable_asset_is_verified_and_recorded(self):
        build = builder.Build.__new__(builder.Build)
        build.details = {}
        build.note = Mock()
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(builder.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(self.release()).encode())), \
                patch.object(builder, "download") as download:
            destination = Path(folder)
            build.v2raya(destination)
            download.assert_called_once_with(self.release()["assets"][0]["browser_download_url"],
                                             destination / "v2raya.deb", "a" * 64)
            self.assertEqual(json.loads((destination / "v2raya-release.json").read_text()), build.details["v2raya"])
            self.assertEqual(build.details["v2raya"]["version"], "2.5.13")

    def test_unexpected_release_or_unverified_asset_is_rejected(self):
        for change in ("prerelease", "draft", "tag", "missing", "digest", "url"):
            release = self.release()
            if change in ("prerelease", "draft"):
                release[change] = True
            elif change == "tag":
                release["tag_name"] = "../../invalid"
            elif change == "missing":
                release["assets"] = []
            else:
                release["assets"][0]["digest" if change == "digest" else "browser_download_url"] = "invalid"
            build = builder.Build.__new__(builder.Build)
            build.details = {}
            build.note = Mock()
            with self.subTest(change=change), \
                    patch.object(builder.urllib.request, "urlopen", return_value=io.BytesIO(json.dumps(release).encode())), \
                    patch.object(builder, "download") as download, self.assertRaises(RuntimeError):
                build.v2raya(Path("not-created"))
            download.assert_not_called()

    def test_corrupt_package_is_not_published(self):
        with tempfile.TemporaryDirectory() as folder, \
                patch.object(builder.urllib.request, "urlopen", return_value=io.BytesIO(b"corrupted package")):
            target = Path(folder) / "v2raya.deb"
            with self.assertRaisesRegex(RuntimeError, "SHA256 mismatch"):
                builder.download("https://example.test/package.deb", target, "a" * 64)
            self.assertFalse(target.exists())
            self.assertFalse(target.with_suffix(".deb.partial").exists())


class GuestProxyPreset(unittest.TestCase):
    def api(self, *, accounts=False, running=False, wrong_port=False):
        api = Mock()
        state = {"setting": {"logLevel": "info", "futureField": "keep"}, "ports": {}}

        def request(path, data=None, *, method=None, token=None):
            if path == "account":
                if data and not 6 <= len(data["password"]) <= 32:
                    raise RuntimeError("length of password should be between 6 and 32")
                return {"token": "temporary-token"} if data else {"hasAnyAccounts": accounts}
            if path in ("ports", "setting"):
                if method == "PUT":
                    self.assertEqual(token, "temporary-token")
                    state[path] = dict(data)
                result = dict(state[path])
                if wrong_port and path == "ports":
                    result["http"] = 12345
                return {"setting": result} if path == "setting" else result
            if path == "touch":
                return {"running": running}
            raise AssertionError("Unexpected API call: " + path)

        api.request.side_effect = request
        return api, state

    def test_api_presets_preserve_other_fields_and_never_import_or_connect_nodes(self):
        api, state = self.api()
        guest.configure(api)
        self.assertEqual(state["ports"]["http"], 20171)
        self.assertEqual(state["setting"]["futureField"], "keep")
        self.assertEqual(state["setting"]["transparent"], "close")
        self.assertEqual(state["setting"]["dnsMode"], "off")
        self.assertTrue(state["setting"]["ipforward"])
        self.assertFalse(state["setting"]["portSharing"])
        api.check_web.assert_called_once()

    def test_temporary_password_meets_api_limit_and_is_fresh_for_each_build(self):
        passwords = []
        for _ in range(2):
            api, _ = self.api()
            guest.configure(api)
            account_call = next(call for call in api.request.call_args_list
                                if call.args[0] == "account" and len(call.args) > 1)
            password = account_call.args[1]["password"]
            self.assertGreaterEqual(len(password), 6)
            self.assertLessEqual(len(password), 32)
            passwords.append(password)
        self.assertNotEqual(passwords[0], passwords[1])

    def test_existing_account_wrong_port_or_running_core_refuses_build(self):
        for options in ({"accounts": True}, {"wrong_port": True}, {"running": True}):
            with self.subTest(options=options), self.assertRaises(RuntimeError):
                guest.configure(self.api(**options)[0])

    def test_shared_namespace_refuses_before_processes_or_file_changes(self):
        for original in ("", "net:[100]"):
            with self.subTest(original=original), \
                    patch.dict(os.environ, {"UBUNTU_CUSTOMIZER_ORIGINAL_NETNS": original}, clear=True), \
                    patch.object(guest.os, "readlink", return_value="net:[100]"), \
                    patch.object(guest, "check_presets") as presets, \
                    patch.object(guest.sp, "Popen") as start, patch.object(guest.sp, "run") as command:
                with self.assertRaisesRegex(RuntimeError, "Private network namespace"):
                    guest.main()
                presets.assert_not_called()
                start.assert_not_called()
                command.assert_not_called()

    def test_api_failure_stops_instance_and_resets_temporary_account(self):
        process = Mock(pid=1234)
        process.poll.return_value = None
        with patch.object(guest, "check_namespace"), patch.object(guest, "check_presets"), \
                patch.object(guest.Path, "iterdir", return_value=iter([])), \
                patch.object(guest.sp, "Popen", return_value=process) as start, \
                patch.object(guest.sp, "run") as command, patch.object(guest, "API"), \
                patch.object(guest, "configure", side_effect=RuntimeError("API failed")), \
                patch.object(guest.os, "killpg", create=True) as kill, \
                patch.dict(os.environ, {"HTTP_PROXY": "http://build-only.test:1234"}):
            with self.assertRaisesRegex(RuntimeError, "API failed"):
                guest.main()
        kill.assert_called_once_with(1234, guest.signal.SIGTERM)
        process.wait.assert_called_once_with(timeout=15)
        self.assertEqual(command.call_args.args[0], ["/usr/bin/v2raya", "--reset-password"])
        self.assertNotIn("HTTP_PROXY", start.call_args.kwargs["env"])
        self.assertNotIn("HTTP_PROXY", command.call_args.kwargs["env"])

    def test_persistent_database_requires_presets_and_no_accounts_or_nodes(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "v2raya.db"
            with closing(sqlite3.connect(path)) as db, db:
                for table in ("accounts", "servers", "subscriptions"):
                    db.execute(f"CREATE TABLE {table} (id INTEGER)")
                db.execute("CREATE TABLE system_config (key TEXT, value TEXT)")
                for key, value in (("ports", guest.PORTS), ("setting", guest.SETTINGS)):
                    db.execute("INSERT INTO system_config VALUES (?, ?)", ("system:" + key, json.dumps(value)))
            guest.check_database(path)
            for table in ("accounts", "servers", "subscriptions"):
                with self.subTest(table=table), closing(sqlite3.connect(path)) as db, db:
                    db.execute(f"INSERT INTO {table} VALUES (1)")
                    db.commit()
                    with self.assertRaisesRegex(RuntimeError, "credentials or nodes"):
                        guest.check_database(path)
                    db.execute(f"DELETE FROM {table}")
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("INSERT INTO system_config VALUES ('accounts:build-check', '\"password-hash\"')")
            with self.assertRaisesRegex(RuntimeError, "credentials in system_config"):
                guest.check_database(path)
            with closing(sqlite3.connect(path)) as db, db:
                db.execute("DELETE FROM system_config WHERE key='accounts:build-check'")
                db.execute("UPDATE system_config SET value='{}' WHERE key='system:ports'")
            with self.assertRaisesRegex(RuntimeError, "persistent preset"):
                guest.check_database(path)


if __name__ == "__main__":
    unittest.main()
