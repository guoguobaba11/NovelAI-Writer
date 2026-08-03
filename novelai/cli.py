"""
novelai.cli
极简 CLI 入口。

命令：
  init                       初始化项目（创建数据库 + 默认项目元信息）
  set-synopsis <文本>        设置项目梗概
  set-style <文本>           设置文风
  set-pov <限知视角|全知视角>
  set-unit <日|小时|不定>    时间单位
  add-character <JSON>       交互式添加人物（接受 name=... 形式或 JSON 字符串）
  add-world <cat> <name> <content>
  add-fact <content> [--category=...] [--reliable|rumored|secret|false] [--known-by=id1,id2]
  add-relationship <a> <b> <type> [state]
  add-thread <title> <desc> [--type=foreshadow|mystery|...] [--status=planted|...]
  add-chapter <idx> <title> [--outline=...] [--time=start~end] [--pov=<name>] [--location=...]
  generate-outline [target]  生成/刷新章节目录大纲
  show-context <idx>         打印第 N 章的上下文（用于调试）
  write-chapter <idx> [--words=N]   端到端生成+检查+入库
  write-raw <idx>            只生成正文不检查
  check <idx> <text-file>    对章节做硬校验（text-file 路径或 stdin）
  list-chapters              列出所有章节
  show-chapter <idx>         打印章节
  show-events [idx]          打印事件
  show-threads               打印伏笔
  show-character <name>      打印人物档案
  timeline                   打印完整时间线
  help
  quit
"""
from __future__ import annotations
import json
import shlex
import sys
import time
from pathlib import Path
from typing import Any

from .config import CONFIG
from .db import Database
from . import knowledge as kb
from . import retriever, writer, consistency
from . import scanner
from . import importer
from . import personality
from . import optimizer
from . import structure
from . import pipeline
from .ai_client import AIClient


BANNER = """
============================================================
 NovelAI Writer - 长篇小说 AI 辅助写作
 目标：事件链 / 时间顺序 / 人物性格 / 逻辑 / 信息把控 一致性
============================================================
"""


HELP = """
通用命令：
  help                              显示帮助
  init                              初始化项目
  set-synopsis <文本>               设置项目梗概
  set-style <文本>                  设置文风
  set-pov <限知视角|全知视角>        设置视角
  set-unit <日|小时|不定>            故事内时间单位
  show-project                      查看项目元信息
  web                               启动实时进度面板（浏览器打开 http://127.0.0.1:8765）

知识库（核心）：
  add-character                     交互式添加人物
  add-character-json '<json>'       直接以 JSON 字符串添加人物
  show-character <name>             查看人物档案
  list-characters                   列出所有人物
  add-world <cat> <name> <content>  添加世界观
  list-world [cat]                  列出世界观
  add-fact <content> [--category=...] [--reliability=...] [--known-by=id,id|public]
  list-facts [category]
  add-relationship <a_name> <b_name> <type> [state]
  web                               启动实时进度面板（http://127.0.0.1:8765）
  import-md <path>                  导入 Markdown 手稿（单文件/目录自动识别）
  scan-threads                      伏笔扫描
  scan-logic                        逻辑链扫描
  scan-style                        文风漂移扫描
  scan-all                          一键全扫
  extract-events <idx>              LLM 抽取单章正文为结构化事件
  extract-events-all                LLM 抽取全本所有章为事件
  extract-threads <idx>             LLM 抽取单章正文为伏笔
  extract-threads-all               LLM 抽取全本所有章为伏笔
  extract-all                       一键抽取：事件 + 伏笔 全本
  scan-structure [chapter|volume|full]  叙事结构分析（起承转合 + 重要性曲线 + 8 大问题）
  optimize-structure <level> [idx]    LLM 给出结构优化建议（level: full/volume/chapter）
  pipeline-quick                    快速诊断：4 扫描 + 结构分析（秒级，无 LLM）
  pipeline                          完整流水线：诊断 + LLM 优化 + 路线图（5-15 分钟）
  pipeline-llm                      仅跑 LLM 阶段（基于已跑的 quick 结果）
  set-mbti <name> <MBTI>            设置人物 MBTI（如 INTJ）+ 自动生成认知功能栈和 baseline 关键词
  show-character-matrix             展示人物 MBTI 矩阵与性格冲突
  scan-personality                  性格漂移扫描（不依赖 LLM）
  add-milestone <name> <chapter_idx> <type> <desc>  [选项: --dimension=... --before=... --after=... --quote=...]
  show-arc <name>                   展示指定人物成长线
  add-rel-evol <a> <b> <chapter> <intimacy> <trust> [conflict] [dynamics]
  optimize-personality <name>       LLM 性格优化建议
  optimize-arc <name>               LLM 成长线优化建议
  optimize-relationship <a> <b>    LLM 人物交会优化建议
  optimize-all                      LLM 全局优化建议
  list-suggestions [type]           列出所有建议（按 type 过滤：personality/arc/relationship/global）
  apply-suggestion <id>             标记建议为已应用
  dismiss-suggestion <id>           标记建议为已忽略

章节：
  add-chapter <idx> <title> [--outline=...] [--time=start~end] [--pov=<name>] [--location=...]
  list-chapters
  show-chapter <idx>
  show-events [idx]
  timeline

AI：
  generate-outline [target_chapters]   生成大纲（用 LLM）
  show-context <idx>                   打印第 N 章的上下文
  write-raw <idx> [--words=N]          只生成正文
  write-chapter <idx> [--words=N]      生成 + 一致性检查 + 入库
  check <idx> <text-file>              硬校验章节文本
  hard-check <idx>                     对已入库章节运行硬校验

  quit
"""


# ============================================================
# 命令实现
# ============================================================

def cmd_init(args, db, ai):
    p = kb.get_or_create_project(db)
    print(f"[init] 项目已存在或新建：{p['title']} (id={p['id']})")


def cmd_set(args, db, ai):
    if not args:
        print("用法：set-synopsis | set-style | set-pov | set-unit")
        return
    op = args[0]
    rest = " ".join(args[1:])
    if op == "synopsis":
        kb.update_project(db, synopsis=rest)
        print("[ok] 已更新梗概")
    elif op == "style":
        kb.update_project(db, style=rest)
        print("[ok] 已更新文风")
    elif op == "pov":
        if rest not in ("限知视角", "全知视角"):
            print("[warn] pov 应为 '限知视角' 或 '全知视角'，已强制写入")
        kb.update_project(db, pov_mode=rest or "限知视角")
        print("[ok] 已更新视角")
    elif op == "unit":
        kb.update_project(db, story_time_unit=rest or "日")
        print("[ok] 已更新时间单位")
    else:
        print(f"[err] 未知 set 子命令：{op}")


def cmd_show_project(args, db, ai):
    p = kb.get_or_create_project(db)
    for k in ("title", "synopsis", "style", "pov_mode", "story_time_unit"):
        print(f"  {k}: {p.get(k, '')}")


