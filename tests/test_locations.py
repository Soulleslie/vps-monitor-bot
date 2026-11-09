"""按地区监控：基于 portal.sa.net 的真实页面（tests/fixtures/sa_*.html）。"""
import html as htmllib
import re
from pathlib import Path

import pytest

from bot.config import Config
from bot.db import DB
from bot.fetcher import FetchError, FetchResult
from bot.monitor import Monitor
from bot.parser import IN_STOCK, OUT_OF_STOCK, parse_config_groups, pick_location_group

FIX = Path(__file__).parent / "fixtures"
STORE = (FIX / "sa_store.html").read_text(encoding="utf-8")
CONF_SG = (FIX / "sa_confproduct.html").read_text(encoding="utf-8")
# 东京补货后的配置页：下拉框里多出 Tokyo 选项
CONF_SG_TYO = CONF_SG.replace(
    '</option>\n                                                </select>',
    '</option>\n<option value="812">Tokyo</option>\n                                                </select>',
)
assert "Tokyo</option>" in CONF_SG_TYO
STORE_URL = "https://portal.sa.net/store/cloud-server?language=chinese"


def plain(s: str) -> str:
    return htmllib.unescape(re.sub(r"<[^>]+>", "", s))


class RoutedFetcher:
    """按网址返回不同页面；某个网址的值是列表时依次返回，最后一个重复使用。None 表示请求失败。"""

    def __init__(self, routes):
        self.routes = routes
        self.calls = []

    async def get(self, url, retries=2, fresh_session=False, use_proxy=False):
        self.calls.append(url)
        for prefix, pages in self.routes.items():
            if url.startswith(prefix):
                page = pages.pop(0) if isinstance(pages, list) and len(pages) > 1 else (pages[0] if isinstance(pages, list) else pages)
                if page is None:
                    raise FetchError("timeout")
                final = "https://portal.sa.net/cart.php?a=confproduct&i=0" if "/store/cloud-server/" in url else url
                return FetchResult(final, 200, page, 0.1)
        raise FetchError("unexpected url " + url)

    async def close(self):
        pass


def test_parse_real_configure_page():
    groups = parse_config_groups(CONF_SG_TYO)
    g = pick_location_group(groups)
    assert g.id == "75" and g.label == "Location"
    assert [(o.value, o.label, o.available) for o in g.options] == [("811", "Singapore", True), ("812", "Tokyo", True)]


def test_option_labels_with_price_and_soldout_marks():
    html = """<form id="frmConfigureProduct"><label for="c9">机房</label>
      <select name="configoption[9]" id="c9">
        <option value="1">香港 (+$5.00 USD)</option>
        <option value="2">东京 [售罄]</option>
        <option value="3" disabled>大阪</option>
      </select>
      <label>Location</label>
      <label><input type="radio" name="configoption[10]" value="7"> Los Angeles $2.00USD</label>
      <label><input type="radio" name="configoption[10]" value="8"> San Jose (Sold Out)</label></form>"""
    groups = {g.id: g for g in parse_config_groups(html)}
    assert [(o.label, o.available) for o in groups["9"].options] == [("香港", True), ("东京", False), ("大阪", False)]
    assert [(o.label, o.available) for o in groups["10"].options] == [("Los Angeles", True), ("San Jose", False)]
    assert groups["10"].label == "Location"


@pytest.fixture
async def db():
    d = DB(":memory:")
    await d.open()
    yield d
    await d.close()


