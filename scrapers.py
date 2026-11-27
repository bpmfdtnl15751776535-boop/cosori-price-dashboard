"""COSORI 空气炸锅三平台价格采集适配器。

平台：Wildberries / Yandex Market / Ozon
输出：统一结构的 Product 列表（型号、店铺、到手价、原价、商品链接、采集状态）。
"""
from __future__ import annotations

import html as html_mod
import json
import os
import re
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import quote, urljoin, urlsplit, unquote

import requests
from bs4 import BeautifulSoup

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {
    "User-Agent": UA,
    "Accept-Language": "ru-RU,ru;q=0.9,en;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/json;q=0.9,*/*;q=0.8",
}
MSK_TZ = timezone(timedelta(hours=3))


def playwright_proxy(url: str) -> dict[str, str] | None:
    """把 http://user:pass@host:port 拆成 Playwright 的 server/username/password。

    Playwright 不接受把凭据嵌在 server URL 里（会报 ERR_INVALID_AUTH_CREDENTIALS），
    必须拆开传。无代理返回 None。
    """
    if not url:
        return None
    s = urlsplit(url)
    server = f"{s.scheme}://{s.hostname}:{s.port}" if s.hostname and s.port else url
    out = {"server": server}
    if s.username:
        out["username"] = unquote(s.username)
    if s.password:
        out["password"] = unquote(s.password)
    return out

AIRFRYER_WORDS = ("аэрогриль", "аэро гриль", "фритюрниц", "air fryer", "аэрогрил")
ACCESSORY_WORDS = ("коврик", "силикон", "cookbook", "рецепт", "книг", "чехол",
                   "крышка", "фильтр", "корзин", "запчасть", "термометр", "бумаг",
                   "пакет", "щетк", "насадк", "ручкав", "cover", "mat ", "food thermometer")

# Yandex 搜索页只给商家 ID，不直接给店铺名。这里维护已确认的商家 ID → 店铺名。
YANDEX_SHOP_NAMES = {
    "197276196": "Federal Express",
    "216468793": "WISE MALL",
    "7860288": "COSORI",
}
CUSTOMER_SHOPS = ("techhub", "awsd", "wise mall", "wiswmall", "wamallb",
                  "smart mall", "federal express", "absolyut-treyd",
                  "абсолют трейд", "супер-центр")
OWN_LOCAL_URL = "https://www.ozon.ru/seller/cosori-official/"
OWN_CROSS_URL = "https://www.ozon.ru/seller/cosori-official-online/"


def now_str() -> str:
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S MSK")


@dataclass
class Product:
    platform: str
    platform_cn: str
    title: str
    model: str
    shop: str
    price: float | None          # 到手价（卢布）
    original_price: float | None # 划线价（卢布）
    url: str
    status: str = "ok"           # ok / blocked / error
    note: str = ""
    seller_id: str = ""
    seller_url: str = ""
    seller_type: str = ""
    fetched_at: str = field(default_factory=now_str)

    def to_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["seller_type"] = classify_seller(self)
        return d


def classify_seller(p: "Product") -> str:
    """按已知店铺名单把商品归为：我司本土店 / 我司跨境店 / 客户店铺 / 其他卖家。"""
    shop = (p.shop or "").lower()
    url = (p.seller_url or "").lower()
    if "cosori-official-online" in url:
        return "我司跨境店"
    if ("cosori-official" in url
            or (p.platform == "ozon" and "本店自售" in shop)):
        return "我司本土店"
    if any(k in shop or k in url for k in CUSTOMER_SHOPS):
        return "客户店铺"
    return "其他卖家"


def extract_models(text: str) -> str:
    """从商品标题中提取规范型号：CAF-DC112 -> DC112，CP158-AF -> CP158。"""
    if not text:
        return "未识别型号"
    t = text.upper().replace("（", "(").replace("）", ")")
    codes = r"(?:DC|CP|AF|TK|TF|R|L|P|DR|TS|CE)"
    patterns = [
        rf"(?<![A-Z0-9])CAF[\s\-]?({codes}\s?\d{{2,4}}[A-Z]{{0,2}})",
        rf"(?<![A-Z0-9])({codes}\s?\d{{3}}[A-Z]{{0,2}})",
        r"(?<![A-Z0-9])([A-Z]{1,3}\s?\d{3}[A-Z]{0,2})",
    ]
    for pat in patterns:
        m = re.search(pat, t)
        if m:
            return re.sub(r"[\s\-]", "", m.group(1)).upper()
    for name, pat in [
        ("Turbo Tower Pro", r"TURBO[\s\-]?TOWER"),
        ("Turbo Blaze", r"TURBO[\s\-]?BLAZE"),
        ("Dual Blaze", r"DUAL[\s\-]?BLAZE"),
    ]:
        if re.search(pat, t):
            return name
    return "未识别型号"


