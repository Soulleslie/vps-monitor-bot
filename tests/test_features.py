"""新增功能：售罄显示有货时长、立即购买按钮、价格变动提醒、其他推送渠道、配置导出导入、补货规律统计。"""
import json
import re
from html import unescape

import httpx
import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.auth import WebAuth
from bot.backup import export_config, import_config
from bot.channels import CHANNEL_ID_BASE, SENT_OK, ChannelError, Notifier, parse_config
from bot.config import Config
from bot.db import DB
from bot.messages import buy_buttons, render_card
from bot.meta import Card, price_change
from bot.monitor import Monitor, apply_snapshot
from bot.parser import IN_STOCK, OUT_OF_STOCK, Product, parse_store
from bot.runner import BotRunner, to_markup
from bot.web import PRODUCT_META_KEYS, SITE_META_KEYS, clean_meta, create_app

H = {"X-Requested-With": "vrb"}
TOKEN = "test-admin-token-123456"
PAGE = (__import__("pathlib").Path(__file__).parent / "fixtures" / "twentyone_zh.html").read_text(encoding="utf-8")


def plain(s: str) -> str:
    return unescape(re.sub(r"<[^>]+>", "", s))


def prod(key, status, price="$10.00 USD / month", name=None):
    return Product(key=key, name=name or key, status=status, qty=None, price=price, order_url=f"https://s/{key}")


@pytest.fixture
async def db():
    d = DB(":memory:")
    await d.open()
    yield d
    await d.close()


# ---------------------------------------------------------------- 消息格式
CARD = Card(title="SA-Premium VPS", price="$49.00 USD / month", location="🇯🇵 日本 · 东京", lines=["CN2GIA"],
            coupon=None, specs="2 Core / 4GB", buy_url="https://x.com/buy?configoption[75]=812", tags=["sa"])


def test_soldout_shows_duration_and_buttons():
    sold = plain(render_card(CARD, "soldout", duration=200))
    assert "⏱ 有货时长：约 3 分 20 秒" in sold and sold.endswith("#sa #售罄")
    assert "⏱" not in render_card(CARD, "restock", duration=200)      # 只在售罄时显示
    assert "⏱" not in render_card(CARD, "soldout", duration=None)
    assert buy_buttons(CARD, "restock") == [("🛒 立即购买", CARD.buy_url)]
    assert buy_buttons(CARD, "soldout") == []
    # Telegram 按钮：方括号编码；空列表 = 移除按钮
    assert to_markup(buy_buttons(CARD, "new")).inline_keyboard[0][0].url == "https://x.com/buy?configoption%5B75%5D=812"
    assert to_markup([]).inline_keyboard == () and to_markup(None) is None


def test_price_card():
    html = render_card(CARD, "price_down", old_price="$59.00 USD / month")
    assert html.startswith("💲 降价提醒") and "💰 <s>$59.00 USD / month</s> → $49.00 USD / month" in html
    assert html.endswith("#sa #降价")


@pytest.mark.parametrize("old,new,expected", [
    ("$59.00 USD / month", "$49.00 USD / month", "down"),
    ("$49.00 USD / month", "$59.00 USD / month", "up"),
    ("$1,200.00 USD / year", "$999.00 USD / year", "down"),
    ("$49.00 USD / month", "$49.00 USD / month", None),
    ("$49.00 USD / month", "¥350.00 CNY / month", None),          # 货币不同（如切换语言）：不算
    ("$49.00 USD / month", "$49.00 USD / year", None),            # 周期不同：不算
    ("$49.00 USD / month + $50.00 设定费", "$49.00 USD / month + $0.00 设定费", None),   # 主价格没变
    (None, "$1", None), ("免费", "促销", None),
])
def test_price_change(old, new, expected):
    assert price_change(old, new) == expected


