import sys
sys.path.insert(0, ".")
from novelai.config import CONFIG
from novelai.db import Database
from novelai import pipeline
db = Database(CONFIG.db_path)
import time
t0 = time.time()
r = pipeline.run_quick_pipeline(db)
print(f"quick done in {time.time()-t0:.2f}s")
print(f"  total issues: {sum(v['count'] for v in r['issues_by_category'].values())}")
for k, v in r['issues_by_category'].items():
    print(f"    {k}: {v['count']} (H={v.get('high',0)})")
roadmap = pipeline.build_roadmap(r, llm_suggestions=None)
print(f"  roadmap: {len(roadmap)} items")
print(f"  top 10:")
for it in roadmap[:10]:
    print(f"    #{it['rank']} [{it['severity']}/{it['type']}] ch={it['chapter_ref']} {it['title'][:60]}")
