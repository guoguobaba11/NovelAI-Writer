"""
novelai.scanner.logic
全本逻辑链扫描器。覆盖：
1. 死人复活（人物 status=死 但后续章节仍有 final_text/draft 中出现名字 + 动词）
2. 同一时间人在两处（基于事件表 story_time）
3. 因果倒置（结果事件先于原因事件）
4. 信息边界泄漏（POV 角色不知道的事实在他章节中作为"已知"使用）—— 委托给 consistency 模块的硬规则
5. 事件链断裂（两个相邻事件之间缺关键因果说明）—— 基于章节内事件共现
"""
from __future__ import annotations
from typing import Any
from ..db import Database
from .. import knowledge as kb
from .. import consistency as cons_mod


def scan_logic(db: Database) -> dict:
    """
    返回分组：
      {
        "dead_appears": [...],
        "location_clash": [...],
        "causality_reversed": [...],
        "info_leak": [...],            # 委托给 hard_check (逐 POV)
        "chain_break": [...],
        "summary": { "total": N, "by_severity": {...} }
      }
    """
    chapters = kb.list_chapters(db)
    events = kb.list_events(db)
    characters = kb.list_characters(db)
    issues: dict[str, list[dict]] = {
        "dead_appears": [],
        "location_clash": [],
        "causality_reversed": [],
        "info_leak": [],
        "chain_break": [],
    }

    # 1) 死人复活：status 含死亡关键词的角色，在后续章节正文里以 主语+动词 出现
    dead_chars = [c for c in characters if kb.is_dead_status(c.get("status"))]
    ch_by_id = {c["id"]: c for c in chapters}
    for c in dead_chars:
        first_death_ch = _first_status_change_chapter(db, c)
        if not first_death_ch:
            continue
        for ch in chapters:
            if ch["idx"] <= first_death_ch["idx"]:
                continue
            text = ch.get("final_text") or ch.get("draft") or ""
            if not text:
                continue
            # 简易：检查"X 走/坐/说/看/笑/叹"等动词搭配
            if _appears_alive(c["name"], text):
                issues["dead_appears"].append({
                    "character_id": c["id"],
                    "character_name": c["name"],
                    "dead_chapter_idx": first_death_ch["idx"],
                    "appears_chapter_idx": ch["idx"],
                    "severity": "high",
                    "context": f"角色「{c['name']}」在第{first_death_ch['idx']}章已确认死亡，但第{ch['idx']}章正文中仍以活人姿态出现。",
                    "fix_suggestion": "删除/改为鬼魂/记忆/他人转述等合理处理。",
                })

    # 2) 同一时间人在两处：同一 character 同时出现在两个事件的 location 不同
    # 简化：story_time 相同或差 ≤0.1，且参与了两个 location 不同的事件
    # B-新44: char_by_id 缓存, 避免循环里重复查 db
    char_by_id = {c["id"]: c for c in characters}
    by_char: dict[int, list[dict]] = {}
    for e in events:
        for pid in (e.get("participants") or []):
            try:
                by_char.setdefault(int(pid), []).append(e)
            except (ValueError, TypeError):
                pass
    for pid, evs in by_char.items():
        evs_sorted = sorted(evs, key=lambda x: (x.get("story_time") or 0))
        # 检查所有事件对（不仅是相邻的），但限制窗口避免 O(n²) 爆炸
        for i in range(len(evs_sorted)):
            a = evs_sorted[i]
            if not a.get("location"):
                continue
            for j in range(i + 1, min(i + 6, len(evs_sorted))):
                b = evs_sorted[j]
                if not b.get("location") or a["location"] == b["location"]:
                    continue
                dt = (b.get("story_time") or 0) - (a.get("story_time") or 0)
                if dt < 1.0:  # 同一日内跨地点
                    ch_a = ch_by_id.get(a["chapter_id"])
                    ch_b = ch_by_id.get(b["chapter_id"])
                    if ch_a and ch_b and ch_a["idx"] == ch_b["idx"]:
                        char = char_by_id.get(pid)
                        if char:
                            issues["location_clash"].append({
                                "character_id": pid,
                                "character_name": char["name"],
                                "chapter_idx": ch_a["idx"],
                                "severity": "medium",
                                "context": (
                                    f"第{ch_a['idx']}章内「{char['name']}」在事件「{a['title']}」（{a['location']}）和「{b['title']}」（{b['location']}）之间相距 {dt} 个时间单位，"
                                    f"但两个地点不同。是否需要交代移动？"
                                ),
                                "fix_suggestion": "添加移动/过渡描写，或调整时间。",
                            })
                            break  # 同一角色本章已报警一次即可

    # 3) 因果倒置：事件 b 在 a 之后发生，但 a 标记为 b 的 cause
    event_by_id = {e["id"]: e for e in events}
    for e in events:
        for cid in (e.get("cause_event_ids") or []):
            cause = event_by_id.get(cid)
            if not cause:
                continue
            t_c = cause.get("story_time") or 0
            t_e = e.get("story_time") or 0
            if t_c > t_e:
                ch_e = ch_by_id.get(e["chapter_id"])
                ch_c = ch_by_id.get(cause["chapter_id"])
                issues["causality_reversed"].append({
                    "event_id": e["id"],
                    "event_title": e["title"],
                    "cause_event_id": cause["id"],
                    "cause_title": cause["title"],
                    "severity": "high",
                    "context": (
                        f"事件「{e['title']}」（第{ch_e['idx'] if ch_e else '?'}章 @{t_e}）"
                        f"被标记为「{cause['title']}」（第{ch_c['idx'] if ch_c else '?'}章 @{t_c}）的原因，"
                        f"但时间上前者更早。果不应先于因。"
                    ),
                    "fix_suggestion": "删除该 cause 关系，或调整事件时间。",
                })

    # 4) 信息边界：按章节 POV 跑硬规则
    for ch in chapters:
        if not ch.get("pov_character_id"):
            continue
        text = ch.get("final_text") or ch.get("draft") or ""
        if not text:
            continue
        for it in cons_mod.hard_check(db, ch["idx"], text):
            if it.get("category") == "info_leak":
                issues["info_leak"].append({
                    "chapter_idx": ch["idx"],
                    "pov_character_id": ch["pov_character_id"],
                    "severity": it.get("severity", "high"),
                    "context": it.get("explanation", ""),
                    "fix_suggestion": it.get("fix_suggestion", ""),
                })

    # 5) 事件链断裂：启发式检测相邻章节事件是否有关联
    # 策略：比较前章末事件的关键词是否出现在本章首事件的摘要中
    chapter_idx_to_events: dict[int, list[dict]] = {}
    for e in events:
        ch = ch_by_id.get(e["chapter_id"])
        if ch:
            chapter_idx_to_events.setdefault(ch["idx"], []).append(e)
    sorted_idxs = sorted(chapter_idx_to_events.keys())
    for i in range(1, len(sorted_idxs)):
        cur = sorted_idxs[i]
        prev = sorted_idxs[i - 1]
        if cur - prev > 1:
            continue  # 跳章不报
        cur_evs = chapter_idx_to_events[cur]
        prev_evs = chapter_idx_to_events[prev]
        if not prev_evs or not cur_evs:
            continue
        # 取前章末事件和本章首事件
        prev_last = prev_evs[-1]
        cur_first = cur_evs[0]
        # 关键词重叠检测
        prev_kws = set(re.findall(r'[\u4e00-\u9fa5]{2,4}', prev_last.get("summary") or ""))
        cur_kws = set(re.findall(r'[\u4e00-\u9fa5]{2,4}', cur_first.get("summary") or ""))
        overlap = prev_kws & cur_kws
        # 共享参与者检测
        prev_ps = set(prev_last.get("participants") or [])
        cur_ps = set(cur_first.get("participants") or [])
        shared = prev_ps & cur_ps
        # 既无关键词重叠也无共享参与者 → 事件链可能断
        if not overlap and not shared:
            issues["chain_break"].append({
                "from_chapter_idx": prev,
                "to_chapter_idx": cur,
                "severity": "low",
                "context": (
                    f"第{prev}章末事件「{prev_last.get('title') or ''}」和"
                    f"第{cur}章首事件「{cur_first.get('title') or ''}」之间缺少明显关联。"
                ),
                "fix_suggestion": (
                    f"在第{cur}章开头点明与前章的关联，或补充过渡描写。"
                ),
            })

    # 汇总
    total = sum(len(v) for v in issues.values())
    by_sev = {"high": 0, "medium": 0, "low": 0}
    for v in issues.values():
        for it in v:
            sev = it.get("severity", "low")
            by_sev[sev] = by_sev.get(sev, 0) + 1
    return {
        **issues,
        "summary": {"total": total, "by_severity": by_sev},
    }