# ---------------------------------------------------------------- 价格变动事件
async def test_price_events(db):
    sid = (await db.get_or_create_site("https://s/store/a", "s"))["id"]
    await apply_snapshot(db, sid, [prod("a", IN_STOCK, "$10.00 USD"), prod("b", OUT_OF_STOCK, "$10.00 USD")], 1000, 0, False)
    # 有货机型降价 → 推送；缺货机型降价 → 只记录
    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, "$8.00 USD"), prod("b", OUT_OF_STOCK, "$8.00 USD")], 1100, 0, True)
    assert [(e["kind"], e["direction"], e["old_price"]) for e in ev] == [("price", "down", "$10.00 USD")]
    # 10 分钟内再次变动 → 只记录
    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, "$9.00 USD"), prod("b", OUT_OF_STOCK, "$8.00 USD")], 1200, 0, True)
    assert ev == []
    # 补货同时变价 → 只推送补货
    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, "$9.00 USD"), prod("b", IN_STOCK, "$7.00 USD")], 2000, 0, True)
    assert [e["kind"] for e in ev] == ["restock"]
    kinds = [r["kind"] for r in await db.fetchall("SELECT kind FROM events ORDER BY id")]
    assert kinds.count("price") == 4
    # 价格没有出现在页面上（解析不到）时不算变动
    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, None), prod("b", IN_STOCK, None)], 3000, 0, True)
    assert ev == []


async def test_first_expansion_without_baseline_is_silent(db):
    sid = (await db.get_or_create_site("https://s/store/a", "s"))["id"]
    # 导入后的占位：之前已有地区记录，但这是第一次检查（没有基线）→ 新出现的地区不能当作补货
    await apply_snapshot(db, sid, [prod("a", IN_STOCK), prod("a#loc:1", IN_STOCK)], 1000, 0, False)
    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK), prod("a#loc:1", IN_STOCK), prod("a#loc:2", IN_STOCK)],
                              1100, 0, False)
    assert ev == []


# ---------------------------------------------------------------- 推送分发
class Recorder:
    def __init__(self):
        self.sent, self.edits = [], []

    async def send(self, chat_id, text, markup):
        self.sent.append((chat_id, text, markup))
        return 100 + len(self.sent)

    async def edit(self, chat_id, message_id, text, markup):
        self.edits.append((chat_id, message_id, text, markup))
        return True


async def _monitor_with_site(db, rec, **cfg):
    mon = Monitor(Config(bot_token="x", notify_cooldown=0, **cfg), db, None, rec.send, rec.edit)
    site = await db.get_or_create_site("https://s/store/a", "s")
    await db.execute("UPDATE sites SET last_ok = 1 WHERE id = ?", (site["id"],))
    for chat in (1, 2):
        await db.ensure_chat(chat, None)
        await db.set_chat_subscriptions(chat, site["id"], True, [], [])
    await db.set_chat_flag(2, "notify_price", 0)
    return mon, await db.get_site(site["id"])


async def test_dispatch_price_buttons_and_soldout_duration(db):
    rec = Recorder()
    mon, site = await _monitor_with_site(db, rec)
    sid = site["id"]
    await apply_snapshot(db, sid, [prod("a", OUT_OF_STOCK, "$10.00 USD")], 1000, 0, False)

    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, "$10.00 USD")], 1100, 0, True)
    await mon.dispatch(site, ev)
    assert len(rec.sent) == 2 and rec.sent[0][2] == [("🛒 立即购买", "https://s/a")]

    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, "$8.00 USD")], 1200, 0, True)
    await mon.dispatch(site, ev)
    assert len(rec.sent) == 3 and rec.sent[2][0] == 1                 # 2 号关闭了价格变动提醒
    assert plain(rec.sent[2][1]).startswith("💲 降价提醒") and "$10.00 USD → $8.00 USD" in plain(rec.sent[2][1])

    ev = await apply_snapshot(db, sid, [prod("a", OUT_OF_STOCK, "$8.00 USD")], 1500, 0, True)
    await mon.dispatch(site, ev)
    assert len(rec.edits) == 2                                        # 两条补货消息都改为售罄
    text, markup = rec.edits[0][2], rec.edits[0][3]
    assert plain(text).startswith("🔴 售罄") and "⏱ 有货时长：约 6 分 40 秒" in plain(text) and markup == []


