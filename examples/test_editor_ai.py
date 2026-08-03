import sys, time, threading, urllib.request, json
sys.path.insert(0, ".")
import uvicorn
from web.app import app
t = threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=8774, log_level="warning"), daemon=True)
t.start()
time.sleep(3)

# 测 SSE 流式 AI 修改
print("POST /api/editor/chapter/1/ai-edit (SSE)...")
text = "令狐冲第一次来陆家嘴是大学三年级的暑假。"
req = urllib.request.Request(
    "http://127.0.0.1:8774/api/editor/chapter/1/ai-edit",
    data=json.dumps({"instruction": "把这段开头改得更冷峻有文学感", "current_text": text}).encode("utf-8"),
    headers={"Content-Type": "application/json"},
)
t0 = time.time()
try:
    r = urllib.request.urlopen(req, timeout=60)
    chunks = 0
    full = ""
    for line in r:
        line = line.decode("utf-8").strip()
        if line.startswith("data: "):
            try:
                d = json.loads(line[6:])
                if d.get("chunk"):
                    chunks += 1
                    full += d["chunk"]
                elif d.get("done"):
                    print(f"  done signal, total {len(full)} chars")
                elif d.get("error"):
                    print(f"  ERROR: {d['error']}")
            except: pass
    elapsed = time.time() - t0
    print(f"  ✓ Received {chunks} chunks in {elapsed:.1f}s")
    print(f"  AI 改写 ({len(full)} 字符):")
    print(f"  {full[:200]}{'...' if len(full)>200 else ''}")
except Exception as e:
    print(f"  ERR: {e}")
