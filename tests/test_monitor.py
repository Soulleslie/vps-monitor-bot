import pytest

from bot.db import DB
from bot.monitor import Monitor, apply_snapshot, keyword_match
from bot.parser import IN_STOCK, OUT_OF_STOCK, Product


def prod(key, status, qty=None, name=None):
    return Product(key=key, name=name or key, status=status, qty=qty, price="$1", order_url=f"https://s/{key}")


@pytest.fixture
async def db():
    d = DB(":memory:")
    await d.open()
    yield d
    await d.close()


async def site(db):
    return (await db.get_or_create_site("https://s/store/a", "s"))["id"]


async def test_first_snapshot_is_silent(db):
    sid = await site(db)
    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, 3)], 1000, 120, has_baseline=False)
    assert ev == []


async def test_restock_soldout_and_cooldown(db):
    sid = await site(db)
    await apply_snapshot(db, sid, [prod("a", OUT_OF_STOCK, 0)], 1000, 120, False)
    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, 2)], 1060, 120, True)
    assert [e["kind"] for e in ev] == ["restock"]
    ev = await apply_snapshot(db, sid, [prod("a", OUT_OF_STOCK, 0)], 1090, 120, True)
    assert [e["kind"] for e in ev] == ["soldout"] and ev[0]["duration"] == 30
    # 冷却期内再次补货：记录历史但不推送
    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, 1)], 1100, 120, True)
    assert ev == []
    hist = await db.site_events(sid)
    assert [h["kind"] for h in hist] == ["restock", "soldout", "restock"]
    # 冷却期后
    await apply_snapshot(db, sid, [prod("a", OUT_OF_STOCK, 0)], 1150, 120, True)
    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, 1)], 1300, 120, True)
    assert [e["kind"] for e in ev] == ["restock"]


async def test_unknown_does_not_trigger(db):
    sid = await site(db)
    await apply_snapshot(db, sid, [prod("a", OUT_OF_STOCK, 0)], 1000, 0, False)
    ev = await apply_snapshot(db, sid, [prod("a", "unknown")], 1060, 0, True)
    assert ev == []
    row = (await db.site_products(sid))[0]
    assert row["status"] == OUT_OF_STOCK


async def test_hidden_product_reappears_as_restock(db):
    sid = await site(db)
    await apply_snapshot(db, sid, [prod("a", IN_STOCK, 1), prod("b", IN_STOCK, 1)], 1000, 0, False)
    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, 1)], 1060, 0, True)
    assert [(e["kind"], e["product"]["key"]) for e in ev] == [("soldout", "b")]
    ev = await apply_snapshot(db, sid, [prod("a", IN_STOCK, 1), prod("b", IN_STOCK, 4)], 1120, 0, True)
    assert [(e["kind"], e["product"]["key"]) for e in ev] == [("restock", "b")]


async def test_new_product_after_baseline(db):
    sid = await site(db)
    await apply_snapshot(db, sid, [prod("a", OUT_OF_STOCK, 0)], 1000, 0, False)
    ev = await apply_snapshot(db, sid, [prod("a", OUT_OF_STOCK, 0), prod("c", IN_STOCK, 9)], 1060, 0, True)
    assert [(e["kind"], e["product"]["key"]) for e in ev] == [("new", "c")]


async def test_due_sites_respects_custom_interval(db):
    sid = await site(db)
    await db.ensure_chat(1, None)
    await db.set_chat_subscriptions(1, sid, True, [], [])
    await db.execute("UPDATE sites SET interval_sec = 7 WHERE id = ?", (sid,))
    await db.update_site_check(sid, ok=True, ts=1000)
    assert await db.due_sites(60, 1006) == []          # 未到 7 秒
    assert len(await db.due_sites(60, 1007)) == 1      # 刚好 7 秒
    await db.execute("UPDATE sites SET interval_sec = NULL WHERE id = ?", (sid,))
    assert await db.due_sites(60, 1059) == []          # 使用全局 60 秒
    assert len(await db.due_sites(60, 1060)) == 1


def test_keyword_match():
    assert keyword_match("hk 2c", "HK-Pro 2C2G")
    assert not keyword_match("hk 4c", "HK-Pro 2C2G")
    assert not keyword_match("   ", "x")


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages

    async def get(self, url, retries=2, fresh_session=False, use_proxy=False):
        from bot.fetcher import FetchResult
        return FetchResult(url, 200, self.pages.pop(0), 0.1)

    async def close(self):
        pass


def page(*items):
    cards = "".join(
        f'<div class="product" id="product{i}"><header><span id="product{i}-name">{n}</span>'
        f'<span class="qty">{q} Available</span></header>'
        f'<a id="product{i}-order-button" href="/cart.php?a=add&pid={i}">Order</a></div>'
        for i, (n, q) in enumerate(items, 1)
    )
    return f"<html><body>{cards}</body></html>"


