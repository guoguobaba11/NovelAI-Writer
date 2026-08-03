"""
novelai.pipeline
修改流水线：把 4 套工具串联 + LLM 优化 + 路线图。

3 个阶段：
  Stage 1: 快速诊断（秒级，纯启发式）—— 4 个扫描 + 结构分析
  Stage 2: 智能合并（秒级）—— 跨扫描器去重 + 按"修改优先级"排序
  Stage 3: LLM 优化（5-15 分钟）—— 5 类优化（结构/全局/性格/弧光/交会）

输出：
  PipelineReport = {
    health_snapshot,    # 健康度
    issues_by_category, # 各扫描器问题
    roadmap,            # 合并去重排序后的修改路线图
    llm_suggestions,    # LLM 建议（仅 full 跑）
    summary,            # 汇总
  }
"""
from __future__ import annotations
import time
from typing import Any, Callable
from .db import Database
from . import knowledge as kb
from . import scanner, structure, personality, optimizer
from .ai_client import AIClient


# ============================================================
# 阶段 1: 快速诊断
# ============================================================

def run_quick_pipeline(db: Database) -> dict:
    """5 个扫描器 + 结构分析。秒级。"""
    t0 = time.time()
    # 健康度
    proj = kb.get_or_create_project(db)
    chapters = kb.list_chapters(db)
    events = kb.list_events(db)
    threads = kb.list_threads(db)
    characters = kb.list_characters(db)
    total_words = sum((c.get("word_count") or 0) for c in chapters)

    # 4 个扫描器 (B-新61: 各包 try/except, 任何一个挂不影响其他; 返 error 项供前端展示)
    scan_errors: list[str] = []
    try:
        thread_issues = scanner.scan_threads(db)
    except Exception as e:
        scan_errors.append(f"threads: {e}")
        thread_issues = []
    try:
        logic_result = scanner.scan_logic(db)
    except Exception as e:
        scan_errors.append(f"logic: {e}")
        logic_result = {"summary": {"total": 0, "by_severity": {}}, "dead_appears": [], "location_clash": [], "causality_reversed": [], "info_leak": [], "chain_break": []}
    try:
        style_result = scanner.scan_style(db, baseline_first_n=3, z_threshold=2.0)
    except Exception as e:
        scan_errors.append(f"style: {e}")
        style_result = {"per_chapter": [], "drift_issues": [], "overall_drift_curve": [], "baseline_range": [1, 3]}
    chars_with_mbti = [c for c in characters if c.get("mbti")]
    try:
        drift_results = personality.scan_personality_drift(db, chars_with_mbti)
        drift_signals = sum(1 for r in drift_results if r.get("drift_signals"))
    except Exception as e:
        scan_errors.append(f"personality: {e}")
        drift_results = []
        drift_signals = 0

    # 结构分析
    struct_ana = structure.StructureAnalyzer(db)
    try:
        struct_full = struct_ana.analyze_full()
    except Exception as e:
        scan_errors.append(f"structure.full: {e}")
        struct_full = {"error": str(e), "issues": []}
    try:
        struct_summary = struct_ana.full_issues_summary()
    except Exception as e:
        scan_errors.append(f"structure.summary: {e}")
        struct_summary = {"total": 0, "by_severity": {}}

    elapsed = time.time() - t0

    return {
        "elapsed_seconds": round(elapsed, 2),
        "scan_errors": scan_errors,  # B-新61: 暴露给前端"部分扫描失败"提示
        "health": {
            "n_chapters": len(chapters),
            "n_events": len(events),
            "n_threads": len(threads),
            "n_characters": len(characters),
            "n_characters_with_mbti": len(chars_with_mbti),
            "total_words": total_words,
        },
        "issues_by_category": {
            "thread": {
                "count": len(thread_issues),
                "high": sum(1 for it in thread_issues if it.get("severity") == "high"),
                "items": thread_issues,
            },
            "logic": {
                "count": logic_result.get("summary", {}).get("total", 0) if isinstance(logic_result.get("summary"), dict) else 0,
                "high": logic_result.get("summary", {}).get("by_severity", {}).get("high", 0) if isinstance(logic_result.get("summary"), dict) else 0,
                "breakdown": {
                    "dead_appears": len(logic_result.get("dead_appears", [])),
                    "location_clash": len(logic_result.get("location_clash", [])),
                    "causality_reversed": len(logic_result.get("causality_reversed", [])),
                    "info_leak": len(logic_result.get("info_leak", [])),
                    "chain_break": len(logic_result.get("chain_break", [])),
                },
                "items": _flatten_logic(logic_result),
            },
            "style": {
                "count": len(style_result.get("drift_issues", [])),
                "high": sum(1 for it in style_result.get("drift_issues", []) if it.get("severity") == "high"),
                "items": style_result.get("drift_issues", []),
            },
            "personality": {
                "count": drift_signals,
                "items": [r for r in drift_results if r.get("drift_signals")],
            },
            "structure": {
                "count": len(struct_full.get("issues", [])) if not struct_full.get("error") else 0,
                "high": sum(1 for it in struct_full.get("issues", []) if it.get("severity") == "high") if not struct_full.get("error") else 0,
                "items": struct_full.get("issues", []) if not struct_full.get("error") else [],
            },
        },
        "structure_data": struct_full if not struct_full.get("error") else None,
        "structure_summary": struct_summary,
    }


