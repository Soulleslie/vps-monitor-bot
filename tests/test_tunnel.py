"""内置节点客户端（anytls:// 节点链接）：链接解析、配置生成、进程启动/重启/停止、网页设置。

真正的 mihomo 只在 Docker 镜像里；这里用 tests/fake_mihomo.py 模拟它的启动和监听行为。"""
import asyncio
import json
import os
import socket
import stat
import sys
from pathlib import Path

import pytest

from bot.config import Config, check_proxy, mask_proxy
from bot.tunnel import ProxyResolver, Tunnel, TunnelError, build_config, mask_link, parse_link

FAKE = Path(__file__).parent / "fake_mihomo.py"
LINK = "anytls://p%40ss%3Aw0rd@home.example.com:8443/?sni=home.example.com&insecure=1&fp=chrome#%E5%AE%B6%E5%AE%BD"


@pytest.fixture
def fake_bin(tmp_path):
    path = tmp_path / "mihomo"
    path.write_text(f"#!/bin/sh\nexec {sys.executable} {FAKE} \"$@\"\n")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return str(path)


def listening(port: int) -> bool:
    with socket.socket() as s:
        s.settimeout(0.5)
        return s.connect_ex(("127.0.0.1", port)) == 0


# ---------------------------------------------------------------- 链接与配置
def test_parse_link():
    node = parse_link(LINK)
    assert node == {"server": "home.example.com", "port": 8443, "password": "p@ss:w0rd", "sni": "home.example.com",
                    "insecure": True, "alpn": [], "fingerprint": "chrome", "name": "家宽"}
    assert parse_link("anytls://pw@1.2.3.4:443")["insecure"] is False
    assert parse_link("anytls://pw@[2001:db8::1]:443?peer=a.com&allowInsecure=true")["server"] == "2001:db8::1"
    for bad, msg in [("anytls://@h:1", "没有密码"), ("anytls://pw@h", "地址和端口"), ("vless://pw@h:1", "只支持 anytls"),
                     ("anytls://pw@h:1 x", "空格")]:
        with pytest.raises(TunnelError, match=msg):
            parse_link(bad)


def test_link_is_masked_everywhere():
    assert mask_link(LINK).startswith("anytls://***@home.example.com:8443") and "w0rd" not in mask_link(LINK)
    assert mask_proxy(LINK) == mask_link(LINK)
    assert check_proxy(LINK) == LINK


def test_build_config_only_listens_locally_with_auth():
    cfg = build_config(parse_link(LINK), 19080, "u", "p")
    assert cfg["bind-address"] == "127.0.0.1" and cfg["allow-lan"] is False
    assert cfg["authentication"] == ["u:p"] and cfg["skip-auth-prefixes"] == []      # 本机也要验证
    assert cfg["rules"] == ["MATCH,node"] and cfg["geo-auto-update"] is False
    node = cfg["proxies"][0]
    assert node["type"] == "anytls" and node["password"] == "p@ss:w0rd" and node["skip-cert-verify"] is True
    assert node["sni"] == "home.example.com" and node["client-fingerprint"] == "chrome"


# ---------------------------------------------------------------- 进程管理
async def test_tunnel_start_stop(fake_bin, tmp_path, monkeypatch):
    record = tmp_path / "record.json"
    monkeypatch.setenv("FAKE_MIHOMO_MODE", "record")
    monkeypatch.setenv("FAKE_MIHOMO_RECORD", str(record))
    t = Tunnel(fake_bin)
    url = await t.start(LINK)
    try:
        assert url.startswith("socks5://r") and "@127.0.0.1:" in url
        port = int(url.rsplit(":", 1)[1])
        assert listening(port) and t.running
        seen = json.loads(record.read_text())
        assert seen["mode"] == "0o600"                                   # 含节点密码的配置只有自己可读
        assert seen["config"]["mixed-port"] == port and seen["config"]["proxies"][0]["server"] == "home.example.com"
        user_pass = url.split("//", 1)[1].split("@", 1)[0]
        assert seen["config"]["authentication"] == [user_pass]
        workdir = seen["args"][seen["args"].index("-d") + 1]
        assert os.path.isdir(workdir)
    finally:
        await t.stop()
    assert not t.running and t.proxy_url is None and not os.path.exists(workdir)   # 停止后清理临时配置
    await asyncio.sleep(0.2)
    assert not listening(port)


