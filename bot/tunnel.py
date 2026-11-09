"""内置节点客户端：在“抓取代理”里直接粘贴节点分享链接即可使用
（anytls / vless / vmess / trojan / ss / hysteria2 / tuic）。

原理：在容器里启动 mihomo（构建镜像时已下载并校验），让它连接你的节点，
同时只在容器内的 127.0.0.1 上开一个带随机账号密码的代理入口；抓取时走这个入口，
请求就会经过节点，从节点所在的网络（如家庭宽带）发出。

- 只监听 127.0.0.1，容器外（包括公网）都访问不到。
- 每次启动都用新的随机端口和账号密码，不写入数据库。
- mihomo 意外退出时自动重启（间隔逐步加长，最长 1 分钟）。
"""
from __future__ import annotations

import asyncio
import base64
import binascii
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
from urllib.parse import parse_qs, quote, unquote, urlparse

log = logging.getLogger(__name__)

TUNNEL_SCHEMES = ("anytls", "vless", "vmess", "trojan", "ss", "hysteria2", "hy2", "tuic")
READY_TIMEOUT = 15.0
BIN_ENV = "MIHOMO_BIN"
DEFAULT_BIN = "/usr/local/bin/mihomo"

_TRUE = ("1", "true", "yes", "on")
SUPPORTED_TEXT = "anytls / vless / vmess / trojan / ss / hysteria2 / tuic"


class TunnelError(ValueError):
    pass


def is_link(value: str | None) -> bool:
    return bool(value) and urlparse(value.strip()).scheme.lower() in TUNNEL_SCHEMES


def _b64(text: str) -> str | None:
    """宽松的 base64 解码（兼容 URL 安全字符和缺少的补齐），失败返回 None。"""
    raw = text.strip().replace("-", "+").replace("_", "/")
    raw += "=" * (-len(raw) % 4)
    try:
        return base64.b64decode(raw, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError, ValueError):
        return None


def _host_port(netloc: str, default_port: int | None = None) -> tuple[str, int]:
    parsed = urlparse("//" + netloc)
    try:
        port = parsed.port or default_port
    except ValueError:
        raise TunnelError("节点端口不正确（目前不支持端口范围）") from None
    if not parsed.hostname or not port:
        raise TunnelError("节点链接需要包含地址和端口，例如 ...@1.2.3.4:443")
    return parsed.hostname, port


def _query(parsed) -> dict[str, str]:
    return {k.lower(): v[-1] for k, v in parse_qs(parsed.query, keep_blank_values=True).items()}


def _flag(q: dict, *names: str) -> bool:
    return any(q.get(n, "").lower() in _TRUE for n in names)


def _alpn(value: str | None) -> list[str]:
    return [a.strip() for a in (value or "").split(",") if a.strip()]


def _transport(proxy: dict, net: str, path: str, host: str, service: str) -> None:
    """把 ws / grpc / h2 等传输方式写成 mihomo 的格式。"""
    net = (net or "tcp").lower()
    if net in ("tcp", "", "none", "raw"):
        return
    if net == "ws":
        opts: dict = {"path": path or "/"}
        if host:
            opts["headers"] = {"Host": host}
        proxy.update(network="ws", **{"ws-opts": opts})
    elif net == "grpc":
        proxy.update(network="grpc", **{"grpc-opts": {"grpc-service-name": service or path or ""}})
    elif net in ("h2", "http"):
        opts = {"path": path or "/"}
        if host:
            opts["host"] = [h.strip() for h in host.split(",") if h.strip()]
        proxy.update(network="h2", **{"h2-opts": opts})
    else:
        raise TunnelError(f"暂不支持 {net} 传输方式（支持 tcp / ws / grpc / h2）")


def _tls_common(proxy: dict, q: dict, sni_key: str = "servername") -> None:
    sni = q.get("sni") or q.get("peer") or q.get("servername") or ""
    if sni:
        proxy[sni_key] = sni
    if _flag(q, "insecure", "allowinsecure", "allow_insecure", "skip-cert-verify"):
        proxy["skip-cert-verify"] = True
    if _alpn(q.get("alpn")):
        proxy["alpn"] = _alpn(q.get("alpn"))
    fp = q.get("fp") or q.get("fingerprint") or q.get("client-fingerprint")
    if fp:
        proxy["client-fingerprint"] = fp


