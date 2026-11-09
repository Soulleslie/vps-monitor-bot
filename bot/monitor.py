"""监控调度：定时抓取、状态比对、事件分发。"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass
from typing import Awaitable, Callable
from urllib.parse import quote

from .config import Config
from .db import DB, now
from .fetcher import Fetcher, FetchError, UnsafeURLError
from .db import load_meta
from .messages import h, render_card, site_display_name
from .meta import build_card, detect_location
from .parser import (
    IN_STOCK, OUT_OF_STOCK, UNKNOWN, Product, is_configure_page, parse_cart_probe, parse_config_groups,
    parse_store, pick_location_group,
)

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


VARIANT_MARK = "#loc:"   # 按地区拆分出的机型，键为 “<机型键>#loc:<选项值>”


def base_key(key: str) -> str:
    return key.split("#", 1)[0]


def _match_text(product: dict) -> str:
    """关键词匹配的文本：机型名称 + 识别出的地区中文名（让“东京”能匹配“Premium VPS · Tokyo”）。"""
    name = product.get("name") or ""
    loc = detect_location(name.rsplit(" · ", 1)[-1]) if VARIANT_MARK in (product.get("key") or "") else None
    return f"{name} {loc.text}" if loc else name


def chat_wants(subs: list, product: dict) -> bool:
    key = product.get("key") or ""
    for s in subs:
        if s["kind"] == "all":
            return True
        # 订阅整个机型时，也包含它拆分出的各个地区
        if s["kind"] == "product" and (s["value"] == key or s["value"] == base_key(key)):
            return True
        if s["kind"] == "keyword" and keyword_match(s["value"], _match_text(product)):
            return True
    return False


async def apply_snapshot(
    db: DB, site_id: int, parsed: list[Product], ts: int, cooldown: int, has_baseline: bool,
    keep_variants_of: set[str] | None = None, silent: bool = False,
) -> list[dict]:
    """把一次抓取结果写入数据库，返回产生的事件。

    规则：
    - 只有 缺货 → 有货 才算补货；unknown 不参与状态转换。
    - 首次抓取（没有基线）只记录，不推送。
    - 已有基线后出现的新机型且有货 → 上新事件。
    - 原本在页面上、这次消失的机型视为缺货（很多商家会隐藏缺货商品）。
    - 冷却时间内重复的补货只记历史，不推送，用于抑制库存抖动。
    - 按地区拆分：某个地区第一次出现时，如果这个机型之前已经拆分过（说明它是缺货后重新出现），
      记为补货；如果是第一次拆分，只记录不推送。
    - keep_variants_of：这些机型本次没能读取配置页，它们各地区的状态保持不变（不当作消失）。
    - silent：只更新状态，不产生任何事件、不写补货历史（修改监控网址后重新建立基线时使用）。
    """
    existing = {r["key"]: dict(r) for r in await db.site_products(site_id)}
    keep_variants_of = keep_variants_of or set()
    expanded_before = {base_key(k) for k in existing if VARIANT_MARK in k}
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
            if VARIANT_MARK in p.key:
                kind = "restock" if base_key(p.key) in expanded_before else None
            else:
                kind = "new" if has_baseline else None
            if kind and p.status == IN_STOCK and not silent:
                await db.conn.execute("UPDATE products SET last_notified_at = ? WHERE id = ?", (ts, row_id))
                await db.add_event(row_id, kind, p.qty, None, ts)
                events.append({"kind": kind, "ts": ts, "product": {**p.__dict__, "id": row_id}})
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
        if silent:
            pass
        elif restock:
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
        if VARIANT_MARK in key and base_key(key) in keep_variants_of:
            continue  # 配置页本次读取失败：保持原状态，避免误报“售罄”和随后的“补货”
        if old["status"] == IN_STOCK and not silent:
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
            if p.status == IN_STOCK and p.qty is None and p.order_url and (keys is None or base_key(p.key) in keys)
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
        """有人订阅的机型键（订阅了某个地区时，返回它所属的机型）。None 表示无法预先确定。"""
        subs = await self.db.site_subscriptions(site_id)
        if any(s["kind"] in ("all", "keyword") for s in subs):
            return None  # 关键词/全部订阅无法预先确定机型，交由上限控制
        return {base_key(s["value"]) for s in subs if s["kind"] == "product"}

    async def _expand_locations(self, products: list[Product], keys: set[str] | None) -> tuple[list[Product], set[str]]:
        """打开机型的购买页，读取“地区”配置项，把每个地区拆成一个单独的机型。

        返回 (拆分出的地区机型, 本次未能确认、应保持原状态的机型键)。
        """
        variants: list[Product] = []
        confirmed: set[str] = set()
        # 所有机型都拆分地区（这样后台能看到每个机型有哪些地区、可以单独勾选）；
        # 有人订阅的机型排在前面，超过每个网址的上限时优先保证它们
        candidates = [p for p in products if p.order_url and p.status != OUT_OF_STOCK and VARIANT_MARK not in p.key]
        candidates.sort(key=lambda p: 0 if keys is None or p.key in keys else 1)
        targets = candidates[:PROBE_LIMIT_PER_SITE]
        for p in targets:
            try:
                res = await self.fetcher.get(p.order_url, retries=1, fresh_session=True)
            except (FetchError, UnsafeURLError) as exc:
                log.info("读取配置页失败，保持各地区原状态：%s（%s）", p.name, exc)
                continue
            if not is_configure_page(res.text, res.url):
                if parse_cart_probe(res.text, res.url) == OUT_OF_STOCK:
                    p.status = OUT_OF_STOCK      # 整个机型缺货：各地区都视为售罄
                    confirmed.add(p.key)
                else:
                    log.info("无法识别配置页，保持各地区原状态：%s（%s）", p.name, res.url)
                continue
            confirmed.add(p.key)
            group = pick_location_group(parse_config_groups(res.text), is_location=lambda t: detect_location(t) is not None)
            if group is None:
                continue  # 没有地区选项（或所有地区都已下架）
            for opt in group.options:
                sep = "&" if "?" in p.order_url else "?"
                variants.append(Product(
                    key=f"{p.key}{VARIANT_MARK}{opt.value}",
                    name=f"{p.name} · {opt.label}",
                    status=IN_STOCK if opt.available else OUT_OF_STOCK,
                    price=p.price,
                    order_url=f"{p.order_url}{sep}configoption[{group.id}]={quote(opt.value)}",
                    pid=p.pid,
                    description=p.description,
                ))
            p.status = IN_STOCK if any(o.available for o in group.options) else OUT_OF_STOCK
        # 列表页上已经缺货的机型不用打开配置页（各地区随之视为售罄）；其余没能确认的保持原状态
        keep = {p.key for p in products if p.key not in confirmed and p.status != OUT_OF_STOCK}
        return variants, keep

    async def add_site(self, url: str, name: str | None = None):
        """抓取并建立基线（不推送）。

        name：用户填写的名称；留空时用页面生成的名称（如“Riven Cloud · Cloud Server”），
        页面上也取不到时用网址生成。网址已存在时直接返回；若填写了名称则更新为新名称。
        """
        name = (name or "").strip()[:60] or None
        site = await self.db.fetchone("SELECT * FROM sites WHERE url = ?", (url,))
        if site and site["last_ok"]:
            if name and name != site["name"]:
                await self.db.execute("UPDATE sites SET name = ? WHERE id = ?", (name, site["id"]))
            return await self.db.get_site(site["id"])
        parsed = await self.fetch_products(url)
        final_name = name or parsed.suggested_name or site_display_name(url)
        if site is None:
            site = await self.db.get_or_create_site(url, final_name)
        elif name:
            await self.db.execute("UPDATE sites SET name = ? WHERE id = ?", (name, site["id"]))
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

    async def change_site_url(self, site_id: int, url: str) -> int:
        """修改监控网址，保留推送对象、订阅、显示设置和名称。返回新页面识别到的机型数。

        先读取新网址，确认能识别到机型后才保存；随后静默建立新的基线（不推送、不写历史），
        避免新旧页面机型不同而产生一批误报。失败时抛出 FetchError / ValueError，不做任何改动。
        """
        if site_id in self._running:
            raise ValueError("这个网址正在检查中，请稍后再试")
        self._running.add(site_id)
        try:
            site = await self.db.get_site(site_id)
            if site is None:
                raise ValueError("站点不存在")
            if url == site["url"]:
                return len(await self.db.site_products(site_id, present_only=True))
            other = await self.db.fetchone("SELECT id, name FROM sites WHERE url = ? AND id != ?", (url, site_id))
            if other:
                raise ValueError(f"这个网址已经在监控列表中（#{other['id']} {other['name']}）")
            parsed = await self.fetch_products(url)            # 读取失败会抛出 FetchError，此时不做任何改动
            await self.db.execute("UPDATE sites SET url = ? WHERE id = ?", (url, site_id))
            # 新页面上仍存在的机型，保留其各地区状态，等下次检查时再按配置页确认
            keep = {p.key for p in parsed.products}
            await apply_snapshot(
                self.db, site_id, parsed.products, now(), self.cfg.notify_cooldown,
                has_baseline=False, keep_variants_of=keep, silent=True,
            )
            await self.db.execute("UPDATE sites SET fail_alerted = 0 WHERE id = ?", (site_id,))
            await self.db.update_site_check(site_id, ok=True, method=parsed.method)
            log.info("站点 %s 的网址已修改：%s → %s", site_id, site["url"], url)
            return len(parsed.products)
        finally:
            self._running.discard(site_id)

    async def check_site(self, site_id: int) -> CheckOutcome:
        site = await self.db.get_site(site_id)
        if site is None:
            return CheckOutcome(ok=False, error="站点不存在")
        started = now()  # 间隔从检查开始时算，避免网页加载慢把实际间隔拉长
        self.total_checks += 1
        expand = load_meta(site["meta"]).get("expand") == "1"
        keep: set[str] | None = None
        try:
            keys = await self._subscribed_keys(site_id)
            # 按地区拆分时会打开每个机型的购买页，已包含“购物车探测”的作用，不再重复请求
            parsed = await self.fetch_products(
                site["url"], probe=site["probe_mode"] == "auto" and not expand, probe_keys=keys
            )
            if expand:
                variants, keep = await self._expand_locations(parsed.products, keys)
                parsed.products.extend(variants)
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
            self.db, site_id, parsed.products, now(), self.cfg.notify_cooldown,
            has_baseline=site["last_ok"] is not None, keep_variants_of=keep,
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
    async def card_for(self, site, product: dict, price: str | None = None):
        """按网址/机型的显示设置生成推送卡片。product 需包含 id；会读取数据库中最新的显示设置。"""
        row = await self.db.get_product(product["id"])
        base = {**dict(row), **{k: v for k, v in product.items() if v is not None}} if row else dict(product)
        meta = load_meta(row["meta"]) if row else {}
        return build_card(dict(site), load_meta(site["meta"]), base, meta, price_override=price)

    async def dispatch(self, site, events: list[dict]) -> None:
        ts = events[0]["ts"]
        # 1. 售罄：把之前发出的补货消息编辑成“售罄”样式
        sold_ids = [e["product"]["id"] for e in events if e["kind"] == "soldout"]
        if sold_ids:
            await self.refresh_notices(site, await self.db.mark_items_soldout(sold_ids, ts), ts)

        # 2. 补货/上新：每个机型单独一条消息。
        # 已按地区拆分的机型，只推送各地区的变化，不再推送机型整体的变化（避免同一件事推两条）
        split = {base_key(r["key"]) for r in await self.db.site_products(site["id"]) if VARIANT_MARK in r["key"]}
        events = [e for e in events if VARIANT_MARK in e["product"].get("key", "") or e["product"].get("key") not in split]
        subs_by_chat: dict[int, list] = defaultdict(list)
        for s in await self.db.site_subscriptions(site["id"]):
            subs_by_chat[s["chat_id"]].append(s)
        watchers = {c["chat_id"]: c for c in await self.db.site_watchers(site["id"])}
        cards: dict[tuple[int, str], object] = {}

        for chat_id, chat in watchers.items():
            subs = subs_by_chat.get(chat_id, [])
            for e in events:
                wanted = chat_wants(subs, e["product"])
                if e["kind"] == "restock" and wanted:
                    state = "restock"
                elif e["kind"] == "new" and (wanted or (chat["notify_new"] and subs)):
                    state = "new"
                elif e["kind"] == "soldout" and wanted and chat["notify_soldout"]:
                    state = "soldout"  # 可选的额外售罄提醒（原补货消息无论如何都会被编辑）
                else:
                    continue
                key = (e["product"]["id"], state)
                if key not in cards:
                    cards[key] = await self.card_for(site, e["product"])
                message_id = await self.send(chat_id, render_card(cards[key], state), None)
                if message_id and state != "soldout":
                    p = e["product"]
                    await self.db.add_notice(chat_id, message_id, site["id"], [{
                        "product_id": p["id"], "kind": e["kind"], "name": p["name"], "price": p.get("price"),
                        "qty": p.get("qty"), "order_url": p.get("order_url"), "ts": e["ts"],
                    }], ts)

    async def refresh_notices(self, site, notice_ids: list[int], ts: int) -> None:
        if self.edit is None:
            return
        for notice_id in notice_ids:
            notice, items = await self.db.get_notice(notice_id)
            if notice is None or not items:
                continue
            item = dict(items[0])
            if not item["soldout_ts"]:
                continue
            card = await self.card_for(site, {"id": item["product_id"], "name": item["name"],
                                              "order_url": item["order_url"]}, price=item["price"])
            still_editable = await self.edit(notice["chat_id"], notice["message_id"], render_card(card, "soldout"), None)
            if not still_editable or all(i["soldout_ts"] for i in items):
                await self.db.close_notice(notice_id)

    async def _notify_watchers(self, site_id: int, text: str) -> None:
        for chat in await self.db.site_watchers(site_id):
            await self.send(chat["chat_id"], text, None)
