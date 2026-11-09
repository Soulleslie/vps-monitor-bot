"""Telegram 机器人生命周期管理：支持在 Web 后台修改 Token 后热重启。"""
from __future__ import annotations

import asyncio
import logging

from urllib.parse import quote

from telegram import Bot, BotCommand, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ParseMode
from telegram.error import BadRequest, Forbidden, InvalidToken, RetryAfter, TelegramError
from telegram.ext import AIORateLimiter, Application

from . import handlers
from .config import Config
from .db import DB

log = logging.getLogger(__name__)

USER_COMMANDS = [
    BotCommand("id", "查看当前会话的 Chat ID"),
]


async def test_token(token: str) -> str:
    """验证 Token，返回机器人用户名；无效时抛 ValueError。"""
    try:
        async with Bot(token) as bot:
            me = await bot.get_me()
            return me.username
    except InvalidToken:
        raise ValueError("Token 无效（被拒绝），请检查是否复制完整") from None
    except TelegramError as exc:
        raise ValueError(f"无法连接 Telegram：{exc}") from None


class BotRunner:
    def __init__(self, cfg: Config, db: DB) -> None:
        self.cfg = cfg
        self.db = db
        self.shared: dict = {}          # 注入 handlers 的 bot_data（cfg/db/monitor/auth…）
        self.app: Application | None = None
        self.username: str | None = None
        self.error: str | None = None
        self._lock = asyncio.Lock()

    @property
    def running(self) -> bool:
        return self.app is not None

    async def start(self) -> None:
        async with self._lock:
            await self._start_locked()

    async def _start_locked(self) -> None:
        if self.app is not None:
            return
        if not self.cfg.bot_token:
            self.error = "未设置 Bot Token"
            return
        app = (
            Application.builder()
            .token(self.cfg.bot_token)
            .rate_limiter(AIORateLimiter(max_retries=3))
            .concurrent_updates(True)
            .build()
        )
        app.bot_data.update(self.shared, sender=self.send)
        handlers.register(app)
        try:
            await app.initialize()
            me = await app.bot.get_me()
            await app.bot.set_my_commands(USER_COMMANDS)
            await app.start()
            await app.updater.start_polling(
                allowed_updates=["message", "my_chat_member"], drop_pending_updates=True
            )
        except (InvalidToken, TelegramError) as exc:
            self.error = "Token 无效" if isinstance(exc, InvalidToken) else f"连接 Telegram 失败：{exc}"
            log.error("机器人启动失败：%s", self.error)
            try:
                await app.shutdown()
            except Exception:
                pass
            return
        self.app, self.username, self.error = app, me.username, None
        log.info("机器人 @%s 已连接", me.username)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        app, self.app = self.app, None
        if app is None:
            return
        try:
            if app.updater and app.updater.running:
                await app.updater.stop()
            if app.running:
                await app.stop()
            await app.shutdown()
        except Exception:
            log.exception("停止机器人时出错")
        log.info("机器人已断开")

    async def restart(self) -> None:
        async with self._lock:
            await self._stop_locked()
            await self._start_locked()

    async def send(self, chat_id: int, text: str, markup) -> int | None:
        """发送消息，返回消息 ID；失败返回 None。"""
        app = self.app
        if app is None:
            log.warning("机器人未连接，消息未发送（chat=%s）", chat_id)
            return None
        markup = to_markup(markup)
        for attempt in range(3):
            try:
                msg = await app.bot.send_message(
                    chat_id, text, parse_mode=ParseMode.HTML, reply_markup=markup, disable_web_page_preview=True
                )
                return msg.message_id
            except RetryAfter as exc:
                await asyncio.sleep(_retry_seconds(exc) + 1)
            except Forbidden:
                # 用户屏蔽了机器人 / 机器人被移出群组：暂停该会话，避免每次都失败
                log.info("会话 %s 无法送达（Forbidden），已自动暂停", chat_id)
                await self.db.set_chat_flag(chat_id, "paused", 1)
                return None
            except BadRequest as exc:
                if markup is not None and _button_problem(exc):
                    log.info("按钮被 Telegram 拒绝，改为不带按钮发送：%s", exc)
                    markup = None
                    continue
                if "chat not found" in str(exc).lower():
                    await self.db.set_chat_flag(chat_id, "paused", 1)
                log.warning("发送到 %s 失败: %s", chat_id, exc)
                return None
            except TelegramError as exc:
                log.warning("发送到 %s 失败（第 %s 次）: %s", chat_id, attempt + 1, exc)
                await asyncio.sleep(2 * (attempt + 1))
        return None

    async def edit(self, chat_id: int, message_id: int, text: str, markup) -> bool:
        """编辑已发出的消息。返回 False 表示这条消息以后不必再尝试编辑（已删除、无权限等）。"""
        app = self.app
        if app is None:
            return True  # 暂时连不上，保留下次再试
        markup = to_markup(markup)
        for _ in range(3):
            try:
                await app.bot.edit_message_text(
                    text, chat_id=chat_id, message_id=message_id, parse_mode=ParseMode.HTML,
                    reply_markup=markup, disable_web_page_preview=True,
                )
                return True
            except RetryAfter as exc:
                await asyncio.sleep(_retry_seconds(exc) + 1)
            except BadRequest as exc:
                if "not modified" in str(exc).lower():
                    return True
                if markup is not None and _button_problem(exc):
                    markup = None
                    continue
                log.info("无法编辑消息 %s/%s：%s", chat_id, message_id, exc)
                return False
            except Forbidden:
                return False
            except TelegramError as exc:
                log.warning("编辑消息 %s/%s 失败：%s", chat_id, message_id, exc)
                return True
        return True


def to_markup(buttons) -> InlineKeyboardMarkup | None:
    """[(文字, 网址)] → Telegram 按钮。None 表示不带按钮；空列表表示移除原有按钮（编辑消息时）。

    网址中的方括号等字符先做百分号编码（如 configoption[75]=812），否则 Telegram 可能拒绝。"""
    if buttons is None:
        return None
    if isinstance(buttons, InlineKeyboardMarkup):
        return buttons
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(text, url=quote(url, safe=":/?&=%#+,;@!$'()*~._-"))] for text, url in buttons
    ])


def _button_problem(exc: BadRequest) -> bool:
    msg = str(exc).lower()
    return "button" in msg or "url" in msg or "reply markup" in msg


def _retry_seconds(exc: RetryAfter) -> float:
    wait = exc.retry_after
    return wait.total_seconds() if hasattr(wait, "total_seconds") else float(wait)
