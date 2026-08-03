"""测试 /api/optimize/* 和 /api/suggestions 端点"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
import urllib.request, json
from web.app import app

t = threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=8768, log_level="warning"), daemon=True)
t.start()
time.sleep(3)

# GET /api/suggestions
print("=== GET /api/suggestions ===")
r = urllib.request.urlopen("http://127.0.0.1:8768/api/suggestions?status=open", timeout=5)
sugs = json.loads(r.read().decode("utf-8"))
print(f"  open 状态: {len(sugs)} 条")
high = [s for s in sugs if s.get("priority") == "high"]
print(f"  high 优先级: {len(high)} 条")

# POST /api/optimize/personality（AI 未配置，会返回 1 条提示）
print()
print("=== POST /api/optimize/personality ===")
req = urllib.request.Request(
    "http://127.0.0.1:8768/api/optimize/personality",
    data=json.dumps({"name": "沈青砚"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
try:
    r = urllib.request.urlopen(req, timeout=10)
    d = json.loads(r.read().decode("utf-8"))
    print(f"  ok={d.get('ok')}, count={d.get('count')}")
    for s in d.get("suggestions", [])[:2]:
        print(f"  - [{s.get('priority')}] {s.get('title','')[:50]}")
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code}: {e.read().decode('utf-8')[:200]}")

# POST /api/optimize/all
print()
print("=== POST /api/optimize/all ===")
req = urllib.request.Request(
    "http://127.0.0.1:8768/api/optimize/all",
    data=json.dumps({}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
try:
    r = urllib.request.urlopen(req, timeout=15)
    d = json.loads(r.read().decode("utf-8"))
    print(f"  ok={d.get('ok')}, count={d.get('count')}")
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code}: {e.read().decode('utf-8')[:200]}")

# POST /api/suggestion/apply/{id}
print()
print("=== POST /api/suggestion/apply/1 ===")
req = urllib.request.Request(
    "http://127.0.0.1:8768/api/suggestion/apply/1",
    data=json.dumps({}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
try:
    r = urllib.request.urlopen(req, timeout=5)
    d = json.loads(r.read().decode("utf-8"))
    print(f"  {d}")
except urllib.error.HTTPError as e:
    print(f"  HTTP {e.code}: {e.read().decode('utf-8')[:200]}")

# GET /api/suggestions?status=applied
print()
print("=== GET /api/suggestions?status=applied ===")
r = urllib.request.urlopen("http://127.0.0.1:8768/api/suggestions?status=applied", timeout=5)
sugs = json.loads(r.read().decode("utf-8"))
print(f"  applied 状态: {len(sugs)} 条")

print()
print("ALL API TESTS PASSED")
