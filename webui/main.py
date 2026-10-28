# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: MIT-0
"""Bedrock Invocation Analytics WebUI — Entry point."""

import os
import secrets
import tomllib
from pathlib import Path
from urllib.parse import quote

from fastapi import Request
from fastapi.responses import RedirectResponse
from starlette.middleware.base import BaseHTTPMiddleware
from nicegui import app, ui

from webui import dashboard  # noqa: F401
from webui import pricing  # noqa: F401
from webui.auth import LoginThrottle, credentials_match, safe_redirect

with open(Path(__file__).parent.parent / "pyproject.toml", "rb") as f:
    VERSION = tomllib.load(f)["project"]["version"]

dashboard.VERSION = VERSION

# Credentials come from .env.deploy (written by deploy.sh from config.yaml).
_admin_user = os.environ.get("ADMIN_USER", "")
_admin_pass = os.environ.get("ADMIN_PASS", "")
AUTH_ENABLED = bool(_admin_user and _admin_pass)
if not AUTH_ENABLED:
    print("[WARN] ADMIN_USER/ADMIN_PASS not set — authentication disabled. Set credentials in config.yaml and run deploy.sh.")

# Loopback by default; reach it remotely through SSH/SSM port forwarding or a TLS reverse proxy.
HOST = os.environ.get("WEBUI_HOST", "127.0.0.1")
STORAGE_SECRET = os.environ.get("STORAGE_SECRET") or secrets.token_hex(32)
if not os.environ.get("STORAGE_SECRET"):
    print("[WARN] STORAGE_SECRET not set — using a per-process secret; sessions end when the server restarts.")

UNRESTRICTED = {"/login"}
throttle = LoginThrottle()


@app.add_middleware
class AuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if AUTH_ENABLED and not app.storage.user.get("authenticated", False):
            path = request.url.path
            if not path.startswith("/_nicegui") and path not in UNRESTRICTED:
                return RedirectResponse(f"/login?redirect_to={quote(path)}")
        return await call_next(request)


@ui.page("/login")
def login_page(request: Request, redirect_to: str = "/") -> RedirectResponse | None:
    redirect_to = safe_redirect(redirect_to)
    if app.storage.user.get("authenticated", False):
        return RedirectResponse(redirect_to)

    ui.dark_mode(False)
    client_ip = request.client.host if request.client else "unknown"

    def try_login():
        if throttle.is_locked(client_ip):
            ui.notify("Too many failed attempts. Try again later.", color="negative")
            return
        if credentials_match(username.value, password.value, _admin_user, _admin_pass):
            throttle.reset(client_ip)
            app.storage.user.update({"username": username.value, "authenticated": True})
            ui.navigate.to(redirect_to)
        else:
            throttle.record_failure(client_ip)
            ui.notify("Invalid credentials", color="negative")

    with ui.card().classes("absolute-center min-w-[300px]"):
        ui.label("Bedrock Invocation Analytics").classes("text-xl font-bold text-center w-full mb-4")
        username = ui.input("Username").on("keydown.enter", try_login).classes("w-full")
        password = ui.input("Password", password=True, password_toggle_button=True).on("keydown.enter", try_login).classes("w-full")
        ui.button("Log in", on_click=try_login).classes("w-full mt-4")
    return None


if __name__ in {"__main__", "__mp_main__"}:
    ui.run(
        title="Bedrock Invocation Analytics",
        favicon="webui/favicon.svg",
        host=HOST,
        port=int(os.environ.get("PORT", "8060")),
        reload=False,
        storage_secret=STORAGE_SECRET,
    )