async def test_buy_button_can_be_disabled(db):
    rec = Recorder()
    mon, site = await _monitor_with_site(db, rec, buy_button=0)
    await apply_snapshot(db, site["id"], [prod("a", OUT_OF_STOCK)], 1000, 0, False)
    await mon.dispatch(site, await apply_snapshot(db, site["id"], [prod("a", IN_STOCK)], 1100, 0, True))
    assert rec.sent and all(m is None for _, _, m in rec.sent)


# ---------------------------------------------------------------- 其他推送渠道
def test_parse_channel_config():
    assert parse_config("bark", "https://api.day.app/AbC123/") == {"server": "https://api.day.app", "key": "AbC123"}
    assert parse_config("bark", "https://bark.me.com/sub/KEY") == {"server": "https://bark.me.com/sub", "key": "KEY"}
    assert parse_config("pushplus", "a" * 32) == {"token": "a" * 32}
    for kind, raw in [("bark", "https://api.day.app/"), ("bark", "api.day.app/x"), ("wecom", "https://qyapi.weixin.qq.com/x"),
                      ("pushplus", "bad token"), ("webhook", "ftp://x"), ("telegram", "1")]:
        with pytest.raises(ChannelError):
            parse_config(kind, raw)


async def test_notifier_channels(db):
    calls = []

    def handler(request: httpx.Request):
        body = json.loads(request.content)
        calls.append((str(request.url), body))
        if "day.app" in str(request.url):
            return httpx.Response(200, json={"code": 200})
        if "qyapi" in str(request.url):
            return httpx.Response(200, json={"errcode": 0 if "good" in str(request.url) else 93000})
        if "pushplus" in str(request.url):
            return httpx.Response(200, json={"code": 200})
        return httpx.Response(204)

    tg = Recorder()
    n = Notifier(db, tg.send, tg.edit, httpx.AsyncClient(transport=httpx.MockTransport(handler)))
    rows = [("bark", {"server": "https://api.day.app", "key": "K"}), ("wecom", {"url": "https://qyapi.weixin.qq.com/send?key=good"}),
            ("pushplus", {"token": "T" * 20}), ("webhook", {"url": "https://hook.example.com/x"}),
            ("wecom", {"url": "https://qyapi.weixin.qq.com/send?key=bad"})]
    for i, (kind, conf) in enumerate(rows):
        await db.execute("INSERT INTO chats (chat_id, title, channel, config, created_at) VALUES (?, ?, ?, ?, 0)",
                         (CHANNEL_ID_BASE + i + 1, kind, kind, json.dumps(conf)))
    await db.ensure_chat(42, "tg")
    html = render_card(CARD, "restock")
    buttons = buy_buttons(CARD, "restock")
    results = [await n.send(CHANNEL_ID_BASE + i + 1, html, buttons) for i in range(len(rows))]
    assert results == [SENT_OK, SENT_OK, SENT_OK, SENT_OK, None]          # 最后一个返回了错误码
    bark = calls[0][1]
    assert calls[0][0] == "https://api.day.app/push" and bark["device_key"] == "K"
    assert bark["title"] == "🟢 补货提醒" and "🏷 SA-Premium VPS" in bark["body"] and bark["url"] == CARD.buy_url
    assert "<" not in calls[1][1]["text"]["content"]                        # 企业微信收到的是纯文本
    assert calls[2][1]["token"] == "T" * 20 and calls[3][1]["url"] == CARD.buy_url
    # Telegram 会话仍走机器人
    assert await n.send(42, "hi", None) == 101 and tg.sent[0][0] == 42
    # 非 Telegram 渠道不能编辑：售罄时另发一条，并告知不必再编辑
    assert await n.edit(CHANNEL_ID_BASE + 1, SENT_OK, render_card(CARD, "soldout"), []) is False
    assert calls[-1][1]["title"] == "🔴 售罄"
    await n.close()


