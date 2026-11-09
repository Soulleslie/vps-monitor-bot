"""HTTP 抓取：URL 安全检查、同域名限速、重试。"""
from __future__ import annotations

import asyncio
import ipaddress
import logging
import random
import re
import socket
import time
from dataclasses import dataclass
from urllib.parse import urljoin, urlparse

import httpx

log = logging.getLogger(__name__)

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 5


class UnsafeURLError(ValueError):
    pass


class FetchError(RuntimeError):
    pass


@dataclass
class FetchResult:
    url: str          # 最终 URL（跟随跳转后）
    status: int
    text: str
    elapsed: float


def normalize_url(raw: str) -> str:
    raw = raw.strip().strip("<>")
    if re.match(r"^[a-z][a-z0-9+.-]*://", raw, re.I):
        if not raw.lower().startswith(("http://", "https://")):
            raise UnsafeURLError("只支持 http/https 网址")
    else:
        raw = "https://" + raw
    parsed = urlparse(raw)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise UnsafeURLError("只支持 http/https 网址")
    if parsed.username or parsed.password:
        raise UnsafeURLError("网址中不能包含账号密码")
    if len(raw) > 500:
        raise UnsafeURLError("网址过长")
    # 去掉锚点，保留查询参数（例如 ?language=chinese 会影响页面语言）
    return parsed._replace(fragment="").geturl()


async def assert_public_host(url: str) -> None:
    """拒绝指向内网/本机的地址，防止机器人被用来探测服务器内网（SSRF）。"""
    host = urlparse(url).hostname or ""
    try:
        ip = ipaddress.ip_address(host)
        addrs = [ip]
    except ValueError:
        if host.lower() in ("localhost",) or host.lower().endswith((".local", ".internal", ".localhost")):
            raise UnsafeURLError("不允许访问内网地址")
        loop = asyncio.get_running_loop()
        try:
            infos = await loop.getaddrinfo(host, None, type=socket.SOCK_STREAM)
        except socket.gaierror as exc:
            raise FetchError(f"域名解析失败: {host}") from exc
        addrs = [ipaddress.ip_address(info[4][0]) for info in infos]
    for addr in addrs:
        if not addr.is_global:
            raise UnsafeURLError("不允许访问内网/保留地址")


class Fetcher:
    def __init__(
        self,
        user_agent: str,
        timeout: float = 20.0,
        proxy: str | None = None,
        host_min_gap: float = 3.0,
        verify_hosts: bool = True,
    ) -> None:
        self._headers = {
            "User-Agent": user_agent,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Cache-Control": "no-cache",
        }
        self._timeout = httpx.Timeout(timeout, connect=min(10.0, timeout))
        self._proxy = proxy
        self._host_min_gap = host_min_gap
        self._host_locks: dict[str, asyncio.Lock] = {}
        self._host_last: dict[str, float] = {}
        self._verify_hosts = verify_hosts
        self._client = self._new_client(None)                       # 直连
        self._proxy_client = self._new_client(proxy) if proxy else None   # 通过抓取代理

    @property
    def proxy(self) -> str | None:
        return self._proxy

    def _new_client(self, proxy: str | None) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            headers=self._headers,
            timeout=self._timeout,
            proxy=proxy,
            follow_redirects=False,
            limits=httpx.Limits(max_connections=20, max_keepalive_connections=10),
        )

    async def set_proxy(self, proxy: str | None) -> None:
        """在网页上修改抓取代理后立即生效，不需要重启。"""
        old, self._proxy = self._proxy_client, proxy or None
        self._proxy_client = self._new_client(self._proxy) if self._proxy else None
        if old is not None:
            await old.aclose()

    async def close(self) -> None:
        await self._client.aclose()
        if self._proxy_client is not None:
            await self._proxy_client.aclose()

    async def _wait_host(self, host: str) -> None:
        lock = self._host_locks.setdefault(host, asyncio.Lock())
        async with lock:
            gap = self._host_min_gap - (time.monotonic() - self._host_last.get(host, 0.0))
            if gap > 0:
                await asyncio.sleep(gap)
            self._host_last[host] = time.monotonic()

    async def _get_once(self, client: httpx.AsyncClient, url: str) -> FetchResult:
        start = time.monotonic()
        current = url
        for _ in range(MAX_REDIRECTS + 1):
            if self._verify_hosts:
                await assert_public_host(current)
            await self._wait_host(urlparse(current).hostname or "")
            async with client.stream("GET", current) as resp:
                if resp.is_redirect and "location" in resp.headers:
                    current = urljoin(current, resp.headers["location"])
                    continue
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.aiter_bytes():
                    size += len(chunk)
                    if size > MAX_BYTES:
                        raise FetchError("页面过大（>5MB），已放弃")
                    chunks.append(chunk)
                body = b"".join(chunks)
                encoding = resp.encoding or "utf-8"
                try:
                    text = body.decode(encoding, errors="replace")
                except LookupError:
                    text = body.decode("utf-8", errors="replace")
                return FetchResult(str(resp.url), resp.status_code, text, time.monotonic() - start)
        raise FetchError("跳转次数过多")

    async def get(
        self, url: str, retries: int = 2, fresh_session: bool = False, use_proxy: bool = False
    ) -> FetchResult:
        """抓取页面。fresh_session=True 时使用独立 cookie（用于购物车探测，避免状态串扰）；
        use_proxy=True 时通过抓取代理访问。"""
        if use_proxy and not self._proxy:
            raise FetchError("这个网址设置为通过代理读取，但还没有填写抓取代理（设置 → 抓取代理）")
        proxy = self._proxy if use_proxy else None
        last_exc: Exception | None = None
        for attempt in range(retries + 1):
            shared = self._proxy_client if use_proxy else self._client
            client = self._new_client(proxy) if fresh_session else shared
            try:
                result = await self._get_once(client, url)
                if result.status >= 500 and attempt < retries:
                    raise FetchError(f"HTTP {result.status}")
                return result
            except UnsafeURLError:
                raise
            except (httpx.HTTPError, FetchError) as exc:
                last_exc = exc
                if attempt < retries:
                    await asyncio.sleep(1.5 * (2**attempt) + random.random())
            finally:
                if fresh_session:
                    await client.aclose()
        msg = str(last_exc) or last_exc.__class__.__name__
        if use_proxy and isinstance(last_exc, (httpx.ProxyError, httpx.ConnectError, httpx.ConnectTimeout)):
            msg = f"通过抓取代理连接失败，请检查代理是否可用（{msg}）"
        raise FetchError(msg) from last_exc
