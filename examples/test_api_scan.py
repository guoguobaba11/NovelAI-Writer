import threading, time, urllib.request, json
import uvicorn
from web.app import app

t = threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=8766, log_level="warning"), daemon=True)
t.start()
time.sleep(3)

for url in [
    "/api/scan/threads",
    "/api/scan/logic",
    "/api/scan/style?baseline=3&threshold=2",
    "/api/volumes",
    "/api/recent_issues?limit=5",
]:
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8766" + url, timeout=5)
        d = json.loads(r.read().decode("utf-8"))
        if isinstance(d, dict) and "summary" in d:
            print(f"{url:50s} OK summary={d['summary']}")
        elif isinstance(d, dict) and "issues" in d:
            print(f"{url:50s} OK issues={len(d.get('issues',[]))}")
        elif isinstance(d, list):
            print(f"{url:50s} OK list_len={len(d)}")
        elif isinstance(d, dict) and "drift_issues" in d:
            print(f"{url:50s} OK drift={len(d.get('drift_issues',[]))} curve_pts={len(d.get('overall_drift_curve',[]))}")
        else:
            print(f"{url:50s} OK keys={list(d.keys())[:5]}")
    except Exception as e:
        print(f"{url:50s} ERR {e}")

# 测试 import 端点
print()
print("=== 测试 /api/import ===")
req = urllib.request.Request(
    "http://127.0.0.1:8766/api/import",
    data=json.dumps({"path": "examples/test_manuscript.md", "title": "测试书2"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
r = urllib.request.urlopen(req, timeout=10)
print("import response:", r.read().decode("utf-8")[:200])
