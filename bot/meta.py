"""从机型名称/描述中识别地区、线路、配置，整理价格，并生成推送卡片。

识别结果都可以在 Web 后台按网址、按机型手动覆盖。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

# ---------------------------------------------------------------- 地区
# (代码, 国旗, 国家/地区, 城市, 国家标签, 关键词)。城市在前、国家兜底在后；
# 中文关键词按子串匹配，英文按单词匹配（不区分大小写），全大写代码按单词匹配且区分大小写。
_LOC_TABLE: list[tuple[str, str, str, str, str, tuple[str, ...]]] = [
    ("LAX", "🇺🇸", "美国", "洛杉矶", "US", ("洛杉矶", "洛杉磯", "Los Angeles", "LAX")),
    ("SJC", "🇺🇸", "美国", "圣何塞", "US", ("圣何塞", "聖荷西", "San Jose", "SJC", "硅谷", "Silicon Valley")),
    ("FMT", "🇺🇸", "美国", "弗里蒙特", "US", ("弗里蒙特", "Fremont")),
    ("SEA", "🇺🇸", "美国", "西雅图", "US", ("西雅图", "Seattle", "SEA")),
    ("NYC", "🇺🇸", "美国", "纽约", "US", ("纽约", "New York", "NYC")),
    ("DAL", "🇺🇸", "美国", "达拉斯", "US", ("达拉斯", "Dallas", "DFW")),
    ("CHI", "🇺🇸", "美国", "芝加哥", "US", ("芝加哥", "Chicago", "ORD")),
    ("MIA", "🇺🇸", "美国", "迈阿密", "US", ("迈阿密", "Miami", "MIA")),
    ("PHX", "🇺🇸", "美国", "凤凰城", "US", ("凤凰城", "Phoenix", "PHX")),
    ("LAS", "🇺🇸", "美国", "拉斯维加斯", "US", ("拉斯维加斯", "Las Vegas")),
    ("ATL", "🇺🇸", "美国", "亚特兰大", "US", ("亚特兰大", "Atlanta", "ATL")),
    ("ASH", "🇺🇸", "美国", "阿什本", "US", ("阿什本", "Ashburn")),
    ("KC", "🇺🇸", "美国", "堪萨斯", "US", ("堪萨斯", "Kansas City")),
    ("BUF", "🇺🇸", "美国", "水牛城", "US", ("水牛城", "Buffalo")),
    ("YYZ", "🇨🇦", "加拿大", "多伦多", "CA", ("多伦多", "Toronto", "YYZ")),
    ("YUL", "🇨🇦", "加拿大", "蒙特利尔", "CA", ("蒙特利尔", "Montreal", "YUL")),
    ("FRA", "🇩🇪", "德国", "法兰克福", "DE", ("法兰克福", "Frankfurt", "FRA")),
    ("DUS", "🇩🇪", "德国", "杜塞尔多夫", "DE", ("杜塞尔多夫", "Dusseldorf", "Düsseldorf", "DUS")),
    ("NUE", "🇩🇪", "德国", "纽伦堡", "DE", ("纽伦堡", "Nuremberg", "Nürnberg")),
    ("AMS", "🇳🇱", "荷兰", "阿姆斯特丹", "NL", ("阿姆斯特丹", "Amsterdam", "AMS")),
    ("LON", "🇬🇧", "英国", "伦敦", "UK", ("伦敦", "London", "LON", "LHR")),
    ("PAR", "🇫🇷", "法国", "巴黎", "FR", ("巴黎", "Paris", "PAR", "CDG")),
    ("WAW", "🇵🇱", "波兰", "华沙", "PL", ("华沙", "Warsaw", "WAW")),
    ("MOW", "🇷🇺", "俄罗斯", "莫斯科", "RU", ("莫斯科", "Moscow", "MOW")),
    ("TYO", "🇯🇵", "日本", "东京", "JP", ("东京", "東京", "Tokyo", "TYO", "NRT", "HND")),
    ("OSA", "🇯🇵", "日本", "大阪", "JP", ("大阪", "Osaka", "OSA", "KIX")),
    ("HKG", "🇭🇰", "中国香港", "香港", "HK", ("香港", "Hong Kong", "HongKong", "HKG", "HK")),
    ("MFM", "🇲🇴", "中国澳门", "澳门", "MO", ("澳门", "澳門", "Macau", "Macao", "MFM")),
    ("TPE", "🇹🇼", "中国台湾", "台北", "TW", ("台北", "臺北", "Taipei", "TPE")),
    ("SIN", "🇸🇬", "新加坡", "新加坡", "SG", ("新加坡", "Singapore", "SIN", "SG")),
    ("SEL", "🇰🇷", "韩国", "首尔", "KR", ("首尔", "首爾", "Seoul", "SEL", "ICN")),
    ("CHC", "🇰🇷", "韩国", "春川", "KR", ("春川", "Chuncheon")),
    ("SYD", "🇦🇺", "澳大利亚", "悉尼", "AU", ("悉尼", "Sydney", "SYD")),
    ("BOM", "🇮🇳", "印度", "孟买", "IN", ("孟买", "Mumbai", "BOM")),
    ("JNB", "🇿🇦", "南非", "约翰内斯堡", "ZA", ("约翰内斯堡", "Johannesburg", "JNB")),
    ("SAO", "🇧🇷", "巴西", "圣保罗", "BR", ("圣保罗", "Sao Paulo", "São Paulo", "GRU")),
    ("DXB", "🇦🇪", "阿联酋", "迪拜", "AE", ("迪拜", "Dubai", "DXB")),
    # 只有国家/地区、没有城市
    ("US", "🇺🇸", "美国", "", "US", ("美国", "美國", "United States", "USA", "US")),
    ("JP", "🇯🇵", "日本", "", "JP", ("日本", "Japan", "JP")),
    ("KR", "🇰🇷", "韩国", "", "KR", ("韩国", "韓國", "Korea", "KR")),
    ("TW", "🇹🇼", "中国台湾", "", "TW", ("台湾", "臺灣", "Taiwan", "TW")),
    ("DE", "🇩🇪", "德国", "", "DE", ("德国", "Germany", "DE")),
    # 注意：不能把 “GB” 作为英国的关键词，否则 “40 GB SSD” 会被识别成英国
    ("UK", "🇬🇧", "英国", "", "UK", ("英国", "United Kingdom", "UK")),
    ("NL", "🇳🇱", "荷兰", "", "NL", ("荷兰", "Netherlands", "NL")),
    ("FR", "🇫🇷", "法国", "", "FR", ("法国", "France", "FR")),
    ("CA", "🇨🇦", "加拿大", "", "CA", ("加拿大", "Canada")),
    ("RU", "🇷🇺", "俄罗斯", "", "RU", ("俄罗斯", "Russia", "RU")),
    ("AU", "🇦🇺", "澳大利亚", "", "AU", ("澳大利亚", "澳洲", "Australia")),
    ("VN", "🇻🇳", "越南", "", "VN", ("越南", "Vietnam")),
    ("TH", "🇹🇭", "泰国", "", "TH", ("泰国", "Thailand")),
    ("MY", "🇲🇾", "马来西亚", "", "MY", ("马来西亚", "Malaysia")),
    ("PH", "🇵🇭", "菲律宾", "", "PH", ("菲律宾", "Philippines")),
    ("ID", "🇮🇩", "印度尼西亚", "", "ID", ("印尼", "印度尼西亚", "Indonesia")),
    ("TR", "🇹🇷", "土耳其", "", "TR", ("土耳其", "Turkey", "Türkiye")),
]


@dataclass(frozen=True)
class Location:
    code: str
    flag: str
    country: str
    city: str
    cc: str

    @property
    def text(self) -> str:
        return f"{self.flag} {self.country}" + (f" · {self.city}" if self.city and self.city != self.country else "")

    @property
    def tags(self) -> list[str]:
        return [self.cc] + ([self.code] if self.city and self.code != self.cc else [])


LOCATIONS = {row[0]: Location(*row[:5]) for row in _LOC_TABLE}


def _kw_regex(kw: str) -> re.Pattern:
    if re.search(r"[一-鿿]", kw):
        return re.compile(re.escape(kw))
    if kw.isupper() and len(kw) <= 4:  # 代码：区分大小写，前后不能紧挨字母
        return re.compile(rf"(?<![A-Za-z]){re.escape(kw)}(?![A-Za-z])")
    return re.compile(rf"(?<![A-Za-z]){re.escape(kw)}(?![A-Za-z])", re.I)


_LOC_PATTERNS = [(row[0], [_kw_regex(k) for k in row[5]]) for row in _LOC_TABLE]


def detect_locations(*texts: str, limit: int = 4) -> list[Location]:
    """找出文本中提到的所有地区（如“Tokyo or Singapore”→ 东京、新加坡），按出现顺序排列。

    靠前的文本（如机型名称）优先：只使用第一段包含地区信息的文本。
    同一国家已识别到城市时，不再重复列出国家本身。
    """
    for text in texts:
        if not text:
            continue
        hits: list[tuple[int, Location]] = []
        for code, patterns in _LOC_PATTERNS:
            positions = [m.start() for p in patterns for m in [p.search(text)] if m]
            if positions:
                hits.append((min(positions), LOCATIONS[code]))
        found: list[Location] = []
        for _, loc in sorted(hits, key=lambda x: x[0]):
            if loc in found or (not loc.city and any(f.cc == loc.cc for f in found)):
                continue
            if loc.city and any(f.cc == loc.cc and not f.city for f in found):
                found = [f for f in found if not (f.cc == loc.cc and not f.city)]
            found.append(loc)
        if found:
            return found[:limit]
    return []


def _alias_map() -> dict[str, list[Location]]:
    """关键词 → 地区：包含每个地区的全部中英文别名、代码，以及中文国家名、城市名（不区分大小写）。"""
    out: dict[str, list[Location]] = {}
    for row in _LOC_TABLE:
        loc = LOCATIONS[row[0]]
        for alias in (*row[5], row[2], row[3], row[0]):
            if alias:
                bucket = out.setdefault(alias.lower(), [])
                if loc not in bucket:
                    bucket.append(loc)
    return out


_ALIASES = _alias_map()


def term_locations(term: str) -> list[Location]:
    """如果关键词是一个地区名（如“东京”“Tokyo”“TYO”“日本”），返回对应地区；否则返回空列表。"""
    return _ALIASES.get(term.strip().lower(), [])


def location_term_matches(term: str, text: str) -> bool:
    """地区关键词按地区匹配，中英文通用：
    “东京”能匹配“Tokyo”，“Tokyo”能匹配“东京”；国家名（如“日本”）匹配该国家的所有城市。"""
    wanted = term_locations(term)
    if not wanted:
        return False
    found = detect_locations(text, limit=16)
    return any(f == w or (not w.city and f.cc == w.cc) for w in wanted for f in found)


def detect_location(*texts: str) -> Location | None:
    found = detect_locations(*texts)
    return found[0] if found else None


# ---------------------------------------------------------------- 线路
# 按 电信 → 联通 → 移动 的顺序排列，输出时也保持这个顺序
_LINES: list[tuple[str, re.Pattern]] = [
    ("CTGNET", re.compile(r"CTG\s*NET|(?<!\d)23764(?!\d)", re.I)),
    ("CN2GIA", re.compile(r"CN2\s*[-_ ]?\s*GIA|(?<![A-Za-z])GIA(?![A-Za-z])", re.I)),
    ("CN2GT", re.compile(r"CN2\s*[-_ ]?\s*GT(?![A-Za-z])", re.I)),
    ("9929", re.compile(r"(?<!\d)9929(?!\d)")),
    ("10099", re.compile(r"(?<!\d)10099(?!\d)")),
    ("4837", re.compile(r"(?<!\d)4837(?!\d)")),
    ("联通精品", re.compile(r"(?:China\s*)?Unicom\s*Premium|CU\s*Premium|联通精品|聯通精品", re.I)),
    ("CUII", re.compile(r"CUII", re.I)),
    ("CMIN2", re.compile(r"CMIN2", re.I)),
    ("CMI", re.compile(r"(?<![A-Za-z])CMI(?![A-Za-z0-9])", re.I)),
    ("BGP", re.compile(r"(?<![A-Za-z])BGP(?![A-Za-z])", re.I)),
]
OPTIMIZED_LINES = {"CTGNET", "CN2GIA", "CN2GT", "9929", "10099", "4837", "联通精品", "CUII", "CMIN2"}


def detect_lines(*texts: str) -> list[str]:
    joined = " ".join(t for t in texts if t)
    return [name for name, pat in _LINES if pat.search(joined)]


# ---------------------------------------------------------------- 配置
def _size(num: str, unit: str) -> str:
    unit = unit.upper().rstrip("B")
    unit = {"G": "GB", "M": "MB", "T": "TB"}.get(unit, unit + "B")
    if unit == "MB" and num.isdigit() and int(num) >= 1024 and int(num) % 1024 == 0:
        num, unit = str(int(num) // 1024), "GB"   # 4096 MB → 4GB
    n = num.rstrip("0").rstrip(".") if "." in num else num
    return f"{n}{unit}"


def detect_specs(*texts: str) -> str | None:
    """生成形如 “2 Core / 2GB / 30GB / 无限流量@30Mbps” 的配置串；识别不到的项省略。"""
    text = " ".join(t for t in texts if t)
    if not text:
        return None
    cpu = ram = disk = traffic = speed = None

    m = re.search(r"(?<![\d.])(\d+)\s*C\s*(\d+(?:\.\d+)?)\s*G(?![a-z])", text, re.I)  # 2C2G
    if m:
        cpu, ram = m.group(1), _size(m.group(2), "G")
    m = re.search(r"(\d+)\s*(?:x\s*)?(?:v?CPU|vCores?|Cores?|核心|核|個核心)", text, re.I) or \
        re.search(r"(\d+)\s*[x×]\s*(?:AMD|Intel|Ryzen|EPYC|Xeon|Core\s*i\d)", text, re.I) or \
        re.search(r"(?:CPU|核心)\s*[:：]?\s*(\d+)", text, re.I)
    if m:
        cpu = m.group(1)
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|G|MB|M)\s*(?:DDR\d\s*)?(?:RAM|内存|記憶體|Memory)", text, re.I) or \
        re.search(r"(?:RAM|内存|記憶體|Memory)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(GB|G|MB|M)", text, re.I)
    if m:
        ram = _size(m.group(1), m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(GB|G|TB|T)\s*(?:NVMe|SSD|HDD|硬盘|硬碟|磁盘|Disk|存储|Storage)", text, re.I) or \
        re.search(r"(?:NVMe|SSD|HDD|硬盘|硬碟|磁盘|Disk|存储|Storage)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(GB|G|TB|T)", text, re.I)
    if m:
        disk = _size(m.group(1), m.group(2))
    if re.search(r"无限流量|不限流量|流量\s*[:：]?\s*(?:无限|不限)|Unlimited\s*(?:Traffic|Bandwidth|Transfer)?", text, re.I):
        traffic = "无限流量"
    else:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(TB|T|GB|G)\s*(?:/\s*(?:月|mo|month))?\s*(?:流量|Traffic|Transfer|Bandwidth|月流量)", text, re.I) or \
            re.search(r"(?:流量|Traffic|Transfer)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(TB|T|GB|G)", text, re.I)
        if m:
            traffic = _size(m.group(1), m.group(2))
    m = re.search(r"(\d+(?:\.\d+)?)\s*(Mbps|Gbps|M\s*带宽|G\s*带宽|M\s*端口|G\s*端口)", text, re.I) or \
        re.search(r"(?:带宽|端口|Port)\s*[:：]?\s*(\d+(?:\.\d+)?)\s*(Mbps|Gbps|M|G)", text, re.I)
    if m:
        unit = m.group(2).strip().upper()
        speed = f"{m.group(1)}{'Gbps' if unit.startswith('G') else 'Mbps'}"

    parts = []
    if cpu:
        parts.append(f"{cpu} Core")
    if ram:
        parts.append(ram)
    if disk:
        parts.append(disk)
    if traffic or speed:
        parts.append(f"{traffic or ''}{'@' if traffic and speed else ''}{speed or ''}")
    return " / ".join(parts) if len(parts) >= 2 else None  # 只识别到一项时可靠性太低，不显示


# ---------------------------------------------------------------- 价格
_SYMBOL_CODE = {"¥": "CNY", "￥": "CNY", "$": "USD", "US$": "USD", "HK$": "HKD", "€": "EUR", "£": "GBP", "CN¥": "CNY"}
_CYCLES = [
    (r"monthly|月付|/\s*月|/\s*mo(?:nth)?\b|每月|月", "month"),
    (r"quarterly|季付|/\s*季|季度", "quarter"),
    (r"semi-?annually|半年付|半年", "half-year"),
    (r"biennially|两年付|二年付", "2 years"),
    (r"triennially|三年付", "3 years"),
    (r"annually|年付|/\s*年|/\s*yr|/\s*year|每年", "year"),
    (r"one\s*time|一次性", "one-time"),
]


def normalize_price(raw: str | None) -> str | None:
    """'¥30.00CNY 月付' → '¥30.00 CNY / month'；无法解析时原样返回。"""
    if not raw:
        return None
    m = re.search(r"(HK\$|US\$|CN¥|[¥￥$€£])?\s*(\d[\d,]*(?:\.\d+)?)\s*(USD|CNY|RMB|HKD|EUR|GBP|元)?", raw, re.I)
    if not m or not (m.group(1) or m.group(3)):
        return raw.strip()
    symbol, amount, code = m.group(1) or "", m.group(2), (m.group(3) or "").upper()
    if code in ("RMB", "元"):
        code = "CNY"
    code = code or _SYMBOL_CODE.get(symbol, "")
    if not symbol:
        symbol = {"CNY": "¥", "USD": "$", "EUR": "€", "GBP": "£", "HKD": "HK$"}.get(code, "")
    rest = raw[m.end():]
    cycle = next((name for pat, name in _CYCLES if re.search(pat, rest + " " + raw[:m.start()], re.I)), None)
    out = f"{symbol}{amount} {code}".strip()
    if cycle:
        out += f" / {cycle}"
    # 设定费 / 安装费（如“$50.00 设定费”“Setup Fee: $50”）
    fee = re.search(r"(HK\$|US\$|[¥￥$€£])?\s*(\d[\d,]*(?:\.\d+)?)\s*(?:USD|CNY|RMB|HKD|EUR|GBP|元)?\s*"
                    r"(?:设定费|設定費|设置费|安装费|安裝費|初装费|开通费|Setup\s*Fee)", rest, re.I) or \
        re.search(r"(?:设定费|設定費|设置费|安装费|初装费|开通费|Setup\s*Fee)\s*[:：]?\s*(HK\$|US\$|[¥￥$€£])?\s*(\d[\d,]*(?:\.\d+)?)",
                  rest, re.I)
    if fee and float(fee.group(2).replace(",", "")) > 0:
        out += f" + {fee.group(1) or symbol}{fee.group(2)} 设定费"
    return out


_NUM = re.compile(r"\d[\d,]*(?:\.\d+)?")


def price_change(old: str | None, new: str | None) -> str | None:
    """比较两次抓到的价格，返回 "down"（降价）/ "up"（涨价）/ None（没变或无法比较）。

    为避免误报，只有两次价格的格式完全一致（货币、周期、文字都相同）、只是数字不同时才算变动；
    例如页面切换语言或货币导致的变化不算。比较的是第一个数字（主价格）。
    """
    if not old or not new or old == new:
        return None
    if _NUM.sub("#", old).split() != _NUM.sub("#", new).split():
        return None
    a, b = _NUM.search(old), _NUM.search(new)
    if not a or not b:
        return None
    x, y = float(a.group().replace(",", "")), float(b.group().replace(",", ""))
    if x == y:
        return None
    return "down" if y < x else "up"


# ---------------------------------------------------------------- 卡片
def tagify(text: str) -> str:
    """Telegram 话题标签只能包含字母、数字、下划线和中文。"""
    return re.sub(r"[^0-9A-Za-z_一-鿿]", "", text or "")


def merchant_from_url(url: str) -> str:
    host = (urlparse(url).hostname or "").lower()
    parts = [p for p in host.split(".") if p not in ("www", "my", "portal", "cloud", "billing", "client", "clients", "store", "shop")]
    if len(parts) >= 2:
        return parts[-2]
    return parts[0] if parts else host


def apply_aff(url: str | None, aff: str | None) -> str | None:
    """在购买链接后追加推广参数（如 aff=123），已存在的同名参数会被替换。"""
    if not url or not aff:
        return url
    extra = parse_qsl(aff.lstrip("?&"), keep_blank_values=True)
    if not extra:
        return url
    p = urlparse(url)
    keys = {k for k, _ in extra}
    query = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True) if k not in keys] + extra
    return urlunparse(p._replace(query=urlencode(query, safe="/:")))


@dataclass
class Card:
    title: str
    price: str | None
    location: str | None
    lines: list[str]
    coupon: str | None
    specs: str | None
    buy_url: str | None
    tags: list[str] = field(default_factory=list)
    detected: dict = field(default_factory=dict)  # 自动识别结果，供后台显示


def build_card(site: dict, site_meta: dict, product: dict, product_meta: dict, price_override: str | None = None) -> Card:
    """合并自动识别结果与手动设置，得到推送卡片。site/product 为数据库行（dict）。"""
    name = product.get("name") or ""
    desc = product.get("description") or ""
    group = site_meta.get("prefix") or ""   # 名称前缀（如“AndNode-美国云”）也参与识别
    texts = (name, desc, group, site.get("name") or "")

    # 按地区拆分出来的机型（键含 #loc:）只看它自己的名称（如“Plan · Tokyo”），
    # 不能再从描述“Tokyo or Singapore”里识别出多个地区
    if "#loc:" in (product.get("key") or ""):
        locs_auto = detect_locations(name.rsplit(" · ", 1)[-1], name)[:1]
    else:
        locs_auto = detect_locations(*texts)
    loc_auto = locs_auto[0] if locs_auto else None
    lines_auto = detect_lines(name, desc, group)
    specs_auto = detect_specs(name, desc)
    merchant = (site_meta.get("merchant") or "").strip() or merchant_from_url(site.get("url", ""))

    # 名称：网址设置的前缀 + 机型名
    prefix = (site_meta.get("prefix") or site_meta.get("merchant") or "").strip()
    display = (product_meta.get("name") or "").strip() or name
    title = display if not prefix or display.lower().startswith(prefix.lower()) else f"{prefix}-{display}"

    # 地区：auto / none / 地区代码
    loc_choice = (product_meta.get("location") or "auto").strip()
    if loc_choice == "none":
        locs: list[Location] = []
    elif loc_choice in LOCATIONS:
        locs = [LOCATIONS[loc_choice]]
    else:
        locs = locs_auto

    lines_raw = product_meta.get("lines")
    lines = [x for x in re.split(r"[/,，\s]+", lines_raw) if x] if lines_raw not in (None, "") else lines_auto
    if lines_raw == "-":
        lines = []

    specs_raw = product_meta.get("specs")
    specs = None if specs_raw == "-" else (specs_raw or specs_auto)

    coupon = (product_meta.get("coupon") or site_meta.get("coupon") or "").strip() or None
    if coupon == "-":
        coupon = None

    buy_url = (product_meta.get("buy_url") or "").strip() or apply_aff(product.get("order_url"), site_meta.get("aff"))
    price = normalize_price(price_override if price_override is not None else product.get("price"))

    tags = [tagify(merchant).lower()] if tagify(merchant) else []
    for loc in locs:
        tags += loc.tags
    tags += [tagify(x) for x in lines]
    if any(x.upper().replace(" ", "") in OPTIMIZED_LINES for x in lines):
        tags.append("优化线路")
    for extra in re.split(r"[\s,，#]+", f"{site_meta.get('tags') or ''} {product_meta.get('tags') or ''}"):
        if tagify(extra):
            tags.append(tagify(extra))
    tags = list(dict.fromkeys(t for t in tags if t))

    return Card(
        title=title,
        price=price,
        location=" / ".join(x.text for x in locs) or None,
        lines=lines,
        coupon=coupon,
        specs=specs,
        buy_url=buy_url,
        tags=tags,
        detected={
            "location": loc_auto.code if loc_auto else None,
            "location_text": " / ".join(x.text for x in locs_auto) or None,
            "lines": "/".join(lines_auto),
            "specs": specs_auto,
            "merchant": merchant_from_url(site.get("url", "")),
            "price": price,
        },
    )


def location_choices() -> list[dict]:
    return [{"code": c, "text": loc.text} for c, loc in LOCATIONS.items()]