# ---------------------------------------------------------------- 导出 / 导入
async def _populated(db):
    site = await db.get_or_create_site("https://s.example.com/store/a", "我的 A")
    sid = site["id"]
    await db.set_meta("sites", sid, {"merchant": "SA", "expand": "1"})
    await db.execute("UPDATE sites SET interval_sec = 30, last_ok = 1 WHERE id = ?", (sid,))
    await apply_snapshot(db, sid, [prod("pid:1", IN_STOCK), prod("pid:2", IN_STOCK)], 1000, 0, False)
    p1 = (await db.site_products(sid))[0]
    await db.set_meta("products", p1["id"], {"name": "东京 Pro"})
    await db.ensure_chat(42, "我", "private")
    await db.set_chat_flag(42, "notify_price", 0)
    await db.set_chat_subscriptions(42, sid, False, ["pid:1"], ["东京"])
    await db.execute("INSERT INTO chats (chat_id, title, channel, config, created_at) VALUES (?, 'Bark', 'bark', ?, 0)",
                     (CHANNEL_ID_BASE + 1, json.dumps({"server": "https://api.day.app", "key": "SECRETKEY"})))
    await db.set_chat_subscriptions(CHANNEL_ID_BASE + 1, sid, True, [], [])
    return sid


CLEANERS = dict(clean_site_meta=lambda m: clean_meta(m, SITE_META_KEYS),
                clean_product_meta=lambda m: clean_meta(m, PRODUCT_META_KEYS))


async def test_export_import_roundtrip(db):
    await _populated(db)
    cfg = Config(bot_token="123:SECRET", check_interval=45)
    plain_export = await export_config(db, cfg, include_secrets=False)
    dumped = json.dumps(plain_export, ensure_ascii=False)
    assert "SECRET" not in dumped and "SECRETKEY" not in dumped     # 默认不含密钥
    full = await export_config(db, cfg, include_secrets=True)
    assert full["settings"]["bot_token"] == "123:SECRET"

    new = DB(":memory:")
    await new.open()
    try:
        cfg2 = Config()
        # 不含密钥：Bark 渠道跳过，其余正常导入
        r = await import_config(new, cfg2, json.loads(dumped), **CLEANERS)
        assert (r["sites"], r["added_sites"], r["chats"], r["skipped_channels"]) == (1, 1, 1, 1)
        assert r["settings"]["check_interval"] == "45" and "bot_token" not in r["settings"]
        site = (await new.fetchall("SELECT * FROM sites"))[0]
        assert site["name"] == "我的 A" and site["interval_sec"] == 30 and site["last_ok"] is None
        assert json.loads(site["meta"]) == {"merchant": "SA", "expand": "1"}
        prods = {p["key"]: json.loads(p["meta"]) for p in await new.site_products(site["id"])}
        assert prods == {"pid:1": {"name": "东京 Pro"}}
        subs = {(s["kind"], s["value"]) for s in await new.chat_subscriptions(42, site["id"])}
        assert subs == {("product", "pid:1"), ("keyword", "东京")}
        assert (await new.get_chat(42))["notify_price"] == 0
        # 含密钥：Bark 渠道也导入；重复导入不会产生重复记录
        for _ in range(2):
            r = await import_config(new, cfg2, json.loads(json.dumps(full)), **CLEANERS)
        assert r["skipped_channels"] == 0 and r["added_chats"] == 0
        chats = await new.fetchall("SELECT * FROM chats ORDER BY chat_id")
        assert [c["channel"] for c in chats] == ["telegram", "bark"]
        assert len(await new.chat_subscriptions(chats[1]["chat_id"], site["id"])) == 1
        assert r["settings"]["bot_token"] == "123:SECRET"
    finally:
        await new.close()