async def test_tokyo_restock_end_to_end(db):
    premium_conf = [CONF_SG, CONF_SG_TYO, None, CONF_SG]
    fetcher = RoutedFetcher({
        STORE_URL: STORE,
        "https://portal.sa.net/store/cloud-server/premium": premium_conf,
        "https://portal.sa.net/store/cloud-server/": CONF_SG,   # Ultra / Max 一直只有新加坡
    })
    sent, edits = [], []

    async def send(chat_id, text, markup):
        sent.append((chat_id, text))
        return 1000 + len(sent)

    async def edit(chat_id, message_id, text, markup):
        edits.append((chat_id, message_id, text))
        return True

    mon = Monitor(Config(bot_token="x", notify_cooldown=0), db, fetcher, send, edit)
    site = await mon.add_site(STORE_URL, "portal.sa.net/cloud-server")
    sid = site["id"]
    await db.set_meta("sites", sid, {"expand": "1", "merchant": "SA"})
    for chat in (1, 2):
        await db.ensure_chat(chat, None)
    await db.set_chat_subscriptions(1, sid, False, ["slug:premium"], [])   # 订阅了 Premium VPS 整个机型
    await db.set_chat_subscriptions(2, sid, False, [], ["东京"])           # 用中文关键词订阅东京

    # 1) 首次拆分地区：只记录，不推送
    out = await mon.check_site(sid)
    assert out.ok and sent == []
    names = {r["name"]: r["status"] for r in await db.site_products(sid)}
    assert names["Premium VPS · Singapore"] == IN_STOCK and "Premium VPS · Tokyo" not in names

    # 2) 东京出现在下拉框 → 补货推送给两个对象；新加坡、机型整体都不推送
    out = await mon.check_site(sid)
    assert out.ok and len(sent) == 2 and {c for c, _ in sent} == {1, 2}
    msg = plain(sent[0][1])
    assert msg.startswith("🟢 补货提醒")
    assert "🏷 SA-Premium VPS · Tokyo" in msg
    assert "💰 $49.00 USD / month + $50.00 设定费" in msg
    assert "📍 🇯🇵 日本 · 东京\n" in msg                       # 只显示东京，不是“东京 / 新加坡”
    assert "🌐 CTGNET/CN2GIA/联通精品/CMIN2" in msg
    assert "💻 2 Core / 4GB / 40GB / 1TB@1Gbps" in msg
    assert "🛒 购买：https://portal.sa.net/store/cloud-server/premium?configoption[75]=812" in msg
    assert msg.endswith("#sa #JP #TYO #CTGNET #CN2GIA #联通精品 #CMIN2 #优化线路 #补货")

    # 3) 配置页读取失败 → 保持原状态，不能误报售罄
    out = await mon.check_site(sid)
    assert out.ok and len(sent) == 2 and edits == []
    tokyo = next(r for r in await db.site_products(sid) if r["name"] == "Premium VPS · Tokyo")
    assert tokyo["status"] == IN_STOCK and tokyo["present"] == 1

    # 4) 东京再次从下拉框消失 → 原来两条补货消息改为售罄
    out = await mon.check_site(sid)
    assert out.ok and len(sent) == 2
    assert sorted(m for _, m, _ in edits) == [1001, 1002]
    sold = plain(edits[0][2])
    assert sold.startswith("🔴 售罄") and "Premium VPS · Tokyo" in sold and sold.endswith("#售罄")
    tokyo = next(r for r in await db.site_products(sid) if r["name"] == "Premium VPS · Tokyo")
    assert tokyo["status"] == OUT_OF_STOCK


async def test_disabled_expansion_does_not_open_configure_pages(db):
    fetcher = RoutedFetcher({STORE_URL: STORE, "https://portal.sa.net/store/cloud-server/": CONF_SG})

    async def send(*a):
        return 1

    mon = Monitor(Config(bot_token="x"), db, fetcher, send)
    site = await mon.add_site(STORE_URL, "x")
    await db.execute("UPDATE sites SET probe_mode = 'off' WHERE id = ?", (site["id"],))
    await db.ensure_chat(1, None)
    await db.set_chat_subscriptions(1, site["id"], True, [], [])
    await mon.check_site(site["id"])
    assert all(c == STORE_URL for c in fetcher.calls)
    assert not any("#loc:" in r["key"] for r in await db.site_products(site["id"]))


async def test_site_name_auto_custom_and_rename_on_readd(db):
    from bot.parser import parse_store

    assert parse_store(STORE, STORE_URL).suggested_name == "Riven Cloud · Cloud Server"
    fetcher = RoutedFetcher({STORE_URL: STORE, "https://portal.sa.net/other": STORE.replace("<title>购物车 - Riven Cloud</title>", "")})

    async def send(*a):
        return 1

    mon = Monitor(Config(bot_token="x"), db, fetcher, send)
    # 留空：从页面自动识别
    assert (await mon.add_site(STORE_URL))["name"] == "Riven Cloud · Cloud Server"
    # 已存在的网址再次添加并填写名称：更新为新名称
    assert (await mon.add_site(STORE_URL, "  我的 SA 东京  "))["name"] == "我的 SA 东京"
    # 留空再次添加：保留已有名称
    assert (await mon.add_site(STORE_URL, ""))["name"] == "我的 SA 东京"
    # 页面没有 <title> 时只用大标题
    assert (await mon.add_site("https://portal.sa.net/other"))["name"] == "Cloud Server"


