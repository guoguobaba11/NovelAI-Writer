"""
novelai.structure
叙事结构分析器——三层次：章回 / 全卷 / 全篇

起承转合经典 4 段式（按位置归一化 0~1）：
  起（Setup）         : 0.00 - 0.15
  承（Development）  : 0.15 - 0.60
  转（Climax/Turn）  : 0.60 - 0.80
  合（Resolution）   : 0.80 - 1.00

注：阈值是"启发式参考"，不是绝对值——实际叙事结构可以自由发挥，
工具的作用是**让作者看到自己目前的结构分布**。

8 大结构问题检测：
  1. 无转折点（卷/章内 importance>=4 的事件 < 1）
  2. 前重后轻（前 1/3 重要性 > 后 1/3 重要性 ×1.5）
  3. 节奏塌陷（连续 3+ 章重要性 < 1.0）
  4. 卷间断档（前卷 climax < 0.5，下卷 setup 提前 0.4）
  5. 节奏过密（单章 > 15 个事件 或 > 12K 字）
  6. 节奏过疏（单章 < 1 个事件 且 < 800 字）
  7. 伏笔失衡（前 1/3 累积 > 70% planted）
  8. 伏笔集中揭晓（> 50% payoff 集中在最后 15%）
"""
from __future__ import annotations
import math
from typing import Any
from . import knowledge as kb
from .db import Database


# ============================================================
# 4 段位置
# ============================================================

PHASE_BOUNDS = {
    "setup": (0.00, 0.15),
    "development": (0.15, 0.60),
    "climax": (0.60, 0.80),
    "resolution": (0.80, 1.00),
}

PHASE_LABELS_CN = {
    "setup": "起（铺陈）",
    "development": "承（发展）",
    "climax": "转（高潮）",
    "resolution": "合（收束）",
}


# ============================================================
# 工具
# ============================================================

def _phase_of(pos: float) -> str:
    for name, (lo, hi) in PHASE_BOUNDS.items():
        if lo <= pos < hi:
            return name
    return "resolution"


def _normalized_pos(idx: int, total: int) -> float:
    if total <= 1:
        return 0.5
    return (idx - 1) / (total - 1)


# ============================================================
# 结构分析器
# ============================================================

