"""可被外部命令驱动的浏览器代理：读取 cmd.json 执行操作，结果写入 result.json。

用法：本脚本后台常驻；外部通过写入 cmd.json 发命令，读 result.json 取结果。
支持命令：goto / screenshot / click / click_text / fill / press / eval / body / wait / close
"""
import json
import time
from pathlib import Path

from playwright.sync_api import sync_playwright

ROOT = Path(__file__).resolve().parent
CMD = ROOT / "cmd.json"
RES = ROOT / "result.json"
PROFILE = ROOT / "feishu_profile"
PROFILE.mkdir(exist_ok=True)

result = {"status": "idle", "output": "", "id": ""}


def save():
    RES.write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


save()

with sync_playwright() as p:
    ctx = p.chromium.launch_persistent_context(
        user_data_dir=str(PROFILE),
        channel="chrome",
        headless=False,
        viewport={"width": 1440, "height": 900},
        args=["--no-sandbox", "--no-proxy-server",
              "--disable-blink-features=AutomationControlled"],
    )
    page = ctx.pages[0] if ctx.pages else ctx.new_page()
    result["status"] = "ready"
    result["output"] = "浏览器已就绪"
    save()

    while True:
        if CMD.exists():
            try:
                cmd = json.loads(CMD.read_text(encoding="utf-8"))
            except Exception as e:
                result = {"status": "error", "output": f"命令解析失败: {e}", "id": ""}
                save()
                CMD.write_text('{"cmd": "noop"}', encoding="utf-8")
                continue
            # 不删除命令文件（避免触发批量删除保护），用空命令覆盖表示已消费
            CMD.write_text('{"cmd": "noop"}', encoding="utf-8")
            op = cmd.get("cmd")
            if op == "noop":
                time.sleep(0.3)
                continue
            cmd_id = cmd.get("id", "")
            try:
                if op == "goto":
                    page.goto(cmd["url"], wait_until=cmd.get("wait", "domcontentloaded"),
                              timeout=cmd.get("timeout", 45000))
                    page.wait_for_timeout(cmd.get("delay", 2500))
                    out = f"URL={page.url} TITLE={page.title()}"
                elif op == "screenshot":
                    path = str(ROOT / cmd.get("path", "shot.png"))
                    page.screenshot(path=path, full_page=cmd.get("full", False))
                    out = path
                elif op == "click":
                    page.click(cmd["selector"], timeout=cmd.get("timeout", 10000))
                    page.wait_for_timeout(cmd.get("delay", 1500))
                    out = "clicked"
                elif op == "click_text":
                    page.get_by_text(cmd["text"], exact=cmd.get("exact", False)).first.click(
                        timeout=cmd.get("timeout", 10000))
                    page.wait_for_timeout(cmd.get("delay", 1500))
                    out = "clicked text"
                elif op == "fill":
                    page.fill(cmd["selector"], cmd["value"], timeout=10000)
                    out = "filled"
                elif op == "press":
                    page.press(cmd["selector"], cmd["key"])
                    out = "pressed"
                elif op == "eval":
                    out = page.evaluate(cmd["script"])
                elif op == "body":
                    out = page.locator("body").inner_text()[:cmd.get("limit", 3000)]
                elif op == "wait":
                    page.wait_for_timeout(cmd.get("ms", 5000))
                    out = "waited"
                elif op == "close":
                    result = {"status": "ok", "output": "closed", "id": cmd_id}
                    save()
                    break
                else:
                    out = f"未知命令 {op}"
                result = {"status": "ok", "output": str(out), "id": cmd_id}
            except Exception as e:
                result = {"status": "error", "output": f"{type(e).__name__}: {e}", "id": cmd_id}
            save()
        time.sleep(1)
    ctx.close()
