"""
novelai.personality
MBTI 16型 + 认知功能 + 性格冲突分析。

16型对应表（每型 4 个字母，4 维）：
- I/E（内倾/外倾）
- S/N（实感/直觉）
- T/F（思维/情感）
- J/P（判断/感知）

每型对应 8 个认知功能中的 4 个（主功能/辅助/第三/劣势）：
- 主功能 (dominant)
- 辅助 (auxiliary)
- 第三 (tertiary)
- 劣势 (inferior)

8 维认知功能：
- Te (外向思维) Ti (内向思维)
- Fe (外向情感) Fi (内向情感)
- Se (外向实感) Si (内向实感)
- Ne (外向直觉) Ni (内向直觉)
"""
from __future__ import annotations
from typing import Any
import math
from . import knowledge as kb


# ============================================================
# 16 型 → 认知功能栈
# ============================================================

MBTI_STACK: dict[str, list[str]] = {
    "ISTJ": ["Si", "Te", "Fi", "Ne"],
    "ISFJ": ["Si", "Fe", "Ti", "Ne"],
    "INFJ": ["Ni", "Fe", "Ti", "Se"],
    "INTJ": ["Ni", "Te", "Fi", "Se"],
    "ISTP": ["Ti", "Se", "Ni", "Fe"],
    "ISFP": ["Fi", "Se", "Ni", "Te"],
    "INFP": ["Fi", "Ne", "Si", "Te"],
    "INTP": ["Ti", "Ne", "Si", "Fe"],
    "ESTP": ["Se", "Ti", "Fe", "Ni"],
    "ESFP": ["Se", "Fi", "Te", "Ni"],
    "ENFP": ["Ne", "Fi", "Te", "Si"],
    "ENTP": ["Ne", "Ti", "Fe", "Si"],
    "ESTJ": ["Te", "Si", "Ne", "Fi"],
    "ESFJ": ["Fe", "Si", "Ne", "Ti"],
    "ENFJ": ["Fe", "Ni", "Se", "Ti"],
    "ENTJ": ["Te", "Ni", "Se", "Fi"],
}


# 维度的"对立功能"映射
OPPOSITE_FUNCTION = {
    "Te": "Ti", "Ti": "Te",
    "Fe": "Fi", "Fi": "Fe",
    "Se": "Si", "Si": "Se",
    "Ne": "Ni", "Ni": "Ne",
}


# ============================================================
# 性格关键词表（用于 baseline_keywords 自动生成）
# ============================================================

# 每个认知功能 → 典型行为/对话关键词
FUNCTION_KEYWORDS: dict[str, dict[str, list[str]]] = {
    "Te": {
        "positive": ["效率", "结果", "目标", "控制", "组织", "执行", "决策", "权威", "管理", "规范", "系统", "逻辑", "责任", "计划", "指标", "改进"],
        "negative": ["专横", "强势", "霸道", "控制欲", "功利", "无情", "急功近利"],
    },
    "Ti": {
        "positive": ["分析", "原理", "本质", "精确", "严密", "逻辑自洽", "独立思考", "求真", "模型", "分类"],
        "negative": ["钻牛角尖", "冷漠", "孤僻", "脱离实际", "理论派"],
    },
    "Fe": {
        "positive": ["和谐", "共情", "体贴", "群体", "氛围", "他人感受", "社交", "维护关系", "顾及面子"],
        "negative": ["八面玲珑", "过度在意他人", "失去自我", "情绪化"],
    },
    "Fi": {
        "positive": ["价值观", "真实", "深度情感", "内在信念", "理想主义", "忠于自我", "同理心", "真诚", "独特"],
        "negative": ["敏感", "过度内省", "自我", "情绪化", "不合群"],
    },
    "Se": {
        "positive": ["行动", "当下", "感官", "体验", "冒险", "即兴", "临场反应", "实用", "享受"],
        "negative": ["冲动", "鲁莽", "纵欲", "短视"],
    },
    "Si": {
        "positive": ["传统", "细节", "记忆", "可靠", "经验", "稳定", "惯例", "忠诚"],
        "negative": ["保守", "怀旧", "固执", "抗拒变化"],
    },
    "Ne": {
        "positive": ["联想", "创意", "可能性", "发散", "好奇", "想象", "多角度", "灵感"],
        "negative": ["跳跃", "不切实际", "难以专注", "虎头蛇尾"],
    },
    "Ni": {
        "positive": ["洞察", "远见", "本质", "预见", "专注", "直觉", "战略", "深度", "神秘"],
        "negative": ["神秘主义", "钻牛角尖", "脱离现实", "执念"],
    },
}