def cmd_web(args, db, ai):
    """启动实时进度面板（FastAPI + 静态前端）"""
    import webbrowser
    port = 8765
    if args and args[0].isdigit():
        port = int(args[0])
    print(f"[web] 启动实时进度面板…")
    print(f"[web] 打开浏览器: http://127.0.0.1:{port}")
    print(f"[web] 按 Ctrl+C 退出。")
    try:
        webbrowser.open(f"http://127.0.0.1:{port}")
    except Exception:
        pass
    from .web.app import run as web_run
    web_run(port)


def cmd_import_md(args, db, ai):
    if not args:
        print("用法: import-md <md文件或目录> [--title=...] [--unit=回|日|...]")
        return
    pos, flags = _parse_flags(args)
    path = pos[0]
    title = flags.get("title")
    unit = flags.get("unit", "回")
    print(f"[import-md] 导入 {path} …")
    try:
        result = importer.import_markdown(
            db, path,
            project_title=title,
            story_time_unit=unit,
            progress_cb=lambda s, m: print(f"  [{s}] {m}"),
        )
        print(f"\n[ok] 导入完成：{result['chapters']} 章，{result['words']} 字，{result['volumes']} 卷")
    except Exception as e:
        print(f"[err] {type(e).__name__}: {e}")


def cmd_scan_threads(args, db, ai):
    issues = scanner.scan_threads(db)
    if not issues:
        print("[ok] 伏笔扫描通过。")
        return
    print(f"[scan-threads] 发现 {len(issues)} 个问题：")
    for it in issues:
        print(f"  [{it['severity']}] {it['issue_type']} {it['title']}")
        print(f"      {it['context']}")
        if it.get("fix_suggestion"):
            print(f"      建议: {it['fix_suggestion']}")


def cmd_scan_logic(args, db, ai):
    result = scanner.scan_logic(db)
    s = result["summary"]
    print(f"[scan-logic] 总计 {s['total']} 个问题 (high={s['by_severity'].get('high',0)} medium={s['by_severity'].get('medium',0)} low={s['by_severity'].get('low',0)})")
    for k, label in [
        ("dead_appears", "死人复活"),
        ("location_clash", "地点冲突"),
        ("causality_reversed", "因果倒置"),
        ("info_leak", "信息泄漏"),
        ("chain_break", "事件链断裂"),
    ]:
        items = result.get(k, [])
        if not items:
            continue
        print(f"\n  [{label}] {len(items)} 个")
        for it in items[:10]:
            print(f"    [{it['severity']}] {it.get('context', '')[:120]}")


def cmd_scan_style(args, db, ai):
    pos, flags = _parse_flags(args)
    n = int(flags.get("baseline", 3))
    z = float(flags.get("threshold", 2.0))
    result = scanner.scan_style(db, baseline_first_n=n, z_threshold=z)
    print(f"[scan-style] 基线：第{result['baseline_range'][0]}-{result['baseline_range'][1]}章；阈值 z>={z}")
    print(f"漂移问题：{len(result['drift_issues'])} 个\n")
    for it in result["drift_issues"][:15]:
        print(f"  [{it['severity']}] 第{it['chapter_idx']}章 {it['dimension']}: z={it['z_score']} (值 {it['value']} vs 基线 {it['baseline']})")
        print(f"      {it.get('fix_suggestion', '')}")
    print("\n综合距离曲线：")
    for c in result["overall_drift_curve"]:
        bar = "█" * min(40, int(c["distance"] * 8))
        print(f"  第{c['idx']:2d}章  {c['distance']:5.2f}  {bar}")


def cmd_scan_all(args, db, ai):
    print("=" * 60)
    print("[1/3] 伏笔扫描…")
    cmd_scan_threads([], db, ai)
    print()
    print("=" * 60)
    print("[2/3] 逻辑链扫描…")
    cmd_scan_logic([], db, ai)
    print()
    print("=" * 60)
    print("[3/3] 文风漂移扫描…")
    cmd_scan_style([], db, ai)


def _check_ai_ready(ai) -> bool:
    if not ai.ready:
        print("[err] AI 未配置。请设置 NOVELAI_API_KEY 和 NOVELAI_BASE_URL。")
        return False
    return True


def cmd_extract_events(args, db, ai):
    if not args:
        print("用法: extract-events <chapter_idx>")
        return
    if not _check_ai_ready(ai):
        return
    idx = int(args[0])
    print(f"[extract-events] 正在抽取第 {idx} 章事件…")
    r = writer.extract_events_for_chapter(db, ai, idx)
    if not r.get("ok"):
        print(f"[err] {r.get('error')}")
        return
    print(f"  ✓ 新增 {r.get('added', 0)} 个事件（跳过 {r.get('skipped', 0)} 个）")
    for ev in r.get("events", []):
        print(f"    · [{ev.get('event_type','?')}] imp={ev.get('importance',3)} {ev.get('title','')}：{ev.get('summary','')[:60]}")


def cmd_extract_events_all(args, db, ai):
    if not _check_ai_ready(ai):
        return
    print(f"[extract-events-all] 正在抽取全本所有章事件（{len(kb.list_chapters(db))} 章）…")
    report = writer.extract_events_only(db, ai)
    ev = report["events"]
    print(f"  ✓ 事件抽取：{ev['ok']}/{ev['ok']+ev['failed']} 章成功，新增 {ev['added']} 个事件（跳过 {ev['skipped']}）")


def cmd_extract_threads(args, db, ai):
    if not args:
        print("用法: extract-threads <chapter_idx>")
        return
    if not _check_ai_ready(ai):
        return
    idx = int(args[0])
    print(f"[extract-threads] 正在抽取第 {idx} 章伏笔…")
    r = writer.extract_threads_for_chapter(db, ai, idx)
    if not r.get("ok"):
        print(f"[err] {r.get('error')}")
        return
    print(f"  ✓ 新增 {r.get('added', 0)} 个伏笔（关联 {r.get('linked', 0)} 个）")
    for th in r.get("threads", []):
        link = f" → linked#{th['linked_to']}" if th.get("linked_to") else ""
        print(f"    · [{th.get('status','?')}] {th.get('title','')}：{th.get('description','')[:60]}{link}")


def cmd_extract_threads_all(args, db, ai):
    if not _check_ai_ready(ai):
        return
    print(f"[extract-threads-all] 正在抽取全本所有章伏笔…")
    report = writer.extract_threads_only(db, ai)
    print(f"  ✓ 新增 {report['threads']['added']} 个伏笔（关联 {report['threads']['linked']}）")