def _flatten_logic(logic_result: dict) -> list[dict]:
    items = []
    for key, label in [
        ("dead_appears", "💀 死人复活"),
        ("location_clash", "📍 地点冲突"),
        ("causality_reversed", "⏪ 因果倒置"),
        ("info_leak", "🕳 信息泄漏"),
        ("chain_break", "🔌 事件链断裂"),
    ]:
        for it in logic_result.get(key, []):
            it2 = dict(it)
            it2["sub_type"] = key
            it2["category"] = label
            items.append(it2)
    return items


# ============================================================
# 阶段 2: 智能合并 / 路线图
# ============================================================

# 问题严重度权重
SEVERITY_WEIGHT = {"high": 100, "medium": 30, "low": 5}

# 类型权重（修改时优先处理哪些）
CATEGORY_WEIGHT = {
    "thread_overdue": 90,      # 伏笔超期（影响核心）
    "causality_reversed": 95,  # 因果倒置（影响叙事逻辑）
    "dead_appears": 100,       # 死人复活（硬错误）
    "info_leak": 80,            # 信息泄漏（限知视角破坏）
    "location_clash": 50,        # 地点冲突
    "chain_break": 40,           # 链断裂
    "few_turning_points": 70,    # 转折点缺失
    "intensity_sink": 50,        # 塌陷
    "volume_disconnect": 80,     # 卷间断档
    "front_heavy": 40,           # 前重后轻
    "foreshadowing_imbalance": 60, # 伏笔失衡
    "climax_too_early": 80,      # 高潮过早
    "no_climax": 90,              # 无高潮
    "style_drift": 30,            # 文风漂移
    "personality_drift": 50,      # 性格漂移
    "chapter_too_dense": 20,
    "chapter_too_short": 15,
    "very_long": 15,
    "very_short": 10,
    "no_events": 20,
    "no_turning": 25,
    "low_intensity": 10,
    "no_resolution": 30,
    "no_thread": 5,
    "other": 30,
}