def _first_status_change_chapter(db: Database, character: dict) -> dict | None:
    """从一致性报告或事件中找角色第一次出现 status 变化的章节。
    简化：从 fact 表查带 established_chapter_id 的事实，匹配角色名。
    """
    facts = kb.list_facts(db)
    name = character["name"]
    # B-新49: 用 re.findall 整词匹配, 避免 1-2 字名 "王" 撞 "王国"
    for f in facts:
        content = f.get("content", "")
        if not name or not content:
            continue
        # 用标点/空白/中文字符边界包夹匹配
        if not re.search(r"(?:^|[\s，。！？；：「」（）、])" + re.escape(name) + r"(?:$|[\s，。！？；：「」（）、])", content):
            continue
        if not any(k in content for k in kb.DEAD_KEYWORDS):
            continue
        ch_id = f.get("established_chapter_id")
        if ch_id:
            ch = kb.get_chapter(db, ch_id)
            if ch:
                return ch
    return None


# 简易：判断人名在正文中"以活人姿态出现"
import re
_ALIVE_VERBS = (
    "说", "道", "答", "问", "喊", "叫", "笑", "叹", "哭", "怒", "喝", "低声", "高声",
    "走", "跑", "坐", "站", "立", "入", "出", "来", "去", "回", "至", "至",
    "看", "望", "听", "视", "顾", "盯",
    "握", "持", "拔", "举", "挥",
    "思", "想", "念", "忆", "心",
    "是", "在", "为", "如",
)