def to_rub(value: Any) -> float | None:
    """把 '23 831 ₽' / 23831 / 23831.5 / 2383100(戈比) 统一为卢布浮点。"""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return round(float(value) / 100 if value > 1_000_000 else float(value), 2)
    s = str(value).replace("\u2009", "").replace("\u00a0", "").replace(" ", "")
    m = re.search(r"(\d+(?:[.,]\d+)?)", s)
    if not m:
        return None
    num = float(m.group(1).replace(",", "."))
    return round(num, 2)


def is_airfryer(title: str) -> bool:
    t = title.lower()
    if "cosori" not in t or any(w in t for w in ACCESSORY_WORDS):
        return False
    if not any(w in t for w in AIRFRYER_WORDS):
        return False
    upper = t.upper()
    has_code = bool(re.search(r"(?:DC|CP|AF|TK|TF|R|L|P|DR|TS|CE)\s?\d{3}", upper))
    return (has_code or t.lstrip().startswith("аэрогриль")
            or "turbo" in t or "dual blaze" in t)


def blocked(platform: str, cn: str, reason: str) -> list[Product]:
    return [Product(platform=platform, platform_cn=cn, title="—", model="—",
                    shop="—", price=None, original_price=None, url="",
                    status="blocked", note=reason)]


def _norm_title(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())


def yandex_shop_map(markup: str) -> dict[str, str]:
    """从 Yandex 搜索页 SSR JSON 提取 商品标题 -> businessId 映射。"""
    result: dict[str, str] = {}
    soup = BeautifulSoup(markup, "lxml")

    def walk(obj: Any) -> None:
        if isinstance(obj, dict):
            bid = str(obj.get("businessId") or "")
            if bid:
                title = obj.get("title") or obj.get("name")
                if isinstance(title, str) and title:
                    result.setdefault(_norm_title(title), bid)
            for value in obj.values():
                walk(value)
        elif isinstance(obj, list):
            for value in obj[:200]:
                walk(value)

    for nf in soup.find_all("noframes"):
        try:
            data = json.loads(nf.string or nf.get_text())
        except Exception:
            continue
        walk(data)
    return result


YANDEX_SHOP_CACHE_PATH = Path(__file__).resolve().parent / "data" / "yandex_shops.json"


