"""多账号存储（2026-11-15 员工权限隔离）：var/data/users.json。

角色两档：总监（全量可见+账号管理+配置管理）/ 员工（只见自己查询过的企业）。
密码 pbkdf2 加盐哈希落盘，明文永不进文件/日志/报告。
引导：首次使用时若 users.json 不存在，用 .env 的 GEO_AUTH_USER/GEO_AUTH_PASSWORD
建同名总监账号——老部署升级零操作，原凭据直接可登录。
"""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import threading
import time

from paths import DATA_DIR

USERS_FILE = DATA_DIR / "users.json"
ROLE_DIR = "总监"
ROLE_STAFF = "员工"
_lock = threading.Lock()


def _hash(pw: str, salt: str | None = None) -> str:
    salt = salt or os.urandom(8).hex()
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt.encode(), 120_000)
    return f"{salt}${dk.hex()}"


def _load() -> list[dict]:
    try:
        return json.loads(USERS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []


def _save(users: list[dict]):
    tmp = USERS_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(users, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(USERS_FILE)


def bootstrap(env_creds):
    """env_creds=(user,pass)：users.json 缺失/为空且有 .env 凭据时，建初始总监账号。"""
    with _lock:
        users = _load()
        if users or not (env_creds and env_creds[0] and env_creds[1]):
            return
        u, p = env_creds[0].strip(), env_creds[1].strip()
        users.append({"username": u, "name": u, "role": ROLE_DIR,
                      "pass": _hash(p), "created": time.strftime("%Y-%m-%d %H:%M")})
        _save(users)


def verify(username: str, password: str) -> dict | None:
    """凭据正确且账号启用时返回 {username,name,role}，否则 None。恒时比较防时序探测。
    禁用账号与密码错误同回 None（不向登录方泄露禁用状态）。"""
    for x in _load():
        salt = x["pass"].split("$", 1)[0]
        if hmac.compare_digest(x["pass"], _hash(password, salt)) \
                and hmac.compare_digest(x["username"], username):
            if not x.get("enabled", True):
                return None
            return {"username": x["username"], "name": x.get("name") or x["username"],
                    "role": x.get("role") or ROLE_STAFF}
    return None


def list_users() -> list[dict]:
    """管理列表（不含口令哈希）。"""
    return [{"username": x["username"], "name": x.get("name") or "",
             "role": x.get("role") or ROLE_STAFF, "created": x.get("created") or "",
             "enabled": x.get("enabled", True)}
            for x in _load()]


def add_user(username: str, name: str, password: str, role: str) -> str | None:
    """成功返回 None，失败返回中文原因。"""
    username, name = username.strip(), name.strip()
    if not username or not password:
        return "登录名和密码不能为空"
    if role not in (ROLE_DIR, ROLE_STAFF):
        return f"角色只能是{ROLE_DIR}或{ROLE_STAFF}"
    # 登录名只允许字母/数字（2026-11-16 定）：中文/符号一律拒
    if not (2 <= len(username) <= 40 and username.isascii() and username.isalnum()):
        return "登录名只能是2~40位字母或数字"
    with _lock:
        users = _load()
        if any(x["username"] == username for x in users):
            return f"登录名「{username}」已存在"
        users.append({"username": username, "name": name or username, "role": role,
                      "pass": _hash(password), "created": time.strftime("%Y-%m-%d %H:%M"),
                      "enabled": True})
        _save(users)
    return None


def update_user(username: str, *, name=None, password=None, role=None,
                enabled=None) -> str | None:
    """字段为 None=不改。enabled=False 禁用（禁用即不可登录，存量会话由调用方踢出）。
    改最后一个启用中总监的角色、禁用最后一个启用中总监都会被拒绝（防无人可管）。"""
    with _lock:
        users = _load()
        x = next((x for x in users if x["username"] == username), None)
        if x is None:
            return f"账号「{username}」不存在"
        _is_dir = x.get("role") == ROLE_DIR
        _dir_cnt = sum(1 for i in users
                       if i.get("role") == ROLE_DIR and i.get("enabled", True))
        if role is not None:
            if role not in (ROLE_DIR, ROLE_STAFF):
                return f"角色只能是{ROLE_DIR}或{ROLE_STAFF}"
            if _is_dir and role != ROLE_DIR and _dir_cnt <= 1:
                return "至少保留一个启用中的总监账号"
            x["role"] = role
        if enabled is not None:
            if not enabled and _is_dir and _dir_cnt <= 1:
                return "不能禁用最后一个启用中的总监账号"
            x["enabled"] = bool(enabled)
        if name is not None and name.strip():
            x["name"] = name.strip()
        if password:
            x["pass"] = _hash(password)
        _save(users)
    return None


def delete_user(username: str) -> str | None:
    with _lock:
        users = _load()
        x = next((x for x in users if x["username"] == username), None)
        if x is None:
            return f"账号「{username}」不存在"
        if x.get("role") == ROLE_DIR and sum(1 for i in users if i.get("role") == ROLE_DIR) <= 1:
            return "至少保留一个总监账号"
        _save([i for i in users if i["username"] != username])
    return None


def has_users() -> bool:
    return bool(_load())
