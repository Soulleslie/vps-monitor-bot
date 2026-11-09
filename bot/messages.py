"""消息文本与按钮构造（Telegram HTML 格式）。"""
from __future__ import annotations

from datetime import datetime
import re
from html import escape, unescape
from urllib.parse import urlparse
from zoneinfo import ZoneInfo


from .parser import IN_STOCK, OUT_OF_STOCK

STATUS_ICON = {IN_STOCK: "🟢", OUT_OF_STOCK: "🔴"}
STATUS_TEXT = {IN_STOCK: "有货", OUT_OF_STOCK: "缺货"}


def status_label(status: str, qty: int | None) -> str:
    icon = STATUS_ICON.get(status, "⚪")
    text = STATUS_TEXT.get(status, "未知")
    if qty is not None and status == IN_STOCK:
        text += f" ×{qty}"
    return f"{icon}{text}"


def fmt_time(ts: int | None, tz: ZoneInfo) -> str:
    if not ts:
        return "-"
    return datetime.fromtimestamp(ts, tz).strftime("%m-%d %H:%M:%S")


def fmt_duration(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    seconds = max(0, int(seconds))
    if seconds < 60:
        return f"{seconds} 秒"
    if seconds < 3600:
        return f"{seconds // 60} 分 {seconds % 60} 秒"
    if seconds < 86400:
        return f"{seconds // 3600} 小时 {seconds % 3600 // 60} 分"
    return f"{seconds // 86400} 天 {seconds % 86400 // 3600} 小时"


def site_display_name(url: str, title: str | None = None) -> str:
    host = urlparse(url).hostname or url
    path = urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]
    base = f"{host}/{path}" if path and path not in ("store", "cart.php", "index.php") else host
    return base[:60]


def h(text: object) -> str:
    return escape(str(text) if text is not None else "", quote=False)


HEADERS = {
    "restock": "🟢 补货提醒", "new": "🆕 上新提醒", "soldout": "🔴 售罄",
    "price_down": "💲 降价提醒", "price_up": "💲 涨价提醒",
}
STATE_TAG = {"restock": "补货", "new": "上新", "soldout": "售罄", "price_down": "降价", "price_up": "涨价"}


def render_card(card, state: str, duration: int | None = None, old_price: str | None = None) -> str:
    """单个机型的推送消息（Telegram HTML）。state: restock / new / soldout / price_down / price_up。

    售罄时用同一函数重新渲染并编辑原消息：标题变为“🔴 售罄”，购买链接加删除线；
    duration 为本次有货的秒数，售罄时显示“⏱ 有货时长”。价格变动时 old_price 为原价格。
    """
    sold = state == "soldout"
    out = [HEADERS[state], ""]
    out.append(f"🏷 {h(card.title)}")
    if old_price and card.price:
        out.append(f"💰 <s>{h(old_price)}</s> → {h(card.price)}")
    elif card.price:
        out.append(f"💰 {h(card.price)}")
    if card.location:
        out.append(f"📍 {h(card.location)}")
    if card.lines:
        out.append(f"🌐 {h('/'.join(card.lines))}")
    if card.coupon:
        out.append(f"🎟 {h(card.coupon)}")
    if card.specs:
        out += ["", f"💻 {h(card.specs)}"]
    if card.buy_url:
        link = f'<a href="{escape(card.buy_url, quote=True)}">{h(card.buy_url)}</a>'
        out += ["", f"🛒 购买：{'<s>' + link + '</s>' if sold else link}"]
    if sold and duration is not None and duration > 0:
        out.append(f"⏱ 有货时长：约 {fmt_duration(duration)}")
    tags = card.tags + [STATE_TAG[state]]
    out += ["", " ".join(f"#{t}" for t in dict.fromkeys(tags))]
    return "\n".join(out)


def buy_buttons(card, state: str) -> list[tuple[str, str]]:
    """消息下方的按钮，形如 [(文字, 网址)]。售罄时返回空列表（编辑原消息时会移除按钮）。"""
    if state == "soldout" or not card.buy_url:
        return []
    return [("🛒 立即购买", card.buy_url)]


def html_to_text(html: str) -> str:
    """把推送用的 Telegram HTML 转成纯文本（供 Bark、企业微信等渠道使用）。"""
    return unescape(re.sub(r"<[^>]+>", "", html))
