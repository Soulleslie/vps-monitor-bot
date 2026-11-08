"""监控调度：定时抓取、状态比对、事件分发。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable

from .config import Config
from .db import DB, now
from .fetcher import Fetcher, FetchError, UnsafeURLError
from .messages import event_message, h, render_notice
from .parser import IN_STOCK, OUT_OF_STOCK, UNKNOWN, Product, parse_cart_probe, parse_store

log = logging.getLogger(__name__)

# send(chat_id, html_text, reply_markup) -> 消息 ID（失败返回 None）
Sender = Callable[[int, str, object], Awaitable[int | None]]
# edit(chat_id, message_id, html_text, reply_markup) -> 是否仍可编辑（消息被删除等返回 False）
Editor = Callable[[int, int, str, object], Awaitable[bool]]

PROBE_LIMIT_PER_SITE = 10


@dataclass
class CheckOutcome:
    ok: bool
    products: int = 0
    events: int = 0
    error: str | None = None
    method: str | None = None


def keyword_match(keyword: str, name: str) -> bool:
    """关键词以空格分隔，全部命中才算匹配（不区分大小写）。"""
    terms = [t for t in keyword.lower().split() if t]
    lowered = name.lower()
    return bool(terms) and all(t in lowered for t in terms)


def chat_wants(subs: list, product: dict) -> bool:
    for s in subs:
        if s["kind"] == "all":
            return True
        if s["kind"] == "product" and s["value"] == product["key"]:
            return True
        if s["kind"] == "keyword" and keyword_match(s["value"], product["name"]):
            return True
    return False


async def apply_snapshot(
    db: DB, site_id: int, parsed: list[Product], ts: int, cooldown: int, has_baseline: bool
) -> list[dict]:
    """把一次抓取结果写入数据库，返回产生的事件。

    规则：
    - 只有 缺货 → 有货 才算补货；unknown 不参与状态转换。
    - 首次抓取（没有基线）只记录，不推送。
    - 已有基线后出现的新机型且有货 → 上新事件。
    - 原本在页面上、这次消失的机型视为缺货（很多商家会隐藏缺货商品）。
    - 冷却时间内重复的补货只记历史，不推送，用于抑制库存抖动。
    """
    existing = {r["key"]: dict(r) for r in await db.site_products(site_id)}
    events: list[dict] = []
    seen: set[str] = set()

    for p in parsed:
        seen.add(p.key)
        old = existing.get(p.key)
        if old is None:
            in_since = ts if p.status == IN_STOCK else None
            pid = await db.conn.execute(
                "INSERT INTO products (site_id, key, name, status, qty, price, order_url, description, "
                "present, first_seen, last_seen, in_stock_since) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?)",
                (site_id, p.key, p.name, p.status, p.qty, p.price, p.order_url, p.description, ts, ts, in_since),
            )
            row_id = pid.lastrowid
            if has_baseline and p.status == IN_STOCK:
                await db.conn.execute("UPDATE products SET last_notified_at = ? WHERE id = ?", (ts, row_id))
                await db.add_event(row_id, "new", p.qty, None, ts)
                events.append({"kind": "new", "ts": ts, "product": {**p.__dict__, "id": row_id}})
            continue

        old_status = old["status"]
        new_status = p.status if p.status != UNKNOWN else old_status
        restock = old_status == OUT_OF_STOCK and new_status == IN_STOCK
        soldout = old_status == IN_STOCK and new_status == OUT_OF_STOCK

        in_since = old["in_stock_since"]
        if restock or (new_status == IN_STOCK and in_since is None):
            in_since = ts
        elif new_status != IN_STOCK:
            in_since = None

        notified_at = old["last_notified_at"]
        product_view = {**p.__dict__, "id": old["id"], "status": new_status}
        if restock:
            await db.add_event(old["id"], "restock", p.qty, None, ts)
            if notified_at is None or ts - notified_at >= cooldown:
                notified_at = ts
                events.append({"kind": "restock", "ts": ts, "product": product_view})
            else:
                log.info("补货在冷却期内，跳过推送: site=%s %s", site_id, p.name)
        elif soldout:
            duration = ts - old["in_stock_since"] if old["in_stock_since"] else None
            await db.add_event(old["id"], "soldout", None, duration, ts)
            events.append({"kind": "soldout", "ts": ts, "duration": duration, "product": product_view})

        await db.conn.execute(
            "UPDATE products SET name = ?, status = ?, qty = ?, price = COALESCE(?, price), "
            "order_url = COALESCE(?, order_url), description = COALESCE(?, description), present = 1, "
            "last_seen = ?, in_stock_since = ?, last_notified_at = ? WHERE id = ?",
            (p.name, new_status, p.qty, p.price, p.order_url, p.description, ts, in_since, notified_at, old["id"]),
        )

    # 页面上消失的机型
    for key, old in existing.items():
        if key in seen or not old["present"]:
            continue
        if old["status"] == IN_STOCK:
            duration = ts - old["in_stock_since"] if old["in_stock_since"] else None
            await db.add_event(old["id"], "soldout", None, duration, ts)
            events.append({"kind": "soldout", "ts": ts, "duration": duration, "product": {**old, "qty": None}})
        await db.conn.execute(
            "UPDATE products SET present = 0, status = ?, qty = NULL, in_stock_since = NULL WHERE id = ?",
            (OUT_OF_STOCK if old["status"] != UNKNOWN else UNKNOWN, old["id"]),
        )

    await db.conn.commit()
    return events


class Monitor:
    def __init__(self, cfg: Config, db: DB, fetcher: Fetcher, send: Sender, edit: Editor | None = None) -> None:
        self.cfg = cfg
        self.db = db
        self.fetcher = fetcher
        self.send = send
        self.edit = edit
        self._sem = asyncio.Semaphore(cfg.concurrency)
        self._running: set[int] = set()
        self._task: asyncio.Task | None = None
        self._stopping = asyncio.Event()
        self.started_at = time.time()
        self.total_checks = 0
        self.total_events = 0

    # ---------- 生命周期 ----------
    def start(self) -> None:
        self._task = asyncio.create_task(self._loop(), name="monitor-loop")

    async def stop(self) -> None:
        self._stopping.set()
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    async def _loop(self) -> None:
        log.info("监控已启动，默认间隔 %ss，并发 %s", self.cfg.check_interval, self.cfg.concurrency)
        while not self._stopping.is_set():
            try:
                for site in await self.db.due_sites(self.cfg.check_interval, now()):
                    if site["id"] not in self._running:
                        self._running.add(site["id"])
                        asyncio.create_task(self._guarded_check(site["id"]))
            except Exception:  # 调度本身不能因为单次异常退出
                log.exception("调度循环异常")
            try:
                await asyncio.wait_for(self._stopping.wait(), timeout=1)
            except asyncio.TimeoutError:
                pass

    async def _guarded_check(self, site_id: int) -> None:
        try:
            async with self._sem:
                await self.check_site(site_id)
        except Exception:
            log.exception("检查站点 %s 时出现未处理异常", site_id)
        finally:
            self._running.discard(site_id)

    # ---------- 抓取与解析 ----------
    async def fetch_products(self, url: str, probe: bool = False, probe_keys: set[str] | None = None):
        """抓取并解析。供添加网址时预览和定时检查共用。"""
        result = await self.fetcher.get(url)
        parsed = parse_store(result.text, result.url, result.status)
        if not parsed.products:
            reason = parsed.blocked_reason or (
                f"HTTP {result.status}，页面中没有识别到商品" if result.status != 200 else "页面中没有识别到商品"
            )
            raise FetchError(reason)
        if probe:
            await self._probe(parsed.products, probe_keys)
        return parsed

    async def _probe(self, products: list[Product], keys: set[str] | None) -> None:
        """页面上既没显示库存数量也没有缺货标记时，访问购物车链接确认库存。只探测有人订阅的机型。"""
        targets = [
            p for p in products
            if p.status == IN_STOCK and p.qty is None and p.order_url and (keys is None or p.key in keys)
        ][:PROBE_LIMIT_PER_SITE]
        for p in targets:
            try:
                res = await self.fetcher.get(p.order_url, retries=1, fresh_session=True)
            except (FetchError, UnsafeURLError) as exc:
                log.debug("探测失败 %s: %s", p.order_url, exc)
                continue
            status = parse_cart_probe(res.text, res.url)
            if status != UNKNOWN:
                p.status = status
                p.extra["probed"] = True

    async def _subscribed_keys(self, site_id: int) -> set[str] | None:
        subs = await self.db.site_subscriptions(site_id)
        if any(s["kind"] in ("all", "keyword") for s in subs):
            return None  # 关键词/全部订阅无法预先确定机型，交由上限控制
        return {s["value"] for s in subs if s["kind"] == "product"}

    async def add_site(self, url: str, name: str):
        """抓取并建立基线（不推送）。已存在的站点直接返回。"""
        site = await self.db.fetchone("SELECT * FROM sites WHERE url = ?", (url,))
        if site and site["last_ok"]:
            return site
        parsed = await self.fetch_products(url)
        site = site or await self.db.get_or_create_site(url, name)
        await apply_snapshot(self.db, site["id"], parsed.products, now(), self.cfg.notify_cooldown, has_baseline=False)
        await self.db.update_site_check(site["id"], ok=True, method=parsed.method)
        return await self.db.get_site(site["id"])

    async def check_now(self, site_id: int) -> CheckOutcome:
        """手动立即检查；若定时任务正在检查同一站点则不重复执行。"""
        if site_id in self._running:
            return CheckOutcome(ok=False, error="正在检查中，请稍后")
        self._running.add(site_id)
        try:
            return await self.check_site(site_id)
        finally:
            self._running.discard(site_id)

    async def check_site(self, site_id: int) -> CheckOutcome:
        site = await self.db.get_site(site_id)
        if site is None:
            return CheckOutcome(ok=False, error="站点不存在")
        started = now()  # 间隔从检查开始时算，避免网页加载慢把实际间隔拉长
        self.total_checks += 1
        try:
            keys = await self._subscribed_keys(site_id)
            parsed = await self.fetch_products(site["url"], probe=site["probe_mode"] == "auto", probe_keys=keys)
        except (FetchError, UnsafeURLError) as exc:
            updated = await self.db.update_site_check(site_id, ok=False, error=str(exc), ts=started)
            log.warning("站点 %s 检查失败（连续 %s 次）: %s", site["url"], updated["fail_count"], exc)
            if updated["fail_count"] >= self.cfg.fail_alert_threshold and not updated["fail_alerted"]:
                await self.db.execute("UPDATE sites SET fail_alerted = 1 WHERE id = ?", (site_id,))
                await self._notify_watchers(
                    site_id,
                    f"⚠️ <b>监控异常</b> · {h(site['name'])}\n"
                    f"已连续失败 {updated['fail_count']} 次：{h(exc)}\n"
                    "机器人会自动降低频率重试，恢复后会通知你。",
                )
            return CheckOutcome(ok=False, error=str(exc))

        events = await apply_snapshot(
            self.db, site_id, parsed.products, now(), self.cfg.notify_cooldown, has_baseline=site["last_ok"] is not None
        )
        await self.db.update_site_check(site_id, ok=True, method=parsed.method, ts=started)
        if site["fail_alerted"]:
            await self.db.execute("UPDATE sites SET fail_alerted = 0 WHERE id = ?", (site_id,))
            await self._notify_watchers(site_id, f"✅ <b>监控已恢复</b> · {h(site['name'])}")
        if events:
            self.total_events += len(events)
            await self.dispatch(site, events)
        return CheckOutcome(ok=True, products=len(parsed.products), events=len(events), method=parsed.method)

    # ---------- 分发 ----------
    async def dispatch(self, site, events: list[dict]) -> None:
        ts = events[0]["ts"]
        # 1. 售罄：把之前发出的补货消息编辑成“已售罄”
        sold_ids = [e["product"]["id"] for e in events if e["kind"] == "soldout"]
        if sold_ids:
            await self.refresh_notices(site, await self.db.mark_items_soldout(sold_ids, ts), ts)

        # 2. 补货/上新：按订阅发送新消息
        subs_by_chat: dict[int, list] = defaultdict(list)
        for s in await self.db.site_subscriptions(site["id"]):
            subs_by_chat[s["chat_id"]].append(s)
        watchers = {c["chat_id"]: c for c in await self.db.site_watchers(site["id"])}

        for chat_id, chat in watchers.items():
            subs = subs_by_chat.get(chat_id, [])
            fresh, soldout = [], []
            for e in events:
                wanted = chat_wants(subs, e["product"])
                if e["kind"] == "restock" and wanted:
                    fresh.append(e)
                elif e["kind"] == "new" and (wanted or (chat["notify_new"] and subs)):
                    fresh.append(e)
                elif e["kind"] == "soldout" and wanted and chat["notify_soldout"]:
                    soldout.append(e)
            if fresh:
                items = [
                    {
                        "product_id": e["product"]["id"],
                        "kind": e["kind"],
                        "name": e["product"]["name"],
                        "price": e["product"].get("price"),
                        "qty": e["product"].get("qty"),
                        "order_url": e["product"].get("order_url"),
                        "ts": e["ts"],
                        "soldout_ts": None,
                    }
                    for e in fresh
                ]
                text, markup = render_notice(site["name"], site["url"], items, self.cfg.tz)
                message_id = await self.send(chat_id, text, markup)
                if message_id:
                    await self.db.add_notice(chat_id, message_id, site["id"], items, ts)
            if soldout:  # 可选：额外发一条售罄提醒（默认关闭，原消息无论如何都会被编辑）
                text, markup = event_message(site["name"], site["url"], soldout, self.cfg.tz)
                await self.send(chat_id, text, markup)

    async def refresh_notices(self, site, notice_ids: list[int], ts: int) -> None:
        if self.edit is None:
            return
        for notice_id in notice_ids:
            notice, items = await self.db.get_notice(notice_id)
            if notice is None or not items:
                continue
            text, markup = render_notice(site["name"], site["url"], [dict(i) for i in items], self.cfg.tz, ts)
            still_editable = await self.edit(notice["chat_id"], notice["message_id"], text, markup)
            if not still_editable or all(i["soldout_ts"] for i in items):
                await self.db.close_notice(notice_id)

    async def _notify_watchers(self, site_id: int, text: str) -> None:
        for chat in await self.db.site_watchers(site_id):
            await self.send(chat["chat_id"], text, None)
