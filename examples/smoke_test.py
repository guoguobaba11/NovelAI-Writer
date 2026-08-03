"""端到端烟雾测试"""
from novelai.config import CONFIG
from novelai.db import Database
from novelai import knowledge as kb
from novelai import retriever, consistency

db = Database(CONFIG.db_path)

print("=== 项目 ===")
p = kb.get_or_create_project(db)
for k in ('title', 'synopsis', 'style', 'pov_mode', 'story_time_unit'):
    v = p.get(k, "") or ""
    print(f"  {k}: {v[:60]}")

print()
print("=== 人物 ===")
for c in kb.list_characters(db):
    print(f"  [{c['id']}] {c['name']} ({c.get('role','')})")

print()
print("=== 章节 ===")
for c in kb.list_chapters(db):
    pov = ""
    if c.get('pov_character_id'):
        cc = kb.get_character(db, c['pov_character_id'])
        if cc:
            pov = cc['name']
    print(f"  第{c['idx']}章 {c['title']} (pov={pov})")

print()
print("=== 上下文工程（为第 1 章准备）===")
ctx = retriever.build_chapter_context(db, 1)
print(f"  POV: {ctx['pov_profile'][:60]}")
print(f"  已知事实: {len(ctx['known_facts'].splitlines())} 条")
print(f"  上一章摘要: {ctx['prev_chapter_summary'][:60]}")
print(f"  临近事件: {len(ctx['recent_event_summaries'].splitlines())} 行")
print(f"  相关伏笔: {len(ctx['relevant_threads'].splitlines())} 条")
print(f"  世界观: {len(ctx['world_settings'].splitlines())} 条")
print(f"  关系: {len(ctx['relationships'].splitlines())} 条")

print()
print("=== 硬校验 1：含信息泄漏的正文（应报警）===")
test_text_leak = (
    "沈青砚来到义宁坊。他看着尸体，喉中无伤，心脉却断。"
    "他想：这玉佩是宫中才有的双鸾衔枝样式，看来与承乾宫变有关。"
    "沈青砚忽然记起，自己生父其实是皇孙，三十年前承乾宫变中幸存的皇孙。"
)
issues = consistency.hard_check(db, 1, test_text_leak)
print(f"  发现 {len(issues)} 个硬规则问题：")
for it in issues:
    print(f"   - [{it['severity']}/{it['category']}] {it['explanation']}")
    if it.get("fix_suggestion"):
        print(f"       建议: {it['fix_suggestion']}")

print()
print("=== 硬校验 2：干净的正文（应少报警）===")
test_text_clean = (
    "沈青砚来到义宁坊。他蹲在尸体旁细看，喉中无伤，心脉却断。"
    "他拿起那枚断裂的玉佩，纹样是宫中才有的双鸾衔枝。"
    "他心中暗想：此事恐怕不简单。"
)
issues = consistency.hard_check(db, 1, test_text_clean)
print(f"  发现 {len(issues)} 个问题：")
for it in issues:
    print(f"   - [{it['severity']}/{it['category']}] {it['explanation']}")

print()
print("=== 硬校验 3：未登记人名（应报警）===")
test_text_newchar = (
    "沈青砚与陈三在义宁坊相遇。陈三告诉他案子的真相。"
)
issues = consistency.hard_check(db, 1, test_text_newchar)
print(f"  发现 {len(issues)} 个问题：")
for it in issues:
    print(f"   - [{it['severity']}/{it['category']}] {it['explanation']}")

print()
print("ALL TESTS DONE")