def mbti_to_keywords(mbti: str) -> list[str]:
    """从 MBTI 推一组 baseline 关键词（基于其认知功能）"""
    stack = MBTI_STACK.get(mbti.upper())
    if not stack:
        return []
    kws: list[str] = []
    # 主功能 + 辅助给高权重
    for fn in stack[:2]:
        kws.extend(FUNCTION_KEYWORDS[fn]["positive"])
    return list(dict.fromkeys(kws))  # 去重保持顺序


def get_stack(mbti: str) -> list[str]:
    return MBTI_STACK.get(mbti.upper(), [])


# ============================================================
# 性格冲突分析
# ============================================================

# 共享认知功能越多越和谐；越少越冲突
def compatibility_score(mbti_a: str, mbti_b: str) -> dict:
    """
    返回 {score, shared, total, interpretation}
    score: 0~1，1 为完全相同
    """
    if not mbti_a or not mbti_b:
        return {"score": 0, "shared": 0, "total": 4, "interpretation": "未知"}
    a = mbti_a.upper()
    b = mbti_b.upper()
    sa = set(MBTI_STACK.get(a, []))
    sb = set(MBTI_STACK.get(b, []))
    if not sa or not sb:
        return {"score": 0, "shared": 0, "total": 4, "interpretation": "未知"}
    shared = len(sa & sb)
    total = 4
    score = shared / total
    # 字母维度匹配度
    dim_match = sum(1 for x, y in zip(a, b) if x == y)
    # 综合
    combined = (shared / 4) * 0.7 + (dim_match / 4) * 0.3
    interp = _interpret_compat(combined, a, b, shared)
    return {
        "score": round(combined, 2),
        "shared_functions": shared,
        "letter_match": dim_match,
        "interpretation": interp,
    }


def _interpret_compat(score: float, a: str, b: str, shared: int) -> str:
    if score >= 0.85:
        return f"高契合：{a} 与 {b} 在认知功能栈共享 {shared}/4，行为模式高度一致，关系易稳固但可能缺乏互补。"
    if score >= 0.65:
        return f"较好：{a} 与 {b} 共享 {shared}/4 功能，互信顺畅。"
    if score >= 0.4:
        return f"互补型：{a} 与 {b} 共享 {shared}/4，差异带来张力但也带来互补，适合师徒/对手关系。"
    return f"易冲突：{a} 与 {b} 共享仅 {shared}/4，价值取向与行为模式差异大，需大量笔触化解冲突。"


# ============================================================
# 人物矩阵生成
# ============================================================

def build_character_matrix(characters: list[dict]) -> dict:
    """
    返回 Web 端用的矩阵：
    {
      "characters": [{id, name, role, mbti, stack, ...}],
      "matrix": {
        "<charA>": { "<charB>": {score, ...}, ... },
        ...
      }
    }
    """
    chars_clean = []
    for c in characters:
        mbti = (c.get("mbti") or "").upper()
        stack = get_stack(mbti) if mbti else []
        chars_clean.append({
            "id": c["id"],
            "name": c["name"],
            "role": c.get("role", "supporting"),
            "mbti": mbti,
            "stack": stack,
            "stack_str": "-".join(stack),
            "cognitive_dominant": stack[0] if stack else "",
            "arc_type": c.get("arc_type", ""),
            "arc_progress": c.get("arc_progress") or 0.0,
        })
    matrix: dict[str, dict] = {}
    for a in chars_clean:
        matrix[a["name"]] = {}
        for b in chars_clean:
            if a["id"] == b["id"]:
                matrix[a["name"]][b["name"]] = {"score": 1.0, "self": True}
            else:
                matrix[a["name"]][b["name"]] = compatibility_score(a["mbti"], b["mbti"])
    return {"characters": chars_clean, "matrix": matrix}


# ============================================================
# 性格漂移检测（不依赖 LLM）
# ============================================================

import re

# 对话 / 行为指示词
_DIALOGUE_PAT = re.compile(r"[「」\"\"'']")
_MONOLOGUE = ("我想", "我思", "心中", "心下", "暗道", "忖道", "暗想", "默念", "暗自", "心想", "心道", "不由得想", "我心中")


def _extract_character_segments(text: str, char_name: str) -> list[str]:
    """从正文中提取"该角色相关的段落"——简化做法：含角色名的句子前后 50 字。"""
    segments = []
    if not text or not char_name:
        return segments
    sentences = re.split(r"(?<=[。！？；…\n])", text)
    for s in sentences:
        if char_name in s:
            segments.append(s)
    return segments


def _function_score(segments: list[str], func: str) -> float:
    """根据 segment 里关键词出现频次给该功能一个得分（0~1 归一化）"""
    if not segments:
        return 0.0
    pos_kws = FUNCTION_KEYWORDS.get(func, {}).get("positive", [])
    neg_kws = FUNCTION_KEYWORDS.get(func, {}).get("negative", [])
    text = " ".join(segments)
    pos = sum(text.count(k) for k in pos_kws)
    neg = sum(text.count(k) for k in neg_kws)
    # 线性归一化：0=无关键词, 1=全正面关键词
    total = pos + neg
    if total == 0:
        return 0.05  # 微小的基线偏移，确保漂移检测可达 <0.3
    return pos / total