def cmd_extract_all(args, db, ai):
    if not _check_ai_ready(ai):
        return
    chapters = kb.list_chapters(db)
    print(f"[extract-all] 正在抽取全本 {len(chapters)} 章的事件 + 伏笔…")
    print(f"  预计调用 {len(chapters)*2} 次 LLM（每章 2 次：事件 + 伏笔）")
    report = writer.extract_all(db, ai)
    print()
    print("=" * 60)
    print("📊 抽取报告")
    print("=" * 60)
    print(f"  事件：{report['events']['ok']}/{report['events']['ok']+report['events']['failed']} 章成功")
    print(f"       新增 {report['events']['added']} 个事件（跳过 {report['events']['skipped']}）")
    print(f"  伏笔：{report['threads']['ok']}/{report['threads']['ok']+report['threads']['failed']} 章成功")
    print(f"       新增 {report['threads']['added']} 个伏笔（自动关联 {report['threads']['linked']} 个）")
    print()
    print("各章详情：")
    print(f"  {'章节':<8} {'事件':<6} {'伏笔':<6} {'状态'}")
    print("  " + "-" * 40)
    n = min(len(report["events"]["details"]), len(report["threads"]["details"]))
    for i in range(n):
        ed = report["events"]["details"][i]
        td = report["threads"]["details"][i]
        status = "✓" if not ed.get("error") and not td.get("error") else "✗"
        print(f"  第{ed['chapter_idx']:>2}回  +{ed['added']:<3}   +{td.get('added',0):<3}   {status}")


def cmd_scan_structure(args, db, ai):
    """叙事结构分析（不依赖 LLM）"""
    pos, _ = _parse_flags(args)
    level = pos[0] if pos else "full"
    ana = structure.StructureAnalyzer(db)
    if level == "full" or level not in ("chapter", "volume"):
        r = ana.analyze_full()
        if r.get("error"):
            print(f"[err] {r['error']}"); return
        print(f"\n{'='*60}\n📊 全篇结构\n{'='*60}")
        print(f"  卷: {r['n_volumes']}  章节: {r['n_chapters']}  字数: {r['total_words']:,}  事件: {r['n_events']}  turning_point: {r['n_turning_points']}")
        print(f"  全篇高潮位置: {r['climax_position']}（在第 {r['climax_chapter_idx']} 章）")
        print(f"\n  【4 段事件分布】")
        for p, info in r["phase_breakdown"].items():
            print(f"    {info['label']:<10}  pos {info['position_range'][0]:.2f}-{info['position_range'][1]:.2f}  事件 {info['n_events']:>2}  imp {info['importance_avg']}  埋 {info['n_threads_planted']}  揭 {info['n_threads_payoff']}")
        print(f"\n  【重要性曲线】")
        for c in r["intensity_curve"]:
            bar = "█" * int(c["intensity"] * 4)
            marker = "🔥" if c["n_turning"] else "  "
            print(f"    第{c['chapter_idx']:>2}章  pos={c['position']:.2f}  int={c['intensity']:>4.1f}  n={c['n_events']:>2} {marker} {bar}")
        print(f"\n  【结构问题】（{len(r['issues'])} 个）")
        for it in r["issues"]:
            print(f"    [{it['severity']}] {it['type']}: {it['context']}")
    elif level == "volume":
        for v in kb.list_volumes(db):
            r = ana.analyze_volume(v["idx"])
            if r.get("error"): continue
            print(f"\n  第 {v['idx']} 卷《{v['title']}》：")
            print(f"    章节 {r['n_chapters']}, 字数 {r['word_count']:,}, 事件 {r['n_events']}, 转折 {r['n_turning_points']} @ {r['turning_positions']}")
            for p, info in r["phase_breakdown"].items():
                cr = info.get("chapter_range", [0, 0])
                print(f"    {info['label']:<10}  第 {cr[0]:>2}-{cr[1]:>2} 章  事件 {info['n_events']:>2}  imp {info['importance_avg']}  埋 {info['n_threads_planted']}  揭 {info['n_threads_payoff']}")
            if r["issues"]:
                print(f"    问题（{len(r['issues'])}）:")
                for it in r["issues"]:
                    print(f"      [{it['severity']}] {it['type']}: {it['context']}")
    elif level == "chapter":
        chapters = kb.list_chapters(db)
        for ch in chapters:
            r = ana.analyze_chapter(ch["idx"])
            if r.get("error"): continue
            tag = " ⚠" if r["issues"] else ""
            print(f"  第{r['chapter_idx']:>2}回《{r['title'][:25]}》 wc={r['word_count']:>5}  n={r['n_events']:>2}  turn={r['n_turning_points']}  imp={r['importance_avg']}{tag}")


def cmd_optimize_structure(args, db, ai):
    pos, flags = _parse_flags(args)
    if not pos:
        print("用法: optimize-structure <level> [idx]")
        print("      level: full | volume | chapter")
        return
    level = pos[0]
    try:
        idx = int(pos[1]) if len(pos) > 1 else None
    except ValueError:
        print(f"[err] 编号必须是数字，收到: {pos[1]}")
        return
    if level not in ("full", "volume", "chapter"):
        print("[err] level 必须是 full / volume / chapter")
        return
    if not _check_ai_ready(ai):
        return
    print(f"[optimize-structure] 正在用 LLM 分析 {level} 层结构" + (f"（第 {idx} " if idx else "") + "）…")
    opt = optimizer.Optimizer(db, ai)
    sugs = opt.optimize_structure(level, idx)
    if not sugs:
        print("[ok] 无建议（或 LLM 不可用）")
        return
    print(f"\n[ok] 生成 {len(sugs)} 条建议：\n")
    for i, s in enumerate(sugs, 1):
        pri = s.get("priority", "medium")
        marker = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(pri, "⚪")
        print(f"{marker} [{pri}] {s.get('title','')}")
        if s.get("chapter_focus"):
            print(f"    范围: {s['chapter_focus']}")
        if s.get("expected_impact"):
            print(f"    预期影响: {s['expected_impact']}")
        if s.get("evidence"):
            print(f"    依据: {s['evidence']}")
        print(f"    {s.get('content','')[:300]}{'…' if len(s.get('content',''))>300 else ''}")
        print()


def cmd_pipeline_quick(args, db, ai):
    """快速诊断（秒级，无 LLM）"""
    print("=" * 60)
    print("🚀 修改流水线 · 阶段 1: 快速诊断")
    print("=" * 60)
    t0 = time.time()
    report = pipeline.run_quick_pipeline(db)
    print(f"\n  ✓ 阶段 1 完成（{report['elapsed_seconds']}s）")
    print()
    print(f"  项目概况: {report['health']['n_chapters']} 章 / {report['health']['n_events']} 事件 / "
          f"{report['health']['n_threads']} 伏笔 / {report['health']['n_characters']} 人物 / "
          f"{report['health']['n_characters_with_mbti']} 已标 MBTI / {report['health']['total_words']:,} 字")
    print()
    print("  5 类问题分布：")
    for cat_key, label in [
        ("thread", "🧵 伏笔"),
        ("logic", "🔗 逻辑链"),
        ("style", "📜 文风"),
        ("personality", "🎭 性格"),
        ("structure", "📊 结构"),
    ]:
        info = report["issues_by_category"][cat_key]
        high_mark = f" (H={info.get('high',0)})" if info.get("high", 0) else ""
        print(f"    {label:<10}  {info['count']:>3} 个{high_mark}")
    print()
    # 阶段 2: 路线图（只用硬规则部分）
    print("=" * 60)
    print("📋 阶段 2: 修改路线图（基于硬规则）")
    print("=" * 60)
    roadmap = pipeline.build_roadmap(report, llm_suggestions=None)
    print(f"\n  共 {len(roadmap)} 项（前 15 个）：\n")
    for item in roadmap[:15]:
        sev_mark = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(item["severity"], "⚪")
        ch_ref = f"第{item['chapter_ref']}章" if item["chapter_ref"] else "全局"
        print(f"  #{item['rank']:>2}  {sev_mark} [{ch_ref}] {item['category']} {item['type']}")
        print(f"       {item['title'][:60]}")
    if len(roadmap) > 15:
        print(f"  …（共 {len(roadmap)} 项，省略后 {len(roadmap)-15} 项）")
    print()
    print(f"⏱️  总耗时 {time.time()-t0:.1f}s")


