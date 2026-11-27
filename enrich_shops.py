"""补全 Wildberries 商品的真实店铺名称。

流程：浏览器过 WB 反爬 -> 从商品卡片图片 URL 精确取 basket 分片 -> 静态 card.json 取
supplier_id -> 复用浏览器会话打开 seller 页，从 title 读取店名。

不做 34 个分片的暴力探测（太慢），改为从页面 DOM 直接提取 host。
"""
from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path

import requests
from scrapers import Product
from scraper import render_html

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "products.json"
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
PROXY = os.environ.get("HTTPS_PROXY") or os.environ.get("https_proxy")


def main() -> int:
    from playwright.sync_api import sync_playwright

    d = json.loads(DATA.read_text(encoding="utf-8"))
    rows = d["products"]
    wb_rows = [r for r in rows if r["platform"] == "wildberries" and r["status"] == "ok"]
    target_ids = {re.search(r"/catalog/(\d+)/", r["url"]).group(1)
                  for r in wb_rows if re.search(r"/catalog/(\d+)/", r["url"])}
    print(f"[WB] 待补全 {len(wb_rows)} 件 / {len(target_ids)} 个 nm", flush=True)

    nm_host: dict[str, str] = {}
    sid_by_nm: dict[str, str] = {}
    with sync_playwright() as p:
        b = p.chromium.launch(
            channel="chrome", headless=True,
            proxy={"server": PROXY} if PROXY else None,
            args=["--disable-blink-features=AutomationControlled", "--no-sandbox"])
        ctx = b.new_context(locale="ru-RU", viewport={"width": 1440, "height": 900}, user_agent=UA)
        ctx.add_init_script("Object.defineProperty(navigator,'webdriver',{get:()=>undefined})")
        page = ctx.new_page()
        page.goto("https://www.wildberries.ru/catalog/0/search.aspx?search=COSORI",
                  wait_until="domcontentloaded", timeout=90000)
        for i in range(9):
            page.wait_for_timeout(5000)
            if page.locator(".product-card").count() >= 10:
                break
        print(f"[WB] 商品卡片 {page.locator('.product-card').count()} 个", flush=True)

        # 从卡片 DOM 提取 nm -> basket host（精确分片，避免暴力探测）
        mapping = page.evaluate("""(ids) => {
          const out = {};
          for (const card of document.querySelectorAll('.product-card')) {
            const nm = card.getAttribute('data-nm-id');
            if (!nm || !ids.includes(nm)) continue;
            const img = card.querySelector('img');
            const src = img ? (img.getAttribute('src') || img.getAttribute('data-src') || '') : '';
            const m = src.match(/https:\\/\\/(basket-\\d+\\.wbbasket\\.\\w+)\\//);
            if (m) out[nm] = m[1];
          }
          return out;
        }""", sorted(target_ids))
        nm_host.update(mapping)
        print(f"[WB] 提取到 {len(nm_host)} 个 basket host", flush=True)

        sess = requests.Session()
        if PROXY:
            sess.proxies.update({"http": PROXY, "https": PROXY})
        for nm in sorted(target_ids):
            host = nm_host.get(nm) or f"basket-{(int(nm)//100000)%30+1:02d}.wbbasket.ru"
            vol, part = int(nm)//100000, int(nm)//1000
            url = f"https://{host}/vol{vol}/part{part}/{nm}/info/ru/card.json"
            try:
                r = sess.get(url, headers={"User-Agent": UA}, timeout=12)
                if r.status_code == 200:
                    sid = (r.json().get("selling") or {}).get("supplier_id")
                    if sid:
                        sid_by_nm[nm] = str(sid)
            except Exception:
                pass
        print(f"[WB] 取到 {len(sid_by_nm)} 个 supplier_id", flush=True)

        # 去重后访问 seller 页读店名
        sids = sorted(set(sid_by_nm.values()))
        names: dict[str, str] = {}
        for sid in sids:
            try:
                page.goto(f"https://www.wildberries.ru/seller/{sid}",
                          wait_until="domcontentloaded", timeout=60000)
                for _ in range(3):
                    page.wait_for_timeout(2500)
                    title = page.title()
                    if title and "Antibot" not in title and "challenge" not in title.lower():
                        break
                title = page.title()
                m = re.match(r"^(.*?)(?:\s*-\s*каталог продавца|\s*-\s*Wildberries)", title)
                names[sid] = (m.group(1).strip() if m else title.strip()) or "见商品页"
                print(f"  {sid} -> {names[sid]}", flush=True)
            except Exception as exc:
                print(f"  {sid} err {type(exc).__name__}", flush=True)
            time.sleep(1)
        b.close()

    filled = 0
    for r in wb_rows:
        m = re.search(r"/catalog/(\d+)/", r["url"])
        sid = sid_by_nm.get(m.group(1)) if m else None
        if sid and names.get(sid) and names[sid] != "见商品页":
            r["shop"] = names[sid]
            r["seller_id"] = sid
            r["seller_url"] = f"https://www.wildberries.ru/seller/{sid}"
            r["note"] = "到手价为 WB Кошельком 价"
            filled += 1
    d["count"] = len(rows)
    DATA.write_text(json.dumps(d, ensure_ascii=False, indent=2), encoding="utf-8")
    render_html([Product(**r) for r in rows], d["generated_at"])
    print(f"[OK] 补全店铺名 {filled} 件；页面已刷新", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