async def test_import_rejects_bad_files_without_changes(db):
    cfg = Config()
    good = await export_config(db, cfg, False)
    for bad in [{"format": "x"}, {**good, "version": 99},
                {**good, "sites": [{"url": "ftp://a.com/x"}]},
                {**good, "sites": [{"url": "https://u:p@a.com/x"}]},
                {**good, "sites": [{"url": "https://a.com/x", "meta": {"evil": "1"}}]},
                {**good, "settings": {"check_interval": 1}}]:
        with pytest.raises(Exception):
            await import_config(db, cfg, bad, **CLEANERS)
    assert await db.fetchall("SELECT * FROM sites") == []


# ---------------------------------------------------------------- 网页接口
class FakeRunner(BotRunner):
    async def send(self, chat_id, text, markup):
        return 1


@pytest.fixture
async def client():
    cfg = Config(bot_token="", web_admin_token=TOKEN)
    d = DB(":memory:")
    await d.open()
    runner = FakeRunner(cfg, d)
    mon = Monitor(cfg, d, None, runner.send, runner.edit)

    async def fake_fetch(url, probe=False, probe_keys=None, use_proxy=False):
        return parse_store(PAGE, url)

    mon.fetch_products = fake_fetch
    auth = WebAuth(cfg, d)
    await auth.init()
    c = TestClient(TestServer(create_app(cfg, d, mon, runner, auth)))
    await c.start_server()
    c.db = d
    assert (await c.post("/api/login", json={"token": TOKEN}, headers=H)).status == 200
    yield c
    await c.close()
    await d.close()


async def test_web_channels_never_return_secrets(client):
    r = await client.post("/api/chats", json={"channel": "bark", "target": "https://api.day.app/TOPSECRET", "title": "手机"},
                          headers=H)
    assert r.status == 200
    chat = (await r.json())["chat"]
    assert chat["chat_id"] > CHANNEL_ID_BASE and chat["detail"] == "api.day.app"
    listing = await (await client.get("/api/chats")).text()
    assert "TOPSECRET" not in listing and "config" not in json.loads(listing)["chats"][0]
    r = await client.post("/api/chats", json={"channel": "bark", "target": "not a url"}, headers=H)
    assert r.status == 400
    # 手动添加 Telegram 时不能使用渠道专用的 ID 范围
    r = await client.post("/api/chats", json={"chat_id": str(CHANNEL_ID_BASE + 5)}, headers=H)
    assert r.status == 400
    r = await client.patch(f"/api/chats/{chat['chat_id']}", json={"notify_price": False}, headers=H)
    assert (await r.json())["chat"]["notify_price"] == 0


async def test_web_export_import_and_stats(client):
    r = await client.post("/api/sites", json={"url": "https://portal.example.net/store/cloud-server"}, headers=H)
    sid = (await r.json())["site"]["id"]
    exported = await (await client.get("/api/export")).json()
    assert exported["format"] == "vps-restock-backup" and len(exported["sites"]) == 1
    exported["sites"].append({"url": "https://other.example.com/store/b", "name": "B"})
    r = await client.post("/api/import", json=exported, headers=H)
    body = await r.json()
    assert r.status == 200 and body["added_sites"] == 1

    # 补货规律：两次补货、一次售罄
    pid = (await client.db.site_products(sid))[0]["id"]
    for kind, dur, ts in [("restock", None, 1_800_000_000), ("soldout", 300, 1_800_000_300), ("new", None, 1_800_003_600)]:
        await client.db.add_event(pid, kind, None, dur, ts)
    await client.db.conn.commit()
    import bot.web as web_mod
    web_mod.now = lambda: 1_800_010_000
    try:
        detail = await (await client.get(f"/api/sites/{sid}")).json()
    finally:
        from bot.db import now as real_now
        web_mod.now = real_now
    st = detail["stats"][str(pid)]
    assert st["restocks"] == 2 and st["avg_duration"] == 300 and len(st["top_hours"]) == 2
    assert sum(detail["hours"]) == 2 and len(detail["hours"]) == 24


