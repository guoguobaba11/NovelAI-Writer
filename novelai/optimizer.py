"""
novelai.optimizer
LLM 优化建议生成器。

4 类建议：
1. personality — 性格优化（基于漂移检测结果）
2. arc — 成长线优化（基于 milestone 进度）
3. relationship — 人物交会优化（基于 MBTI 兼容 + 关系演变）
4. global — 全局优化（综合扫描）

设计原则：
- LLM 之前先做"硬性数据准备"——把全本上下文压缩成结构化 briefing
- LLM 严格按"基于已有数据，不要发明"原则输出
- 输出 JSON 列表，每条带 priority + evidence + chapter_focus
- 全部入库到 optimization_suggestion 表
"""
from __future__ import annotations
import json
from typing import Any
from .db import Database
from . import knowledge as kb
from . import personality, scanner
from .ai_client import AIClient, AICallError


# ============================================================
# 系统提示词
# ============================================================

OPTIMIZER_SYSTEM = """你是一位资深长篇小说修改编辑，专门为已有手稿提供"基于数据的优化建议"。

你的输入是结构化的"项目 briefing"：人物档案、关系、扫描结果、漂移数据。
你的输出必须是严格的 JSON 列表。

铁律：
1. **不发明**：建议必须基于 briefing 中给出的事实。如果 briefing 里没有某人物，不要提他/她。
2. **要具体**：建议要指向"在第 N 章做 X 修改"，不要给空泛的"加强人物塑造"。
3. **要可执行**：每条建议都要让作者能马上动笔——给方向、给修改示例、给替代措辞。
4. **要权衡**：指出修改可能带来的副作用，让作者权衡。
5. **要优先**：priority=high 表示"必修"（影响主线或读者认知）；medium="应改"；low="可润色"。

输出 JSON schema：
{
  "suggestions": [
    {
      "title": "≤ 30 字简短标题",
      "content": "详细建议（200-500 字）",
      "priority": "high|medium|low",
      "evidence": "依据：哪条数据/哪个章节支撑这个建议",
      "chapter_focus": "建议应用的章节范围，如'第 5-8 章' 或 '第 12 章'"
    }
  ]
}
"""


# ============================================================
# 上下文准备（briefing）
# ============================================================

def _briefing_project(db: Database) -> dict:
    p = kb.get_or_create_project(db)
    chapters = kb.list_chapters(db)
    return {
        "title": p.get("title", ""),
        "synopsis": p.get("synopsis", ""),
        "style": p.get("style", ""),
        "pov_mode": p.get("pov_mode", ""),
        "total_chapters": len(chapters),
        "total_volumes": len(kb.list_volumes(db)),
    }


def _briefing_characters(db: Database) -> list[dict]:
    out = []
    for c in kb.list_characters(db):
        out.append({
            "id": c["id"],
            "name": c["name"],
            "role": c.get("role", ""),
            "mbti": c.get("mbti", ""),
            "cognitive_stack": c.get("cognitive_stack", ""),
            "arc_type": c.get("arc_type", ""),
            "arc_progress": c.get("arc_progress") or 0.0,
            "basic_info": c.get("basic_info", ""),
            "personality": c.get("personality", ""),
            "speech_style": c.get("speech_style", ""),
            "status": c.get("status", ""),
        })
    return out


def _briefing_drift(db: Database, char_id: int) -> dict:
    """某角色的漂移数据"""
    chars = [c for c in kb.list_characters(db) if c.get("mbti") and c["id"] == char_id]
    if not chars:
        return {"available": False}
    results = personality.scan_personality_drift(db, chars)
    sig_rows = [r for r in results if r.get("drift_signals")]
    return {
        "available": True,
        "n_chapters_analyzed": len(results),
        "n_drift_signals": len(sig_rows),
        "drift_signals": [
            {
                "chapter_idx": r["chapter_idx"],
                "chapter_title": r.get("chapter_title", ""),
                "signals": r["drift_signals"],
                "baseline_overlap": r.get("baseline_overlap", 0),
                "function_scores": r.get("function_scores", {}),
            }
            for r in sig_rows
        ],
    }


