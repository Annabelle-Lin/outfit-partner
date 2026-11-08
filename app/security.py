# -*- coding: utf-8 -*-
"""认证与密码安全模块。

设计目标（对应需求一）：新用户注册后拥有独立账号；老用户登录后取回自己
账号下的全部内容（衣物、会话、反馈、评估、偏好）。

做法：
1. 密码：PBKDF2-HMAC-SHA256 + 每用户随机盐，只存不可逆摘要；
2. 登录态：HMAC 签名的无状态 token（内含 user_id + 过期时间），
   每次请求由中间件校验，校验通过后把 user_id 写入 request.state；
3. 数据归属：所有业务接口都以 token 中的 user_id 为准，
   中间件还会校验请求里出现过的 user_id 是否与 token 一致，防止越权读取他人数据。

不引入额外三方依赖（只用标准库 + FastAPI/Starlette）。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import time
from pathlib import Path

from starlette.responses import JSONResponse

# 迭代次数：兼顾移动端低配设备与安全强度
_PBKDF2_ITERATIONS = 120_000
_SECRET_FILE = Path.home() / ".wardrobe" / "auth.key"
_TOKEN_TTL = 30 * 24 * 3600  # 30 天


# ---------- 签名密钥 ----------
def _load_secret() -> bytes:
    """取签名密钥：优先环境变量，其次本地密钥文件（首次自动生成）。"""
    env = os.environ.get("AUTH_SECRET", "").strip()
    if env:
        return env.encode("utf-8")
    try:
        if _SECRET_FILE.exists():
            raw = _SECRET_FILE.read_bytes().strip()
            if raw:
                return raw
        _SECRET_FILE.parent.mkdir(parents=True, exist_ok=True)
        secret = secrets.token_hex(32).encode("utf-8")
        _SECRET_FILE.write_bytes(secret)
        try:
            os.chmod(_SECRET_FILE, 0o600)
        except OSError:
            pass
        return secret
    except OSError:
        # 极端情况下（目录只读）退化为进程内随机密钥，重启后需重新登录
        return secrets.token_hex(32).encode("utf-8")


_SECRET = _load_secret()


# ---------- 密码 ----------
def hash_password(password: str) -> str:
    """生成 `pbkdf2_sha256$迭代$盐$摘要` 格式的密码摘要。"""
    salt = secrets.token_hex(16)
    dk = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), _PBKDF2_ITERATIONS)
    return f"pbkdf2_sha256${_PBKDF2_ITERATIONS}${salt}${dk.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """校验密码，使用恒定时间比较避免时序侧信道。"""
    if not stored or not password:
        return False
    try:
        algo, iterations, salt, digest = stored.split("$", 3)
        if algo != "pbkdf2_sha256":
            return False
        dk = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(iterations)
        )
        return hmac.compare_digest(dk.hex(), digest)
    except (ValueError, TypeError):
        return False


# ---------- 登录 token ----------
def _sign(payload: str) -> str:
    return hmac.new(_SECRET, payload.encode("utf-8"), hashlib.sha256).hexdigest()


def issue_token(user_id: str, ttl: int = _TOKEN_TTL) -> str:
    """签发登录凭证：user_id.exp.signature（base64 包一层，避免特殊字符）。"""
    exp = int(time.time()) + int(ttl)
    payload = f"{user_id}.{exp}"
    token = f"{payload}.{_sign(payload)}"
    return base64.urlsafe_b64encode(token.encode("utf-8")).decode("ascii")


def parse_token(token: str) -> str | None:
    """校验并解析 token，成功返回 user_id，失败返回 None。"""
    if not token:
        return None
    try:
        raw = base64.urlsafe_b64decode(token.encode("ascii")).decode("utf-8")
        user_id, exp_s, sig = raw.rsplit(".", 2)
        exp = int(exp_s)
    except Exception:
        return None
    if exp < int(time.time()):
        return None
    if not hmac.compare_digest(sig, _sign(f"{user_id}.{exp}")):
        return None
    return user_id


# ---------- 中间件 ----------
# 无需登录即可访问的接口
_PUBLIC_PATHS = {
    "/api/v1/user/register",
    "/api/v1/user/login",
    "/api/v1/user/username_available",
}
# 无需登录即可访问的前缀（天气查询、静态资源、文档）
_PUBLIC_PREFIXES = (
    "/api/v1/weather",
    "/uploads",
    "/health",
    "/docs",
    "/openapi.json",
    "/redoc",
)


async def _empty_receive():
    """给 JSONResponse 用的空 receive。"""
    return {"type": "http.request", "body": b"", "more_body": False}


class AuthMiddleware:
    """统一登录校验：保护 /api/v1 下的业务接口。

    - token 无效 / 过期 -> 401
    - 请求里带的 user_id 与 token 不一致 -> 403
    - 校验通过：request.state.user_id 可用，下游接口直接取用
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        method = scope.get("method", "GET").upper()
        if method == "OPTIONS" or not self._protected(path):
            await self.app(scope, receive, send)
            return

        headers = {
            k.decode("latin-1").lower(): v.decode("latin-1")
            for k, v in (scope.get("headers") or [])
        }
        auth = (headers.get("authorization") or "").strip()
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else auth
        user_id = parse_token(token)
        if not user_id:
            await self._deny(scope, send, 401, "未登录或登录已过期，请重新登录")
            return
        if not self._user_exists(user_id):
            await self._deny(scope, send, 401, "账号不存在，请重新登录")
            return

        # 查询参数里的 user_id 必须与 token 一致
        qs_uid = self._query_user_id(scope)
        if qs_uid is not None and qs_uid != user_id:
            await self._deny(scope, send, 403, "无权访问他人数据")
            return

        content_type = (headers.get("content-type") or "").lower()
        if "application/json" in content_type:
            body, _messages = await self._read_body(receive)
            # 先写入登录用户再放行：GET/无 body 的请求同样需要 request.state.user_id，
            # 否则下游 assert_owner / current_user_id 会误判为「未登录」。
            state = scope.setdefault("state", {})
            state["user_id"] = user_id
            if not body:
                await self.app(scope, self._make_receive(b"", receive), send)
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except (ValueError, UnicodeDecodeError):
                payload = None
            if isinstance(payload, dict):
                body_uid = payload.get("user_id")
                if isinstance(body_uid, str) and body_uid and body_uid != user_id:
                    await self._deny(scope, send, 403, "无权操作他人数据")
                    return
            state = scope.setdefault("state", {})
            state["user_id"] = user_id
            await self.app(scope, self._make_receive(body, receive), send)
            return

        state = scope.setdefault("state", {})
        state["user_id"] = user_id
        await self.app(scope, receive, send)

    # -- 内部工具 --
    @staticmethod
    def _protected(path: str) -> bool:
        if not path.startswith("/api/v1/"):
            return False
        if path.rstrip("/") in _PUBLIC_PATHS:
            return False
        return not any(path == p or path.startswith(p + "/") for p in _PUBLIC_PREFIXES)

    @staticmethod
    def _query_user_id(scope) -> str | None:
        from urllib.parse import parse_qs

        raw = (scope.get("query_string") or b"").decode("latin-1")
        if not raw:
            return None
        try:
            vals = parse_qs(raw).get("user_id") or []
        except Exception:
            return None
        return vals[0] if vals else None

    @staticmethod
    def _user_exists(user_id: str) -> bool:
        from app.db import SessionLocal, User

        db = SessionLocal()
        try:
            return db.query(User).filter(User.user_id == user_id).first() is not None
        finally:
            db.close()

    @staticmethod
    async def _read_body(receive) -> tuple[bytes, list]:
        body = b""
        messages = []
        while True:
            msg = await receive()
            messages.append(msg)
            if msg["type"] == "http.request":
                body += msg.get("body", b"")
                if not msg.get("more_body", False):
                    break
            elif msg["type"] == "http.disconnect":
                break
        return body, messages

    @staticmethod
    def _make_receive(body: bytes, original=None):
        """把已读出的 body 重新包装成 receive，供下游接口正常解析。

        只把「已缓冲的 body」返回一次，之后的调用必须交还给原始 receive。
        否则 StreamingResponse 的 listen_for_disconnect 会不停拿到 http.request，
        永远等不到 http.disconnect，退化成忙等死循环，把事件循环占满（表现为
        流式请求不返回、CPU 打满、整个服务假死）。
        """

        state = {"used": False}

        async def _receive():
            if not state["used"]:
                state["used"] = True
                return {"type": "http.request", "body": body, "more_body": False}
            if original is not None:
                return await original()
            # 没有原始 receive 可用时，必须真正挂起而不是空转，
            # 给调用一个「客户端已断开」的终态，避免再次忙等。
            import anyio

            await anyio.sleep(3600)
            return {"type": "http.disconnect"}

        return _receive

    @staticmethod
    async def _deny(scope, send, status: int, msg: str):
        """直接返回统一错误结构，避免进入下游接口。"""
        resp = JSONResponse({"code": status, "msg": msg, "data": None}, status_code=status)
        await resp(scope, _empty_receive, send)


# ---------- 供接口使用的当前用户 ----------
def current_user_id(request) -> str:
    """从 request.state 取当前登录用户（由 AuthMiddleware 写入）。

    取不到说明中间件没覆盖到该路由，直接拒绝，避免出现「无主」请求。
    """
    from fastapi import HTTPException

    uid = getattr(getattr(request, "state", None), "user_id", None)
    if not uid:
        raise HTTPException(status_code=401, detail="未登录，请重新登录")
    return uid


def assert_owner(owner_id: str, request) -> None:
    """校验资源归属：资源不属于当前登录用户时返回 403。"""
    from fastapi import HTTPException

    uid = current_user_id(request)
    if (owner_id or "") != uid:
        raise HTTPException(status_code=403, detail="无权访问他人数据")