async def test_static_files_revalidated_after_update(client):
    """一键更新后浏览器不能继续使用旧的样式/脚本：静态文件每次都要向服务器确认。"""
    for path in ("/static/style.css", "/static/app.js", "/static/theme.js", "/static/manifest.json"):
        r = await client.get(path)
        assert r.status == 200 and r.headers["Cache-Control"] == "no-cache", path
        last = r.headers["Last-Modified"]
        r2 = await client.get(path, headers={"If-Modified-Since": last})
        assert r2.status == 304, path          # 没有变化时只返回 304，不重新下载
    r = await client.get("/")
    index = await r.text()
    assert r.headers["Cache-Control"] == "no-cache" and r.headers["Content-Type"].startswith("text/html")
    assert index.index("theme.js") < index.index("style.css")   # 先应用外观，再加载样式，避免闪烁
    # 网址带内容指纹：文件一变网址就变，手机浏览器不能继续用缓存里的旧文件
    import hashlib
    from bot.web import STATIC_DIR
    for name in ("app.js", "style.css", "theme.js", "manifest.json", "apple-touch-icon.png"):
        digest = hashlib.sha256((STATIC_DIR / name).read_bytes()).hexdigest()[:10]
        assert f'"/static/{name}?v={digest}"' in index, name
        assert (await client.get(f"/static/{name}?v={digest}")).status == 200


# ---------------------------------------------------------------- 抓取代理
async def test_fetcher_routes_by_proxy_flag():
    from bot.fetcher import FetchError as FE, FetchResult, Fetcher

    f = Fetcher("ua", proxy=None, host_min_gap=0, verify_hosts=False)
    used = []

    async def fake_once(client, url):
        used.append(client)
        return FetchResult(url, 200, "", 0.0)

    f._get_once = fake_once
    with pytest.raises(FE, match="还没有填写抓取代理"):
        await f.get("https://a.example/x", use_proxy=True)
    await f.get("https://a.example/x")
    assert used == [f._client]
    await f.set_proxy("http://127.0.0.1:3128")                 # 网页上修改后立即生效
    await f.get("https://a.example/x", use_proxy=True)
    await f.get("https://a.example/x")
    assert used[1] is f._proxy_client and used[2] is f._client and f.proxy == "http://127.0.0.1:3128"
    await f.set_proxy(None)
    assert f._proxy_client is None
    await f.close()


class ProxyRecorder:
    """记录每次请求是否走代理。"""

    def __init__(self, page):
        self.page, self.calls = page, []

    async def get(self, url, retries=2, fresh_session=False, use_proxy=False):
        from bot.fetcher import FetchResult
        self.calls.append((url, use_proxy))
        return FetchResult(url, 200, self.page, 0.1)

    async def close(self):
        pass


async def test_monitor_uses_proxy_per_site(db):
    rec = Recorder()
    fetcher = ProxyRecorder(PAGE)
    cfg = Config(bot_token="x", fetch_proxy="http://127.0.0.1:3128")
    mon = Monitor(cfg, db, fetcher, rec.send, rec.edit)
    direct = await mon.add_site("https://a.example/store/x")
    proxied = await mon.add_site("https://b.example/store/y", use_proxy=True)   # 添加时勾选“通过代理读取”
    assert json.loads(proxied["meta"]) == {"proxy": "1"} and json.loads(direct["meta"]) == {}
    assert fetcher.calls == [("https://a.example/store/x", False), ("https://b.example/store/y", True)]
    for site in (direct, proxied):
        await db.ensure_chat(1, None)
        await db.set_chat_subscriptions(1, site["id"], True, [], [])
    fetcher.calls.clear()
    await mon.check_site(direct["id"])
    await mon.check_site(proxied["id"])
    by_host = {}
    for url, via in fetcher.calls:
        by_host.setdefault(url.split("/")[2], set()).add(via)
    assert by_host["a.example"] == {False}                       # 包括购物车探测等后续请求
    assert by_host["b.example"] == {True}
    # 设置为“所有网址都用代理”
    cfg.proxy_scope = "all"
    fetcher.calls.clear()
    await mon.check_site(direct["id"])
    assert {via for _, via in fetcher.calls} == {True}