async def test_dispatch_routes_by_subscription(db):
    from bot.config import Config
    from zoneinfo import ZoneInfo

    cfg = Config(bot_token="x", notify_cooldown=0, tz=ZoneInfo("UTC"))
    sent = []

    async def send(chat_id, text, markup):
        sent.append((chat_id, text))
        return len(sent)

    fetcher = FakeFetcher([
        page(("HK-A", 0), ("HK-B", 0), ("JP-C", 0)),
        page(("HK-A", 3), ("HK-B", 2), ("JP-C", 1)),
    ])
    mon = Monitor(cfg, db, fetcher, send)
    s = await db.get_or_create_site("https://s.example/store/a", "S")
    for chat in (1, 2, 3, 4):
        await db.ensure_chat(chat, None)
        await db.attach_site(chat, s["id"])
    await db.subscribe(1, s["id"], "product", "pid:1")      # 只要 HK-A
    await db.subscribe(2, s["id"], "keyword", "jp")         # 关键词 JP
    await db.subscribe(3, s["id"], "all")                   # 全部
    await db.subscribe(4, s["id"], "all")
    await db.set_chat_flag(4, "paused", 1)                   # 暂停的不推送

    assert (await mon.check_site(s["id"])).ok
    assert sent == []  # 首次只建立基线
    out = await mon.check_site(s["id"])
    assert out.events == 3
    got: dict[int, list[str]] = {}
    for c, t in sent:  # 每个机型单独一条消息
        got.setdefault(c, []).append(t)
    assert set(got) == {1, 2, 3}

    def names(msgs):
        return sorted(m.split("🏷 ")[1].split("\n")[0] for m in msgs)

    assert names(got[1]) == ["HK-A"]
    assert names(got[2]) == ["JP-C"]
    assert names(got[3]) == ["HK-A", "HK-B", "JP-C"]
    assert all(m.startswith("🟢 补货提醒") and m.endswith("#补货") for m in got[3])


async def test_failure_alert_and_recovery(db):
    from bot.config import Config
    from bot.fetcher import FetchError, FetchResult

    cfg = Config(bot_token="x", fail_alert_threshold=2)
    sent = []

    async def send(chat_id, text, markup):
        sent.append(text)
        return len(sent)

    class Flaky(FakeFetcher):
        async def get(self, url, retries=2, fresh_session=False, use_proxy=False):
            item = self.pages.pop(0)
            if item is None:
                raise FetchError("timeout")
            return FetchResult(url, 200, item, 0)

    mon = Monitor(cfg, db, Flaky([page(("A", 1)), None, None, None, page(("A", 1))]), send)
    s = await db.get_or_create_site("https://s.example/store/a", "S")
    await db.ensure_chat(1, None)
    await db.attach_site(1, s["id"])
    await db.subscribe(1, s["id"], "all")
    for _ in range(5):
        await mon.check_site(s["id"])
    assert sum("监控异常" in t for t in sent) == 1
    assert sum("监控已恢复" in t for t in sent) == 1


async def test_soldout_edits_its_own_message(db):
    from zoneinfo import ZoneInfo

    from bot.config import Config

    cfg = Config(bot_token="x", notify_cooldown=0, tz=ZoneInfo("UTC"))
    sent, edits = [], []

    async def send(chat_id, text, markup):
        sent.append((chat_id, text))
        return 100 + len(sent)

    async def edit(chat_id, message_id, text, markup):
        edits.append((chat_id, message_id, text))
        return True

    fetcher = FakeFetcher([
        page(("HK-A", 0), ("HK-B", 0)),
        page(("HK-A", 3), ("HK-B", 2)),   # 两个都补货 → 各发一条（101=A，102=B）
        page(("HK-A", 0), ("HK-B", 2)),   # A 售罄 → 只编辑 A 的消息
        page(("HK-A", 0), ("HK-B", 0)),   # B 售罄 → 编辑 B 的消息
        page(("HK-A", 5), ("HK-B", 0)),   # A 再次补货 → 发新消息，旧消息不再改动
    ])
    mon = Monitor(cfg, db, fetcher, send, edit)
    s = await db.get_or_create_site("https://s.example/store/a", "S")
    await db.ensure_chat(1, None)
    await db.set_chat_subscriptions(1, s["id"], True, [], [])

    await mon.check_site(s["id"])
    await mon.check_site(s["id"])
    assert len(sent) == 2 and all(t.startswith("🟢 补货提醒") for _, t in sent)
    assert '🛒 购买：<a href="https://s.example/cart.php?a=add&amp;pid=1">' in sent[0][1]

    await mon.check_site(s["id"])
    assert len(sent) == 2 and len(edits) == 1
    chat_id, message_id, text = edits[0]
    assert (chat_id, message_id) == (1, 101)
    assert text.startswith("🔴 售罄") and "🏷 HK-A" in text
    assert "🛒 购买：<s><a href=" in text and text.endswith("#售罄") and "#补货" not in text

    await mon.check_site(s["id"])
    assert len(edits) == 2 and edits[1][1] == 102 and "🏷 HK-B" in edits[1][2]
    closed = await db.fetchall("SELECT message_id, closed FROM notices ORDER BY message_id")
    assert [(r["message_id"], r["closed"]) for r in closed] == [(101, 1), (102, 1)]

    await mon.check_site(s["id"])
    assert len(sent) == 3 and "🏷 HK-A" in sent[2][1] and len(edits) == 2


async def test_deleted_message_is_closed(db):
    from bot.config import Config

    cfg = Config(bot_token="x", notify_cooldown=0)

    async def send(chat_id, text, markup):
        return 7

    async def edit(chat_id, message_id, text, markup):
        return False  # 用户删除了消息

    mon = Monitor(cfg, db, FakeFetcher([page(("A", 0), ("B", 0)), page(("A", 1), ("B", 1)), page(("A", 0), ("B", 1))]), send, edit)
    s = await db.get_or_create_site("https://s.example/store/a", "S")
    await db.ensure_chat(1, None)
    await db.set_chat_subscriptions(1, s["id"], True, [], [])
    for _ in range(3):
        await mon.check_site(s["id"])
    rows = await db.fetchall(
        "SELECT ni.name, n.closed FROM notices n JOIN notice_items ni ON ni.notice_id = n.id ORDER BY ni.name"
    )
    assert [(r["name"], r["closed"]) for r in rows] == [("A", 1), ("B", 0)]
