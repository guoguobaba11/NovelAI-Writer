"""
novelai.consistency
程序化硬校验层（不依赖 LLM）——给 LLM 一致性审查之外的第二道防线。

聚焦四个用户最关心的维度：
1. 信息边界泄漏：POV 角色不知道的关键名词短语是否出现在正文
2. 时间线乱序：故事内时间是否单调
3. 人物关系引用：正文提到的人名是否都有定义
4. 事件链断裂：上一章未完成动作是否在本章被回应

调用方式：
    from novelai.consistency import hard_check
    issues = hard_check(db, chapter_idx, chapter_text)
"""
from __future__ import annotations
import re
from typing import Any
from . import knowledge as kb
from .db import Database


# 常见中文停用词，提取名词短语时跳过
STOPWORDS = set("""
的 了 在 是 我 你 他 她 它 我们 你们 他们 她们 它们 和 与 或 但 而
也 都 还 就 要 会 能 可以 不会 不曾 已经 仍然 正在 突然 终于 然后
于是 但是 不过 然而 啊 呢 吗 吧 嗯 哦 啊 哎 喂
这里 那里 那个 这个 什么 怎么 为什么 怎样 哪里 哪个
""".split())


def _extract_keywords(text: str, min_len: int = 2, top_k: int = 30) -> list[str]:
    """
    关键词提取：优先使用 jieba 分词；回退到 n-gram 滑动窗口。
    """
    if not text:
        return []
    try:
        import jieba
        words = [w for w in jieba.cut(text)
                 if len(w) >= min_len and w not in STOPWORDS and re.search(r'[\u4e00-\u9fa5]', w)]
        from collections import Counter
        counter = Counter(words)
        # 只保留非停用词、非纯标点、长度≥min_len 的词
        keywords = [w for w, _ in counter.most_common(top_k) if w not in STOPWORDS]
        # 补上英文关键词
        en_segs = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text)
        en_count: dict[str, int] = {}
        for s in en_segs:
            en_count[s] = en_count.get(s, 0) + 1
        en_items = sorted(en_count.items(), key=lambda x: -x[1])
        keywords += [w for w, _ in en_items[:top_k]]
        return keywords[:top_k * 2]
    except ImportError:
        pass
    # 回退：n-gram 滑动窗口
    cn_segs = re.findall(r"[\u4e00-\u9fa5]{2,6}", text)
    cn_count: dict[str, int] = {}
    for s in cn_segs:
        if s in STOPWORDS:
            continue
        cn_count[s] = cn_count.get(s, 0) + 1
    en_segs = re.findall(r"[A-Za-z][A-Za-z\-]{2,}", text)
    en_count: dict[str, int] = {}
    for s in en_segs:
        en_count[s] = en_count.get(s, 0) + 1
    items = sorted(cn_count.items(), key=lambda x: (-x[1], -len(x[0])))
    en_items = sorted(en_count.items(), key=lambda x: -x[1])
    keywords = [w for w, _ in items[:top_k]] + [w for w, _ in en_items[:top_k]]
    return keywords


def _check_info_leak(db: Database, chapter_idx: int, text: str) -> list[dict]:
    """
    信息边界硬校验。

    判定 POV 是否"应知"一条事实：
    - reliability ∈ {secret, rumored}：默认 POV 不知道，除非显式 known_by 含 POV
    - reliability ∈ {reliable, false}：
        - 如果 known_by 非空：仅 known_by 列表中的人物知道
        - 如果 known_by 为空：视为"公开/上帝全知"，不报警

    满足"POV 不知道"且正文出现核心关键词 → 报警。
    """
    chapter = kb.get_chapter_by_idx(db, chapter_idx)
    if not chapter or not chapter.get("pov_character_id"):
        return []
    pov_id = chapter["pov_character_id"]
    issues = []
    all_facts = kb.list_facts(db)
    for f in all_facts:
        known = f.get("known_by") or []
        rel = f.get("reliability") or "reliable"
        # 判定 POV 是否应知
        if rel in ("secret", "rumored"):
            # 默认 POV 不知道；显式 known_by 含 POV 才算知道
            if pov_id in known:
                continue
            pov_should_know = False
        else:  # reliable / false
            if not known:
                # 公开事实
                continue
            if pov_id in known:
                continue
            pov_should_know = False
        # 到这里 = POV 不应知
        fk = _extract_keywords(f["content"], top_k=8)
        if not fk:
            continue
        anchors = [k for k in fk if len(k) >= 3][:5]
        if not anchors:
            continue
        for a in anchors:
            if a in text:
                issues.append({
                    "severity": "high",
                    "category": "info_leak",
                    "location": f"含关键词「{a}」",
                    "explanation": (
                        f"POV 角色不应知道的事实（reliability={rel}）：{f['content'][:80]}"
                    ),
                    "fix_suggestion": "删除/模糊化该信息，或在事实库中把 POV 加入 known_by，或切换为全知视角段落。",
                })
                break
    return issues


