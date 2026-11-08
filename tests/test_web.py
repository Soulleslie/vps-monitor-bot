import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.auth import WebAuth
from bot.config import Config
from bot.db import DB
from bot.monitor import Monitor
from bot.parser import parse_store
from bot.runner import BotRunner
from bot.web import create_app

H = {"X-Requested-With": "vrb"}
TOKEN = "test-admin-token-123456"
PAGE = (__import__("pathlib").Path(__file__).parent / "fixtures" / "twentyone_zh.html").read_text(encoding="utf-8")


class FakeRunner(BotRunner):
    sent: list

    async def send(self, chat_id, text, markup):
        self.sent.append((chat_id, text))
        return 1


@pytest.fixture
async def client():
    cfg = Config(bot_token="", web_admin_token=TOKEN)
    db = DB(":memory:")
    await db.open()
    runner = FakeRunner(cfg, db)
    runner.sent = []
    mon = Monitor(cfg, db, None, runner.send, runner.edit)

    async def fake_fetch(url, probe=False, probe_keys=None):
        return parse_store(PAGE, url)

    mon.fetch_products = fake_fetch
    auth = WebAuth(cfg, db)
    await auth.init()
    c = TestClient(TestServer(create_app(cfg, db, mon, runner, auth)))
    await c.start_server()
    c.db, c.runner, c.cfg = db, runner, cfg
    yield c
    await c.close()
    await db.close()


async def login(c):
    r = await c.post("/api/login", json={"token": TOKEN}, headers=H)
    assert r.status == 200


async def test_requires_login_and_csrf(client):
    assert (await client.get("/api/overview")).status == 401
    assert (await client.post("/api/login", json={"token": TOKEN})).status == 403  # 缺少安全请求头
    r = await client.post("/api/login", json={"token": "wrong"}, headers=H)
    assert r.status == 401
    await login(client)
    assert (await client.get("/api/overview")).status == 200
    assert (await client.patch("/api/sites/1", json={})).status == 403  # 已登录也要安全请求头
    r = await client.get("/")
    assert r.status == 200 and "script-src 'self'" in r.headers["Content-Security-Policy"]


async def test_bruteforce_lockout(client):
    for _ in range(10):
        await client.post("/api/login", json={"token": "x"}, headers=H)
    r = await client.post("/api/login", json={"token": TOKEN}, headers=H)
    assert r.status == 429


async def test_full_flow(client):
    await login(client)
    r = await client.post("/api/sites", json={"url": "https://portal.example.net/store/cloud-server"}, headers=H)
    site = (await r.json())["site"]
    detail = await (await client.get(f"/api/sites/{site['id']}")).json()
    assert {p["name"] for p in detail["products"]} == {"HK-Basic 1C1G", "HK-Pro 2C2G", "JP-Lite"}

    # 推送对象未登记时不能订阅
    r = await client.put(f"/api/sites/{site['id']}/targets/555", json={"all": True}, headers=H)
    assert r.status == 404
    r = await client.post("/api/chats", json={"chat_id": "555", "title": "我"}, headers=H)
    assert r.status == 200

    r = await client.put(f"/api/sites/{site['id']}/targets/555", json={"products": ["pid:999"]}, headers=H)
    assert r.status == 400  # 不存在的机型
    r = await client.put(
        f"/api/sites/{site['id']}/targets/555", json={"products": ["pid:11"], "keywords": ["JP"]}, headers=H
    )
    assert r.status == 200
    detail = await (await client.get(f"/api/sites/{site['id']}")).json()
    assert detail["targets"][0]["products"] == ["pid:11"] and detail["targets"][0]["keywords"] == ["jp"]

    r = await client.patch(f"/api/sites/{site['id']}", json={"interval_sec": 3}, headers=H)
    assert r.status == 400  # 低于 5 秒技术下限
    r = await client.patch(f"/api/sites/{site['id']}", json={"interval_sec": "abc"}, headers=H)
    assert r.status == 400
    r = await client.patch(f"/api/sites/{site['id']}", json={"interval_sec": 5, "name": "SA 云"}, headers=H)
    assert (await r.json())["site"]["interval_sec"] == 5
    r = await client.patch(f"/api/sites/{site['id']}", json={"interval_sec": None}, headers=H)
    assert (await r.json())["site"]["interval_sec"] is None  # 留空恢复全局默认

    sites = await (await client.get("/api/sites")).json()
    assert sites["sites"][0]["targets"] == 1

    r = await client.delete(f"/api/sites/{site['id']}/targets/555", headers=H)
    assert r.status == 200
    assert (await client.get(f"/api/sites/{site['id']}")).status == 200  # 移除推送对象不会删除站点
    r = await client.delete(f"/api/sites/{site['id']}", headers=H)
    assert (await client.get(f"/api/sites/{site['id']}")).status == 404


