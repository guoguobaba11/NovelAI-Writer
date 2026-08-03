"""结构分析真实手稿测试"""
import sys, os, json
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from novelai.config import CONFIG
from novelai.db import Database
from novelai import knowledge as kb
from novelai import structure

db = Database(CONFIG.db_path)
ana = structure.StructureAnalyzer(db)

print("=" * 70)
print("【叙事结构 真实手稿测试】")
print("=" * 70)

# 全篇级
print("\n" + "=" * 70)
print("📊 全篇级分析")
print("=" * 70)
r_full = ana.analyze_full()
if r_full.get("error"):
    print(f"  错误: {r_full['error']}")
else:
    print(f"  总章节: {r_full['n_chapters']}, 总字数: {r_full['total_words']:,}")
    print(f"  事件数: {r_full['n_events']}, turning_point: {r_full['n_turning_points']}")
    print(f"  全篇高潮位置: {r_full['climax_position']} (在第 {r_full['climax_chapter_idx']} 章)")
    print()
    print("  【4 段事件分布】")
    for p, info in r_full["phase_breakdown"].items():
        print(f"    {info['label']:<10}  位置 {info['position_range'][0]:.2f}-{info['position_range'][1]:.2f}  "
              f"事件 {info['n_events']:>2}  重要度 {info['importance_avg']}  "
              f"埋伏笔 {info['n_threads_planted']}  揭晓 {info['n_threads_payoff']}")
    print()
    print("  【3 幕结构】")
    for ab in r_full["act_breakdown"]:
        chs = ab.get('chapter_range') or [0, 0]
        print(f"    {ab['label']:<18}  第 {chs[0]}-{chs[1]} 章  {ab['n_chapters']} 章  {ab['word_count']:,} 字  {ab['n_events']} 事件")
    print()
    print("  【重要性曲线（每章）】")
    for c in r_full["intensity_curve"]:
        bar = "█" * int(c["intensity"] * 6)
        marker = "🔥" if c["n_turning"] > 0 else "  "
        print(f"    第{c['chapter_idx']:>2}章  pos={c['position']:.2f}  int={c['intensity']:>4.1f}  "
              f"n={c['n_events']:>2} {marker} {bar}")
    print()
    print(f"  【结构问题】（{len(r_full['issues'])} 个）")
    for it in r_full["issues"]:
        print(f"    [{it['severity']}] {it['type']}: {it['context']}")

# 全篇问题汇总
print("\n" + "=" * 70)
print("🚨 全篇问题汇总")
print("=" * 70)
summary = ana.full_issues_summary()
print(f"  总问题: {summary['total_issues']}")
print(f"  - 章回级: {sum(len(x['issues']) for x in summary['chapters'])}")
print(f"  - 卷级:   {sum(len(x['issues']) for x in summary['volumes'])}")
print(f"  - 全篇级: {len(summary['full'])}")

# 全卷级
print("\n" + "=" * 70)
print("📚 全卷级分析（1 卷）")
print("=" * 70)
volumes = kb.list_volumes(db)
for v in volumes:
    r_v = ana.analyze_volume(v["idx"])
    if r_v.get("error"):
        continue
    print(f"\n  第 {v['idx']} 卷《{v['title']}》")
    print(f"    章节: {r_v['n_chapters']}, 字数: {r_v['word_count']:,}")
    print(f"    事件: {r_v['n_events']}, turning_point: {r_v['n_turning_points']} @ {r_v['turning_positions']}")
    print(f"    平均重要性: {r_v['importance_avg']}")
    print(f"    4 段事件分布:")
    for p, info in r_v["phase_breakdown"].items():
        cr = info.get("chapter_range") or [0, 0]  # 该段无章时 chapter_range 为 []，防空列表下标崩
        print(f"      {info['label']:<10}  第 {cr[0]:>2}-{cr[1]:>2} 章  "
              f"事件 {info['n_events']:>2}  重要度 {info['importance_avg']}  "
              f"埋 {info['n_threads_planted']}  揭晓 {info['n_threads_payoff']}")
    print(f"    卷内问题（{len(r_v['issues'])}）")
    for it in r_v["issues"]:
        print(f"      [{it['severity']}] {it['type']}: {it['context']}")

# 章回级 - 选几章
print("\n" + "=" * 70)
print("📖 章回级分析（前 3 章 + 后 1 章）")
print("=" * 70)
chapters = kb.list_chapters(db)
sample = [chapters[0], chapters[1] if len(chapters) > 1 else chapters[0], chapters[2] if len(chapters) > 2 else chapters[0], chapters[-1]]
for ch in sample:
    r_c = ana.analyze_chapter(ch["idx"])
    if r_c.get("error"):
        continue
    print(f"\n  第 {r_c['chapter_idx']} 回《{r_c['title'][:30]}》")
    print(f"    字数: {r_c['word_count']}, 事件: {r_c['n_events']}, turning: {r_c['n_turning_points']}")
    print(f"    平均重要性: {r_c['importance_avg']}, 推进伏笔: {r_c['thread_count']}")
    if r_c["turning_positions"]:
        print(f"    转折点位置: {r_c['turning_positions']}")
    if r_c["issues"]:
        print(f"    问题:")
        for it in r_c["issues"]:
            print(f"      [{it['severity']}] {it['type']}: {it['context']}")
