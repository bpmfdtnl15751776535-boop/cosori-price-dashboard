"""把采集结果推送到飞书：多维表格 + 群机器人卡片。

配置（环境变量或 .env 文件）：
  FEISHU_APP_ID / FEISHU_APP_SECRET  飞书自建应用凭证（多维表格需要）
  FEISHU_APP_TOKEN / FEISHU_TABLE_ID 多维表格 app_token 与 table_id
  FEISHU_WEBHOOK                    群机器人 webhook（推送卡片，最简方案）
  FEISHU_WEBHOOK_SECRET             可选，签名校验密钥

用法：
  python push_feishu.py --test                     # 只推送测试消息
  python push_feishu.py --mode card                # 推群卡片
  python push_feishu.py --mode bitable             # 写入多维表格
  python push_feishu.py --mode both                # 两者都做
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import hmac
import json
import os
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data" / "products.json"
USER_TOKEN_FILE = ROOT / "feishu_user_token.json"
TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal"
APP_TOKEN_URL = "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal"
REFRESH_URL = "https://open.feishu.cn/open-apis/authen/v1/refresh_access_token"
BITABLE_URL = ("https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
               "/tables/{table_id}/records/batch_create")
RECORDS_URL = ("https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
               "/tables/{table_id}/records")
DELETE_URL = ("https://open.feishu.cn/open-apis/bitable/v1/apps/{app_token}"
              "/tables/{table_id}/records/batch_delete")


def _load_dotenv() -> None:
    """把 .env 中的 KEY=VALUE 补进环境变量（已存在的环境变量优先）。"""
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, val = line.split("=", 1)
        key, val = key.strip(), val.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = val


_load_dotenv()


def env(name: str, default: str = "") -> str:
    return os.environ.get(name, default)


def load_products() -> tuple[list[dict], str]:
    d = json.loads(DATA.read_text(encoding="utf-8"))
    return [p for p in d["products"] if p["status"] == "ok"], d.get("generated_at", "")


def fmt_price(v):
    return f"{v:,.0f} ₽" if isinstance(v, (int, float)) else "—"


def tenant_token() -> str:
    app_id, app_secret = env("FEISHU_APP_ID"), env("FEISHU_APP_SECRET")
    if not app_id or not app_secret:
        raise RuntimeError("缺少 FEISHU_APP_ID / FEISHU_APP_SECRET")
    r = requests.post(TOKEN_URL, json={"app_id": app_id, "app_secret": app_secret}, timeout=20)
    r.raise_for_status()
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 token 失败：{data}")
    return data["tenant_access_token"]


def app_token() -> str:
    """企业自建应用凭证，用于刷新用户 token。"""
    r = requests.post(
        APP_TOKEN_URL,
        json={"app_id": env("FEISHU_APP_ID"), "app_secret": env("FEISHU_APP_SECRET")},
        timeout=20,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取应用凭证失败：{data}")
    return data["app_access_token"]


def user_token() -> str:
    """返回可用的用户身份凭证；即将过期时自动用 refresh_token 续期。

    多维表格的数据权限无法授予自建应用身份，因此改用用户身份调用 API。
    首次授权：python feishu_oauth.py <授权码>
    """
    if not USER_TOKEN_FILE.exists():
        raise RuntimeError(
            "缺少用户授权凭证，请先运行 OAuth 授权（python feishu_oauth.py <code>）"
        )
    saved = json.loads(USER_TOKEN_FILE.read_text(encoding="utf-8"))
    age = time.time() - float(saved.get("obtained_at", 0))
    expires = int(saved.get("expires_in", 7200))
    if age < expires - 300 and saved.get("access_token"):
        return saved["access_token"]

    refresh = saved.get("refresh_token")
    if not refresh:
        raise RuntimeError("用户凭证已过期且没有 refresh_token，请重新授权")
    at = app_token()
    r = requests.post(
        REFRESH_URL,
        headers={"Authorization": f"Bearer {at}"},
        json={"grant_type": "refresh_token", "refresh_token": refresh},
        timeout=20,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"刷新用户凭证失败（需重新授权）：{data}")
    new = data["data"]
    USER_TOKEN_FILE.write_text(
        json.dumps(
            {
                "access_token": new["access_token"],
                "refresh_token": new.get("refresh_token", refresh),
                "expires_in": new.get("expires_in", 7200),
                "obtained_at": time.time(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print("[凭证] 用户访问凭证已自动续期")
    return new["access_token"]


def clear_old_records(headers: dict, app_token: str, table_id: str) -> int:
    """删除本工具此前写入的记录，避免每日重复堆积。

    通过「平台」字段识别，仅删除值以 Wildberries / Yandex / Ozon 开头的记录。
    """
    url = RECORDS_URL.format(app_token=app_token, table_id=table_id)
    to_delete: list[str] = []
    page_token = ""
    while True:
        params: dict[str, str | int] = {"page_size": 100}
        if page_token:
            params["page_token"] = page_token
        r = requests.get(url, headers=headers, params=params, timeout=30)
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"读取现有记录失败：{data}")
        for item in data.get("data", {}).get("items") or []:
            fields = item.get("fields") or {}
            platform = str(fields.get("平台") or "")
            if platform.startswith(("Wildberries", "Yandex", "Ozon")):
                to_delete.append(item["record_id"])
        if not data.get("data", {}).get("has_more"):
            break
        page_token = data["data"].get("page_token") or ""
        if not page_token:
            break
    delete_url = DELETE_URL.format(app_token=app_token, table_id=table_id)
    for i in range(0, len(to_delete), 100):
        r = requests.post(delete_url, headers=headers,
                          json={"records": to_delete[i:i + 100]}, timeout=30)
        if r.json().get("code") != 0:
            raise RuntimeError(f"清理旧记录失败：{r.text[:200]}")
    return len(to_delete)


def push_bitable(products: list[dict], replace: bool = True) -> int:
    token = user_token()
    app_token, table_id = env("FEISHU_APP_TOKEN"), env("FEISHU_TABLE_ID")
    if not app_token or not table_id:
        raise RuntimeError("缺少 FEISHU_APP_TOKEN / FEISHU_TABLE_ID")
    url = BITABLE_URL.format(app_token=app_token, table_id=table_id)
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    if replace:
        removed = clear_old_records(headers, app_token, table_id)
        if removed:
            print(f"[清理] 已删除本工具旧记录 {removed} 条")

    # 列名需与多维表格中创建的字段完全一致。
    # 当前表格字段：平台/型号/商品名称/店铺/到手价/商品链接/采集日期（无"原价"列）。
    # 采集日期是 DateTime 类型，必须传毫秒时间戳（int），不能传字符串。
    def _to_ms(s: str) -> int | None:
        import re
        from datetime import datetime, timezone
        if not s:
            return None
        # 形如 "2026-11-26 12:06:00 MSK"，MSK=UTC+3
        m = re.search(r"(\d{4})-(\d{2})-(\d{2})[ T](\d{2}):(\d{2}):(\d{2})", str(s))
        if not m:
            return None
        try:
            dt = datetime(*[int(x) for x in m.groups()])
            return int(dt.replace(tzinfo=timezone.utc).timestamp() * 1000)
        except ValueError:
            return None

    records = []
    for p in products:
        fields = {
            "平台": p["platform_cn"],
            "型号": p["model"],
            "商品名称": p["title"],
            "店铺": p.get("shop") or "见商品页",
            "到手价": p["price"],
            # 飞书 URL 类型字段必须传 {"link": ..., "text": ...} 对象
            "商品链接": {"link": p["url"], "text": p["url"]} if p.get("url") else None,
            "采集日期": _to_ms(p["fetched_at"]),
        }
        records.append({"fields": {k: v for k, v in fields.items() if v is not None}})

    pushed = 0
    for i in range(0, len(records), 100):  # 飞书批量接口每批最多 1000，保守取 100
        batch = records[i:i+100]
        r = requests.post(url, headers=headers, json={"records": batch}, timeout=30)
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"写入多维表格失败：{data}")
        pushed += len(batch)
    return pushed


def sign(secret: str, timestamp: int) -> str:
    s = f"{timestamp}\n{secret}"
    h = hmac.new(s.encode("utf-8"), digestmod=hashlib.sha256).digest()
    return base64.b64encode(h).decode("utf-8")


def push_card(products: list[dict], generated_at: str) -> None:
    webhook = env("FEISHU_WEBHOOK")
    if not webhook:
        raise RuntimeError("缺少 FEISHU_WEBHOOK")
    by_platform = {}
    for p in products:
        by_platform.setdefault(p["platform_cn"], []).append(p)
    cheapest = sorted([p for p in products if p["price"]], key=lambda x: x["price"])[:5]

    lines = [f"**采集时间**：{generated_at}", f"**有效商品**：{len(products)} 件", ""]
    for platform, rows in by_platform.items():
        lo = min((r["price"] for r in rows if r["price"]), default=None)
        lines.append(f"• **{platform}**：{len(rows)} 件，最低 {fmt_price(lo)}")
    lines += ["", "**各型号最低到手价 TOP5**"]
    for p in cheapest:
        shop = p.get("shop") or "见商品页"
        lines.append(f"• [{p['model']}] {fmt_price(p['price'])} · {shop} · [商品]({p['url']})")

    # 用 markdown 超链接代替 action 按钮：飞书群机器人 webhook 发送的卡片，
    # action 按钮会被当成「卡片交互」回调，需要给应用配置卡片交互能力，否则点击报
    # 「该应用尚未配置卡片交互功能」。lark_md 里的普通链接点击直接跳转，无需交互配置。
    dashboard = env("DASHBOARD_URL", "")
    if dashboard:
        lines += ["", f"👉 [打开完整看板（全部 {len(products)} 件）]({dashboard})"]

    payload = {
        "msg_type": "interactive",
        "card": {
            "config": {"wide_screen_mode": True},
            "header": {
                "title": {"tag": "plain_text", "content": "COSORI 空气炸锅 · 每日到手价"},
                "template": "blue",
            },
            "elements": [
                {"tag": "div", "text": {"tag": "lark_md", "content": "\n".join(lines)}},
            ],
        },
    }
    secret = env("FEISHU_WEBHOOK_SECRET")
    if secret:
        ts = int(time.time())
        payload["timestamp"] = str(ts)
        payload["sign"] = sign(secret, ts)
    r = requests.post(webhook, json=payload, timeout=20)
    data = r.json()
    if data.get("StatusCode") not in (0, None) or data.get("code") not in (0, None):
        raise RuntimeError(f"推送卡片失败：{data}")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["card", "bitable", "both"], default="card")
    ap.add_argument("--test", action="store_true", help="发送测试消息")
    ap.add_argument("--append", action="store_true",
                    help="追加写入而不清理旧记录（默认先清理，避免重复堆积）")
    args = ap.parse_args()

    products, generated_at = load_products()
    if args.test:
        products = products[:3]
    if args.mode in ("card", "both"):
        push_card(products, generated_at)
        print(f"[OK] 飞书卡片已推送（{len(products)} 件）")
    if args.mode in ("bitable", "both"):
        try:
            n = push_bitable(products, replace=not args.append)
            print(f"[OK] 多维表格已写入 {n} 条")
        except RuntimeError as e:
            # 用户凭证过期/被占用时不要让整个流程失败；群卡片已照常推送
            msg = str(e)
            if "刷新用户凭证失败" in msg or "重新授权" in msg or "refresh_token" in msg:
                print("[WARN] 多维表格写入跳过：飞书用户凭证需重新 OAuth 授权。"
                      "方法见 FEISHU_SETUP_GUIDE.md 的「用户身份授权」章节")
                print(f"[WARN] 详情：{msg[:200]}")
            else:
                raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
