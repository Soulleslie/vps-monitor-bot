"""Telegram 端：只负责推送，不提供任何管理功能。

唯一的交互是告诉对方当前会话的 Chat ID，并把会话登记到数据库，
这样管理员就能在 Web 后台把它选为推送对象。
"""
from __future__ import annotations

import logging
from html import escape

from telegram import Update
from telegram.constants import ChatMemberStatus, ChatType, ParseMode
from telegram.ext import Application, ChatMemberHandler, CommandHandler, ContextTypes

from .db import DB

log = logging.getLogger(__name__)


def _title(update: Update) -> str:
    chat = update.effective_chat
    if chat.type == ChatType.PRIVATE:
        user = update.effective_user
        if user and user.username:
            return f"@{user.username}"
        return user.full_name if user else str(chat.id)
    return chat.title or str(chat.id)


async def cmd_id(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    if chat is None or update.effective_message is None:
        return
    db: DB = ctx.application.bot_data["db"]
    await db.ensure_chat(chat.id, _title(update), chat.type)
    await update.effective_message.reply_text(
        "这是一个只负责推送的补货提醒机器人。\n\n"
        f"当前会话 Chat ID：<code>{chat.id}</code>\n"
        f"名称：{escape(_title(update))}\n\n"
        "已登记。管理员在 Web 后台的“推送对象”里就能看到并选择它。",
        parse_mode=ParseMode.HTML,
    )


async def on_my_chat_member(update: Update, ctx: ContextTypes.DEFAULT_TYPE):
    """机器人被拉进群/频道或被移除时，自动登记或暂停。"""
    change = update.my_chat_member
    if change is None:
        return
    db: DB = ctx.application.bot_data["db"]
    chat = change.chat
    status = change.new_chat_member.status
    if status in (ChatMemberStatus.MEMBER, ChatMemberStatus.ADMINISTRATOR):
        await db.ensure_chat(chat.id, chat.title or chat.username or str(chat.id), chat.type)
        await db.set_chat_flag(chat.id, "paused", 0)
        log.info("已登记会话 %s（%s）", chat.id, chat.title)
    elif status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        if await db.get_chat(chat.id):
            await db.set_chat_flag(chat.id, "paused", 1)
        log.info("机器人已离开会话 %s，已暂停推送", chat.id)


async def on_error(update: object, ctx: ContextTypes.DEFAULT_TYPE):
    log.error("处理更新时出错", exc_info=ctx.error)


def register(app: Application) -> None:
    app.add_handler(CommandHandler(["start", "id"], cmd_id))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)