def analyze_chapter_personality(
    text: str,
    char_name: str,
    mbti: str,
    baseline_keywords: list[str] | None = None,
) -> dict:
    """
    对单章该角色的"性格指纹"做分析：
    - 8 个认知功能在该章的强度
    - 与 baseline_keywords 的重合度
    - 简易 MBTI 推断（基于功能强度）
    """
    stack = get_stack(mbti)
    if not stack:
        return {"error": f"unknown MBTI: {mbti}"}
    segments = _extract_character_segments(text, char_name)
    if not segments:
        return {
            "char_name": char_name,
            "mbti_baseline": mbti,
            "n_segments": 0,
            "function_scores": {},
            "baseline_overlap": 0.0,
            "inferred_mbti": None,
            "drift_signals": ["无相关段落"],
        }
    # 8 功能得分
    func_scores = {fn: round(_function_score(segments, fn), 3) for fn in FUNCTION_KEYWORDS}
    # 与 baseline 关键词的重合度
    overlap = 0.0
    if baseline_keywords:
        joined = " ".join(segments)
        hit = sum(1 for kw in baseline_keywords if kw in joined)
        overlap = hit / max(1, len(baseline_keywords))
    # 简易 MBTI 推断：取两个最强功能作为主+辅
    sorted_fns = sorted(func_scores.items(), key=lambda x: -x[1])
    top1, top2 = sorted_fns[0][0], sorted_fns[1][0]
    inferred = _functions_to_mbti(top1, top2)
    # 漂移信号
    signals = []
    # 主功能 / 辅助功能在该章得分偏低
    if stack:
        dom_score = func_scores.get(stack[0], 0)
        aux_score = func_scores.get(stack[1], 0) if len(stack) > 1 else 0
        if dom_score < 0.3:
            signals.append(f"主功能 {stack[0]} 表现弱 ({dom_score})，可能偏离 baseline")
        if aux_score < 0.3:
            signals.append(f"辅助功能 {stack[1]} 表现弱 ({aux_score})，可能偏离 baseline")
    # 劣势功能异常强（"压力下使用劣势功能" = grip stress）
    if len(stack) >= 4:
        inf_score = func_scores.get(stack[3], 0)
        if inf_score > 0.7:
            signals.append(f"劣势功能 {stack[3]} 异常活跃 ({inf_score})——可能是'grip'压力状态，慎用避免持续")
    return {
        "char_name": char_name,
        "mbti_baseline": mbti,
        "n_segments": len(segments),
        "function_scores": func_scores,
        "baseline_overlap": round(overlap, 3),
        "inferred_mbti": inferred,
        "drift_signals": signals,
    }


def scan_personality_drift(
    db,
    characters: list[dict] | None = None,
    chapter_window: int | None = None,
) -> list[dict]:
    """
    扫描全本每个有 mbti 的主要角色的性格漂移。
    返回每章每角色的 {char_id, char_name, chapter_idx, mbti, function_scores, drift_signals, baseline_overlap, inferred_mbti}
    """
    if characters is None:
        characters = [c for c in kb.list_characters(db) if c.get("mbti")]
    if not characters:
        return []
    chapters = kb.list_chapters(db)
    out: list[dict] = []
    for c in characters:
        baseline_kws = c.get("baseline_keywords") or mbti_to_keywords(c["mbti"])
        for ch in chapters:
            if chapter_window is not None and ch["idx"] > chapter_window:
                continue
            text = ch.get("final_text") or ch.get("draft") or ""
            if not text:
                continue
            r = analyze_chapter_personality(text, c["name"], c["mbti"], baseline_kws)
            if "error" in r:
                continue
            r["char_id"] = c["id"]
            r["chapter_idx"] = ch["idx"]
            r["chapter_title"] = ch["title"]
            # 该角色在该章节是否出现过
            if r["n_segments"] == 0:
                # 不视为漂移——可能 POV 不在这章
                r["drift_signals"] = []
            out.append(r)
    return out


def _functions_to_mbti(dominant: str, auxiliary: str) -> str | None:
    """根据 主导 + 辅助两个功能反推 MBTI 字母"""
    if not dominant or not auxiliary:
        return None
    # 用 8 维字母表
    # 主功能：J/P + 4维字母 + 内外倾
    # 简化：已知 16 型的 dom+aux 唯一，反查
    for mbti, stack in MBTI_STACK.items():
        if stack[0] == dominant and stack[1] == auxiliary:
            return mbti
    return None
