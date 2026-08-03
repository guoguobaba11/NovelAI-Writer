import sys, time, threading, urllib.request, json
sys.path.insert(0, ".")
import uvicorn
from web.app import app
t = threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=8773, log_level="warning"), daemon=True)
t.start()
time.sleep(3)

r = urllib.request.urlopen("http://127.0.0.1:8773/api/editor/chapter/1", timeout=5)
d = json.loads(r.read().decode("utf-8"))
print("GET /api/editor/chapter/1:")
print(f"  title: {d['chapter']['title']}")
print(f"  text_len: {len(d['text'])}")
print(f"  prev: {d['prev_idx']}, next: {d['next_idx']}")
print(f"  n_events: {len(d['events'])}, n_threads: {len(d['threads'])}")
print(f"  characters: {len(d['characters'])}")

# /api/editor/chapter/1/analyze
req = urllib.request.Request(
    "http://127.0.0.1:8773/api/editor/chapter/1/analyze",
    data=json.dumps({"text": d["text"]}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
r = urllib.request.urlopen(req, timeout=10)
d2 = json.loads(r.read().decode("utf-8"))
print()
print("POST /api/editor/chapter/1/analyze:")
print(f"  n_issues: {d2.get('n_issues')}")
print(f"  by_severity: {d2.get('by_severity')}")
