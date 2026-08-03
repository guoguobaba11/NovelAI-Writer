import sys
sys.path.insert(0, ".")
from novelai.config import CONFIG
from novelai.db import Database
from novelai import knowledge as kb, scanner, personality
db = Database(CONFIG.db_path)
chars = [c for c in kb.list_characters(db) if c.get("mbti")]
results = personality.scan_personality_drift(db, chars)
by_char = {}
for r in results:
    by_char.setdefault(r["char_id"], []).append(r)
print("=== 性格漂移（按角色） ===")
for cid, rows in by_char.items():
    char = kb.get_character(db, cid)
    total_seg = sum(r["n_segments"] for r in rows)
    avg_overlap = sum(r["baseline_overlap"] for r in rows) / max(1, len(rows))
    sigs = sum(len(r["drift_signals"]) for r in rows)
    name = char["name"]
    mbti = char["mbti"]
    print(f"  {name:8s} ({mbti}) 出现 {len(rows)} 回, 共 {total_seg} 段, 平均 overlap={avg_overlap:.3f}, {sigs} 漂移信号")