def _parse_vmess(link: str) -> dict:
    data = _b64(link[len("vmess://"):].split("#", 1)[0])
    try:
        cfg = json.loads(data) if data else None
    except ValueError:
        cfg = None
    if not isinstance(cfg, dict):
        raise TunnelError("vmess 链接格式不正确（应为 vmess:// 加 base64 编码的配置）")
    if not cfg.get("add") or not cfg.get("id"):
        raise TunnelError("vmess 链接缺少地址或 UUID")
    try:
        port, aid = int(cfg.get("port") or 0), int(cfg.get("aid") or 0)
    except (TypeError, ValueError):
        raise TunnelError("vmess 链接里的端口不正确") from None
    if not port:
        raise TunnelError("vmess 链接缺少端口")
    proxy = {"type": "vmess", "server": str(cfg["add"]), "port": port, "uuid": str(cfg["id"]), "alterId": aid,
             "cipher": cfg.get("scy") or "auto"}
    if str(cfg.get("tls", "")).lower() == "tls":
        proxy["tls"] = True
        q = {"sni": cfg.get("sni") or cfg.get("host") or "", "alpn": cfg.get("alpn") or "", "fp": cfg.get("fp") or "",
             "insecure": str(cfg.get("allowInsecure") or cfg.get("insecure") or "")}
        _tls_common(proxy, {k: str(v) for k, v in q.items()})
    _transport(proxy, cfg.get("net") or "tcp", str(cfg.get("path") or ""), str(cfg.get("host") or ""), str(cfg.get("path") or ""))
    return {"proxy": proxy, "name": str(cfg.get("ps") or "")}


def _parse_ss(link: str) -> dict:
    body, _, name = link[len("ss://"):].partition("#")
    body, _, query = body.partition("?")
    body = body.rstrip("/")
    if "@" in body:
        userinfo, hostport = body.rsplit("@", 1)
        decoded = _b64(unquote(userinfo))
        cred = decoded if decoded and ":" in decoded else unquote(userinfo)
    else:
        decoded = _b64(body)
        if not decoded or "@" not in decoded:
            raise TunnelError("ss 链接格式不正确")
        cred, hostport = decoded.rsplit("@", 1)
    if ":" not in cred:
        raise TunnelError("ss 链接缺少加密方式或密码")
    if "plugin=" in query:
        raise TunnelError("暂不支持带插件（plugin）的 ss 节点")
    method, password = cred.split(":", 1)
    server, port = _host_port(hostport)
    return {"proxy": {"type": "ss", "server": server, "port": port, "cipher": method, "password": password},
            "name": unquote(name)}


