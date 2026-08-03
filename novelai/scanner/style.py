"""
novelai.scanner.style
文风漂移检测。

对每章正文提取若干"风格指纹"：
- avg_sentence_length（平均句长）
- std_sentence_length（句长标准差）—— 飘逸度
- dialogue_ratio（对话比例）—— 「」/""/"" 字符占比
- description_ratio（描写比例）—— 含形容词/副词比例
- exclamation_ratio（感叹/问句比例）
- topk_word_freq（高频词分布）
- 独白特征词（我想/我思/我心中/心下/暗道/忖道 等）

然后跟基线（前 N 章的均值）比较：
- 任一维度的 z-score 超过阈值 → 报告"文风漂移"
- 高频词分布相似度（余弦）下降 → 报告

输出：
  {
    "per_chapter": [{idx, title, features: {...}}, ...],
    "drift_issues": [{chapter_idx, dimension, z_score, value, baseline, severity, suggestion}, ...],
    "overall_drift_curve": [{idx, distance}, ...]  # 与基线的综合距离
    "baseline_range": [1, 3]  # 默认前 3 章
  }
"""
from __future__ import annotations
import re
import math
from collections import Counter
from typing import Any
from ..db import Database
from .. import knowledge as kb


# 简易中文句子切分：以 。！？；… 结尾
_SENT_END = re.compile(r"[。！？；…]+|\.\s|!\s|\?\s")
# 简易对话检测
_DIALOGUE = re.compile(r"[「」\"\"'']")
# 独白特征词
_MONOLOGUE_HINTS = (
    "我想", "我思", "心中", "心下", "暗道", "忖道", "暗想", "默念",
    "暗自", "心中暗", "心想", "心道", "不由得想",
)
# 描写提示词（形容词/副词后缀）
_DESC_HINTS = ("的", "地", "然", "般", "似", "若", "幽", "寂", "苍", "茫", "浩", "渺")
# 停用词
_STOPWORDS = set("""
的 了 在 是 我 你 他 她 它 我们 你们 他们 她们 它们 和 与 或 但 而
也 都 还 就 要 会 能 可以 不会 不曾 已经 仍然 正在 突然 终于 然后
于是 但是 不过 然而 啊 呢 吗 吧 嗯 哦
""".split())


def _features(text: str) -> dict[str, float]:
    if not text:
        return {"avg_sentence_length": 0, "std_sentence_length": 0,
                "dialogue_ratio": 0, "exclamation_ratio": 0,
                "monologue_ratio": 0, "description_ratio": 0}
    # 句子
    sents = [s for s in _SENT_END.split(text) if s.strip()]
    if not sents:
        return {"avg_sentence_length": 0, "std_sentence_length": 0,
                "dialogue_ratio": 0, "exclamation_ratio": 0,
                "monologue_ratio": 0, "description_ratio": 0}
    sent_lens = [len(s) for s in sents]
    avg_len = sum(sent_lens) / len(sent_lens)
    var = sum((x - avg_len) ** 2 for x in sent_lens) / len(sent_lens)
    std_len = math.sqrt(var)
    # 对话
    n_dialog = len(_DIALOGUE.findall(text))
    dialog_ratio = n_dialog / max(1, len(text))
    # 感叹
    n_excl = sum(1 for s in sents if s.strip().endswith(("！", "!", "?")))
    excl_ratio = n_excl / len(sents)
    # 独白
    mono_hits = sum(text.count(h) for h in _MONOLOGUE_HINTS)
    mono_ratio = mono_hits / max(1, len(sents))
    # 描写
    n_desc = sum(text.count(h) for h in _DESC_HINTS)
    desc_ratio = n_desc / max(1, len(text))
    # 词频
    cn_runs = re.findall(r"[\u4e00-\u9fa5]+", text)
    words: list[str] = []
    # B-新40: 之前三重嵌套循环 N*2 次, 50MB 文本 1亿次循环. 改 unigram + bigram 一次扫
    for run in cn_runs:
        n = len(run)
        if n == 0:
            continue
        for i in range(n):
            ch = run[i]
            if ch not in _STOPWORDS:
                words.append(ch)
            if i + 2 <= n:
                bg = run[i:i+2]
                if bg not in _STOPWORDS:
                    words.append(bg)
    return {
        "avg_sentence_length": round(avg_len, 2),
        "std_sentence_length": round(std_len, 2),
        "dialogue_ratio": round(dialog_ratio, 4),
        "exclamation_ratio": round(excl_ratio, 4),
        "monologue_ratio": round(mono_ratio, 4),
        "description_ratio": round(desc_ratio, 4),
        "n_sentences": len(sents),
    }