async def test_tunnel_reports_start_failure(fake_bin, monkeypatch):
    monkeypatch.setenv("FAKE_MIHOMO_MODE", "fail")
    t = Tunnel(fake_bin)
    with pytest.raises(TunnelError, match="invalid password"):
        await t.start(LINK)
    assert not t.running and t.error and "invalid password" in t.error


async def test_tunnel_missing_binary(monkeypatch, tmp_path):
    monkeypatch.setenv("MIHOMO_BIN", str(tmp_path / "nope"))
    monkeypatch.setenv("PATH", str(tmp_path))
    with pytest.raises(TunnelError, match="更新到最新版本"):
        await Tunnel().start(LINK)


async def test_tunnel_restarts_after_crash(fake_bin, monkeypatch):
    t = Tunnel(fake_bin)
    url = await t.start(LINK)
    port = int(url.rsplit(":", 1)[1])
    try:
        t._proc.kill()                                  # 模拟意外退出
        for _ in range(60):                             # 2 秒后自动重启
            await asyncio.sleep(0.1)
            if t.running and listening(port) and t.error is None:
                break
        assert t.running and listening(port) and t.error is None
        assert t.proxy_url == url                       # 地址不变，抓取器无需更新
    finally:
        await t.stop()


async def test_resolver(fake_bin):
    r = ProxyResolver(fake_bin)
    try:
        url = await r.apply(LINK)
        assert url.startswith("socks5://") and r.status(LINK)["running"] is True
        assert r.status(LINK)["node"] == "家宽 · home.example.com:8443"
        assert await r.apply(LINK) == url                # 同一个链接：继续使用，不重启
        assert await r.apply("http://1.2.3.4:8080") == "http://1.2.3.4:8080"   # 换成普通代理：关闭节点客户端
        assert not r.tunnel.running and r.status("http://1.2.3.4:8080") is None
        assert await r.apply("") is None
    finally:
        await r.close()


# ---------------------------------------------------------------- 网页设置
async def test_web_settings_with_link(fake_bin, monkeypatch):
    from aiohttp.test_utils import TestClient, TestServer

    from bot.auth import WebAuth
    from bot.db import DB
    from bot.fetcher import Fetcher
    from bot.monitor import Monitor
    from bot.runner import BotRunner
    from bot.web import create_app

    H = {"X-Requested-With": "vrb"}
    cfg = Config(web_admin_token="tok-1234567890abcdef", fetch_proxy="http://old:1")
    db = DB(":memory:")
    await db.open()
    fetcher = Fetcher("ua", proxy="http://old:1", host_min_gap=0)
    resolver = ProxyResolver(fake_bin)
    c = None
    try:
        runner = BotRunner(cfg, db)
        mon = Monitor(cfg, db, fetcher, runner.send, runner.edit)
        auth = WebAuth(cfg, db)
        await auth.init()
        c = TestClient(TestServer(create_app(cfg, db, mon, runner, auth, resolver=resolver)))
        await c.start_server()
        assert (await c.post("/api/login", json={"token": "tok-1234567890abcdef"}, headers=H)).status == 200
        # 节点客户端启动失败：不保存，继续用原来的代理
        monkeypatch.setenv("FAKE_MIHOMO_MODE", "fail")
        r = await c.put("/api/settings", json={"fetch_proxy": LINK}, headers=H)
        assert r.status == 400 and "设置未保存" in (await r.json())["error"]
        assert cfg.fetch_proxy == "http://old:1" and fetcher.proxy == "http://old:1"
        # 正常：保存链接，抓取器改用本机的节点客户端入口；网页上不显示节点密码
        monkeypatch.delenv("FAKE_MIHOMO_MODE")
        r = await c.put("/api/settings", json={"fetch_proxy": LINK}, headers=H)
        body = await r.json()
        assert r.status == 200 and body["tunnel"]["running"] is True and "w0rd" not in json.dumps(body)
        assert cfg.fetch_proxy == LINK and fetcher.proxy == resolver.tunnel.proxy_url
        assert fetcher.proxy.startswith("socks5://") and "@127.0.0.1:" in fetcher.proxy
        # 原样提交隐藏密码的链接 = 不修改，节点客户端不重启
        before = resolver.tunnel.proxy_url
        await c.put("/api/settings", json={"fetch_proxy": body["fetch_proxy"]}, headers=H)
        assert resolver.tunnel.proxy_url == before
        # 清除：节点客户端随之关闭
        await c.put("/api/settings", json={"fetch_proxy": ""}, headers=H)
        assert not resolver.tunnel.running and fetcher.proxy is None
    finally:
        if c is not None:
            await c.close()
        await resolver.close()
        await fetcher.close()
        await db.close()
