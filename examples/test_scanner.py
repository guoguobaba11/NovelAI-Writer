"""烟雾测试：导入 + 三个扫描器"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from novelai.config import CONFIG
from novelai.db import Database
from novelai import importer
from novelai import scanner

# 用上一轮的 db
db = Database(CONFIG.db_path)

# 1) 列出已导入的章节
print("=== 已导入章节 ===")
for c in db.query("SELECT idx, title, volume_idx, word_count FROM chapter ORDER BY idx"):
    print(f"  第{c['idx']}回 {c['title']} (vol={c['volume_idx']}, {c['word_count']}字)")

# 2) 添加测试用人物和事件（让逻辑链扫描器有数据可扫）
print()
print("=== 注入测试数据 ===")

# 添加人物：沈青砚在第 4 回"死"了
if not db.query_one("SELECT id FROM character WHERE name='沈青砚'"):
    shen = db.insert("INSERT INTO character(name, role, basic_info) VALUES(?,?,?)",
                     ("沈青砚", "protagonist", "测试主角"))
    print(f"  [+] 沈青砚 id={shen}")
else:
    shen = db.query_one("SELECT id FROM character WHERE name='沈青砚'")["id"]

if not db.query_one("SELECT id FROM character WHERE name='李琰'"):
    li = db.insert("INSERT INTO character(name, role, basic_info) VALUES(?,?,?)",
                   ("李琰", "antagonist", "测试反派"))
    print(f"  [+] 李琰 id={li}")
else:
    li = db.query_one("SELECT id FROM character WHERE name='李琰'")["id"]

# 标记沈青砚在第 4 回"死"
# 用 fact 表示
existing_fact = db.query_one("SELECT id FROM fact WHERE content LIKE '%沈青砚%死%'")
if not existing_fact:
    f_id = db.insert(
        "INSERT INTO fact(category, content, reliability, known_by, established_chapter_id, created_at) VALUES(?,?,?,?,?,?)",
        ("人物", "沈青砚在第4回中战死", "reliable", "[]", 4, 0)
    )
    print(f"  [+] fact 沈青砚战死 id={f_id}")

# 给章节加 POV
ch4 = db.query_one("SELECT id, title FROM chapter WHERE idx=4")
if ch4 and not ch4.get("pov_character_id") if False else True:  # 简化
    db.execute("UPDATE character SET status=? WHERE id=?", ("已死", shen))

# 3) 添加伏笔
threads_test = [
    ("玉佩之谜", "死者遗留的玉佩", "mystery", "planted", 1, None),
    ("林婉身世", "林婉的家族秘密", "mystery", "developing", 2, None),
    ("承乾宫变", "三十年前的宫廷旧案", "mystery", "planted", 1, None),
    ("李琰谋反", "李琰暗中筹谋", "foreshadow", "abandoned", 3, None),  # 已放弃但描述里说"重要"
]
# 先清理
db.execute("DELETE FROM plot_thread")
for title, desc, ttype, status, planted, payoff in threads_test:
    tid = db.insert(
        "INSERT INTO plot_thread(title, description, thread_type, status, planted_chapter_id, related_characters) VALUES(?,?,?,?,?,?)",
        (title, desc, ttype, status, planted, "[]")
    )
    print(f"  [+] 伏笔 id={tid} {title} ({status})")
# 改 abandoned 的 description 让它"暗示重要"
db.execute("UPDATE plot_thread SET description=? WHERE status='abandoned'",
           ("这是李琰关键伏笔，决定结局走向",))

# 4) 添加事件，让逻辑链扫描器有数据
db.execute("DELETE FROM event")
# BUG 修复：章节 idx 可能跳号（如 1,10,18…），直接下标 None 会崩。缺章时提示并退出。
_ch = {}
for _i in (1, 2, 3, 4):
    _row = db.query_one("SELECT id FROM chapter WHERE idx=?", (_i,))
    if _row is None:
        print(f"[!] 数据库中没有第 {_i} 章（本测试要求第 1-4 章连续存在，请先运行 test_importer.py）")
        sys.exit(1)
    _ch[_i] = _row["id"]
ch1, ch2, ch3, ch4 = _ch[1], _ch[2], _ch[3], _ch[4]

e1 = db.insert("INSERT INTO event(chapter_id, story_time, sequence_in_chapter, title, summary, event_type, location, participants, importance) VALUES(?,?,?,?,?,?,?,?,?)",
               (ch1, 1.0, 1, "发现玉佩", "沈青砚发现玉佩", "discovery", "城西", f"[{shen}]", 4))
e2 = db.insert("INSERT INTO event(chapter_id, story_time, sequence_in_chapter, title, summary, event_type, location, participants, importance) VALUES(?,?,?,?,?,?,?,?,?)",
               (ch2, 2.0, 1, "询问李琰", "沈青砚找李琰询问玉佩", "dialogue", "平康坊", f"[{shen},{li}]", 3))
e3 = db.insert("INSERT INTO event(chapter_id, story_time, sequence_in_chapter, title, summary, event_type, location, participants, importance) VALUES(?,?,?,?,?,?,?,?,?)",
               (ch3, 3.0, 1, "李琰透露身份", "李琰告知死者身份", "revelation", "平康坊", f"[{li}]", 5))
# 制造因果倒置：让 e3 引用 e1 作为 cause（e1 时间早于 e3，没问题）
# 然后再造一个事件 e4 在第 2 章但 cause 是 e3
e4 = db.insert("INSERT INTO event(chapter_id, story_time, sequence_in_chapter, title, summary, event_type, location, participants, importance) VALUES(?,?,?,?,?,?,?,?,?)",
               (ch2, 1.5, 2, "做梦", "沈青砚做了一个奇怪的梦", "action", "梦境", f"[{shen}]", 1))
# 因果倒置：e4 的 cause 设为 e3（时间倒置）
db.execute("UPDATE event SET cause_event_ids=? WHERE id=?", (f"[{e3}]", e4))

# 死人复活测试：第 4 回添加正文说"沈青砚站起来..."
ch4_row = db.query_one("SELECT * FROM chapter WHERE idx=4")
new_text = (ch4_row["final_text"] or "") + "\n\n沈青砚忽然从地上站起来，看着远方的灯火，默默叹息。"
db.execute("UPDATE chapter SET final_text=?, draft=? WHERE id=?",
           (new_text, new_text, ch4_row["id"]))

# 5) 跑扫描器
print()
print("=" * 60)
print("=== 伏笔扫描器 ===")
issues = scanner.scan_threads(db)
print(f"发现 {len(issues)} 个问题：")
for it in issues:
    print(f"  [{it['severity']}] {it['issue_type']} {it['title']}: {it['context']}")

print()
print("=" * 60)
print("=== 逻辑链扫描器 ===")
result = scanner.scan_logic(db)
print(f"汇总: {result['summary']}")
for k in ["dead_appears", "location_clash", "causality_reversed", "info_leak", "chain_break"]:
    if result[k]:
        print(f"  [{k}] ({len(result[k])})")
        for it in result[k]:
            print(f"    - [{it['severity']}] {it.get('context', '')[:80]}")

print()
print("=" * 60)
print("=== 文风漂移扫描器 ===")
result = scanner.scan_style(db, baseline_first_n=2)
print(f"基线范围: 第{result['baseline_range'][0]}-{result['baseline_range'][1]}章")
print(f"漂移问题: {len(result['drift_issues'])} 个")
for it in result['drift_issues'][:8]:
    print(f"  [{it['severity']}] 第{it['chapter_idx']}章 {it['dimension']} z={it['z_score']}: {it['value']} vs 基线 {it['baseline']}")
print("综合距离曲线:")
for c in result['overall_drift_curve']:
    bar = "█" * int(c['distance'] * 4)
    print(f"  第{c['idx']:2d}章  {c['distance']:5.2f}  {bar}")

print()
print("ALL SCAN TESTS DONE")