async def test_web_proxy_settings(client):
    r = await client.put("/api/settings", json={"fetch_proxy": "socks5://me:secret@10.0.0.2:1080"}, headers=H)
    body = await r.json()
    assert r.status == 200 and body["fetch_proxy"] == "socks5://me:***@10.0.0.2:1080"
    assert "secret" not in await (await client.get("/api/settings")).text()       # 网页上不显示密码
    # 原样提交隐藏密码的地址 = 不修改
    r = await client.put("/api/settings", json={"fetch_proxy": body["fetch_proxy"], "proxy_scope": "all"}, headers=H)
    assert (await r.json())["proxy_scope"] == "all"
    assert (await client.db.get_settings())["fetch_proxy"] == "socks5://me:secret@10.0.0.2:1080"
    for bad in ["10.0.0.2:1080", "socks5h://a:1", "http://a"]:
        r = await client.put("/api/settings", json={"fetch_proxy": bad}, headers=H)
        assert r.status == 400, bad
    r = await client.put("/api/settings", json={"proxy_scope": "x"}, headers=H)
    assert r.status == 400
    # 清除
    r = await client.put("/api/settings", json={"fetch_proxy": ""}, headers=H)
    assert (await r.json())["fetch_proxy"] == ""


async def test_web_test_proxy_and_site_flag(client, monkeypatch):
    import bot.web as web_mod

    calls = []

    async def fake_try(cfg, url, proxy):
        calls.append((url, proxy))
        if proxy:
            return {"ok": True, "status": 200, "elapsed": 0.5, "products": 3, "names": ["A"], "error": None}
        return {"ok": False, "status": 403, "error": "被 Cloudflare 人机验证拦截"}

    monkeypatch.setattr(web_mod, "_try_fetch", fake_try)
    r = await client.post("/api/settings/test-proxy", json={"url": "https://x.example/c"}, headers=H)
    assert r.status == 400                                              # 还没有代理
    await client.put("/api/settings", json={"fetch_proxy": "http://u:pw@10.0.0.2:3128"}, headers=H)
    r = await client.post("/api/settings/test-proxy",
                          json={"url": "https://x.example/c", "proxy": "http://u:***@10.0.0.2:3128"}, headers=H)
    data = await r.json()
    assert data["direct"]["ok"] is False and data["proxy"]["products"] == 3
    assert calls[-1] == ("https://x.example/c", "http://u:pw@10.0.0.2:3128")   # 用的是保存的真实地址
    # 网址单独开启“通过代理读取”
    sid = (await (await client.post("/api/sites", json={"url": "https://portal.example.net/store/a"}, headers=H)).json())["site"]["id"]
    r = await client.put(f"/api/sites/{sid}/meta", json={"proxy": "1"}, headers=H)
    assert r.status == 200
    sites = (await (await client.get("/api/sites")).json())
    assert sites["sites"][0]["via_proxy"] is True and sites["proxy"] == {"configured": True, "scope": "selected"}
    assert (await client.put(f"/api/sites/{sid}/meta", json={"proxy": "yes"}, headers=H)).status == 400


async def test_backup_proxy_is_a_secret(db):
    cfg = Config(fetch_proxy="http://u:pw@10.0.0.2:3128", proxy_scope="selected")
    assert "fetch_proxy" not in (await export_config(db, cfg, False))["settings"]
    full = await export_config(db, cfg, True)
    assert full["settings"]["fetch_proxy"] == "http://u:pw@10.0.0.2:3128" and full["settings"]["proxy_scope"] == "selected"
    cfg2 = Config(fetch_proxy="http://keep:1")
    plain = await export_config(db, cfg, False)
    r = await import_config(db, cfg2, plain, **CLEANERS)
    assert "fetch_proxy" not in r["settings"]                   # 备份不含代理时保留现有代理
    r = await import_config(db, cfg2, full, **CLEANERS)
    assert r["settings"]["fetch_proxy"] == "http://u:pw@10.0.0.2:3128"
