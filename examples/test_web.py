"""测试启动 uvicorn 在后台，并调用 API"""
import sys
import time
import threading
import urllib.request
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

import uvicorn
from web.app import app


def start_server():
    uvicorn.run(app, host="127.0.0.1", port=8765, log_level="warning")


t = threading.Thread(target=start_server, daemon=True)
t.start()
time.sleep(3)

print("=== 探测 /api/project ===")
r = urllib.request.urlopen("http://127.0.0.1:8765/api/project", timeout=5)
print(r.status, r.read().decode("utf-8"))

print()
print("=== 探测 /api/progress ===")
r = urllib.request.urlopen("http://127.0.0.1:8765/api/progress", timeout=5)
data = json.loads(r.read().decode("utf-8"))
print(f"  total_chapters={data['total_chapters']} written={data['written_chapters']} words={data['total_words']}")
print(f"  current_chapter={data['current_chapter']['title'] if data['current_chapter'] else None}")
print(f"  current_story_time={data['current_story_time']}")
print(f"  thread_stats={data['thread_stats']}")

print()
print("=== 探测 /api/timeline ===")
r = urllib.request.urlopen("http://127.0.0.1:8765/api/timeline", timeout=5)
data = json.loads(r.read().decode("utf-8"))
print(f"  chapter_ranges={len(data['chapter_ranges'])}")
print(f"  event_points={len(data['event_points'])}")
print(f"  thread_marks={len(data['thread_marks'])}")

print()
print("=== 探测 /api/rhythm ===")
r = urllib.request.urlopen("http://127.0.0.1:8765/api/rhythm", timeout=5)
data = json.loads(r.read().decode("utf-8"))
print(f"  idx={data['idx']}")
print(f"  words={data['words']}")
print(f"  event_count={data['event_count']}")

print()
print("=== 探测 /api/relationship_network ===")
r = urllib.request.urlopen("http://127.0.0.1:8765/api/relationship_network", timeout=5)
data = json.loads(r.read().decode("utf-8"))
print(f"  nodes={len(data['nodes'])} edges={len(data['edges'])}")

print()
print("=== 探测 / (index.html) ===")
r = urllib.request.urlopen("http://127.0.0.1:8765/", timeout=5)
content = r.read().decode("utf-8")
print(f"  status={r.status} length={len(content)}")
print(f"  has ECharts: {'echarts' in content.lower()}")
print(f"  has vis-network: {'vis-network' in content.lower()}")
print(f"  has app.js: {'/static/app.js' in content}")

print()
print("ALL API TESTS PASSED")
