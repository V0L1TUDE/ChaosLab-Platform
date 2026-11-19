"""Authentication API: register, login, logout, current user."""

from . import auth, config
from .http_core import json_response, router, session_cookie_header


@router.route("POST", "/api/auth/register", auth_required=False)
def register(req):
    body = req.json()
    user = auth.register_user(body.get("name"), body.get("email"), body.get("password"))
    token = auth.create_session(user["id"])
    return json_response({"user": user}, 201, [session_cookie_header(token, config.SESSION_TTL_SECONDS)])


@router.route("POST", "/api/auth/login", auth_required=False)
def login(req):
    body = req.json()
    user, token = auth.login(body.get("email"), body.get("password"))
    return json_response({"user": user}, 200, [session_cookie_header(token, config.SESSION_TTL_SECONDS)])


@router.route("POST", "/api/auth/logout", auth_required=False)
def logout(req):
    auth.logout(req.token)
    return json_response({"ok": True}, 200, [session_cookie_header("", 0)])


@router.route("GET", "/api/auth/me", auth_required=False)
def me(req):
    return json_response({"user": req.user})