def _briefing_arc(db: Database, char_id: int) -> dict:
    ms = kb.list_milestones(db, character_id=char_id)
    char = kb.get_character(db, char_id)
    ch_by_id = {c["id"]: c for c in kb.list_chapters(db)}
    return {
        "arc_type": char.get("arc_type") if char else "",
        "arc_progress": char.get("arc_progress") if char else 0.0,
        "n_milestones": len(ms),
        "milestones": [
            {
                "chapter_idx": ch_by_id.get(m["chapter_id"], {}).get("idx", "?"),
                "type": m["milestone_type"],
                "dimension": m.get("dimension", ""),
                "description": m["description"],
                "before": m.get("before_state", ""),
                "after": m.get("after_state", ""),
            }
            for m in ms
        ],
    }


def _briefing_relationship(db: Database, a_id: int, b_id: int) -> dict:
    rels = kb.get_relationships_for(db, a_id)
    target = None
    for r in rels:
        if r["char_a_id"] == b_id or r["char_b_id"] == b_id:
            target = r
            break
    char_a = kb.get_character(db, a_id)
    char_b = kb.get_character(db, b_id)
    if not target:
        return {"exists": False, "a": char_a["name"] if char_a else None, "b": char_b["name"] if char_b else None}
    evols = kb.list_rel_evolution(db, relationship_id=target["id"])
    ch_by_id = {c["id"]: c for c in kb.list_chapters(db)}
    mbti_a = char_a.get("mbti", "") if char_a else ""
    mbti_b = char_b.get("mbti", "") if char_b else ""
    compat = personality.compatibility_score(mbti_a, mbti_b) if mbti_a and mbti_b else None
    return {
        "exists": True,
        "rel_type": target.get("rel_type", ""),
        "current_state": target.get("current_state", ""),
        "description": target.get("description", ""),
        "a": char_a["name"] if char_a else None,
        "a_mbti": mbti_a,
        "b": char_b["name"] if char_b else None,
        "b_mbti": mbti_b,
        "compatibility": compat,
        "n_evolutions": len(evols),
        "evolutions": [
            {
                "chapter_idx": ch_by_id.get(e["chapter_id"], {}).get("idx", "?"),
                "intimacy": e.get("intimacy"),
                "trust": e.get("trust"),
                "conflict": e.get("conflict"),
                "dynamics": e.get("dynamics", ""),
            }
            for e in evols
        ],
    }


def _briefing_global_health(db: Database) -> dict:
    """全本健康度速报"""
    logic = scanner.scan_logic(db)
    summary = logic.get("summary", {}) if isinstance(logic.get("summary"), dict) else {}
    return {
        "thread_issues": len(scanner.scan_threads(db)),
        "logic_total": summary.get("total", 0),
        "logic_by_severity": summary.get("by_severity", {}),
        "dead_appears": len(logic.get("dead_appears", [])),
        "location_clash": len(logic.get("location_clash", [])),
        "causality_reversed": len(logic.get("causality_reversed", [])),
        "info_leak": len(logic.get("info_leak", [])),
        "chain_break": len(logic.get("chain_break", [])),
    }


# ============================================================
# 4 类优化器
# ============================================================

