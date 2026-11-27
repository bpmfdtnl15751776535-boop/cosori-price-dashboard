"""向常驻浏览器代理发送一条命令并等待结果。用法: python bcmd.py '<json>'"""
import json
import sys
import time
import uuid
from pathlib import Path

root = Path(__file__).resolve().parent
cmd_file = root / "cmd.json"
res_file = root / "result.json"

payload = json.loads(sys.argv[1])
cmd_id = uuid.uuid4().hex
payload["id"] = cmd_id
res_file.write_text(json.dumps({"status": "busy", "output": "", "id": ""}, ensure_ascii=False), encoding="utf-8")
cmd_file.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

for _ in range(120):
    time.sleep(0.5)
    try:
        result = json.loads(res_file.read_text(encoding="utf-8"))
    except Exception:
        continue
    if result.get("status") in ("ok", "error") and result.get("id") == cmd_id:
        print(json.dumps(result, ensure_ascii=False))
        break
else:
    print(json.dumps({"status": "timeout", "output": "等待浏览器代理超时"}, ensure_ascii=False))