async def test_change_site_url_keeps_settings_and_is_silent(db):
    other_page = (FIX / "twentyone_zh.html").read_text(encoding="utf-8")   # 机型完全不同的页面
    new_url = "https://portal.sa.net/store/cloud-server?language=english"
    fetcher = RoutedFetcher({
        new_url: STORE,
        STORE_URL: STORE,
        "https://portal.sa.net/store/other": other_page,
        "https://portal.sa.net/store/broken": None,
    })
    sent = []

    async def send(chat_id, text, markup):
        sent.append(text)
        return len(sent)

    mon = Monitor(Config(bot_token="x", notify_cooldown=0), db, fetcher, send)
    site = await mon.add_site(STORE_URL, "我的 SA")
    sid = site["id"]
    await db.ensure_chat(1, None)
    await db.set_chat_subscriptions(1, sid, False, ["slug:premium"], ["东京"])
    await db.set_meta("sites", sid, {"merchant": "SA", "expand": ""})
    await db.execute("UPDATE sites SET interval_sec = 45 WHERE id = ?", (sid,))
    events_before = len(await db.site_events(sid, 100))

    # 1) 读取失败：不做任何改动
    with pytest.raises(FetchError):
        await mon.change_site_url(sid, "https://portal.sa.net/store/broken")
    assert (await db.get_site(sid))["url"] == STORE_URL

    # 2) 与其他已监控的网址重复：拒绝
    other = await mon.add_site("https://portal.sa.net/store/other", "另一个")
    with pytest.raises(ValueError, match="已经在监控列表中"):
        await mon.change_site_url(sid, "https://portal.sa.net/store/other")

    # 3) 正常修改：名称、订阅、显示设置、检查间隔都保留
    assert await mon.change_site_url(sid, new_url) == 3
    s = await db.get_site(sid)
    assert s["url"] == new_url and s["name"] == "我的 SA" and s["interval_sec"] == 45
    assert '"merchant": "SA"' in s["meta"]
    subs = await db.chat_subscriptions(1, sid)
    assert {(x["kind"], x["value"]) for x in subs} == {("product", "slug:premium"), ("keyword", "东京")}

    # 4) 换成机型完全不同的页面：切换时和之后的检查都不产生误报
    await db.delete_site(other["id"])
    assert await mon.change_site_url(sid, "https://portal.sa.net/store/other") == 3
    names = {r["name"]: r["present"] for r in await db.site_products(sid)}
    assert names["Premium VPS"] == 0 and names["HK-Pro 2C2G"] == 1
    out = await mon.check_site(sid)
    assert out.ok and out.events == 0
    assert sent == [] and len(await db.site_events(sid, 100)) == events_before


async def test_all_models_are_split_even_if_only_one_is_subscribed(db):
    fetcher = RoutedFetcher({STORE_URL: STORE, "https://portal.sa.net/store/cloud-server/": CONF_SG})

    async def send(*a):
        return 1

    mon = Monitor(Config(bot_token="x"), db, fetcher, send)
    site = await mon.add_site(STORE_URL)
    await db.set_meta("sites", site["id"], {"expand": "1"})
    await db.ensure_chat(1, None)
    await db.set_chat_subscriptions(1, site["id"], False, ["slug:premium"], [])   # 只订阅了 Premium
    assert (await mon.check_now(site["id"])).ok
    names = sorted(r["name"] for r in await db.site_products(site["id"]) if "#loc:" in r["key"])
    assert names == ["Max VPS · Singapore", "Premium VPS · Singapore", "Ultra VPS · Singapore"]
    # 订阅的机型最先读取
    conf_calls = [c for c in fetcher.calls if c != STORE_URL]
    assert conf_calls[0].endswith("/premium")


@pytest.mark.parametrize("keyword,name,expected", [
    ("东京", "JP-Tokyo 2C2G", True),             # 中文关键词 → 英文名称
    ("Tokyo", "东京 CN2 VPS", True),             # 英文关键词 → 中文名称
    ("TYO", "Premium VPS · Tokyo", True),        # 地区代码
    ("日本", "Premium VPS · Tokyo", True),       # 国家包含城市
    ("Japan", "大阪 VPS", True),
    ("香港", "HK-Pro 2C2G", True),
    ("premium 东京", "Premium VPS · Tokyo", True),
    ("东京", "Premium VPS · Singapore", False),
    ("东京", "大阪 VPS", False),
    ("大阪", "日本 VPS", False),                  # 城市不能匹配只写了国家的名称
    ("英国", "40 GB SSD", False),
    ("Ultra 东京", "Premium VPS · Tokyo", False),
])
def test_keyword_location_bilingual(keyword, name, expected):
    from bot.monitor import keyword_match

    assert keyword_match(keyword, name) is expected


async def test_english_keyword_matches_tokyo_variant(db):
    fetcher = RoutedFetcher({STORE_URL: STORE, "https://portal.sa.net/store/cloud-server/premium": [CONF_SG, CONF_SG_TYO],
                             "https://portal.sa.net/store/cloud-server/": CONF_SG})
    sent = []

    async def send(chat_id, text, markup):
        sent.append(chat_id)
        return len(sent)

    mon = Monitor(Config(bot_token="x", notify_cooldown=0), db, fetcher, send)
    sid = (await mon.add_site(STORE_URL))["id"]
    await db.set_meta("sites", sid, {"expand": "1"})
    for chat, kw in ((1, "Tokyo"), (2, "日本"), (3, "新加坡")):
        await db.ensure_chat(chat, None)
        await db.set_chat_subscriptions(chat, sid, False, [], [kw])
    await mon.check_site(sid)
    await mon.check_site(sid)          # 东京出现
    assert sorted(sent) == [1, 2]
