from __future__ import annotations

import base64
import binascii
import hashlib
import html
import ipaddress
import json
import imaplib
import math
import os
import poplib
import secrets
import sqlite3
import threading
import time
from collections import deque
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlencode, urlparse

from auth.oauth import (
    microsoft_oauth_scope,
    microsoft_oauth_urls,
    normalized_token_response,
    oauth_error_message,
    post_form_json,
)
from common.clock import now_iso
from common.errors import SyncError
from config.validation import MICROSOFT_IMAP_SCOPE
from connection import connect, connect_pop, safe_logout, safe_pop_quit
from imap.labels import discover_target_labels
from imap.probe import run_imap_mailbox_probe
from imap.protocol import select_mailbox
from imap.source import list_folders
from monitoring.listener import idle_supported
from storage.config_repository import (
    delete_mailbox_account,
    delete_proxy_profile,
    delete_sync_rule,
    delete_sync_rule_logs,
    delete_synology_label,
    list_proxy_profiles,
    list_synology_labels,
    mailbox_account_profile,
    save_mailbox_account,
    save_mailbox_probe_results,
    save_proxy_profile,
    save_sync_rule,
    set_sync_rule_enabled,
    sync_rule_logs,
    source_account_profile,
    sync_rules_detail,
    target_mailbox_detail,
    update_synology_label,
)
from storage.state import connect_db, init_db
from storage.users import authenticate_user, find_user_by_username
from web import session


HTML_DIR = Path(__file__).resolve().parents[2] / "html"
MICROSOFT_NATIVE_REDIRECT_URI = (
    "https://login.microsoftonline.com/common/oauth2/nativeclient"
)
STATIC_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".html": "text/html; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".mjs": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".gif": "image/gif",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
}
HEARTBEAT_MAX_AGE_SECONDS = 600
HEARTBEAT_MAX_AGE_ENV = "MAIL_SYNC_HEARTBEAT_MAX_AGE_SECONDS"
HEARTBEAT_FILENAME = "watch.heartbeat"
TRUSTED_PROXY_CIDRS_ENV = "MAIL_SYNC_TRUSTED_PROXY_CIDRS"
DEFAULT_TRUSTED_PROXY_CIDRS = "127.0.0.0/8,::1/128"
LOGIN_FAILURE_LIMIT = 5
LOGIN_FAILURE_WINDOW_SECONDS = 5 * 60
LOGIN_FAILURE_CACHE_SIZE = 4096
CONTENT_SECURITY_POLICY = (
    "default-src 'self'; "
    "base-uri 'self'; "
    "object-src 'none'; "
    "frame-ancestors 'none'; "
    "script-src 'self'; "
    "style-src 'self' 'unsafe-inline'; "
    "img-src 'self' data:; "
    "font-src 'self' data:; "
    "connect-src 'self'; "
    "form-action 'self'"
)


def _content_security_policy(script_nonce: str | None = None) -> str:
    if not script_nonce:
        return CONTENT_SECURITY_POLICY
    return CONTENT_SECURITY_POLICY.replace(
        "script-src 'self';",
        f"script-src 'self' 'nonce-{script_nonce}';",
    )


def _environment_flag(name: str) -> bool:
    return os.environ.get(name, "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }


def _heartbeat_max_age_seconds() -> int:
    raw_value = os.environ.get(HEARTBEAT_MAX_AGE_ENV, "").strip()
    if not raw_value:
        return HEARTBEAT_MAX_AGE_SECONDS
    try:
        value = int(raw_value)
    except ValueError:
        return HEARTBEAT_MAX_AGE_SECONDS
    return value if value > 0 else HEARTBEAT_MAX_AGE_SECONDS


def _heartbeat_path(state_path: Path) -> Path:
    configured = os.environ.get("MAIL_SYNC_HEARTBEAT_PATH", "").strip()
    path = (
        Path(configured).expanduser()
        if configured
        else state_path.with_name(HEARTBEAT_FILENAME)
    )
    return path if path.is_absolute() else path.resolve()


def _liveness_snapshot(state_path: Path) -> tuple[dict[str, Any], int]:
    checks: dict[str, dict[str, Any]] = {
        "web": {"status": "ok"},
    }
    healthy = True

    try:
        database_uri = f"{state_path.resolve().as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(
                database_uri,
                uri=True,
                timeout=5,
            )
        ) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=5000")
            db.execute("SELECT 1").fetchone()
        checks["database"] = {"status": "ok"}
    except (OSError, sqlite3.Error, TypeError, ValueError):
        healthy = False
        checks["database"] = {"status": "error"}

    max_age_seconds = _heartbeat_max_age_seconds()
    try:
        modified_at = _heartbeat_path(state_path).stat().st_mtime
        age_seconds = max(0.0, time.time() - modified_at)
        heartbeat_status = "ok" if age_seconds <= max_age_seconds else "stale"
        checks["heartbeat"] = {
            "status": heartbeat_status,
            "age_seconds": round(age_seconds, 1),
            "max_age_seconds": max_age_seconds,
        }
        if heartbeat_status != "ok":
            healthy = False
    except FileNotFoundError:
        healthy = False
        checks["heartbeat"] = {
            "status": "missing",
            "max_age_seconds": max_age_seconds,
        }
    except OSError:
        healthy = False
        checks["heartbeat"] = {
            "status": "unavailable",
            "max_age_seconds": max_age_seconds,
        }

    status = "healthy" if healthy else "unhealthy"
    return {
        "status": status,
        "healthy": healthy,
        "checks": checks,
    }, 200 if healthy else 503


def _health_snapshot(state_path: Path) -> tuple[dict[str, Any], int]:
    live_data, _ = _liveness_snapshot(state_path)
    checks = dict(live_data["checks"])
    healthy = bool(live_data["healthy"])

    try:
        database_uri = f"{state_path.resolve().as_uri()}?mode=ro"
        with closing(
            sqlite3.connect(
                database_uri,
                uri=True,
                timeout=5,
            )
        ) as db:
            db.execute("PRAGMA query_only=ON")
            db.execute("PRAGMA busy_timeout=5000")
            enabled_rules, failed_rules = db.execute(
                """
                SELECT COUNT(*),
                       COALESCE(SUM(
                           CASE WHEN COALESCE(last_sync_status, '')='ok'
                                THEN 0 ELSE 1 END
                       ), 0)
                FROM source_rules
                WHERE enabled=1
                """
            ).fetchone()
            pending_count = int(
                db.execute("SELECT COUNT(*) FROM pending_operations").fetchone()[0]
            )
        enabled_rules = int(enabled_rules)
        failed_rules = int(failed_rules)
        checks["rules"] = {
            "status": "ok" if failed_rules == 0 else "error",
            "enabled": enabled_rules,
            "failed": failed_rules,
        }
        checks["pending"] = {
            "status": "ok" if pending_count == 0 else "error",
            "count": pending_count,
        }
        if failed_rules or pending_count:
            healthy = False
    except (OSError, sqlite3.Error, TypeError, ValueError):
        healthy = False
        checks["database"] = {"status": "error"}
        checks["rules"] = {"status": "unavailable"}
        checks["pending"] = {"status": "unavailable"}

    status = "healthy" if healthy else "unhealthy"
    return {
        "status": status,
        "healthy": healthy,
        "checks": checks,
    }, 200 if healthy else 503


def _normalized_ip_address(value: str):
    address = ipaddress.ip_address(value)
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped:
        return address.ipv4_mapped
    return address


def _trusted_proxy_networks() -> list:
    raw = os.environ.get(
        TRUSTED_PROXY_CIDRS_ENV,
        DEFAULT_TRUSTED_PROXY_CIDRS,
    )
    networks = []
    for value in raw.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            networks.append(ipaddress.ip_network(value, strict=False))
        except ValueError:
            continue
    return networks


def _is_trusted_proxy(address, networks: list) -> bool:
    return any(
        address.version == network.version and address in network
        for network in networks
    )


def _request_client_ip(client_address: object, forwarded_for: str) -> str:
    raw_peer = (
        str(client_address[0])
        if isinstance(client_address, tuple) and client_address
        else "unknown"
    )
    try:
        peer = _normalized_ip_address(raw_peer)
    except ValueError:
        return raw_peer[:128]
    if not _environment_flag("MAIL_SYNC_TRUST_PROXY"):
        return str(peer)

    trusted_networks = _trusted_proxy_networks()
    if not _is_trusted_proxy(peer, trusted_networks):
        return str(peer)

    forwarded_addresses = []
    for value in forwarded_for.split(","):
        value = value.strip()
        if not value:
            continue
        try:
            forwarded_addresses.append(_normalized_ip_address(value))
        except ValueError:
            return str(peer)
    for address in reversed(forwarded_addresses):
        if _is_trusted_proxy(address, trusted_networks):
            continue
        return str(address)
    return str(peer)