async def test_settings(client):
    await login(client)
    s = await (await client.get("/api/settings")).json()
    assert s["bot"]["has_token"] is False and s["bot_token_masked"] == ""
    r = await client.put("/api/settings", json={"check_interval": 4}, headers=H)
    assert r.status == 400  # 低于 5 秒技术下限
    r = await client.put("/api/settings", json={"check_interval": 8, "notify_cooldown": 60}, headers=H)
    assert r.status == 200 and client.cfg.check_interval == 8  # 自定义短间隔允许
    assert (await client.db.get_settings())["check_interval"] == "8"
    r = await client.put("/api/settings", json={"check_interval": 7200}, headers=H)
    assert r.status == 200 and client.cfg.check_interval == 7200  # 长间隔也允许
    r = await client.put("/api/settings", json={"min_interval": 30}, headers=H)
    assert r.status == 400  # 已移除的设置项
    r = await client.put("/api/settings", json={"bot_token": "no-colon"}, headers=H)
    assert r.status == 400
    r = await client.put("/api/settings", json={"web_admin_token_hash": "x"}, headers=H)
    assert r.status == 400  # 不允许改内部字段


async def test_changing_env_token_revokes_sessions(client):
    await login(client)
    assert (await client.get("/api/overview")).status == 200
    # 模拟：.env 中更换令牌后重启
    client.cfg.web_admin_token = "a-brand-new-token-987654"
    await WebAuth(client.cfg, client.db).init()
    assert (await client.get("/api/overview")).status == 401
    # 未更换令牌时重启，不影响已登录会话
    await login(client)  # 测试夹具中的 auth 实例仍持有旧令牌，用于建立新会话
    await WebAuth(client.cfg, client.db).init()
    assert (await client.get("/api/overview")).status == 200


async def test_rotate_web_token(client):
    from bot.web import AUTH

    auth = client.server.app[AUTH]
    await login(client)
    other_browser = await auth.new_session("10.0.0.9")  # 另一台已登录的设备

    r = await client.post("/api/auth/rotate-token", json={"current_token": "wrong"}, headers=H)
    assert r.status == 403
    r = await client.post("/api/auth/rotate-token", json={"current_token": TOKEN}, headers=H)
    assert r.status == 200
    new_token = (await r.json())["token"]
    assert new_token and new_token != TOKEN

    assert (await client.get("/api/overview")).status == 200          # 当前浏览器保持登录
    assert not await auth.session_ok(other_browser)                    # 其他设备被注销
    assert not auth.check_admin_token(TOKEN) and auth.check_admin_token(new_token)

    # 重启（.env 仍是旧令牌、未被修改）：网页上换的新令牌继续有效，旧令牌不会“复活”
    restarted = WebAuth(client.cfg, client.db)
    await restarted.init()
    assert restarted.check_admin_token(new_token) and not restarted.check_admin_token(TOKEN)


async def test_clear_bot_token(client):
    await login(client)
    client.cfg.bot_token = "123:abcdefghijklmnopqrstuvwxyz"
    r = await client.delete("/api/settings/bot-token", headers=H)
    data = await r.json()
    assert r.status == 200 and data["bot"]["has_token"] is False and data["bot_token_masked"] == ""
    assert client.cfg.bot_token == "" and (await client.db.get_settings())["bot_token"] == ""


async def test_logout(client):
    await login(client)
    await client.post("/api/logout", headers=H)
    assert (await client.get("/api/overview")).status == 401