def _check_unknown_characters(db: Database, chapter_idx: int, text: str) -> list[dict]:
    """
    检查正文提到的人名是否在 character 表中。
    简化做法：取 character.name/aliases 组成词典；若正文出现未登记的人名，标记。
    """
    chars = kb.list_characters(db)
    name_set: set[str] = set()
    for c in chars:
        name_set.add(c["name"])
        for a in c.get("aliases") or []:
            if a:
                name_set.add(a)
    if not name_set:
        return []
    # 扩展 name_set：把已登记姓名前 2 字、3 字也作为"已知"避免子串误报
    expanded = set(name_set)
    for n in list(name_set):
        for L in (1, 2, 3):
            if len(n) > L:
                expanded.add(n[:L])
    # 提取正文中的可能人名：先把文本拆成连续汉字段，再在每段上做 2-4 字滑动窗口
    cn_runs = re.findall(r"[\u4e00-\u9fa5]+", text)
    candidates: set[str] = set()
    # B-新64: 之前三重循环 N*3 次, 50MB 文本 ~7500万次. 改 unigram-only 累加 + set 自动去重
    for run in cn_runs:
        n = len(run)
        if n < 2:
            continue
        # 分别按窗长提取，确保所有位置都被覆盖
        for i in range(n - 1):
            candidates.add(run[i:i+2])
        for i in range(n - 2):
            candidates.add(run[i:i+3])
        for i in range(n - 3):
            candidates.add(run[i:i+4])
    unknown = []
    for cand in candidates:
        if cand in expanded:
            continue
        # 启发式：是否像人名（不含"的""了""在"等停用词，含常见姓/名）
        if cand in STOPWORDS:
            continue
        # 简单判断：含常见中文姓氏 或 为已登记姓名的子串
        common_surnames = "王李张刘陈杨黄赵周吴徐孙马朱胡郭何高林罗宋郑谢韩唐冯于董萧程曹袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段雷钱汤尹黎易常武乔贺赖龚文"
        if cand[0] in common_surnames and cand not in expanded and len(cand) <= 3:
            unknown.append(cand)
    if not unknown:
        return []
    return [{
        "severity": "low",
        "category": "worldbuilding",
        "location": f"未登记人名：{', '.join(unknown[:10])}",
        "explanation": "正文出现了 character 表中未登记的人名；可能是误识别或新增人物。",
        "fix_suggestion": "若是新增人物，请先用 add_character 登记到知识库；若为误识别，可忽略。",
    }]


def _check_timeline_monotonic(db: Database, chapter_idx: int) -> list[dict]:
    """检查事件时间是否随章节序号单调不减。"""
    issues = []
    chapters = kb.list_chapters(db)
    by_idx = {c["idx"]: c for c in chapters}
    if chapter_idx not in by_idx:
        return issues
    cur = by_idx[chapter_idx]
    cur_t_start = cur.get("story_time_start")
    if cur_t_start is None:
        return issues
    # 与前几章对比：要求前章 end <= 当前章 start（时间随章节单调不减）
    for prev_idx in range(1, chapter_idx):
        prev = by_idx.get(prev_idx)  # idx 可能跳号（如 1,10,18,32），缺号跳过
        if prev is None:
            continue
        prev_t_end = prev.get("story_time_end")
        if prev_t_end is None:
            continue
        if prev_t_end > cur_t_start + 1e-6:
            issues.append({
                "severity": "medium",
                "category": "timeline",
                "location": f"第{prev_idx}章 vs 第{chapter_idx}章",
                "explanation": f"第{prev_idx}章结束时间 {prev_t_end} 晚于第{chapter_idx}章起始时间 {cur_t_start}。",
                "fix_suggestion": "检查大纲中两章的时间标注。",
            })
            # 不 break：收集所有时序冲突以便一次性修复
    return issues


def _check_unfinished_continuity(db: Database, chapter_idx: int, text: str) -> list[dict]:
    """检查上一章的 UNFINISHED_ACTION 是否在本章被回应。"""
    prev = kb.get_prev_chapter(db, chapter_idx)  # idx 可能跳号，取实际上一章
    if not prev:
        return []
    prev_summary = prev.get("summary") or ""
    m = re.search(r"UNFINISHED_ACTION[:：]\s*(.+)", prev_summary)
    if not m:
        return []
    unfinished = m.group(1).strip()
    if not unfinished:
        return []
    # 在本章大纲或正文中查找关键词
    kws = _extract_keywords(unfinished, top_k=8)
    kws = [k for k in kws if len(k) >= 2][:6]
    if not kws:
        return []
    matched = sum(1 for k in kws if k in text)
    if matched == 0:
        return [{
            "severity": "medium",
            "category": "causality",
            "location": "本章正文",
            "explanation": f"上一章未完成动作「{unfinished}」未在本章找到明显回应。",
            "fix_suggestion": "在本章前段让 POV 角色延续该动作或显式提及。",
        }]
    return []


def hard_check(db: Database, chapter_idx: int, chapter_text: str) -> list[dict]:
    """运行全部硬校验。返回 issues 列表。"""
    issues: list[dict] = []
    # B-新65: 各 check 独立 try, 一个挂不影响其他 (e.g. _check_unknown_characters 跑几千万次, 内存爆掉不影响时间线)
    import logging
    _log = logging.getLogger("novelai.consistency")
    for fn, args in [
        (_check_info_leak, (db, chapter_idx, chapter_text)),
        (_check_unknown_characters, (db, chapter_idx, chapter_text)),
        (_check_timeline_monotonic, (db, chapter_idx)),
        (_check_unfinished_continuity, (db, chapter_idx, chapter_text)),
    ]:
        try:
            issues += fn(*args)
        except Exception:
            _log.warning(f"hard_check: {fn.__name__} 失败", exc_info=True)
    return issues
