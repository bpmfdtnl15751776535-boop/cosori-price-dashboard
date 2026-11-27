"""飞书用户身份授权：用 OAuth code 换取 user_access_token，并支持 refresh_token 续期。

用法:
  python feishu_oauth.py <authorization_code>   # 首次换取 token
  python feishu_oauth.py --refresh              # 用已保存的 refresh_token 续期
  python feishu_oauth.py --check                # 检查当前 token 有效性
"""
import json
import sys
import time
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent
ENV = ROOT / ".env"
TOKEN_FILE = ROOT / "feishu_user_token.json"


def load_env():
    cfg = {}
    for line in ENV.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        cfg[k.strip()] = v.strip()
    return cfg


cfg = load_env()
APP_ID = cfg["FEISHU_APP_ID"]
APP_SECRET = cfg["FEISHU_APP_SECRET"]


def app_access_token():
    r = requests.post(
        "https://open.feishu.cn/open-apis/auth/v3/app_access_token/internal",
        json={"app_id": APP_ID, "app_secret": APP_SECRET},
        timeout=20,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"获取 app_access_token 失败: {data}")
    return data["app_access_token"]


def save_token(data):
    TOKEN_FILE.write_text(
        json.dumps(
            {
                "access_token": data["access_token"],
                "refresh_token": data.get("refresh_token", ""),
                "expires_in": data.get("expires_in", 7200),
                "obtained_at": time.time(),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"已保存用户凭证到 {TOKEN_FILE.name}，有效期 {data.get('expires_in')} 秒")


def get_code():
    if not TOKEN_FILE.exists():
        raise RuntimeError("尚无用户凭证，请先用授权码换取")
    d = json.loads(TOKEN_FILE.read_text(encoding="utf-8"))
    return d["access_token"], d.get("refresh_token", "")


def main():
    args = sys.argv[1:]
    if args and args[0] == "--check":
        token, refresh = get_code()
        h = {"Authorization": f"Bearer {token}"}
        url = (f"https://open.feishu.cn/open-apis/bitable/v1/apps/"
               f"{cfg['FEISHU_APP_TOKEN']}/tables/{cfg['FEISHU_TABLE_ID']}/records?page_size=1")
        r = requests.get(url, headers=h, timeout=20)
        print("读取表格状态码:", r.status_code, r.json().get("code"), r.json().get("msg"))
        return

    if args and args[0] == "--refresh":
        token, refresh = get_code()
        if not refresh:
            raise RuntimeError("没有 refresh_token，需重新授权")
    elif args:
        code = args[0]
        refresh = ""
        at = app_access_token()
        r = requests.post(
            "https://open.feishu.cn/open-apis/authen/v1/access_token",
            headers={"Authorization": f"Bearer {at}"},
            json={"grant_type": "authorization_code", "code": code},
            timeout=20,
        )
        data = r.json()
        if data.get("code") != 0:
            raise RuntimeError(f"换取 user_access_token 失败: {data}")
        save_token(data["data"])
        print("授权成功，用户:", data["data"].get("user_id", ""))
        return
    else:
        raise RuntimeError("用法: feishu_oauth.py <code> | --refresh | --check")

    at = app_access_token()
    r = requests.post(
        "https://open.feishu.cn/open-apis/authen/v1/refresh_access_token",
        headers={"Authorization": f"Bearer {at}"},
        json={"grant_type": "refresh_token", "refresh_token": refresh},
        timeout=20,
    )
    data = r.json()
    if data.get("code") != 0:
        raise RuntimeError(f"刷新凭证失败: {data}")
    save_token(data["data"])


if __name__ == "__main__":
    main()