def cmd_pipeline(args, db, ai):
    """完整流水线（阶段 1 + 2 + 3，含 LLM 优化）"""
    print("=" * 60)
    print("🚀 修改流水线 · 完整版（含 LLM 优化）")
    print("=" * 60)
    print("  阶段 1: 快速诊断")
    print("  阶段 2: 合并去重 → 路线图")
    print("  阶段 3: LLM 优化（5 类，预计 5-15 分钟）")
    print()
    t0 = time.time()
    def cb(stage, msg):
        print(f"  [{stage}] {msg}")
    report = pipeline.run_full_pipeline(db, ai, progress_cb=cb)
    print()
    print("=" * 60)
    print("📊 流水线汇总")
    print("=" * 60)
    s = report["summary"]
    print(f"  扫描问题: {s['total_scanner_issues']} 个（H={s['high_issues']}）")
    print(f"  LLM 建议: {s['llm_suggestions']} 条")
    print(f"  路线图:   {s['roadmap_items']} 项")
    print(f"  扫描耗时: {s['elapsed_total_seconds']:.1f}s")
    print(f"  总耗时:   {time.time()-t0:.1f}s")
    print()
    print("  路线图前 10 项：")
    for item in report["roadmap"][:10]:
        sev_mark = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(item["severity"], "⚪")
        ch_ref = f"第{item['chapter_ref']}章" if item["chapter_ref"] else "全局"
        print(f"    #{item['rank']:>2}  {sev_mark} [{ch_ref}] {item['category']} {item['type']}: {item['title'][:50]}")


def cmd_pipeline_llm(args, db, ai):
    """仅跑 LLM 阶段（基于已跑的 quick 结果）"""
    if not _check_ai_ready(ai):
        return
    print("=" * 60)
    print("🤖 仅 LLM 优化阶段")
    print("=" * 60)
    print("  先跑 quick…")
    quick = pipeline.run_quick_pipeline(db)
    print(f"  ✓ quick 完成，{sum(v['count'] for v in quick['issues_by_category'].values())} 个问题")
    print()
    print("  开始 LLM 阶段…")
    llm_suggestions = []
    opt = optimizer.Optimizer(db, ai)
    sugs = opt.optimize_all()
    for s in sugs: s["target_label"] = "全局"
    llm_suggestions.extend(sugs)
    print(f"  [1/5] 全局: {len(sugs)}")
    sugs = opt.optimize_structure("full")
    for s in sugs: s["target_label"] = "全篇结构"
    llm_suggestions.extend(sugs)
    print(f"  [2/5] 全篇结构: {len(sugs)}")
    chars = [c for c in kb.list_characters(db) if c.get("mbti") and c.get("role") in ("protagonist", "antagonist", "supporting")]
    for ch in chars[:5]:
        sugs = opt.optimize_personality(ch["name"])
        for s in sugs: s["target_label"] = f"性格: {ch['name']}"
        llm_suggestions.extend(sugs)
        sugs = opt.optimize_arc(ch["name"])  # BUG 修复：原漏掉弧光，输出却宣称"性格 + 弧光"
        for s in sugs: s["target_label"] = f"弧光: {ch['name']}"
        llm_suggestions.extend(sugs)
    print(f"  [3-4/5] 性格 + 弧光（5 人物）: {len([s for s in llm_suggestions if s.get('target_label','').startswith(('性格','弧光'))])}")
    rels = kb.list_relationships(db)
    for r in rels[:3]:
        a = kb.get_character(db, r["char_a_id"])
        b = kb.get_character(db, r["char_b_id"])
        if not a or not b: continue
        sugs = opt.optimize_relationship(a["name"], b["name"])
        for s in sugs: s["target_label"] = f"关系: {a['name']}↔{b['name']}"
        llm_suggestions.extend(sugs)
    print(f"  [5/5] 关系（3 对）: {len([s for s in llm_suggestions if s.get('target_label','').startswith('关系')])}")
    print(f"\n  ✓ 共 {len(llm_suggestions)} 条 LLM 建议（已入库）")


def cmd_set_mbti(args, db, ai):
    if len(args) < 2:
        print("用法: set-mbti <name> <MBTI>")
        return
    name, mbti = args[0], args[1].upper()
    c = kb.find_character_by_name(db, name)
    if not c:
        print(f"[err] 人物不存在: {name}")
        return
    if mbti not in personality.MBTI_STACK:
        print(f"[err] 未知 MBTI 类型: {mbti}")
        print(f"      可选: {', '.join(personality.MBTI_STACK.keys())}")
        return
    stack = personality.get_stack(mbti)
    kws = personality.mbti_to_keywords(mbti)
    kb.update_character(
        db, c["id"],
        mbti=mbti,
        cognitive_stack="-".join(stack),
        baseline_keywords=kws,
    )
    print(f"[ok] {name}: MBTI={mbti}  认知栈={'-'.join(stack)}")
    print(f"     自动生成 baseline 关键词 {len(kws)} 个：{', '.join(kws[:8])}{'...' if len(kws)>8 else ''}")


def cmd_show_character_matrix(args, db, ai):
    chars = kb.list_characters(db)
    main_chars = [c for c in chars if c.get("role") in ("protagonist", "antagonist", "supporting") and c.get("mbti")]
    if not main_chars:
        print("[warn] 还没有任何人物设置 MBTI。先用 set-mbti <name> <MBTI>。")
        return
    mat = personality.build_character_matrix(main_chars)
    print(f"\n=== 人物 MBTI 矩阵（{len(mat['characters'])} 人）===\n")
    print(f"{'人物':<10} {'MBTI':<6} {'认知栈':<16} {'主功能':<6} {'弧光类型':<10} {'进度':<6}")
    print("-" * 60)
    for c in mat["characters"]:
        prog = f"{c['arc_progress']*100:.0f}%" if c["arc_progress"] is not None else "—"
        print(f"{c['name']:<10} {c['mbti']:<6} {c['stack_str']:<16} {c['cognitive_dominant']:<6} {c['arc_type'] or '—':<10} {prog:<6}")
    print("\n=== 性格兼容性矩阵（数字 0~1，越高越契合）===\n")
    names = [c["name"] for c in mat["characters"]]
    # 表头
    print(f"{'':10}" + "".join(f"{n[:6]:>8}" for n in names))
    for a in names:
        row = f"{a[:10]:10}"
        for b in names:
            score = mat["matrix"][a][b].get("score", 0)
            if a == b:
                row += f"{'—':>8}"
            else:
                row += f"{score:>8.2f}"
        print(row)
    print("\n=== 兼容性解读（每对组合）===")
    shown = set()
    for a in names:
        for b in names:
            if a >= b:
                continue
            k = tuple(sorted([a, b]))
            if k in shown:
                continue
            shown.add(k)
            data = mat["matrix"][a][b]
            print(f"  {a} ↔ {b}: {data.get('interpretation','')}")