def _yandex_shop_cache() -> dict[str, str]:
    try:
        return json.loads(YANDEX_SHOP_CACHE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {}


def resolve_yandex_shop_name(bid: str) -> str:
    """已知名单/本地缓存优先；未知 businessId 访问商家页提取 businessTitle。"""
    if not bid:
        return ""
    if bid in YANDEX_SHOP_NAMES:
        return YANDEX_SHOP_NAMES[bid]
    cache = _yandex_shop_cache()
    if bid in cache:
        return cache[bid]
    url = f"https://market.yandex.ru/search?text=COSORI&businessId={bid}&searchContext=sins_ctx"
    name = ""
    try:
        r = requests.get(url, headers=HEADERS, timeout=(20, 45))
        soup = BeautifulSoup(r.text, "lxml")
        found: set[str] = set()

        def walk(obj: Any) -> None:
            if isinstance(obj, dict):
                if str(obj.get("businessId") or "") == bid and obj.get("businessTitle"):
                    found.add(str(obj["businessTitle"]).strip())
                for value in obj.values():
                    walk(value)
            elif isinstance(obj, list):
                for value in obj[:200]:
                    walk(value)

        for nf in soup.find_all("noframes"):
            try:
                walk(json.loads(nf.string or nf.get_text()))
            except Exception:
                continue
        if found:
            name = sorted(found, key=len)[0][:80]
    except Exception:
        pass
    if name:
        cache[bid] = name
        YANDEX_SHOP_CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        YANDEX_SHOP_CACHE_PATH.write_text(
            json.dumps(cache, ensure_ascii=False, indent=2), encoding="utf-8")
    return name


def yandex_shop_info(bid: str) -> tuple[str, str]:
    """businessId -> (店铺名, 店铺搜索链接)。"""
    if not bid:
        return "Яндекс Маркет / 第三方卖家", ""
    name = resolve_yandex_shop_name(bid) or f"Яндекс 商家 {bid}"
    url = f"https://market.yandex.ru/search?text=COSORI&businessId={bid}&searchContext=sins_ctx"
    return name, url


# ---------------------------------------------------------------- Yandex Market

def fetch_yandex_html(query: str = "COSORI", pages: int = 1) -> list[str]:
    """Yandex 连接偶发提前断流，重试并保留已收到的字节。"""
    out: list[str] = []
    for page in range(1, pages + 1):
        url = f"https://market.yandex.ru/search?text={quote(query)}&cvredirect=1"
        if page > 1:
            url += f"&page={page}"
        best = b""
        for attempt in range(5):
            try:
                with requests.get(url, headers=HEADERS, timeout=(20, 60), stream=True) as r:
                    if r.status_code != 200:
                        break
                    buf = bytearray()
                    for chunk in r.iter_content(65536):
                        if chunk:
                            buf += chunk
                    if len(buf) > len(best):
                        best = bytes(buf)
                if len(best) > 1_000_000:
                    break
            except Exception:
                continue
        if best:
            out.append(best.decode("utf-8", errors="ignore"))
        time.sleep(1.5)
    return out


def scrape_yandex(pages: int = 1) -> list[Product]:
    pages_html = fetch_yandex_html(pages=pages)
    if not pages_html:
        return blocked("yandex", "Yandex Market", "页面请求失败或连接被重置")

    products: list[Product] = []
    seen: set[str] = set()
    for markup in pages_html:
        soup = BeautifulSoup(markup, "lxml")
        biz_by_title = yandex_shop_map(markup)

        # 1) SSR 补丁 JSON：搜索页真正使用的商品列表（含 productId/price/url）
        def find_items(obj: Any) -> list[dict] | None:
            if isinstance(obj, dict):
                items = obj.get("items")
                if (isinstance(items, list) and items and isinstance(items[0], dict)
                        and "productId" in items[0] and "price" in items[0]):
                    return items
                for v in obj.values():
                    found = find_items(v)
                    if found:
                        return found
            elif isinstance(obj, list):
                for v in obj[:8]:
                    found = find_items(v)
                    if found:
                        return found
            return None

        for nf in soup.find_all("noframes"):
            blob = nf.string or nf.get_text()
            try:
                data = json.loads(blob)
            except Exception:
                continue
            items = find_items(data)
            if not items:
                continue
            for row in items:
                name = row.get("name", "")
                url = row.get("url", "")
                if not name or not url or not is_airfryer(name):
                    continue
                key = url.split("?")[0]
                if key in seen:
                    continue
                seen.add(key)
                bid = biz_by_title.get(_norm_title(name), "")
                shop_name, seller_url = yandex_shop_info(bid)
                products.append(Product(
                    platform="yandex", platform_cn="Yandex Market", title=name,
                    model=extract_models(name),
                    shop=shop_name,
                    price=to_rub(row.get("price")), original_price=None,
                    url=url if url.startswith("http") else urljoin("https://market.yandex.ru", url),
                    seller_id=bid, seller_url=seller_url,
                    note="到手价为搜索页展示价；店铺名来自搜索页商家 ID" if bid
                         else "到手价为搜索页展示价；未识别店铺",
                ))

        # 2) JSON-LD 兜底
        for tag in soup.find_all("script", type="application/ld+json"):
            try:
                data = json.loads(tag.string or "{}")
            except Exception:
                continue
            items = data.get("itemListElement", []) if isinstance(data, dict) else []
            for entry in items:
                obj = entry.get("item", entry) if isinstance(entry, dict) else {}
                name = obj.get("name", "")
                url = obj.get("url", "")
                if not name or not url or "cosori" not in name.lower():
                    continue
                key = url.split("?")[0]
                if key in seen or not is_airfryer(name):
                    continue
                seen.add(key)
                offers = obj.get("offers", {})
                price = to_rub(offers.get("price") or offers.get("lowPrice"))
                bid = biz_by_title.get(_norm_title(name), "")
                shop_name, seller_url = yandex_shop_info(bid)
                products.append(Product(
                    platform="yandex", platform_cn="Yandex Market", title=name,
                    model=extract_models(name),
                    shop=shop_name,
                    price=price, original_price=None,
                    url=url if url.startswith("http") else urljoin("https://market.yandex.ru", url),
                    seller_id=bid, seller_url=seller_url,
                    note="到手价为搜索页展示价；店铺名来自搜索页商家 ID" if bid
                         else "到手价为搜索页展示价；未识别店铺",
                ))

        # 2) 从 SSR 卡片文本补充"вместо X"原价与 Яндекс Пэй 到手价
        text_by_url: dict[str, str] = {}
        for card in soup.select('[data-data-source="ss-product"]'):
            link = next((a["href"] for a in card.find_all("a", href=True)
                         if "/card/" in a["href"]), None)
            if not link:
                continue
            txt = card.get_text(" ", strip=True)
            # 去掉内嵌 JSON 噪声，只保留可见商品文本
            vis = txt[txt.rfind("}}") + 2:] if "}}" in txt else txt
            text_by_url[link.split("?")[0]] = vis[-600:]

        for p in products:
            key = p.url.split("?")[0]
            vis = text_by_url.get(key, "")
            m = re.search(r"Цена с картой Яндекс Пэй\s*([\d\s\u2009\u00a0]+)₽\s*вместо\s*([\d\s\u2009\u00a0]+)₽", vis)
            if m:
                p.price = to_rub(m.group(1))
                p.original_price = to_rub(m.group(2))
            elif re.search(r"\b₽", vis):
                m2 = re.search(r"([\d\s\u2009\u00a0]+)₽", vis)
                if m2:
                    p.price = to_rub(m2.group(1))
            sm = re.search(r"Продавец[:\s]+([^,;]{2,60})", vis)
            if sm:
                p.shop = sm.group(1).strip()
                p.note = ""

    if not products:
        return blocked("yandex", "Yandex Market", "页面中未解析到 COSORI 商品，可能触发验证")
    return products


# ------------------------------------------------------------------ Wildberries

WB_SEARCH_API = "https://search.wb.ru/exactmatch/ru/common/v9/search"
WB_CARD_API = "https://card.wb.ru/cards/v4/detail"


def _wb_request(session: requests.Session, url: str, params: dict) -> Any | None:
    try:
        r = session.get(url, params=params, headers={**HEADERS,
                        "Origin": "https://www.wildberries.ru",
                        "Referer": "https://www.wildberries.ru/",
                        "Accept": "*/*"}, timeout=25)
        if r.status_code == 200:
            return r.json()
    except Exception:
        pass
    return None


def scrape_wildberries(pages: int = 2, proxy: str | None = None) -> list[Product]:
    """优先走 WB 内部 JSON 接口；被反爬拦截时可回退到浏览器方案。"""
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    products: list[Product] = []
    for page in range(1, pages + 1):
        data = _wb_request(session, WB_SEARCH_API, {
            "appType": 1, "curr": "rub", "dest": -1257786,
            "page": page, "query": "COSORI", "resultset": "catalog",
            "sort": "popular", "suppressSpellcheck": "false",
        })
        if not data:
            break
        raw = data.get("data", {}).get("products", [])
        if not raw:
            break
        for item in raw:
            name = item.get("name", "")
            if not is_airfryer(name):
                continue
            sizes = item.get("sizes") or []
            price_data = next((s.get("price", {}) for s in sizes
                               if s.get("price", {}).get("product")), {})
            price = to_rub(price_data.get("product") or price_data.get("total"))
            orig = to_rub(price_data.get("basic")) if price_data.get("basic") else None
            nm = item.get("id") or item.get("nmId")
            if not nm:
                continue
            products.append(Product(
                platform="wildberries", platform_cn="Wildberries", title=name,
                model=extract_models(name),
                shop=item.get("supplier") or item.get("brand") or "未标注",
                price=price, original_price=orig,
                url=f"https://www.wildberries.ru/catalog/{nm}/detail.aspx",
            ))
        time.sleep(1)

    if products:
        return products

    # 回退：真实浏览器加载（若代理可用）
    browser_items = _scrape_wb_browser(proxy)
    if browser_items:
        return browser_items
    return blocked("wildberries", "Wildberries",
                   "当前网络 IP 被 WB 反爬拦截（HTTP 498）。配置俄罗斯住宅代理后重试")


def _scrape_wb_browser(proxy: str | None) -> list[Product]:
    """WB 反爬挑战由前端 JS 自动求解，实测需等待约 40 秒后才渲染商品卡片。"""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []
    import os
    # 只用显式传入或 COSORI_PROXY 指定的代理；绝不读 HTTPS_PROXY（那通常是本机 VPN/系统代理，
    # 会让 Playwright 走错出口导致 WB 498 / Ozon 403，实测裸连反而正常）。
    use_proxy = proxy or os.environ.get("COSORI_PROXY")
    out: list[Product] = []
    try:
        with sync_playwright() as p:
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "proxy": playwright_proxy(use_proxy),
                "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            }
            if os.environ.get("COSORI_BROWSER") != "chromium":
                launch_kwargs["channel"] = "chrome"
            browser = p.chromium.launch(**launch_kwargs)
            ctx = browser.new_context(locale="ru-RU", viewport={"width": 1440, "height": 900},
                                      user_agent=UA)
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = ctx.new_page()
            page.goto("https://www.wildberries.ru/catalog/0/search.aspx?search=COSORI",
                      wait_until="domcontentloaded", timeout=90000)
            cards = []
            for _ in range(9):
                page.wait_for_timeout(10000)
                cards = page.query_selector_all(".product-card")
                if len(cards) >= 10:
                    break
            print(f"    [浏览器] WB 渲染商品卡片 {len(cards)} 个", flush=True)

            for card in cards:
                nm = card.get_attribute("data-nm-id")
                link = card.query_selector('a[href*="detail.aspx"]')
                href = link.get_attribute("href") if link else ""
                title = (link.get_attribute("aria-label") if link else "") or ""
                text = (card.inner_text() or "").strip()
                if not title:
                    title = next((ln.strip() for ln in text.splitlines()
                                  if "cosori" in ln.lower()), text[:120])
                if not nm or "cosori" not in title.lower() or not is_airfryer(title):
                    continue
                prices = re.findall(r"([\d\u00a0\u2009 ]+)\s*₽", text)
                out.append(Product(
                    platform="wildberries", platform_cn="Wildberries", title=title[:180],
                    model=extract_models(title), shop="见商品页",
                    price=to_rub(prices[0]) if prices else None,
                    original_price=to_rub(prices[1]) if len(prices) > 1 else None,
                    url=href or f"https://www.wildberries.ru/catalog/{nm}/detail.aspx"))

            # 尝试在已通过验证的页面上下文里取供应商（店铺）名称
            if out:
                ids = [re.search(r"/catalog/(\d+)/", p.url).group(1)
                       for p in out if re.search(r"/catalog/(\d+)/", p.url)]
                try:
                    js = """async (ids) => {
                      const u = 'https://card.wb.ru/cards/v4/detail?appType=1&curr=rub'
                        + '&dest=-1257786&spp=30&nm=' + ids.join(';');
                      const r = await fetch(u, {credentials: 'include'});
                      if (!r.ok) return null;
                      return await r.json();
                    }"""
                    data = page.evaluate(js, ids[:30])
                    suppliers = {}
                    for prod in (data or {}).get("data", {}).get("products", []):
                        suppliers[str(prod.get("id"))] = prod.get("supplier") or prod.get("brand")
                    for p in out:
                        m = re.search(r"/catalog/(\d+)/", p.url)
                        if m and m.group(1) in suppliers:
                            p.shop = suppliers[m.group(1)]
                            p.note = ""
                except Exception:
                    pass
            browser.close()
    except Exception as exc:
        print(f"    [浏览器] WB 采集失败：{type(exc).__name__}", flush=True)
        return []
    seen, uniq = set(), []
    for p in out:
        if p.url in seen:
            continue
        seen.add(p.url)
        uniq.append(p)
    return uniq


# ------------------------------------------------------------------------- Ozon

OZON_API = "https://www.ozon.ru/composer-api.bx/page/json/v2"


def scrape_ozon(pages: int = 2, proxy: str | None = None) -> list[Product]:
    session = requests.Session()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})
    products: list[Product] = []
    for page in range(1, pages + 1):
        path = "/search/?text=cosori"
        if page > 1:
            path += f"&page={page}"
        try:
            r = session.get(OZON_API, params={"url": path},
                            headers={**HEADERS, "Referer": "https://www.ozon.ru/",
                                     "Accept": "application/json"},
                            timeout=25, allow_redirects=False)
            if r.status_code == 200:
                data = r.json()
                products.extend(_parse_ozon_json(data))
                if products:
                    break
        except Exception:
            break
        time.sleep(1)

    if not products:
        products = _scrape_ozon_browser(proxy)
    if not products:
        return blocked("ozon", "Ozon",
                       "当前网络 IP 被 Ozon 拦截（403/重定向循环）。配置俄罗斯住宅代理后重试")
    seen, uniq = set(), []
    for p in products:
        if p.url in seen:
            continue
        seen.add(p.url)
        uniq.append(p)
    return uniq


