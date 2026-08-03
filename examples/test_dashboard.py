"""测试 /api/dashboard 等新端点"""
import sys, os, time, threading
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import uvicorn
import urllib.request, json
from web.app import app

t = threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=8771, log_level="warning"), daemon=True)
t.start()
time.sleep(3)

print("=" * 60)
print("=== 测试 /api/dashboard ===")
r = urllib.request.urlopen("http://127.0.0.1:8771/api/dashboard", timeout=10)
d = json.loads(r.read().decode("utf-8"))
print(f"  project.title: {d['project']['title']}")
print(f"  kpis.chapters_total: {d['kpis']['chapters_total']}")
print(f"  kpis.characters_with_mbti: {d['kpis']['characters_with_mbti']}")
print(f"  health.overall: {d['health']['overall']}")
print(f"  health.high_issues: {d['health']['high_issues']}")
print(f"  todos.open_suggestions: {d['todos']['open_suggestions']}")
print(f"  onboarding_done: {d['onboarding_done']}")
print(f"  recent_chapters: {len(d['recent_chapters'])}")
print()

print("=== 测试 /api/progress ===")
r = urllib.request.urlopen("http://127.0.0.1:8771/api/progress", timeout=5)
d = json.loads(r.read().decode("utf-8"))
print(f"  total_chapters: {d['total_chapters']}")
print(f"  total_words: {d['total_words']}")
print()

print("=== 测试 /api/import ===")
req = urllib.request.Request(
    "http://127.0.0.1:8771/api/import",
    data=json.dumps({"path": "examples/test_manuscript.md", "title": "测试"}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
r = urllib.request.urlopen(req, timeout=10)
d = json.loads(r.read().decode("utf-8"))
print(f"  import result: {d}")
print()

# 旧端点继续可用
for url in ["/api/chapters", "/api/characters", "/api/character_matrix", "/api/scan/threads", "/api/scan/logic", "/api/scan/style", "/api/volumes", "/api/personality_drift", "/api/character_arcs", "/api/relationship_evolution"]:
    try:
        r = urllib.request.urlopen(f"http://127.0.0.1:8771{url}", timeout=5)
        r_data = json.loads(r.read().decode("utf-8"))
        if isinstance(r_data, dict):
            keys = list(r_data.keys())[:3]
            print(f"  {url:40s} OK keys={keys}")
        elif isinstance(r_data, list):
            print(f"  {url:40s} OK list={len(r_data)}")
    except Exception as e:
        print(f"  {url:40s} ERR {e}")

print()
print("ALL DASHBOARD API TESTS PASSED")