def cmd_scan_personality(args, db, ai):
    pos, flags = _parse_flags(args)
    chars = [c for c in kb.list_characters(db) if c.get("mbti")]
    if not chars:
        print("[warn] 没有 MBTI 标注的人物。用 set-mbti 设置。")
        return
    window = int(flags.get("window", 0)) or None
    results = personality.scan_personality_drift(db, chars, chapter_window=window)
    if not results:
        print("没有数据。")
        return
    # 按角色分组
    by_char: dict[int, list[dict]] = {}
    for r in results:
        by_char.setdefault(r["char_id"], []).append(r)
    print(f"[scan-personality] {len(chars)} 角色 / {len(results)} 章次分析\n")
    for char_id, rows in by_char.items():
        char = kb.get_character(db, char_id)
        if not char:
            continue
        print(f"── {char['name']} (MBTI={char['mbti']}, 认知栈={char['cognitive_stack']}) ──")
        # 统计该角色每章的 baseline_overlap、drift_signals 数
        rows.sort(key=lambda r: r["chapter_idx"])
        any_signal = False
        for r in rows:
            if r["drift_signals"]:
                any_signal = True
                print(f"  第{r['chapter_idx']:>2}章 {r['chapter_title'][:14]:<14}  baseline重叠={r['baseline_overlap']:.2f}  推断={r['inferred_mbti']}")
                for s in r["drift_signals"]:
                    print(f"      ⚠ {s}")
        if not any_signal:
            print(f"  ✅ 全本 {len(rows)} 章内性格稳定")


def cmd_add_milestone(args, db, ai):
    if len(args) < 4:
        print("用法: add-milestone <name> <chapter_idx> <type> <desc>")
        print("      type: starting_point | catalyst | crisis | climax | resolution | ending")
        return
    pos, flags = _parse_flags(args)
    try:
        ch_idx = int(pos[1])
    except (ValueError, IndexError):
        print(f"[err] 章节序号必须是数字，收到: {pos[1] if len(pos) > 1 else '无'}")
        return
    name, mtype, desc = pos[0], pos[2], " ".join(pos[3:])
    char = kb.find_character_by_name(db, name)
    if not char:
        print(f"[err] 人物不存在: {name}")
        return
    ch = kb.get_chapter_by_idx(db, ch_idx)
    if not ch:
        print(f"[err] 章节不存在: {ch_idx}")
        return
    dimension = flags.get("dimension", "personality")
    before = flags.get("before", "")
    after = flags.get("after", "")
    quote = flags.get("quote", "")
    mid = kb.add_milestone(
        db, character_id=char["id"], chapter_id=ch["id"],
        milestone_type=mtype, description=desc,
        dimension=dimension, before_state=before, after_state=after,
        quote=quote, importance=int(flags.get("importance", 3)),
    )
    # 自动推进 arc_progress（每加一个 milestone +0.1）
    cur = char.get("arc_progress") or 0.0
    new_prog = min(1.0, cur + 0.1)
    kb.update_character(db, char["id"], arc_progress=new_prog)
    print(f"[ok] 里程碑 id={mid}: 第{ch_idx}章《{ch['title']}》 {name} 发生「{mtype}」")
    print(f"     {desc}")
    print(f"     arc_progress: {cur:.2f} → {new_prog:.2f}")


def cmd_show_arc(args, db, ai):
    if not args:
        print("用法: show-arc <name>")
        return
    name = args[0]
    char = kb.find_character_by_name(db, name)
    if not char:
        print(f"[err] 人物不存在: {name}")
        return
    ms = kb.list_milestones(db, character_id=char["id"])
    ch_by_id = {c["id"]: c for c in kb.list_chapters(db)}
    print(f"\n=== {name} 成长线（MBTI={char.get('mbti') or '?'}  进度={char.get('arc_progress') or 0:.0%}）===\n")
    if not ms:
        print("（暂无里程碑）")
        return
    ms.sort(key=lambda m: ch_by_id.get(m["chapter_id"], {}).get("idx", 0))
    for m in ms:
        ch = ch_by_id.get(m["chapter_id"], {})
        idx = ch.get("idx", "?")
        print(f"  第{idx}章 · [{m['milestone_type']}] · {m.get('dimension','')}")
        print(f"      {m['description']}")
        if m.get("before_state") or m.get("after_state"):
            print(f"      变化：{m.get('before_state','?')} → {m.get('after_state','?')}")
        if m.get("quote"):
            print(f"      「{m['quote']}」")
    # 进度条
    prog = char.get("arc_progress") or 0.0
    bar = "█" * int(prog * 30) + "░" * (30 - int(prog * 30))
    print(f"\n  弧光进度: [{bar}] {prog*100:.0f}%")


def cmd_add_rel_evol(args, db, ai):
    if len(args) < 5:
        print("用法: add-rel-evol <a_name> <b_name> <chapter_idx> <intimacy> <trust> [conflict] [dynamics]")
        print("      intimacy/trust: -1.0~1.0；conflict: 0.0~1.0")
        return
    a, b, ch_idx, intimacy, trust = args[0], args[1], int(args[2]), float(args[3]), float(args[4])
    conflict = float(args[5]) if len(args) > 5 else None
    dynamics = args[6] if len(args) > 6 else ""
    ca = kb.find_character_by_name(db, a)
    cb = kb.find_character_by_name(db, b)
    if not ca or not cb:
        print("[err] 人物不存在")
        return
    ch = kb.get_chapter_by_idx(db, ch_idx)
    if not ch:
        print(f"[err] 章节不存在: {ch_idx}")
        return
    # 找现有关系
    rels = kb.get_relationships_for(db, ca["id"])
    target_rel = None
    for r in rels:
        if (r["char_a_id"] == cb["id"]) or (r["char_b_id"] == cb["id"]):
            target_rel = r
            break
    if not target_rel:
        # 创建默认关系
        rid = kb.add_relationship(db, ca["id"], cb["id"], "未分类", description="自动创建用于追踪")
        target_rel = kb.get_relationship(db, rid)
    rev_id = kb.add_rel_evolution(
        db, relationship_id=target_rel["id"], chapter_id=ch["id"],
        intimacy=intimacy, trust=trust, conflict=conflict,
        dynamics=dynamics,
    )
    print(f"[ok] 关系演变记录 id={rev_id}")
    print(f"     {a} ↔ {b} 在第{ch_idx}章：亲密度={intimacy:+.2f}  信任={trust:+.2f}  冲突={conflict or 0:.2f}  动态={dynamics or '—'}")