class Optimizer:
    def __init__(self, db: Database, ai: AIClient):
        self.db = db
        self.ai = ai

    # ---- 性格优化 ----
    def optimize_personality(self, char_name: str) -> list[dict]:
        c = kb.find_character_by_name(self.db, char_name)
        if not c:
            return []
        if not c.get("mbti"):
            return [{"target_type": "personality", "target_id": str(c["id"]),
                     "target_label": char_name,
                     "title": f"{char_name} 未设置 MBTI",
                     "content": f"请先用 set-mbti {char_name} <MBTI> 给该人物标注 MBTI 类型，再做性格优化。",
                     "priority": "high", "evidence": "性格漂移需要 MBTI baseline", "chapter_focus": "全本"}]

        briefing = {
            "character": c,
            "drift": _briefing_drift(self.db, c["id"]),
            "milestones": _briefing_arc(self.db, c["id"]),
            "nearby_chapters": _briefing_chapter_text(self.db, char_name, c["id"]),
        }
        prompt = (
            f"请为【{char_name}】生成性格层面的修改建议。\n\n"
            f"## 人物档案\n{json.dumps(briefing['character'], ensure_ascii=False, indent=2)}\n\n"
            f"## 漂移检测结果\n{json.dumps(briefing['drift'], ensure_ascii=False, indent=2)}\n\n"
            f"## 已记录的成长线\n{json.dumps(briefing['milestones'], ensure_ascii=False, indent=2)}\n\n"
            f"## 相关章节正文片段（每章取该角色出现段落前后文）\n{json.dumps(briefing['nearby_chapters'], ensure_ascii=False, indent=2)}\n\n"
            f"任务：\n"
            f"1. 找出该角色 MBTI baseline 与实际表现的偏离点\n"
            f"2. 评估当前 baseline_overlap 是否健康（< 0.4 偏弱）\n"
            f"3. 给出 2-4 条具体可执行的修改建议\n"
            f"4. 如发现 grip 压力（劣势功能异常活跃），单独提示"
        )
        suggestions = self._call_and_persist(
            prompt, target_type="personality",
            target_id=str(c["id"]), target_label=char_name,
        )
        return suggestions

    # ---- 弧光优化 ----
    def optimize_arc(self, char_name: str) -> list[dict]:
        c = kb.find_character_by_name(self.db, char_name)
        if not c:
            return []
        if not c.get("arc_type"):
            return [{"target_type": "arc", "target_id": str(c["id"]),
                     "target_label": char_name,
                     "title": f"{char_name} 未设置弧光类型",
                     "content": f"请先用 update-character 或 SQL 给该人物设置 arc_type（positive/negative/flat/circular）。",
                     "priority": "medium", "evidence": "成长线优化需要 arc_type 锚点", "chapter_focus": "全本"}]

        proj = _briefing_project(self.db)
        total_ch = proj["total_chapters"]
        prog = c.get("arc_progress") or 0.0
        briefing = {
            "character": c,
            "arc_data": _briefing_arc(self.db, c["id"]),
            "total_chapters": total_ch,
            "expected_progress": f"{prog*100:.0f}% (基于 milestone 累计)",
        }
        prompt = (
            f"请为【{char_name}】的成长线（人物弧光）生成修改建议。\n\n"
            f"## 项目结构\n全书共 {total_ch} 章\n\n"
            f"## 人物档案\n{json.dumps(c, ensure_ascii=False, indent=2)}\n\n"
            f"## 当前成长线数据\n{json.dumps(briefing['arc_data'], ensure_ascii=False, indent=2)}\n\n"
            f"## 弧光类型参考\n"
            f"- positive：起点消极 → 终点积极（成长）\n"
            f"- negative：起点积极 → 终点消极（堕落）\n"
            f"- flat：起点与终点相同（保持型，稳态角色）\n"
            f"- circular：起点终点相同但人物理解更深（圆环式）\n\n"
            f"任务：\n"
            f"1. 评估当前 arc_progress 进度是否与全书节奏匹配（应在全书 60% 时达到 60%）\n"
            f"2. 检查是否缺关键节点（starting_point / catalyst / crisis / climax / resolution）\n"
            f"3. 如有节点缺失或顺序错乱，给出 2-4 条补全建议\n"
            f"4. 建议要具体到'在第 N 章加入 X 类节点'，不要空泛"
        )
        suggestions = self._call_and_persist(
            prompt, target_type="arc",
            target_id=str(c["id"]), target_label=char_name,
        )
        return suggestions

    # ---- 关系优化 ----
    def optimize_relationship(self, a_name: str, b_name: str) -> list[dict]:
        ca = kb.find_character_by_name(self.db, a_name)
        cb = kb.find_character_by_name(self.db, b_name)
        if not ca or not cb:
            return []
        briefing = _briefing_relationship(self.db, ca["id"], cb["id"])
        prompt = (
            f"请为【{a_name} ↔ {b_name}】的关系演变生成修改建议。\n\n"
            f"## 关系 Briefing\n{json.dumps(briefing, ensure_ascii=False, indent=2)}\n\n"
            f"## 任务\n"
            f"1. 评估当前关系曲线（亲密度/信任/冲突）的健康度\n"
            f"2. 检查 MBTI 兼容性是否被充分利用——是否在剧情中体现两人认知功能的差异/互补？\n"
            f"3. 检查关系演变是否有'足够多'的转折点（每隔 5-10 章应有 1 次关键变化）\n"
            f"4. 给出 2-4 条具体可执行的修改建议\n\n"
            f"## 输出建议方向示例（仅供参考，不要照抄）\n"
            f"- '亲密度持续 +0.5 太平淡，建议在第 N 章加入一次价值观碰撞，亲密度暂时回落到 0.2'\n"
            f"- '信任从 0.7 跌到 -0.3 但关系类型仍是朋友，建议在第 N+2 章显式说明决裂'\n"
            f"- 'ENTJ 和 ISFP 共享 1/4 功能，目前合作很顺；制造戏剧冲突可以让 ISFP 在关键决策上质疑 ENTJ 的方式'"
        )
        suggestions = self._call_and_persist(
            prompt, target_type="relationship",
            target_id=f"{ca['id']}-{cb['id']}",
            target_label=f"{a_name}↔{b_name}",
        )
        return suggestions

    # ---- 全局优化 ----
    def optimize_all(self) -> list[dict]:
        proj = _briefing_project(self.db)
        chars = _briefing_characters(self.db)
        threads = kb.list_threads(self.db)
        rels = kb.list_relationships(self.db)
        # 关键关系演变（取最近 3 个）
        all_evols = []
        for r in rels:
            all_evols.extend(kb.list_rel_evolution(self.db, relationship_id=r["id"]))
        all_evols.sort(key=lambda e: e["chapter_id"], reverse=True)
        recent_evols = all_evols[:6]
        scan_summary = _briefing_global_health(self.db)
        # 漂移最多的人物
        drifts = []
        for c in chars:
            if c.get("mbti"):
                d = _briefing_drift(self.db, c["id"])
                if d.get("n_drift_signals", 0) > 0:
                    drifts.append({
                        "name": c["name"],
                        "mbti": c["mbti"],
                        "n_signals": d["n_drift_signals"],
                    })
        drifts.sort(key=lambda x: -x["n_signals"])
        # 弧光滞后
        arc_lag = []
        for c in chars:
            prog = c.get("arc_progress") or 0.0
            expected = min(1.0, 0.6)
            if proj["total_chapters"] > 0:
                # 简化：不做位置加权
                pass
            if prog < expected - 0.1:
                arc_lag.append({"name": c["name"], "progress": prog})

        briefing = {
            "project": proj,
            "main_characters": [c for c in chars if c.get("role") in ("protagonist", "antagonist", "supporting")],
            "active_threads": [t for t in threads if t.get("status") in ("planted", "developing")],
            "abandoned_threads": [t for t in threads if t.get("status") == "abandoned"],
            "relationships": [{"a": r["char_a_id"], "b": r["char_b_id"], "type": r.get("rel_type", ""), "state": r.get("current_state", "")} for r in rels],
            "recent_evolutions": recent_evols,
            "drift_hotspots": drifts[:5],
            "arc_lag": arc_lag,
            "scan_health": scan_summary,
        }
        prompt = (
            f"请基于以下『项目健康度 briefing』生成全局修改建议（不是单人物/单关系，而是整体）。\n\n"
            f"## Briefing\n{json.dumps(briefing, ensure_ascii=False, indent=2)}\n\n"
            f"## 任务\n"
            f"1. 找出最影响读者体验的 3-5 个全局问题\n"
            f"2. 给出优先级 + 具体修改方向（章节范围 + 操作建议）\n"
            f"3. 不要重复 briefing 已有数据，要给出**洞察**——例如：\n"
            f"   - 'ENTJ 主角和 2 个 INTJ 配角同时存在，主角认知功能被配角稀释，建议把其中一个 INTJ 改为 INTP 制造差异'\n"
            f"   - 'X 已 abandoned 但 description 说重要——这是写作犹豫，建议要么真正写要么明确放弃'\n"
            f"   - '全书事件密度集中在前 20 章，后 20 章稀薄——这是节奏问题'"
        )
        suggestions = self._call_and_persist(
            prompt, target_type="global",
            target_id="project", target_label="全局",
        )
        return suggestions

    # ---- 内部辅助 ----
    def _call_and_persist(self, user_prompt: str, target_type: str, target_id: str, target_label: str) -> list[dict]:
        if not self.ai.ready:
            return [{
                "target_type": target_type, "target_id": target_id, "target_label": target_label,
                "title": "AI 未配置",
                "content": "请先在 .env 填入 NOVELAI_API_KEY。可用 OpenAI / Anthropic / DeepSeek 等。",
                "priority": "high", "evidence": "", "chapter_focus": "全本",
            }]
        messages = [
            {"role": "system", "content": OPTIMIZER_SYSTEM},
            {"role": "user", "content": user_prompt},
        ]
        try:
            data = self.ai.chat_json(messages, temperature=0.5)
        except AICallError as e:
            return [{
                "target_type": target_type, "target_id": target_id, "target_label": target_label,
                "title": "LLM 调用失败",
                "content": str(e),
                "priority": "high", "evidence": "", "chapter_focus": "全本",
            }]
        if not isinstance(data, dict) or "suggestions" not in data:
            return [{
                "target_type": target_type, "target_id": target_id, "target_label": target_label,
                "title": "LLM 返回格式异常",
                "content": json.dumps(data, ensure_ascii=False)[:500],
                "priority": "medium", "evidence": "", "chapter_focus": "全本",
            }]
        # 补全 target_* 字段后入库
        suggestions = data.get("suggestions")
        if not isinstance(suggestions, list):
            return [{
                "target_type": target_type, "target_id": target_id, "target_label": target_label,
                "title": "LLM 返回格式异常",
                "content": f"suggestions 应为列表，实际类型: {type(suggestions).__name__}。原始数据: {json.dumps(data, ensure_ascii=False)[:500]}",
                "priority": "medium", "evidence": "", "chapter_focus": "全本",
            }]
        out = []
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            s.setdefault("target_type", target_type)
            s.setdefault("target_id", target_id)
            s.setdefault("target_label", target_label)
            s.setdefault("title", "")
            s.setdefault("content", "")
            s.setdefault("priority", "medium")
            s.setdefault("evidence", "")
            s.setdefault("chapter_focus", "")
            out.append(s)
        kb.add_suggestions_bulk(self.db, out)
        return out

    # ---- 结构优化（3 个 level）----
    def optimize_structure(self, level: str, target_idx: int | None = None) -> list[dict]:
        from . import structure
        from .prompts import (
            STRUCTURE_OPT_SYSTEM, STRUCTURE_OPT_FULL_USER,
            STRUCTURE_OPT_VOLUME_USER, STRUCTURE_OPT_CHAPTER_USER,
        )
        ana = structure.StructureAnalyzer(self.db)
        if level == "full":
            r = ana.analyze_full()
            if r.get("error"):
                return []
            user_prompt = STRUCTURE_OPT_FULL_USER.format(
                title=kb.get_or_create_project(self.db).get("title", ""),
                n_volumes=r.get("n_volumes", 0),
                n_chapters=r.get("n_chapters", 0),
                total_words=r.get("total_words", 0),
                n_events=r.get("n_events", 0),
                n_turning_points=r.get("n_turning_points", 0),
                climax_position=r.get("climax_position"),
                phase_breakdown=json.dumps(r["phase_breakdown"], ensure_ascii=False, indent=2),
                act_breakdown=json.dumps(r["act_breakdown"], ensure_ascii=False, indent=2),
                intensity_curve="\n".join(
                    f"  第{c['chapter_idx']:>2}章  pos={c['position']:.2f}  intensity={c['intensity']:.2f}  n={c['n_events']}  turning={c['n_turning']}"
                    for c in r["intensity_curve"]
                ),
                issues="\n".join(f"  - [{it['severity']}] {it['type']}: {it['context']}" for it in r.get("issues", [])) or "（无）",
            )
            messages = [
                {"role": "system", "content": STRUCTURE_OPT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ]
            return self._call_and_persist_struct(messages, "structure", "full", "全篇结构")

        elif level == "volume":
            if target_idx is None:
                return []
            r = ana.analyze_volume(target_idx)
            if r.get("error"):
                return []
            user_prompt = STRUCTURE_OPT_VOLUME_USER.format(
                volume_idx=target_idx,
                volume_title=r.get("title", ""),
                n_chapters=r.get("n_chapters", 0),
                word_count=r.get("word_count", 0),
                n_events=r.get("n_events", 0),
                n_turning_points=r.get("n_turning_points", 0),
                turning_positions=r.get("turning_positions", []),
                importance_avg=r.get("importance_avg", 0),
                phase_breakdown=json.dumps(r["phase_breakdown"], ensure_ascii=False, indent=2),
                issues="\n".join(f"  - [{it['severity']}] {it['type']}: {it['context']}" for it in r.get("issues", [])) or "（无）",
            )
            messages = [
                {"role": "system", "content": STRUCTURE_OPT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ]
            return self._call_and_persist_struct(messages, "structure", f"vol-{target_idx}", f"第{target_idx}卷结构")

        elif level == "chapter":
            if target_idx is None:
                return []
            r = ana.analyze_chapter(target_idx)
            if r.get("error"):
                return []
            user_prompt = STRUCTURE_OPT_CHAPTER_USER.format(
                chapter_idx=target_idx,
                title=r.get("title", ""),
                word_count=r.get("word_count", 0),
                n_events=r.get("n_events", 0),
                n_turning_points=r.get("n_turning_points", 0),
                turning_positions=r.get("turning_positions", []),
                importance_avg=r.get("importance_avg", 0),
                thread_count=r.get("thread_count", 0),
                issues="\n".join(f"  - [{it['severity']}] {it['type']}: {it['context']}" for it in r.get("issues", [])) or "（无）",
            )
            messages = [
                {"role": "system", "content": STRUCTURE_OPT_SYSTEM},
                {"role": "user", "content": user_prompt},
            ]
            return self._call_and_persist_struct(messages, "structure", f"ch-{target_idx}", f"第{target_idx}章结构")
        return []

    def _call_and_persist_struct(self, messages, target_type, target_id, target_label) -> list[dict]:
        if not self.ai.ready:
            return [{
                "target_type": target_type, "target_id": target_id, "target_label": target_label,
                "title": "AI 未配置",
                "content": "请先在 .env 填入 NOVELAI_API_KEY。",
                "priority": "high", "evidence": "", "chapter_focus": "全本",
            }]
        try:
            data = self.ai.chat_json(messages, temperature=0.5)
        except AICallError as e:
            return [{
                "target_type": target_type, "target_id": target_id, "target_label": target_label,
                "title": "LLM 调用失败",
                "content": str(e),
                "priority": "high", "evidence": "", "chapter_focus": "全本",
            }]
        suggestions = data.get("suggestions") if isinstance(data, dict) else None
        if not isinstance(suggestions, list):
            return [{
                "target_type": target_type, "target_id": target_id, "target_label": target_label,
                "title": "LLM 返回格式异常",
                "content": f"未返回有效的 suggestions 列表。原始数据: {json.dumps(data, ensure_ascii=False)[:500]}" if isinstance(data, dict) else str(data)[:500],
                "priority": "medium", "evidence": "", "chapter_focus": "全本",
            }]
        out = []
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            s.setdefault("target_type", target_type)
            s.setdefault("target_id", target_id)
            s.setdefault("target_label", target_label)
            s.setdefault("title", "")
            s.setdefault("content", "")
            s.setdefault("priority", "medium")
            s.setdefault("evidence", "")
            s.setdefault("chapter_focus", "")
            out.append(s)
        kb.add_suggestions_bulk(self.db, out)
        return out


def _briefing_chapter_text(db: Database, char_name: str, char_id: int) -> list[dict]:
    """取该角色出现的最近 5 章，每章取该角色所在段落前后 200 字"""
    import re as _re
    # 词边界匹配，避免单字名 "王" 匹配 "王国"
    _boundary = r"(?:^|[\s，。！？；：「」（）、\n])"
    _char_pat = _re.compile(_boundary + _re.escape(char_name) + _boundary)

    chapters = kb.list_chapters(db)
    matched: list[dict] = []
    for ch in chapters:
        text = ch.get("final_text") or ch.get("draft") or ""
        if not text or not _char_pat.search(text):
            continue
        # 找该角色所有出现位置，每处取前后 200 字
        snippets = []
        for m in _char_pat.finditer(text):
            p = m.start() + len(m.group().split(char_name)[0])
            s = max(0, p - 200)
            e = min(len(text), p + 200)
            snippets.append(text[s:e])
        matched.append({
            "chapter_idx": ch["idx"],
            "chapter_title": ch["title"],
            "snippets": snippets[:3],  # 最多 3 段
        })
        if len(matched) >= 5:
            break
    return matched
