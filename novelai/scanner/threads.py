"""
novelai.scanner.threads
伏笔扫描器：扫"埋了没解"、"未埋先解"、"超期未兑现"。
"""
from __future__ import annotations
from typing import Any
from ..db import Database
from .. import knowledge as kb


def scan_threads(db: Database) -> list[dict]:
    issues: list[dict] = []
    threads = kb.list_threads(db)
    chapters = kb.list_chapters(db)
    if not threads:
        return issues
    ch_by_id = {c["id"]: c for c in chapters}
    total = max(len(chapters), 1)  # B-新51: 防御 0 章时除零 (旧代码 `or 1` 已在 0 时返 1, 但 `max` 更明确)
    # BUG 修复：旧代码把 planted_pos=ch["idx"]（原始序号，可能跳号如1,10,18,32）
    # 与 total=len(chapters)（章数，如6）混算 → total-planted_pos 永远为负 → overdue 永远漏报。
    # 改为按 idx 排序后的"位置排名"(1-based)，与 total 同量纲。
    sorted_idx = sorted(c["idx"] for c in chapters)
    idx_to_rank = {idx: rank for rank, idx in enumerate(sorted_idx, 1)}
    for t in threads:
        status = t.get("status", "planted")
        planted_id = t.get("planted_chapter_id")
        payoff_id = t.get("payoff_chapter_id")
        resolved_id = t.get("resolved_chapter_id")
        planted_pos = _chapter_pos(planted_id, ch_by_id, idx_to_rank)
        payoff_pos = _chapter_pos(payoff_id, ch_by_id, idx_to_rank)
        resolved_pos = _chapter_pos(resolved_id, ch_by_id, idx_to_rank)

        # 1) 埋了没解
        if status in ("planted", "developing") and not resolved_id:
            if planted_pos is not None:
                desc = t.get("description") or ""
                title = t.get("title") or ""
                combined = title + desc
                # 主线/核心伏笔宽容处理：全书最后 15% 才报警
                is_main = any(kw in combined for kw in ("主线", "核心", "主要", "主剧情", "全书主线"))
                overdue_threshold = max(2, total // 3)
                if is_main:
                    # 主线伏笔宽容处理：全书最后 15% 才报警。
                    # 允许晾 int(total*0.85) 章；超过则判 overdue。
                    # 注意 threshold 不能再减 planted_pos——否则条件恒真。
                    overdue_threshold = max(2, int(total * 0.85))
                overdue = (total - planted_pos) > overdue_threshold
                issues.append({
                    "thread_id": t["id"],
                    "title": t["title"],
                    "issue_type": "no_payoff" if not overdue else "overdue",
                    "severity": "medium" if (is_main and overdue) else ("high" if overdue else "medium"),
                    "context": (
                        f"伏笔在第{planted_pos}章埋设，但全书 {total} 章，"
                        f"至今未揭晓（状态：{status}）。"
                        + ("（主线伏笔，允许晚揭晓）" if is_main else "")
                    ),
                    "fix_suggestion": (
                        "要么在后续章节揭晓，要么明确标记为 abandoned 并在文中给出说明。"
                    ),
                })
        # 2) 未埋先解
        if resolved_id and (not planted_id or planted_pos is None):
            issues.append({
                "thread_id": t["id"],
                "title": t["title"],
                "issue_type": "premature_payoff",
                "severity": "high",
                "context": f"第{resolved_pos if resolved_pos else '?'}章标记为已解决，但没有对应的埋设记录。",
                "fix_suggestion": "回溯埋设章节；或在 plot_thread 表中补 planted_chapter_id。",
            })
        # 3) 解决位置 < 埋设位置
        if planted_pos is not None and resolved_pos is not None and resolved_pos < planted_pos:
            issues.append({
                "thread_id": t["id"],
                "title": t["title"],
                "issue_type": "causality_reversed",
                "severity": "high",
                "context": f"伏笔在第{planted_pos}章埋设，但第{resolved_pos}章就标记为已解决（时序倒置）。",
                "fix_suggestion": "检查 plot_thread 里的 planted/resolved 章节号。",
            })
        # 4) abandoned 但描述暗示重要
        if status == "abandoned" and ("重要" in (t.get("description") or "") or "关键" in (t.get("description") or "")):
            issues.append({
                "thread_id": t["id"],
                "title": t["title"],
                "issue_type": "abandoned_important",
                "severity": "medium",
                "context": "伏笔被标为 abandoned，但描述中暗示其重要。",
                "fix_suggestion": "若确实要放弃，把描述改清楚；若反悔，恢复为 developing。",
            })
    return issues


def _chapter_pos(chapter_id, ch_by_id, idx_to_rank=None):
    """返回章节的「位置排名」(1-based，按 idx 排序)。
    idx_to_rank 为 None 时退化为原始 idx（向后兼容）。
    用排名而非原始 idx，因为调用方用 len(chapters) 做分母——两者必须同量纲。"""
    if chapter_id is None:
        return None
    ch = ch_by_id.get(chapter_id)
    if not ch:
        return None
    if idx_to_rank:
        return idx_to_rank.get(ch["idx"])
    return ch["idx"]
