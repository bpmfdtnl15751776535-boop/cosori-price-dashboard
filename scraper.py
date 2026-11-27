"""一键采集三平台 COSORI 空气炸锅价格，并生成可离线打开的前端页面。

用法：
  python scraper.py                        # 默认采集所有平台
  python scraper.py --pages 3              # 每平台多翻几页
  python scraper.py --proxy http://user:pass@host:port   # 使用住宅代理绕过反爬
  python scraper.py --skip-wildberries --skip-ozon       # 只采集可用平台

输出：
  data/products.json   结构化数据
  index.html           带内嵌数据的展示页（双击即可打开）
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

from scrapers import Product, scrape_wildberries, scrape_yandex, scrape_ozon, now_str

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"


def safe_json(value) -> str:
    return (json.dumps(value, ensure_ascii=False, separators=(",", ":"))
            .replace("</", "<\\/"))


def render_html(products: list[Product], generated_at: str) -> Path:
    template = (ROOT / "index.template.html").read_text(encoding="utf-8")
    html = (template
            .replace("/*__PRODUCTS__*/[]", safe_json([p.to_dict() for p in products]))
            .replace("__GENERATED_AT__", generated_at))
    out = ROOT / "index.html"
    out.write_text(html, encoding="utf-8")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="COSORI 空气炸锅三平台价格采集")
    ap.add_argument("--pages", type=int, default=1, help="每平台抓取页数（默认 1）")
    ap.add_argument("--proxy", default=os.environ.get("COSORI_PROXY"),
                    help="代理地址，如 http://user:pass@host:port（也可设 COSORI_PROXY 环境变量）")
    ap.add_argument("--skip-wildberries", action="store_true")
    ap.add_argument("--skip-yandex", action="store_true")
    ap.add_argument("--skip-ozon", action="store_true")
    args = ap.parse_args()

    generated_at = now_str()
    all_products: list[Product] = []

    jobs = [
        ("Wildberries", args.skip_wildberries, lambda: scrape_wildberries(pages=args.pages, proxy=args.proxy)),
        ("Yandex Market", args.skip_yandex, lambda: scrape_yandex(pages=args.pages)),
        ("Ozon", args.skip_ozon, lambda: scrape_ozon(pages=args.pages, proxy=args.proxy)),
    ]
    for name, skip, fn in jobs:
        if skip:
            print(f"[跳过] {name}")
            continue
        print(f"[采集] {name} ...", flush=True)
        try:
            items = fn()
        except Exception as exc:
            items = [Product(platform=name.lower().split()[0], platform_cn=name,
                             title="—", model="—", shop="—", price=None,
                             original_price=None, url="", status="error",
                             note=f"采集异常：{type(exc).__name__}: {exc}")]
        print(f"  -> {len(items)} 条"
              + (f"（状态：{items[0].status}）" if items else ""), flush=True)
        all_products.extend(items)

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "query": "COSORI 空气炸锅",
        "generated_at": generated_at,
        "generated_iso": datetime.now(timezone.utc).isoformat(),
        "count": len(all_products),
        "products": [p.to_dict() for p in all_products],
    }
    json_path = DATA_DIR / "products.json"
    json_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    html_path = render_html(all_products, generated_at)

    ok = [p for p in all_products if p.status == "ok" and p.price]
    print(f"\n完成：有效商品 {len(ok)} 条 / 总计 {len(all_products)} 条")
    print(f"数据：{json_path}")
    print(f"页面：{html_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
