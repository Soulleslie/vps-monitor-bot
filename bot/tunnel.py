"""内置节点客户端：在“抓取代理”里直接粘贴 anytls:// 节点链接即可使用。

原理：在容器里启动 mihomo（构建镜像时已下载并校验），让它连接你的节点，
同时只在容器内的 127.0.0.1 上开一个带随机账号密码的代理入口；抓取时走这个入口，
请求就会经过节点，从节点所在的网络（如家庭宽带）发出。

- 只监听 127.0.0.1，容器外（包括公网）都访问不到。
- 每次启动都用新的随机端口和账号密码，不写入数据库。
- mihomo 意外退出时自动重启（间隔逐步加长，最长 1 分钟）。
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import socket
import tempfile
from collections import deque
from urllib.parse import parse_qs, unquote, urlparse

log = logging.getLogger(__name__)

TUNNEL_SCHEMES = ("anytls",)
READY_TIMEOUT = 15.0
BIN_ENV = "MIHOMO_BIN"
DEFAULT_BIN = "/usr/local/bin/mihomo"

_TRUE = ("1", "true", "yes", "on")


class TunnelError(ValueError):
    pass


def is_link(value: str | None) -> bool:
    return bool(value) and urlparse(value.strip()).scheme.lower() in TUNNEL_SCHEMES


def parse_link(link: str) -> dict:
    """解析 anytls://密码@地址:端口?sni=...&insecure=1#名称，返回节点参数。格式不对时抛 TunnelError。"""
    link = (link or "").strip()
    if re.search(r"\s", link):
        raise TunnelError("节点链接不能包含空格或换行")
    parsed = urlparse(link)
    if parsed.scheme.lower() != "anytls":
        raise TunnelError("目前只支持 anytls:// 节点链接")
    # anytls 链接的密码写在用户名位置（anytls://密码@地址:端口）
    password = unquote(parsed.username or "")
    if parsed.password:   # 少数客户端会写成 用户:密码 的形式
        password = unquote(f"{parsed.username}:{parsed.password}")
    if not password:
        raise TunnelError("节点链接里没有密码，应类似 anytls://密码@地址:端口")
    host = parsed.hostname
    try:
        port = parsed.port
    except ValueError:
        raise TunnelError("节点端口不正确") from None
    if not host or not port:
        raise TunnelError("节点链接需要包含地址和端口，应类似 anytls://密码@1.2.3.4:443")
    q = {k.lower(): v[-1] for k, v in parse_qs(parsed.query).items()}
    sni = q.get("sni") or q.get("peer") or q.get("servername") or ""
    insecure = any(q.get(k, "").lower() in _TRUE for k in ("insecure", "allowinsecure", "skip-cert-verify"))
    alpn = [a for a in (q.get("alpn") or "").split(",") if a]
    fingerprint = q.get("fp") or q.get("fingerprint") or q.get("client-fingerprint") or ""
    return {
        "server": host, "port": port, "password": password, "sni": sni, "insecure": insecure,
        "alpn": alpn, "fingerprint": fingerprint, "name": unquote(parsed.fragment or "")[:60],
    }


def mask_link(link: str) -> str:
    """显示用：隐藏节点密码，如 anytls://***@1.2.3.4:443?sni=a.com#家宽。"""
    parsed = urlparse(link.strip())
    netloc = parsed.netloc.rsplit("@", 1)[-1]
    out = f"{parsed.scheme}://***@{netloc}"
    if parsed.query:
        out += "?" + parsed.query
    if parsed.fragment:
        out += "#" + parsed.fragment
    return out


def describe(link: str) -> str:
    node = parse_link(link)
    return f"{node['name'] + ' · ' if node['name'] else ''}{node['server']}:{node['port']}"


def build_config(node: dict, port: int, user: str, password: str) -> dict:
    """生成 mihomo 配置（JSON 也是合法的 YAML）。所有请求都走这个节点。"""
    proxy = {
        "name": "node", "type": "anytls", "server": node["server"], "port": node["port"],
        "password": node["password"], "udp": False, "skip-cert-verify": bool(node["insecure"]),
    }
    if node["sni"]:
        proxy["sni"] = node["sni"]
    if node["alpn"]:
        proxy["alpn"] = node["alpn"]
    if node["fingerprint"]:
        proxy["client-fingerprint"] = node["fingerprint"]
    return {
        "mixed-port": port,
        "bind-address": "127.0.0.1",
        "allow-lan": False,
        "authentication": [f"{user}:{password}"],
        "skip-auth-prefixes": [],
        "mode": "rule",
        "log-level": "warning",
        "ipv6": True,
        "geo-auto-update": False,
        "find-process-mode": "off",
        "profile": {"store-selected": False, "store-fake-ip": False},
        "proxies": [proxy],
        "rules": ["MATCH,node"],
    }


def find_binary() -> str | None:
    path = os.environ.get(BIN_ENV) or DEFAULT_BIN
    if os.path.isfile(path) and os.access(path, os.X_OK):
        return path
    return shutil.which("mihomo")


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


async def _port_open(port: int) -> bool:
    try:
        _, writer = await asyncio.wait_for(asyncio.open_connection("127.0.0.1", port), 1.0)
    except (OSError, asyncio.TimeoutError):
        return False
    writer.close()
    with contextlib.suppress(Exception):
        await writer.wait_closed()
    return True


