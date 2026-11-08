"""Web 后台认证。

使用管理员访问令牌登录，登录成功后发放会话 Cookie（7 天有效）。数据库里只保存令牌的 SHA-256 哈希。

令牌来源与优先级：
- 首次启动：使用 .env 中的 WEB_ADMIN_TOKEN；未设置则自动生成，并在日志里显示一次。
- 之后在 Web 后台或用 reset-web-token 命令更换令牌，重启后依然有效（不会被 .env 覆盖）。
- 只有当 .env 中的 WEB_ADMIN_TOKEN 被手动改成新值时，才以 .env 为准。
任何一次更换都会注销所有已登录的会话，旧令牌立即失效。
"""
from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from collections import defaultdict, deque

from .config import Config
from .db import DB

log = logging.getLogger(__name__)

SESSION_TTL = 7 * 86400
MAX_FAILS = 10
FAIL_WINDOW = 900
ADMIN_TOKEN_KEY = "web_admin_token_hash"
ENV_SEEN_KEY = "web_admin_env_token_hash"   # 上次见到的 .env 令牌，用于判断 .env 是否被手动修改


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


class WebAuth:
    def __init__(self, cfg: Config, db: DB) -> None:
        self.cfg = cfg
        self.db = db
        self._admin_hash: str | None = None
        self._fails: dict[str, deque] = defaultdict(deque)

    async def init(self) -> None:
        settings = await self.db.get_settings()
        stored = settings.get(ADMIN_TOKEN_KEY)
        env_token = self.cfg.web_admin_token
        if env_token:
            if len(env_token) < 16:
                raise SystemExit("WEB_ADMIN_TOKEN 太短，至少 16 个字符")
            env_hash = sha256(env_token)
            if settings.get(ENV_SEEN_KEY) != env_hash:
                # 首次启动，或 .env 中的令牌被手动修改：以 .env 为准
                await self.db.save_settings({ENV_SEEN_KEY: env_hash})
                if stored != env_hash:
                    await self._set_hash(env_hash)
                    if stored:
                        log.info("检测到 .env 中的访问令牌已更换，已注销所有 Web 登录会话")
                else:
                    self._admin_hash = env_hash
                return
        if stored:
            self._admin_hash = stored  # 沿用（可能是在 Web 后台更换过的）令牌
            return
        token = await self.reset_admin_token()
        log.warning(
            "\n" + "=" * 60 + "\n已生成 Web 后台访问令牌（只显示这一次，请妥善保存）：\n  %s\n"
            "忘记后可运行：python -m bot reset-web-token\n" + "=" * 60,
            token,
        )

    async def _set_hash(self, token_hash: str) -> None:
        self._admin_hash = token_hash
        await self.db.save_settings({ADMIN_TOKEN_KEY: token_hash})
        await self.db.delete_all_sessions()  # 旧令牌失效，所有已登录的浏览器需要重新登录

    async def reset_admin_token(self) -> str:
        """生成新令牌（只返回这一次），旧令牌立即失效并注销所有会话。"""
        token = secrets.token_urlsafe(24)
        await self._set_hash(sha256(token))
        return token

    # ---------- 防暴力破解 ----------
    def is_locked(self, ip: str) -> bool:
        q = self._fails[ip]
        cutoff = time.monotonic() - FAIL_WINDOW
        while q and q[0] < cutoff:
            q.popleft()
        return len(q) >= MAX_FAILS

    def record_fail(self, ip: str) -> None:
        self._fails[ip].append(time.monotonic())

    # ---------- 校验 ----------
    def check_admin_token(self, token: str) -> bool:
        if not self._admin_hash or not token:
            return False
        return hmac.compare_digest(sha256(token.strip()), self._admin_hash)

    # ---------- 会话 ----------
    async def new_session(self, ip: str | None) -> str:
        token = secrets.token_urlsafe(32)
        await self.db.create_session(sha256(token), SESSION_TTL, ip)
        return token

    async def session_ok(self, token: str | None) -> bool:
        return bool(token) and await self.db.session_valid(sha256(token))

    async def end_session(self, token: str | None) -> None:
        if token:
            await self.db.delete_session(sha256(token))