class StructureAnalyzer:
    def __init__(self, db: Database):
        self.db = db

    # ----- 章回级 -----
    def analyze_chapter(self, chapter_idx: int) -> dict:
        # B-新59: 防御 chapter_idx ≤0
        if not isinstance(chapter_idx, int) or chapter_idx < 1:
            return {"error": f"chapter_idx 必须 ≥1, 收到 {chapter_idx!r}"}
        chapter = kb.get_chapter_by_idx(self.db, chapter_idx)
        if not chapter:
            return {"error": f"第 {chapter_idx} 章不存在"}
        events = kb.list_events(self.db, chapter_id=chapter["id"])
        threads = kb.list_threads(self.db)
        # 在本章 planted/resolved 的伏笔
        threads_in_chapter = [t for t in threads if t.get("planted_chapter_id") == chapter["id"] or t.get("resolved_chapter_id") == chapter["id"] or t.get("payoff_chapter_id") == chapter["id"]]
        n = len(events)
        # 重要性分布
        importances = [e.get("importance", 3) or 3 for e in events]
        avg_imp = sum(importances) / max(1, n)
        # 转折点
        turning = [e for e in events if e.get("event_type") == "turning_point"]
        # 章内事件位置（按 sequence_in_chapter 归一化）
        seq_max = max((e.get("sequence_in_chapter") or 1) for e in events) if events else 1
        turning_positions = []
        for e in turning:
            seq = e.get("sequence_in_chapter") or 1
            turning_positions.append(round(seq / max(1, seq_max), 2))
        # 问题检测
        issues = []
        word_count = chapter.get("word_count") or 0
        if n == 0:
            issues.append({"severity": "medium", "type": "no_events", "context": f"本章无任何事件（是否未抽取？）"})
        elif n > 15:
            issues.append({"severity": "medium", "type": "too_dense", "context": f"本章事件数 {n} 偏多（>15），节奏可能过密"})
        if word_count > 12000:
            issues.append({"severity": "low", "type": "very_long", "context": f"本章 {word_count} 字（>12000），读者注意力可能衰减"})
        elif 0 < word_count < 800:
            issues.append({"severity": "low", "type": "very_short", "context": f"本章仅 {word_count} 字（<800），可能过短"})
        if not turning and n > 3:
            issues.append({"severity": "low", "type": "no_turning", "context": "本章无 turning_point 事件，是否缺关键转折？"})
        if avg_imp < 1.5 and n > 3:
            issues.append({"severity": "low", "type": "low_intensity", "context": f"本章平均重要性 {avg_imp:.1f} 偏低（建议 ≥2.0）"})
        return {
            "chapter_idx": chapter_idx,
            "title": chapter.get("title", ""),
            "word_count": word_count,
            "n_events": n,
            "n_turning_points": len(turning),
            "importance_avg": round(avg_imp, 2),
            "turning_positions": turning_positions,
            "thread_count": len(threads_in_chapter),
            "issues": issues,
        }

    # ----- 卷级 -----
    def analyze_volume(self, volume_idx: int) -> dict:
        volume = kb.get_volume_by_idx(self.db, volume_idx)
        if not volume:
            return {"error": f"第 {volume_idx} 卷不存在"}
        chapters = kb.list_chapters(self.db, volume_idx=volume_idx)
        if not chapters:
            return {"error": f"第 {volume_idx} 卷无章节"}
        # 卷内所有事件
        all_events = []
        for ch in chapters:
            all_events.extend(kb.list_events(self.db, chapter_id=ch["id"]))
        # 章节位置归一化
        n = len(chapters)
        # 4 段位置（按章节）
        ch_phase_map: dict[int, str] = {}
        for i, ch in enumerate(chapters):
            pos = _normalized_pos(i + 1, n)
            ch_phase_map[ch["id"]] = _phase_of(pos)
        # 4 段事件分布
        phase_events: dict[str, list] = {p: [] for p in PHASE_BOUNDS}
        for ev in all_events:
            p = ch_phase_map.get(ev["chapter_id"])
            if p:
                phase_events[p].append(ev)
        # 4 段伏笔动作
        threads = kb.list_threads(self.db)
        ch_id_to_idx = {ch["id"]: i + 1 for i, ch in enumerate(chapters)}
        phase_threads_planted: dict[str, list] = {p: [] for p in PHASE_BOUNDS}
        phase_threads_payoff: dict[str, list] = {p: [] for p in PHASE_BOUNDS}
        for t in threads:
            pid = t.get("planted_chapter_id")
            if pid in ch_id_to_idx:
                p = ch_phase_map.get(pid)
                if p: phase_threads_planted[p].append(t)
            pid = t.get("payoff_chapter_id") or t.get("resolved_chapter_id")
            if pid in ch_id_to_idx:
                p = ch_phase_map.get(pid)
                if p: phase_threads_payoff[p].append(t)
        # 卷阶段质量
        issues = []
        n_setup = len(phase_events["setup"])
        n_dev = len(phase_events["development"])
        n_climax = len(phase_events["climax"])
        n_resol = len(phase_events["resolution"])
        # 全卷重要性
        all_imp = [e.get("importance", 3) or 3 for e in all_events]
        avg_imp = sum(all_imp) / max(1, len(all_imp))
        # 转折点
        turning = [e for e in all_events if e.get("event_type") == "turning_point"]
        turning_pos = []
        for e in turning:
            cid = e["chapter_id"]
            if cid in ch_id_to_idx and n > 0:
                turning_pos.append(_normalized_pos(ch_id_to_idx[cid], n))
        # 判定
        if n_climax == 0:
            issues.append({"severity": "high", "type": "no_climax", "context": f"第{volume_idx}卷无任何高潮事件（应在 60-80% 处出现 turning_point）"})
        elif turning_pos and min(turning_pos) < 0.5:
            issues.append({"severity": "medium", "type": "climax_too_early", "context": f"第{volume_idx}卷高潮位置 {min(turning_pos):.2f}（应 ≥0.60）"})
        if n_setup > n_dev and n_setup > 3:
            issues.append({"severity": "medium", "type": "setup_too_heavy", "context": f"第{volume_idx}卷起（{n_setup}）>承（{n_dev}），铺陈过重"})
        if n_resol == 0 and n > 5:
            issues.append({"severity": "low", "type": "no_resolution", "context": f"第{volume_idx}卷无收束事件，可能结尾过快"})
        # 伏笔失衡
        n_planted = sum(len(v) for v in phase_threads_planted.values())
        n_payoff = sum(len(v) for v in phase_threads_payoff.values())
        planted_in_setup = len(phase_threads_planted["setup"])
        if n_planted > 0 and planted_in_setup / n_planted > 0.7 and n_planted >= 3:
            issues.append({"severity": "medium", "type": "foreshadowing_imbalance",
                          "context": f"第{volume_idx}卷 {planted_in_setup}/{n_planted}（{planted_in_setup/n_planted*100:.0f}%）的伏笔在起（setup）阶段铺设——太集中"})
        # 卷长
        word_count = sum(c.get("word_count") or 0 for c in chapters)
        if len(chapters) < 5:
            issues.append({"severity": "low", "type": "volume_short", "context": f"第{volume_idx}卷仅 {len(chapters)} 章（建议 8-12）"})
        elif len(chapters) > 15:
            issues.append({"severity": "low", "type": "volume_long", "context": f"第{volume_idx}卷有 {len(chapters)} 章（建议 ≤15）"})
        return {
            "volume_idx": volume_idx,
            "title": volume.get("title", ""),
            "n_chapters": len(chapters),
            "word_count": word_count,
            "n_events": len(all_events),
            "n_turning_points": len(turning),
            "importance_avg": round(avg_imp, 2),
            "phase_breakdown": {
                p: {
                    "label": PHASE_LABELS_CN[p],
                    "chapter_range": self._phase_chapter_range(chapters, p),
                    "n_events": len(phase_events[p]),
                    "n_threads_planted": len(phase_threads_planted[p]),
                    "n_threads_payoff": len(phase_threads_payoff[p]),
                    "importance_avg": round(
                        sum((e.get("importance",3) or 3) for e in phase_events[p]) / max(1, len(phase_events[p])), 2),
                } for p in PHASE_BOUNDS
            },
            "turning_positions": [round(p, 2) for p in turning_pos],
            "issues": issues,
        }

    def _phase_chapter_range(self, chapters, phase: str) -> list[int]:
        n = len(chapters)
        ids = []
        for i, ch in enumerate(chapters):
            pos = _normalized_pos(i + 1, n)
            if _phase_of(pos) == phase:
                ids.append(ch["idx"])
        return [min(ids), max(ids)] if ids else []

    # ----- 全篇级 -----
    def analyze_full(self) -> dict:
        chapters = kb.list_chapters(self.db)
        if not chapters:
            return {"error": "无章节"}
        volumes = kb.list_volumes(self.db)
        all_events = kb.list_events(self.db)
        threads = kb.list_threads(self.db)
        n = len(chapters)
        word_count = sum(c.get("word_count") or 0 for c in chapters)
        # 章节位置
        ch_idx_to_pos = {c["idx"]: _normalized_pos(i + 1, n) for i, c in enumerate(chapters)}
        # 4 段事件
        phase_events: dict[str, list] = {p: [] for p in PHASE_BOUNDS}
        for ev in all_events:
            ch = kb.get_chapter(self.db, ev["chapter_id"])
            if ch:
                pos = ch_idx_to_pos.get(ch["idx"], 0.5)
                p = _phase_of(pos)
                phase_events[p].append(ev)
        # 重要性曲线（按章节）
        intensity_curve: list[dict] = []
        for ch in chapters:
            evs = [e for e in all_events if e["chapter_id"] == ch["id"]]
            avg = sum((e.get("importance",3) or 3) for e in evs) / max(1, len(evs)) if evs else 0
            intensity_curve.append({
                "chapter_idx": ch["idx"],
                "position": round(ch_idx_to_pos[ch["idx"]], 3),
                "intensity": round(avg, 2),
                "n_events": len(evs),
                "n_turning": sum(1 for e in evs if e.get("event_type") == "turning_point"),
            })
        # 全篇高潮位置
        all_imp = [e.get("importance", 3) or 3 for e in all_events]
        max_imp = max(all_imp) if all_imp else 0
        climax_chapter = None
        if max_imp >= 4:
            for ev in all_events:
                if (ev.get("importance") or 3) == max_imp:
                    ch = kb.get_chapter(self.db, ev["chapter_id"])
                    if ch:
                        climax_chapter = ch["idx"]
                        break
        # 3-act 比例
        act_breakdown = []
        # 末章归一化位置恰好为 1.0，半开区间 [lo,hi) 会把它排除在所有 act 外，
        # 因此最后一个 act 用闭区间 <=hi，其余仍为半开区间。
        for act_name, (lo, hi) in [
            ("act1_setup", (0.0, 0.25)),
            ("act2_development", (0.25, 0.75)),
            ("act3_resolution", (0.75, 1.0)),
        ]:
            if hi >= 1.0:
                act_chs = [c for c in chapters if lo <= ch_idx_to_pos[c["idx"]] <= hi]
            else:
                act_chs = [c for c in chapters if lo <= ch_idx_to_pos[c["idx"]] < hi]
            act_events = [e for e in all_events
                          if any(c["id"] == e["chapter_id"] for c in act_chs)]
            act_breakdown.append({
                "name": act_name,
                "label": {"act1_setup": "第一幕（开端）", "act2_development": "第二幕（发展）", "act3_resolution": "第三幕（收束）"}[act_name],
                "position_range": [lo, hi],
                "chapter_range": [act_chs[0]["idx"], act_chs[-1]["idx"]] if act_chs else None,
                "n_chapters": len(act_chs),
                "n_events": len(act_events),
                "word_count": sum(c.get("word_count") or 0 for c in act_chs),
            })
        # 伏笔总体
        n_planted = sum(1 for t in threads if t.get("status") in ("planted", "developing") and t.get("planted_chapter_id"))
        n_payoff = sum(1 for t in threads if t.get("status") in ("payoff", "resolved"))
        n_payoff_chs: set[int] = set()
        for t in threads:
            pid = t.get("payoff_chapter_id") or t.get("resolved_chapter_id")
            if pid:
                n_payoff_chs.add(pid)
        # 4 段伏笔动作
        phase_threads_planted: dict[str, list] = {p: [] for p in PHASE_BOUNDS}
        phase_threads_payoff: dict[str, list] = {p: [] for p in PHASE_BOUNDS}
        for t in threads:
            pid = t.get("planted_chapter_id")
            if pid:
                p = _phase_of(ch_idx_to_pos.get(self._chapter_idx_of_chapter_id(pid), 0.5))
                if p: phase_threads_planted[p].append(t)
            pid = t.get("payoff_chapter_id") or t.get("resolved_chapter_id")
            if pid:
                p = _phase_of(ch_idx_to_pos.get(self._chapter_idx_of_chapter_id(pid), 0.5))
                if p: phase_threads_payoff[p].append(t)
        # 问题检测
        issues = []
        # 1. 转折点缺失
        turning_all = [e for e in all_events if e.get("event_type") == "turning_point"]
        if len(turning_all) < max(2, n // 10):
            issues.append({"severity": "high", "type": "few_turning_points",
                          "context": f"全篇仅 {len(turning_all)} 个 turning_point 事件（建议 ≥{max(2, n//10)} 个）"})
        # 2. 前重后轻（需要至少 3 章才有"前/后 1/3"可比性）
        n_third = max(1, n // 3)
        if n >= 3:
            first_third_imp = sum(intensity_curve[i]["intensity"] for i in range(n_third)) / n_third
            last_third_imp = sum(intensity_curve[i]["intensity"] for i in range(max(0, n - n_third), n)) / n_third
            if first_third_imp > last_third_imp * 1.5 and first_third_imp > 2.5:
                issues.append({"severity": "medium", "type": "front_heavy",
                              "context": f"前 1/3 平均重要性 {first_third_imp:.1f}，后 1/3 仅 {last_third_imp:.1f}——前重后轻"})
        # 3. 塌陷（连续 3+ 章 importance < 1.0）
        for i in range(n - 2):
            window = intensity_curve[i:i+3]
            if all(w["intensity"] < 1.0 for w in window):
                issues.append({"severity": "medium", "type": "intensity_sink",
                              "context": f"第 {window[0]['chapter_idx']}-{window[-1]['chapter_idx']} 章连续 3 章 importance < 1.0（塌陷）"})
                break
        # 4. 卷间断档（如果有多卷）
        if len(volumes) >= 2:
            for i in range(len(volumes) - 1):
                v_a = self.analyze_volume(volumes[i]["idx"])
                v_b = self.analyze_volume(volumes[i + 1]["idx"])
                if v_a.get("error") or v_b.get("error"):
                    continue
                # A 卷 climax 太早（< 0.5）+ B 卷 setup 太早（< 0.3）→ 衔接塌陷
                a_climax = min(v_a["turning_positions"]) if v_a.get("turning_positions") else None
                b_setup_pos = 0.0
                rng = v_b.get("phase_breakdown", {}).get("setup", {}).get("chapter_range")
                if rng and len(rng) >= 2 and rng[1] != rng[0]:
                        # 取 setup 阶段的章节位置归一化
                        ch_idx_setup = rng[0]
                        b_setup_pos = ch_idx_to_pos.get(ch_idx_setup, 0)
                if a_climax and a_climax < 0.5 and b_setup_pos < 0.3:
                    issues.append({"severity": "high", "type": "volume_disconnect",
                                  "context": f"第{volumes[i]['idx']}卷高潮 ({a_climax:.2f}) 与第{volumes[i+1]['idx']}卷铺陈 ({b_setup_pos:.2f}) 衔接塌陷"})
        # 5. 节奏过密/过疏
        for ch in chapters:
            evs_n = sum(1 for e in all_events if e["chapter_id"] == ch["id"])
            wc = ch.get("word_count") or 0
            if evs_n > 15:
                issues.append({"severity": "medium", "type": "chapter_too_dense",
                              "context": f"第 {ch['idx']} 章 {evs_n} 个事件，节奏过密"})
            elif evs_n == 0 and wc > 1000:
                issues.append({"severity": "low", "type": "chapter_no_events",
                              "context": f"第 {ch['idx']} 章 {wc} 字但无事件——可能未抽取或纯过渡章"})
        # 7. 伏笔失衡
        planted_in_first_third = sum(1 for t in threads
                                      if t.get("planted_chapter_id")
                                      and ch_idx_to_pos.get(self._chapter_idx_of_chapter_id(t["planted_chapter_id"]), 0.5) < 0.33)
        if n_planted > 0 and planted_in_first_third / n_planted > 0.7:
            issues.append({"severity": "medium", "type": "foreshadowing_imbalance",
                          "context": f"前 1/3 累积了 {planted_in_first_third}/{n_planted} ({planted_in_first_third/n_planted*100:.0f}%) 的伏笔——铺设过集中"})
        # 8. 伏笔集中揭晓
        payoff_in_last_15pct = sum(1 for cid in n_payoff_chs
                                    if ch_idx_to_pos.get(self._chapter_idx_of_chapter_id(cid), 0.5) > 0.85)
        if n_payoff > 0 and payoff_in_last_15pct / n_payoff > 0.5 and n_payoff >= 3:
            issues.append({"severity": "medium", "type": "foreshadowing_late_payoff",
                          "context": f"后 15% 集中揭晓了 {payoff_in_last_15pct}/{n_payoff}（{payoff_in_last_15pct/n_payoff*100:.0f}%）的伏笔——应分批揭晓"})
        return {
            "n_chapters": n,
            "n_volumes": len(volumes),
            "total_words": word_count,
            "n_events": len(all_events),
            "n_turning_points": len(turning_all),
            "intensity_curve": intensity_curve,
            "climax_chapter_idx": climax_chapter,
            # climax_chapter 是章 idx（可能跳号），用 ch_idx_to_pos 取归一化位置而非 idx/n
            "climax_position": round(ch_idx_to_pos[climax_chapter], 3) if climax_chapter in ch_idx_to_pos else None,
            "act_breakdown": act_breakdown,
            "phase_breakdown": {
                p: {
                    "label": PHASE_LABELS_CN[p],
                    "position_range": list(PHASE_BOUNDS[p]),
                    "n_events": len(phase_events[p]),
                    "importance_avg": round(
                        sum((e.get("importance",3) or 3) for e in phase_events[p]) / max(1, len(phase_events[p])), 2),
                    "n_threads_planted": len(phase_threads_planted[p]),
                    "n_threads_payoff": len(phase_threads_payoff[p]),
                } for p in PHASE_BOUNDS
            },
            "issues": issues,
        }

    def _chapter_idx_of_chapter_id(self, chapter_id: int) -> int:
        ch = kb.get_chapter(self.db, chapter_id)
        return ch["idx"] if ch else 0

    # ----- 全篇结构问题汇总 -----
    def full_issues_summary(self) -> dict:
        """全篇问题汇总：把 3 个 level 的 issues 合并 + 高亮。"""
        chapters_issues = []
        for c in kb.list_chapters(self.db):
            r = self.analyze_chapter(c["idx"])
            if r.get("issues"):
                chapters_issues.append({"chapter_idx": c["idx"], "title": c["title"], "issues": r["issues"]})
        volumes_issues = []
        for v in kb.list_volumes(self.db):
            r = self.analyze_volume(v["idx"])
            if r.get("issues"):
                volumes_issues.append({"volume_idx": v["idx"], "title": v["title"], "issues": r["issues"]})
        full_r = self.analyze_full()
        full_issues = full_r.get("issues", []) if not full_r.get("error") else []
        return {
            "chapters": chapters_issues,
            "volumes": volumes_issues,
            "full": full_issues,
            "total_issues": sum(len(x.get("issues", [])) for x in chapters_issues + volumes_issues) + len(full_issues),
        }
