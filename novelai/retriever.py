"""
novelai.retriever
上下文检索引擎——为"写第 N 章"准备高信噪比的上下文。

召回策略（按重要性排序）：
1. 本章大纲相关的 POV 角色档案
2. POV 角色的"已知事实"清单（信息边界）
3. 本章其他出场人物的精简档案
4. 上一章摘要 + 上一章未完成动作
5. 最近 N 章的事件摘要（带时间戳）
6. 涉及地点的世界观
7. 标记为"本章推进"的伏笔/线索

所有方法都返回结构化 dict，便于上层 writer 拼 prompt。
"""
from __future__ import annotations
import re
import time
from typing import Any
from . import knowledge as kb
from .db import Database

# 上下文缓存（project 不变时复用，减少 DB 查询）
# BUG 修复：旧实现用单一全局 _cache["t"]，任一 key 刷新会重置所有 key 的 TTL，
# 且 invalidate_cache 从未被调用——导致编辑人物/事实/设定后 60s 内仍读到旧数据。
# 改为按 key 存 (ts, val)，TTL 各自独立。
_cache: dict[str, tuple[float, Any]] = {}
_CACHE_TTL = 60  # 60 秒


def invalidate_cache() -> None:
    """外部修改数据后调用，强制下次重建上下文"""
    _cache.clear()


def _cached(key: str, db: Database, fetcher, force: bool = False) -> Any:
    now = time.time()
    entry = _cache.get(key)
    if not force and entry is not None and now - entry[0] < _CACHE_TTL:
        return entry[1]
    val = fetcher(db)
    _cache[key] = (now, val)
    return val


# ---------- Embedding 语义检索（增强召回，provider 不支持时静默降级） ----------

def _semantic_recall(
    db: Database,
    query: str,
    entity_type: str,
    items: list[dict],
    text_fn,
    top_k: int = 8,
    min_score: float = 0.25,
) -> set[int]:
    """用 embedding 语义检索召回相关实体 id。

    返回与 query 语义相关的 entity_id 集合。provider 不支持 embeddings 时返回空集
    （调用方应与原 LIKE 匹配取并集，而非替换）。
    """
    # 全局开关检查
    from .config import CONFIG
    if not CONFIG.ai.enable_embedding:
        return set()
    if not query or not items:
        return set()
    try:
        from . import embeddings as emb
        from .ai_client import AIClient
        ai = AIClient()
        if not ai.ready:
            return set()
        # 确保索引（源文本变了才重算，命中缓存则零开销）
        emb.ensure_indexed(db, ai, entity_type, items, text_fn)
        qv = ai.embed([query[:1500]])  # 截断防超长
        if not qv:
            return set()
        results = emb.search(db, qv[0], entity_type, top_k=top_k)
        return {eid for eid, score in results if score >= min_score}
    except Exception:
        # NotImplementedError（anthropic）或 provider 不支持 embeddings → 静默降级
        return set()


def _char_brief(c: dict) -> str:
    """精简人物档案（用于 prompt 上下文）"""
    parts = [f"# {c['name']}（{c.get('role','')}）"]
    if c.get("aliases"):
        parts.append(f"别号：{', '.join(c['aliases'])}")
    if c.get("basic_info"):
        parts.append(f"基础信息：{c['basic_info']}")
    if c.get("personality"):
        parts.append(f"性格：{c['personality']}")
    if c.get("speech_style"):
        parts.append(f"说话风格：{c['speech_style']}")
    if c.get("abilities"):
        parts.append(f"能力：{c['abilities']}")
    if c.get("arc"):
        parts.append(f"人物弧光：{c['arc']}")
    if c.get("status"):
        parts.append(f"当前状态：{c['status']}")
    return "\n".join(parts)


def _char_full(c: dict) -> str:
    return _char_brief(c) + (f"\n扩展：{c.get('extra', {})}" if c.get("extra") else "")


# role 重要度权重（用于 200+ 人物场景的召回裁剪排序）
_ROLE_WEIGHT = {
    "protagonist": 5,
    "antagonist": 4,
    "major": 3,
    "supporting": 2,
    "minor": 1,
}


