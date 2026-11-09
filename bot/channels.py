"""推送渠道：Telegram 之外，还支持 Bark、企业微信群机器人、PushPlus 和自定义 Webhook。

所有渠道都登记在 chats 表里（channel 列区分），这样订阅、暂停、测试、群发等功能对所有渠道通用。
非 Telegram 渠道的 chat_id 从 CHANNEL_ID_BASE 开始分配，远大于 Telegram 的 ID，又小于网页 JS
能精确表示的最大整数（2^53），不会冲突。

非 Telegram 渠道无法修改已发出的消息，所以售罄时改为另发一条“🔴 售罄”消息。
"""
from __future__ import annotations

import json
import logging
import re
from typing import Awaitable, Callable
from urllib.parse import urlparse

import httpx

from .db import DB, load_meta
from .messages import html_to_text

log = logging.getLogger(__name__)

CHANNEL_ID_BASE = 4_000_000_000_000_000
SENT_OK = -1     # 非 Telegram 渠道发送成功时返回的“消息 ID”（不能编辑）

CHANNELS = {
    "telegram": "Telegram",
    "bark": "Bark",
    "wecom": "企业微信",
    "pushplus": "PushPlus",
    "webhook": "Webhook",
}

TgSend = Callable[[int, str, object], Awaitable[int | None]]
TgEdit = Callable[[int, int, str, object], Awaitable[bool]]


class ChannelError(ValueError):
    pass


def _http_url(raw: str, what: str) -> str:
    url = (raw or "").strip()
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname or re.search(r"\s", url):
        raise ChannelError(f"{what}必须是以 http:// 或 https:// 开头的完整网址")
    return url


def parse_config(channel: str, raw: str) -> dict:
    """把网页上填写的内容转成渠道配置；格式不对时抛 ChannelError。"""
    raw = (raw or "").strip()
    if channel == "bark":
        # 支持 https://api.day.app/KEY 或 https://api.day.app/KEY/ 以及自建服务器
        url = _http_url(raw, "Bark 地址")
        parsed = urlparse(url)
        parts = [p for p in parsed.path.split("/") if p]
        if not parts:
            raise ChannelError("Bark 地址里没有找到设备 Key，应类似 https://api.day.app/xxxxxxxx")
        server = f"{parsed.scheme}://{parsed.netloc}" + ("/" + "/".join(parts[:-1]) if len(parts) > 1 else "")
        return {"server": server, "key": parts[-1]}
    if channel == "wecom":
        url = _http_url(raw, "企业微信机器人地址")
        if "key=" not in url:
            raise ChannelError("企业微信机器人地址应类似 https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=...")
        return {"url": url}
    if channel == "pushplus":
        if not re.fullmatch(r"[A-Za-z0-9]{16,64}", raw):
            raise ChannelError("PushPlus Token 格式不正确（16～64 位字母或数字）")
        return {"token": raw}
    if channel == "webhook":
        return {"url": _http_url(raw, "Webhook 地址")}
    raise ChannelError("不支持的推送渠道")


def describe(channel: str, config: dict) -> str:
    """网页上显示的渠道摘要，不包含密钥。"""
    if channel == "bark":
        return urlparse(config.get("server", "")).netloc
    if channel in ("wecom", "webhook"):
        return urlparse(config.get("url", "")).netloc
    if channel == "pushplus":
        return "pushplus.plus"
    return ""


def split_title(text: str) -> tuple[str, str]:
    """消息第一行作为标题，其余作为正文。"""
    title, _, rest = text.partition("\n")
    return title.strip(), rest.strip()


def first_url(buttons) -> str | None:
    if buttons:
        return buttons[0][1]
    return None


class Notifier:
    """按推送对象的渠道发送消息。Monitor 和网页后台都通过它发送。"""

    def __init__(self, db: DB, tg_send: TgSend, tg_edit: TgEdit, client: httpx.AsyncClient | None = None) -> None:
        self.db = db
        self.tg_send = tg_send
        self.tg_edit = tg_edit
        self.client = client or httpx.AsyncClient(timeout=15, follow_redirects=False)

    async def close(self) -> None:
        await self.client.aclose()

    async def _chat(self, chat_id: int):
        return await self.db.get_chat(chat_id)

    async def send(self, chat_id: int, html: str, buttons) -> int | None:
        chat = await self._chat(chat_id)
        if chat is None or chat["channel"] == "telegram":
            return await self.tg_send(chat_id, html, buttons)
        ok = await self.push(chat["channel"], load_meta(chat["config"]), html, buttons)
        return SENT_OK if ok else None

    async def edit(self, chat_id: int, message_id: int, html: str, buttons) -> bool:
        chat = await self._chat(chat_id)
        if chat is None or chat["channel"] == "telegram":
            return await self.tg_edit(chat_id, message_id, html, buttons)
        # 不能修改已发出的消息：另发一条（售罄），之后不再处理这条记录
        if not chat["paused"]:
            await self.push(chat["channel"], load_meta(chat["config"]), html, buttons)
        return False

    async def push(self, channel: str, config: dict, html: str, buttons) -> bool:
        text = html_to_text(html)
        title, body = split_title(text)
        url = first_url(buttons)
        try:
            if channel == "bark":
                payload = {"device_key": config["key"], "title": title, "body": body or title, "group": "VPS补货"}
                if url:
                    payload["url"] = url
                r = await self.client.post(f"{config['server'].rstrip('/')}/push", json=payload)
                return self._ok(channel, r, lambda d: d.get("code") == 200)
            if channel == "wecom":
                r = await self.client.post(config["url"], json={"msgtype": "text", "text": {"content": text}})
                return self._ok(channel, r, lambda d: d.get("errcode") == 0)
            if channel == "pushplus":
                r = await self.client.post("https://www.pushplus.plus/send", json={
                    "token": config["token"], "title": title, "content": body or title, "template": "txt",
                })
                return self._ok(channel, r, lambda d: d.get("code") == 200)
            if channel == "webhook":
                r = await self.client.post(config["url"], json={"title": title, "text": body, "url": url, "html": html})
                return self._ok(channel, r, None)
        except (httpx.HTTPError, KeyError) as exc:
            log.warning("%s 推送失败：%s", CHANNELS.get(channel, channel), exc.__class__.__name__)
            return False
        log.warning("未知的推送渠道：%s", channel)
        return False

    @staticmethod
    def _ok(channel: str, r: httpx.Response, check) -> bool:
        if r.status_code >= 300:
            log.warning("%s 推送失败：HTTP %s", CHANNELS[channel], r.status_code)
            return False
        if check is None:
            return True
        try:
            data = r.json()
        except (ValueError, json.JSONDecodeError):
            data = {}
        if not isinstance(data, dict) or not check(data):
            # 只记录返回内容的前 200 个字符，避免日志过长；返回内容不含我们的密钥
            log.warning("%s 推送失败：%s", CHANNELS[channel], r.text[:200])
            return False
        return True