def _run_optimizer(db, ai, kind: str, *args):
    opt = optimizer.Optimizer(db, ai)
    method = {
        "personality": opt.optimize_personality,
        "arc": opt.optimize_arc,
        "relationship": opt.optimize_relationship,
        "all": opt.optimize_all,
    }.get(kind)
    if not method:
        print(f"[err] 未知类型: {kind}")
        return
    print(f"[optimize-{kind}] 调用 LLM 生成建议…")
    suggestions = method(*args)
    if not suggestions:
        print("[ok] 无建议")
        return
    print(f"\n[ok] 生成 {len(suggestions)} 条建议（已入库）：\n")
    for i, s in enumerate(suggestions, 1):
        pri = s.get("priority", "medium")
        marker = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(pri, "⚪")
        print(f"{marker} [{pri}] {s.get('title','')}")
        if s.get("chapter_focus"):
            print(f"    范围: {s['chapter_focus']}")
        if s.get("evidence"):
            print(f"    依据: {s['evidence']}")
        print(f"    {s.get('content','')[:300]}{'…' if len(s.get('content',''))>300 else ''}")
        print()


def cmd_optimize_personality(args, db, ai):
    if not args:
        print("用法: optimize-personality <name>")
        return
    _run_optimizer(db, ai, "personality", args[0])


def cmd_optimize_arc(args, db, ai):
    if not args:
        print("用法: optimize-arc <name>")
        return
    _run_optimizer(db, ai, "arc", args[0])


def cmd_optimize_relationship(args, db, ai):
    if len(args) < 2:
        print("用法: optimize-relationship <a_name> <b_name>")
        return
    _run_optimizer(db, ai, "relationship", args[0], args[1])


def cmd_optimize_all(args, db, ai):
    _run_optimizer(db, ai, "all")


def cmd_list_suggestions(args, db, ai):
    pos, _ = _parse_flags(args)
    t = pos[0] if pos else None
    if t and t not in ("personality", "arc", "relationship", "global"):
        print("[err] type 必须是 personality/arc/relationship/global")
        return
    sugs = kb.list_suggestions(db, target_type=t, status="open")
    if not sugs:
        print("[ok] 没有待处理建议")
        return
    print(f"[list-suggestions] 共 {len(sugs)} 条待处理建议：\n")
    for s in sugs:
        pri = s.get("priority", "medium")
        marker = {"high": "🔴", "medium": "🟡", "low": "🟢"}.get(pri, "⚪")
        print(f"  {marker} [#{s['id']}|{s['target_type']}|{pri}] {s['title']}")
        if s.get("target_label"):
            print(f"      目标: {s['target_label']}")
        if s.get("chapter_focus"):
            print(f"      范围: {s['chapter_focus']}")
        print(f"      {s.get('content','')[:200]}{'…' if len(s.get('content',''))>200 else ''}")
        print()


def cmd_apply_suggestion(args, db, ai):
    if not args:
        print("用法: apply-suggestion <id>")
        return
    sid = int(args[0])
    s = kb.get_suggestion(db, sid)
    if not s:
        print(f"[err] 建议 #{sid} 不存在")
        return
    kb.update_suggestion_status(db, sid, "applied")
    print(f"[ok] 建议 #{sid} 标记为已应用")


def cmd_dismiss_suggestion(args, db, ai):
    if not args:
        print("用法: dismiss-suggestion <id>")
        return
    sid = int(args[0])
    s = kb.get_suggestion(db, sid)
    if not s:
        print(f"[err] 建议 #{sid} 不存在")
        return
    kb.update_suggestion_status(db, sid, "dismissed")
    print(f"[ok] 建议 #{sid} 标记为已忽略")


def _parse_flags(args: list[str]) -> tuple[list[str], dict]:
    pos = []
    flags = {}
    i = 0
    while i < len(args):
        a = args[i]
        if a.startswith("--"):
            key = a[2:]
            if "=" in key:
                k, v = key.split("=", 1)
                flags[k] = v
            else:
                # 取下一个作为值；若下一个也是 --flag 则视为布尔
                if i + 1 < len(args) and not args[i + 1].startswith("--"):
                    flags[key] = args[i + 1]
                    i += 1
                else:
                    flags[key] = "true"
        else:
            pos.append(a)
        i += 1
    return pos, flags


def cmd_add_character(args, db, ai):
    if not args:
        print("进入交互式添加人物模式（直接回车结束）。")
        name = input("  姓名: ").strip()
        if not name:
            print("[cancel]")
            return
        aliases = input("  别号 (逗号分隔): ").strip()
        role = input("  角色 (protagonist/antagonist/supporting): ").strip() or "supporting"
        basic_info = input("  基础信息 (年龄/性别/外貌/职业/出身): ").strip()
        personality = input("  性格关键词/价值观/恐惧/欲望: ").strip()
        speech_style = input("  说话风格/口头禅: ").strip()
        abilities = input("  能力/技能: ").strip()
        arc = input("  人物弧光 (起点→转折→终点): ").strip()
        status = input("  当前状态: ").strip()
    elif args[0] == "-json" and len(args) >= 2:
        d = json.loads(args[1])
        valid_params = {"name", "aliases", "role", "basic_info", "personality",
                        "speech_style", "abilities", "arc", "status",
                        "mbti", "cognitive_stack", "enneagram", "arc_type",
                        "arc_progress", "baseline_keywords", "extra"}
        filtered = {k: v for k, v in d.items() if k in valid_params}
        cid = kb.add_character(db, **filtered)
        print(f"[ok] 已添加人物 id={cid} name={d.get('name')}")
        return
    else:
        # 简易：name=... role=... ... 这种 key=value 形式
        flags = {}
        for a in args:
            if "=" in a:
                k, v = a.split("=", 1)
                flags[k] = v
        if "name" not in flags:
            print("[err] 至少需要 name=...")
            return
        aliases = [x.strip() for x in flags.pop("aliases", "").split(",") if x.strip()] or None
        # 只传递 add_character 接受的参数
        valid_params = {"name", "aliases", "role", "basic_info", "personality",
                        "speech_style", "abilities", "arc", "status",
                        "mbti", "cognitive_stack", "enneagram", "arc_type",
                        "arc_progress", "baseline_keywords", "extra"}
        filtered = {k: v for k, v in flags.items() if k in valid_params}
        cid = kb.add_character(db, aliases=aliases, **filtered)
        print(f"[ok] 已添加人物 id={cid} name={flags['name']}")
        return

    aliases_list = [x.strip() for x in aliases.split(",") if x.strip()] or None
    cid = kb.add_character(
        db,
        name=name, aliases=aliases_list, role=role,
        basic_info=basic_info, personality=personality,
        speech_style=speech_style, abilities=abilities,
        arc=arc, status=status,
    )
    print(f"[ok] 已添加人物 id={cid} name={name}")


def cmd_show_character(args, db, ai):
    if not args:
        print("用法: show-character <name>")
        return
    c = kb.find_character_by_name(db, args[0])
    if not c:
        print(f"[err] 未找到人物 {args[0]}")
        return
    for k in ("name", "aliases", "role", "basic_info", "personality",
              "speech_style", "abilities", "arc", "status"):
        v = c.get(k)
        if v:
            print(f"  {k}: {v}")


def cmd_list_characters(args, db, ai):
    for c in kb.list_characters(db):
        basic = c.get('basic_info') or ''
        print(f"  [{c['id']}] {c['name']} ({c.get('role','')}) — {basic[:50]}")


