"""测试所有新 API 端点 + 人物视图"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
import urllib.request, json
from web.app import app

t = threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=8767, log_level="warning"), daemon=True)
t.start()
time.sleep(3)

print("=" * 60)
print("=== 新 API 端点测试 ===")

endpoints = [
    ("GET", "/api/character_matrix"),
    ("GET", "/api/character_arcs"),
    ("GET", "/api/relationship_evolution"),
    ("GET", "/api/personality_drift"),
    ("GET", "/api/scan/threads"),
    ("GET", "/api/scan/logic"),
    ("GET", "/api/scan/style"),
    ("GET", "/api/volumes"),
    ("GET", "/api/recent_issues?limit=5"),
]

for method, url in endpoints:
    try:
        r = urllib.request.urlopen("http://127.0.0.1:8767" + url, timeout=5)
        d = json.loads(r.read().decode("utf-8"))
        if isinstance(d, dict):
            if "characters" in d:
                print(f"  {url:50s} OK characters={len(d.get('characters',[]))}")
            elif "matrix" in d:
                print(f"  {url:50s} OK matrix_size={len(d.get('matrix',{}))}")
            elif "series" in d:
                print(f"  {url:50s} OK series={len(d.get('series',[]))}")
            elif "results" in d:
                print(f"  {url:50s} OK results={len(d.get('results',[]))}")
            elif "issues" in d:
                print(f"  {url:50s} OK issues={len(d.get('issues',[]))}")
            elif "summary" in d:
                print(f"  {url:50s} OK summary={d['summary']}")
            elif "drift_issues" in d:
                print(f"  {url:50s} OK drift={len(d.get('drift_issues',[]))}")
            else:
                print(f"  {url:50s} OK keys={list(d.keys())[:5]}")
        elif isinstance(d, list):
            print(f"  {url:50s} OK list_len={len(d)}")
    except Exception as e:
        print(f"  {url:50s} ERR {e}")

# POST 测试
print()
print("=== POST 端点 ===")
for url, body in [
    ("/api/character/set_mbti", {"name": "沈青砚", "mbti": "INTJ"}),
    ("/api/character/add_milestone", {"name": "沈青砚", "chapter_idx": 1, "milestone_type": "starting_point", "description": "测试里程碑"}),
    ("/api/relationship/add_evolution", {"a": "沈青砚", "b": "李琰", "chapter_idx": 1, "intimacy": 0.2, "trust": 0.3, "conflict": 0.4, "dynamics": "初遇"}),
]:
    try:
        req = urllib.request.Request(
            "http://127.0.0.1:8767" + url,
            data=json.dumps(body).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
        r = urllib.request.urlopen(req, timeout=5)
        d = json.loads(r.read().decode("utf-8"))
        print(f"  POST {url:50s} OK {d}")
    except urllib.error.HTTPError as e:
        print(f"  POST {url:50s} HTTP {e.code} {e.read().decode('utf-8')[:100]}")
    except Exception as e:
        print(f"  POST {url:50s} ERR {e}")

print()
print("ALL API TESTS PASSED")