def _parse_ozon_json(data: dict) -> list[Product]:
    out: list[Product] = []
    widgets = data.get("widgetStates", {})
    for key, value in widgets.items():
        if "search" not in key.lower() and "product" not in key.lower():
            continue
        try:
            obj = json.loads(value) if isinstance(value, str) else value
        except Exception:
            continue
        rows: Iterable[dict] = []
        for holder in (obj, obj.get("results", {}), obj.get("state", {})):
            if isinstance(holder, dict) and isinstance(holder.get("items"), list):
                rows = holder["items"]
                break
        for row in rows:
            if not isinstance(row, dict):
                continue
            name = row.get("name") or row.get("title") or ""
            if not name:
                continue
            title = html_mod.unescape(re.sub(r"<[^>]+>", "", name))
            if not is_airfryer(title):
                continue
            price = to_rub(row.get("price") or (row.get("priceInfo") or {}).get("price"))
            orig = to_rub(row.get("oldPrice") or (row.get("priceInfo") or {}).get("oldPrice"))
            link = row.get("url") or row.get("link") or ""
            if link and not link.startswith("http"):
                link = urljoin("https://www.ozon.ru", link)
            if not link:
                link = f"https://www.ozon.ru/product/{row.get('id') or ''}/"
            shop = row.get("shopName") or row.get("seller") or (row.get("brand") or {}).get("name") if isinstance(row.get("brand"), dict) else row.get("shopName") or row.get("seller") or ""
            out.append(Product(platform="ozon", platform_cn="Ozon", title=title,
                               model=extract_models(title), shop=shop or "见商品页",
                               price=price, original_price=orig, url=link))
    return out


