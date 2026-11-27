"""连上/断开 VPN 后各跑一次，对比出口 IP 与 WB / Ozon 可达性。

用法：python check_vpn.py
"""
from __future__ import annotations

import requests
from playwright.sync_api import sync_playwright

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

try:
    d = requests.get("https://ipinfo.io/json", timeout=15).json()
    print(f"出口 IP：{d.get('ip')}  {d.get('country')}/{d.get('region')} {d.get('city')}")
    print(f"运营商：{d.get('org')}")
except Exception as exc:
    print(f"出口 IP 查询失败：{exc}")
print("=" * 66, flush=True)

targets = [
    ("Wildberries", "https://www.wildberries.ru/catalog/0/search.aspx?search=COSORI", 45000),
    ("Ozon", "https://www.ozon.ru/search/?text=COSORI", 15000),
]
with sync_playwright() as p:
    browser = p.chromium.launch(
        channel="chrome", headless=True,
        args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
    for name, url, wait_ms in targets:
        ctx = browser.new_context(locale="ru-RU", user_agent=UA,
                                  viewport={"width": 1366, "height": 850})
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()
        print(f"[测试] {name} ...", flush=True)
        try:
            resp = page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(wait_ms)
            cards = len(page.query_selector_all(".product-card, [data-widget='searchResults']"))
            print(f"  HTTP {resp.status if resp else '—'} | 标题：{(page.title() or '')[:60]} "
                  f"| 卡片数：{cards}", flush=True)
        except Exception as exc:
            print(f"  失败：{type(exc).__name__}: {exc}"[:150], flush=True)
        finally:
            ctx.close()
    browser.close()
