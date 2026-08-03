"""
端到端测试：用户的 7 个真实手稿回
- 复制 7 个文件到 test_real/ 目录
- 清空 db 关键表
- 调用 importer 导入
- 注入主要人物 + MBTI（基于文件名前几回推测）
- 跑 4 个扫描器
- 输出结构化报告
"""
import sys
import os
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from novelai.config import CONFIG
from novelai.db import Database
from novelai import knowledge as kb
from novelai import importer
from novelai import scanner
from novelai import personality

USER_DOWNLOADS = Path(r"C:\Users\hslji\Downloads")
TEST_DIR = ROOT / "test_real"
SOURCE_FILES = [
    "第一回_三百万水漂砸信誉_无名客夜车点迷津.md",
    "第十回_风清扬初露真面目_江湖再见是开端.md",
    "第十八回_盈盈得证父涉暗局_糖纸信物结暗缘.md",
    "第三十二回_限制出境任我行落子_九十天令狐冲倒计时.md",
    "第三十三回_林平之绝地反击_证据链一朝引爆.md",
    "第三十五回_盈盈举报生父惊变_父女短信肝肠断.md",
]


def main():
    print("=" * 70)
    print("【真实手稿端到端测试】")
    print("=" * 70)

    # Step 1: 复制文件
    print("\n[1/6] 复制 7 个手稿文件到 test_real/")
    TEST_DIR.mkdir(parents=True, exist_ok=True)
    for old in TEST_DIR.glob("*.md"):
        old.unlink()
    for fn in SOURCE_FILES:
        src = USER_DOWNLOADS / fn
        if not src.exists():
            print(f"  ✗ 找不到: {src}")
            continue
        dst = TEST_DIR / fn
        shutil.copy(src, dst)
        print(f"  ✓ {fn} ({src.stat().st_size} B)")

    # 找额外的"第十八回(1)"（用户重复发了一版）—— 选较大的版本
    extras = [f for f in USER_DOWNLOADS.glob("第十八回*") if f.name not in SOURCE_FILES]
    for e in extras:
        dst = TEST_DIR / e.name
        shutil.copy(e, dst)
        print(f"  ✓ [extra] {e.name} ({e.stat().st_size} B)")

    # Step 2: 备份并清空 db
    print("\n[2/6] 备份并清空 db")
    db_path = CONFIG.db_path
    backup = Path(str(db_path) + ".pre_real_test.bak")
    if db_path.exists():
        shutil.copy(db_path, backup)
        print(f"  ✓ 备份到 {backup.name}")
    db = Database(db_path)
    # 按依赖反向顺序清空（先清引用表）
    clear_order = [
        "consistency_report",
        "character_milestone",
        "relationship_evolution",
        "optimization_suggestion",
        "event",
        "plot_thread",
        "fact",
        "chapter",
        "volume",
        "relationship",
        "character",
    ]
    for t in clear_order:
        try:
            db.execute(f"DELETE FROM {t}")
        except Exception as e:
            print(f"  ! {t}: {e}")
    print(f"  ✓ 清空 {len(clear_order)} 张表")

    # Step 3: import-md
    print("\n[3/6] 导入 7 个回")
    result = importer.import_markdown(
        db, str(TEST_DIR),
        project_title="测试手稿（7 个重点回）",
        story_time_unit="回",
        progress_cb=lambda s, m: print(f"    [{s}] {m}"),
    )
    print(f"  ✓ 导入完成：{result['chapters']} 章，{result['words']} 字，{result['volumes']} 卷")

    # Step 4: 注入主要人物 + MBTI（基于手稿内容推测）
    print("\n[4/6] 创建主要人物 + 标 MBTI")
    char_mbtis = [
        ("令狐冲", "protagonist", "ENFP",
         "主角，FA 投资经理，因三百万水漂事件入局。性格外放、行动派、有点鲁莽但有同理心。"),
        ("盈盈", "protagonist", "INFJ",
         "任我行之女，知性独立，深度情感，共情能力强。"),
        ("风清扬", "supporting", "INTP",
         "隐世高手，思维独立，不慕权势，逻辑严密。"),
        ("林平之", "supporting", "ISTJ",
         "复仇者，谨慎、可靠、但被仇恨驱动。"),
        ("任我行", "antagonist", "ENTJ",
         "任盈盈之父，权势型反派。战略型、强势、目标导向。"),
        ("老金", "supporting", "ESTJ",
         "令狐冲的老板，FA 公司创始人。组织型、效率导向。"),
        ("老韩", "supporting", "ESFJ",
         "令狐冲的大学师兄，社会型、人脉导向。"),
        ("钱先生", "supporting", "ESTP",
         "资金方，绍兴商人，行动型、外向。"),
        ("孙总", "antagonist", "ENTP",
         "骗子项目方，诡辩、善于钻营。"),
    ]
    for name, role, mbti, desc in char_mbtis:
        existing = kb.find_character_by_name(db, name)
        if existing:
            cid = existing["id"]
            stack = personality.get_stack(mbti)
            kws = personality.mbti_to_keywords(mbti)
            kb.update_character(
                db, cid, mbti=mbti, role=role,
                cognitive_stack="-".join(stack), baseline_keywords=kws,
                basic_info=desc, arc_type="positive", arc_progress=0.5,
            )
        else:
            cid = kb.add_character(
                db, name=name, role=role, basic_info=desc,
                arc_type="positive", arc_progress=0.5,
            )
            stack = personality.get_stack(mbti)
            kws = personality.mbti_to_keywords(mbti)
            kb.update_character(
                db, cid, mbti=mbti,
                cognitive_stack="-".join(stack), baseline_keywords=kws,
            )
        print(f"  ✓ {name} ({mbti}, {role})")

    # Step 5: 跑 4 个扫描器
    print("\n[5/6] 跑 4 个扫描器")

    print("\n--- A) 伏笔扫描 ---")
    thread_issues = scanner.scan_threads(db)
    if not thread_issues:
        print("  无伏笔记录（可能未手动登记）")
    else:
        for it in thread_issues:
            print(f"  [{it['severity']}] {it['issue_type']} {it['title']}: {it['context'][:60]}")

    print("\n--- B) 逻辑链扫描 ---")
    logic = scanner.scan_logic(db)
    s = logic.get("summary", {})
    print(f"  总计: {s.get('total', 0)} 个问题 (H={s.get('by_severity',{}).get('high',0)} M={s.get('by_severity',{}).get('medium',0)} L={s.get('by_severity',{}).get('low',0)})")
    for k, label in [
        ("dead_appears", "💀 死人复活"),
        ("location_clash", "📍 地点冲突"),
        ("causality_reversed", "⏪ 因果倒置"),
        ("info_leak", "🕳 信息泄漏"),
        ("chain_break", "🔌 事件链断裂"),
    ]:
        items = logic.get(k, [])
        if items:
            print(f"  [{label}] {len(items)}")
            for it in items[:5]:
                print(f"    · [{it['severity']}] {it.get('context','')[:100]}")

    print("\n--- C) 文风漂移 ---")
    style = scanner.scan_style(db, baseline_first_n=1, z_threshold=1.5)
    print(f"  基线: 第{style['baseline_range'][0]}章")
    print(f"  漂移问题: {len(style['drift_issues'])}")
    for it in style['drift_issues'][:8]:
        print(f"  [{it['severity']}] 第{it['chapter_idx']}章 {it['dimension']}: z={it['z_score']} ({it['value']} vs 基线 {it['baseline']})")
    print("  综合距离曲线:")
    for c in style['overall_drift_curve']:
        bar = "█" * min(40, int(c['distance'] * 6))
        print(f"    第{c['idx']:>2}回  {c['distance']:5.2f}  {bar}")

    print("\n--- D) 性格漂移 ---")
    chars = [c for c in kb.list_characters(db) if c.get("mbti")]
    drift_results = personality.scan_personality_drift(db, chars)
    by_char = {}
    for r in drift_results:
        by_char.setdefault(r["char_id"], []).append(r)
    for cid, rows in by_char.items():
        char = kb.get_character(db, cid)
        sig_rows = [r for r in rows if r.get("drift_signals")]
        if not sig_rows:
            continue
        print(f"  📍 {char['name']} ({char['mbti']}, 出现 {len(rows)} 回, {len(sig_rows)} 漂移)")
        for r in sig_rows:
            print(f"    · 第{r['chapter_idx']}回  baseline={r['baseline_overlap']:.2f}")
            for s in r['drift_signals']:
                print(f"      ⚠ {s}")

    # Step 6: 关键统计
    print("\n[6/6] 关键统计")
    chapters = kb.list_chapters(db)
    total_words = sum(c.get("word_count") or 0 for c in chapters)
    print(f"  章节数: {len(chapters)}")
    print(f"  总字数: {total_words:,}")
    print(f"  平均每回字数: {total_words // max(1, len(chapters)):,}")

    # 列出每回
    print("\n  各回概要：")
    for c in chapters:
        title = c["title"]
        wc = c.get("word_count") or 0
        print(f"    第{c['idx']:>2}回  {wc:>5,}字  {title[:50]}")

    # 字数条形图
    print("\n  字数条形图：")
    max_w = max((c.get("word_count") or 0) for c in chapters) or 1
    for c in chapters:
        wc = c.get("word_count") or 0
        bar = "█" * int(wc / max_w * 30)
        print(f"    第{c['idx']:>2}回  {wc:>5,}  {bar}")

    # Web 面板入口
    print("\n" + "=" * 70)
    print("✅ 全流程跑通！")
    print(f"📖 启动 Web 面板查看可视化: python -m web.app")
    print(f"   然后浏览器打开 http://127.0.0.1:8765")
    print("=" * 70)


if __name__ == "__main__":
    main()