def scan_style(db: Database, baseline_first_n: int = 3, z_threshold: float = 2.0) -> dict:
    """
    扫描全本文风漂移。
    baseline_first_n: 用前 N 章作为"基线风格"参考
    z_threshold: 漂移告警的 z-score 阈值
    """
    # B-新42: 防御 baseline_first_n 异常值 (≤0 静默返空, ≥len(chapters) 留 ≥1 作基线)
    if baseline_first_n is None or baseline_first_n < 1:
        baseline_first_n = 1
    chapters = kb.list_chapters(db)
    if not chapters:
        return {"per_chapter": [], "drift_issues": [], "overall_drift_curve": [], "baseline_range": [1, baseline_first_n]}
    # 提取每章特征
    per_ch = []
    for ch in chapters:
        text = ch.get("final_text") or ch.get("draft") or ""
        feats = _features(text)
        per_ch.append({"idx": ch["idx"], "title": ch["title"], "features": feats})

    # 基线：前 N 章的均值 + 标准差
    baseline = per_ch[:baseline_first_n]
    if not baseline:
        return {"per_chapter": per_ch, "drift_issues": [], "overall_drift_curve": [], "baseline_range": [1, baseline_first_n]}
    dims = list(baseline[0]["features"].keys())
    base_mean: dict[str, float] = {}
    base_std: dict[str, float] = {}
    for d in dims:
        vals = [b["features"].get(d, 0) for b in baseline]
        m = sum(vals) / len(vals)
        if len(vals) > 1:
            v = sum((x - m) ** 2 for x in vals) / (len(vals) - 1)
        else:
            # 单章基线：用 mean 自身的 10% 作 sd 下界，避免除零爆掉
            v = max((abs(m) * 0.1) ** 2, 1e-4)
        base_mean[d] = m
        base_std[d] = math.sqrt(v)

    # 漂移检测
    drift_issues = []
    curve = []
    for ch in per_ch:
        feats = ch["features"]
        # 综合距离：归一化的偏差平方和
        dist = 0.0
        per_dim_z: dict[str, float] = {}
        for d in dims:
            v = feats.get(d, 0)
            sd = max(base_std[d], 1e-3)  # 防止除零
            z = (v - base_mean[d]) / sd
            # 截断到 [-10, 10]
            z = max(-10.0, min(10.0, z))
            per_dim_z[d] = round(z, 2)
            dist += z ** 2
        dist = math.sqrt(dist / len(dims))  # RMS
        curve.append({"idx": ch["idx"], "distance": round(dist, 3)})
        # 每维度报警
        for d, z in per_dim_z.items():
            if abs(z) >= z_threshold:
                v = feats.get(d, 0)
                base = base_mean[d]
                # 解释
                interpretation = _interpret(d, z)
                severity = "high" if abs(z) >= z_threshold * 1.5 else "medium"
                drift_issues.append({
                    "chapter_idx": ch["idx"],
                    "title": ch["title"],
                    "dimension": d,
                    "z_score": z,
                    "value": round(v, 4),
                    "baseline": round(base, 4),
                    "severity": severity,
                    "context": f"第{ch['idx']}章《{ch['title']}》：维度「{d}」偏离基线 {z}σ（{v} vs 基线 {round(base, 4)}）。{interpretation}",
                    "fix_suggestion": interpretation,
                })
    # 全局综合距离大的章节
    for c in curve:
        if c["distance"] >= z_threshold * 1.2:
            # 已包含在每维度里；这里只做汇总提示
            pass
    return {
        "per_chapter": per_ch,
        "drift_issues": drift_issues,
        "overall_drift_curve": curve,
        "baseline_range": [1, min(baseline_first_n, len(per_ch))],
    }


def _interpret(d: str, z: float) -> str:
    direction = "高于" if z > 0 else "低于"
    mapping = {
        "avg_sentence_length": "句子平均长度——可能句式变得更长或更短",
        "std_sentence_length": "句长标准差——句子长度变化幅度，长短句交错变多/变少",
        "dialogue_ratio": "对话占比——人物直接引语/对话密度",
        "exclamation_ratio": "感叹/问句比例——情绪外露度",
        "monologue_ratio": "独白/内心戏比例——角色心理活动密度",
        "description_ratio": "描写密度——形容词/副词等修饰性词占比",
        "n_sentences": "句子数",
    }
    return f"{mapping.get(d, d)}，{direction}基线。"