def parse_link(link: str) -> dict:
    """解析节点分享链接，返回 {"proxy": mihomo 节点配置, "name": 名称}。格式不对时抛 TunnelError。

    支持 anytls / vless（含 reality）/ vmess / trojan / ss / hysteria2 / tuic 的常见分享链接写法。"""
    link = (link or "").strip()
    if re.search(r"\s", link):
        raise TunnelError("节点链接不能包含空格或换行")
    scheme = urlparse(link).scheme.lower()
    if scheme not in TUNNEL_SCHEMES:
        raise TunnelError(f"不支持的节点链接，目前支持 {SUPPORTED_TEXT}")
    if scheme == "vmess":
        node = _parse_vmess(link)
    elif scheme == "ss":
        node = _parse_ss(link)
    else:
        parsed = urlparse(link)
        q = _query(parsed)
        user = unquote(parsed.username or "")
        secret = unquote(f"{parsed.username}:{parsed.password}") if parsed.password is not None else user
        host, port = _host_port(parsed.netloc.rsplit("@", 1)[-1], 443 if scheme in ("hysteria2", "hy2") else None)
        proxy: dict = {"server": host, "port": port}
        if scheme == "anytls":
            if not secret:
                raise TunnelError("节点链接里没有密码，应类似 anytls://密码@地址:端口")
            proxy.update(type="anytls", password=secret)
            _tls_common(proxy, q, "sni")
        elif scheme == "trojan":
            if not secret:
                raise TunnelError("trojan 链接里没有密码")
            proxy.update(type="trojan", password=secret)
            _tls_common(proxy, q, "sni")
            _transport(proxy, q.get("type", "tcp"), q.get("path", ""), q.get("host", ""), q.get("servicename", ""))
        elif scheme == "vless":
            if not user:
                raise TunnelError("vless 链接里没有 UUID")
            if q.get("encryption", "none") not in ("", "none"):
                raise TunnelError("暂不支持开启了 encryption 的 vless 节点")
            proxy.update(type="vless", uuid=user)
            security = q.get("security", "none").lower()
            if security in ("tls", "reality", "xtls"):
                proxy["tls"] = True
                _tls_common(proxy, q)
            if security == "reality":
                if not q.get("pbk"):
                    raise TunnelError("vless reality 链接缺少公钥（pbk）")
                proxy["reality-opts"] = {"public-key": q["pbk"], "short-id": q.get("sid", "")}
                proxy.setdefault("client-fingerprint", "chrome")   # reality 必须指定指纹
            if q.get("flow"):
                proxy["flow"] = q["flow"]
            _transport(proxy, q.get("type", "tcp"), q.get("path", ""), q.get("host", ""), q.get("servicename", ""))
        elif scheme in ("hysteria2", "hy2"):
            if not secret:
                raise TunnelError("hysteria2 链接里没有密码")
            proxy.update(type="hysteria2", password=secret)
            _tls_common(proxy, q, "sni")
            if q.get("obfs"):
                proxy["obfs"] = q["obfs"]
                proxy["obfs-password"] = q.get("obfs-password", "")
        elif scheme == "tuic":
            if not user or parsed.password is None:
                raise TunnelError("tuic 链接应类似 tuic://UUID:密码@地址:端口")
            proxy.update(type="tuic", uuid=user, password=unquote(parsed.password))
            _tls_common(proxy, q, "sni")
            proxy.setdefault("alpn", ["h3"])
            if q.get("congestion_control"):
                proxy["congestion-controller"] = q["congestion_control"]
            if q.get("udp_relay_mode"):
                proxy["udp-relay-mode"] = q["udp_relay_mode"]
            if _flag(q, "disable_sni"):
                proxy["disable-sni"] = True
        node = {"proxy": proxy, "name": unquote(parsed.fragment or "")}
    node["proxy"]["udp"] = False
    node["name"] = node["name"][:60]
    node["server"], node["port"] = node["proxy"]["server"], node["proxy"]["port"]
    return node


def mask_link(link: str) -> str:
    """显示用：不显示密码 / UUID，如 vless://***@1.2.3.4:443#家宽。"""
    scheme = urlparse(link.strip()).scheme.lower()
    try:
        node = parse_link(link)
    except TunnelError:
        return f"{scheme}://***"
    host = f"[{node['server']}]" if ":" in node["server"] else node["server"]
    return f"{scheme}://***@{host}:{node['port']}" + (f"#{node['name']}" if node["name"] else "")


def describe(link: str) -> str:
    node = parse_link(link)
    kind = node["proxy"]["type"]
    return f"{node['name'] + ' · ' if node['name'] else ''}{kind} {node['server']}:{node['port']}"


def build_config(node: dict, port: int, user: str, password: str) -> dict:
    """生成 mihomo 配置（JSON 也是合法的 YAML）。所有请求都走这个节点。"""
    proxy = {"name": "node", **node["proxy"]}
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


def normalize_socks_link(value: str) -> str:
    """v2rayN 等导出的 socks://base64(用户:密码)@地址:端口#名称 → socks5://用户:密码@地址:端口。其他原样返回。"""
    parsed = urlparse(value)
    if parsed.scheme.lower() != "socks":
        return value
    netloc = parsed.netloc
    if "@" in netloc:
        userinfo, hostport = netloc.rsplit("@", 1)
        decoded = _b64(unquote(userinfo))
        cred = decoded if decoded and ":" in decoded else unquote(userinfo)
        user, _, pw = cred.partition(":")
        netloc = f"{quote(user, safe='')}:{quote(pw, safe='')}@{hostport}" if pw else f"{quote(user, safe='')}@{hostport}"
    return f"socks5://{netloc}"


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