def _rank_characters(chars: list[dict]) -> list[dict]:
    """按重要度排序：role 权重降序 → 出场次数降序 → 名字（稳定排序）。"""
    return sorted(
        chars,
        key=lambda c: (
            -_ROLE_WEIGHT.get(c.get("role", "supporting"), 2),
            -(c.get("appearance_count") or 0),
            c.get("name", ""),
        ),
    )


def _char_brief_compact(c: dict) -> str:
    """minor 角色精简档案（~50 token），只保留 name + role + 一句话 basic_info。"""
    parts = [f"- {c['name']}（{c.get('role', 'supporting')}）"]
    bi = (c.get("basic_info") or "").strip()
    if bi:
        parts.append(bi[:60])
    return "：".join(parts[:2])


def _fact_brief(f: dict) -> str:
    return f"- [{f.get('category','general')}|{f.get('reliability','reliable')}] {f['content']}"


def _thread_brief(t: dict) -> str:
    return (
        f"- [{t['thread_type']}|{t['status']}] {t['title']} —— {t.get('description','')}"
        + (f" 备注：{t['notes']}" if t.get('notes') else "")
    )


def build_chapter_context(
    db: Database,
    chapter_idx: int,
    recent_window: int = 3,
) -> dict:
    """
    为生成 chapter_idx 的正文准备上下文。
    返回的 dict 可直接喂给 prompts.CHAPTER_USER_TEMPLATE。
    """
    project = kb.get_or_create_project(db)
    # B-新56: 防御 chapter_idx ≤0 (前端 Path(ge=1) 兜了, 但 retriever 也兜)
    if not isinstance(chapter_idx, int) or chapter_idx < 1:
        raise ValueError(f"chapter_idx 必须 ≥1, 收到 {chapter_idx!r}")
    chapter = kb.get_chapter_by_idx(db, chapter_idx)
    if not chapter:
        raise ValueError(f"第 {chapter_idx} 章不存在")

    # POV 角色
    pov = None
    if chapter.get("pov_character_id"):
        pov = kb.get_character(db, chapter["pov_character_id"])

    # 时间单位
    time_unit = project.get("story_time_unit") or "日"

    # 大纲
    outline = chapter.get("outline") or ""

    # 相关人物：名字 LIKE 匹配 + embedding 语义召回（取并集）
    mentioned_char_ids: set[int] = set()
    if pov:
        mentioned_char_ids.add(pov["id"])
    all_chars = _cached("characters", db, kb.list_characters)
    # 1) 精确名字匹配（LIKE）
    for c in all_chars:
        names = [c["name"]] + (c.get("aliases") or [])
        for n in names:
            if n and n in outline:
                mentioned_char_ids.add(c["id"])
                break
    # 2) embedding 语义召回（捕捉代称/语义相关但字面不匹配的人物，如"那个戴兜帽的女人"→某角色）
    mentioned_char_ids |= _semantic_recall(
        db, outline, "character", all_chars,
        text_fn=lambda c: " ".join(filter(None, [c.get("name"), " ".join(c.get("aliases") or []), c.get("basic_info") or "", c.get("personality") or ""])),
        top_k=5,
    )
    # 回退：当无 POV 且未匹配到任何人物时，只塞 top-3 主角（非全量，防 token 爆炸）
    if not mentioned_char_ids:
        main_chars = _rank_characters([c for c in all_chars if c.get("role") in ("protagonist", "antagonist")])
        for c in main_chars[:3]:
            mentioned_char_ids.add(c["id"])

    # 关系：与 POV 角色有关的所有关系；无 POV 时显示所有提到人物间的关系
    rels_text_parts = []
    all_rels = kb.list_relationships(db)
    if pov:
        for r in all_rels:
            if r["char_a_id"] == pov["id"] or r["char_b_id"] == pov["id"]:
                other_id = r["char_b_id"] if r["char_a_id"] == pov["id"] else r["char_a_id"]
                other = kb.get_character(db, other_id)
                if other:
                    rels_text_parts.append(
                        f"- {pov['name']} ↔ {other['name']}：{r['rel_type']}（{r.get('current_state','')}）"
                    )
    else:
        # 无 POV：显示所有提到人物之间的关系
        char_id_to_name = {c["id"]: c["name"] for c in all_chars}
        for r in all_rels:
            if r["char_a_id"] in mentioned_char_ids and r["char_b_id"] in mentioned_char_ids:
                a_name = char_id_to_name.get(r["char_a_id"], "?")
                b_name = char_id_to_name.get(r["char_b_id"], "?")
                rels_text_parts.append(
                    f"- {a_name} ↔ {b_name}：{r['rel_type']}（{r.get('current_state','')}）"
                )
    rels_text = "\n".join(rels_text_parts) if rels_text_parts else "（无）"

    # 已知事实（POV 信息边界）
    if pov:
        known = kb.facts_known_by(db, pov["id"])
        # 章节已建立的事实（按章节 id 过滤）也加入
        known_facts_text = "\n".join(_fact_brief(f) for f in known) if known else "（无）"
    else:
        known_facts_text = "（全知视角）"

    # 其他出场人物档案（按重要度排序，top-K 展开，其余 minor 只列名）
    other_chars_raw = []
    for cid in mentioned_char_ids:
        if pov and cid == pov["id"]:
            continue
        c = kb.get_character(db, cid)
        if c:
            other_chars_raw.append(c)
    ranked = _rank_characters(other_chars_raw)
    MAX_FULL_PROFILES = 8  # 展开完整档案的上限（防 token 爆炸）
    other_profiles_text = ""
    for i, c in enumerate(ranked):
        if i < MAX_FULL_PROFILES:
            other_profiles_text += "\n" + _char_full(c) + "\n"
        else:
            # 超出 top-K 的次要角色只列精简名（让 AI 知道存在但不占 token）
            other_profiles_text += "\n" + _char_brief_compact(c)
    if len(ranked) > MAX_FULL_PROFILES:
        other_profiles_text += f"\n（另有 {len(ranked) - MAX_FULL_PROFILES} 个次要人物，仅列名）"
    if not other_profiles_text.strip():
        other_profiles_text = "（除 POV 外无其他明确出场人物）"

    # 世界观：按章节地点 LIKE 匹配 + embedding 语义召回；无匹配则退化到所有设定
    world_text = ""
    location = chapter.get("location") or ""
    all_world = _cached("world", db, kb.list_world)
    world_by_id = {w["id"]: w for w in all_world}
    matched_world_ids: set[int] = set()
    if location:
        items = kb.search_world(db, location)
        matched_world_ids = {it["id"] for it in items}
        # embedding 召回（用 location+outline 作为 query，捕捉语义相关的设定）
        world_query = f"{location} {outline}".strip()
        matched_world_ids |= _semantic_recall(
            db, world_query, "world_setting", all_world,
            text_fn=lambda w: " ".join(filter(None, [w.get("name"), w.get("category"), w.get("content") or ""])),
            top_k=6,
        )
    # 渲染匹配到的设定
    if matched_world_ids:
        matched = [world_by_id[wid] for wid in matched_world_ids if wid in world_by_id]
        world_text = "\n".join(
            f"- [{it['category']}] {it['name']}：{it['content']}" for it in matched
        )
    if not world_text.strip():
        # 无地点或无匹配 → 退化到所有设定
        if all_world:
            world_text = "\n".join(
                f"- [{it['category']}] {it['name']}：{it['content']}" for it in all_world
            )
    if not world_text.strip():
        world_text = "（无相关设定）"

    # 上一章摘要 + 未完成动作
    prev_summary = ""
    prev_unfinished = "（无）"
    prev_chapter = kb.get_prev_chapter(db, chapter_idx)  # idx 可能跳号，取实际上一章
    if prev_chapter:
        prev_summary = prev_chapter.get("summary") or ""
        # H4 修复：优先读 chapter.unfinished_action（consistency 检查写入的结构化字段）
        prev_unfinished = (prev_chapter.get("unfinished_action") or "").strip()
        # 回退：从 summary 末尾正则提取 UNFINISHED_ACTION 标记
        if not prev_unfinished and prev_summary:
            m = re.search(r"UNFINISHED_ACTION[:：]\s*(.+)", prev_summary)
            if m and m.group(1).strip():
                prev_unfinished = m.group(1).strip()
        if not prev_unfinished:
            prev_unfinished = "（无）"
        # 上一章 final_text 的最后几行作为承接提示
        if prev_chapter.get("final_text"):
            tail = prev_chapter["final_text"][-400:]
            if not prev_summary:
                prev_summary = f"（上一章最后片段）\n{tail}"

    # 临近章节事件摘要（带时间戳）
    all_chapters = kb.list_chapters(db)
    recent_summaries_parts = []
    # 窗口：取本章之前的最近 recent_window 章（按 idx 感知跳号，不用绝对减法）
    prev_chapters = [ch for ch in all_chapters if ch["idx"] < chapter_idx]
    window_chapters = prev_chapters[-recent_window:] if recent_window > 0 else []
    for ch in window_chapters:
        ch_events = kb.list_events(db, ch["id"])
        if ch["summary"]:
            recent_summaries_parts.append(
                f"第{ch['idx']}章 {ch['title']}（时间 {ch.get('story_time_start')}~{ch.get('story_time_end')}）：{ch['summary']}"
            )
        for ev in ch_events:
            recent_summaries_parts.append(
                f"  事件@{ev['story_time']} [{ev['event_type']}] {ev['title']}：{ev['summary']}"
            )
    recent_summaries_text = "\n".join(recent_summaries_parts) if recent_summaries_parts else "（无）"

    # 伏笔/线索：所有 planted/developing 状态，与本章地点/人物/大纲相关的优先
    threads = kb.list_threads(db, status="planted") + kb.list_threads(db, status="developing")
    # embedding 召回与本章大纲语义相关的伏笔（捕捉字面不匹配但语义相关的线索）
    thread_query = f"{outline} {location}".strip()
    semantic_thread_ids = _semantic_recall(
        db, thread_query, "plot_thread", threads,
        text_fn=lambda t: " ".join(filter(None, [t.get("title"), t.get("description") or "", t.get("notes") or ""])),
        top_k=6,
    )
    thread_parts = []
    for t in threads:
        related_ids = set(t.get("related_characters") or [])
        # 触达条件：相关人物在本章出现 或 描述中包含本章地点 或 embedding 语义相关
        trigger = False
        if related_ids & mentioned_char_ids:
            trigger = True
        if location and location in (t.get("title","") + (t.get("description","") or "")):
            trigger = True
        if t["id"] in semantic_thread_ids:
            trigger = True
        # 若无明确触发但状态是 developing 也带上
        if t["status"] == "developing" or trigger:
            thread_parts.append(_thread_brief(t))
    # top-K 裁剪：长篇可能有几十条伏笔，全塞会超 token。优先 developing，最多 15 条
    MAX_THREADS = 15
    if len(thread_parts) > MAX_THREADS:
        thread_parts = thread_parts[:MAX_THREADS]
    threads_text = "\n".join(thread_parts) if thread_parts else "（无）"

    # 借鉴 AI_NovelGenerator 的 next_chapter_draft：前瞻下一章，让 AI 为下一章冲突埋种子
    next_chapter = kb.get_chapter_by_idx(db, chapter_idx + 1)
    next_chapter_title = next_chapter.get("title", "") if next_chapter else ""
    next_chapter_outline = (next_chapter.get("outline") or "") if next_chapter else ""

    # 分层记忆 L3：全书摘要（截至当前卷）
    book_sum = kb.get_book_summary(db)
    book_summary_text = (book_sum["summary"] if book_sum else "（暂无，开篇阶段）")

    # 分层记忆 L2：本卷进展（volume.synopsis）
    volume_summary_text = "（暂无，第一卷开篇）"
    ch_volume_idx = chapter.get("volume_idx")
    if ch_volume_idx:
        vol = kb.get_volume_by_idx(db, ch_volume_idx)
        if vol and vol.get("synopsis"):
            volume_summary_text = vol["synopsis"]

    # 分层记忆 RAG：全本高重要性事件（importance≥4），上限 20 条，让 AI 看到全局关键节点
    key_events = db.query(
        "SELECT e.title, e.summary, e.story_time, e.event_type, c.idx as ch_idx "
        "FROM event e JOIN chapter c ON e.chapter_id=c.id "
        "WHERE e.importance>=4 AND c.idx<? ORDER BY e.story_time DESC LIMIT 20",
        (chapter_idx,),
    )
    if key_events:
        ke_parts = [f"第{e['ch_idx']}章 @{e['story_time']} [{e['event_type']}] {e['title']}：{(e['summary'] or '')[:40]}" for e in key_events]
        key_events_text = "\n".join(ke_parts)
    else:
        key_events_text = "（暂无）"

    return {
        "synopsis": project.get("synopsis", ""),
        "book_summary": book_summary_text,
        "volume_summary": volume_summary_text,
        "key_events": key_events_text,
        "style": project.get("style", ""),
        "pov_mode": project.get("pov_mode", "限知视角"),
        "outline": outline,
        "story_time_start": chapter.get("story_time_start"),
        "story_time_end": chapter.get("story_time_end"),
        "time_unit": time_unit,
        "pov_profile": _char_full(pov) if pov else "（无明确 POV 角色 / 全知视角）",
        "known_facts": known_facts_text,
        "other_characters_profiles": other_profiles_text,
        "relationships": rels_text,
        "world_settings": world_text,
        "prev_chapter_summary": prev_summary or "（这是第一章）",
        "prev_chapter_unfinished": prev_unfinished,
        "recent_event_summaries": recent_summaries_text,
        "relevant_threads": threads_text,
        "next_chapter_title": next_chapter_title,
        "next_chapter_outline": next_chapter_outline,
    }