def create_web_server(
    state_path: Path,
    host: str,
    port: int,
) -> ThreadingHTTPServer:
    handler = make_handler(state_path)
    return ThreadingHTTPServer((host, port), handler)


def serve_web(state_path: Path, host: str, port: int) -> int:
    db = init_db(state_path)
    db.close()
    server = create_web_server(state_path, host, port)
    print(
        json.dumps(
            {
                "event": "web_server_started",
                "host": host,
                "port": port,
                "state_db": str(state_path),
            },
            ensure_ascii=False,
        ),
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


def make_handler(state_path: Path) -> type[BaseHTTPRequestHandler]:
    login_failures: dict[tuple[str, str], deque[float]] = {}
    login_failures_lock = threading.Lock()

    class MailSyncWebHandler(BaseHTTPRequestHandler):
        server_version = "MailSyncWeb/1.0"

        def do_GET(self) -> None:
            self._begin_request_log()
            path = urlparse(self.path).path
            if path == "/api/live":
                data, status = _liveness_snapshot(state_path)
                self._send_json(data, status=status)
                return
            if path == "/api/health":
                data, status = _health_snapshot(state_path)
                self._send_json(data, status=status)
                return
            if path.startswith("/assets/") or path in {"/favicon.ico"}:
                self._serve_file(path.lstrip("/"))
                return
            if path == "/logout":
                self._redirect("/#/login", cookies=[session.clear_session_cookie()])
                return
            if path == "/outlook/callback":
                with closing(self._db()) as db:
                    self._serve_outlook_callback(db)
                return

            with closing(self._db()) as db:
                username = self._current_username(db)
                if path == "/api/session":
                    self._send_json(
                        {
                            "authenticated": username is not None,
                            "username": username,
                        }
                    )
                    return
                if path == "/login":
                    if username is not None:
                        self._redirect("/#/rules")
                        return
                    self._serve_app()
                    return
                if username is None:
                    if path.startswith("/api/"):
                        self._send_json(
                            {"error": "请先登录。"},
                            status=401,
                        )
                        return
                    self._serve_app()
                    return
                self._serve_authenticated_get(db, username, path)

        def do_POST(self) -> None:
            self._begin_request_log()
            path = urlparse(self.path).path
            try:
                fields = self._read_fields()
            except SyncError as exc:
                self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            self._request_log_context = self._fields_log_context(path, fields)
            if path not in {"/login", "/api/login"}:
                with closing(self._db()) as db:
                    username = self._current_username(db)
                    if username is None:
                        self._redirect("/login")
                        return
                    self._serve_authenticated_post(db, path, fields)
                return
            username = self._field(fields, "username").strip()
            password = self._field(fields, "password")
            wants_json = path == "/api/login" or self._wants_json()
            retry_after = self._login_retry_after(username)
            if retry_after > 0:
                self._send_login_rate_limited(retry_after)
                return
            with closing(self._db()) as db:
                user = authenticate_user(db, username, password)
                if user is None:
                    retry_after = self._record_login_failure(username)
                    if retry_after > 0:
                        self._send_login_rate_limited(retry_after)
                        return
                    if wants_json:
                        self._send_json(
                            {
                                "ok": False,
                                "error": "用户名或密码不正确。",
                            },
                            status=401,
                        )
                        return
                    self._redirect("/login?error=invalid_credentials")
                    return
                self._clear_login_failures(username)
                cookie = session.make_session_cookie(db, user["username"])
            if wants_json:
                self._send_json(
                    {
                        "ok": True,
                        "username": user["username"],
                        "redirect_url": "/#/rules",
                    },
                    cookies=[cookie],
                )
                return
            self._redirect("/#/rules", cookies=[cookie])

        def log_message(self, format: str, *args: object) -> None:
            return

        def _begin_request_log(self) -> None:
            self._request_started_at = time.monotonic()
            self._request_log_context = {}

        def _login_key(self, username: str) -> tuple[str, str]:
            client_ip = _request_client_ip(
                getattr(self, "client_address", None),
                self.headers.get("X-Forwarded-For", ""),
            )
            return client_ip[:128], username.strip().casefold()[:256]

        def _login_retry_after(self, username: str) -> int:
            now = time.monotonic()
            key = self._login_key(username)
            with login_failures_lock:
                self._prune_login_failures(now)
                attempts = login_failures.get(key)
                if attempts is None or len(attempts) < LOGIN_FAILURE_LIMIT:
                    return 0
                return max(
                    1,
                    math.ceil(
                        LOGIN_FAILURE_WINDOW_SECONDS - (now - attempts[0])
                    ),
                )

        def _record_login_failure(self, username: str) -> int:
            now = time.monotonic()
            key = self._login_key(username)
            cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
            with login_failures_lock:
                self._prune_login_failures(now)
                attempts = login_failures.setdefault(key, deque())
                while attempts and attempts[0] <= cutoff:
                    attempts.popleft()
                attempts.append(now)
                if len(attempts) < LOGIN_FAILURE_LIMIT:
                    return 0
                return max(
                    1,
                    math.ceil(
                        LOGIN_FAILURE_WINDOW_SECONDS - (now - attempts[0])
                    ),
                )

        def _clear_login_failures(self, username: str) -> None:
            key = self._login_key(username)
            with login_failures_lock:
                login_failures.pop(key, None)

        def _prune_login_failures(self, now: float) -> None:
            cutoff = now - LOGIN_FAILURE_WINDOW_SECONDS
            for key, attempts in list(login_failures.items()):
                while attempts and attempts[0] <= cutoff:
                    attempts.popleft()
                if not attempts:
                    login_failures.pop(key, None)
            if len(login_failures) < LOGIN_FAILURE_CACHE_SIZE:
                return
            oldest_key = min(
                login_failures,
                key=lambda key: login_failures[key][-1],
            )
            login_failures.pop(oldest_key, None)

        def _send_login_rate_limited(self, retry_after: int) -> None:
            self._send_json(
                {
                    "ok": False,
                    "error": "登录尝试过于频繁，请稍后重试。",
                },
                status=429,
                headers={"Retry-After": str(retry_after)},
            )

        def _fields_log_context(
            self,
            path: str,
            fields: dict[str, list[str]],
        ) -> dict[str, Any]:
            context: dict[str, Any] = {}
            if path == "/api/rules/save":
                context.update(
                    {
                        "source_email": self._field(fields, "source_email"),
                        "target_email": self._field(fields, "target_email"),
                        "source_service": self._field(
                            fields,
                            "source_service_address",
                        ),
                        "target_service": self._field(
                            fields,
                            "target_service_address",
                        ),
                        "folder_count": self._field(fields, "folder_count"),
                    }
                )
            elif path in {"/api/rules/toggle", "/api/rules/delete"}:
                context["source_email"] = self._field(fields, "source_email")
            elif path.startswith("/api/rules/"):
                for key in ("source_email", "email", "target_email", "role"):
                    value = self._field(fields, key)
                    if value:
                        context[key] = value
            elif path.startswith("/api/mail/"):
                for key in ("email", "original_email", "role"):
                    value = self._field(fields, key)
                    if value:
                        context[key] = value
            return {
                key: value
                for key, value in context.items()
                if str(value or "").strip()
            }

        def _serve_authenticated_get(
            self,
            db: sqlite3.Connection,
            username: str,
            path: str,
        ) -> None:
            redirects = {
                "/accounts": "/#/mail",
                "/proxies": "/#/config",
                "/labels": "/#/mail",
            }
            if path in redirects:
                self._redirect(redirects[path])
                return
            if path == "/target":
                query = urlparse(self.path).query
                location = "/#/mail" + (f"?{query}" if query else "")
                self._redirect(location)
                return
            if path == "/api/rules/logs":
                try:
                    self._send_json(
                        {
                            "ok": True,
                            **sync_rule_logs(
                                db,
                                self._query_value("source")
                                or self._query_value("source_email")
                                or "",
                                success_page=self._query_int("success_page", 1),
                                failed_page=self._query_int("failed_page", 1),
                                page_size=self._query_int("page_size", 10),
                                keyword=self._query_value("keyword")
                                or self._query_value("filter")
                                or "",
                            ),
                        }
                    )
                except SyncError as exc:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)
                return
            if path == "/api/rules":
                rule_dialog = self._query_value("dialog")
                rule_error = self._query_value("error")
                selected_source = (
                    self._query_value("source")
                    if rule_dialog == "detail" or rule_error
                    else None
                )
                selected_target = (
                    self._query_value("target")
                    if rule_dialog == "detail" or rule_error
                    else None
                )
                self._send_json(
                    {
                        "username": username,
                        "notice": self._notice_message(
                            self._query_value("notice")
                        ),
                        "error": self._query_value("error"),
                        "detail": sync_rules_detail(
                            db,
                            selected_source,
                            selected_target,
                            self._query_value("source_service"),
                            self._query_value("target_service"),
                        ),
                    }
                )
                return
            if path == "/api/mail":
                detail = target_mailbox_detail(
                    db,
                    self._query_value("email")
                    or self._query_value("target"),
                    None,
                    self._query_value("dialog"),
                    self._query_value("service"),
                )
                detail["outlook_defaults"] = self._outlook_defaults(db)
                self._send_json(
                    {
                        "username": username,
                        "notice": self._notice_message(
                            self._query_value("notice")
                        ),
                        "error": self._query_value("error"),
                        "detail": detail,
                    }
                )
                return
            if path == "/api/config":
                self._send_json(
                    {
                        "username": username,
                        "notice": self._notice_message(
                            self._query_value("notice")
                        ),
                        "error": self._query_value("error"),
                        "outlook_login": self._outlook_login_settings(db),
                        "proxies": list_proxy_profiles(db),
                    }
                )
                return
            if path in {"/", "/rules", "/mail", "/config"}:
                self._serve_app()
                return
            if path.startswith("/api/"):
                self._send_json({"error": "接口不存在。"}, status=404)
                return
            self._serve_app(status=404)

        def _serve_authenticated_post(
            self,
            db: sqlite3.Connection,
            path: str,
            fields: dict[str, list[str]],
        ) -> None:
            action_path = path[4:] if path.startswith("/api/") else path
            api_action = path.startswith("/api/")

            def finish(location: str) -> None:
                if api_action:
                    self._send_json({"ok": True, "redirect_url": location})
                    return
                self._redirect(location)

            try:
                if action_path == "/rules/save":
                    folder_rules = self._folder_rule_fields(fields)
                    self._validate_rule_target_folders(
                        db,
                        self._field(fields, "target_email"),
                        self._field(fields, "target_service_address"),
                        folder_rules,
                    )
                    source_email = save_sync_rule(
                        db,
                        original_email=self._field(fields, "original_email"),
                        source_email=self._field(fields, "source_email"),
                        target_email=self._field(fields, "target_email"),
                        source_service_address=self._field(
                            fields,
                            "source_service_address",
                        ),
                        target_service_address=self._field(
                            fields,
                            "target_service_address",
                        ),
                        enabled=self._checked(fields, "enabled"),
                        account_label=self._field(fields, "account_label"),
                        default_folder_label=self._field(
                            fields,
                            "default_folder_label",
                        ),
                        deleted_label=self._field(fields, "deleted_label"),
                        sync_interval=self._field(fields, "sync_interval"),
                        batch_size=self._field(fields, "batch_size"),
                        skip_deleted_drafts=self._checked(
                            fields,
                            "skip_deleted_drafts",
                        ),
                        sync_mode=self._field(fields, "sync_mode"),
                        date_policy=self._field(fields, "date_policy"),
                        out_of_scope_policy=self._field(
                            fields,
                            "out_of_scope_policy",
                        ),
                        out_of_scope_label=fields.get("out_of_scope_label", []),
                        pop_fallback_enabled=self._checked(
                            fields,
                            "pop_fallback_enabled",
                        ),
                        pop_server=self._field(fields, "pop_server"),
                        pop_port=self._field(fields, "pop_port"),
                        pop_use_ssl=self._checked(fields, "pop_use_ssl"),
                        pop_timeout=self._field(fields, "pop_timeout"),
                        folder_rules=folder_rules,
                    )
                    finish("/#/rules?notice=rule_saved")
                    return
                if action_path == "/rules/toggle":
                    source_email = self._field(fields, "source_email")
                    set_sync_rule_enabled(
                        db,
                        source_email,
                        self._checked(fields, "enabled"),
                    )
                    notice = (
                        "rule_resumed"
                        if self._checked(fields, "enabled")
                        else "rule_paused"
                    )
                    finish(f"/#/rules?notice={notice}")
                    return
                if action_path == "/rules/delete":
                    delete_sync_rule(db, self._field(fields, "source_email"))
                    finish("/#/rules?notice=rule_deleted")
                    return
                if action_path == "/rules/logs/delete":
                    self._send_json(
                        {
                            "ok": True,
                            **delete_sync_rule_logs(
                                db,
                                self._field(fields, "source_email"),
                                fields.get("success_hashes", []),
                                fields.get("failed_hashes", []),
                            ),
                        }
                    )
                    return
                if action_path == "/rules/folders":
                    result = self._mailbox_folder_list(
                        db,
                        self._field(fields, "role"),
                        self._field(fields, "email"),
                        self._field(fields, "service_address"),
                    )
                    self._send_json(result)
                    return
                if action_path == "/rules/test-source":
                    source_email = self._field(fields, "source_email")
                    result = self._test_source_connection(db, source_email)
                    self._send_json(result)
                    return
                if action_path == "/mail/account/save":
                    mailbox_email = save_mailbox_account(
                        db,
                        original_role=self._field(fields, "original_role"),
                        original_email=self._field(fields, "original_email"),
                        role=self._field(fields, "role"),
                        email=self._field(fields, "email"),
                        enabled=True,
                        auth_provider=self._field(fields, "auth_provider"),
                        imap_server=self._field(fields, "imap_server"),
                        imap_port=self._field(fields, "imap_port"),
                        use_ssl=self._checked(fields, "use_ssl"),
                        password=self._field(fields, "password"),
                        oauth_client_id=self._field(fields, "oauth_client_id"),
                        oauth_token_file=self._field(fields, "oauth_token_file"),
                        target_folder=self._field(fields, "target_folder"),
                        imap_timeout=self._field(fields, "imap_timeout"),
                        proxy_profile_id=self._field(fields, "proxy_profile_id"),
                        pop_fallback_enabled=self._checked(
                            fields,
                            "pop_fallback_enabled",
                        ),
                        pop_server=self._field(fields, "pop_server"),
                        pop_port=self._field(fields, "pop_port"),
                        pop_use_ssl=self._checked(fields, "pop_use_ssl"),
                        pop_timeout=self._field(fields, "pop_timeout"),
                        original_service_address=self._field(
                            fields,
                            "original_service_address",
                        ),
                        service_address=self._field(fields, "service_address"),
                        imap_enabled=self._checked(fields, "imap_enabled"),
                        smtp_enabled=self._checked(fields, "smtp_enabled"),
                        smtp_server=self._field(fields, "smtp_server"),
                        smtp_port=self._field(fields, "smtp_port"),
                        smtp_use_ssl=self._checked(fields, "smtp_use_ssl"),
                        smtp_timeout=self._field(fields, "smtp_timeout"),
                        probe_results_json=self._field(
                            fields,
                            "probe_results_json",
                        ),
                        realtime_poll_interval=self._field(
                            fields,
                            "realtime_poll_interval",
                        ),
                    )
                    mailbox_service = (
                        self._field(fields, "service_address")
                        or self._field(fields, "imap_server")
                    )
                    finish(
                        f"/#/mail?email={quote(mailbox_email)}"
                        f"&service={quote(mailbox_service)}"
                        f"&notice=mailbox_saved"
                    )
                    return
                if action_path == "/mail/account/delete":
                    mailbox_email = self._field(fields, "email")
                    delete_mailbox_account(
                        db,
                        role=self._field(fields, "role"),
                        email=mailbox_email,
                        service_address=self._field(fields, "service_address"),
                    )
                    finish("/#/mail?notice=mailbox_deleted")
                    return
                if action_path == "/mail/labels/discover":
                    target_email = self._field(fields, "target_email").strip()
                    target_service = self._field(fields, "target_service_address").strip()
                    profile = mailbox_account_profile(
                        db,
                        "target",
                        target_email,
                        target_service,
                    )
                    known_names = {
                        row["label_id"]: row["label_name"]
                        for row in list_synology_labels(
                            db,
                            target_email,
                            target_service,
                        )
                    }
                    client = None
                    try:
                        client = connect(profile)
                        result = discover_target_labels(
                            client,
                            profile.get("folder") or "INBOX",
                            self._field(fields, "subject"),
                            known_names,
                            message_id=self._field(fields, "message_id"),
                        )
                    except (imaplib.IMAP4.error, OSError, EOFError, UnicodeError) as exc:
                        # A failed IMAP request must still produce an HTTP
                        # response, otherwise reverse proxies report a 502.
                        raise SyncError(f"读取邮件标签失败：{exc}") from exc
                    finally:
                        if client is not None:
                            safe_logout(client)
                    self._send_json({"ok": True, **result})
                    return
                if action_path == "/mail/labels/update":
                    target_email = self._field(fields, "target_email")
                    target_service = self._field(fields, "target_service_address")
                    update_synology_label(
                        db,
                        target_email=target_email,
                        target_service_address=target_service,
                        original_label_id=self._field(
                            fields,
                            "original_label_id",
                        ),
                        label_id=self._field(fields, "label_id"),
                        label_name=self._field(fields, "label_name"),
                    )
                    finish(
                        f"/#/mail?email={quote(target_email)}"
                        f"&service={quote(target_service)}"
                        f"&dialog=labels&notice=label_saved"
                    )
                    return
                if action_path == "/mail/labels/delete":
                    target_email = self._field(fields, "target_email")
                    target_service = self._field(fields, "target_service_address")
                    delete_synology_label(
                        db,
                        target_email=target_email,
                        target_service_address=target_service,
                        label_id=self._field(fields, "label_id"),
                    )
                    finish(
                        f"/#/mail?email={quote(target_email)}"
                        f"&service={quote(target_service)}"
                        f"&dialog=labels&notice=label_deleted"
                    )
                    return
                if action_path == "/mail/test-connection":
                    role = self._field(fields, "role")
                    email = self._field(fields, "email")
                    service = self._field(fields, "service_address")
                    result = self._test_mailbox_connection(
                        db,
                        role,
                        email,
                        service,
                        fields,
                    )
                    self._send_json(result)
                    return
                if action_path == "/mail/outlook/start":
                    self._send_json(self._start_outlook_login(fields))
                    return
                if action_path == "/mail/outlook/authorize":
                    self._send_json(self._start_outlook_authorization(db, fields))
                    return
                if action_path == "/mail/outlook/complete":
                    self._send_json(self._complete_outlook_login(db, fields))
                    return
                if action_path == "/config/save":
                    self._save_config_settings(db, fields)
                    finish("/#/config?notice=config_saved")
                    return
                if action_path == "/config/outlook/save":
                    self._save_outlook_login_settings(db, fields)
                    finish("/#/config?notice=outlook_login_saved")
                    return
                if action_path == "/config/proxy/save":
                    save_proxy_profile(
                        db,
                        proxy_id=self._field(fields, "proxy_id"),
                        name=self._field(fields, "name"),
                        proxy_type=self._field(fields, "type"),
                        host=self._field(fields, "host"),
                        port=self._field(fields, "port"),
                        username=self._field(fields, "username"),
                        password=self._field(fields, "password"),
                    )
                    finish("/#/config?notice=proxy_saved")
                    return
                if action_path == "/config/proxy/delete":
                    delete_proxy_profile(db, self._field(fields, "proxy_id"))
                    finish("/#/config?notice=proxy_deleted")
                    return
            except SyncError as exc:
                if action_path in {
                    "/rules/folders",
                    "/rules/test-source",
                    "/mail/outlook/authorize",
                    "/mail/test-connection",
                    "/mail/outlook/start",
                    "/mail/outlook/complete",
                }:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)
                    return
                if api_action:
                    self._send_json({"ok": False, "error": str(exc)}, status=400)
                    return
                self._redirect_fields_error(action_path, fields, str(exc))
                return
            self._send_json({"error": "接口不存在。"}, status=404)

        def _mailbox_folder_list(
            self,
            db: sqlite3.Connection,
            role: str,
            email: str,
            service_address: str = "",
        ) -> dict:
            role = role.strip() or "mailbox"
            email = email.strip()
            service_address = service_address.strip()
            if not email:
                raise SyncError("邮箱地址是必填项")
            profile = mailbox_account_profile(db, role, email, service_address)
            client = connect(profile)
            try:
                folders = list_folders(client)
                if role == "target":
                    folders = self._selectable_folders(folders)
                folders = self._sort_folders_by_source_name(folders)
                return {
                    "ok": True,
                    "role": role,
                    "email": email,
                    "service_address": service_address,
                    "message": f"已读取 {email}；找到 {len(folders)} 个文件夹。",
                    "folders": folders,
                }
            finally:
                safe_logout(client)

        def _sort_folders_by_source_name(self, folders: list) -> list:
            return sorted(
                folders,
                key=lambda folder: (
                    str(getattr(folder, "display_name", "") or getattr(folder, "raw_name", "")).casefold(),
                    str(getattr(folder, "raw_name", "") or "").casefold(),
                ),
            )

        def _selectable_folders(self, folders: list) -> list:
            return [
                folder
                for folder in folders
                if r"\noselect" not in {
                    str(attr).casefold()
                    for attr in getattr(folder, "attributes", ())
                }
            ]

        def _validate_rule_target_folders(
            self,
            db: sqlite3.Connection,
            target_email: str,
            target_service_address: str,
            folder_rules: list[dict],
        ) -> None:
            profile = mailbox_account_profile(
                db,
                "target",
                target_email.strip(),
                target_service_address.strip(),
            )
            default_folder = str(profile.get("folder") or "INBOX").strip() or "INBOX"
            required = [default_folder]
            for rule in folder_rules:
                if rule.get("delete") or rule.get("excluded"):
                    continue
                target_folder = (
                    str(rule.get("target_folder") or default_folder).strip()
                    or default_folder
                )
                if target_folder not in required:
                    required.append(target_folder)

            client = None
            try:
                client = connect(profile)
                folders = list_folders(client)
            except (imaplib.IMAP4.error, OSError, EOFError, ValueError) as exc:
                raise SyncError(f"目标邮箱文件夹校验失败：{exc}") from exc

            try:
                available = set()
                inbox_available = False
                for folder in folders:
                    attrs = {attr.casefold() for attr in folder.attributes}
                    if r"\noselect" in attrs:
                        continue
                    for name in (folder.raw_name, folder.display_name):
                        if not name:
                            continue
                        available.add(name)
                        if name.casefold() == "inbox":
                            inbox_available = True

                missing = [
                    folder
                    for folder in required
                    if folder not in available
                    and not (folder.casefold() == "inbox" and inbox_available)
                ]
                if missing:
                    raise SyncError("目标文件夹不存在：" + "、".join(missing))
                unavailable = []
                for folder in required:
                    try:
                        select_mailbox(client, folder, readonly=True)
                    except (
                        SyncError,
                        imaplib.IMAP4.error,
                        OSError,
                        EOFError,
                        ValueError,
                    ) as exc:
                        unavailable.append(f"{folder}（{exc}）")
                if unavailable:
                    raise SyncError("目标文件夹不可用：" + "、".join(unavailable))
            finally:
                if client is not None:
                    safe_logout(client)

        def _test_source_connection(
            self,
            db: sqlite3.Connection,
            source_email: str,
        ) -> dict:
            profile = source_account_profile(db, source_email)
            client = connect(profile)
            try:
                folders = list_folders(client)
                idle = idle_supported(client)
                return {
                    "ok": True,
                    "message": (
                        f"已连接 {source_email}；找到 {len(folders)} 个文件夹；"
                        f"IMAP IDLE {'支持' if idle else '不支持'}。"
                    ),
                    "idle_supported": idle,
                    "folders": folders,
                }
            finally:
                safe_logout(client)

        def _test_mailbox_connection(
            self,
            db: sqlite3.Connection,
            role: str,
            email: str,
            service_address: str,
            fields: dict[str, list[str]] | None = None,
        ) -> dict:
            profile = self._mailbox_test_profile(
                db,
                role,
                email,
                service_address,
                fields,
            )
            if profile.get("_test_protocol") == "pop":
                result = self._test_pop_connection(profile)
                self._save_test_probe_results(db, profile, fields, result)
                return result

            result = run_imap_mailbox_probe(profile)
            self._save_test_probe_results(db, profile, fields, result)
            return result

        def _test_pop_connection(self, profile: dict) -> dict:
            client = None
            tested_at = now_iso()
            items = []
            features = {"pop_login": False}

            def add_item(
                key: str,
                label: str,
                status: str,
                message: str,
                scope: str = "source",
            ) -> None:
                items.append(
                    {
                        "key": key,
                        "label": label,
                        "status": status,
                        "message": message,
                        "scope": scope,
                    }
                )

            try:
                client = connect_pop(profile)
                features["pop_login"] = True
                add_item("pop_login", "POP 登录", "ok", f"已登录 {profile['email']}。")
                _, rows, _ = client.list()
                message_count = len(rows or [])
                features["pop_list"] = True
                add_item(
                    "pop_list",
                    "读取邮件列表",
                    "ok",
                    f"找到 {message_count} 封邮件。",
                )
                return {
                    "ok": True,
                    "message": (
                        f"已通过 POP 连接 {profile['email']}；"
                        f"找到 {message_count} 封邮件。"
                    ),
                    "message_count": message_count,
                    "probe_results": {
                        "tested_at": tested_at,
                        "protocol": "pop",
                        "items": items,
                        "features": features,
                        "feature_groups": {
                            "common": {},
                            "source": dict(features),
                            "target": {},
                        },
                    },
                    "items": items,
                    "features": features,
                    "feature_groups": {
                        "common": {},
                        "source": dict(features),
                        "target": {},
                    },
                }
            except (poplib.error_proto, OSError, EOFError, ValueError) as exc:
                if not items:
                    add_item("pop_login", "POP 登录", "fail", f"连接失败：{exc}")
                return {
                    "ok": False,
                    "error": f"POP 连接失败：{exc}",
                    "message": f"POP 连接失败：{exc}",
                    "probe_results": {
                        "tested_at": tested_at,
                        "protocol": "pop",
                        "items": items,
                        "features": features,
                        "feature_groups": {
                            "common": {},
                            "source": dict(features),
                            "target": {},
                        },
                    },
                    "items": items,
                    "features": features,
                    "feature_groups": {
                        "common": {},
                        "source": dict(features),
                        "target": {},
                    },
                }
            finally:
                if client is not None:
                    safe_pop_quit(client)

        def _save_test_probe_results(
            self,
            db: sqlite3.Connection,
            profile: dict,
            fields: dict[str, list[str]] | None,
            result: dict,
        ) -> None:
            probe_results = result.get("probe_results")
            if not isinstance(probe_results, dict):
                return
            email = str(profile.get("email") or "").strip()
            service = str(profile.get("service_address") or "").strip()
            if not email:
                return
            saved = save_mailbox_probe_results(
                db,
                email,
                service,
                probe_results,
            )
            result["probe_results_saved"] = saved

        def _outlook_defaults(self, db: sqlite3.Connection) -> dict:
            settings = self._outlook_login_settings(db)
            if settings.get("client_id"):
                return settings
            rows = db.execute(
                """
                SELECT auth_json
                FROM mail_accounts
                WHERE auth_json IS NOT NULL AND auth_json <> ''
                ORDER BY sort_order, email
                """
            ).fetchall()
            for row in rows:
                try:
                    auth = json.loads(row["auth_json"])
                except (TypeError, json.JSONDecodeError):
                    continue
                if (
                    isinstance(auth, dict)
                    and auth.get("type") == "oauth2"
                    and auth.get("provider") == "microsoft"
                    and auth.get("client_id")
                ):
                    return {"client_id": str(auth["client_id"])}
            return {}

        def _outlook_login_settings(self, db: sqlite3.Connection) -> dict:
            row = db.execute(
                "SELECT value_json FROM app_settings WHERE key='outlook_login'"
            ).fetchone()
            settings: dict[str, object] = {}
            if row is not None:
                try:
                    value = json.loads(row["value_json"])
                except (TypeError, json.JSONDecodeError):
                    value = {}
                if isinstance(value, dict):
                    settings = value
            return {
                "client_id": str(settings.get("client_id") or ""),
            }

        def _save_outlook_login_settings(
            self,
            db: sqlite3.Connection,
            fields: dict[str, list[str]],
        ) -> None:
            client_id = self._field(fields, "client_id").strip()
            payload = {
                "client_id": client_id,
            }
            with db:
                db.execute(
                    """
                    INSERT INTO app_settings(key, value_json)
                    VALUES('outlook_login', ?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
                    """,
                    (json.dumps(payload, ensure_ascii=False),),
                )

        def _save_config_settings(
            self,
            db: sqlite3.Connection,
            fields: dict[str, list[str]],
        ) -> None:
            self._save_outlook_login_settings(db, fields)
            raw_count = self._field(fields, "proxy_count", "0")
            try:
                count = int(raw_count or "0")
            except ValueError as exc:
                raise SyncError("代理配置数量无效") from exc

            for index in range(count):
                proxy_id = self._field(fields, f"proxy_id_{index}")
                name = self._field(fields, f"proxy_name_{index}")
                proxy_type = self._field(fields, f"proxy_type_{index}")
                host = self._field(fields, f"proxy_host_{index}")
                port = self._field(fields, f"proxy_port_{index}")
                username = self._field(fields, f"proxy_username_{index}")
                password = self._field(fields, f"proxy_password_{index}")
                if self._checked(fields, f"proxy_delete_{index}"):
                    delete_proxy_profile(db, proxy_id)
                    continue
                if not proxy_id and not any(
                    value.strip()
                    for value in (name, host, port, username, password)
                ):
                    continue
                save_proxy_profile(
                    db,
                    proxy_id=proxy_id,
                    name=name,
                    proxy_type=proxy_type,
                    host=host,
                    port=port,
                    username=username,
                    password=password,
                )

        def _start_outlook_login(self, fields: dict[str, list[str]]) -> dict:
            auth = self._outlook_auth_from_fields(fields)
            device_url, _ = microsoft_oauth_urls(auth)
            device = post_form_json(
                device_url,
                {
                    "client_id": auth["client_id"],
                    "scope": microsoft_oauth_scope(auth),
                },
            )
            for field in ("device_code", "user_code", "verification_uri", "expires_in"):
                if not device.get(field):
                    raise SyncError(f"Microsoft 登录响应缺少 {field}")
            return {
                "ok": True,
                "client_id": auth["client_id"],
                "device_code": device["device_code"],
                "verification_uri": device["verification_uri"],
                "verification_uri_complete": (
                    device.get("verification_uri_complete")
                    or device["verification_uri"]
                ),
                "user_code": device["user_code"],
                "message": device.get("message") or "",
                "interval": max(1, int(device.get("interval", 5))),
                "expires_in": int(device["expires_in"]),
            }

        def _start_outlook_authorization(
            self,
            db: sqlite3.Connection,
            fields: dict[str, list[str]],
        ) -> dict:
            auth = self._outlook_auth_from_fields(fields)
            redirect_uri = self._outlook_redirect_uri()
            state = secrets.token_urlsafe(32)
            verifier = secrets.token_urlsafe(64)
            challenge = self._pkce_challenge(verifier)
            flow = {
                "auth": auth,
                "code_verifier": verifier,
                "created_at": int(time.time()),
                "redirect_uri": redirect_uri,
            }
            with db:
                self._delete_expired_outlook_flows(db)
                db.execute(
                    """
                    INSERT INTO app_settings(key, value_json)
                    VALUES(?, ?)
                    ON CONFLICT(key) DO UPDATE SET value_json=excluded.value_json
                    """,
                    (
                        self._outlook_flow_key(state),
                        json.dumps(flow, ensure_ascii=False),
                    ),
                )
            return {
                "ok": True,
                "authorization_url": self._outlook_authorization_url(
                    auth,
                    redirect_uri,
                    state,
                    challenge,
                ),
                "state": state,
                "redirect_uri": redirect_uri,
            }

        def _serve_outlook_callback(self, db: sqlite3.Connection) -> None:
            try:
                result = self._complete_outlook_authorization(db)
                self._send_outlook_callback_page(result)
            except SyncError as exc:
                self._send_outlook_callback_page(
                    {
                        "ok": False,
                        "message": str(exc),
                    },
                    status=400,
                )

        def _complete_outlook_authorization(
            self,
            db: sqlite3.Connection,
        ) -> dict:
            error = self._query_value("error")
            if error:
                description = self._query_value("error_description") or error
                raise SyncError(f"Outlook 登录失败：{description}")

            code = self._query_value("code") or ""
            state = self._query_value("state") or ""
            if not code or not state:
                raise SyncError("Microsoft 回调缺少授权信息，请重新登录")

            key = self._outlook_flow_key(state)
            row = db.execute(
                "SELECT value_json FROM app_settings WHERE key=?",
                (key,),
            ).fetchone()
            if row is None:
                raise SyncError("Outlook 登录状态已失效，请重新开始登录")
            with db:
                db.execute("DELETE FROM app_settings WHERE key=?", (key,))
            try:
                flow = json.loads(row["value_json"])
            except (TypeError, json.JSONDecodeError) as exc:
                raise SyncError("Outlook 登录状态无效，请重新开始登录") from exc
            if not isinstance(flow, dict):
                raise SyncError("Outlook 登录状态无效，请重新开始登录")
            if int(flow.get("created_at") or 0) < int(time.time()) - 900:
                raise SyncError("Outlook 登录已超时，请重新开始登录")

            auth = flow.get("auth")
            if not isinstance(auth, dict):
                raise SyncError("Outlook 登录配置无效，请重新开始登录")
            _, token_url = microsoft_oauth_urls(auth)
            payload = post_form_json(
                token_url,
                {
                    "client_id": auth["client_id"],
                    "grant_type": "authorization_code",
                    "code": code,
                    "redirect_uri": str(flow["redirect_uri"]),
                    "code_verifier": str(flow["code_verifier"]),
                    "scope": microsoft_oauth_scope(auth),
                },
                return_oauth_error=True,
            )
            if payload.get("error"):
                raise SyncError(
                    "Outlook 登录失败："
                    f"{oauth_error_message(payload, 'unknown response')}"
                )

            token = normalized_token_response(payload)
            if not token.get("refresh_token"):
                raise SyncError("Microsoft 未返回可持久保存的刷新令牌")
            email = self._outlook_email_from_token(token)
            token_auth = dict(auth)
            return self._save_outlook_login_token(db, token_auth, token)

        def _complete_outlook_login(
            self,
            db: sqlite3.Connection,
            fields: dict[str, list[str]],
        ) -> dict:
            device_code = self._field(fields, "device_code").strip()
            auth = self._outlook_auth_from_fields(fields)
            if not device_code:
                raise SyncError("Outlook 登录码已失效，请重新开始登录")
            _, token_url = microsoft_oauth_urls(auth)
            token_fields = {
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": auth["client_id"],
                "device_code": device_code,
            }
            payload = post_form_json(token_url, token_fields, return_oauth_error=True)
            if self._oauth_error_mentions(payload, "redirect_uri"):
                token_fields["redirect_uri"] = MICROSOFT_NATIVE_REDIRECT_URI
                payload = post_form_json(
                    token_url,
                    token_fields,
                    return_oauth_error=True,
                )
            error = payload.get("error")
            if error == "authorization_pending":
                return {
                    "ok": False,
                    "pending": True,
                    "message": "等待 Microsoft 登录完成...",
                }
            if error == "slow_down":
                return {
                    "ok": False,
                    "pending": True,
                    "slow_down": True,
                    "message": "Microsoft 要求降低检查频率，继续等待...",
                }
            if error:
                message = oauth_error_message(payload, "unknown response")
                if "redirect_uri" in message:
                    raise SyncError(
                        "Outlook 登录失败：Microsoft 要求此应用登记公共客户端回调地址。"
                        "请在 Microsoft Entra 应用的移动/桌面平台添加 "
                        f"{MICROSOFT_NATIVE_REDIRECT_URI} 后重试。"
                        f"原始错误：{message}"
                    )
                raise SyncError(
                    "Outlook 登录失败："
                    f"{message}"
                )

            token = normalized_token_response(payload)
            if not token.get("refresh_token"):
                raise SyncError("Microsoft 未返回可持久保存的刷新令牌")
            email = self._outlook_email_from_token(token)
            auth = self._outlook_auth_from_fields(fields, email)
            return self._save_outlook_login_token(db, auth, token)

        def _save_outlook_login_token(
            self,
            db: sqlite3.Connection,
            auth: dict,
            token: dict,
        ) -> dict:
            email = self._outlook_email_from_token(token)

            service = "outlook.office365.com"
            mailbox_email = save_mailbox_account(
                db,
                original_role="source",
                original_email=email,
                original_service_address=service,
                role="source",
                email=email,
                enabled=True,
                auth_provider="outlook",
                imap_server=service,
                imap_port="993",
                use_ssl=True,
                password="",
                oauth_client_id=str(auth["client_id"]),
                oauth_token_file="",
                oauth_token=token,
                target_folder="INBOX",
                imap_timeout="",
                proxy_profile_id="",
                pop_fallback_enabled=False,
                pop_server="",
                pop_port="",
                pop_use_ssl=True,
                pop_timeout="",
                service_address=service,
                imap_enabled=True,
                smtp_enabled=False,
                smtp_server="",
                smtp_port="",
                smtp_use_ssl=True,
                smtp_timeout="",
            )
            return {
                "ok": True,
                "message": f"Outlook 登录成功，已保存 {mailbox_email}。",
                "redirect_url": (
                    f"/#/mail?email={quote(mailbox_email)}"
                    f"&service={quote(service)}&notice=mailbox_saved"
                ),
            }

        def _outlook_authorization_url(
            self,
            auth: dict,
            redirect_uri: str,
            state: str,
            challenge: str,
        ) -> str:
            device_url, _ = microsoft_oauth_urls(auth)
            authorize_url = device_url.rsplit("/", 1)[0] + "/authorize"
            return authorize_url + "?" + urlencode(
                {
                    "client_id": auth["client_id"],
                    "response_type": "code",
                    "redirect_uri": redirect_uri,
                    "response_mode": "query",
                    "scope": microsoft_oauth_scope(auth),
                    "state": state,
                    "code_challenge": challenge,
                    "code_challenge_method": "S256",
                    "prompt": "select_account",
                }
            )

        def _outlook_redirect_uri(self) -> str:
            request_host = (
                self.headers.get("X-Forwarded-Host")
                or self.headers.get("Host")
                or f"{host}:{port}"
            )
            proto = self.headers.get("X-Forwarded-Proto")
            if not proto:
                proto = "https" if request_host.endswith(":443") else "http"
            return f"{proto}://{request_host}/outlook/callback"

        def _oauth_error_mentions(self, payload: dict, text: str) -> bool:
            if not payload.get("error"):
                return False
            message = oauth_error_message(payload, "").lower()
            return text.lower() in message

        def _pkce_challenge(self, verifier: str) -> str:
            digest = hashlib.sha256(verifier.encode("ascii")).digest()
            return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")

        def _outlook_flow_key(self, state: str) -> str:
            return f"outlook_login_flow:{state}"

        def _delete_expired_outlook_flows(self, db: sqlite3.Connection) -> None:
            cutoff = int(time.time()) - 900
            rows = db.execute(
                """
                SELECT key, value_json
                FROM app_settings
                WHERE key LIKE 'outlook_login_flow:%'
                """
            ).fetchall()
            for row in rows:
                try:
                    flow = json.loads(row["value_json"])
                except (TypeError, json.JSONDecodeError):
                    flow = {}
                if not isinstance(flow, dict) or int(flow.get("created_at") or 0) < cutoff:
                    db.execute("DELETE FROM app_settings WHERE key=?", (row["key"],))

        def _send_outlook_callback_page(
            self,
            result: dict,
            *,
            status: int = 200,
        ) -> None:
            payload = json.dumps(
                {
                    "type": "mail-sync-outlook-login",
                    **result,
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ).replace("<", "\\u003c")
            script_nonce = secrets.token_urlsafe(24)
            title = "Outlook 登录完成" if result.get("ok") else "Outlook 登录失败"
            message = str(result.get("message") or title)
            body = f"""<!doctype html>
<html lang="zh-CN">
  <head>
    <meta charset="utf-8">
    <title>{html.escape(title)}</title>
  </head>
  <body>
    <p>{html.escape(message)}</p>
    <script nonce="{script_nonce}">
      const payload = {payload};
      if (window.opener) {{
        window.opener.postMessage(payload, window.location.origin);
      }}
      setTimeout(() => window.close(), 600);
    </script>
  </body>
</html>
"""
            self._send_html(
                body.encode("utf-8"),
                status=status,
                headers={
                    "Content-Security-Policy": _content_security_policy(
                        script_nonce
                    )
                },
            )

        def _outlook_auth_from_fields(
            self,
            fields: dict[str, list[str]],
            email: str = "",
        ) -> dict:
            with closing(self._db()) as settings_db:
                settings = self._outlook_login_settings(settings_db)
            client_id = (
                self._field(fields, "oauth_client_id").strip()
                or settings["client_id"]
            )
            if not client_id:
                raise SyncError("请先在配置页维护 Outlook 客户端 ID")
            auth = {
                "type": "oauth2",
                "provider": "microsoft",
                "client_id": client_id,
                "scopes": [
                    MICROSOFT_IMAP_SCOPE,
                    "offline_access",
                    "openid",
                    "profile",
                    "email",
                ],
            }
            return auth

        def _outlook_email_from_token(self, token: dict) -> str:
            for key in ("id_token", "access_token"):
                claims = self._decode_jwt_payload(str(token.get(key) or ""))
                email = self._outlook_email_from_claims(claims)
                if email:
                    return email
            email = self._outlook_email_from_claims(token)
            if email:
                return email
            raise SyncError(
                "Microsoft 未返回邮箱地址，请确认应用授权范围包含 openid/profile/email。"
            )

        def _decode_jwt_payload(self, value: str) -> dict:
            parts = value.split(".")
            if len(parts) < 2:
                return {}
            padded = parts[1] + ("=" * (-len(parts[1]) % 4))
            try:
                payload = base64.urlsafe_b64decode(padded.encode("ascii"))
                decoded = json.loads(payload.decode("utf-8"))
            except (
                binascii.Error,
                UnicodeDecodeError,
                json.JSONDecodeError,
                ValueError,
            ):
                return {}
            return decoded if isinstance(decoded, dict) else {}

        def _outlook_email_from_claims(self, claims: dict) -> str:
            for key in ("preferred_username", "email", "upn", "unique_name"):
                value = claims.get(key)
                if isinstance(value, str) and self._looks_like_email(value):
                    return value.strip().lower()
            emails = claims.get("emails")
            if isinstance(emails, list):
                for value in emails:
                    if isinstance(value, str) and self._looks_like_email(value):
                        return value.strip().lower()
            return ""

        def _looks_like_email(self, value: str) -> bool:
            text = value.strip()
            return "@" in text and "." in text.rsplit("@", 1)[-1]

        def _mailbox_test_profile(
            self,
            db: sqlite3.Connection,
            role: str,
            email: str,
            service_address: str,
            fields: dict[str, list[str]] | None,
        ) -> dict:
            if not fields or not self._field(fields, "mailbox_protocol_tab"):
                return mailbox_account_profile(db, role, email, service_address)

            protocol = self._field(fields, "mailbox_protocol_tab").strip() or "imap"
            if protocol not in {"imap", "pop"}:
                raise SyncError("邮箱协议无效")

            email = (
                self._field(fields, "email").strip()
                or self._field(fields, "original_email").strip()
            )
            if not email:
                raise SyncError("邮箱地址是必填项")
            existing = self._existing_mailbox_profile(db, fields)
            password = self._field(fields, "password") or existing.get("password", "")
            auth_provider = self._field(fields, "auth_provider", "manual")
            proxy = self._proxy_profile_from_fields(db, fields)

            if protocol == "pop":
                pop_server = self._field(fields, "pop_server").strip()
                if not pop_server:
                    raise SyncError("POP 服务地址是必填项")
                if not password:
                    raise SyncError("测试 POP 需要填写密码")
                pop_use_ssl = self._checked(fields, "pop_use_ssl")
                pop_port = self._positive_int(
                    self._field(fields, "pop_port") or ("995" if pop_use_ssl else "110"),
                    "POP 端口",
                )
                pop_timeout = self._field(fields, "pop_timeout")
                profile: dict[str, object] = {
                    "_test_protocol": "pop",
                    "email": email,
                    "service_address": self._field(fields, "service_address") or pop_server,
                    "protocols": ["pop"],
                    "password": password,
                    "pop_fallback": {
                        "server": pop_server,
                        "port": pop_port,
                        "use_ssl": pop_use_ssl,
                    },
                }
                if pop_timeout:
                    profile["pop_fallback"]["timeout"] = self._positive_int(
                        pop_timeout,
                        "POP 超时",
                    )
                if proxy is not None:
                    profile["imap_proxy"] = proxy
                return profile

            if not self._checked(fields, "imap_enabled"):
                raise SyncError("文件夹测试需要切换到 IMAP 并启用 IMAP")

            imap_server = self._field(fields, "imap_server").strip()
            if not imap_server:
                raise SyncError("IMAP 服务地址是必填项")
            profile: dict[str, object] = {
                "_test_protocol": "imap",
                "email": email,
                "service_address": self._field(fields, "service_address") or imap_server,
                "imap_server": imap_server,
                "use_ssl": self._checked(fields, "use_ssl"),
                "protocols": ["imap"],
                "folder": self._field(fields, "target_folder").strip() or "INBOX",
            }
            imap_port = self._positive_int(
                self._field(fields, "imap_port") or ("993" if profile["use_ssl"] else "143"),
                "端口",
            )
            profile["imap_port"] = imap_port
            imap_timeout = self._field(fields, "imap_timeout")
            if imap_timeout:
                profile["imap_timeout"] = self._positive_int(imap_timeout, "超时")

            if auth_provider == "outlook":
                existing_auth = existing.get("auth", {})
                client_id = self._field(fields, "oauth_client_id") or str(
                    existing_auth.get("client_id", "")
                )
                if not client_id:
                    raise SyncError("Outlook 客户端 ID 是必填项")
                auth = dict(existing_auth)
                if not auth.get("token") and not auth.get("token_file"):
                    raise SyncError("Outlook 令牌未保存，请重新登录 Outlook")
                auth.update(
                    {
                        "type": "oauth2",
                        "provider": "microsoft",
                        "client_id": client_id,
                    }
                )
                profile["auth"] = auth
            else:
                if not password:
                    raise SyncError("测试连接需要填写密码")
                profile["password"] = password

            if proxy is not None:
                profile["imap_proxy"] = proxy
            return profile

        def _existing_mailbox_profile(
            self,
            db: sqlite3.Connection,
            fields: dict[str, list[str]],
        ) -> dict:
            original_role = self._field(fields, "original_role")
            original_email = self._field(fields, "original_email")
            original_service = self._field(fields, "original_service_address")
            if not original_email:
                return {}
            try:
                return mailbox_account_profile(
                    db,
                    original_role or "mailbox",
                    original_email,
                    original_service,
                )
            except SyncError:
                return {}

        def _proxy_profile_from_fields(
            self,
            db: sqlite3.Connection,
            fields: dict[str, list[str]],
        ) -> dict | None:
            raw_id = self._field(fields, "proxy_profile_id")
            if not raw_id:
                return None
            proxy_id = self._positive_int(raw_id, "代理")
            proxy = db.execute(
                "SELECT * FROM proxy_profiles WHERE id=?",
                (proxy_id,),
            ).fetchone()
            if proxy is None:
                raise SyncError("选择的代理不存在")
            result = {
                "type": proxy["type"],
                "host": proxy["host"],
                "port": int(proxy["port"]),
            }
            if proxy["username"] is not None:
                result["username"] = proxy["username"]
            if proxy["password"] is not None:
                result["password"] = proxy["password"]
            if proxy["extra_json"]:
                extra = json.loads(proxy["extra_json"])
                if isinstance(extra, dict):
                    result.update(extra)
            return result

        def _positive_int(self, raw: str, context: str) -> int:
            try:
                value = int(raw)
            except (TypeError, ValueError) as exc:
                raise SyncError(f"{context}必须是数字") from exc
            if value < 1:
                raise SyncError(f"{context}必须大于 0")
            return value

        def _serve_app(self, *, status: int = 200) -> None:
            self._serve_file("index.html", status=status)

        def _serve_file(self, relative_path: str, *, status: int = 200) -> None:
            if ".." in Path(relative_path).parts:
                self.send_error(404)
                return
            root = HTML_DIR.resolve()
            file_path = (root / relative_path).resolve()
            try:
                file_path.relative_to(root)
            except ValueError:
                self.send_error(404)
                return
            if not file_path.is_file():
                self.send_error(404)
                return
            content_type = STATIC_TYPES.get(
                file_path.suffix,
                "application/octet-stream",
            )
            self._send_bytes(
                file_path.read_bytes(),
                content_type,
                status=status,
            )

        def _redirect_fields_error(
            self,
            path: str,
            fields: dict[str, list[str]],
            message: str,
        ) -> None:
            if path.startswith("/config"):
                self._redirect(f"/#/config?{urlencode({'error': message})}")
                return

            if path.startswith("/rules"):
                params = {"dialog": "detail", "error": message}
                source_email = (
                    self._field(fields, "source_email")
                    or self._field(fields, "original_email")
                )
                if source_email:
                    params["source"] = source_email
                source_service = self._field(fields, "source_service_address")
                if source_service:
                    params["source_service"] = source_service
                target_email = self._field(fields, "target_email")
                if target_email:
                    params["target"] = target_email
                target_service = self._field(fields, "target_service_address")
                if target_service:
                    params["target_service"] = target_service
                self._redirect(f"/#/rules?{urlencode(params)}")
                return

            target_email = (
                self._field(fields, "target_email")
                or self._field(fields, "original_email")
                or self._field(fields, "email")
            )
            target_service = (
                self._field(fields, "target_service_address")
                or self._field(fields, "original_service_address")
                or self._field(fields, "service_address")
            )
            role = self._field(fields, "role") or self._field(
                fields,
                "original_role",
            )
            dialog = self._field(fields, "dialog")
            if not dialog and path == "/mail/account/save":
                dialog = "edit"
            params = {"error": message}
            for key, value in (
                ("email", target_email),
                ("service", target_service),
                ("dialog", dialog),
            ):
                if value:
                    params[key] = value
            self._redirect(f"/#/mail?{urlencode(params)}")

        def _current_username(self, db: sqlite3.Connection) -> Optional[str]:
            username = session.current_username(db, self.headers.get("Cookie"))
            if username is None:
                return None
            user = find_user_by_username(db, username)
            if user is None or not user["enabled"]:
                return None
            return username

        def _db(self) -> sqlite3.Connection:
            return connect_db(state_path)

        def _read_fields(self) -> dict[str, list[str]]:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                length = 0
            length = min(length, 64 * 1024)
            raw_body = self.rfile.read(length)
            if not raw_body:
                return {}
            content_type = (
                self.headers.get("Content-Type", "")
                .split(";", 1)[0]
                .strip()
                .lower()
            )
            if content_type != "application/json" and not content_type.endswith("+json"):
                raise SyncError("请使用 application/json 请求。")
            try:
                body = json.loads(raw_body.decode("utf-8"))
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise SyncError("JSON 请求体格式无效") from exc
            if body is None:
                return {}
            if not isinstance(body, dict):
                raise SyncError("JSON 请求体必须是对象")
            return {
                str(key): self._json_field_values(value)
                for key, value in body.items()
            }

        def _json_field_values(self, value: Any) -> list[str]:
            if isinstance(value, list):
                if not value:
                    return [""]
                return [self._json_field_value(item) for item in value]
            return [self._json_field_value(value)]

        def _json_field_value(self, value: Any) -> str:
            if value is None:
                return ""
            if isinstance(value, bool):
                return "true" if value else "false"
            if isinstance(value, (str, int, float)):
                return str(value)
            return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

        def _field(
            self,
            fields: dict[str, list[str]],
            name: str,
            default: str = "",
        ) -> str:
            return fields.get(name, [default])[0]

        def _checked(self, fields: dict[str, list[str]], name: str) -> bool:
            return self._field(fields, name).strip().lower() in {
                "1",
                "true",
                "on",
                "yes",
            }

        def _wants_json(self) -> bool:
            requested_with = self.headers.get("X-Requested-With", "")
            accept = self.headers.get("Accept", "")
            return (
                requested_with.lower() == "xmlhttprequest"
                or "application/json" in accept.lower()
            )

        def _folder_rule_fields(
            self,
            fields: dict[str, list[str]],
        ) -> list[dict]:
            raw_count = self._field(fields, "folder_count", "0")
            try:
                count = int(raw_count or "0")
            except ValueError as exc:
                raise SyncError("文件夹映射数量无效") from exc
            folder_rules = []
            for index in range(count):
                folder_labels = [
                    value
                    for value in fields.get(f"folder_label_{index}", [])
                    if value
                ]
                folder_rules.append(
                    {
                        "folder_name": self._field(
                            fields,
                            f"folder_name_{index}",
                        ),
                        "display_name": self._field(
                            fields,
                            f"folder_display_name_{index}",
                        ),
                        "attributes": [
                            value
                            for value in fields.get(
                                f"folder_attribute_{index}",
                                [],
                            )
                            if value
                        ],
                        "target_folder": self._field(
                            fields,
                            f"target_folder_{index}",
                        ),
                        "folder_label": self._field(
                            fields,
                            f"folder_label_{index}",
                        ),
                        "folder_labels": folder_labels,
                        "excluded": self._checked(
                            fields,
                            f"folder_excluded_{index}",
                        ),
                        "skip_uids": self._field(
                            fields,
                            f"folder_skip_uids_{index}",
                        ),
                        "delete": self._checked(
                            fields,
                            f"folder_delete_{index}",
                        ),
                    }
                )
            return folder_rules

        def _query_value(self, name: str) -> Optional[str]:
            values = parse_qs(urlparse(self.path).query, keep_blank_values=True).get(
                name
            )
            if not values:
                return None
            return values[0]

        def _query_int(self, name: str, default: int) -> int:
            value = self._query_value(name)
            if value is None:
                return default
            try:
                return int(value)
            except ValueError:
                return default

        def _notice_message(self, code: Optional[str]) -> Optional[str]:
            messages = {
                "mailbox_saved": "邮箱已保存。",
                "mailbox_deleted": "邮箱已删除。",
                "target_saved": "目标邮箱已保存。",
                "label_created": "标签映射已创建。",
                "label_saved": "标签映射已保存。",
                "label_deleted": "标签映射已删除。",
                "rule_saved": "同步规则已保存。",
                "rule_paused": "同步规则已暂停。",
                "rule_resumed": "同步规则已恢复。",
                "rule_deleted": "同步规则已删除。",
                "config_saved": "配置已保存。",
                "outlook_login_saved": "Outlook 登录配置已保存。",
                "proxy_saved": "代理配置已保存。",
                "proxy_deleted": "代理配置已删除。",
            }
            if code is None:
                return None
            return messages.get(code)

        def _redirect(
            self,
            location: str,
            *,
            cookies: Optional[list[str]] = None,
        ) -> None:
            self.send_response(303)
            self._send_security_headers()
            self.send_header("Location", location)
            for cookie in cookies or []:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()

        def _send_json(
            self,
            data: Any,
            status: int = 200,
            *,
            cookies: Optional[list[str]] = None,
            headers: Optional[dict[str, str]] = None,
        ) -> None:
            body = json.dumps(
                self._json_ready(data),
                ensure_ascii=False,
                separators=(",", ":"),
            ).encode("utf-8")
            self._send_bytes(
                body,
                "application/json; charset=utf-8",
                status=status,
                cookies=cookies,
                headers=headers,
            )
            self._log_json_response(data, status)

        def _log_json_response(self, data: Any, status: int) -> None:
            path = urlparse(self.path).path
            if not path.startswith("/api/"):
                return
            started = getattr(self, "_request_started_at", None)
            duration_ms = (
                round((time.monotonic() - started) * 1000, 1)
                if isinstance(started, float)
                else None
            )
            event: dict[str, Any] = {
                "event": "web_request",
                "method": self.command,
                "path": path,
                "status": status,
            }
            if duration_ms is not None:
                event["duration_ms"] = duration_ms
            event.update(getattr(self, "_request_log_context", {}) or {})
            if isinstance(data, dict):
                if "ok" in data:
                    event["ok"] = bool(data.get("ok"))
                error = str(data.get("error") or "").strip()
                if error:
                    event["error"] = error[:300]
            print(json.dumps(event, ensure_ascii=False), flush=True)

        def _json_ready(self, value: Any) -> Any:
            if isinstance(value, sqlite3.Row):
                return {key: self._json_ready(value[key]) for key in value.keys()}
            if isinstance(value, dict):
                return {
                    str(key): self._json_ready(item)
                    for key, item in value.items()
                }
            if isinstance(value, (list, tuple)):
                return [self._json_ready(item) for item in value]
            if hasattr(value, "display_name") and hasattr(value, "raw_name"):
                return {
                    "display_name": self._json_ready(value.display_name),
                    "raw_name": self._json_ready(value.raw_name),
                    "attributes": self._json_ready(
                        getattr(value, "attributes", ()),
                    ),
                }
            return value

        def _send_html(
            self,
            body: bytes,
            status: int = 200,
            *,
            headers: Optional[dict[str, str]] = None,
        ) -> None:
            self._send_bytes(
                body,
                "text/html; charset=utf-8",
                status=status,
                headers=headers,
            )

        def _send_bytes(
            self,
            body: bytes,
            content_type: str,
            *,
            status: int = 200,
            cookies: Optional[list[str]] = None,
            headers: Optional[dict[str, str]] = None,
        ) -> None:
            self.send_response(status)
            response_headers = dict(headers or {})
            content_security_policy = response_headers.pop(
                "Content-Security-Policy",
                CONTENT_SECURITY_POLICY,
            )
            self._send_security_headers(content_security_policy)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            for key, value in response_headers.items():
                self.send_header(key, value)
            for cookie in cookies or []:
                self.send_header("Set-Cookie", cookie)
            self.end_headers()
            self.wfile.write(body)

        def _send_security_headers(
            self,
            content_security_policy: str = CONTENT_SECURITY_POLICY,
        ) -> None:
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "same-origin")
            self.send_header("Content-Security-Policy", content_security_policy)

    return MailSyncWebHandler