def _scrape_ozon_browser(proxy: str | None) -> list[Product]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return []
    import os
    # 只用显式传入或 COSORI_PROXY 指定的代理；绝不读 HTTPS_PROXY（那通常是本机 VPN/系统代理，
    # 会让 Playwright 走错出口导致 WB 498 / Ozon 403，实测裸连反而正常）。
    use_proxy = proxy or os.environ.get("COSORI_PROXY")
    out: list[Product] = []
    try:
        with sync_playwright() as p:
            launch_kwargs: dict[str, Any] = {
                "headless": True,
                "proxy": playwright_proxy(use_proxy),
                "args": ["--disable-blink-features=AutomationControlled", "--no-sandbox"],
            }
            if os.environ.get("COSORI_BROWSER") != "chromium":
                launch_kwargs["channel"] = "chrome"
            browser = p.chromium.launch(**launch_kwargs)
            ctx = browser.new_context(locale="ru-RU", viewport={"width": 1440, "height": 900},
                                      user_agent=UA)
            ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
            page = ctx.new_page()
            page.on("response", lambda r: _collect_ozon_response(r, out))
            page.goto("https://www.ozon.ru/search/?text=COSORI",
                      wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(18000)
            browser.close()
    except Exception:
        pass
    return out


def _collect_ozon_response(response, out: list[Product]) -> None:
    if "json" not in response.headers.get("content-type", ""):
        return
    try:
        data = response.json()
    except Exception:
        return
    if not isinstance(data, dict):
        return
    out.extend(_parse_ozon_json(data))
