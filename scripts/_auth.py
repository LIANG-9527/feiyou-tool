#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""门禁登录 helper（2026-09-24，配合 main.py 全站登录门禁）。
.env 配了 GEO_AUTH_USER/GEO_AUTH_PASSWORD 才生效；任一为空返回 None/静默，脚本保持原行为。"""
import http.cookiejar
import json
import os
import urllib.request

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAR = http.cookiejar.CookieJar()
OPENER = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(JAR))


def _creds():
    env = {}
    try:
        for line in open(os.path.join(REPO, ".env"), encoding="utf-8"):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, _, v = line.partition("=")
                env[k.strip()] = v.strip().strip('"').strip("'")
    except OSError:
        pass
    u = (env.get("GEO_AUTH_USER") or os.environ.get("GEO_AUTH_USER") or "").strip()
    p = (env.get("GEO_AUTH_PASSWORD") or os.environ.get("GEO_AUTH_PASSWORD") or "").strip()
    return u, p


def login(base: str):
    """门禁启用时登录并返回 geo_session Cookie 值；未启用返回 None。"""
    u, p = _creds()
    if not (u and p):
        return None
    req = urllib.request.Request(base.rstrip("/") + "/api/login",
                                 data=json.dumps({"username": u, "password": p}).encode(),
                                 headers={"Content-Type": "application/json"})
    OPENER.open(req, timeout=10).read()
    for c in JAR:
        if c.name == "geo_session":
            return c.value
    return None


def install(base: str):
    """登录成功后接管全局 urlopen（后续请求自动带会话 Cookie）。未启用则无操作。"""
    if login(base) is not None:
        urllib.request.install_opener(OPENER)