def cmd_add_world(args, db, ai):
    if len(args) < 3:
        print("用法: add-world <category> <name> <content>")
        return
    cat, name, content = args[0], args[1], " ".join(args[2:])
    wid = kb.add_world(db, cat, name, content)
    print(f"[ok] 已添加世界观 id={wid}")


def cmd_list_world(args, db, ai):
    cat = args[0] if args else None
    for w in kb.list_world(db, cat):
        print(f"  [{w['category']}] {w['name']}: {w['content'][:60]}")


def cmd_add_fact(args, db, ai):
    pos, flags = _parse_flags(args)
    if not pos:
        print("用法: add-fact <content> [--category=...] [--reliability=reliable|rumored|secret|false] [--known-by=id,id|public]")
        return
    content = " ".join(pos)
    cat = flags.get("category", "general")
    rel = flags.get("reliability", "reliable")
    kb_raw = flags.get("known-by", flags.get("known_by", "public"))
    if kb_raw == "public":
        known_by: list[int] = []
    else:
        known_by = [int(x) for x in kb_raw.split(",") if x.strip().isdigit()]
    fid = kb.add_fact(db, content=content, category=cat, reliability=rel, known_by=known_by)
    print(f"[ok] 已添加事实 id={fid} category={cat} reliability={rel} known_by={known_by}")


def cmd_list_facts(args, db, ai):
    cat = args[0] if args else None
    for f in kb.list_facts(db, cat):
        print(f"  [{f['id']}|{f.get('category','')}|{f.get('reliability','')}] {f['content']}  known_by={f.get('known_by',[])}")


def cmd_add_relationship(args, db, ai):
    if len(args) < 3:
        print("用法: add-relationship <a_name> <b_name> <type> [state]")
        return
    a, b, t = args[0], args[1], args[2]
    state = args[3] if len(args) > 3 else ""
    ca = kb.find_character_by_name(db, a)
    cb = kb.find_character_by_name(db, b)
    if not ca or not cb:
        print("[err] 找不到人物")
        return
    rid = kb.add_relationship(db, ca["id"], cb["id"], t, current_state=state)
    print(f"[ok] 已添加关系 id={rid}")


def cmd_add_thread(args, db, ai):
    if len(args) < 2:
        print("用法: add-thread <title> <desc> [--type=...] [--status=...]")
        return
    pos, flags = _parse_flags(args)
    title = pos[0]
    desc = " ".join(pos[1:]) if len(pos) > 1 else ""
    ttype = flags.get("type", "foreshadow")
    status = flags.get("status", "planted")
    tid = kb.add_thread(db, title, desc, thread_type=ttype, status=status)
    print(f"[ok] 已添加伏笔 id={tid} status={status}")


def cmd_add_chapter(args, db, ai):
    pos, flags = _parse_flags(args)
    if len(pos) < 2:
        print("用法: add-chapter <idx> <title> [--outline=...] [--time=start~end] [--pov=<name>] [--location=...]")
        return
    try:
        idx = int(pos[0])
    except ValueError:
        print(f"[err] 章节序号必须是数字，收到: {pos[0]}")
        return
    title = pos[1]
    outline = flags.get("outline", "")
    time_s = time_e = None
    if "time" in flags:
        try:
            ts, te = flags["time"].split("~")
            time_s = float(ts)
            time_e = float(te)
        except Exception:
            print("[warn] --time 格式错误，应为 start~end")
    pov_id = None
    if "pov" in flags:
        c = kb.find_character_by_name(db, flags["pov"])
        if c:
            pov_id = c["id"]
    loc = flags.get("location", "")
    cid = kb.add_chapter(
        db, idx=idx, title=title, outline=outline,
        story_time_start=time_s, story_time_end=time_e,
        location=loc, pov_character_id=pov_id,
    )
    print(f"[ok] 已添加章节 id={cid} idx={idx}")


def cmd_list_chapters(args, db, ai):
    for c in kb.list_chapters(db):
        w = c.get("word_count") or 0
        print(f"  [{c['idx']}] {c['title']} ({w}字) @t={c.get('story_time_start')}~{c.get('story_time_end')} loc={c.get('location','')}")


def cmd_show_chapter(args, db, ai):
    if not args:
        print("用法: show-chapter <idx>")
        return
    ch = kb.get_chapter_by_idx(db, int(args[0]))
    if not ch:
        print("[err] 章节不存在")
        return
    print(f"=== 第{ch['idx']}章 {ch['title']} ===")
    if ch.get("outline"):
        print(f"[大纲]\n{ch['outline']}\n")
    if ch.get("summary"):
        print(f"[摘要]\n{ch['summary']}\n")
    if ch.get("final_text"):
        print(f"[正文]\n{ch['final_text']}\n")
    elif ch.get("draft"):
        print(f"[草稿]\n{ch['draft']}\n")


def cmd_show_events(args, db, ai):
    if not args:
        print("用法: show-events <idx>")
        return
    try:
        idx = int(args[0])
    except ValueError:
        print(f"[err] 章节序号必须是数字，收到: {args[0]}")
        return
    ch = kb.get_chapter_by_idx(db, idx)
    if not ch:
        print(f"[err] 第 {idx} 章不存在")
        return
    for e in kb.list_events(db, ch["id"]):
        parts = [e["title"], f"@{e['story_time']}", f"[{e.get('event_type','')}]", e["summary"]]
        print("  " + " ".join(parts))


def cmd_timeline(args, db, ai):
    print("=== 故事时间线 ===")
    events = kb.list_events(db)
    if not events:
        print("（暂无事件）")
        return
    for e in events:
        ch = kb.get_chapter(db, e["chapter_id"])
        idx = ch["idx"] if ch else "?"
        print(f"  第{idx}章 @{e['story_time']} [{e.get('event_type','')}] {e['title']}：{e['summary']}")


def cmd_generate_outline(args, db, ai):
    if not ai.ready:
        print("[err] AI 未配置 API key，无法生成大纲。请设置 NOVELAI_API_KEY。")
        return
    target = int(args[0]) if args else 30
    print(f"[ai] 正在生成 {target} 章大纲…")
    data = writer.generate_outline(db, ai, target_chapters=target)
    n = len(data.get("chapters", []))
    print(f"[ok] 已写入 {n} 个章节大纲")
    if data.get("structural_notes"):
        print("\n[结构备注]")
        print(data["structural_notes"])


def cmd_show_context(args, db, ai):
    if not args:
        print("用法: show-context <idx>")
        return
    ctx = retriever.build_chapter_context(db, int(args[0]))
    for k, v in ctx.items():
        print(f"\n--- {k} ---\n{v}")


def cmd_write_raw(args, db, ai):
    if not ai.ready:
        print("[err] AI 未配置 API key。")
        return
    pos, flags = _parse_flags(args)
    if not pos:
        print("用法: write-raw <idx> [--words=N]")
        return
    try:
        idx = int(pos[0])
    except ValueError:
        print(f"[err] 章节序号必须是数字，收到: {pos[0]}")
        return
    words = int(flags.get("words", 0)) or None
    text = writer.generate_chapter(db, ai, idx, target_words=words)
    chapter = kb.get_chapter_by_idx(db, idx)
    if chapter:
        kb.update_chapter(db, chapter["id"], draft=text, word_count=len(text))
    print(f"[ok] 第 {idx} 章已生成草稿 ({len(text)} 字)，未做检查。")
    print("=" * 60)
    print(text)