# 闪回/梦境/幻觉/灵异 叙事标记 — 如果附近出现这些词，死人出现是合理的
_FLASHBACK_HINTS = (
    "梦", "回忆", "想起", "记起", "幻觉", "幻象", "鬼", "魂", "幽灵",
    "尸体", "尸首", "遗体", "闪回", "回想", "追忆", "往事", "从前",
    "曾经", "当年", "那时候", "转世", "灵魂", "亡灵", "在天之灵",
    "托梦", "入梦", "幻想", "仿佛看到", "眼前浮现",
)

def _appears_alive(name: str, text: str, context_window: int = 80) -> bool:
    """检查「name + 动词」短距离共现，排除闪回/梦境等合理场景"""
    if not name or not text:
        return False
    # B-新50: 整词匹配, 避免 1-2 字名 ("王") 撞 "王国"
    pattern = re.compile(r"(?:^|[\s，。！？；：「」（）、])" + re.escape(name) + r"(?:$|[\s，。！？；：「」（）、])")
    for m in pattern.finditer(text):
        p = m.start()
        window = text[max(0, p - 6): p + len(name) + 6]
        # 检查是否有活人动词
        has_alive_verb = False
        for v in _ALIVE_VERBS:
            if v in window:
                has_alive_verb = True
                break
        if not has_alive_verb:
            continue
        # 检查上下文是否含闪回/梦境标记
        ctx_start = max(0, p - context_window)
        ctx = text[ctx_start: p + len(name) + context_window]
        is_legit = any(h in ctx for h in _FLASHBACK_HINTS)
        if is_legit:
            continue  # 合理叙事，不报警
        return True
    return False