class Tunnel:
    """管理一个 mihomo 进程。start() 成功后 proxy_url 即为本机代理地址。"""

    def __init__(self, binary: str | None = None) -> None:
        self.binary = binary
        self.link: str | None = None
        self.proxy_url: str | None = None
        self.error: str | None = None
        self._proc: asyncio.subprocess.Process | None = None
        self._dir: str | None = None
        self._config: dict | None = None
        self._port = 0
        self._watch: asyncio.Task | None = None
        self._reader: asyncio.Task | None = None
        self._stopping = False
        self._lines: deque[str] = deque(maxlen=20)

    @property
    def running(self) -> bool:
        return self._proc is not None and self._proc.returncode is None

    async def start(self, link: str) -> str:
        """启动并等待代理入口可用，返回本机代理地址；失败时抛 TunnelError（并清理）。"""
        await self.stop()
        node = parse_link(link)
        binary = self.binary or find_binary()
        if not binary:
            raise TunnelError("当前安装的程序不包含节点客户端，请先在“设置 → 版本与更新”更新到最新版本")
        self.binary = binary
        user, password = "r" + secrets.token_hex(6), secrets.token_hex(16)
        self._port = _free_port()
        self._config = build_config(node, self._port, user, password)
        self._dir = tempfile.mkdtemp(prefix="vrb-mihomo-")
        path = os.path.join(self._dir, "config.yaml")
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)   # 含节点密码，只有自己可读
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(self._config, fh, ensure_ascii=False)
        self.link = link
        self._stopping = False
        try:
            await self._spawn()
            await self._wait_ready()
        except TunnelError as exc:
            self.error = str(exc)
            await self.stop(keep_error=True)
            raise
        self.proxy_url = f"socks5://{user}:{password}@127.0.0.1:{self._port}"
        self.error = None
        self._watch = asyncio.create_task(self._watchdog(), name="tunnel-watchdog")
        log.info("节点客户端已启动：%s", describe(link))
        return self.proxy_url

    async def _spawn(self) -> None:
        self._lines.clear()
        try:
            self._proc = await asyncio.create_subprocess_exec(
                self.binary, "-d", self._dir, "-f", os.path.join(self._dir, "config.yaml"),
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
            )
        except OSError as exc:
            raise TunnelError(f"无法启动节点客户端：{exc}") from None
        self._reader = asyncio.create_task(self._read_output(self._proc), name="tunnel-output")

    async def _read_output(self, proc: asyncio.subprocess.Process) -> None:
        assert proc.stdout is not None
        while True:
            raw = await proc.stdout.readline()
            if not raw:
                return
            line = raw.decode("utf-8", "replace").strip()
            if line:
                self._lines.append(line[:300])
                if "level=error" in line or "level=fatal" in line or "ERR" in line[:40]:
                    log.warning("节点客户端：%s", line[:300])

    def last_output(self) -> str:
        return " | ".join(list(self._lines)[-3:])

    async def _wait_ready(self) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + READY_TIMEOUT
        while loop.time() < deadline:
            if self._proc.returncode is not None:
                await asyncio.sleep(0.1)   # 等输出读完
                detail = self.last_output() or f"退出码 {self._proc.returncode}"
                raise TunnelError(f"节点客户端启动失败：{detail}")
            if await _port_open(self._port):
                return
            await asyncio.sleep(0.2)
        raise TunnelError("节点客户端启动超时")

    async def _watchdog(self) -> None:
        """进程意外退出时自动重启。"""
        delay = 2.0
        while not self._stopping:
            proc = self._proc
            if proc is None:
                return
            code = await proc.wait()
            if self._stopping:
                return
            self.error = f"节点客户端意外退出（{code}）：{self.last_output() or '无输出'}，{int(delay)} 秒后重启"
            log.warning(self.error)
            await asyncio.sleep(delay)
            if self._stopping:
                return
            try:
                await self._spawn()
                await self._wait_ready()
                self.error = None
                log.info("节点客户端已重新启动")
                delay = 2.0
            except TunnelError as exc:
                self.error = str(exc)
                log.warning("节点客户端重启失败：%s", exc)
                delay = min(delay * 2, 60.0)

    async def stop(self, keep_error: bool = False) -> None:
        self._stopping = True
        watch, self._watch = self._watch, None
        if watch is not None and watch is not asyncio.current_task():
            watch.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watch
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), 5)
            except asyncio.TimeoutError:
                proc.kill()
                await proc.wait()
        reader, self._reader = self._reader, None
        if reader is not None:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await reader
        if self._dir:
            shutil.rmtree(self._dir, ignore_errors=True)
            self._dir = None
        self.proxy_url = None
        self.link = None
        if not keep_error:
            self.error = None


class ProxyResolver:
    """把“抓取代理”设置解析为实际使用的代理地址：普通代理原样返回；节点链接则启动内置客户端。"""

    def __init__(self, binary: str | None = None) -> None:
        self.tunnel = Tunnel(binary)

    async def apply(self, value: str | None) -> str | None:
        """应用新的设置，返回抓取时使用的代理地址；节点客户端启动失败时抛 TunnelError。"""
        if is_link(value):
            if self.tunnel.running and self.tunnel.link == value:
                return self.tunnel.proxy_url
            return await self.tunnel.start(value)
        await self.tunnel.stop()
        return value or None

    def status(self, value: str | None) -> dict | None:
        if not is_link(value):
            return None
        return {
            "node": describe(value),
            "running": self.tunnel.running and self.tunnel.link == value,
            "error": self.tunnel.error,
        }

    async def close(self) -> None:
        await self.tunnel.stop()