def build_consistency_context(
    db: Database,
    chapter_idx: int,
    chapter_text: str,
) -> dict:
    """为一致性审查准备上下文（更广、更全）"""
    ctx = build_chapter_context(db, chapter_idx, recent_window=5)
    ctx["chapter_text"] = chapter_text

    # 事实库：top-K 裁剪（长篇可能有几百条事实，全塞会超 token 导致 JSON 截断）
    all_facts = _cached("facts", db, kb.list_facts)
    if all_facts:
        # 按 established_chapter_id 降序（近章事实优先），最多 60 条
        sorted_facts = sorted(all_facts, key=lambda f: f.get("established_chapter_id") or 0, reverse=True)
        ctx["world_facts"] = "\n".join(_fact_brief(f) for f in sorted_facts[:60])
    else:
        ctx["world_facts"] = "（事实库为空）"

    # 临近事件 + 时间戳
    parts = []
    for ch in kb.list_chapters(db):
        if ch["idx"] >= chapter_idx:
            continue
        for ev in kb.list_events(db, ch["id"]):
            parts.append(
                f"第{ch['idx']}章 @{ev['story_time']} [{ev['event_type']}] {ev['title']}：{ev['summary']}"
            )
    ctx["recent_events_with_time"] = "\n".join(parts) if parts else "（无）"
    ctx["prev_unfinished"] = ctx.get("prev_chapter_unfinished", "（无）")

    # 借鉴 AI_NovelGenerator 的 plot_arcs 思路：注入所有待回收伏笔（developing/planted）
    # 让一致性审查检查"本章是否遗忘了应推进的伏笔"
    # top-K 裁剪：长篇可能有几十条，全塞超 token 导致 JSON 截断
    active_threads = kb.list_threads(db, status="planted") + kb.list_threads(db, status="developing")
    if active_threads:
        # developing 优先，最多 20 条
        sorted_at = sorted(active_threads, key=lambda t: 0 if t.get("status") == "developing" else 1)
        ctx["active_threads"] = "\n".join(_thread_brief(t) for t in sorted_at[:20])
    else:
        ctx["active_threads"] = "（无）"
    return ctx
