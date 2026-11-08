import html
import re

from bot.messages import render_card
from bot.meta import apply_aff, build_card, detect_lines, detect_location, detect_specs, normalize_price


def plain(html_text: str) -> str:
    """Telegram HTML → 用户看到的纯文本（去掉标签）。"""
    return html.unescape(re.sub(r"<[^>]+>", "", html_text))


ANDNODE_SITE = {"url": "https://cloud.andnode.com/store/us-cloud", "name": "cloud.andnode.com/us-cloud"}
ANDNODE_META = {"merchant": "AndNode", "prefix": "AndNode-美国云", "coupon": "andnode（八折）"}
ANDNODE_PRODUCT = {
    "name": "标准型",
    "price": "¥18.00CNY 月付",
    "description": "洛杉矶 9929 + CMIN2 优化线路 · 2 核 CPU · 2GB 内存 · 30GB SSD · 无限流量 · 30Mbps 带宽",
    "order_url": "https://cloud.andnode.com/cart?action=configureproduct&pid=11",
}

VPS_SITE = {"url": "https://vps.hosting/cart/frankfurt/", "name": "vps.hosting/frankfurt"}
VPS_META = {"merchant": "V.PS"}
VPS_PRODUCT = {
    "name": "Frankfurt Starter",
    "price": "€6.95 EUR Monthly",
    "description": "2 vCPU / 1 GB RAM / 20 GB NVMe / 1 TB Traffic · CN2 GIA + 9929 + CMIN2",
    "order_url": "https://vps.hosting/cart.php?a=add&pid=10",
}


def test_example_restock_matches_user_format():
    card = build_card(ANDNODE_SITE, ANDNODE_META, ANDNODE_PRODUCT, {})
    assert plain(render_card(card, "restock")) == """🟢 补货提醒

🏷 AndNode-美国云-标准型
💰 ¥18.00 CNY / month
📍 🇺🇸 美国 · 洛杉矶
🌐 9929/CMIN2
🎟 andnode（八折）

💻 2 Core / 2GB / 30GB / 无限流量@30Mbps

🛒 购买：https://cloud.andnode.com/cart?action=configureproduct&pid=11

#andnode #US #LAX #9929 #CMIN2 #优化线路 #补货"""


def test_example_soldout_matches_user_format_with_strikethrough_link():
    card = build_card(VPS_SITE, VPS_META, VPS_PRODUCT, {"buy_url": "https://vps.hosting/?affid=2421&oid=10"})
    out = render_card(card, "soldout")
    assert plain(out) == """🔴 售罄

🏷 V.PS-Frankfurt Starter
💰 €6.95 EUR / month
📍 🇩🇪 德国 · 法兰克福
🌐 CN2GIA/9929/CMIN2

💻 2 Core / 1GB / 20GB / 1TB

🛒 购买：https://vps.hosting/?affid=2421&oid=10

#vps #DE #FRA #CN2GIA #9929 #CMIN2 #优化线路 #售罄"""
    # 售罄时链接带删除线
    assert '🛒 购买：<s><a href="https://vps.hosting/?affid=2421&amp;oid=10">' in out


def test_manual_overrides():
    card = build_card(ANDNODE_SITE, ANDNODE_META, ANDNODE_PRODUCT, {
        "name": "AndNode-美国云-旗舰型", "location": "SJC", "lines": "CN2GIA", "specs": "4 Core / 8GB",
        "coupon": "-", "tags": "#限量 年付特惠",
    })
    text = plain(render_card(card, "restock"))
    assert "🏷 AndNode-美国云-旗舰型\n" in text            # 已带前缀时不重复
    assert "📍 🇺🇸 美国 · 圣何塞" in text and "🌐 CN2GIA" in text and "💻 4 Core / 8GB" in text
    assert "🎟" not in text                                 # “-” 表示不显示
    assert text.endswith("#andnode #US #SJC #CN2GIA #优化线路 #限量 #年付特惠 #补货")
    hidden = build_card(ANDNODE_SITE, ANDNODE_META, ANDNODE_PRODUCT, {"location": "none", "lines": "-", "specs": "-"})
    t2 = plain(render_card(hidden, "restock"))
    assert "📍" not in t2 and "🌐" not in t2 and "💻" not in t2 and "#优化线路" not in t2


def test_aff_param_appended_to_order_url():
    card = build_card(ANDNODE_SITE, {**ANDNODE_META, "aff": "aff=123"}, ANDNODE_PRODUCT, {})
    assert card.buy_url == "https://cloud.andnode.com/cart?action=configureproduct&pid=11&aff=123"
    assert apply_aff("https://x.com/cart.php?a=add&pid=1&aff=9", "aff=5") == "https://x.com/cart.php?a=add&pid=1&aff=5"


def test_minimal_card_without_detectable_info():
    card = build_card({"url": "https://my.example-host.com/store/vps", "name": "x"}, {},
                      {"name": "Plan A", "price": None, "description": "", "order_url": None}, {})
    assert plain(render_card(card, "new")) == "🆕 上新提醒\n\n🏷 Plan A\n\n#examplehost #上新"


def test_detectors():
    assert detect_location("HK-Pro 2C2G").code == "HKG"
    assert detect_location("Tokyo Lite").code == "TYO"
    assert detect_location("Contact us for USD pricing") is None   # 小写 us / USD 不能误判为美国
    assert detect_location("日本 大阪 BGP").code == "OSA"           # 同一段文本里城市优先于国家
    assert detect_lines("CN2-GIA 回程", "AS9929") == ["CN2GIA", "9929"]
    assert detect_lines("19929 端口") == []
    assert detect_specs("HK-Pro 2C2G", "40G SSD 500G 流量") == "2 Core / 2GB / 40GB / 500GB"
    assert detect_specs("1 vCore 512MB RAM 10GB NVMe Unlimited 1Gbps") == "1 Core / 512MB / 10GB / 无限流量@1Gbps"
    assert detect_specs("只有名称") is None
    assert normalize_price("$4.99 USD Monthly") == "$4.99 USD / month"
    assert normalize_price("¥120.00CNY 年付") == "¥120.00 CNY / year"
    assert normalize_price("HK$30.00 季付") == "HK$30.00 HKD / quarter"
    assert normalize_price("免费") == "免费"
