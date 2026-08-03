import sys, time, threading, urllib.request, json
sys.path.insert(0, ".")
import uvicorn
from web.app import app
t = threading.Thread(target=lambda: uvicorn.run(app, host="127.0.0.1", port=8772, log_level="warning"), daemon=True)
t.start()
time.sleep(3)
r = urllib.request.urlopen("http://127.0.0.1:8772/api/pipeline/quick", timeout=10)
d = json.loads(r.read().decode("utf-8"))
print("quick OK:")
print(f"  total issues: {sum(v['count'] for v in d['issues_by_category'].values())}")
for k, v in d['issues_by_category'].items():
    print(f"    {k}: {v['count']} (H={v.get('high',0)})")