def cmd_write_chapter(args, db, ai):
    if not ai.ready:
        print("[err] AI 未配置 API key。")
        return
    pos, flags = _parse_flags(args)
    if not pos:
        print("用法: write-chapter <idx> [--words=N]")
        return
    try:
        idx = int(pos[0])
    except ValueError:
        print(f"[err] 章节序号必须是数字，收到: {pos[0]}")
        return
    words = int(flags.get("words", 0)) or None
    print(f"[ai] 端到端生成第 {idx} 章…")
    result = writer.write_chapter_pipeline(db, ai, idx, target_words=words)
    cr = result["consistency_report"]
    print(f"\n[ok] 生成完成。字数={len(result['text'])}，事件数={len(result['events'])}，"
          f"一致性通过={cr.get('passed')}，高严重问题={sum(1 for i in (cr.get('issues') or []) if i.get('severity')=='high')}")
    print(f"\n[摘要]\n{result['summary']}")
    if cr.get("issues"):
        print("\n[一致性问题]")
        for it in cr["issues"]:
            print(f"  - [{it.get('severity','')}] {it.get('category','')}: {it.get('explanation','')}")
    print("\n[正文预览前 1500 字]\n" + result["text"][:1500] + ("\n…(省略)" if len(result["text"]) > 1500 else ""))


def cmd_check(args, db, ai):
    if len(args) < 2:
        print("用法: check <idx> <text-file-path>")
        return
    idx = int(args[0])
    p = Path(args[1])
    if not p.exists():
        print(f"[err] 文件不存在：{p}")
        return
    text = p.read_text(encoding="utf-8")
    print(f"[hard-check] 正在对第 {idx} 章做程序化校验…")
    issues = consistency.hard_check(db, idx, text)
    if not issues:
        print("[ok] 未发现硬规则问题。")
        return
    for it in issues:
        print(f"  - [{it.get('severity','')}] {it.get('category','')}: {it.get('explanation','')}")
        if it.get("fix_suggestion"):
            print(f"      建议: {it['fix_suggestion']}")


def cmd_hard_check(args, db, ai):
    if not args:
        print("用法: hard-check <idx>")
        return
    idx = int(args[0])
    ch = kb.get_chapter_by_idx(db, idx)
    if not ch or not (ch.get("final_text") or ch.get("draft")):
        print("[err] 章节无正文")
        return
    text = ch.get("final_text") or ch.get("draft")
    issues = consistency.hard_check(db, idx, text)
    if not issues:
        print("[ok] 未发现硬规则问题。")
        return
    for it in issues:
        print(f"  - [{it.get('severity','')}] {it.get('category','')}: {it.get('explanation','')}")
        if it.get("fix_suggestion"):
            print(f"      建议: {it['fix_suggestion']}")


# ============================================================
# 主循环
# ============================================================

COMMANDS = {
    "init": cmd_init,
    "set-synopsis": lambda a, d, ai: cmd_set(["synopsis"] + a, d, ai),
    "set-style": lambda a, d, ai: cmd_set(["style"] + a, d, ai),
    "set-pov": lambda a, d, ai: cmd_set(["pov"] + a, d, ai),
    "set-unit": lambda a, d, ai: cmd_set(["unit"] + a, d, ai),
    "show-project": cmd_show_project,
    "web": cmd_web,
    "import-md": cmd_import_md,
    "scan-threads": cmd_scan_threads,
    "scan-logic": cmd_scan_logic,
    "scan-style": cmd_scan_style,
    "scan-all": cmd_scan_all,
    "set-mbti": cmd_set_mbti,
    "show-character-matrix": cmd_show_character_matrix,
    "scan-personality": cmd_scan_personality,
    "add-milestone": cmd_add_milestone,
    "show-arc": cmd_show_arc,
    "add-rel-evol": cmd_add_rel_evol,
    "optimize-personality": cmd_optimize_personality,
    "optimize-arc": cmd_optimize_arc,
    "optimize-relationship": cmd_optimize_relationship,
    "optimize-all": cmd_optimize_all,
    "list-suggestions": cmd_list_suggestions,
    "apply-suggestion": cmd_apply_suggestion,
    "dismiss-suggestion": cmd_dismiss_suggestion,
    "extract-events": cmd_extract_events,
    "extract-events-all": cmd_extract_events_all,
    "extract-threads": cmd_extract_threads,
    "extract-threads-all": cmd_extract_threads_all,
    "extract-all": cmd_extract_all,
    "scan-structure": cmd_scan_structure,
    "optimize-structure": cmd_optimize_structure,
    "pipeline-quick": cmd_pipeline_quick,
    "pipeline": cmd_pipeline,
    "pipeline-llm": cmd_pipeline_llm,
    "add-character": cmd_add_character,
    "show-character": cmd_show_character,
    "list-characters": cmd_list_characters,
    "add-world": cmd_add_world,
    "list-world": cmd_list_world,
    "add-fact": cmd_add_fact,
    "list-facts": cmd_list_facts,
    "add-relationship": cmd_add_relationship,
    "add-thread": cmd_add_thread,
    "add-chapter": cmd_add_chapter,
    "list-chapters": cmd_list_chapters,
    "show-chapter": cmd_show_chapter,
    "show-events": cmd_show_events,
    "timeline": cmd_timeline,
    "generate-outline": cmd_generate_outline,
    "show-context": cmd_show_context,
    "write-raw": cmd_write_raw,
    "write-chapter": cmd_write_chapter,
    "check": cmd_check,
    "hard-check": cmd_hard_check,
}


def run():
    print(BANNER)
    print(f"  DB: {CONFIG.db_path}")
    print(f"  AI: provider={CONFIG.ai.provider} model={CONFIG.ai.model} ready={'yes' if AIClient().ready else 'no'}")
    print("  输入 help 查看命令；quit 退出。\n")

    db = Database(CONFIG.db_path)
    ai = AIClient()
    cmd_init([], db, ai)

    while True:
        try:
            line = input("novel> ").strip()
        except (EOFError, KeyboardInterrupt):
            print("\nbye.")
            break
        if not line:
            continue
        if line in ("quit", "exit", ":q"):
            print("bye.")
            break
        if line in ("help", "?"):
            print(HELP)
            continue
        try:
            parts = shlex.split(line)
        except ValueError as e:
            print(f"[err] 无法解析命令: {e}")
            continue
        op, rest = parts[0], parts[1:]
        fn = COMMANDS.get(op)
        if not fn:
            print(f"[err] 未知命令: {op}。输入 help。")
            continue
        try:
            fn(rest, db, ai)
        except Exception as e:
            import traceback
            traceback.print_exc()
            print(f"[err] {type(e).__name__}: {e}")


if __name__ == "__main__":
    run()
