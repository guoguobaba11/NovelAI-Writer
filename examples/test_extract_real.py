"""LLM 抽取真实手稿测试"""
import sys, os, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 从 .env 加载环境变量（novelai.config 已自动加载）
from novelai.config import CONFIG
from novelai.db import Database
from novelai import knowledge as kb
from novelai import writer as _writer_mod
from novelai.ai_client import AIClient

db = Database(CONFIG.db_path)
ai = AIClient()
print("ai.ready:", ai.ready)
print("provider:", ai.cfg.provider, "base:", ai.cfg.base_url, "model:", ai.cfg.model)
if not ai.ready:
    print("AI 未配置，退出")
    sys.exit(1)

# Step 1: 抽第 1 章事件
print("\n=== 抽第 1 章事件 ===")
r1 = _writer_mod.extract_events_for_chapter(db, ai, 1)
print(f"  ok={r1.get('ok')}, added={r1.get('added',0)}, skipped={r1.get('skipped',0)}")
for ev in r1.get("events", []):
    print(f"    · [{ev.get('event_type','?')}] imp={ev.get('importance',3)} {ev.get('title','')}")
    print(f"      {ev.get('summary','')[:80]}")
    print(f"      @t={ev.get('actual_story_time', '?')}  loc={ev.get('location','')}  part={ev.get('participants', [])}")

# Step 2: 抽第 1 章伏笔
print("\n=== 抽第 1 章伏笔 ===")
r2 = _writer_mod.extract_threads_for_chapter(db, ai, 1)
print(f"  ok={r2.get('ok')}, added={r2.get('added',0)}, linked={r2.get('linked',0)}")
for th in r2.get("threads", []):
    link = f" → linked#{th['linked_to']}" if th.get("linked_to") else ""
    print(f"    · [{th.get('status','?')}] {th.get('title','')}")
    print(f"      {th.get('description','')[:80]}{link}")

# Step 3: 跑全本抽取（先清空之前的事件/伏笔）
print("\n=== 清空事件/伏笔后跑全本抽取 ===")
db.execute("DELETE FROM event")
db.execute("DELETE FROM plot_thread")

# 记录开始时间
start = time.time()
report = _writer_mod.extract_all(db, ai)
elapsed = time.time() - start

print(f"\n  耗时: {elapsed:.1f} 秒")
print(f"  事件: {report['events']['ok']}/{report['events']['ok']+report['events']['failed']} 章成功")
print(f"        新增 {report['events']['added']} 个事件（跳过 {report['events']['skipped']}）")
print(f"  伏笔: {report['threads']['ok']}/{report['threads']['ok']+report['threads']['failed']} 章成功")
print(f"        新增 {report['threads']['added']} 个伏笔（自动关联 {report['threads']['linked']}）")
print()
print("  详情：")
print(f"  {'章节':<8} {'事件':<6} {'伏笔':<6} {'状态'}")
print("  " + "-" * 40)
for i in range(len(report["events"]["details"])):
    ed = report["events"]["details"][i]
    td = report["threads"]["details"][i]
    status = "✓" if not ed.get("error") and not td.get("error") else "✗"
    err = ed.get("error") or td.get("error") or ""
    print(f"  第{ed['chapter_idx']:>2}回  +{ed['added']:<3}   +{td.get('added',0):<3}   {status} {err[:30]}")

# Step 4: 跑一次全本扫描看效果
print("\n=== 跑全本扫描（基于抽取后的事件库）===")
from novelai import scanner

t_issues = scanner.scan_threads(db)
print(f"  伏笔问题: {len(t_issues)}")
for it in t_issues[:5]:
    print(f"    [{it['severity']}] {it['issue_type']} {it['title']}: {it['context'][:60]}")

l_result = scanner.scan_logic(db)
l_sum = l_result.get("summary", {})
print(f"  逻辑链: 总 {l_sum.get('total',0)} 个 (H{l_sum.get('by_severity',{}).get('high',0)} M{l_sum.get('by_severity',{}).get('medium',0)} L{l_sum.get('by_severity',{}).get('low',0)})")
for k, label in [("dead_appears", "死人复活"), ("location_clash", "地点冲突"), ("causality_reversed", "因果倒置"), ("info_leak", "信息泄漏"), ("chain_break", "事件链断裂")]:
    items = l_result.get(k, [])
    if items:
        print(f"    [{label}] {len(items)}")
        for it in items[:3]:
            print(f"      · [{it['severity']}] {it.get('context','')[:80]}")

# 总体统计
print("\n=== 最终统计 ===")
print(f"  章节: {len(kb.list_chapters(db))}")
print(f"  人物: {len(kb.list_characters(db))}")
print(f"  事件: {len(kb.list_events(db))}")
print(f"  伏笔: {len(kb.list_threads(db))}")
print(f"  关系: {len(kb.list_relationships(db))}")
print(f"  伏笔状态分布:")
from collections import Counter
st = Counter(t.get("status", "?") for t in kb.list_threads(db))
for s, c in st.most_common():
    print(f"    {s}: {c}")
