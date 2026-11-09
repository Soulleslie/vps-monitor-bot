"""Web 管理后台（JSON API + 静态页面）。所有管理操作都在这里完成，Telegram 只负责推送。"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any

from aiohttp import web

from .auth import SESSION_TTL, WebAuth
from .backup import export_config, import_config
from .channels import CHANNEL_ID_BASE, CHANNELS, ChannelError, Notifier, describe, parse_config
from .config import INTERVAL_FLOOR, Config
from .db import DB, now
from .fetcher import FetchError, UnsafeURLError, normalize_url
from .db import load_meta
from .messages import h, render_card
from .meta import LOCATIONS, build_card, location_choices, merchant_from_url
from .monitor import Monitor, keyword_match
from .runner import BotRunner, test_token
from .updater import Updater

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
COOKIE = "vrb_session"
CSRF_HEADER = "X-Requested-With"
CSRF_VALUE = "vrb"
PUBLIC_API = {"/api/login", "/api/health"}
MANUAL_CHECK_GAP = 10

CFG = web.AppKey("cfg", Config)
DBK = web.AppKey("db", DB)
MON = web.AppKey("monitor", Monitor)
RUN = web.AppKey("runner", BotRunner)
AUTH = web.AppKey("auth", WebAuth)
UPD = web.AppKey("updater", Updater)
NOTI = web.AppKey("notifier", Notifier)
INDEX_HTML = web.AppKey("index_html", str)


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


# ---------------- 工具 ----------------
def client_ip(request: web.Request) -> str:
    if request.app[CFG].trust_proxy:
        xff = request.headers.get("X-Forwarded-For", "")
        if xff:
            return xff.split(",")[-1].strip()  # 最右侧是反向代理写入的真实来源
    return request.remote or "unknown"


def is_https(request: web.Request) -> bool:
    if request.secure:
        return True
    return request.app[CFG].trust_proxy and request.headers.get("X-Forwarded-Proto", "").lower() == "https"


def mask_token(token: str) -> str:
    if not token:
        return ""
    head, _, tail = token.partition(":")
    return f"{head}:{tail[:3]}…{tail[-3:]}" if len(tail) > 8 else "已设置"


async def body(request: web.Request) -> dict[str, Any]:
    try:
        data = await request.json()
    except Exception:
        raise ApiError("请求格式错误（需要 JSON）")
    if not isinstance(data, dict):
        raise ApiError("请求格式错误")
    return data


def row(r) -> dict | None:
    return dict(r) if r is not None else None


def int_param(request: web.Request, name: str) -> int:
    try:
        return int(request.match_info[name])
    except (KeyError, ValueError):
        raise ApiError("参数错误", 404)


# ---------------- 中间件 ----------------
@web.middleware
async def error_mw(request: web.Request, handler):
    try:
        return await handler(request)
    except ApiError as exc:
        return web.json_response({"error": str(exc)}, status=exc.status)
    except ValueError as exc:
        return web.json_response({"error": str(exc)}, status=400)
    except web.HTTPException:
        raise
    except Exception:
        log.exception("Web 请求出错: %s %s", request.method, request.path)
        return web.json_response({"error": "服务器内部错误，请查看日志"}, status=500)


@web.middleware
async def security_mw(request: web.Request, handler):
    resp = await handler(request)
    resp.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
        "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    )
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "no-referrer")
    if request.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-store"
    elif request.path.startswith("/static/"):
        # 每次使用前向服务器确认（未修改时返回 304），避免一键更新后手机浏览器继续用旧的样式/脚本
        resp.headers["Cache-Control"] = "no-cache"
    return resp


@web.middleware
async def auth_mw(request: web.Request, handler):
    if request.path.startswith("/api/"):
        if request.method not in ("GET", "HEAD") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
            raise ApiError("缺少安全请求头", 403)
        if request.path not in PUBLIC_API:
            if not await request.app[AUTH].session_ok(request.cookies.get(COOKIE)):
                raise ApiError("未登录或登录已过期", 401)
    return await handler(request)


# ---------------- 认证 ----------------
async def api_health(request: web.Request):
    return web.json_response({"ok": True})


async def api_login(request: web.Request):
    auth = request.app[AUTH]
    ip = client_ip(request)
    if auth.is_locked(ip):
        raise ApiError("失败次数过多，请 15 分钟后再试", 429)
    data = await body(request)
    if not auth.check_admin_token(str(data.get("token", ""))):
        auth.record_fail(ip)
        log.warning("Web 登录失败，来源 %s", ip)
        raise ApiError("访问令牌错误", 401)
    session = await auth.new_session(ip)
    resp = web.json_response({"ok": True})
    resp.set_cookie(
        COOKIE, session, max_age=SESSION_TTL, httponly=True, samesite="Strict", secure=is_https(request), path="/"
    )
    log.info("Web 登录成功，来源 %s", ip)
    return resp


async def api_logout(request: web.Request):
    await request.app[AUTH].end_session(request.cookies.get(COOKIE))
    resp = web.json_response({"ok": True})
    resp.del_cookie(COOKIE, path="/")
    return resp


async def api_rotate_token(request: web.Request):
    """更换访问令牌：需验证当前令牌；旧令牌立即失效，所有会话注销，当前浏览器自动重新登录。"""
    auth = request.app[AUTH]
    ip = client_ip(request)
    if auth.is_locked(ip):
        raise ApiError("失败次数过多，请 15 分钟后再试", 429)
    data = await body(request)
    if not auth.check_admin_token(str(data.get("current_token", ""))):
        auth.record_fail(ip)
        raise ApiError("当前访问令牌不正确", 403)
    new_token = await auth.reset_admin_token()  # 同时删除所有会话
    session = await auth.new_session(ip)
    log.warning("Web 访问令牌已在后台更换（来源 %s），所有其他登录已注销", ip)
    resp = web.json_response({"token": new_token})
    resp.set_cookie(
        COOKIE, session, max_age=SESSION_TTL, httponly=True, samesite="Strict", secure=is_https(request), path="/"
    )
    return resp


# ---------------- 概览 ----------------
def bot_state(runner: BotRunner, cfg: Config) -> dict:
    return {
        "running": runner.running,
        "username": runner.username,
        "error": runner.error,
        "has_token": bool(cfg.bot_token),
    }


async def api_overview(request: web.Request):
    db, mon, cfg = request.app[DBK], request.app[MON], request.app[CFG]
    one = lambda sql, p=(): db.fetchone(sql, p)  # noqa: E731
    counts = {
        "sites": (await one("SELECT COUNT(*) n FROM sites"))["n"],
        "active_sites": (await one("SELECT COUNT(DISTINCT site_id) n FROM subscriptions"))["n"],
        "failing_sites": (await one("SELECT COUNT(*) n FROM sites WHERE fail_count > 0"))["n"],
        "products": (await one("SELECT COUNT(*) n FROM products WHERE present = 1"))["n"],
        "in_stock": (await one("SELECT COUNT(*) n FROM products WHERE present = 1 AND status = 'in_stock'"))["n"],
        "chats": (await one("SELECT COUNT(*) n FROM chats"))["n"],
        "events_24h": (await one("SELECT COUNT(*) n FROM events WHERE ts > ? AND kind != 'price'", (now() - 86400,)))["n"],
    }
    events = await db.fetchall(
        "SELECT e.kind, e.qty, e.duration, e.ts, e.detail, p.name, s.id AS site_id, s.name AS site_name "
        "FROM events e JOIN products p ON p.id = e.product_id JOIN sites s ON s.id = p.site_id "
        "ORDER BY e.ts DESC, e.id DESC LIMIT 30"
    )
    # 近 14 天每天的补货/上新次数（按配置的时区划分日期）
    today = datetime.now(cfg.tz).date()
    days = [today - timedelta(days=i) for i in range(13, -1, -1)]
    start = int(datetime.combine(days[0], datetime.min.time(), cfg.tz).timestamp())
    daily = {d.isoformat(): {"restock": 0, "soldout": 0} for d in days}
    for r in await db.fetchall("SELECT kind, ts FROM events WHERE ts >= ? AND kind != 'price'", (start,)):
        key = datetime.fromtimestamp(r["ts"], cfg.tz).date().isoformat()
        if key in daily:
            daily[key]["soldout" if r["kind"] == "soldout" else "restock"] += 1
    return web.json_response({
        "bot": bot_state(request.app[RUN], cfg),
        "counts": counts,
        "daily": [{"date": k, **v} for k, v in daily.items()],
        "uptime": int(time.time() - mon.started_at),
        "checks": mon.total_checks,
        "pushed_events": mon.total_events,
        "check_interval": cfg.check_interval,
        "events": [dict(e) for e in events],
        "now": now(),
    })


# ---------------- 设置 ----------------
def settings_view(request: web.Request) -> dict:
    cfg = request.app[CFG]
    return {
        "bot": bot_state(request.app[RUN], cfg),
        "bot_token_masked": mask_token(cfg.bot_token),
        "check_interval": cfg.check_interval,
        "interval_floor": INTERVAL_FLOOR,
        "notify_cooldown": cfg.notify_cooldown,
        "fail_alert_threshold": cfg.fail_alert_threshold,
        "buy_button": cfg.buy_button,
        "readonly": {
            "concurrency": cfg.concurrency,
            "host_min_gap": cfg.host_min_gap,
            "request_timeout": cfg.request_timeout,
            "fetch_proxy": bool(cfg.fetch_proxy),
            "timezone": str(cfg.tz),
        },
    }


async def api_get_settings(request: web.Request):
    return web.json_response(settings_view(request))


async def api_put_settings(request: web.Request):
    cfg, db, runner = request.app[CFG], request.app[DBK], request.app[RUN]
    data = await body(request)
    token_changed = "bot_token" in data and str(data["bot_token"]).strip() != cfg.bot_token
    if "bot_token" in data and not token_changed:
        data.pop("bot_token")
    values = cfg.validate_update(data)  # 先做本地格式校验，再联网验证 Token
    if token_changed and values.get("bot_token"):
        username = await _test_token_or_400(values["bot_token"])
        log.info("Web 后台更换 Bot Token，新机器人 @%s", username)
    await db.save_settings(values)
    cfg.apply_overrides(values)
    if token_changed:
        await runner.restart() if cfg.bot_token else await runner.stop()
    return web.json_response(settings_view(request))


async def _test_token_or_400(token: str) -> str:
    try:
        return await test_token(token)
    except ValueError as exc:
        raise ApiError(str(exc))


async def api_test_token(request: web.Request):
    data = await body(request)
    token = str(data.get("token", "")).strip() or request.app[CFG].bot_token
    if not token:
        raise ApiError("请先填写 Token")
    return web.json_response({"username": await _test_token_or_400(token)})


async def api_clear_bot_token(request: web.Request):
    """清除 Bot Token：断开机器人并从数据库删除 Token（网页设置优先于 .env，清除后 .env 中的值也不再使用）。"""
    cfg, db, runner = request.app[CFG], request.app[DBK], request.app[RUN]
    await runner.stop()
    await db.save_settings({"bot_token": ""})
    cfg.apply_overrides({"bot_token": ""})
    runner.error = None
    runner.username = None
    log.warning("Bot Token 已在 Web 后台清除，机器人已断开")
    return web.json_response(settings_view(request))


async def api_reconnect(request: web.Request):
    await request.app[RUN].restart()
    return web.json_response({"bot": bot_state(request.app[RUN], request.app[CFG])})


# ---------------- 推送对象（Telegram 会话） ----------------
def chat_view(r) -> dict:
    """推送对象的网页显示数据：不返回渠道密钥，只返回摘要。"""
    d = dict(r)
    d["detail"] = describe(d.get("channel", "telegram"), load_meta(d.pop("config", "{}")))
    return d


async def api_chats(request: web.Request):
    rows = await request.app[DBK].fetchall(
        "SELECT c.*, (SELECT COUNT(*) FROM chat_sites cs WHERE cs.chat_id = c.chat_id) AS sites "
        "FROM chats c ORDER BY c.created_at"
    )
    return web.json_response({"chats": [chat_view(r) for r in rows], "channels": CHANNELS})


async def _add_channel(db: DB, channel: str, title: str, raw: str) -> int:
    """登记一个非 Telegram 推送渠道，返回分配的 chat_id。"""
    if channel not in CHANNELS or channel == "telegram":
        raise ApiError("不支持的推送渠道")
    try:
        config = parse_config(channel, raw)
    except ChannelError as exc:
        raise ApiError(str(exc))
    top = await db.fetchone("SELECT MAX(chat_id) AS m FROM chats WHERE chat_id >= ?", (CHANNEL_ID_BASE,))
    chat_id = top["m"] + 1 if top and top["m"] else CHANNEL_ID_BASE + 1
    await db.execute(
        "INSERT INTO chats (chat_id, title, chat_type, channel, config, created_at) VALUES (?, ?, ?, ?, ?, ?)",
        (chat_id, title or CHANNELS[channel], channel, channel, json.dumps(config, ensure_ascii=False), now()),
    )
    return chat_id


async def api_add_chat(request: web.Request):
    db, runner = request.app[DBK], request.app[RUN]
    data = await body(request)
    channel = str(data.get("channel", "telegram") or "telegram")
    if channel != "telegram":
        title = str(data.get("title", "")).strip()[:100]
        chat_id = await _add_channel(db, channel, title, str(data.get("target", "")))
        return web.json_response({"chat": chat_view(await db.get_chat(chat_id))})
    try:
        chat_id = int(str(data.get("chat_id", "")).strip())
    except ValueError:
        raise ApiError("Chat ID 必须是数字（群组以 -100 开头）")
    title = str(data.get("title", "")).strip()[:100] or None
    chat_type = None
    if runner.app is not None:
        try:
            chat = await runner.app.bot.get_chat(chat_id)
            title = title or chat.title or (f"@{chat.username}" if chat.username else chat.full_name)
            chat_type = chat.type
        except Exception as exc:
            raise ApiError(
                f"机器人无法访问这个会话：{exc}。私聊需要对方先给机器人发 /start；群组/频道需要先把机器人拉进去。"
            )
    if chat_id >= CHANNEL_ID_BASE:
        raise ApiError("Chat ID 不正确")
    await db.ensure_chat(chat_id, title or str(chat_id), chat_type)
    return web.json_response({"chat": chat_view(await db.get_chat(chat_id))})


async def api_patch_chat(request: web.Request):
    db = request.app[DBK]
    chat_id = int_param(request, "chat_id")
    if not await db.get_chat(chat_id):
        raise ApiError("推送对象不存在", 404)
    data = await body(request)
    for flag in ("paused", "notify_soldout", "notify_new", "notify_price"):
        if flag in data:
            await db.set_chat_flag(chat_id, flag, 1 if data[flag] else 0)
    if "title" in data:
        await db.execute("UPDATE chats SET title = ? WHERE chat_id = ?", (str(data["title"]).strip()[:100], chat_id))
    return web.json_response({"chat": chat_view(await db.get_chat(chat_id))})


async def api_delete_chat(request: web.Request):
    await request.app[DBK].delete_chat(int_param(request, "chat_id"))
    return web.json_response({"ok": True})


async def api_test_chat(request: web.Request):
    runner, db = request.app[RUN], request.app[DBK]
    chat_id = int_param(request, "chat_id")
    chat = await db.get_chat(chat_id)
    if chat is None:
        raise ApiError("推送对象不存在", 404)
    telegram = chat["channel"] == "telegram"
    if telegram and not runner.running:
        raise ApiError("机器人未连接，请先在“设置”里填写正确的 Bot Token")
    message_id = await request.app[NOTI].send(chat_id, "✅ 测试消息\n推送通道正常。", None)
    if not message_id:
        raise ApiError(
            "发送失败。私聊需要对方先给机器人发 /start；群组需要机器人在群里且有发言权限。" if telegram
            else f"发送失败，请检查 {CHANNELS.get(chat['channel'], '')} 的地址或密钥是否正确，详细原因见程序日志。"
        )
    return web.json_response({"ok": True})


async def api_broadcast(request: web.Request):
    db, runner = request.app[DBK], request.app[RUN]
    text = str((await body(request)).get("text", "")).strip()
    if not text:
        raise ApiError("内容不能为空")
    if len(text) > 3500:
        raise ApiError("内容过长")
    rows = await db.fetchall("SELECT chat_id, channel FROM chats WHERE paused = 0")
    if not runner.running and any(r["channel"] == "telegram" for r in rows):
        rows = [r for r in rows if r["channel"] != "telegram"]   # 机器人未连接时只发给其他渠道
        if not rows:
            raise ApiError("机器人未连接")
    ok = 0
    for r in rows:
        if await request.app[NOTI].send(r["chat_id"], f"📢 公告\n{h(text)}", None):
            ok += 1
    return web.json_response({"sent": ok, "total": len(rows)})


# ---------------- 监控站点 ----------------
async def api_sites(request: web.Request):
    db, cfg = request.app[DBK], request.app[CFG]
    rows = await db.fetchall(
        "SELECT s.*, "
        "(SELECT COUNT(*) FROM products p WHERE p.site_id = s.id AND p.present = 1) AS products, "
        "(SELECT COUNT(*) FROM products p WHERE p.site_id = s.id AND p.present = 1 AND p.status = 'in_stock') AS in_stock, "
        "(SELECT COUNT(DISTINCT chat_id) FROM subscriptions sub WHERE sub.site_id = s.id) AS targets, "
        "(SELECT MAX(e.ts) FROM events e JOIN products p ON p.id = e.product_id "
        " WHERE p.site_id = s.id AND e.kind IN ('restock', 'new')) AS last_restock "
        "FROM sites s ORDER BY s.id"
    )
    return web.json_response({
        "sites": [dict(r) for r in rows], "default_interval": cfg.check_interval, "interval_floor": INTERVAL_FLOOR,
    })


async def api_add_site(request: web.Request):
    mon = request.app[MON]
    data = await body(request)
    try:
        url = normalize_url(str(data.get("url", "")))
    except UnsafeURLError as exc:
        raise ApiError(str(exc))
    name = str(data.get("name", "") or "").strip()[:60] or None   # 留空：由页面自动生成名称
    try:
        site = await mon.add_site(url, name)
    except (FetchError, UnsafeURLError) as exc:
        raise ApiError(
            f"读取失败：{exc}。请确认这是商品分组/列表页（如 /store/xxx 或 cart.php?gid=1），而不是首页。"
        )
    return web.json_response({"site": row(site)})


async def api_site_detail(request: web.Request):
    db = request.app[DBK]
    site_id = int_param(request, "site_id")
    site = await db.get_site(site_id)
    if site is None:
        raise ApiError("站点不存在", 404)
    products = await db.site_products(site_id)
    # 按地区拆分出的机型排在所属机型下面
    order = {p["key"]: i for i, p in enumerate(products) if "#loc:" not in p["key"]}
    products = sorted(products, key=lambda p: (order.get(p["key"].split("#", 1)[0], len(order)), "#loc:" in p["key"], p["id"]))
    stats, hours = await restock_stats(db, site_id, request.app[CFG].tz)
    targets = []
    for c in await db.fetchall(
        "SELECT c.* FROM chat_sites cs JOIN chats c ON c.chat_id = cs.chat_id WHERE cs.site_id = ? ORDER BY cs.created_at",
        (site_id,),
    ):
        subs = await db.chat_subscriptions(c["chat_id"], site_id)
        targets.append({
            "chat": chat_view(c),
            "all": any(s["kind"] == "all" for s in subs),
            "products": [s["value"] for s in subs if s["kind"] == "product"],
            "keywords": [s["value"] for s in subs if s["kind"] == "keyword"],
        })
    events = await db.site_events(site_id, 30)
    site_meta = load_meta(site["meta"])
    product_list = []
    for p in products:
        pm = load_meta(p["meta"])
        card = build_card(dict(site), site_meta, dict(p), pm)
        product_list.append({**dict(p), "meta": pm, "card": {
            "title": card.title, "price": card.price, "location": card.location, "lines": card.lines,
            "specs": card.specs, "coupon": card.coupon, "buy_url": card.buy_url, "tags": card.tags,
            "detected": card.detected,
        }})
    return web.json_response({
        "site": {**dict(site), "meta": site_meta},
        "products": product_list,
        "targets": targets,
        "events": [dict(e) for e in events],
        "stats": stats,
        "hours": hours,
        "default_interval": request.app[CFG].check_interval,
        "interval_floor": INTERVAL_FLOOR,
        "locations": location_choices(),
        "merchant_default": merchant_from_url(site["url"]),
    })


STATS_DAYS = 30


async def restock_stats(db: DB, site_id: int, tz) -> tuple[dict, list[int]]:
    """近 30 天每个机型的补货规律：补货次数、常见补货时段、平均有货时长、上次补货。

    返回 ({机型 id: 统计}, 全网址按小时（0～23 点，按设置的时区）的补货次数)。"""
    rows = await db.fetchall(
        "SELECT e.product_id, e.kind, e.duration, e.ts FROM events e JOIN products p ON p.id = e.product_id "
        "WHERE p.site_id = ? AND e.ts >= ? AND e.kind IN ('restock', 'new', 'soldout')",
        (site_id, now() - STATS_DAYS * 86400),
    )
    per: dict[int, dict] = {}
    site_hours = [0] * 24
    for r in rows:
        st = per.setdefault(r["product_id"], {"restocks": 0, "durations": [], "hours": [0] * 24, "last": None})
        if r["kind"] == "soldout":
            if r["duration"]:
                st["durations"].append(r["duration"])
            continue
        hour = datetime.fromtimestamp(r["ts"], tz).hour
        st["restocks"] += 1
        st["hours"][hour] += 1
        site_hours[hour] += 1
        st["last"] = max(st["last"] or 0, r["ts"])
    out = {}
    for pid, st in per.items():
        top = sorted((h for h in range(24) if st["hours"][h]), key=lambda h: (-st["hours"][h], h))[:2]
        out[pid] = {
            "restocks": st["restocks"],
            "avg_duration": int(sum(st["durations"]) / len(st["durations"])) if st["durations"] else None,
            "top_hours": top,
            "last_restock": st["last"],
        }
    return out, site_hours


# ---------------- 推送显示设置 ----------------
SITE_META_KEYS = {"merchant": 40, "prefix": 60, "coupon": 80, "aff": 200, "tags": 120, "expand": 1}
PRODUCT_META_KEYS = {"name": 100, "location": 10, "lines": 60, "specs": 100, "coupon": 80, "buy_url": 500, "tags": 120}


def clean_meta(data: Any, limits: dict[str, int]) -> dict[str, str]:
    if not isinstance(data, dict):
        raise ApiError("请求格式错误")
    out: dict[str, str] = {}
    for key, value in data.items():
        if key not in limits:
            raise ApiError(f"未知字段：{key}")
        value = "" if value is None else str(value).strip()
        if len(value) > limits[key]:
            raise ApiError(f"“{key}” 太长（最多 {limits[key]} 个字符）")
        if "\n" in value or "\r" in value:
            raise ApiError("不能包含换行")
        out[key] = value
    if out.get("buy_url") and not re.match(r"^https?://[^\s<>\"']+$", out["buy_url"], re.I):
        raise ApiError("购买链接必须以 http:// 或 https:// 开头")
    if out.get("aff") and not re.match(r"^[?&]?[\w.~%-]+=[\w.~%-]*(&[\w.~%-]+=[\w.~%-]*)*$", out["aff"]):
        raise ApiError("推广参数格式应为 aff=123（多个用 & 连接）")
    loc = out.get("location")
    if loc and loc not in ("auto", "none") and loc not in LOCATIONS:
        raise ApiError("未知的地区")
    if out.get("expand") not in (None, "", "1"):
        raise ApiError("expand 只能是 1 或空")
    return out


async def api_put_site_meta(request: web.Request):
    db = request.app[DBK]
    site_id = int_param(request, "site_id")
    site = await db.get_site(site_id)
    if not site:
        raise ApiError("站点不存在", 404)
    meta = clean_meta(await body(request), SITE_META_KEYS)
    was_expanded = load_meta(site["meta"]).get("expand") == "1"
    await db.set_meta("sites", site_id, meta)
    checking = False
    if was_expanded and meta.get("expand") != "1":
        # 关闭“按地区分别监控”：删除拆分出的地区记录，否则它们会被当作“消失”而误报售罄
        await db.execute("DELETE FROM products WHERE site_id = ? AND key LIKE ?", (site_id, "%#loc:%"))
    elif not was_expanded and meta.get("expand") == "1":
        # 刚开启：立即在后台检查一次，读取各机型的地区（第一次只记录现状，不推送）
        _run_in_background(request.app[MON].check_now(site_id))
        checking = True
    return web.json_response({"ok": True, "checking": checking})


_BG_TASKS: set = set()


def _run_in_background(coro) -> None:
    """在后台执行，不阻塞请求；保留引用直到完成，避免任务被提前回收；出错时写入日志。"""
    async def runner():
        try:
            await coro
        except Exception:
            log.exception("后台任务出错")

    task = asyncio.create_task(runner())
    _BG_TASKS.add(task)
    task.add_done_callback(_BG_TASKS.discard)


async def _site_product(request: web.Request):
    db = request.app[DBK]
    site_id, product_id = int_param(request, "site_id"), int_param(request, "product_id")
    site, product = await db.get_site(site_id), await db.get_product(product_id)
    if site is None or product is None or product["site_id"] != site_id:
        raise ApiError("机型不存在", 404)
    return site, product


async def api_put_product_meta(request: web.Request):
    site, product = await _site_product(request)
    await request.app[DBK].set_meta("products", product["id"], clean_meta(await body(request), PRODUCT_META_KEYS))
    return web.json_response({"ok": True})


async def api_preview_product(request: web.Request):
    """按（可能尚未保存的）显示设置渲染推送消息，用于后台实时预览。"""
    site, product = await _site_product(request)
    data = await body(request)
    state = data.get("state", "restock")
    if state not in ("restock", "new", "soldout"):
        raise ApiError("state 无效")
    site_meta = clean_meta(data["site_meta"], SITE_META_KEYS) if "site_meta" in data else load_meta(site["meta"])
    product_meta = clean_meta(data["meta"], PRODUCT_META_KEYS) if "meta" in data else load_meta(product["meta"])
    card = build_card(dict(site), site_meta, dict(product), product_meta)
    duration = None
    if state == "soldout":   # 用最近一次真实的有货时长作为示例
        last = await request.app[DBK].fetchone(
            "SELECT duration FROM events WHERE product_id = ? AND kind = 'soldout' AND duration > 0 ORDER BY ts DESC LIMIT 1",
            (product["id"],),
        )
        duration = last["duration"] if last else None
    return web.json_response({"html": render_card(card, state, duration=duration), "detected": card.detected})


async def api_change_site_url(request: web.Request):
    db, mon = request.app[DBK], request.app[MON]
    site_id = int_param(request, "site_id")
    if not await db.get_site(site_id):
        raise ApiError("站点不存在", 404)
    data = await body(request)
    try:
        url = normalize_url(str(data.get("url", "")))
    except UnsafeURLError as exc:
        raise ApiError(str(exc))
    try:
        count = await mon.change_site_url(site_id, url)
    except (FetchError, UnsafeURLError) as exc:
        raise ApiError(
            f"新网址读取失败：{exc}。网址未修改，仍在监控原网址。请确认这是商品分组/列表页（如 /store/xxx）。"
        )
    return web.json_response({"site": row(await db.get_site(site_id)), "products": count})


async def api_patch_site(request: web.Request):
    db = request.app[DBK]
    site_id = int_param(request, "site_id")
    if not await db.get_site(site_id):
        raise ApiError("站点不存在", 404)
    data = await body(request)
    if "name" in data:
        name = str(data["name"]).strip()[:60]
        if not name:
            raise ApiError("名称不能为空")
        await db.execute("UPDATE sites SET name = ? WHERE id = ?", (name, site_id))
    if "interval_sec" in data:
        sec = data["interval_sec"]
        try:
            sec = int(sec) if sec not in (None, "", 0, "0") else None
        except (TypeError, ValueError):
            raise ApiError("检查间隔必须是整数秒")
        if sec is not None and sec < INTERVAL_FLOOR:
            raise ApiError(f"检查间隔不能小于 {INTERVAL_FLOOR} 秒")
        await db.execute("UPDATE sites SET interval_sec = ? WHERE id = ?", (sec, site_id))
    if "enabled" in data:
        await db.execute("UPDATE sites SET enabled = ?, fail_count = 0 WHERE id = ?", (1 if data["enabled"] else 0, site_id))
    if "probe_mode" in data:
        if data["probe_mode"] not in ("auto", "off"):
            raise ApiError("probe_mode 只能是 auto 或 off")
        await db.execute("UPDATE sites SET probe_mode = ? WHERE id = ?", (data["probe_mode"], site_id))
    return web.json_response({"site": row(await db.get_site(site_id))})


async def api_delete_site(request: web.Request):
    await request.app[DBK].delete_site(int_param(request, "site_id"))
    return web.json_response({"ok": True})


async def api_check_site(request: web.Request):
    db, mon = request.app[DBK], request.app[MON]
    site_id = int_param(request, "site_id")
    site = await db.get_site(site_id)
    if site is None:
        raise ApiError("站点不存在", 404)
    if site["last_check"] and now() - site["last_check"] < MANUAL_CHECK_GAP:
        raise ApiError(f"刚检查过，请 {MANUAL_CHECK_GAP} 秒后再试", 429)
    out = await mon.check_now(site_id)
    return web.json_response({
        "ok": out.ok, "products": out.products, "events": out.events, "error": out.error, "method": out.method,
    })


async def api_put_target(request: web.Request):
    db = request.app[DBK]
    site_id, chat_id = int_param(request, "site_id"), int_param(request, "chat_id")
    if not await db.get_site(site_id):
        raise ApiError("站点不存在", 404)
    if not await db.get_chat(chat_id):
        raise ApiError("推送对象不存在，请先在“推送对象”里添加", 404)
    data = await body(request)
    valid_keys = {p["key"] for p in await db.site_products(site_id)}
    products = [k for k in data.get("products", []) if isinstance(k, str)]
    unknown = [k for k in products if k not in valid_keys]
    if unknown:
        raise ApiError("包含不存在的机型，请刷新页面后重试")
    keywords = [str(k)[:50] for k in data.get("keywords", []) if str(k).strip()][:20]
    await db.set_chat_subscriptions(chat_id, site_id, bool(data.get("all")), products, keywords)
    return web.json_response({"ok": True})


async def api_delete_target(request: web.Request):
    await request.app[DBK].detach_site(int_param(request, "chat_id"), int_param(request, "site_id"))
    return web.json_response({"ok": True})


async def api_keyword_preview(request: web.Request):
    db = request.app[DBK]
    site_id = int_param(request, "site_id")
    kw = request.query.get("q", "")
    products = await db.site_products(site_id, present_only=True)
    return web.json_response({"matches": [p["name"] for p in products if keyword_match(kw, p["name"])]})


# ---------------- 备份与迁移 ----------------
async def api_export(request: web.Request):
    include = request.query.get("secrets") == "1"
    data = await export_config(request.app[DBK], request.app[CFG], include)
    name = f"vps-restock-backup-{datetime.now(request.app[CFG].tz):%Y%m%d-%H%M}.json"
    return web.json_response(data, headers={"Content-Disposition": f'attachment; filename="{name}"'},
                             dumps=lambda d: json.dumps(d, ensure_ascii=False, indent=2))


async def api_import(request: web.Request):
    cfg, db, runner = request.app[CFG], request.app[DBK], request.app[RUN]
    data = await body(request)
    old_token = cfg.bot_token
    result = await import_config(
        db, cfg, data,
        clean_site_meta=lambda m: clean_meta(m, SITE_META_KEYS),
        clean_product_meta=lambda m: clean_meta(m, PRODUCT_META_KEYS),
    )
    cfg.apply_overrides(result["settings"])
    if cfg.bot_token != old_token:
        await runner.restart()
    log.warning("已在 Web 后台导入配置：%s 个网址、%s 个推送对象", result["sites"], result["chats"])
    result["settings"] = sorted(k for k in result["settings"] if k != "bot_token") + (
        ["bot_token"] if "bot_token" in result["settings"] else [])
    return web.json_response(result)


# ---------------- 页面 ----------------
_STATIC_REF = re.compile(r'(["\'])(/static/[\w.-]+\.(?:js|css|json|png|svg))\1')


def render_index() -> str:
    """首页：给样式、脚本等静态文件的网址加上内容指纹（如 app.js?v=1a2b3c4d5e）。

    文件内容一变，网址就变，浏览器（尤其是手机）必须重新下载，
    不会出现一键更新后“新页面 + 旧样式/旧脚本”混用的情况。"""
    html = (STATIC_DIR / "index.html").read_text(encoding="utf-8")

    def stamp(m: re.Match) -> str:
        path = STATIC_DIR / m.group(2)[len("/static/"):]
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()[:10]
        except OSError:
            return m.group(0)
        return f"{m.group(1)}{m.group(2)}?v={digest}{m.group(1)}"

    return _STATIC_REF.sub(stamp, html)


async def index(request: web.Request):
    return web.Response(text=request.app[INDEX_HTML], content_type="text/html", headers={"Cache-Control": "no-cache"})


async def api_update_info(request: web.Request):
    upd = request.app[UPD]
    check = await upd.check(force=request.query.get("refresh") == "1")
    return web.json_response({
        "current": upd.current(),
        "check": check,
        "host_available": upd.host_available(),
        "status": upd.status(),
        "repo": upd.repo,
    })


async def api_update_status(request: web.Request):
    upd = request.app[UPD]
    return web.json_response({"current": upd.current(), "status": upd.status()})


async def api_update_apply(request: web.Request):
    upd = request.app[UPD]
    status = upd.request_update()  # ValueError → 400
    upd.invalidate()
    return web.json_response({"status": status})


def create_app(
    cfg: Config, db: DB, monitor: Monitor, runner: BotRunner, auth: WebAuth, updater: Updater | None = None,
    notifier: Notifier | None = None,
) -> web.Application:
    # 4 MB：足够导入几百个网址的备份文件
    app = web.Application(middlewares=[error_mw, security_mw, auth_mw], client_max_size=4 * 1024 * 1024)
    app[CFG], app[DBK], app[MON], app[RUN], app[AUTH] = cfg, db, monitor, runner, auth
    app[NOTI] = notifier or Notifier(db, runner.send, runner.edit)
    app[INDEX_HTML] = render_index()   # 静态文件只在一键更新时变化，而更新会重启程序，所以启动时生成一次即可
    app[UPD] = updater or Updater(cfg.update_repo, cfg.update_branch, cfg.run_dir, cfg.fetch_proxy)
    r = app.router
    r.add_get("/", index)
    r.add_static("/static/", STATIC_DIR, show_index=False)
    r.add_get("/api/health", api_health)
    r.add_post("/api/login", api_login)
    r.add_post("/api/logout", api_logout)
    r.add_post("/api/auth/rotate-token", api_rotate_token)
    r.add_delete("/api/settings/bot-token", api_clear_bot_token)
    r.add_get("/api/overview", api_overview)
    r.add_get("/api/settings", api_get_settings)
    r.add_put("/api/settings", api_put_settings)
    r.add_post("/api/settings/test-token", api_test_token)
    r.add_post("/api/bot/reconnect", api_reconnect)
    r.add_get("/api/chats", api_chats)
    r.add_post("/api/chats", api_add_chat)
    r.add_patch("/api/chats/{chat_id}", api_patch_chat)
    r.add_delete("/api/chats/{chat_id}", api_delete_chat)
    r.add_post("/api/chats/{chat_id}/test", api_test_chat)
    r.add_post("/api/broadcast", api_broadcast)
    r.add_get("/api/sites", api_sites)
    r.add_post("/api/sites", api_add_site)
    r.add_get("/api/sites/{site_id}", api_site_detail)
    r.add_patch("/api/sites/{site_id}", api_patch_site)
    r.add_delete("/api/sites/{site_id}", api_delete_site)
    r.add_post("/api/sites/{site_id}/url", api_change_site_url)
    r.add_post("/api/sites/{site_id}/check", api_check_site)
    r.add_get("/api/sites/{site_id}/keyword-preview", api_keyword_preview)
    r.add_put("/api/sites/{site_id}/meta", api_put_site_meta)
    r.add_put("/api/sites/{site_id}/products/{product_id}/meta", api_put_product_meta)
    r.add_post("/api/sites/{site_id}/products/{product_id}/preview", api_preview_product)
    r.add_get("/api/export", api_export)
    r.add_post("/api/import", api_import)
    r.add_get("/api/update", api_update_info)
    r.add_get("/api/update/status", api_update_status)
    r.add_post("/api/update/apply", api_update_apply)
    r.add_put("/api/sites/{site_id}/targets/{chat_id}", api_put_target)
    r.add_delete("/api/sites/{site_id}/targets/{chat_id}", api_delete_target)
    return app


async def start_web(app: web.Application, host: str, port: int) -> web.AppRunner:
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    await web.TCPSite(runner, host, port).start()
    log.info("Web 后台已启动：http://%s:%s", host, port)
    return runner
