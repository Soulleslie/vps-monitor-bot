"""WHMCS 等商店页面的机型/库存解析。

设计思路（按优先级）：
1. WHMCS 官方模板（six / twenty-one 及大多数衍生主题）的商品容器带 id="productN"，
   名称在 #productN-name，库存在 span.qty（"5 Available" / "5 可用"），
   下单按钮 #productN-order-button。
2. 第三方主题（Lagom 等）没有固定 id 时，以“下单链接”为锚点向上查找，
   找到只包含这一个下单链接的最大祖先节点，视为一个商品卡片。
3. 再兜底：class 中含 product / package / plan 的块。

每个商品输出 status：
  in_stock      明确有货（库存数 > 0，或有可用下单按钮且无缺货标记）
  out_of_stock  明确缺货（库存数 = 0、缺货关键字、按钮被禁用）
  unknown       无法判断
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qs, urljoin, urlparse

from bs4 import BeautifulSoup, Tag

IN_STOCK = "in_stock"
OUT_OF_STOCK = "out_of_stock"
UNKNOWN = "unknown"

# 缺货关键字（统一转小写后匹配）
OOS_KEYWORDS = (
    "out of stock",
    "sold out",
    "soldout",
    "no stock",
    "缺货",
    "售罄",
    "售完",
    "已售完",
    "无货",
    "暂无库存",
    "库存不足",
    "补货中",
    "已满",
    "缺貨",
    "售罄中",
)

# "5 Available" / "5 可用" / "5 个可用" / "5 left" / "5 in stock"
_QTY_AFTER = re.compile(
    r"(\d+)\s*(?:个|台|件)?\s*(?:available|可用|可購買|可购买|left|in\s*stock|remaining|有货)",
    re.I,
)
# "库存: 5" / "Stock: 5" / "剩余 5"
_QTY_BEFORE = re.compile(
    r"(?:库存|庫存|stock|剩余|剩餘|余量|qty|quantity)\s*[:：]?\s*(\d+)",
    re.I,
)
_PRICE = re.compile(
    r"(?:[¥￥$€£]|HK\$|US\$|CN¥)\s?\d[\d,]*(?:\.\d+)?(?:\s?(?:USD|CNY|RMB|HKD|EUR|GBP|元))?"
    r"|\d[\d,]*(?:\.\d+)?\s?(?:USD|CNY|RMB|HKD|EUR|GBP|元)",
    re.I,
)
_CYCLE = re.compile(
    r"(monthly|quarterly|semi-annually|annually|biennially|triennially|one\s*time|free"
    r"|月付|季付|半年付|年付|两年付|三年付|一次性|/\s*月|/\s*年|/mo|/yr)",
    re.I,
)
_PRODUCT_ID = re.compile(r"^product(\d+)$")
_WS = re.compile(r"\s+")

# 明显不是商品的导航链接（下单链接识别时排除）
_NON_PRODUCT_PATH = re.compile(r"/(?:cart|login|register|clientarea|knowledgebase|announcements)\b", re.I)


@dataclass
class Product:
    key: str                 # 机型唯一标识：优先 pid，其次 URL slug，最后名称
    name: str
    status: str
    qty: int | None = None
    price: str | None = None
    order_url: str | None = None
    pid: str | None = None
    description: str | None = None
    extra: dict = field(default_factory=dict)


@dataclass
class ParseResult:
    products: list[Product]
    title: str | None
    method: str              # 使用的解析策略，便于排错
    blocked_reason: str | None = None


def _text(node: Tag | None) -> str:
    if node is None:
        return ""
    return _WS.sub(" ", node.get_text(" ", strip=True)).strip()


def normalize_name(name: str) -> str:
    return _WS.sub(" ", name).strip().lower()


def _is_order_href(href: str, page_url: str) -> bool:
    if not href or href.startswith(("#", "javascript:", "mailto:", "tel:")):
        return False
    full = urljoin(page_url, href)
    parsed = urlparse(full)
    qs = parse_qs(parsed.query)
    if "pid" in qs and (qs.get("a", [""])[0] in ("add", "") or "cart.php" in parsed.path):
        return True
    # WHMCS 8 友好链接：/store/<group>/<product-slug>
    page_path = urlparse(page_url).path.rstrip("/")
    path = parsed.path.rstrip("/")
    if _NON_PRODUCT_PATH.search(path):
        return False
    m = re.search(r"/store/([^/]+)/([^/]+)$", path)
    if m and path != page_path and parsed.netloc == urlparse(page_url).netloc:
        return True
    return False


def _extract_pid(href: str | None) -> str | None:
    if not href:
        return None
    qs = parse_qs(urlparse(href).query)
    pid = qs.get("pid", [None])[0]
    if pid and pid.isdigit():
        return pid
    return None


def _slug_from_url(href: str | None) -> str | None:
    if not href:
        return None
    m = re.search(r"/store/[^/]+/([^/?#]+)", urlparse(href).path)
    return m.group(1).lower() if m else None


def _find_order_link(card: Tag, page_url: str) -> Tag | None:
    # 官方模板的按钮 id
    btn = card.find(id=re.compile(r"-order-button$"))
    if isinstance(btn, Tag) and btn.name == "a" and btn.get("href"):
        return btn
    for a in card.find_all("a", href=True):
        if _is_order_href(a["href"], page_url):
            return a
    return None


def _find_name(card: Tag, pid_num: str | None) -> str:
    if pid_num is not None:
        node = card.find(id=f"product{pid_num}-name")
        if node:
            return _text(node)
    for selector in (
        "[id$=-name]",
        ".product-name",
        ".product-title",
        ".package-name",
        ".package-title",
        ".plan-name",
        ".card-title",
        ".title",
        "header",
    ):
        node = card.select_one(selector)
        if node:
            # header 里通常同时包含名称和库存，去掉 .qty 部分
            clone = BeautifulSoup(str(node), "lxml")
            for q in clone.select(".qty, .stock, .badge, .label"):
                q.decompose()
            text = _text(clone)
            if text:
                return text
    for level in ("h1", "h2", "h3", "h4", "h5", "strong", "b"):
        node = card.find(level)
        if node and _text(node):
            return _text(node)
    return ""


def _find_price(card: Tag) -> str | None:
    for selector in ("[id$=-price]", ".product-pricing", ".package-price", ".price", ".pricing"):
        node = card.select_one(selector)
        if node:
            text = _text(node)
            m = _PRICE.search(text)
            if m:
                # 从价格开始保留后面的文字（付款周期、设定费等），如
                # “$49.00 USD 每月一次 $50.00 设定费”，由 meta.normalize_price 统一整理格式
                tail = text[m.start():][:120].strip()
                cyc = _CYCLE.search(text)
                if cyc and cyc.group(0) not in tail:
                    tail = f"{tail} {cyc.group(0)}"
                return tail
    m = _PRICE.search(_text(card))
    return m.group(0).strip() if m else None


def _find_description(card: Tag, pid_num: str | None) -> str | None:
    node = None
    if pid_num is not None:
        node = card.find(id=f"product{pid_num}-description")
    if node is None:
        node = card.select_one(".product-desc, .package-content, .package-body, .description, ul")
    text = _text(node)
    return text[:600] if text else None


def detect_stock(card: Tag, order_link: Tag | None) -> tuple[str, int | None]:
    """返回 (status, qty)。"""
    qty: int | None = None
    qty_node = card.select_one(".qty, .stock, .product-qty, .package-qty, .stock-qty, .inventory")
    candidates = [_text(qty_node)] if qty_node else []
    candidates.append(_text(card))
    for text in candidates:
        m = _QTY_AFTER.search(text) or _QTY_BEFORE.search(text)
        if m:
            qty = int(m.group(1))
            break

    if qty is not None:
        return (IN_STOCK if qty > 0 else OUT_OF_STOCK), qty

    lowered = _text(card).lower()
    if any(k in lowered for k in OOS_KEYWORDS):
        return OUT_OF_STOCK, 0

    # 带缺货语义的 class（如 Lagom 的 .package-out-of-stock / .sold-out）
    classes = " ".join(
        " ".join(el.get("class", [])) for el in [card, *card.find_all(True)]
    ).lower()
    if re.search(r"out-?of-?stock|sold-?out|no-?stock", classes):
        return OUT_OF_STOCK, 0

    if order_link is not None:
        cls = " ".join(order_link.get("class", [])).lower()
        if "disabled" in cls or order_link.has_attr("disabled") or order_link.get("aria-disabled") == "true":
            return OUT_OF_STOCK, 0
        return IN_STOCK, None

    # 没有下单按钮，也没有任何库存信息
    disabled_btn = card.select_one("button[disabled], .btn.disabled")
    if disabled_btn is not None:
        return OUT_OF_STOCK, 0
    return UNKNOWN, None


def _cards_by_id(soup: BeautifulSoup) -> list[tuple[Tag, str | None]]:
    out = []
    for node in soup.find_all(id=_PRODUCT_ID):
        out.append((node, _PRODUCT_ID.match(node["id"]).group(1)))
    return out


def _cards_by_order_links(soup: BeautifulSoup, page_url: str) -> list[tuple[Tag, str | None]]:
    links = [a for a in soup.find_all("a", href=True) if _is_order_href(a["href"], page_url)]
    if not links:
        return []

    def target(a: Tag) -> str:
        return urljoin(page_url, a["href"])

    cards: list[Tag] = []
    seen: set[int] = set()
    for link in links:
        mine = target(link)
        card = link
        node = link.parent
        while isinstance(node, Tag) and node.name not in ("body", "html"):
            targets = {target(a) for a in node.find_all("a", href=True) if _is_order_href(a["href"], page_url)}
            if len(targets) > 1 or mine not in targets:
                break
            card = node
            node = node.parent
        if id(card) not in seen and card is not link:
            seen.add(id(card))
            cards.append(card)
    return [(c, None) for c in cards]


def _cards_by_class(soup: BeautifulSoup) -> list[tuple[Tag, str | None]]:
    pattern = re.compile(r"^(product|package|plan|pricing-item|product-item|product-card)$", re.I)
    nodes = [n for n in soup.find_all(class_=pattern)]
    # 只保留最外层
    top = [n for n in nodes if not any(p in nodes for p in n.parents)]
    return [(n, None) for n in top]


def detect_block(html: str, status_code: int) -> str | None:
    lowered = html[:20000].lower()
    if "cf-chl" in lowered or "just a moment" in lowered or "challenge-platform" in lowered:
        return "被 Cloudflare 人机验证拦截"
    if "ddos-guard" in lowered:
        return "被 DDoS-Guard 拦截"
    if status_code in (403, 429, 503) and len(lowered) < 3000:
        return f"HTTP {status_code}，可能被防火墙或频率限制拦截"
    return None


def parse_store(html: str, page_url: str, status_code: int = 200) -> ParseResult:
    soup = BeautifulSoup(html, "lxml")
    title = _text(soup.title) or None

    method = "whmcs-id"
    cards = _cards_by_id(soup)
    if not cards:
        method = "order-link"
        cards = _cards_by_order_links(soup, page_url)
    if not cards:
        method = "css-class"
        cards = _cards_by_class(soup)

    products: list[Product] = []
    used_keys: set[str] = set()
    for card, pid_num in cards:
        order_link = _find_order_link(card, page_url)
        order_url = urljoin(page_url, order_link["href"]) if order_link is not None else None
        name = _find_name(card, pid_num)
        if not name:
            continue
        status, qty = detect_stock(card, order_link)
        pid = _extract_pid(order_url)
        slug = _slug_from_url(order_url)
        if pid:
            key = f"pid:{pid}"
        elif slug:
            key = f"slug:{slug}"
        else:
            key = f"name:{normalize_name(name)}"
        if key in used_keys:  # 极少数主题会重复渲染（如移动端/桌面端两份）
            continue
        used_keys.add(key)
        products.append(
            Product(
                key=key,
                name=name[:120],
                status=status,
                qty=qty,
                price=_find_price(card),
                order_url=order_url,
                pid=pid,
                description=_find_description(card, pid_num),
            )
        )

    blocked = None
    if not products:
        blocked = detect_block(html, status_code)
    return ParseResult(products=products, title=title, method=method if products else "none", blocked_reason=blocked)


def parse_cart_probe(html: str, final_url: str) -> str:
    """解析 cart.php?a=add&pid=X 的返回页面，用于没有显示库存的站点。"""
    if re.search(r"a=(confproduct|confdomains|view)|/cart\.php\?a=checkout", final_url):
        return IN_STOCK
    lowered = _WS.sub(" ", BeautifulSoup(html, "lxml").get_text(" ")).lower()
    if any(k in lowered for k in OOS_KEYWORDS):
        return OUT_OF_STOCK
    if re.search(r"(configure|配置|billing cycle|付款周期|继续|continue)", lowered):
        return IN_STOCK
    return UNKNOWN


# ---------------------------------------------------------------- 配置页（地区等可选项）
# WHMCS 购买后的配置页（cart.php?a=confproduct）里，可配置项为
#   <label for="inputConfigOption75">Location</label>
#   <select name="configoption[75]"><option value="811">Singapore</option></select>
# 或单选框 <input type="radio" name="configoption[75]" value="811"> Singapore。
# 很多商家在某个地区缺货时，直接把该地区从选项里移除（或标记为售罄/禁用）。
_CONFIG_NAME = re.compile(r"^configoption\[(\d+)\]$")
_LOCATION_LABEL = re.compile(
    r"location|region|data\s*cent(?:er|re)|datacenter|zone|node|"
    r"地区|地區|地域|位置|机房|機房|节点|節點|数据中心|數據中心|区域|區域|线路地区",
    re.I,
)
# 选项文字里附带的价格，如 “Tokyo (+$5.00 USD)”“Tokyo $5.00USD”
_OPTION_PRICE = re.compile(
    r"\s*[\(（\[]?\s*[+＋]?\s*(?:HK\$|US\$|[¥￥$€£])\s*\d[\d,]*(?:\.\d+)?\s*(?:USD|CNY|RMB|HKD|EUR|GBP)?[^)）\]]*[\)）\]]?\s*$",
    re.I,
)


@dataclass
class ConfigOption:
    value: str
    label: str
    available: bool


@dataclass
class ConfigGroup:
    id: str
    label: str
    options: list[ConfigOption]


def is_configure_page(html: str, final_url: str = "") -> bool:
    return "confproduct" in final_url or 'id="frmConfigureProduct"' in html or "configoption[" in html


def _clean_option_label(text: str) -> tuple[str, bool]:
    """去掉选项里的价格和售罄标记，返回 (文字, 是否可购买)。"""
    text = _WS.sub(" ", text).strip()
    lowered = text.lower()
    sold = any(k in lowered for k in OOS_KEYWORDS)
    for k in OOS_KEYWORDS:
        text = re.sub(rf"\s*[\(（\[【-]?\s*{re.escape(k)}\s*[\)）\]】]?", "", text, flags=re.I)
    text = _OPTION_PRICE.sub("", text).strip(" -–—:：")
    return text, not sold


def _group_label(soup: BeautifulSoup, field: Tag) -> str:
    fid = field.get("id")
    if fid:
        lab = soup.find("label", attrs={"for": fid})
        if lab:
            return _text(lab)
    group = field.find_parent(class_=re.compile(r"form-group|option|config", re.I))
    if group:
        lab = group.find("label")
        if lab:
            return _text(lab)
    prev = field.find_previous("label")
    return _text(prev) if prev else ""


def parse_config_groups(html: str) -> list[ConfigGroup]:
    soup = BeautifulSoup(html, "lxml")
    groups: dict[str, ConfigGroup] = {}

    for sel in soup.find_all("select", attrs={"name": _CONFIG_NAME}):
        gid = _CONFIG_NAME.match(sel["name"]).group(1)
        options = []
        for opt in sel.find_all("option"):
            value = (opt.get("value") or "").strip()
            if not value:
                continue
            label, ok = _clean_option_label(opt.get_text(" ", strip=True))
            options.append(ConfigOption(value, label, ok and not opt.has_attr("disabled")))
        groups[gid] = ConfigGroup(gid, _group_label(soup, sel), options)

    for inp in soup.find_all("input", attrs={"type": "radio", "name": _CONFIG_NAME}):
        gid = _CONFIG_NAME.match(inp["name"]).group(1)
        group = groups.setdefault(gid, ConfigGroup(gid, "", []))
        if not group.label:
            # 单选框组的标题是组前面第一个不包含单选框的 <label>
            for lab in inp.find_all_previous("label"):
                if not lab.find("input"):
                    group.label = _text(lab)
                    break
        holder = inp.find_parent("label")
        raw = _text(holder) if holder else _WS.sub(" ", str(inp.next_sibling or "")).strip()
        label, ok = _clean_option_label(raw)
        value = (inp.get("value") or "").strip()
        if value:
            group.options.append(ConfigOption(value, label, ok and not inp.has_attr("disabled")))
    return list(groups.values())


def pick_location_group(groups: list[ConfigGroup], is_location=None) -> ConfigGroup | None:
    """优先按标题（Location / 地区 / 机房…）找；找不到时，选项大多是地名的那一组也算。"""
    for g in groups:
        if _LOCATION_LABEL.search(g.label or ""):
            return g
    if is_location:
        for g in groups:
            if g.options and sum(1 for o in g.options if is_location(o.label)) * 2 >= len(g.options):
                return g
    return None
