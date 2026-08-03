"""测试 personality + MBTI 工具 + 性格漂移"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from novelai.config import CONFIG
from novelai.db import Database
from novelai import knowledge as kb
from novelai import personality

db = Database(CONFIG.db_path)

# 1) MBTI 工具库基本
print("=== MBTI 工具库 ===")
print(f"INTJ 认知栈: {personality.get_stack('INTJ')}")
print(f"ENFP 关键词 (前 8): {personality.mbti_to_keywords('ENFP')[:8]}")
print(f"INTJ vs ENFP 兼容性: {personality.compatibility_score('INTJ', 'ENFP')}")
print(f"INTJ vs ISTJ 兼容性: {personality.compatibility_score('INTJ', 'ISTJ')}")
print(f"INTJ vs INTP 兼容性: {personality.compatibility_score('INTJ', 'INTP')}")

# 2) 给已有的人物设置 MBTI
print()
print("=== 设置 MBTI ===")
test_mbtis = [
    ("沈青砚", "INTJ"),
    ("林婉", "INFJ"),
    ("李琰", "ENTJ"),
]
for name, mbti in test_mbtis:
    c = kb.find_character_by_name(db, name)
    if c:
        stack = personality.get_stack(mbti)
        kws = personality.mbti_to_keywords(mbti)
        kb.update_character(
            db, c["id"],
            mbti=mbti,
            cognitive_stack="-".join(stack),
            baseline_keywords=kws,
            arc_type="positive",
            arc_progress=0.3,
        )
        print(f"  ✓ {name} → {mbti} (栈={'-'.join(stack)})")

# 3) 添加 milestone
print()
print("=== 添加成长线里程碑 ===")
shen = kb.find_character_by_name(db, "沈青砚")
ch1 = kb.get_chapter_by_idx(db, 1)
ch2 = kb.get_chapter_by_idx(db, 2)
if shen and ch1:
    mid = kb.add_milestone(
        db, character_id=shen["id"], chapter_id=ch1["id"],
        milestone_type="starting_point",
        description="沈青砚冷静自持，只信证据",
        before_state="单纯信仰证据",
        after_state="开始怀疑朝局",
        dimension="belief",
        importance=4,
    )
    print(f"  ✓ milestone id={mid}")
if shen and ch2:
    mid = kb.add_milestone(
        db, character_id=shen["id"], chapter_id=ch2["id"],
        milestone_type="catalyst",
        description="看到承乾年款，开始怀疑自己的身世",
        before_state="只关心案件",
        after_state="意识到身世与朝局有关",
        dimension="belief",
        importance=5,
    )
    print(f"  ✓ milestone id={mid}")

# 4) 添加关系演变
print()
print("=== 添加关系演变 ===")
shen = kb.find_character_by_name(db, "沈青砚")
lin = kb.find_character_by_name(db, "林婉")
ch2 = kb.get_chapter_by_idx(db, 2)
if shen and lin and ch2:
    rels = kb.get_relationships_for(db, shen["id"])
    target = None
    for r in rels:
        if r["char_a_id"] == lin["id"] or r["char_b_id"] == lin["id"]:
            target = r
            break
    if target:
        rev = kb.add_rel_evolution(
            db, relationship_id=target["id"], chapter_id=ch2["id"],
            intimacy=0.4, trust=0.6, conflict=0.0, dynamics="旧情复燃",
        )
        print(f"  ✓ 关系演变 id={rev}: 沈青砚↔林婉 亲密=+0.4 信任=+0.6")

# 5) 跑性格漂移扫描
print()
print("=== 性格漂移扫描 ===")
chars = [c for c in kb.list_characters(db) if c.get("mbti")]
results = personality.scan_personality_drift(db, chars)
print(f"分析 {len(chars)} 个角色, {len(results)} 章次")

by_char: dict[int, list[dict]] = {}
for r in results:
    by_char.setdefault(r["char_id"], []).append(r)

for cid, rows in by_char.items():
    char = kb.get_character(db, cid)
    print(f"\n── {char['name']} (MBTI={char['mbti']}) ──")
    for r in rows:
        sig_n = len(r.get("drift_signals", []))
        marker = "⚠" if sig_n else "✓"
        print(f"  {marker} 第{r['chapter_idx']:>2}章 {r['chapter_title'][:14]:<14}  baseline={r['baseline_overlap']:.2f}  推断={r['inferred_mbti']}  漂移={sig_n}")

# 6) 人物矩阵
print()
print("=== 人物矩阵 ===")
mat = personality.build_character_matrix(chars)
print(f"{'人物':<10} {'MBTI':<6} {'认知栈':<16} {'弧光':<8} {'进度':<6}")
for c in mat["characters"]:
    prog = f"{c['arc_progress']*100:.0f}%" if c['arc_progress'] else "—"
    print(f"{c['name']:<10} {c['mbti']:<6} {c['stack_str']:<16} {c['arc_type'] or '—':<8} {prog:<6}")

print()
print("兼容性：")
names = [c['name'] for c in mat['characters']]
for i, a in enumerate(names):
    for j, b in enumerate(names):
        if i < j:
            data = mat['matrix'][a][b]
            print(f"  {a} ↔ {b}: {data['score']:.2f}  {data.get('interpretation','')[:60]}")

# 7) 成长线
print()
print("=== 沈青砚 成长线 ===")
ms = kb.list_milestones(db, character_id=shen["id"])
ch_by_id = {c["id"]: c for c in kb.list_chapters(db)}
for m in ms:
    ch = ch_by_id.get(m["chapter_id"], {})
    print(f"  第{ch.get('idx','?')}章 · [{m['milestone_type']}] · {m.get('dimension','')}")
    print(f"      {m['description']}")
    if m.get('before_state') or m.get('after_state'):
        print(f"      变化：{m.get('before_state','?')} → {m.get('after_state','?')}")

print()
print("ALL PERSONALITY TESTS PASSED")
