"""Ozon Seller API 竞品价格采集器（只读优先）。

官方 PricingStrategyAPI 能读到的竞品数据：
  - /v1/pricing-strategy/competitors/list  竞品卖家名单（含站外平台）
  - /v1/pricing-strategy/list              定价策略列表
  - /v1/pricing-strategy/info              策略详情（竞品 + 系数）
  - /v1/pricing-strategy/product/info      某个自己商品的竞品价与竞品商品链接
  - /v1/report/products/create + info      商品报告，含"另一市场最低价"及链接

重要边界：这是"定价策略里已配置的竞品池"，不是全站搜索。
若店铺尚未配置定价策略，脚本会报告为空，需先在后台建策略并添加商品。

凭证配置（不要硬编码进代码或文档）：
  1) 在 cosori_price_tool/.env 写入：
       OZON_CLIENT_ID=你的ClientId
       OZON_API_KEY=你的ApiKey
  2) 或设置同名环境变量。

用法：
  python ozon_seller.py                 # 只读探测 + 采集
  python ozon_seller.py --merge         # 采集后合并进 data/products.json 与 index.html
  python ozon_seller.py --setup         # 创建 COSORI 定价策略并添加商品（写操作，谨慎）
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import os
import sys
import time
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any

import requests

ROOT = Path(__file__).resolve().parent
DATA_DIR = ROOT / "data"
BASE = "https://api-seller.ozon.ru"
MSK_TZ = timezone(timedelta(hours=3))


def now_str() -> str:
    return datetime.now(MSK_TZ).strftime("%Y-%m-%d %H:%M:%S MSK")


def load_credentials() -> tuple[str, str]:
    """从环境变量或本地 .env 读取凭证，不写入代码。"""
    client_id = os.environ.get("OZON_CLIENT_ID", "")
    api_key = os.environ.get("OZON_API_KEY", "")
    env_file = ROOT / ".env"
    if env_file.exists() and (not client_id or not api_key):
        for line in env_file.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, val = line.split("=", 1)
            key, val = key.strip(), val.strip().strip('"').strip("'")
            if key == "OZON_CLIENT_ID" and not client_id:
                client_id = val
            elif key == "OZON_API_KEY" and not api_key:
                api_key = val
    return client_id, api_key


class OzonSeller:
    def __init__(self, client_id: str, api_key: str):
        self.session = requests.Session()
        self.session.headers.update({
            "Client-Id": client_id,
            "Api-Key": api_key,
            "Content-Type": "application/json",
            "User-Agent": "COSORI-Price-Monitor/1.0",
        })
        self.errors: list[str] = []

    def post(self, path: str, payload: dict[str, Any] | None = None,
             retries: int = 2) -> dict[str, Any] | None:
        url = BASE + path
        for attempt in range(retries + 1):
            try:
                r = self.session.post(url, json=payload or {}, timeout=40)
                if r.status_code == 200:
                    return r.json()
                if r.status_code == 429:
                    time.sleep(2 ** attempt)
                    continue
                self.errors.append(f"{path} HTTP {r.status_code}: {r.text[:200]}")
                return None
            except Exception as exc:
                self.errors.append(f"{path} {type(exc).__name__}: {exc}")
                if attempt == retries:
                    return None
                time.sleep(1)
        return None

    # ---------- 自有商品 ----------
    def own_products(self, keyword: str = "cosori") -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        last_id = ""
        all_ids: list[int] = []
        for _ in range(30):
            resp = self.post("/v3/product/list", {
                "filter": {"visibility": "ALL"},
                "last_id": last_id,
                "limit": 20,
            })
            if not resp:
                break
            result = resp.get("result") or {}
            batch = result.get("items") or []
            all_ids.extend(int(x["product_id"]) for x in batch if x.get("product_id"))
            last_id = result.get("last_id") or ""
            if not last_id or not batch:
                break
        # /v3/product/list 不返回名称，需用 info/list 补全
        for i in range(0, len(all_ids), 50):
            info = self.post("/v3/product/info/list", {"product_id": all_ids[i:i + 50]})
            for row in (info or {}).get("items") or []:
                name = str(row.get("name") or "")
                if keyword.lower() not in name.lower():
                    continue
                price = None
                prices = row.get("price") or {}
                if isinstance(prices, dict):
                    price = prices.get("price") or prices.get("price_without_vat")
                items.append({
                    "product_id": row.get("id") or row.get("product_id"),
                    "offer_id": row.get("offer_id"),
                    "name": name,
                    "price": price,
                })
        return items

    # ---------- 竞品卖家名单 ----------
    def competitors(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while page <= 20:
            resp = self.post("/v1/pricing-strategy/competitors/list",
                             {"page": page, "limit": 20})
            if not resp:
                break
            rows = resp.get("competitor") or []
            for row in rows:
                out.append({
                    "id": row.get("id") or row.get("competitor_id"),
                    "name": row.get("name") or row.get("competitor_name"),
                })
            total = int(resp.get("total") or 0)
            if len(rows) < 20 or len(out) >= total:
                break
            page += 1
        return out

    # ---------- 定价策略 ----------
    def strategies(self) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while page <= 20:
            resp = self.post("/v1/pricing-strategy/list", {"page": page, "limit": 20})
            if not resp:
                break
            rows = resp.get("strategies") or []
            out.extend(rows)
            total = int(resp.get("total") or 0)
            if len(rows) < 20 or len(out) >= total:
                break
            page += 1
        result = []
        for row in out:
            sid = row.get("id") or row.get("strategy_id")
            detail = self.post("/v1/pricing-strategy/info", {"strategy_id": sid})
            info = (detail or {}).get("result") or {}
            members = self.post("/v1/pricing-strategy/products/list", {"strategy_id": sid})
            pids = ((members or {}).get("result") or {}).get("product_id") or []
            result.append({
                "id": sid,
                "name": row.get("name") or info.get("strategy_name"),
                "type": row.get("type") or info.get("type"),
                "enabled": row.get("enabled", info.get("enabled")),
                "competitors": row.get("competitors") or info.get("competitors") or [],
                "product_ids": [str(x) for x in pids],
            })
        return result

    # ---------- 核心：竞品价 + 竞品链接 ----------
    def competitor_prices(self, product_ids: list[int | str]) -> list[dict[str, Any]]:
        out = []
        for pid in dict.fromkeys(product_ids):
            resp = self.post("/v1/pricing-strategy/product/info", {"product_id": int(pid)})
            result = (resp or {}).get("result") or {}
            if result:
                out.append({
                    "own_product_id": int(pid),
                    "strategy_id": result.get("strategy_id"),
                    "is_enabled": result.get("is_enabled"),
                    "strategy_price": result.get("strategy_product_price"),
                    "price_downloaded_at": result.get("price_downloaded_at"),
                    "competitor_id": result.get("strategy_competitor_id"),
                    "competitor_url": result.get("strategy_competitor_product_url"),
                })
        return out

    # ---------- 商品报告：另一市场最低价 ----------
    def product_report(self, keyword: str = "cosori") -> list[dict[str, Any]]:
        resp = self.post("/v1/report/products/create", {
            "language": "DEFAULT",
            "offer_id": [],
            "search": keyword,
            "sku": [],
            "visibility": "ALL",
        })
        code = ((resp or {}).get("result") or {}).get("code")
        if not code:
            return []
        for _ in range(20):
            info = self.post("/v1/report/info", {"code": code})
            result = (info or {}).get("result") or {}
            status = result.get("status")
            if status in ("done", "success"):
                url = result.get("file_url") or result.get("file")
                if not url:
                    return []
                try:
                    raw = requests.get(url, timeout=60).content
                except Exception as exc:
                    self.errors.append(f"report download: {exc}")
                    return []
                return self._parse_report(raw)
            if status in ("failed", "error"):
                self.errors.append(f"report {code} status={status}")
                return []
            time.sleep(3)
        self.errors.append(f"report {code} timeout")
        return []

    @staticmethod
    def _parse_report(raw: bytes) -> list[dict[str, Any]]:
        text = raw.decode("utf-8-sig", errors="ignore")
        if not text.strip():
            text = raw.decode("cp1251", errors="ignore")
        reader = csv.reader(io.StringIO(text), delimiter=";")
        rows = list(reader)
        if len(rows) < 2:
            reader = csv.reader(io.StringIO(text), delimiter=",")
            rows = list(reader)
        if len(rows) < 2:
            return []
        header = [h.strip().strip('"') for h in rows[0]]
        out = []
        for row in rows[1:]:
            if len(row) != len(header):
                continue
            item = dict(zip(header, [c.strip().strip('"') for c in row]))

            def pick(*needles: str) -> str:
                for k, v in item.items():
                    kl = k.lower()
                    if any(n in kl for n in needles):
                        return v
                return ""

            def num(raw: str) -> float | None:
                value = raw.replace("\u2009", "").replace(" ", "").replace(",", ".")
                try:
                    return float(value)
                except ValueError:
                    return None

            name = pick("название товара", "name")
            if not name:
                continue
            rec_price = pick("рекомендован", "recommend")
            rec_url = pick("ссыл", "link", "url")
            out.append({
                "offer_id": pick("артикул", "offer id"),
                "product_id": pick("ozon product id", "product id"),
                "name": name,
                "brand": pick("бренд", "brand"),
                "status": pick("статус товара", "status"),
                "price": num(pick("цена на сайте", "site price")),
                "original_price": num(pick("зачёркнутая цена", "old price")),
                "recommended_price": num(rec_price) if rec_price else None,
                "recommended_url": rec_url,
                "raw": item,
            })
        return out

    # ---------- 可选：写操作 ----------
    def setup_cosori_strategy(self, own_products: list[dict[str, Any]],
                              competitor_ids: list[int]) -> str:
        """创建竞品价策略并把 COSORI 商品加进去。仅在用户明确要求时调用。"""
        resp = self.post("/v1/pricing-strategy/create", {
            "strategy_name": "COSORI 竞品价监控",
            "competitors": [{"competitor_id": int(cid), "coefficient": 1}
                            for cid in competitor_ids],
        })
        sid = ((resp or {}).get("result") or {}).get("id")
        if not sid:
            raise RuntimeError(f"创建策略失败：{self.errors[-1] if self.errors else '未知错误'}")
        pids = [str(p["product_id"]) for p in own_products if p.get("product_id")]
        for i in range(0, len(pids), 50):
            self.post("/v1/pricing-strategy/products/add",
                      {"strategy_id": sid, "product_id": pids[i:i + 50]})
        return sid


def merge_into_dashboard(comp_data: dict[str, Any]) -> None:
    """把 Seller API 竞品价并入现有看板数据。"""
    from scrapers import Product, extract_models, now_str as pnow
    from scraper import render_html

    path = DATA_DIR / "products.json"
    if not path.exists():
        print("[跳过合并] 未找到 data/products.json")
        return
    payload = json.loads(path.read_text(encoding="utf-8"))
    rows = payload.get("products", [])
    # 移除上一轮写入的 Seller 数据，避免重复叠加
    rows = [r for r in rows
            if not (r.get("platform") == "ozon"
                    and r.get("note", "").startswith("来源：Ozon Seller"))]
    added = 0
    for item in comp_data.get("report_recommendations", []):
        price = item.get("price")
        if not price:
            continue
        title = item.get("name") or ""
        pid = item.get("product_id") or ""
        rows.append(Product(
            platform="ozon", platform_cn="Ozon（Seller 后台）",
            title=title, model=extract_models(title),
            shop="本店自售",
            price=float(price), original_price=item.get("original_price"),
            url=f"https://www.ozon.ru/product/{pid}/" if pid else "",
            seller_url="https://www.ozon.ru/seller/cosori-official/",
            status="ok",
            note="来源：Ozon Seller 后台商品报告；到手价以商品页显示为准",
            fetched_at=pnow(),
        ).to_dict())
        added += 1
    # 竞品价只在策略真正启用且价格有效时才写入
    for cp in comp_data.get("competitor_prices", []):
        price = cp.get("strategy_price")
        if not cp.get("is_enabled") or not price:
            continue
        rows.append(Product(
            platform="ozon", platform_cn="Ozon（Seller 竞品价）",
            title=f"Ozon 竞品价监控商品 {cp['own_product_id']}",
            model="—", shop=cp.get("competitor_url") or "见竞品链接",
            price=float(price), original_price=None,
            url=cp.get("competitor_url") or "", status="ok",
            note=(f"来源：Ozon Seller 定价策略官方竞品价；"
                  f"抓取时间 {cp.get('price_downloaded_at') or '未提供'}；"
                  f"该价格可能含策略系数"),
            fetched_at=pnow(),
        ).to_dict())
        added += 1
    payload["products"] = rows
    payload["count"] = len(rows)
    payload["ozon_seller"] = {
        "generated_at": comp_data.get("generated_at"),
        "competitor_count": len(comp_data.get("competitors", [])),
        "strategy_count": len(comp_data.get("strategies", [])),
        "competitors": comp_data.get("competitors", []),
        "strategies": comp_data.get("strategies", []),
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    render_html([Product(**r) for r in rows], payload["generated_at"])
    print(f"[合并] 新增 {added} 条 Ozon 竞品价记录")


def main() -> int:
    ap = argparse.ArgumentParser(description="Ozon Seller API 竞品价格采集")
    ap.add_argument("--merge", action="store_true", help="合并进看板")
    ap.add_argument("--setup", action="store_true",
                    help="创建 COSORI 定价策略并添加商品（写操作）")
    ap.add_argument("--keyword", default="cosori", help="自有商品关键词过滤")
    args = ap.parse_args()

    client_id, api_key = load_credentials()
    if not client_id or not api_key:
        print("缺少凭证。请在 cosori_price_tool/.env 写入 OZON_CLIENT_ID 与 OZON_API_KEY，")
        print("或设置同名环境变量。示例见 .env.example。")
        return 2

    api = OzonSeller(client_id, api_key)
    print("[1/5] 读取自有 COSORI 商品 ...", flush=True)
    own = api.own_products(args.keyword)
    print(f"  -> {len(own)} 件", flush=True)

    print("[2/5] 读取竞品卖家名单 ...", flush=True)
    comps = api.competitors()
    print(f"  -> {len(comps)} 家", flush=True)

    print("[3/5] 读取定价策略 ...", flush=True)
    strategies = api.strategies()
    print(f"  -> {len(strategies)} 个策略", flush=True)

    pids: list[int | str] = [p["product_id"] for p in own if p.get("product_id")]
    for s in strategies:
        pids.extend(s.get("product_ids") or [])
    print("[4/5] 读取竞品价格与竞品链接 ...", flush=True)
    prices = api.competitor_prices(pids)
    print(f"  -> {len(prices)} 条", flush=True)

    print("[5/5] 读取商品报告中的跨平台推荐价 ...", flush=True)
    report = api.product_report(args.keyword)
    print(f"  -> {len(report)} 条", flush=True)

    if args.setup and own and comps:
        try:
            sid = api.setup_cosori_strategy(own, [c["id"] for c in comps[:5]])
            print(f"[写操作] 已创建定价策略 {sid}，请到后台确认系数与最低价保护线")
        except Exception as exc:
            api.errors.append(f"setup: {exc}")

    payload = {
        "generated_at": now_str(),
        "own_products": own,
        "competitors": comps,
        "strategies": strategies,
        "competitor_prices": prices,
        "report_recommendations": report,
        "errors": api.errors,
    }
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    out = DATA_DIR / "ozon_seller.json"
    out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n已保存：{out}")
    if api.errors:
        print(f"接口报错 {len(api.errors)} 条（前 3 条）：")
        for e in api.errors[:3]:
            print("  -", e)
    if args.merge:
        merge_into_dashboard(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