def _classify_issue(category: str, sub_type: str | None, issue_type: str) -> str:
    """把 issue 归到一个'修改类型'，用于去重和排序。"""
    cat = category
    sub = sub_type or ""
    typ = issue_type or ""
    if cat == "thread":
        if "overdue" in typ: return "thread_overdue"
        if "no_payoff" in typ: return "thread_overdue"
        if "premature" in typ: return "thread_premature"
        if "causality" in typ: return "thread_causality"
        return "thread_other"
    if cat == "logic":
        if sub == "dead_appears": return "dead_appears"
        if sub == "location_clash": return "location_clash"
        if sub == "causality_reversed": return "causality_reversed"
        if sub == "info_leak": return "info_leak"
        if sub == "chain_break": return "chain_break"
    if cat == "style":
        return "style_drift"
    if cat == "personality":
        return "personality_drift"
    if cat == "structure":
        # 优先精确匹配（防止子串遮蔽）
        if "no_climax" in typ: return "no_climax"
        if "climax" in typ: return "climax_too_early"
        if "foreshadowing_late_payoff" in typ: return "foreshadowing_late_payoff"
        if "foreshadowing_imbalance" in typ: return "foreshadowing_imbalance"
        if "foreshadowing" in typ: return "foreshadowing_imbalance"
        if "few_turning" in typ: return "few_turning_points"
        if "intensity_sink" in typ: return "intensity_sink"
        if "volume_disconnect" in typ: return "volume_disconnect"
        if "front_heavy" in typ: return "front_heavy"
        if "no_resolution" in typ: return "no_resolution"
        # 章卷级结构问题（补全 analyser 产出的所有类型）
        if "too_dense" in typ: return "chapter_too_dense"
        if "very_long" in typ: return "very_long"
        if "very_short" in typ: return "very_short"
        if "no_turning" in typ: return "no_turning"
        if "low_intensity" in typ: return "low_intensity"
        if "no_events" in typ: return "no_events"
        if "setup_too_heavy" in typ: return "setup_too_heavy"
        if "volume_long" in typ: return "volume_long"
        if "volume_short" in typ: return "volume_short"
        if typ.startswith("chapter_"): return typ
    return "other"


def _extract_chapter_ref(category: str, issue: dict) -> tuple[int, int]:
    """从 issue 中提取 (chapter_idx, importance) 引用。返回 (0, 0) 表示无。"""
    if category == "thread":
        t = issue.get("title", "")
        # 从 context 提取
        ctx = issue.get("context", "")
        # 简化：所有 thread 算全局
        return (0, 5)
    if category == "logic":
        for key in ("chapter_idx", "from_chapter_idx", "to_chapter_idx"):
            if key in issue:
                return (issue[key], 1)
        return (0, 1)
    if category == "style":
        return (issue.get("chapter_idx", 0), 1)
    if category == "personality":
        return (issue.get("chapter_idx", 0), 1)
    if category == "structure":
        ctx = issue.get("context", "")
        # 尝试从 "第N章" 提取
        import re
        m = re.search(r"第(\d+)章", ctx)
        if m:
            return (int(m.group(1)), 1)
        return (0, 1)
    return (0, 1)


