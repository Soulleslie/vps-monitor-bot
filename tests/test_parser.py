from pathlib import Path

from bot.parser import IN_STOCK, OUT_OF_STOCK, parse_cart_probe, parse_store

FIX = Path(__file__).parent / "fixtures"


def load(name):
    return (FIX / name).read_text(encoding="utf-8")


def by_name(result):
    return {p.name: p for p in result.products}


def test_whmcs_twentyone_chinese():
    r = parse_store(load("twentyone_zh.html"), "https://portal.example.net/store/cloud-server?language=chinese")
    assert r.method == "whmcs-id"
    p = by_name(r)
    assert set(p) == {"HK-Basic 1C1G", "HK-Pro 2C2G", "JP-Lite"}
    assert p["HK-Basic 1C1G"].status == OUT_OF_STOCK and p["HK-Basic 1C1G"].qty == 0
    assert p["HK-Pro 2C2G"].status == IN_STOCK and p["HK-Pro 2C2G"].qty == 5
    assert p["HK-Pro 2C2G"].key == "pid:12"
    assert p["HK-Pro 2C2G"].order_url == "https://portal.example.net/cart.php?a=add&pid=12&language=chinese"
    assert p["HK-Pro 2C2G"].price.startswith("¥60.00")
    # 没有库存数量、有下单按钮 → 视为有货（qty 未知）
    assert p["JP-Lite"].status == IN_STOCK and p["JP-Lite"].qty is None
    assert p["JP-Lite"].price.startswith("$4.99 USD")


def test_whmcs_six_english_without_name_id():
    r = parse_store(load("six_en.html"), "https://novix.example.com/store/nttispipvps")
    p = by_name(r)
    assert p["NTT-Mini"].status == OUT_OF_STOCK
    assert p["NTT-Plus"].status == IN_STOCK and p["NTT-Plus"].qty == 12
    assert p["NTT-Plus"].order_url == "https://novix.example.com/store/cart.php?a=add&pid=6"


def test_third_party_theme_by_order_links():
    r = parse_store(load("lagom_like.html"), "https://shop.example.com/store/vps")
    assert r.method == "order-link"
    p = by_name(r)
    assert set(p) == {"US-1G", "US-2G", "US-4G"}
    assert p["US-1G"].status == IN_STOCK and p["US-1G"].key == "slug:us-1g"
    assert p["US-2G"].status == OUT_OF_STOCK  # class 标记
    assert p["US-4G"].status == OUT_OF_STOCK  # Sold Out 徽章
    assert p["US-1G"].price.startswith("$2.50 USD")


def test_cloudflare_detected():
    r = parse_store(load("cloudflare.html"), "https://x.example.com/store/a", 403)
    assert r.products == []
    assert "Cloudflare" in r.blocked_reason


def test_cart_probe():
    assert parse_cart_probe("<p>Out of Stock</p>", "https://x/cart.php?a=add&pid=1") == OUT_OF_STOCK
    assert parse_cart_probe("<p>缺货</p>", "https://x/cart.php?a=add&pid=1") == OUT_OF_STOCK
    assert parse_cart_probe("", "https://x/cart.php?a=confproduct&i=0") == IN_STOCK
