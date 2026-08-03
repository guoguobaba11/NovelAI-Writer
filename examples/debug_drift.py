import sys
sys.path.insert(0, ".")
from novelai.config import CONFIG
from novelai.db import Database
from novelai import knowledge as kb
from novelai import personality

db = Database(CONFIG.db_path)
ch = kb.get_chapter_by_idx(db, 1)
print(f"第 1 回：{ch['title'][:30]} ({ch['word_count']}字)")
text = ch['final_text']

r = personality.analyze_chapter_personality(text, "令狐冲", "ENFP", personality.mbti_to_keywords("ENFP"))
print(f"\n令狐冲(ENFP) 在第 1 回：")
print(f"  n_segments: {r['n_segments']}")
print(f"  baseline_overlap: {r['baseline_overlap']}")
print(f"  inferred_mbti: {r['inferred_mbti']}")
print(f"  function_scores: {r['function_scores']}")
print(f"  drift_signals: {r['drift_signals']}")

print(f"\nENFP baseline keywords: {personality.mbti_to_keywords('ENFP')[:10]}")