def build_roadmap(quick_report: dict, llm_suggestions: list[dict] | None = None) -> list[dict]:
    """
    合并去重所有问题 + LLM 建议，按"修改优先级"排序。
    返回 [{rank, type, severity, score, title, source, chapter_ref, context, fix_suggestion, llm_suggestion_id?}, ...]
    """
    items = []
    issues_by_cat = quick_report.get("issues_by_category", {})

    # 获取总章节数用于动态早期章计算
    total_chapters = quick_report.get("health", {}).get("n_chapters", 1)
    early_boundary = max(3, total_chapters // 3)

    # 扫描器问题
    for cat_key, cat_label in [
        ("thread", "🧵 伏笔"),
        ("logic", "🔗 逻辑链"),
        ("style", "📜 文风"),
        ("personality", "🎭 性格"),
        ("structure", "📊 结构"),
    ]:
        info = issues_by_cat.get(cat_key, {})
        for it in info.get("items", []):
            sev = it.get("severity", "low")
            typ = it.get("type", "")
            sub = it.get("sub_type")
            classification = _classify_issue(cat_key, sub, typ)
            chap_ref, weight_mult = _extract_chapter_ref(cat_key, it)
            sev_w = SEVERITY_WEIGHT.get(sev, 5)
            cat_w = CATEGORY_WEIGHT.get(classification, 30)
            score = sev_w + cat_w * weight_mult
            # 早期章（前 1/3）优先级 +50（修改早期影响更大）
            if chap_ref and chap_ref > 0 and chap_ref <= early_boundary:
                score += 50
            items.append({
                "rank": 0,
                "type": classification,
                "category": cat_label,
                "severity": sev,
                "score": score,
                "title": it.get("title") or f"{cat_label} {typ}".strip(),
                "source": f"{cat_label} 扫描",
                "chapter_ref": chap_ref,
                "context": it.get("context", "") or it.get("explanation", ""),
                "fix_suggestion": it.get("fix_suggestion", ""),
            })

    # LLM 建议
    if llm_suggestions:
        for s in llm_suggestions:
            sev = s.get("priority", "medium")
            sev_w = SEVERITY_WEIGHT.get(sev, 5)
            cat_w = 40  # LLM 建议的"修改类型"权重统一
            items.append({
                "rank": 0,
                "type": "llm_suggestion",
                "category": "💡 LLM 优化",
                "severity": sev,
                "score": sev_w + cat_w,
                "title": s.get("title", ""),
                "source": f"LLM {s.get('target_label', '优化')}",
                "chapter_ref": 0,
                "context": s.get("content", ""),
                "fix_suggestion": s.get("content", ""),
                "llm_id": s.get("id"),
                "evidence": s.get("evidence", ""),
                "chapter_focus": s.get("chapter_focus", ""),
            })

    # 简单去重：相同 type + chapter_ref + 类似 context 视为重复
    seen = set()
    deduped = []
    for it in sorted(items, key=lambda x: -x["score"]):
        # 去重键：type + 章节 + 上下文前 30 字
        key = (it["type"], it["chapter_ref"], it["context"][:30])
        if key in seen:
            continue
        seen.add(key)
        deduped.append(it)
    # 排序 + 排名
    deduped.sort(key=lambda x: (-x["score"], x["chapter_ref"] or 999))
    for i, it in enumerate(deduped, 1):
        it["rank"] = i
    return deduped


# ============================================================
# 阶段 3: LLM 优化
# ============================================================

def run_full_pipeline(
    db: Database,
    ai: AIClient,
    progress_cb: Callable[[str, str], None] | None = None,
) -> dict:
    """完整流水线：阶段 1 + 阶段 2 + 阶段 3。"""
    cb = progress_cb or (lambda s, m: None)
    cb("stage1", "开始快速诊断（5 个扫描器）…")
    quick = run_quick_pipeline(db)
    cb("stage1", f"✓ 阶段 1 完成：{sum(v.get('count', 0) for v in quick.get('issues_by_category', {}).values())} 个问题")

    llm_suggestions = []
    if ai.ready:
        cb("stage3", "开始 LLM 优化（5 类，预计 5-15 分钟）…")
        opt = optimizer.Optimizer(db, ai)
        # 1. 全局优化
        cb("stage3", "[1/5] 全局优化…")
        try:
            sugs = opt.optimize_all()
            for s in sugs: s["target_label"] = "全局"
            llm_suggestions.extend(sugs)
            cb("stage3", f"  → {len(sugs)} 条")
        except Exception as e:
            cb("stage3", f"  → 失败: {e}")
        # 2. 全篇结构
        cb("stage3", "[2/5] 全篇结构优化…")
        try:
            sugs = opt.optimize_structure("full")
            for s in sugs: s["target_label"] = "全篇结构"
            llm_suggestions.extend(sugs)
            cb("stage3", f"  → {len(sugs)} 条")
        except Exception as e:
            cb("stage3", f"  → 失败: {e}")
        # 3. 主要人物 × 性格（top 5 MBTI 人物）
        try:
            chars = [c for c in kb.list_characters(db) if c.get("mbti") and c.get("role") in ("protagonist", "antagonist", "supporting")]
        except Exception as e:
            cb("stage3", f"  → 列人物失败, 跳 3/5 4/5: {e}")
            chars = []
        for i, ch in enumerate(chars[:5]):
            cb("stage3", f"[3/5 性格 {i+1}/{min(5, len(chars))}] {ch['name']}…")
            try:
                sugs = opt.optimize_personality(ch["name"])
                for s in sugs: s["target_label"] = f"性格: {ch['name']}"
                llm_suggestions.extend(sugs)
                cb("stage3", f"  → {len(sugs)} 条")
            except Exception as e:
                cb("stage3", f"  → 失败: {e}")
        # 4. 主要人物 × 成长线（同 top 5）
        for i, ch in enumerate(chars[:5]):
            cb("stage3", f"[4/5 弧光 {i+1}/{min(5, len(chars))}] {ch['name']}…")
            try:
                sugs = opt.optimize_arc(ch["name"])
                for s in sugs: s["target_label"] = f"成长线: {ch['name']}"
                llm_suggestions.extend(sugs)
                cb("stage3", f"  → {len(sugs)} 条")
            except Exception as e:
                cb("stage3", f"  → 失败: {e}")
        # 5. 关键关系对（top 3 关系 × 性格兼容性 + 演变）
        try:
            rels = kb.list_relationships(db)
        except Exception as e:
            cb("stage3", f"  → 列关系失败, 跳 5/5: {e}")
            rels = []
        # 排序：先处理 protagonist × antagonist
        def _rel_score(r):
            a_id, b_id = r["char_a_id"], r["char_b_id"]
            ca = kb.get_character(db, a_id)
            cb_ = kb.get_character(db, b_id)
            if not ca or not cb_: return 0
            score = 0
            if {ca.get("role"), cb_.get("role")} == {"protagonist", "antagonist"}: score += 100
            if ca.get("mbti") and cb_.get("mbti"): score += 30
            return score
        rels_sorted = sorted(rels, key=_rel_score, reverse=True)[:3]
        for i, r in enumerate(rels_sorted):
            a = kb.get_character(db, r["char_a_id"])
            b = kb.get_character(db, r["char_b_id"])
            if not a or not b: continue
            cb("stage3", f"[5/5 关系 {i+1}/{len(rels_sorted)}] {a['name']}↔{b['name']}…")
            try:
                sugs = opt.optimize_relationship(a["name"], b["name"])
                for s in sugs: s["target_label"] = f"关系: {a['name']}↔{b['name']}"
                llm_suggestions.extend(sugs)
                cb("stage3", f"  → {len(sugs)} 条")
            except Exception as e:
                cb("stage3", f"  → 失败: {e}")
        cb("stage3", f"✓ 阶段 3 完成：{len(llm_suggestions)} 条 LLM 建议")
    else:
        cb("stage3", "✗ AI 未配置，跳过 LLM 阶段")

    # 阶段 2: 路线图
    cb("stage2", "正在合并去重并生成修改路线图…")
    roadmap = build_roadmap(quick, llm_suggestions)
    cb("stage2", f"✓ 阶段 2 完成：路线图 {len(roadmap)} 项")

    # 汇总
    total_issues = sum(v["count"] for v in quick["issues_by_category"].values())
    high_issues = sum(v.get("high", 0) for v in quick["issues_by_category"].values())
    return {
        "quick": quick,
        "llm_suggestions_count": len(llm_suggestions),
        "roadmap": roadmap,
        "summary": {
            "total_scanner_issues": total_issues,
            "high_issues": high_issues,
            "llm_suggestions": len(llm_suggestions),
            "roadmap_items": len(roadmap),
            "elapsed_total_seconds": quick.get("elapsed_seconds", 0),
        },
    }
