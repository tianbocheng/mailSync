import base64
import io
import json
import tempfile
import unittest
from datetime import datetime, timezone
from unittest import mock
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from tests import helpers  # noqa: F401

import mail_sync
from imap.protocol import FolderInfo
from storage.config_repository import (
    create_synology_label,
    delete_mailbox_account,
    delete_sync_rule,
    delete_synology_label,
    list_synology_labels,
    save_mailbox_account,
    save_mailbox_probe_results,
    save_proxy_profile,
    save_target_account,
    save_sync_rule,
    set_sync_rule_enabled,
    sync_rule_logs,
    sync_rules_detail,
    target_mailbox_detail,
    update_synology_label,
)
from storage.config_db import import_legacy_config, load_runtime_config
from storage.rule_status import mark_sync_rule_failure, mark_sync_rule_success
from storage.users import authenticate_user
from web.app import make_handler
from web.session import current_username, make_session_cookie


BACKEND_DIR = Path(__file__).resolve().parents[1]
REPO_DIR = BACKEND_DIR.parent
FRONTEND_SRC = REPO_DIR / "frontend/src"
FRONTEND_VIEWS = FRONTEND_SRC / "views"
FRONTEND_COMPONENTS = FRONTEND_SRC / "components"
FRONTEND_API = FRONTEND_SRC / "api"


def jwt_with_claims(claims: dict) -> str:
    def encode(value: dict) -> str:
        return base64.urlsafe_b64encode(
            json.dumps(value).encode("utf-8")
        ).decode("ascii").rstrip("=")

    return f"{encode({'alg': 'none'})}.{encode(claims)}."


class FakeSocket:
    def __init__(self, request: bytes):
        self.input = io.BytesIO(request)
        self.output = io.BytesIO()

    def makefile(self, mode, *args, **kwargs):
        if "r" in mode:
            return self.input
        return self.output

    def sendall(self, data: bytes):
        self.output.write(data)

    def close(self):
        return None


def parse_response(raw: bytes):
    head, body = raw.split(b"\r\n\r\n", 1)
    lines = head.decode("iso-8859-1").split("\r\n")
    status = int(lines[0].split()[1])
    headers = {}
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        headers[key.strip()] = value.strip()
    return status, headers, body


