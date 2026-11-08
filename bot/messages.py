"""消息文本与按钮构造（Telegram HTML 格式）。"""
from __future__ import annotations

from datetime import datetime
from html import escape
from typing import Sequence
from urllib.parse import urlparse
from zoneinfo import ZoneInfo

from telegram import InlineKeyboardButton, InlineKeyboardMarkup

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


def event_message(
    site_name: str,
    site_url: str,
    events: Sequence[dict],
    tz: ZoneInfo,
) -> tuple[str, InlineKeyboardMarkup | None]:
    """把同一站点的多个事件合并成一条消息。events: {kind, product(dict-like), duration}"""
    titles = {"restock": "🟢 补货通知", "new": "🆕 上新通知", "soldout": "🔴 售罄通知"}
    kinds = {e["kind"] for e in events}
    if len(kinds) == 1:
        header = titles[next(iter(kinds))]
    else:
        header = "📦 库存变动"
    lines = [f"<b>{header}</b> · {h(site_name)}", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for e in events:
        p = e["product"]
        tag = {"restock": "补货", "new": "上新", "soldout": "售罄"}[e["kind"]]
        lines.append(f"<b>[{tag}] {h(p['name'])}</b>")
        if p["price"]:
            lines.append(f"  价格：{h(p['price'])}")
        if e["kind"] == "soldout":
            lines.append(f"  本次有货持续：{fmt_duration(e.get('duration'))}")
        else:
            lines.append(f"  库存：{h(p['qty']) if p['qty'] is not None else '有货（未显示数量）'}")
        lines.append("")
        if e["kind"] != "soldout" and p["order_url"] and len(buttons) < 8:
            label = f"🛒 {p['name']}"[:40]
            buttons.append([InlineKeyboardButton(label, url=p["order_url"])])
    lines.append(f"⏰ {fmt_time(events[0]['ts'], tz)}")
    buttons.append([InlineKeyboardButton("📄 打开商品页", url=site_url)])
    return "\n".join(lines).strip(), InlineKeyboardMarkup(buttons)


def render_notice(
    site_name: str,
    site_url: str,
    items: Sequence[dict],
    tz: ZoneInfo,
    updated_ts: int | None = None,
) -> tuple[str, InlineKeyboardMarkup]:
    """补货/上新消息。售罄后用同一函数重新渲染并编辑原消息。

    items: {kind, name, price, qty, order_url, ts, soldout_ts}
    """
    sold = [it for it in items if it.get("soldout_ts")]
    if not sold:
        kinds = {it["kind"] for it in items}
        header = "🆕 上新通知" if kinds == {"new"} else "🟢 补货通知"
    elif len(sold) == len(items):
        header = "🔴 已售罄"
    else:
        header = "🟡 部分售罄"

    lines = [f"<b>{header}</b> · {h(site_name)}", ""]
    buttons: list[list[InlineKeyboardButton]] = []
    for it in items:
        tag = "上新" if it["kind"] == "new" else "补货"
        if it.get("soldout_ts"):
            lines.append(f"🔴 <s>[{tag}] {h(it['name'])}</s> <b>已售罄</b>")
            if it.get("price"):
                lines.append(f"  价格：{h(it['price'])}")
            lines.append(
                f"  {fmt_time(it['soldout_ts'], tz)} 售罄，有货持续 {fmt_duration(it['soldout_ts'] - it['ts'])}"
            )
        else:
            lines.append(f"<b>[{tag}] {h(it['name'])}</b>")
            if it.get("price"):
                lines.append(f"  价格：{h(it['price'])}")
            qty = it.get("qty")
            lines.append(f"  库存：{h(qty) if qty is not None else '有货（未显示数量）'}")
            if it.get("order_url") and len(buttons) < 8:
                buttons.append([InlineKeyboardButton(f"🛒 {it['name']}"[:40], url=it["order_url"])])
        lines.append("")
    lines.append(f"⏰ 补货时间 {fmt_time(min(it['ts'] for it in items), tz)}")
    if updated_ts:
        lines.append(f"🔄 状态更新 {fmt_time(updated_ts, tz)}")
    buttons.append([InlineKeyboardButton("📄 打开商品页", url=site_url)])
    return "\n".join(lines), InlineKeyboardMarkup(buttons)