class WebConsoleTests(unittest.TestCase):
    def valid_config(self):
        return {
            "state_db": "data/state.sqlite3",
            "target": {
                "email": "target@example.com",
                "password": "secret",
                "imap_server": "target.example.com",
                "folder": "INBOX",
            },
            "sources": [
                {
                    "email": "source@example.com",
                    "password": "secret",
                    "imap_server": "source.example.com",
                    "account_label": "SYNC:source@example.com",
                    "folder_labels": {"INBOX": "BOX:收件箱"},
                    "default_folder_label": "BOX:其他文件夹",
                    "deleted_label": "BOX:已删除邮件",
                }
            ],
            "synology_labels": {
                "$account": "SYNC:source@example.com",
                "$inbox": "BOX:收件箱",
                "$other": "BOX:其他文件夹",
                "$deleted": "BOX:已删除邮件",
            },
        }

    def test_mailbox_pop_connection_test_uses_pop_and_proxy(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            self.addCleanup(db.close)
            proxy_id = save_proxy_profile(
                db,
                proxy_id="",
                name="Local proxy",
                proxy_type="http_connect",
                host="127.0.0.1",
                port="7890",
                username="proxy-user",
                password="proxy-pass",
            )
            fields = {
                "mailbox_protocol_tab": ["pop"],
                "original_email": ["source@example.com"],
                "email": [""],
                "password": ["secret"],
                "service_address": ["pop.example.com"],
                "pop_server": ["pop.example.com"],
                "pop_port": ["995"],
                "pop_use_ssl": ["1"],
                "proxy_profile_id": [str(proxy_id)],
            }
            handler = object.__new__(make_handler(state_path))
            client = mock.Mock()
            client.list.return_value = ("+OK", [b"1 100", b"2 200"], 2)

            with (
                mock.patch("web.app.connect_pop", return_value=client) as connect_pop,
                mock.patch("web.app.safe_pop_quit") as safe_pop_quit,
            ):
                result = handler._test_mailbox_connection(
                    db,
                    "mailbox",
                    "",
                    "",
                    fields,
                )

            self.assertTrue(result["ok"])
            self.assertEqual(result["message_count"], 2)
            profile = connect_pop.call_args.args[0]
            self.assertEqual(profile["_test_protocol"], "pop")
            self.assertEqual(profile["email"], "source@example.com")
            self.assertEqual(profile["pop_fallback"]["server"], "pop.example.com")
            self.assertEqual(profile["imap_proxy"]["host"], "127.0.0.1")
            self.assertEqual(profile["imap_proxy"]["port"], 7890)
            self.assertEqual(profile["imap_proxy"]["username"], "proxy-user")
            safe_pop_quit.assert_called_once_with(client)

    def test_saving_existing_pop_account_can_reuse_original_email(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            self.addCleanup(db.close)
            save_mailbox_account(
                db,
                original_role="",
                original_email="",
                role="mailbox",
                email="source@example.com",
                enabled=True,
                auth_provider="manual",
                imap_server="imap.example.com",
                imap_port="993",
                use_ssl=True,
                password="secret",
                oauth_client_id="",
                oauth_token_file="",
                target_folder="INBOX",
                imap_timeout="",
                proxy_profile_id="",
                pop_fallback_enabled=False,
                pop_server="",
                pop_port="",
                pop_use_ssl=True,
                pop_timeout="",
                service_address="imap.example.com",
            )

            saved_email = save_mailbox_account(
                db,
                original_role="mailbox",
                original_email="source@example.com",
                role="mailbox",
                email="",
                enabled=True,
                auth_provider="manual",
                imap_server="",
                imap_port="",
                use_ssl=False,
                password="",
                oauth_client_id="",
                oauth_token_file="",
                target_folder="INBOX",
                imap_timeout="",
                proxy_profile_id="",
                pop_fallback_enabled=True,
                pop_server="pop.example.com",
                pop_port="995",
                pop_use_ssl=True,
                pop_timeout="",
                original_service_address="imap.example.com",
                service_address="imap.example.com",
                imap_enabled=False,
            )

            detail = target_mailbox_detail(
                db,
                "source@example.com",
                selected_service_address="imap.example.com",
            )
            self.assertEqual(saved_email, "source@example.com")
            self.assertEqual(detail["mailbox"]["email"], "source@example.com")
            self.assertEqual(detail["mailbox"]["service_address"], "imap.example.com")
            self.assertEqual(detail["mailbox"]["imap_server"], "imap.example.com")
            self.assertFalse(detail["mailbox"]["imap_enabled"])
            self.assertTrue(detail["mailbox"]["pop_enabled"])
            self.assertEqual(detail["mailbox"]["pop_server"], "pop.example.com")

    def test_blank_pop_server_does_not_enable_pop_or_disable_imap(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            self.addCleanup(db.close)
            save_mailbox_account(
                db,
                original_role="",
                original_email="",
                role="mailbox",
                email="source@example.com",
                enabled=True,
                auth_provider="manual",
                imap_server="imap.example.com",
                imap_port="993",
                use_ssl=True,
                password="secret",
                oauth_client_id="",
                oauth_token_file="",
                target_folder="INBOX",
                imap_timeout="",
                proxy_profile_id="",
                pop_fallback_enabled=False,
                pop_server="",
                pop_port="",
                pop_use_ssl=True,
                pop_timeout="",
                service_address="imap.example.com",
            )

            saved_email = save_mailbox_account(
                db,
                original_role="mailbox",
                original_email="source@example.com",
                role="mailbox",
                email="",
                enabled=True,
                auth_provider="manual",
                imap_server="",
                imap_port="",
                use_ssl=False,
                password="",
                oauth_client_id="",
                oauth_token_file="",
                target_folder="INBOX",
                imap_timeout="",
                proxy_profile_id="",
                pop_fallback_enabled=True,
                pop_server="",
                pop_port="995",
                pop_use_ssl=True,
                pop_timeout="",
                original_service_address="imap.example.com",
                service_address="imap.example.com",
                imap_enabled=False,
            )

            detail = target_mailbox_detail(
                db,
                "source@example.com",
                selected_service_address="imap.example.com",
            )
            self.assertEqual(saved_email, "source@example.com")
            self.assertTrue(detail["mailbox"]["imap_enabled"])
            self.assertFalse(detail["mailbox"]["pop_enabled"])
            self.assertEqual(detail["mailbox"]["imap_server"], "imap.example.com")
            self.assertEqual(detail["mailbox"]["pop_server"], "")

    def test_static_shell_session_and_api_pages(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            html_dir = Path(directory) / "html"
            assets_dir = html_dir / "assets"
            assets_dir.mkdir(parents=True)
            (html_dir / "index.html").write_text(
                '<!doctype html><html><body><div id="app"></div>'
                '<script type="module" src="/assets/index.js"></script>'
                "</body></html>",
                encoding="utf-8",
            )
            (assets_dir / "index.js").write_text(
                "window.__MAIL_SYNC_APP__ = true;",
                encoding="utf-8",
            )
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                self.assertIsNone(authenticate_user(db, "admin", "wrong"))
                user = authenticate_user(
                    db,
                    "admin",
                    "TEST_ADMIN_PASSWORD",
                )
                self.assertIsNotNone(user)

                cookie = make_session_cookie(db, "admin")
                self.assertEqual(
                    current_username(db, cookie.split(";", 1)[0]),
                    "admin",
                )
            finally:
                db.close()

            def request(
                path,
                *,
                cookie_header="",
                method="GET",
                form=None,
                headers=None,
            ):
                handler = make_handler(state_path)
                body = json.dumps(form or {}).encode("utf-8")
                raw_headers = [
                    f"{method} {path} HTTP/1.1",
                    "Host: localhost",
                ]
                if cookie_header:
                    raw_headers.append(f"Cookie: {cookie_header}")
                for key, value in (headers or {}).items():
                    raw_headers.append(f"{key}: {value}")
                if form is not None:
                    raw_headers.append("Content-Type: application/json")
                    raw_headers.append(f"Content-Length: {len(body)}")
                raw = ("\r\n".join(raw_headers) + "\r\n\r\n").encode("utf-8") + body
                socket = FakeSocket(raw)
                handler(socket, ("127.0.0.1", 0), object())
                return parse_response(socket.output.getvalue())

            html_patch = mock.patch("web.app.HTML_DIR", html_dir)
            html_patch.start()
            self.addCleanup(html_patch.stop)

            status, _, body = request("/login")
            login_shell = body.decode("utf-8")
            self.assertEqual(status, 200)
            self.assertIn('/assets/index.js', login_shell)
            self.assertIn('id="app"', login_shell)
            self.assertNotIn("source@example.com", login_shell)

            status, _, body = request("/api/session")
            self.assertEqual(status, 200)
            self.assertFalse(json.loads(body)["authenticated"])

            ajax_headers = {
                "Accept": "application/json",
                "X-Requested-With": "XMLHttpRequest",
            }
            status, headers, body = request(
                "/api/login",
                method="POST",
                headers=ajax_headers,
                form={
                    "username": "admin",
                    "password": "wrong",
                },
            )
            self.assertEqual(status, 401)
            self.assertEqual(json.loads(body)["error"], "用户名或密码不正确。")
            self.assertNotIn("Set-Cookie", headers)

            status, headers, body = request(
                "/api/login",
                method="POST",
                headers=ajax_headers,
                form={
                    "username": "admin",
                    "password": "TEST_ADMIN_PASSWORD",
                },
            )
            self.assertEqual(status, 200)
            login_data = json.loads(body)
            self.assertTrue(login_data["ok"])
            self.assertEqual(login_data["username"], "admin")
            self.assertEqual(login_data["redirect_url"], "/#/rules")
            self.assertIn("mailsync_session=", headers["Set-Cookie"])
            self.assertNotIn("MailSync2026", headers["Set-Cookie"])

            status, headers, _ = request("/logout", cookie_header=headers["Set-Cookie"])
            self.assertEqual(status, 303)
            self.assertEqual(headers["Location"], "/#/login")

            cookie_header = cookie.split(";", 1)[0]
            status, _, body = request("/api/rules", cookie_header=cookie_header)
            rules_data = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(
                rules_data["detail"]["selected_email"],
                "source@example.com",
            )
            self.assertIn("target@example.com", body.decode("utf-8"))

            status, _, body = request("/api/mail", cookie_header=cookie_header)
            mail_data = json.loads(body)
            self.assertEqual(status, 200)
            self.assertEqual(
                mail_data["detail"]["mailboxes"][0]["email"],
                "target@example.com",
            )

            status, _, body = request("/api/config", cookie_header=cookie_header)
            self.assertEqual(status, 200)
            config_data = json.loads(body)
            self.assertIn("proxies", config_data)
            self.assertIn("outlook_login", config_data)
            self.assertNotIn("token_dir", config_data["outlook_login"])

            status, headers, body = request(
                "/api/config/save",
                cookie_header=cookie_header,
                method="POST",
                form={
                    "client_id": "client-id",
                    "proxy_count": "1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body)["redirect_url"],
                "/#/config?notice=config_saved",
            )

            status, _, body = request("/api/config", cookie_header=cookie_header)
            self.assertEqual(status, 200)
            config_data = json.loads(body)
            self.assertEqual(config_data["outlook_login"]["client_id"], "client-id")
            self.assertNotIn("token_dir", config_data["outlook_login"])

            status, headers, body = request(
                "/api/config/save",
                cookie_header=cookie_header,
                method="POST",
                form={
                    "client_id": "client-id",
                    "proxy_count": "1",
                    "proxy_name_0": "本地代理",
                    "proxy_type_0": "socks5",
                    "proxy_host_0": "127.0.0.1",
                    "proxy_port_0": "1080",
                    "proxy_username_0": "proxy-user",
                    "proxy_password_0": "proxy-pass",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body)["redirect_url"],
                "/#/config?notice=config_saved",
            )

            status, _, body = request("/api/config", cookie_header=cookie_header)
            self.assertEqual(status, 200)
            config_data = json.loads(body)
            proxy = config_data["proxies"][0]
            self.assertEqual(proxy["name"], "本地代理")
            self.assertEqual(proxy["type"], "socks5")
            self.assertTrue(proxy["has_password"])
            self.assertEqual(proxy["account_count"], 0)

            status, headers, body = request(
                "/api/config/save",
                cookie_header=cookie_header,
                method="POST",
                form={
                    "client_id": "client-id",
                    "proxy_count": "2",
                    "proxy_id_0": str(proxy["id"]),
                    "proxy_name_0": "本地代理 2",
                    "proxy_type_0": "http_connect",
                    "proxy_host_0": "127.0.0.1",
                    "proxy_port_0": "7890",
                    "proxy_username_0": "proxy-user",
                    "proxy_password_0": "",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body)["redirect_url"],
                "/#/config?notice=config_saved",
            )

            status, _, body = request("/api/config", cookie_header=cookie_header)
            self.assertEqual(status, 200)
            config_data = json.loads(body)
            proxy = config_data["proxies"][0]
            self.assertEqual(proxy["name"], "本地代理 2")
            self.assertEqual(proxy["type"], "http_connect")
            self.assertEqual(proxy["port"], 7890)
            self.assertTrue(proxy["has_password"])

            status, headers, body = request(
                "/api/config/save",
                cookie_header=cookie_header,
                method="POST",
                form={
                    "client_id": "client-id",
                    "proxy_count": "1",
                    "proxy_id_0": str(proxy["id"]),
                    "proxy_delete_0": "1",
                },
            )
            self.assertEqual(status, 200)
            self.assertEqual(
                json.loads(body)["redirect_url"],
                "/#/config?notice=config_saved",
            )

            status, _, body = request("/api/config", cookie_header=cookie_header)
            self.assertEqual(status, 200)
            config_data = json.loads(body)
            self.assertEqual(config_data["proxies"], [])

            status, _, body = request("/api/mail", cookie_header=cookie_header)
            self.assertEqual(status, 200)
            mail_data = json.loads(body)
            self.assertEqual(
                mail_data["detail"]["outlook_defaults"]["client_id"],
                "client-id",
            )

            status, _, body = request("/rules", cookie_header=cookie_header)
            self.assertEqual(status, 200)
            self.assertIn('/assets/index.js', body.decode("utf-8"))

            status, _, body = request(
                "/assets/index.js",
                cookie_header=cookie_header,
            )
            self.assertEqual(status, 200)
            self.assertIn("__MAIL_SYNC_APP__", body.decode("utf-8"))

            app_source = (FRONTEND_SRC / "App.vue").read_text(
                encoding="utf-8"
            )
            config_view = (FRONTEND_VIEWS / "ConfigView.vue").read_text(
                encoding="utf-8"
            )
            rule_editor = (FRONTEND_COMPONENTS / "RuleEditor.vue").read_text(
                encoding="utf-8"
            )
            protocol_fields = (FRONTEND_COMPONENTS / "ProtocolFields.vue").read_text(
                encoding="utf-8"
            )
            mailbox_form = (FRONTEND_COMPONENTS / "MailboxForm.vue").read_text(
                encoding="utf-8"
            )
            mail_view = (FRONTEND_VIEWS / "MailView.vue").read_text(
                encoding="utf-8"
            )
            self.assertIn("<el-container", app_source)
            self.assertIn("<ConfigView", app_source)
            self.assertIn("代理配置", config_view)
            self.assertIn("el-tabs", config_view)
            self.assertIn('label="Direct"', protocol_fields)
            self.assertIn(":empty-values=\"emptyProxyValues\"", protocol_fields)
            self.assertIn("String(item.proxy_profile_id)", mailbox_form)
            self.assertIn("const imapEnabled = Boolean(imapServer)", mailbox_form)
            self.assertIn("const popEnabled = Boolean(popServer)", mailbox_form)
            self.assertNotIn('form.protocol === "imap"', mailbox_form)
            self.assertNotIn('form.protocol === "pop"', mailbox_form)
            self.assertNotIn("account.value.pop_server ?", mailbox_form)
            self.assertIn("function popServerModel", mailbox_form)
            self.assertNotIn("item.pop_server || item.service_address", mailbox_form)
            self.assertIn('return row.imap_server || row.service_address || row.pop_server || "-"', mail_view)
            self.assertIn("item.pop_server", mail_view)
            self.assertIn("/api/config/save", (FRONTEND_API / "config.js").read_text(encoding="utf-8"))
            self.assertIn("/api/rules/folders", (FRONTEND_API / "rule.js").read_text(encoding="utf-8"))
            self.assertIn("文件夹映射", rule_editor)
            self.assertIn("<el-container", app_source)

            with mock.patch("web.app.connect", return_value=object()) as connect_mock, \
                    mock.patch("web.app.safe_logout") as logout_mock, \
                    mock.patch(
                        "web.app.list_folders",
                        return_value=[
                            FolderInfo("INBOX", "收件箱", tuple()),
                            FolderInfo("Archive", "归档", tuple()),
                        ],
                    ):
                status, _, body = request(
                    "/api/rules/folders",
                    cookie_header=cookie_header,
                    method="POST",
                    form={"role": "source", "email": "source@example.com"},
                )
                folder_data = json.loads(body)
                self.assertEqual(status, 200)
                self.assertTrue(folder_data["ok"])
                self.assertEqual(folder_data["folders"][0]["raw_name"], "Archive")
                self.assertEqual(folder_data["folders"][1]["display_name"], "收件箱")
                connect_mock.assert_called_once()
                logout_mock.assert_called_once()

            with mock.patch("web.app.connect", return_value=object()), \
                    mock.patch("web.app.safe_logout"), \
                    mock.patch(
                        "web.app.list_folders",
                        return_value=[
                            FolderInfo("INBOX", "收件箱", tuple()),
                            FolderInfo("Archived", "Archived", (r"\Noselect",)),
                        ],
                    ):
                status, _, body = request(
                    "/api/rules/folders",
                    cookie_header=cookie_header,
                    method="POST",
                    form={"role": "target", "email": "target@example.com"},
                )
                folder_data = json.loads(body)
                self.assertEqual(status, 200)
                self.assertEqual(
                    [row["raw_name"] for row in folder_data["folders"]],
                    ["INBOX"],
                )

    def test_outlook_login_saves_mailbox_from_token_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                db.execute(
                    """
                    INSERT INTO app_settings(key, value_json)
                    VALUES('outlook_login', ?)
                    """,
                    (
                        json.dumps(
                            {
                                "client_id": "client-id",
                            }
                        ),
                    ),
                )
                db.commit()
                cookie_header = make_session_cookie(db, "admin").split(";", 1)[0]

                def request(path, *, method="GET", form=None):
                    handler = make_handler(state_path)
                    body = json.dumps(form or {}).encode("utf-8")
                    raw_headers = [
                        f"{method} {path} HTTP/1.1",
                        "Host: localhost",
                        f"Cookie: {cookie_header}",
                    ]
                    if form is not None:
                        raw_headers.append("Content-Type: application/json")
                        raw_headers.append(f"Content-Length: {len(body)}")
                    raw = ("\r\n".join(raw_headers) + "\r\n\r\n").encode("utf-8") + body
                    socket = FakeSocket(raw)
                    handler(socket, ("127.0.0.1", 0), object())
                    return parse_response(socket.output.getvalue())

                oauth_calls = []

                def microsoft_response(url, fields, return_oauth_error=False):
                    oauth_calls.append((url, dict(fields), return_oauth_error))
                    if url.endswith("/devicecode"):
                        return {
                            "device_code": "device-code",
                            "user_code": "ABCD-EFGH",
                            "verification_uri": "https://microsoft.com/devicelogin",
                            "verification_uri_complete": "https://microsoft.com/devicelogin?code=ABCD-EFGH",
                            "expires_in": 600,
                            "interval": 1,
                            "message": "Use code ABCD-EFGH",
                        }
                    return {
                        "access_token": jwt_with_claims({"upn": "user@outlook.com"}),
                        "id_token": jwt_with_claims(
                            {"preferred_username": "USER@Outlook.COM"}
                        ),
                        "refresh_token": "refresh-token",
                        "expires_in": 3600,
                    }

                with mock.patch("web.app.post_form_json", side_effect=microsoft_response):
                    status, _, body = request(
                        "/api/mail/outlook/start",
                        method="POST",
                        form={},
                    )
                    self.assertEqual(status, 200)
                    started = json.loads(body)
                    self.assertTrue(started["ok"])
                    self.assertNotIn("email", started)
                    self.assertEqual(
                        started["verification_uri_complete"],
                        "https://microsoft.com/devicelogin?code=ABCD-EFGH",
                    )
                    self.assertIn("openid", oauth_calls[0][1]["scope"])

                    status, _, body = request(
                        "/api/mail/outlook/complete",
                        method="POST",
                        form={"device_code": started["device_code"]},
                    )

                self.assertEqual(status, 200)
                completed = json.loads(body)
                self.assertTrue(completed["ok"])
                self.assertIn("user@outlook.com", completed["message"])
                mailbox = db.execute(
                    """
                    SELECT email, auth_json
                    FROM mail_accounts
                    WHERE email='user@outlook.com'
                    """
                ).fetchone()
                self.assertIsNotNone(mailbox)
                auth = json.loads(mailbox["auth_json"])
                self.assertEqual(auth["provider"], "microsoft")
                self.assertEqual(auth["client_id"], "client-id")
                self.assertEqual(auth["token"]["refresh_token"], "refresh-token")
                self.assertNotIn("token_file", auth)
                detail = target_mailbox_detail(
                    db,
                    "user@outlook.com",
                    None,
                    "outlook",
                    "outlook.office365.com",
                )
                self.assertTrue(detail["mailbox"]["oauth_token_saved"])
                outlook_summary = next(
                    item
                    for item in detail["mailboxes"]
                    if item["email"] == "user@outlook.com"
                )
                self.assertTrue(outlook_summary["oauth_token_saved"])

                app_source = (FRONTEND_SRC / "App.vue").read_text(
                    encoding="utf-8"
                )
                mail_view = (FRONTEND_VIEWS / "MailView.vue").read_text(
                    encoding="utf-8"
                )
                mail_api = (FRONTEND_API / "mail.js").read_text(encoding="utf-8")
                session_api = (FRONTEND_API / "session.js").read_text(encoding="utf-8")
                self.assertIn("outlookStart", mail_api)
                self.assertIn("outlookAuthorize", mail_api)
                self.assertIn("allowOkFalse", session_api)
                self.assertIn("{ allowOkFalse: true }", mail_api)
                self.assertIn("startOutlookLogin", mail_view)
                self.assertIn("mailApi.outlookStart", mail_view)
                self.assertNotIn("mailApi.outlookAuthorize({", mail_view)
                self.assertIn("openOutlookLoginPage", mail_view)
                self.assertIn("showOutlookDeviceMessage", mail_view)
                self.assertIn("ElMessageBox.alert", mail_view)
                self.assertIn("center: true", mail_view)
                self.assertIn("showClose: false", mail_view)
                self.assertIn("closeOnClickModal: false", mail_view)
                self.assertIn('confirmButtonText: "去登录"', mail_view)
                self.assertIn("verification_uri_complete", mail_view)
                self.assertIn("打开 Microsoft 登录页面，并输入下面的验证码", mail_view)
                self.assertIn("bindOutlookMessageHandler", mail_view)
                self.assertIn("mail-sync-outlook-login", mail_view)
                self.assertIn("window.open(loginUrl", mail_view)
                self.assertNotIn('window.open("about:blank"', mail_view)
                self.assertNotIn('confirmButtonText: "知道了"', mail_view)
                self.assertIn("Outlook 登录", mail_view)
                self.assertNotIn("outlookResult.user_code", mail_view)
                forms_source = (FRONTEND_SRC / "utils" / "forms.js").read_text(
                    encoding="utf-8"
                )
                self.assertIn("const selected = detail.mailbox", forms_source)
                self.assertIn("return selected;", forms_source)
                self.assertIn("<MailView", app_source)
                self.assertNotIn("textInput(\"Outlook 邮箱\"", app_source)
            finally:
                db.close()

    def test_outlook_authorization_callback_saves_mailbox_without_device_code(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                db.execute(
                    """
                    INSERT INTO app_settings(key, value_json)
                    VALUES('outlook_login', ?)
                    """,
                    (
                        json.dumps(
                            {
                                "client_id": "client-id",
                            }
                        ),
                    ),
                )
                db.commit()
                cookie_header = make_session_cookie(db, "admin").split(";", 1)[0]

                def request(path, *, method="GET", form=None, cookie=True):
                    handler = make_handler(state_path)
                    body = json.dumps(form or {}).encode("utf-8")
                    raw_headers = [
                        f"{method} {path} HTTP/1.1",
                        "Host: localhost",
                    ]
                    if cookie:
                        raw_headers.append(f"Cookie: {cookie_header}")
                    if form is not None:
                        raw_headers.append("Content-Type: application/json")
                        raw_headers.append(f"Content-Length: {len(body)}")
                    raw = ("\r\n".join(raw_headers) + "\r\n\r\n").encode("utf-8") + body
                    socket = FakeSocket(raw)
                    handler(socket, ("127.0.0.1", 0), object())
                    return parse_response(socket.output.getvalue())

                oauth_calls = []

                def microsoft_response(url, fields, return_oauth_error=False):
                    oauth_calls.append((url, dict(fields), return_oauth_error))
                    self.assertTrue(url.endswith("/token"))
                    self.assertEqual(fields["grant_type"], "authorization_code")
                    self.assertEqual(fields["redirect_uri"], "http://localhost/outlook/callback")
                    self.assertIn("code_verifier", fields)
                    return {
                        "access_token": jwt_with_claims({"upn": "callback@outlook.com"}),
                        "id_token": jwt_with_claims(
                            {"preferred_username": "CALLBACK@Outlook.COM"}
                        ),
                        "refresh_token": "refresh-token",
                        "expires_in": 3600,
                    }

                status, _, body = request(
                    "/api/mail/outlook/authorize",
                    method="POST",
                    form={},
                )
                self.assertEqual(status, 200)
                started = json.loads(body)
                self.assertTrue(started["ok"])
                authorize_url = started["authorization_url"]
                self.assertIn("/authorize?", authorize_url)
                self.assertNotIn("/devicecode", authorize_url)
                query = parse_qs(urlparse(authorize_url).query)
                self.assertEqual(query["client_id"], ["client-id"])
                self.assertEqual(query["redirect_uri"], ["http://localhost/outlook/callback"])
                self.assertEqual(query["response_type"], ["code"])
                self.assertEqual(query["code_challenge_method"], ["S256"])
                self.assertIn("IMAP.AccessAsUser.All", query["scope"][0])

                with mock.patch("web.app.post_form_json", side_effect=microsoft_response):
                    status, _, body = request(
                        f"/outlook/callback?code=auth-code&state={query['state'][0]}",
                        cookie=False,
                    )
                self.assertEqual(status, 200)
                page = body.decode("utf-8")
                self.assertIn("mail-sync-outlook-login", page)
                self.assertIn("callback@outlook.com", page)
                self.assertEqual(len(oauth_calls), 1)

                mailbox = db.execute(
                    """
                    SELECT email, auth_json
                    FROM mail_accounts
                    WHERE email='callback@outlook.com'
                    """
                ).fetchone()
                self.assertIsNotNone(mailbox)
                auth = json.loads(mailbox["auth_json"])
                self.assertEqual(auth["provider"], "microsoft")
                self.assertEqual(auth["token"]["refresh_token"], "refresh-token")
                self.assertNotIn("token_file", auth)
            finally:
                db.close()

    def test_target_label_crud_and_runtime_config(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())

                config = load_runtime_config(db, state_path)
                self.assertEqual(
                    config["synology_labels"]["$inbox"],
                    "BOX:收件箱",
                )

                create_synology_label(
                    db,
                    target_email="target@example.com",
                    label_id="$archive",
                    label_name="BOX:归档",
                )
                self.assertTrue(
                    any(
                        row["label_id"] == "$archive"
                        for row in list_synology_labels(
                            db,
                            "target@example.com",
                        )
                    )
                )

                update_synology_label(
                    db,
                    target_email="target@example.com",
                    original_label_id="$archive",
                    label_id="$archive2",
                    label_name="BOX:归档2",
                )
                config = load_runtime_config(db, state_path)
                self.assertEqual(
                    config["synology_labels"]["$archive2"],
                    "BOX:归档2",
                )

                update_synology_label(
                    db,
                    target_email="target@example.com",
                    original_label_id="$inbox",
                    label_id="$inbox",
                    label_name="BOX:Inbox",
                )
                folder_rule = db.execute(
                    """
                    SELECT folder_label
                    FROM folder_rules
                    WHERE source_email='source@example.com' AND folder_name='INBOX'
                    """
                ).fetchone()
                self.assertEqual(folder_rule["folder_label"], "BOX:Inbox")

                with self.assertRaisesRegex(mail_sync.SyncError, "still used"):
                    delete_synology_label(
                        db,
                        target_email="target@example.com",
                        label_id="$inbox",
                    )

                delete_synology_label(
                    db,
                    target_email="target@example.com",
                    label_id="$archive2",
                )
                self.assertFalse(
                    any(
                        row["label_id"] == "$archive2"
                        for row in list_synology_labels(
                            db,
                            "target@example.com",
                        )
                    )
                )

                create_synology_label(
                    db,
                    target_email="source@example.com",
                    label_id="$source_label",
                    label_name="SOURCE:自定义",
                )
                source_labels = list_synology_labels(db, "source@example.com")
                self.assertTrue(
                    any(row["label_id"] == "$source_label" for row in source_labels)
                )
                detail = target_mailbox_detail(
                    db,
                    "source@example.com",
                    "source",
                    "labels",
                )
                self.assertEqual(detail["dialog"], "labels")
                self.assertTrue(
                    any(
                        row["label_name"] == "SOURCE:自定义"
                        for row in detail["labels"]
                    )
                )
                label_panel = (FRONTEND_COMPONENTS / "LabelsPanel.vue").read_text(
                    encoding="utf-8"
                )
                self.assertIn("createLabel", label_panel)
                self.assertIn("/api/mail/labels/create", (FRONTEND_API / "mail.js").read_text(encoding="utf-8"))
            finally:
                db.close()

    def test_label_usage_count_includes_secondary_and_out_of_scope_labels(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                create_synology_label(
                    db,
                    target_email="target@example.com",
                    label_id="$secondary",
                    label_name="BOX:第二标签",
                )
                create_synology_label(
                    db,
                    target_email="target@example.com",
                    label_id="$out_of_scope",
                    label_name="BOX:移出范围",
                )

                db.execute(
                    """
                    UPDATE folder_rules
                    SET folder_label=?, folder_labels_json=?
                    WHERE source_email=? AND folder_name=?
                    """,
                    (
                        "SYNC:source@example.com",
                        json.dumps(
                            ["SYNC:source@example.com", "BOX:第二标签"],
                            ensure_ascii=False,
                        ),
                        "source@example.com",
                        "INBOX",
                    ),
                )
                rule = db.execute(
                    """
                    SELECT extra_json
                    FROM source_rules
                    WHERE email=?
                    """,
                    ("source@example.com",),
                ).fetchone()
                extra = json.loads(rule["extra_json"] or "{}")
                extra["out_of_scope_policy"] = "label"
                extra["out_of_scope_label"] = "BOX:移出范围"
                extra["out_of_scope_labels"] = ["BOX:移出范围"]
                db.execute(
                    """
                    UPDATE source_rules
                    SET extra_json=?
                    WHERE email=?
                    """,
                    (
                        json.dumps(extra, ensure_ascii=False),
                        "source@example.com",
                    ),
                )

                usage = {
                    row["label_name"]: row["usage_count"]
                    for row in list_synology_labels(db, "target@example.com")
                }
                self.assertEqual(usage["BOX:第二标签"], 1)
                self.assertEqual(usage["BOX:移出范围"], 1)
                self.assertEqual(usage["SYNC:source@example.com"], 2)

                with self.assertRaisesRegex(
                    mail_sync.SyncError,
                    "1 rule reference",
                ):
                    delete_synology_label(
                        db,
                        target_email="target@example.com",
                        label_id="$secondary",
                    )
            finally:
                db.close()

    def test_runtime_config_can_explicitly_include_disabled_source(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                db.execute(
                    "UPDATE source_rules SET enabled=0 WHERE email=?",
                    ("source@example.com",),
                )
                db.commit()

                config = load_runtime_config(db, state_path)
                self.assertEqual(config["sources"], [])

                config = load_runtime_config(
                    db,
                    state_path,
                    include_source_email="source@example.com",
                )
                self.assertEqual(
                    [source["email"] for source in config["sources"]],
                    ["source@example.com"],
                )
            finally:
                db.close()

    def test_runtime_config_does_not_borrow_labels_from_other_targets(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                db.execute(
                    """
                    INSERT INTO mail_accounts(
                        email, service_address, enabled, imap_server, imap_port,
                        use_ssl, target_folder, sort_order
                    )
                    VALUES(?, ?, 1, ?, 993, 1, 'INBOX', 10)
                    """,
                    (
                        "no-label-target@example.com",
                        "imap.no-label.example.com",
                        "imap.no-label.example.com",
                    ),
                )
                db.execute(
                    """
                    UPDATE source_rules
                    SET extra_json=?
                    WHERE email=?
                    """,
                    (
                        json.dumps(
                            {
                                "target_email": "no-label-target@example.com",
                                "target_service_address": "imap.no-label.example.com",
                            }
                        ),
                        "source@example.com",
                    ),
                )
                db.commit()

                config = load_runtime_config(db, state_path)
                self.assertEqual(config["synology_labels"], {})
                self.assertEqual(config["sources"][0]["synology_labels"], {})
            finally:
                db.close()

    def test_mailbox_probe_results_are_saved_and_exposed(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                probe_results = {
                    "tested_at": "2026-07-09T10:00:00+00:00",
                    "protocol": "imap",
                    "items": [
                        {
                            "key": "append_internal_date_visible",
                            "label": "原始时间写入",
                            "status": "warn",
                            "message": "同步时会自动跳过原始时间。",
                        }
                    ],
                    "features": {
                        "append_internal_date_supported": False,
                        "omit_append_internal_date": True,
                    },
                    "feature_groups": {
                        "common": {},
                        "source": {"idle_supported": False},
                        "target": {
                            "append_internal_date_supported": False,
                            "omit_append_internal_date": True,
                        },
                    },
                }

                saved = save_mailbox_probe_results(
                    db,
                    "target@example.com",
                    "target.example.com",
                    probe_results,
                )

                self.assertTrue(saved)
                detail = target_mailbox_detail(
                    db,
                    "target@example.com",
                    selected_service_address="target.example.com",
                )
                self.assertTrue(
                    detail["mailbox"]["mailbox_features"]["omit_append_internal_date"]
                )
                self.assertTrue(
                    detail["mailbox"]["mailbox_feature_groups"]["target"][
                        "omit_append_internal_date"
                    ]
                )
                self.assertFalse(
                    detail["mailbox"]["mailbox_feature_groups"]["source"][
                        "idle_supported"
                    ]
                )
                self.assertEqual(
                    detail["mailbox"]["probe_results"]["items"][0]["key"],
                    "append_internal_date_visible",
                )
                config = load_runtime_config(db, state_path)
                self.assertTrue(
                    config["target"]["mailbox_features"]["omit_append_internal_date"]
                )
                self.assertTrue(
                    config["target"]["mailbox_feature_groups"]["target"][
                        "omit_append_internal_date"
                    ]
                )
                self.assertTrue(
                    config["sources"][0]["target"]["mailbox_features"][
                        "omit_append_internal_date"
                    ]
                )
                self.assertTrue(
                    config["sources"][0]["target"]["mailbox_feature_groups"]["target"][
                        "omit_append_internal_date"
                    ]
                )
                save_mailbox_account(
                    db,
                    original_role="",
                    original_email="",
                    role="mailbox",
                    email="new-probe@example.com",
                    enabled=True,
                    auth_provider="manual",
                    imap_server="imap.new-probe.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="secret",
                    oauth_client_id="",
                    oauth_token_file="",
                    target_folder="INBOX",
                    imap_timeout="",
                    proxy_profile_id="",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    service_address="imap.new-probe.example.com",
                    imap_enabled=True,
                    smtp_enabled=False,
                    smtp_server="",
                    smtp_port="",
                    smtp_use_ssl=True,
                    smtp_timeout="",
                    probe_results_json=json.dumps(probe_results),
                    realtime_poll_interval="7",
                )
                new_detail = target_mailbox_detail(
                    db,
                    "new-probe@example.com",
                    selected_service_address="imap.new-probe.example.com",
                )
                self.assertTrue(
                    new_detail["mailbox"]["mailbox_features"][
                        "omit_append_internal_date"
                    ]
                )
                self.assertTrue(
                    new_detail["mailbox"]["mailbox_feature_groups"]["target"][
                        "omit_append_internal_date"
                    ]
                )
                self.assertEqual(new_detail["mailbox"]["realtime_poll_interval"], 7)
            finally:
                db.close()

    def test_target_account_save_moves_label_mappings(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())

                saved_email = save_target_account(
                    db,
                    original_email="target@example.com",
                    email="new-target@example.com",
                    enabled=True,
                    imap_server="new-target.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="",
                    target_folder="Archive",
                    imap_timeout="60",
                    proxy_profile_id="",
                )
                self.assertEqual(saved_email, "new-target@example.com")

                detail = target_mailbox_detail(db, "new-target@example.com")
                self.assertEqual(detail["target"]["target_folder"], "Archive")
                self.assertTrue(
                    any(
                        row["target_email"] == "new-target@example.com"
                        for row in detail["labels"]
                    )
                )
                config = load_runtime_config(db, state_path)
                self.assertEqual(config["target"]["email"], "new-target@example.com")
                self.assertIn("$account", config["synology_labels"])
            finally:
                db.close()

    def test_runtime_config_keeps_target_on_each_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())

                save_mailbox_account(
                    db,
                    original_role="",
                    original_email="",
                    role="source",
                    email="second@example.com",
                    enabled=True,
                    auth_provider="manual",
                    imap_server="second.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="secret",
                    oauth_client_id="",
                    oauth_token_file="",
                    target_folder="INBOX",
                    imap_timeout="",
                    proxy_profile_id="",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    service_address="second.example.com",
                )
                save_mailbox_account(
                    db,
                    original_role="",
                    original_email="",
                    role="target",
                    email="archive@example.com",
                    enabled=True,
                    auth_provider="manual",
                    imap_server="archive.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="secret",
                    oauth_client_id="",
                    oauth_token_file="",
                    target_folder="Archive",
                    imap_timeout="",
                    proxy_profile_id="",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    service_address="archive.example.com",
                )
                for label_id, label_name in {
                    "$account2": "SYNC:second@example.com",
                    "$inbox2": "BOX:第二收件箱",
                    "$other2": "BOX:第二其他",
                    "$deleted2": "BOX:第二已删除",
                }.items():
                    create_synology_label(
                        db,
                        target_email="archive@example.com",
                        label_id=label_id,
                        label_name=label_name,
                    )

                save_sync_rule(
                    db,
                    original_email="",
                    source_email="second@example.com",
                    target_email="archive@example.com",
                    enabled=True,
                    account_label="SYNC:second@example.com",
                    default_folder_label="BOX:第二其他",
                    deleted_label="BOX:第二已删除",
                    sync_interval="",
                    batch_size="",
                    skip_deleted_drafts=False,
                    sync_mode="realtime",
                    date_policy="auto",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    folder_rules=[
                        {
                            "folder_name": "INBOX",
                            "folder_labels": ["BOX:第二收件箱"],
                            "excluded": False,
                            "skip_uids": "",
                        }
                    ],
                )

                config = load_runtime_config(db, state_path)
                sources = {row["email"]: row for row in config["sources"]}
                self.assertEqual(
                    sources["source@example.com"]["target"]["email"],
                    "target@example.com",
                )
                self.assertEqual(
                    sources["second@example.com"]["target"]["email"],
                    "archive@example.com",
                )
                self.assertEqual(
                    sources["second@example.com"]["target"]["folder"],
                    "Archive",
                )
                self.assertEqual(
                    sources["second@example.com"]["synology_labels"]["$account2"],
                    "SYNC:second@example.com",
                )
            finally:
                db.close()

    def test_sync_rule_uses_labels_from_selected_target(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())

                save_mailbox_account(
                    db,
                    original_role="",
                    original_email="",
                    role="target",
                    email="archive@example.com",
                    enabled=True,
                    auth_provider="manual",
                    imap_server="archive.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="secret",
                    oauth_client_id="",
                    oauth_token_file="",
                    target_folder="Archive",
                    imap_timeout="",
                    proxy_profile_id="",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    service_address="archive.example.com",
                )
                for label_id, label_name in {
                    "$archive_account": "SYNC:source@example.com",
                    "$archive_inbox": "BOX:收件箱",
                    "$archive_other": "BOX:其他文件夹",
                    "$archive_deleted": "BOX:已删除邮件",
                }.items():
                    create_synology_label(
                        db,
                        target_email="archive@example.com",
                        label_id=label_id,
                        label_name=label_name,
                    )

                saved = save_sync_rule(
                    db,
                    original_email="source@example.com",
                    source_email="source@example.com",
                    target_email="archive@example.com",
                    target_service_address="archive.example.com",
                    enabled=True,
                    account_label="SYNC:source@example.com",
                    default_folder_label="BOX:其他文件夹",
                    deleted_label="BOX:已删除邮件",
                    sync_interval="",
                    batch_size="",
                    skip_deleted_drafts=False,
                    sync_mode="realtime",
                    date_policy="auto",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    folder_rules=[
                        {
                            "folder_name": "INBOX",
                            "target_folder": "Archive",
                            "folder_labels": ["BOX:收件箱"],
                            "excluded": False,
                            "skip_uids": "",
                        }
                    ],
                )
                self.assertEqual(saved, "source@example.com")

                config = load_runtime_config(db, state_path)
                source = config["sources"][0]
                self.assertEqual(source["target"]["email"], "archive@example.com")
                self.assertEqual(source["account_label"], "SYNC:source@example.com")
                self.assertEqual(source["default_folder_label"], "BOX:其他文件夹")
                self.assertEqual(source["deleted_label"], "BOX:已删除邮件")
                self.assertEqual(source["folder_labels"], {"INBOX": "BOX:收件箱"})
                self.assertEqual(source["target_folders"], {"INBOX": "Archive"})
                self.assertNotIn("$account", source["synology_labels"])
                self.assertEqual(
                    source["synology_labels"]["$archive_account"],
                    "SYNC:source@example.com",
                )
                self.assertEqual(
                    source["synology_labels"]["$archive_inbox"],
                    "BOX:收件箱",
                )
                mail_sync.validate_config(
                    config,
                    mail_sync.label_id_map(config["synology_labels"]),
                )
            finally:
                db.close()

    def test_sync_rule_allows_labels_without_target_validation(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                save_mailbox_account(
                    db,
                    original_role="",
                    original_email="",
                    role="target",
                    email="archive@example.com",
                    enabled=True,
                    auth_provider="manual",
                    imap_server="archive.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="secret",
                    oauth_client_id="",
                    oauth_token_file="",
                    target_folder="Archive",
                    imap_timeout="",
                    proxy_profile_id="",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    service_address="archive.example.com",
                )
                create_synology_label(
                    db,
                    target_email="archive@example.com",
                    label_id="$archive_account",
                    label_name="SYNC:archive@example.com",
                )

                save_sync_rule(
                    db,
                    original_email="source@example.com",
                    source_email="source@example.com",
                    target_email="archive@example.com",
                    target_service_address="archive.example.com",
                    enabled=True,
                    account_label="SYNC:source@example.com",
                    default_folder_label="BOX:其他文件夹",
                    deleted_label="BOX:已删除邮件",
                    sync_interval="",
                    batch_size="",
                    skip_deleted_drafts=False,
                    sync_mode="realtime",
                    date_policy="auto",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    folder_rules=[
                        {
                            "folder_name": "INBOX",
                            "folder_labels": ["BOX:收件箱"],
                            "excluded": False,
                            "skip_uids": "",
                        }
                    ],
                )
                config = load_runtime_config(db, state_path)
                source = config["sources"][0]
                self.assertEqual(source["account_label"], "SYNC:source@example.com")
                self.assertEqual(source["folder_labels"], {"INBOX": "BOX:收件箱"})
                mail_sync.validate_config(
                    config,
                    mail_sync.label_id_map(config["synology_labels"]),
                )
            finally:
                db.close()

    def test_mailbox_identity_uses_email_and_service_address(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())

                with self.assertRaisesRegex(
                    mail_sync.SyncError,
                    "email and service address",
                ):
                    save_mailbox_account(
                        db,
                        original_role="",
                        original_email="",
                        role="source",
                        email="target@example.com",
                        enabled=True,
                        auth_provider="manual",
                        imap_server="target.example.com",
                        imap_port="993",
                        use_ssl=True,
                        password="secret",
                        oauth_client_id="",
                        oauth_token_file="",
                        target_folder="INBOX",
                        imap_timeout="",
                        proxy_profile_id="",
                        pop_fallback_enabled=False,
                        pop_server="",
                        pop_port="",
                        pop_use_ssl=True,
                        pop_timeout="",
                        service_address="target.example.com",
                    )

                saved_email = save_mailbox_account(
                    db,
                    original_role="",
                    original_email="",
                    role="source",
                    email="source@example.com",
                    enabled=True,
                    auth_provider="manual",
                    imap_server="archive.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="secret",
                    oauth_client_id="",
                    oauth_token_file="",
                    target_folder="INBOX",
                    imap_timeout="",
                    proxy_profile_id="",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    service_address="archive.example.com",
                )
                self.assertEqual(saved_email, "source@example.com")

                create_synology_label(
                    db,
                    target_email="source@example.com",
                    target_service_address="source.example.com",
                    label_id="$service_a",
                    label_name="SOURCE:A",
                )
                create_synology_label(
                    db,
                    target_email="source@example.com",
                    target_service_address="archive.example.com",
                    label_id="$service_a",
                    label_name="SOURCE:B",
                )

                service_a_labels = list_synology_labels(
                    db,
                    "source@example.com",
                    "source.example.com",
                )
                service_b_labels = list_synology_labels(
                    db,
                    "source@example.com",
                    "archive.example.com",
                )
                self.assertEqual(service_a_labels[0]["label_name"], "SOURCE:A")
                self.assertEqual(service_b_labels[0]["label_name"], "SOURCE:B")

                detail = target_mailbox_detail(
                    db,
                    "source@example.com",
                    "source",
                    "labels",
                    "archive.example.com",
                )
                self.assertEqual(
                    detail["mailbox"]["service_address"],
                    "archive.example.com",
                )
                label_names = [row["label_name"] for row in detail["labels"]]
                self.assertIn("SOURCE:B", label_names)
                self.assertNotIn("SOURCE:A", label_names)

                save_sync_rule(
                    db,
                    original_email="source@example.com",
                    source_email="source@example.com",
                    source_service_address="archive.example.com",
                    target_email="source@example.com",
                    target_service_address="archive.example.com",
                    enabled=True,
                    account_label="SOURCE:B",
                    default_folder_label="",
                    deleted_label="SOURCE:B",
                    sync_interval="",
                    batch_size="",
                    skip_deleted_drafts=False,
                    sync_mode="realtime",
                    date_policy="auto",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    folder_rules=[
                        {
                            "folder_name": "INBOX",
                            "folder_labels": ["SOURCE:B"],
                            "excluded": False,
                            "skip_uids": "",
                        }
                    ],
                )
                config = load_runtime_config(db, state_path)
                source = config["sources"][0]
                self.assertEqual(source["service_address"], "archive.example.com")
                self.assertEqual(
                    source["target"]["service_address"],
                    "archive.example.com",
                )
                self.assertEqual(
                    source["synology_labels"]["$service_a"],
                    "SOURCE:B",
                )
            finally:
                db.close()

    def test_mailbox_bound_to_rule_cannot_be_deleted(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                save_mailbox_account(
                    db,
                    original_role="",
                    original_email="",
                    role="mailbox",
                    email="source@example.com",
                    enabled=True,
                    auth_provider="manual",
                    imap_server="spare.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="secret",
                    oauth_client_id="",
                    oauth_token_file="",
                    target_folder="INBOX",
                    imap_timeout="",
                    proxy_profile_id="",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    service_address="spare.example.com",
                )

                detail = target_mailbox_detail(db)
                mail_view = (FRONTEND_VIEWS / "MailView.vue").read_text(
                    encoding="utf-8"
                )
                self.assertIn("is_rule_bound", mail_view)
                by_service = {
                    row["service_address"]: row
                    for row in detail["mailboxes"]
                    if row["email"] == "source@example.com"
                }
                self.assertTrue(by_service["source.example.com"]["is_rule_bound"])
                self.assertFalse(by_service["spare.example.com"]["is_rule_bound"])

                with self.assertRaisesRegex(mail_sync.SyncError, "绑定同步规则"):
                    delete_mailbox_account(
                        db,
                        role="mailbox",
                        email="source@example.com",
                        service_address="source.example.com",
                    )

                with self.assertRaisesRegex(mail_sync.SyncError, "绑定同步规则"):
                    delete_mailbox_account(
                        db,
                        role="mailbox",
                        email="target@example.com",
                        service_address="target.example.com",
                    )

                delete_mailbox_account(
                    db,
                    role="mailbox",
                    email="source@example.com",
                    service_address="spare.example.com",
                )
                remaining = target_mailbox_detail(db)["mailboxes"]
                self.assertNotIn(
                    "spare.example.com",
                    {row["service_address"] for row in remaining},
                )
            finally:
                db.close()

    def test_mailbox_protocols_include_smtp(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())

                save_mailbox_account(
                    db,
                    original_role="source",
                    original_email="source@example.com",
                    original_service_address="source.example.com",
                    role="source",
                    email="source@example.com",
                    enabled=True,
                    auth_provider="manual",
                    imap_server="source.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="",
                    oauth_client_id="",
                    oauth_token_file="",
                    target_folder="INBOX",
                    imap_timeout="60",
                    proxy_profile_id="",
                    pop_fallback_enabled=True,
                    pop_server="pop.source.example.com",
                    pop_port="995",
                    pop_use_ssl=True,
                    pop_timeout="120",
                    service_address="source.example.com",
                    imap_enabled=True,
                    smtp_enabled=True,
                    smtp_server="smtp.source.example.com",
                    smtp_port="465",
                    smtp_use_ssl=True,
                    smtp_timeout="90",
                )

                detail = target_mailbox_detail(
                    db,
                    "source@example.com",
                    "source",
                    "edit",
                    "source.example.com",
                )
                mailbox = detail["mailbox"]
                self.assertEqual(mailbox["protocol_summary"], "IMAP / POP / SMTP")
                self.assertEqual(mailbox["smtp_server"], "smtp.source.example.com")
                mail_view = (FRONTEND_VIEWS / "MailView.vue").read_text(
                    encoding="utf-8"
                )
                mail_form = (FRONTEND_COMPONENTS / "MailboxForm.vue").read_text(
                    encoding="utf-8"
                )
                outlook_panel = (
                    FRONTEND_COMPONENTS / "OutlookPanel.vue"
                ).read_text(encoding="utf-8")
                self.assertIn("smtp_server", mail_form)
                self.assertNotIn("mailbox-protocol-smtp", mail_form)
                self.assertIn("probe-panel", mail_form)
                self.assertIn("probe_results_json", mail_form)
                self.assertNotIn("ElMessage.success", mail_form)
                self.assertNotIn("testMessage", mail_form)
                self.assertIn("isOutlookMailbox", mail_view)
                self.assertIn("mailboxServiceText", mail_view)
                self.assertIn("Outlook 邮箱", mail_view)
                self.assertIn("OAuth Client ID", outlook_panel)
                self.assertIn("测试验证", outlook_panel)
                self.assertIn("mailApi.testConnection", outlook_panel)
                self.assertIn('auth_provider: "outlook"', outlook_panel)
                self.assertIn('mailbox_protocol_tab: "imap"', outlook_panel)

                config = load_runtime_config(db, state_path)
                source = config["sources"][0]
                self.assertEqual(source["protocols"], ["imap", "pop", "smtp"])
                self.assertEqual(
                    source["smtp"]["server"],
                    "smtp.source.example.com",
                )
            finally:
                db.close()

    def test_sync_rule_edit_pause_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())

                detail = sync_rules_detail(db, "source@example.com")
                self.assertEqual(
                    detail["selected"]["email"],
                    "source@example.com",
                )
                self.assertEqual(detail["defaults"]["sync_interval"], 900)
                self.assertEqual(detail["defaults"]["batch_size"], 5)
                rule_form = (FRONTEND_COMPONENTS / "RuleEditor.vue").read_text(
                    encoding="utf-8"
                )
                rules_view = (FRONTEND_VIEWS / "RulesView.vue").read_text(
                    encoding="utf-8"
                )
                self.assertIn("规则详情", rule_form)
                self.assertIn("ruleApi.save", rule_form)
                self.assertIn("文件夹映射", rule_form)
                self.assertIn("目标文件夹", rule_form)
                self.assertIn("绑定标签", rule_form)
                self.assertIn("pop_fallback_enabled", rule_form)
                self.assertIn("account_label", rule_form)
                self.assertIn("target_folder_", rule_form)
                self.assertIn("folder_label_", rule_form)
                self.assertIn("folder_display_name_", rule_form)
                self.assertIn("folder_attribute_", rule_form)
                self.assertIn("ruleDefaults.sync_interval", rule_form)
                self.assertIn('FALLBACK_RULE_DEFAULTS', rule_form)
                self.assertIn('submitNumericValue("sync_interval")', rule_form)
                self.assertIn("selectableTargetFolders(targetResult.folders", rule_form)
                self.assertIn("normalizeTargetFolder(existing?.target_folder", rule_form)
                self.assertIn("targetFolderExists(targetDefaultFolder())", rule_form)
                self.assertIn("排除", rule_form)
                self.assertIn("props.detail.all_labels", rule_form)
                self.assertIn("pruneFolderLabelsToTarget", rule_form)
                self.assertIn("targetHasLabels", rule_form)
                self.assertIn("目标邮箱未定义标签", rule_form)
                self.assertIn("row.excluded || !targetHasLabels", rule_form)
                self.assertIn("移出范围", rule_form)
                self.assertIn("保留现状", rule_form)
                self.assertIn("标记标签", rule_form)
                self.assertIn("同步删除", rule_form)
                self.assertIn("v-if=\"form.out_of_scope_policy === 'label'\"", rule_form)
                self.assertIn(':disabled="!targetHasLabels"', rule_form)
                self.assertIn("out_of_scope_policy", rule_form)
                self.assertIn("out_of_scope_label", rule_form)
                self.assertNotIn("allow-create", rule_form)
                self.assertIn("duplicateRuleMessage", rule_form)
                self.assertIn("findDuplicateRule", rule_form)
                self.assertIn("validateDuplicateRule", rule_form)
                self.assertIn("ruleStatusText(row)", rules_view)
                self.assertIn("ruleHasError(row)", rules_view)
                self.assertIn("ruleErrorLines(row)", rules_view)
                self.assertIn("el-tooltip", rules_view)
                self.assertIn("sync_error_summary", rules_view)
                self.assertIn("ruleIntervalText(row)", rules_view)
                self.assertIn("ruleBatchText(row)", rules_view)
                self.assertIn("RULE_FALLBACK_DEFAULTS", rules_view)
                self.assertNotIn("targetLabelRows", rule_form)
                self.assertNotIn("账号标签", rule_form)
                self.assertNotIn("默认文件夹标签", rule_form)
                self.assertNotIn("folderSourceIcon", rule_form)
                self.assertNotIn('v-if="duplicateRuleMessage"', rule_form)
                self.assertNotIn(':disabled="Boolean(duplicateRuleMessage)"', rule_form)

                with self.assertRaisesRegex(
                    mail_sync.SyncError,
                    "同步规则已存在",
                ):
                    save_sync_rule(
                        db,
                        original_email="",
                        source_email="source@example.com",
                        source_service_address="source.example.com",
                        target_email="target@example.com",
                        target_service_address="target.example.com",
                        enabled=True,
                        account_label="SYNC:source@example.com",
                        default_folder_label="BOX:其他文件夹",
                        deleted_label="BOX:已删除邮件",
                        sync_interval="",
                        batch_size="",
                        skip_deleted_drafts=False,
                        sync_mode="realtime",
                        date_policy="auto",
                        pop_fallback_enabled=False,
                        pop_server="",
                        pop_port="",
                        pop_use_ssl=True,
                        pop_timeout="",
                        folder_rules=[],
                    )

                saved = save_sync_rule(
                    db,
                    original_email="source@example.com",
                    source_email="source@example.com",
                    target_email="target@example.com",
                    enabled=True,
                    account_label="SYNC:source@example.com",
                    default_folder_label="BOX:其他文件夹",
                    deleted_label="BOX:已删除邮件",
                    sync_interval="300",
                    batch_size="11",
                    skip_deleted_drafts=True,
                    sync_mode="scheduled",
                    date_policy="qq_internaldate",
                    out_of_scope_policy="label",
                    out_of_scope_label=["BOX:收件箱", "BOX:已删除邮件"],
                    pop_fallback_enabled=True,
                    pop_server="pop.example.com",
                    pop_port="995",
                    pop_use_ssl=True,
                    pop_timeout="120",
                    folder_rules=[
                        {
                            "folder_name": "INBOX",
                            "display_name": "收件箱",
                            "attributes": [],
                            "target_folder": "Archive",
                            "folder_label": "BOX:收件箱",
                            "excluded": False,
                            "skip_uids": "1, 2",
                        },
                        {
                            "folder_name": "Sent",
                            "display_name": "已发送",
                            "attributes": [r"\Sent"],
                            "folder_label": "",
                            "excluded": True,
                            "skip_uids": "",
                        },
                    ],
                )
                self.assertEqual(saved, "source@example.com")

                detail = sync_rules_detail(db, "source@example.com")
                folder_details = {
                    row["folder_name"]: row
                    for row in detail["selected"]["folder_rules"]
                }
                self.assertEqual(folder_details["INBOX"]["display_name"], "收件箱")
                self.assertEqual(folder_details["Sent"]["display_name"], "已发送")
                self.assertEqual(folder_details["Sent"]["attributes"], [r"\Sent"])

                config = load_runtime_config(db, state_path)
                source = config["sources"][0]
                self.assertEqual(source["sync_interval"], 300)
                self.assertEqual(source["batch_size"], 11)
                self.assertTrue(source["skip_deleted_drafts"])
                self.assertEqual(source["sync_mode"], "scheduled")
                self.assertEqual(source["date_policy"], "qq_internaldate")
                self.assertEqual(source["out_of_scope_policy"], "label")
                self.assertEqual(source["out_of_scope_label"], "BOX:收件箱")
                self.assertEqual(
                    source["out_of_scope_labels"],
                    ["BOX:收件箱", "BOX:已删除邮件"],
                )
                self.assertEqual(source["pop_fallback"]["server"], "pop.example.com")
                self.assertEqual(source["pop_fallback"]["timeout"], 120)
                self.assertEqual(source["folder_labels"], {"INBOX": "BOX:收件箱"})
                self.assertEqual(source["target_folders"], {"INBOX": "Archive"})
                self.assertEqual(source["exclude_folders"], ["Sent"])
                self.assertEqual(source["skip_uids"], {"INBOX": [1, 2]})
                stored_folder = db.execute(
                    """
                    SELECT target_folder
                    FROM folder_rules
                    WHERE source_email='source@example.com' AND folder_name='INBOX'
                    """
                ).fetchone()
                self.assertEqual(stored_folder["target_folder"], "Archive")

                save_sync_rule(
                    db,
                    original_email="source@example.com",
                    source_email="source@example.com",
                    target_email="target@example.com",
                    enabled=True,
                    account_label="",
                    default_folder_label="",
                    deleted_label="BOX:已删除邮件",
                    sync_interval="",
                    batch_size="",
                    skip_deleted_drafts=False,
                    sync_mode="realtime",
                    date_policy="auto",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    folder_rules=[
                        {
                            "folder_name": "Archive",
                            "folder_label": "",
                            "excluded": False,
                            "skip_uids": "",
                        },
                    ],
                )
                config = load_runtime_config(db, state_path)
                source = config["sources"][0]
                self.assertEqual(source["default_folder_label"], "")
                self.assertEqual(source["folder_labels"], {})
                mail_sync.validate_config(
                    config,
                    mail_sync.label_id_map(config["synology_labels"]),
                )

                set_sync_rule_enabled(db, "source@example.com", False)
                config = load_runtime_config(db, state_path)
                self.assertEqual(config["sources"], [])

                set_sync_rule_enabled(db, "source@example.com", True)
                delete_sync_rule(db, "source@example.com")
                config = load_runtime_config(db, state_path)
                self.assertEqual(config["sources"], [])
                detail = sync_rules_detail(db, "source@example.com")
                self.assertEqual(detail["rules"], [])
                self.assertIn(
                    "source@example.com",
                    {row["email"] for row in detail["available_sources"]},
                )
            finally:
                db.close()

    def test_sync_rule_latest_error_is_stored_on_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            obsolete_table = "sync_rule" + "_status"
            db.execute(f"CREATE TABLE {obsolete_table}(source_email TEXT)")
            db.commit()
            db.close()

            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                tables = {
                    row["name"]
                    for row in db.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'"
                    )
                }
                self.assertNotIn(obsolete_table, tables)

                mark_sync_rule_failure(
                    db,
                    "source@example.com",
                    "Cannot select mailbox Archived",
                )
                detail = sync_rules_detail(db, "source@example.com")
                self.assertEqual(detail["selected"]["sync_status"], "error")
                self.assertEqual(
                    detail["selected"]["sync_error_summary"],
                    "目标文件夹不可用：Archived",
                )
                self.assertEqual(
                    detail["rule_details"][0]["sync_error_summary"],
                    "目标文件夹不可用：Archived",
                )

                mark_sync_rule_success(db, "source@example.com")
                detail = sync_rules_detail(db, "source@example.com")
                self.assertEqual(detail["selected"]["sync_status"], "ok")
                self.assertEqual(detail["selected"]["sync_error_summary"], "")
                self.assertEqual(detail["selected"]["sync_error_detail"], "")
            finally:
                db.close()

    def test_rule_save_api_rejects_missing_target_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                cookie = make_session_cookie(db, "admin").split(";", 1)[0]
                form = {
                    "original_email": "source@example.com",
                    "source_email": "source@example.com",
                    "target_email": "target@example.com",
                    "enabled": "1",
                    "account_label": "SYNC:source@example.com",
                    "default_folder_label": "BOX:其他文件夹",
                    "deleted_label": "BOX:已删除邮件",
                    "sync_interval": "",
                    "batch_size": "",
                    "sync_mode": "realtime",
                    "date_policy": "auto",
                    "pop_use_ssl": "1",
                    "folder_count": "1",
                    "folder_name_0": "INBOX",
                    "target_folder_0": "Archived",
                    "folder_label_0": "BOX:收件箱",
                }
                body = json.dumps(form).encode("utf-8")
                raw = (
                    "\r\n".join(
                        [
                            "POST /api/rules/save HTTP/1.1",
                            "Host: localhost",
                            f"Cookie: {cookie}",
                            "Accept: application/json",
                            "Content-Type: application/json",
                            f"Content-Length: {len(body)}",
                        ]
                    )
                    + "\r\n\r\n"
                ).encode("utf-8") + body
                socket = FakeSocket(raw)
                handler = make_handler(state_path)
                client = mock.Mock()
                stdout = io.StringIO()

                with (
                    mock.patch("web.app.connect", return_value=client),
                    mock.patch(
                        "web.app.list_folders",
                        return_value=[FolderInfo("INBOX", "INBOX", ())],
                    ),
                    mock.patch("web.app.safe_logout") as safe_logout,
                    mock.patch("sys.stdout", stdout),
                ):
                    handler(socket, ("127.0.0.1", 0), object())

                status, _, response_body = parse_response(socket.output.getvalue())
                payload = json.loads(response_body)
                self.assertEqual(status, 400)
                self.assertEqual(payload["error"], "目标文件夹不存在：Archived")
                log_event = json.loads(stdout.getvalue().strip())
                self.assertEqual(log_event["event"], "web_request")
                self.assertEqual(log_event["method"], "POST")
                self.assertEqual(log_event["path"], "/api/rules/save")
                self.assertEqual(log_event["status"], 400)
                self.assertEqual(log_event["source_email"], "source@example.com")
                self.assertEqual(log_event["target_email"], "target@example.com")
                self.assertEqual(log_event["folder_count"], "1")
                self.assertEqual(log_event["error"], "目标文件夹不存在：Archived")
                safe_logout.assert_called_once_with(client)
                folder = db.execute(
                    """
                    SELECT target_folder
                    FROM folder_rules
                    WHERE source_email='source@example.com'
                      AND folder_name='INBOX'
                    """
                ).fetchone()
                self.assertIsNone(folder["target_folder"])
            finally:
                db.close()

    def test_rule_save_api_rejects_unselectable_target_folder(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                cookie = make_session_cookie(db, "admin").split(";", 1)[0]
                form = {
                    "original_email": "source@example.com",
                    "source_email": "source@example.com",
                    "target_email": "target@example.com",
                    "enabled": "1",
                    "account_label": "SYNC:source@example.com",
                    "default_folder_label": "BOX:其他文件夹",
                    "deleted_label": "BOX:已删除邮件",
                    "sync_interval": "",
                    "batch_size": "",
                    "sync_mode": "realtime",
                    "date_policy": "auto",
                    "pop_use_ssl": "1",
                    "folder_count": "1",
                    "folder_name_0": "INBOX",
                    "target_folder_0": "Spam",
                    "folder_label_0": "BOX:收件箱",
                }
                body = json.dumps(form).encode("utf-8")
                raw = (
                    "\r\n".join(
                        [
                            "POST /api/rules/save HTTP/1.1",
                            "Host: localhost",
                            f"Cookie: {cookie}",
                            "Accept: application/json",
                            "Content-Type: application/json",
                            f"Content-Length: {len(body)}",
                        ]
                    )
                    + "\r\n\r\n"
                ).encode("utf-8") + body
                socket = FakeSocket(raw)
                handler = make_handler(state_path)
                client = mock.Mock()

                with (
                    mock.patch("web.app.connect", return_value=client),
                    mock.patch(
                        "web.app.list_folders",
                        return_value=[
                            FolderInfo("INBOX", "INBOX", ()),
                            FolderInfo("Spam", "Spam", ()),
                        ],
                    ),
                    mock.patch(
                        "web.app.select_mailbox",
                        side_effect=[
                            (0, 1),
                            mail_sync.SyncError("Cannot select mailbox Spam"),
                        ],
                    ),
                    mock.patch("web.app.safe_logout") as safe_logout,
                ):
                    handler(socket, ("127.0.0.1", 0), object())

                status, _, response_body = parse_response(socket.output.getvalue())
                payload = json.loads(response_body)
                self.assertEqual(status, 400)
                self.assertEqual(
                    payload["error"],
                    "目标文件夹不可用：Spam（Cannot select mailbox Spam）",
                )
                safe_logout.assert_called_once_with(client)
                folder = db.execute(
                    """
                    SELECT target_folder
                    FROM folder_rules
                    WHERE source_email='source@example.com'
                      AND folder_name='INBOX'
                    """
                ).fetchone()
                self.assertIsNone(folder["target_folder"])
            finally:
                db.close()

    def test_rule_save_api_validates_target_folder_before_logout(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                cookie = make_session_cookie(db, "admin").split(";", 1)[0]
                form = {
                    "original_email": "source@example.com",
                    "source_email": "source@example.com",
                    "target_email": "target@example.com",
                    "enabled": "1",
                    "account_label": "SYNC:source@example.com",
                    "default_folder_label": "BOX:其他文件夹",
                    "deleted_label": "BOX:已删除邮件",
                    "sync_interval": "",
                    "batch_size": "",
                    "sync_mode": "realtime",
                    "date_policy": "auto",
                    "pop_use_ssl": "1",
                    "folder_count": "1",
                    "folder_name_0": "INBOX",
                    "target_folder_0": "INBOX",
                    "folder_label_0": "BOX:收件箱",
                }
                body = json.dumps(form).encode("utf-8")
                raw = (
                    "\r\n".join(
                        [
                            "POST /api/rules/save HTTP/1.1",
                            "Host: localhost",
                            f"Cookie: {cookie}",
                            "Accept: application/json",
                            "Content-Type: application/json",
                            f"Content-Length: {len(body)}",
                        ]
                    )
                    + "\r\n\r\n"
                ).encode("utf-8") + body
                socket = FakeSocket(raw)
                handler = make_handler(state_path)
                client = mock.Mock()
                client.logged_out = False

                def fake_select(mailbox_client, folder, readonly):
                    self.assertFalse(mailbox_client.logged_out)
                    self.assertEqual(folder, "INBOX")
                    self.assertTrue(readonly)
                    return (1, 2)

                def fake_logout(mailbox_client):
                    mailbox_client.logged_out = True

                with (
                    mock.patch("web.app.connect", return_value=client),
                    mock.patch(
                        "web.app.list_folders",
                        return_value=[FolderInfo("INBOX", "INBOX", ())],
                    ),
                    mock.patch("web.app.select_mailbox", side_effect=fake_select),
                    mock.patch("web.app.safe_logout", side_effect=fake_logout) as safe_logout,
                ):
                    handler(socket, ("127.0.0.1", 0), object())

                status, _, response_body = parse_response(socket.output.getvalue())
                payload = json.loads(response_body)
                self.assertEqual(status, 200)
                self.assertTrue(payload["ok"])
                safe_logout.assert_called_once_with(client)
            finally:
                db.close()

    def test_rule_logs_keep_synced_target_folder_snapshot(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                save_sync_rule(
                    db,
                    original_email="source@example.com",
                    source_email="source@example.com",
                    target_email="target@example.com",
                    enabled=True,
                    account_label="SYNC:source@example.com",
                    default_folder_label="BOX:其他文件夹",
                    deleted_label="BOX:已删除邮件",
                    sync_interval="",
                    batch_size="",
                    skip_deleted_drafts=False,
                    sync_mode="realtime",
                    date_policy="auto",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    folder_rules=[
                        {
                            "folder_name": "INBOX",
                            "target_folder": "Archive",
                            "folder_label": "BOX:收件箱",
                            "excluded": False,
                            "skip_uids": "",
                        },
                    ],
                )
                raw = b"Subject: logged\r\n\r\nbody\r\n"
                message = mail_sync.SourceMessage(
                    source_email="source@example.com",
                    folder="INBOX",
                    uidvalidity=1,
                    uid=10,
                    raw=raw,
                    content_hash=mail_sync.content_hash(raw),
                    message_id="<logged@example.com>",
                    internal_date=None,
                    system_flags=(),
                )
                mail_sync.upsert_state(
                    db,
                    message,
                    2,
                    20,
                    "SYNC:source@example.com",
                    ("BOX:收件箱",),
                    "run-id",
                    sync_action="created",
                    target_folder="Archive",
                )
                db.commit()
                logs = sync_rule_logs(db, "source@example.com")
                self.assertEqual(logs["success"][0]["target_folder"], "Archive")
                self.assertEqual(logs["success"][0]["target_email"], "target@example.com")
                self.assertEqual(logs["success"][0]["source_uid"], 10)
                self.assertEqual(logs["success"][0]["subject"], "logged")

                save_sync_rule(
                    db,
                    original_email="source@example.com",
                    source_email="source@example.com",
                    target_email="target@example.com",
                    enabled=True,
                    account_label="SYNC:source@example.com",
                    default_folder_label="BOX:其他文件夹",
                    deleted_label="BOX:已删除邮件",
                    sync_interval="",
                    batch_size="",
                    skip_deleted_drafts=False,
                    sync_mode="realtime",
                    date_policy="auto",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    folder_rules=[
                        {
                            "folder_name": "INBOX",
                            "target_folder": "Changed",
                            "folder_label": "BOX:收件箱",
                            "excluded": False,
                            "skip_uids": "",
                        },
                    ],
                )
                logs = sync_rule_logs(db, "source@example.com")
                self.assertEqual(logs["success"][0]["target_folder"], "Archive")
                self.assertEqual(logs["success"][0]["subject"], "logged")
            finally:
                db.close()

    def test_sync_rule_logs_note_target_arrival_record_feature(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                save_mailbox_probe_results(
                    db,
                    "target@example.com",
                    "target.example.com",
                    {
                        "features": {
                            "leading_received_header_added": True,
                            "web_display_time_uses_import_time": True,
                        },
                        "feature_groups": {
                            "common": {},
                            "source": {},
                            "target": {
                                "leading_received_header_added": True,
                                "web_display_time_uses_import_time": True,
                            },
                        },
                    },
                )
                raw = b"Subject: arrival\r\n\r\nbody\r\n"
                message = mail_sync.SourceMessage(
                    source_email="source@example.com",
                    folder="INBOX",
                    uidvalidity=1,
                    uid=10,
                    raw=raw,
                    content_hash=mail_sync.content_hash(raw),
                    message_id="<arrival@example.com>",
                    internal_date=None,
                    system_flags=(),
                )
                mail_sync.upsert_state(
                    db,
                    message,
                    2,
                    20,
                    "SYNC:source@example.com",
                    ("BOX:收件箱",),
                    "run-id",
                    sync_action="created",
                    target_folder="INBOX",
                )
                db.commit()

                logs = sync_rule_logs(db, "source@example.com")
                remarks = logs["success"][0]["remarks"]
                self.assertIn("目标补写到达记录", remarks)
                self.assertNotIn("目标网页时间显示导入时间", remarks)
            finally:
                db.close()

    def test_sync_rule_logs_lock_source_and_target_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                save_mailbox_account(
                    db,
                    original_role="",
                    original_email="",
                    role="mailbox",
                    email="second@example.com",
                    enabled=True,
                    auth_provider="manual",
                    imap_server="second.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="secret",
                    oauth_client_id="",
                    oauth_token_file="",
                    target_folder="INBOX",
                    imap_timeout="",
                    proxy_profile_id="",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    service_address="second.example.com",
                )
                save_mailbox_account(
                    db,
                    original_role="",
                    original_email="",
                    role="mailbox",
                    email="archive@example.com",
                    enabled=True,
                    auth_provider="manual",
                    imap_server="archive.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="secret",
                    oauth_client_id="",
                    oauth_token_file="",
                    target_folder="Archive",
                    imap_timeout="",
                    proxy_profile_id="",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    service_address="archive.example.com",
                )
                raw = b"Subject: locked\r\n\r\nbody\r\n"
                message = mail_sync.SourceMessage(
                    source_email="source@example.com",
                    folder="INBOX",
                    uidvalidity=1,
                    uid=10,
                    raw=raw,
                    content_hash=mail_sync.content_hash(raw),
                    message_id="<locked@example.com>",
                    internal_date=None,
                    system_flags=(),
                )
                mail_sync.upsert_state(
                    db,
                    message,
                    2,
                    20,
                    "SYNC:source@example.com",
                    ("BOX:收件箱",),
                    "run-id",
                    sync_action="created",
                    target_folder="INBOX",
                )
                db.commit()

                detail = sync_rules_detail(db, "source@example.com")
                self.assertTrue(detail["selected"]["has_sync_logs"])
                rule_form = (FRONTEND_COMPONENTS / "RuleEditor.vue").read_text(
                    encoding="utf-8"
                )
                self.assertIn("identityLocked", rule_form)

                saved = save_sync_rule(
                    db,
                    original_email="source@example.com",
                    source_email="source@example.com",
                    target_email="target@example.com",
                    enabled=True,
                    account_label="SYNC:source@example.com",
                    default_folder_label="",
                    deleted_label="BOX:已删除邮件",
                    sync_interval="600",
                    batch_size="",
                    skip_deleted_drafts=False,
                    sync_mode="realtime",
                    date_policy="auto",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    folder_rules=[],
                )
                self.assertEqual(saved, "source@example.com")

                with self.assertRaises(mail_sync.SyncError) as source_error:
                    save_sync_rule(
                        db,
                        original_email="source@example.com",
                        source_email="second@example.com",
                        target_email="target@example.com",
                        enabled=True,
                        account_label="SYNC:source@example.com",
                        default_folder_label="",
                        deleted_label="BOX:已删除邮件",
                        sync_interval="",
                        batch_size="",
                        skip_deleted_drafts=False,
                        sync_mode="realtime",
                        date_policy="auto",
                        pop_fallback_enabled=False,
                        pop_server="",
                        pop_port="",
                        pop_use_ssl=True,
                        pop_timeout="",
                        folder_rules=[],
                    )
                self.assertIn("来源邮箱", str(source_error.exception))

                with self.assertRaises(mail_sync.SyncError) as target_error:
                    save_sync_rule(
                        db,
                        original_email="source@example.com",
                        source_email="source@example.com",
                        target_email="archive@example.com",
                        target_service_address="archive.example.com",
                        enabled=True,
                        account_label="SYNC:source@example.com",
                        default_folder_label="",
                        deleted_label="BOX:已删除邮件",
                        sync_interval="",
                        batch_size="",
                        skip_deleted_drafts=False,
                        sync_mode="realtime",
                        date_policy="auto",
                        pop_fallback_enabled=False,
                        pop_server="",
                        pop_port="",
                        pop_use_ssl=True,
                        pop_timeout="",
                        folder_rules=[],
                    )
                self.assertIn("目标邮箱", str(target_error.exception))
            finally:
                db.close()

    def test_delete_sync_rule_removes_its_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                raw = b"Subject: deleted-log\r\n\r\nbody\r\n"
                message = mail_sync.SourceMessage(
                    source_email="source@example.com",
                    folder="INBOX",
                    uidvalidity=1,
                    uid=10,
                    raw=raw,
                    content_hash=mail_sync.content_hash(raw),
                    message_id="<deleted-log@example.com>",
                    internal_date=None,
                    system_flags=(),
                )
                mail_sync.upsert_state(
                    db,
                    message,
                    2,
                    20,
                    "SYNC:source@example.com",
                    ("BOX:收件箱",),
                    "run-id",
                    sync_action="created",
                    target_folder="INBOX",
                )
                mail_sync.upsert_message_location(
                    db,
                    message,
                    "BOX:收件箱",
                    "run-id",
                )
                timestamp = "2026-01-01T00:00:00+00:00"
                pending_raw = b"Subject: pending\r\n\r\nbody\r\n"
                db.execute(
                    """
                    INSERT INTO pending_operations (
                        source_email, content_hash, message_id, source_folder,
                        source_uidvalidity, source_uid, source_flags,
                        internal_date, raw_message, account_label, folder_label,
                        folder_labels_json, old_target_uid, target_folder,
                        target_uidvalidity, target_uid_before, new_target_uid,
                        status, last_error, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?)
                    """,
                    (
                        "source@example.com",
                        mail_sync.content_hash(pending_raw),
                        "<pending@example.com>",
                        "INBOX",
                        1,
                        11,
                        "[]",
                        None,
                        pending_raw,
                        "SYNC:source@example.com",
                        "BOX:收件箱",
                        json.dumps(["BOX:收件箱"], ensure_ascii=False),
                        None,
                        "INBOX",
                        2,
                        20,
                        None,
                        "pending",
                        None,
                        timestamp,
                        timestamp,
                    ),
                )
                db.commit()

                logs = sync_rule_logs(db, "source@example.com")
                self.assertEqual(logs["pagination"]["success"]["total"], 1)
                self.assertEqual(logs["pagination"]["failed"]["total"], 1)

                delete_sync_rule(db, "source@example.com")

                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM messages WHERE source_email=?",
                        ("source@example.com",),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    db.execute(
                        """
                        SELECT COUNT(*)
                        FROM pending_operations
                        WHERE source_email=?
                        """,
                        ("source@example.com",),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    db.execute(
                        """
                        SELECT COUNT(*)
                        FROM message_locations
                        WHERE source_email=?
                        """,
                        ("source@example.com",),
                    ).fetchone()[0],
                    0,
                )
                logs = sync_rule_logs(db, "source@example.com")
                self.assertEqual(logs["pagination"]["success"]["total"], 0)
                self.assertEqual(logs["pagination"]["failed"]["total"], 0)
            finally:
                db.close()

    def test_delete_selected_sync_rule_logs(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                raw = b"Subject: selected-success\r\n\r\nbody\r\n"
                success_hash = mail_sync.content_hash(raw)
                success_mail_time = "2025-05-04T10:11:12+00:00"
                failed_mail_time = "2025-05-05T12:13:14+00:00"
                message = mail_sync.SourceMessage(
                    source_email="source@example.com",
                    folder="INBOX",
                    uidvalidity=1,
                    uid=10,
                    raw=raw,
                    content_hash=success_hash,
                    message_id="<selected-success@example.com>",
                    internal_date=datetime(2025, 5, 4, 10, 11, 12, tzinfo=timezone.utc),
                    system_flags=(),
                )
                mail_sync.upsert_state(
                    db,
                    message,
                    2,
                    20,
                    "SYNC:source@example.com",
                    ("BOX:收件箱",),
                    "run-id",
                    sync_action="created",
                    target_folder="INBOX",
                )
                mail_sync.upsert_message_location(
                    db,
                    message,
                    "BOX:收件箱",
                    "run-id",
                )

                timestamp = "2026-01-01T00:00:00+00:00"
                pending_raw = b"Subject: selected-failed\r\n\r\nbody\r\n"
                failed_hash = mail_sync.content_hash(pending_raw)
                db.execute(
                    """
                    INSERT INTO pending_operations (
                        source_email, content_hash, message_id, source_folder,
                        source_uidvalidity, source_uid, source_flags,
                        internal_date, raw_message, account_label, folder_label,
                        folder_labels_json, old_target_uid, target_folder,
                        target_uidvalidity, target_uid_before, new_target_uid,
                        status, last_error, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                            ?, ?, ?, ?)
                    """,
                    (
                        "source@example.com",
                        failed_hash,
                        "<selected-failed@example.com>",
                        "INBOX",
                        1,
                        11,
                        "[]",
                        failed_mail_time,
                        pending_raw,
                        "SYNC:source@example.com",
                        "BOX:收件箱",
                        json.dumps(["BOX:收件箱"], ensure_ascii=False),
                        None,
                        "INBOX",
                        2,
                        20,
                        None,
                        "pending",
                        "network timeout",
                        timestamp,
                        timestamp,
                    ),
                )
                db.commit()

                logs = sync_rule_logs(db, "source@example.com")
                self.assertEqual(logs["success"][0]["internal_date"], success_mail_time)
                self.assertEqual(logs["failed"][0]["internal_date"], failed_mail_time)

                filtered = sync_rule_logs(
                    db,
                    "source@example.com",
                    keyword="selected-success",
                )
                self.assertEqual(filtered["pagination"]["success"]["total"], 1)
                self.assertEqual(filtered["pagination"]["failed"]["total"], 0)
                self.assertEqual(filtered["success"][0]["content_hash"], success_hash)

                cookie_header = make_session_cookie(db, "admin").split(";", 1)[0]
                body = json.dumps(
                    {
                        "source_email": "source@example.com",
                        "success_hashes": [success_hash],
                        "failed_hashes": [failed_hash],
                    }
                ).encode("utf-8")
                raw = (
                    "\r\n".join(
                        [
                            "POST /api/rules/logs/delete HTTP/1.1",
                            "Host: localhost",
                            f"Cookie: {cookie_header}",
                            "Content-Type: application/json",
                            f"Content-Length: {len(body)}",
                        ]
                    )
                    + "\r\n\r\n"
                ).encode("utf-8") + body
                socket = FakeSocket(raw)
                make_handler(state_path)(socket, ("127.0.0.1", 0), object())
                status, _, response_body = parse_response(
                    socket.output.getvalue()
                )
                self.assertEqual(status, 200)
                result = json.loads(response_body)
                self.assertEqual(result["deleted_success"], 1)
                self.assertEqual(result["deleted_failed"], 1)
                self.assertEqual(
                    db.execute(
                        "SELECT COUNT(*) FROM messages WHERE source_email=?",
                        ("source@example.com",),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    db.execute(
                        """
                        SELECT COUNT(*)
                        FROM pending_operations
                        WHERE source_email=?
                        """,
                        ("source@example.com",),
                    ).fetchone()[0],
                    0,
                )
                self.assertEqual(
                    db.execute(
                        """
                        SELECT COUNT(*)
                        FROM message_locations
                        WHERE source_email=?
                        """,
                        ("source@example.com",),
                    ).fetchone()[0],
                    0,
                )
            finally:
                db.close()

    def test_rule_pop_fallback_toggle_uses_source_mailbox_pop(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())

                save_sync_rule(
                    db,
                    original_email="source@example.com",
                    source_email="source@example.com",
                    target_email="target@example.com",
                    enabled=True,
                    account_label="",
                    default_folder_label="",
                    deleted_label="BOX:已删除邮件",
                    sync_interval="",
                    batch_size="",
                    skip_deleted_drafts=False,
                    sync_mode="realtime",
                    date_policy="auto",
                    pop_fallback_enabled=True,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    folder_rules=[],
                )

                detail = sync_rules_detail(db, "source@example.com")
                self.assertEqual(
                    detail["selected"]["account_label"],
                    "",
                )
                self.assertEqual(detail["selected"]["default_folder_label"], "")
                self.assertFalse(detail["selected"]["pop_fallback_available"])
                self.assertFalse(detail["selected"]["pop_fallback_enabled"])
                config = load_runtime_config(db, state_path)
                self.assertNotIn("pop_fallback", config["sources"][0])

                save_mailbox_account(
                    db,
                    original_role="source",
                    original_email="source@example.com",
                    original_service_address="source.example.com",
                    role="source",
                    email="source@example.com",
                    enabled=True,
                    auth_provider="manual",
                    imap_server="source.example.com",
                    imap_port="993",
                    use_ssl=True,
                    password="",
                    oauth_client_id="",
                    oauth_token_file="",
                    target_folder="INBOX",
                    imap_timeout="",
                    proxy_profile_id="",
                    pop_fallback_enabled=True,
                    pop_server="pop.source.example.com",
                    pop_port="995",
                    pop_use_ssl=True,
                    pop_timeout="120",
                    service_address="source.example.com",
                    imap_enabled=True,
                )
                save_sync_rule(
                    db,
                    original_email="source@example.com",
                    source_email="source@example.com",
                    target_email="target@example.com",
                    enabled=True,
                    account_label="",
                    default_folder_label="",
                    deleted_label="BOX:已删除邮件",
                    sync_interval="",
                    batch_size="",
                    skip_deleted_drafts=False,
                    sync_mode="realtime",
                    date_policy="auto",
                    pop_fallback_enabled=True,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    folder_rules=[],
                )

                detail = sync_rules_detail(db, "source@example.com")
                self.assertTrue(detail["selected"]["pop_fallback_available"])
                self.assertTrue(detail["selected"]["pop_fallback_enabled"])
                config = load_runtime_config(db, state_path)
                self.assertEqual(
                    config["sources"][0]["pop_fallback"]["server"],
                    "pop.source.example.com",
                )

                save_sync_rule(
                    db,
                    original_email="source@example.com",
                    source_email="source@example.com",
                    target_email="target@example.com",
                    enabled=True,
                    account_label="",
                    default_folder_label="",
                    deleted_label="BOX:已删除邮件",
                    sync_interval="",
                    batch_size="",
                    skip_deleted_drafts=False,
                    sync_mode="realtime",
                    date_policy="auto",
                    pop_fallback_enabled=False,
                    pop_server="",
                    pop_port="",
                    pop_use_ssl=True,
                    pop_timeout="",
                    folder_rules=[],
                )

                detail = sync_rules_detail(db, "source@example.com")
                self.assertTrue(detail["selected"]["pop_fallback_available"])
                self.assertFalse(detail["selected"]["pop_fallback_enabled"])
                config = load_runtime_config(db, state_path)
                self.assertNotIn("pop_fallback", config["sources"][0])
                mailbox = target_mailbox_detail(
                    db,
                    "source@example.com",
                    "source",
                    "edit",
                    "source.example.com",
                )["mailbox"]
                self.assertEqual(mailbox["pop_server"], "pop.source.example.com")
            finally:
                db.close()

    def test_legacy_global_labels_migrate_to_target_mailbox(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                db.execute("ALTER TABLE mail_label RENAME TO labels_new")
                db.execute(
                    """
                    CREATE TABLE synology_labels (
                        label_id TEXT PRIMARY KEY,
                        label_name TEXT NOT NULL UNIQUE
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO synology_labels(label_id, label_name)
                    SELECT label_id, label_name FROM labels_new
                    """
                )
                db.execute("DROP TABLE labels_new")
                db.execute("PRAGMA user_version=3")
                db.commit()
            finally:
                db.close()

            db = mail_sync.init_db(state_path)
            try:
                self.assertIsNone(
                    db.execute(
                        """
                        SELECT 1
                        FROM sqlite_master
                        WHERE type='table' AND name='synology_labels'
                        """
                    ).fetchone()
                )
                self.assertIsNotNone(
                    db.execute(
                        """
                        SELECT 1
                        FROM sqlite_master
                        WHERE type='table' AND name='mail_label'
                        """
                    ).fetchone()
                )
                rows = list_synology_labels(db, "target@example.com")
                self.assertTrue(rows)
                self.assertTrue(
                    all(row["target_email"] == "target@example.com" for row in rows)
                )
                config = load_runtime_config(db, state_path)
                self.assertEqual(
                    config["synology_labels"]["$account"],
                    "SYNC:source@example.com",
                )
            finally:
                db.close()

    def test_legacy_mail_accounts_gain_service_identity(self):
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state.sqlite3"
            db = mail_sync.init_db(state_path)
            try:
                import_legacy_config(db, self.valid_config())
                db.execute("ALTER TABLE mail_accounts RENAME TO mail_accounts_new")
                db.execute(
                    """
                    CREATE TABLE mail_accounts (
                        role TEXT NOT NULL CHECK(role IN ('target', 'source')),
                        email TEXT NOT NULL,
                        enabled INTEGER NOT NULL DEFAULT 1,
                        imap_server TEXT NOT NULL,
                        imap_port INTEGER,
                        use_ssl INTEGER NOT NULL DEFAULT 1,
                        password TEXT,
                        auth_json TEXT,
                        proxy_profile_id INTEGER,
                        imap_proxy_json TEXT,
                        imap_id_json TEXT,
                        pop_fallback_json TEXT,
                        imap_timeout INTEGER,
                        target_folder TEXT,
                        extra_json TEXT,
                        sort_order INTEGER NOT NULL DEFAULT 0,
                        PRIMARY KEY(role, email)
                    )
                    """
                )
                db.execute(
                    """
                    INSERT INTO mail_accounts(
                        role, email, enabled, imap_server, imap_port, use_ssl,
                        password, auth_json, proxy_profile_id, imap_proxy_json,
                        imap_id_json, pop_fallback_json, imap_timeout,
                        target_folder, extra_json, sort_order
                    )
                    SELECT
                           CASE
                               WHEN email='target@example.com' THEN 'target'
                               ELSE 'source'
                           END,
                           email, enabled, imap_server, imap_port, use_ssl,
                           password, auth_json, proxy_profile_id, imap_proxy_json,
                           imap_id_json, pop_fallback_json, imap_timeout,
                           target_folder, extra_json, sort_order
                    FROM mail_accounts_new
                    """
                )
                db.execute("DROP TABLE mail_accounts_new")
                db.execute(
                    """
                    UPDATE source_rules
                    SET extra_json=?
                    WHERE email='source@example.com'
                    """,
                    (json.dumps({"target_email": "target@example.com"}),),
                )
                db.execute("PRAGMA user_version=3")
                db.commit()
            finally:
                db.close()

            db = mail_sync.init_db(state_path)
            try:
                columns = {
                    row["name"]
                    for row in db.execute("PRAGMA table_info(mail_accounts)")
                }
                self.assertIn("service_address", columns)
                self.assertIn("protocols_json", columns)
                self.assertIn("smtp_json", columns)
                self.assertNotIn("role", columns)
                rule = db.execute(
                    """
                    SELECT extra_json
                    FROM source_rules
                    WHERE email='source@example.com'
                    """
                ).fetchone()
                rule_extra = json.loads(rule["extra_json"])
                self.assertEqual(
                    rule_extra["source_service_address"],
                    "source.example.com",
                )
                self.assertEqual(
                    rule_extra["target_service_address"],
                    "target.example.com",
                )
                detail = target_mailbox_detail(db, "target@example.com")
                self.assertEqual(
                    detail["mailbox"]["service_address"],
                    "target.example.com",
                )
                self.assertTrue(
                    all(
                        row["target_service_address"] == "target.example.com"
                        for row in detail["labels"]
                    )
                )
            finally:
                db.close()


if __name__ == "__main__":
    unittest.main()
