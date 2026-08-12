"""
novelai.writer
章节生成与一致性检查流水线。

典型流程：
1. 准备大纲（outline 阶段）
2. 生成章节正文
3. 生成摘要 + 提取事件
4. 跑一致性检查
5. 如果有 high severity issue，自动重写或人工确认
6. 写入数据库
"""
from __future__ import annotations
import re
import time
import json
from typing import Any
from .db import Database
from . import knowledge as kb
from . import retriever
from .ai_client import AIClient, AICallError
from .config import CONFIG, context_budget
from . import prompts


# ============================================================
# 1. 大纲生成
# ============================================================

def generate_outline(
    db: Database,
    ai: AIClient,
    target_chapters: int = 30,
) -> dict:
    """
    根据项目信息生成章节目录大纲，并写入 chapter 表（仅大纲，不生成正文）。
    """
    project = kb.get_or_create_project(db)
    chars = kb.list_characters(db)
    chars_brief = "\n".join(
        f"- {c['name']}（{c.get('role','')}）：{c.get('basic_info','')} 性格：{c.get('personality','')}"
        for c in chars
    ) or "（尚未定义人物）"

    threads = kb.list_threads(db)
    beats = "\n".join(
        f"- [{t.get('thread_type','')}] {t['title']}：{t.get('description','')} (状态：{t['status']})"
        for t in threads
    ) or "（尚未定义关键事件/伏笔）"

    messages = [
        {"role": "system", "content": prompts.OUTLINE_SYSTEM},
        {"role": "user", "content": _format_user(prompts.OUTLINE_USER_TEMPLATE,
            synopsis=project.get("synopsis") or "（未填写）",
            style=project.get("style") or "（未指定）",
            pov_mode=project.get("pov_mode") or "限知视角",
            time_unit=project.get("story_time_unit") or "回",
            target_chapters=target_chapters,
            characters_brief=chars_brief,
            key_beats=beats,
        )},
    ]
    data = ai.chat_json(messages, temperature=0.7)
    chapters = data.get("chapters", [])

    # 写入 chapter 表
    char_name_to_id = {c["name"]: c["id"] for c in chars}
    for ch in chapters:
        idx = ch["idx"]
        pov_name = ch.get("pov_character") or ""
        pov_id = char_name_to_id.get(pov_name)
        # outline 拼接：摘要 + 承接 + 钩子 + 关键事件(beats) + 伏笔(threads_touched)
        # beats/threads_touched 是 AI 规划的"本章必须覆盖要点"，写正文时必须能看到
        outline_parts = [ch.get("summary", "")]
        if ch.get("causal_link"):
            outline_parts.append(f"\n\n【承接】{ch['causal_link']}")
        if ch.get("hook"):
            outline_parts.append(f"\n\n【钩子】{ch['hook']}")
        beats = ch.get("beats") or []
        if beats:
            outline_parts.append("\n\n【关键事件（本章必须覆盖）】\n" + "\n".join(f"- {b}" for b in beats))
        tt = ch.get("threads_touched") or []
        if tt:
            outline_parts.append("\n\n【本章应推进的伏笔/线索】\n" + "、".join(tt))
        outline_text = "".join(outline_parts)
        existing = kb.get_chapter_by_idx(db, idx)
        if existing:
            kb.update_chapter(
                db, existing["id"],
                title=ch.get("title", existing["title"]),
                outline=outline_text,
                story_time_start=ch.get("story_time"),
                story_time_end=ch.get("story_time"),
                location=ch.get("location", existing.get("location","")),
                pov_character_id=pov_id if pov_id else existing.get("pov_character_id") or None,
            )
        else:
            kb.add_chapter(
                db,
                idx=idx,
                title=ch.get("title", f"第{idx}章"),
                outline=outline_text,
                story_time_start=ch.get("story_time"),
                story_time_end=ch.get("story_time"),
                location=ch.get("location", ""),
                pov_character_id=pov_id,
            )
    return data


def _save_outline_chapters(db: Database, chapters: list, chars: list) -> None:
    """把 AI 返回的章节大纲写入 chapter 表（generate_outline 和 batched 共用）。"""
    char_name_to_id = {c["name"]: c["id"] for c in chars}
    for ch in chapters:
        if not isinstance(ch, dict):
            continue
        idx = ch.get("idx")
        if not isinstance(idx, int) or idx < 1:
            continue
        pov_name = ch.get("pov_character") or ""
        pov_id = char_name_to_id.get(pov_name)
        # outline 拼接：摘要 + 承接 + 钩子 + 关键事件(beats) + 伏笔(threads_touched)
        outline_parts = [ch.get("summary", "")]
        if ch.get("causal_link"):
            outline_parts.append(f"\n\n【承接】{ch['causal_link']}")
        if ch.get("hook"):
            outline_parts.append(f"\n\n【钩子】{ch['hook']}")
        beats = ch.get("beats") or []
        if beats:
            outline_parts.append("\n\n【关键事件（本章必须覆盖）】\n" + "\n".join(f"- {b}" for b in beats))
        tt = ch.get("threads_touched") or []
        if tt:
            outline_parts.append("\n\n【本章应推进的伏笔/线索】\n" + "、".join(tt))
        outline_text = "".join(outline_parts)
        existing = kb.get_chapter_by_idx(db, idx)
        if existing:
            kb.update_chapter(
                db, existing["id"],
                title=ch.get("title", existing["title"]),
                outline=outline_text,
                story_time_start=ch.get("story_time"),
                story_time_end=ch.get("story_time"),
                location=ch.get("location", existing.get("location","")),
                pov_character_id=pov_id if pov_id else existing.get("pov_character_id") or None,
            )
        else:
            kb.add_chapter(
                db, idx=idx,
                title=ch.get("title", f"第{idx}章"),
                outline=outline_text,
                story_time_start=ch.get("story_time"),
                story_time_end=ch.get("story_time"),
                location=ch.get("location", ""),
                pov_character_id=pov_id,
            )


def generate_outline_batched(
    db: Database,
    ai: AIClient,
    target_chapters: int = 30,
    on_phase=None,
) -> dict:
    """分批生成大纲，避免单次 AI 输出超 token 导致 JSON 截断。
    每批 BATCH_SIZE 章，通过 on_phase 回调推送进度。
    ≤20 章时走单次生成（兼容原逻辑）。
    """
    BATCH_SIZE = 20
    if target_chapters <= BATCH_SIZE:
        if on_phase:
            on_phase("progress", {"done": 0, "total": 1, "msg": "AI 正在生成大纲…"})
        data = generate_outline(db, ai, target_chapters=target_chapters)
        if on_phase:
            on_phase("progress", {"done": 1, "total": 1, "msg": f"完成，{len(data.get('chapters',[]))} 章"})
        return data

    project = kb.get_or_create_project(db)
    chars = kb.list_characters(db)
    chars_brief = "\n".join(
        f"- {c['name']}（{c.get('role','')}）：{c.get('basic_info','')}"
        for c in chars
    ) or "（尚未定义人物）"
    threads = kb.list_threads(db)
    beats = "\n".join(
        f"- [{t.get('thread_type','')}] {t['title']}：{t.get('description','')}"
        for t in threads
    ) or "（尚未定义关键事件/伏笔）"

    # 规划分批
    batches = []
    for start in range(1, target_chapters + 1, BATCH_SIZE):
        end = min(start + BATCH_SIZE - 1, target_chapters)
        batches.append((start, end))

    all_chapters = []
    structural_notes = ""
    for i, (start, end) in enumerate(batches):
        if on_phase:
            on_phase("progress", {
                "done": i, "total": len(batches),
                "msg": f"正在生成第 {start}-{end} 章（批次 {i+1}/{len(batches)}）…",
            })
        # 已生成章节摘要（最近 8 章作为衔接上下文，避免 prompt 过长）
        prev_summary = "（首批，无前文）"
        if all_chapters:
            recent = all_chapters[-8:]
            prev_summary = "\n".join(
                f"第{ch.get('idx','?')}章《{ch.get('title','')}》:{(ch.get('summary','') or '')[:100]}"
                for ch in recent
            )
        messages = [
            {"role": "system", "content": prompts.OUTLINE_SYSTEM},
            {"role": "user", "content": _format_user(prompts.OUTLINE_BATCH_USER_TEMPLATE,
                synopsis=project.get("synopsis") or "（未填写）",
                style=project.get("style") or "（未指定）",
                pov_mode=project.get("pov_mode") or "限知视角",
                time_unit=project.get("story_time_unit") or "回",
                target_chapters=target_chapters,
                characters_brief=chars_brief,
                key_beats=beats,
                prev_summary=prev_summary,
                start=start,
                end=end,
                batch_count=end - start + 1,
            )},
        ]
        try:
            data = ai.chat_json(messages, temperature=0.7)
        except Exception as e:
            if on_phase:
                on_phase("error", f"第 {start}-{end} 章生成失败: {e}")
            break
        batch_chapters = data.get("chapters", []) if isinstance(data, dict) else []
        if batch_chapters:
            _save_outline_chapters(db, batch_chapters, chars)
            all_chapters.extend(batch_chapters)
        if isinstance(data, dict) and data.get("structural_notes") and not structural_notes:
            structural_notes = data["structural_notes"]

    if on_phase:
        on_phase("progress", {"done": len(batches), "total": len(batches),
                              "msg": f"完成，共 {len(all_chapters)} 章"})
    return {"chapters": all_chapters, "structural_notes": structural_notes,
            "count": len(all_chapters)}


# ============================================================
# 2. 章节正文生成
# ============================================================

def _format_user(p: str, **kwargs) -> str:
    """允许 prompt 模板缺字段时优雅降级。"""
    class _D(dict):
        def __missing__(self, k):
            return ""
    return p.format_map(_D(kwargs))


def generate_chapter(
    db: Database,
    ai: AIClient,
    chapter_idx: int,
    target_words: int | None = None,
    max_retries: int = 2,
    on_chunk=None,
    on_phase=None,
    research_supplement: str = "",
) -> str:
    """生成单章正文（不含一致性检查）。返回正文。

    长章节（>5000 字）采用分段续写策略：
    1. 第一段：按大纲生成前半部分
    2. 后续段：把已生成的内容作为前文，让 AI 续写直到达到目标字数
    on_chunk: 流式回调。on_phase: 阶段回调（"生成第1段"/"续写第2段"）。
    """
    chapter = kb.get_chapter_by_idx(db, chapter_idx)
    if not chapter:
        raise ValueError(f"第 {chapter_idx} 章不存在")

    ctx = retriever.build_chapter_context(db, chapter_idx, recent_window=CONFIG.writer.recent_chapter_window)
    target_words = target_words or CONFIG.writer.target_chapter_words

    # 第一段生成
    text = ""
    for attempt in range(max_retries + 1):
        user_content = _format_user(prompts.CHAPTER_USER_TEMPLATE, **ctx)
        if research_supplement:
            user_content += research_supplement  # agentic 查询补充信息
        messages = [
            {"role": "system", "content": prompts.CHAPTER_SYSTEM.format(target_words=target_words)},
            {"role": "user", "content": user_content},
        ]
        if attempt > 0:
            messages.append({
                "role": "user",
                "content": (
                    f"上一次输出仅 {len(text)} 字，远低于目标 {target_words} 字。"
                    "请充分展开描写：每个场景至少 500-800 字，包含环境、动作、对话、内心活动的交替。"
                    "不要只用概括性叙述，要写具体的场景。"
                ),
            })
        if on_phase: on_phase("generate", f"AI 正在写正文（第 1 段）…")
        if on_chunk and attempt == 0:
            text = ""
            for piece in ai.chat_stream(messages, temperature=0.85, max_tokens=CONFIG.ai.max_tokens):
                text += piece
                on_chunk(piece)
        else:
            text = ai.chat(messages, temperature=0.85, max_tokens=CONFIG.ai.max_tokens)
        text = text.strip()
        if len(text) >= min(target_words * 0.3, 2000):
            break

    # 分段续写：如果第一段不够长且目标 >5000 字，继续追加
    max_segments = 4  # 最多续写 3 次（共 4 段）
    segment = 1
    while len(text) < target_words * 0.7 and segment < max_segments:
        segment += 1
        remaining = target_words - len(text)
        if on_phase: on_phase("generate", f"AI 正在续写正文（第 {segment} 段，还需约 {remaining} 字）…")

        # 续写 prompt：把已写的内容作为前文
        cont_messages = [
            {"role": "system", "content": f"你是长篇小说作家。前文已写了 {len(text)} 字，目标是 {target_words} 字。"
             f"请紧接前文继续写，不要重复已有内容，不要写总结或结尾（除非已接近目标字数）。"
             f"继续展开新的场景、对话、冲突。"},
            {"role": "user", "content": f"【本章大纲】\n{ctx.get('outline', '')}\n\n"
             f"【已写的前文最后 {context_budget()['max_cont_preview']} 字】\n...{text[-context_budget()['max_cont_preview']:]}\n\n"
             f"请紧接上文继续写约 {min(remaining, 5000)} 字。直接输出正文，不要标题或解释。"},
        ]
        cont_text = ""
        if on_chunk:
            for piece in ai.chat_stream(cont_messages, temperature=0.85, max_tokens=CONFIG.ai.max_tokens):
                cont_text += piece
                on_chunk(piece)
        else:
            cont_text = ai.chat(cont_messages, temperature=0.85, max_tokens=CONFIG.ai.max_tokens)
        cont_text = cont_text.strip()
        if not cont_text:
            break  # AI 不再续写了
        text += "\n\n" + cont_text

    return text


# ============================================================
# 3. 摘要 + 事件抽取
# ============================================================

def summarize_chapter(db: Database, ai: AIClient, chapter_idx: int, chapter_text: str) -> str:
    chapter = kb.get_chapter_by_idx(db, chapter_idx)
    messages = [
        {"role": "system", "content": prompts.SUMMARIZE_SYSTEM},
        {"role": "user", "content": _format_user(prompts.SUMMARIZE_USER,
            chapter_text=chapter_text,
            outline=chapter.get("outline", ""),
        )},
    ]
    try:
        summary = ai.chat(messages, temperature=0.3, model=CONFIG.ai.mini_model).strip()
        return summary
    except Exception as e:
        # 降级：AI 失败时用正文开头作摘要，保证 pipeline 不中断
        # 注意：不要在摘要里放 UNFINISHED_ACTION 占位符——
        # retriever 的 prev_chapter_unfinished 正则会匹配到它，污染下一章 prompt
        fallback = (chapter_text or "").strip().replace("\n", " ")[:200]
        try:
            from .errors import log_exception
            log_exception("summarize_chapter", e)
        except Exception:
            pass
        return f"{fallback}…"


def extract_events(
    db: Database,
    ai: AIClient,
    chapter_idx: int,
    chapter_text: str,
    summary: str,
) -> list[dict]:
    chapter = kb.get_chapter_by_idx(db, chapter_idx)
    if not chapter:
        return []
    chars = kb.list_characters(db)
    chars_brief = ", ".join(c["name"] for c in chars)
    messages = [
        {"role": "system", "content": prompts.EVENT_EXTRACT_SYSTEM},
        {"role": "user", "content": _format_user(prompts.EVENT_EXTRACT_USER,
            outline=chapter.get("outline", ""),
            summary=summary,
            chapter_text=chapter_text,
            characters_brief=chars_brief,
        )},
    ]
    try:
        data = ai.chat_json(messages, temperature=0.2)
    except AICallError:
        return []
    events = data.get("events", []) if isinstance(data, dict) else []
    # 归一化（修复 bug #12：name→id 映射含别名）
    char_name_to_id = kb.build_name_to_id_map(db, include_aliases=True)
    for ev in events:
        if not isinstance(ev, dict):
            continue  # AI 偶发返回非 dict 元素（如字符串），跳过避免 TypeError
        ev["chapter_id"] = chapter["id"]
        # 映射参与人物；未登记的自动新建 minor（而非丢弃）
        ps = ev.get("participants") or []
        pids = []
        for p in ps:
            if not p or not isinstance(p, str):
                continue
            cid = char_name_to_id.get(p)
            if not cid:
                if len(p) < 2 or p in ("某", "众人", "旁人", "众人皆") or p.isdigit():
                    continue
                try:
                    cid = kb.add_character(db, p, role="minor", basic_info="（抽取自动创建）")
                    char_name_to_id[p] = cid
                except Exception:
                    continue
            if cid:
                pids.append(cid)
        ev["_participants_ids"] = pids
    return events


# ============================================================
# 全本 LLM 抽取：把已有正文直接结构化成事件 + 伏笔入库
# ============================================================

def extract_events_for_chapter(db: Database, ai: AIClient, chapter_idx: int) -> dict:
    """
    抽取单章正文 → 事件入库。
    返回 {ok, added, skipped, error, events: [...]}
    """
    chapter = kb.get_chapter_by_idx(db, chapter_idx)
    if not chapter:
        return {"ok": False, "error": f"第 {chapter_idx} 章不存在"}
    text = (chapter.get("final_text") or chapter.get("draft") or "").strip()
    if not text:
        return {"ok": False, "error": f"第 {chapter_idx} 章无正文"}

    chars = kb.list_characters(db)
    chars_brief = "、".join(c["name"] for c in chars) if chars else "（无）"

    messages = [
        {"role": "system", "content": prompts.EVENT_BATCH_SYSTEM},
        {"role": "user", "content": _format_user(prompts.EVENT_BATCH_USER,
            chapter_idx=chapter["idx"],
            title=chapter.get("title", ""),
            word_count=len(text),
            characters_brief=chars_brief,
            chapter_text=text,
        )},
    ]
    try:
        data = ai.chat_json(messages, temperature=0.2)
    except AICallError as e:
        return {"ok": False, "error": f"LLM 调用失败: {e}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "LLM 返回格式异常", "raw": str(data)[:300]}

    raw_events = data.get("events", []) or []
    if not raw_events:
        return {"ok": True, "added": 0, "skipped": 0, "events": []}

    # 检查已有事件，避免重复（按 title 相似）
    existing_titles = {e.get("title", "") for e in kb.list_events(db, chapter_id=chapter["id"])}

    # 修复 bug #12：name→id 映射含别名（旧版只用 name，别名 participants 被丢弃）
    char_name_to_id = kb.build_name_to_id_map(db, include_aliases=True)
    base_t = chapter.get("story_time_start") or chapter["idx"]
    end_t = chapter.get("story_time_end") or base_t
    span = max(0.1, float(end_t - base_t))

    added = 0
    skipped = 0
    out = []
    # 两阶段：先确定要加入的事件（保留原始序号 i 用于 AI 的 cause_event_ids 序号映射）
    to_add = []  # [(原始序号 i, ev)]
    for i, ev in enumerate(raw_events, 1):
        if not isinstance(ev, dict) or not ev.get("title"):
            skipped += 1
            continue
        title = ev["title"].strip()
        if title in existing_titles:
            skipped += 1
            continue
        to_add.append((i, ev))
    # BUG 修复：AI 的 cause_event_ids 是「本章内事件序号」(1-based)，不是 db id。
    # 先全部插入收集真实 id，再把序号重映射成 id 回填（与 extract 重抽取路径同逻辑）。
    seq_to_id: dict[int, int] = {}
    for i, ev in to_add:
        offset = float(ev.get("story_time_offset") or 0.5)
        offset = max(0.0, min(1.0, offset))
        actual_t = base_t + span * offset
        ps = ev.get("participants") or []
        pids = []
        for p in ps:
            if not p or not isinstance(p, str):
                continue
            cid = char_name_to_id.get(p)
            if not cid:
                # 未登记人物：自动新建 minor 角色（而非丢弃），让小人物也能入库
                # 跳过明显非人名的（如"某"、"众人"、纯数字、过短）
                if len(p) < 2 or p in ("某", "众人", "旁人", "众人皆") or p.isdigit():
                    continue
                try:
                    cid = kb.add_character(db, p, role="minor", basic_info="（抽取自动创建）")
                    char_name_to_id[p] = cid  # 同章后续同名复用
                    _log.info("自动创建 minor 角色：%s（第%d章事件抽取）", p, chapter_idx)
                except Exception:
                    continue
            if cid:
                pids.append(cid)
        try:
            eid = kb.add_event(
                db,
                chapter_id=chapter["id"],
                story_time=actual_t,
                sequence_in_chapter=len(kb.list_events(db, chapter_id=chapter["id"])) + 1,
                title=ev["title"].strip(),
                summary=ev.get("summary", ""),
                event_type=ev.get("event_type", "action"),
                location=ev.get("location", chapter.get("location", "")),
                cause_event_ids=[],  # 占位，下一步回填
                participants=pids,
                importance=int(ev.get("importance") or 3),
            )
            seq_to_id[i] = eid
            out.append({"id": eid, **ev, "chapter_idx": chapter_idx, "actual_story_time": actual_t})
            added += 1
        except Exception as e:
            skipped += 1
    # 回填：序号 -> 真实 db id
    for i, ev in to_add:
        if i not in seq_to_id:
            continue
        raw = ev.get("cause_event_ids") or []
        mapped = [seq_to_id[int(x)] for x in raw
                  if isinstance(x, (int, float, str)) and str(x).isdigit() and int(x) in seq_to_id]
        if mapped:
            db.execute(
                "UPDATE event SET cause_event_ids=? WHERE id=?",
                (Database.to_json(mapped), seq_to_id[i]),
            )
    # 自动回写 character.status（death/disappearance 事件 → 角色状态更新）
    status_updated = kb.apply_status_from_events(db, [ev for _, ev in to_add], char_name_to_id)
    # 自动统计出场频率 + 更新首末章节
    appearance_updated = kb.update_appearances(db, [ev for _, ev in to_add], char_name_to_id, chapter_idx)
    return {"ok": True, "added": added, "skipped": skipped, "events": out, "status_updated": status_updated, "appearances_updated": appearance_updated}


def extract_threads_for_chapter(db: Database, ai: AIClient, chapter_idx: int) -> dict:
    """
    抽取单章正文 → 伏笔入库（区分 planted / payoff / developing）。
    若识别为 payoff/developing 且能 linked 到已存在的伏笔，自动把 resolved_chapter_id 写到那条伏笔。
    """
    chapter = kb.get_chapter_by_idx(db, chapter_idx)
    if not chapter:
        return {"ok": False, "error": f"第 {chapter_idx} 章不存在"}
    text = (chapter.get("final_text") or chapter.get("draft") or "").strip()
    if not text:
        return {"ok": False, "error": f"第 {chapter_idx} 章无正文"}

    # 已有伏笔清单（提供 LLM 用于 linked_title 关联）
    existing = kb.list_threads(db)
    existing_lines = []
    for t in existing:
        existing_lines.append(
            f"  - #{t['id']} [{t.get('status','planted')}] 《{t['title']}》({t.get('description','')[:60]})"
        )
    existing_threads_str = "\n".join(existing_lines) if existing_lines else "（暂无）"

    messages = [
        {"role": "system", "content": prompts.THREAD_BATCH_SYSTEM},
        {"role": "user", "content": _format_user(prompts.THREAD_BATCH_USER,
            chapter_idx=chapter["idx"],
            title=chapter.get("title", ""),
            word_count=len(text),
            existing_threads=existing_threads_str,
            chapter_text=text,
        )},
    ]
    try:
        data = ai.chat_json(messages, temperature=0.2)
    except AICallError as e:
        return {"ok": False, "error": f"LLM 调用失败: {e}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "LLM 返回格式异常", "raw": str(data)[:300]}

    raw_threads = data.get("threads", []) or []
    if not raw_threads:
        return {"ok": True, "added": 0, "linked": 0, "threads": []}

    added = 0
    linked = 0
    out = []
    title_to_thread = {t["title"].strip(): t for t in existing}
    for th in raw_threads:
        if not isinstance(th, dict) or not th.get("title"):
            continue
        title = th["title"].strip()
        status = th.get("status", "planted")
        # 默认参数
        thread_type = th.get("thread_type", "foreshadow")
        desc = th.get("description", "")
        confidence = float(th.get("confidence") or 0.7)
        linked_title = th.get("linked_title")

        # 入库
        planted_id = chapter["id"] if status == "planted" else None
        payoff_id = chapter["id"] if status in ("payoff", "developing") else None
        try:
            tid = kb.add_thread(
                db,
                title=title,
                description=desc,
                thread_type=thread_type,
                status=status,
                planted_chapter_id=planted_id,
                payoff_chapter_id=payoff_id,  # C2 修复：新伏笔也记录 payoff 章节
                confidence=confidence,  # C1 修复：保留 AI 置信度
            )
        except Exception:
            continue
        added += 1
        thread_record = {"id": tid, "title": title, "status": status, **th}

        # 如果是 payoff/developing 且能 linked 到已有伏笔，关联
        if status in ("payoff", "developing") and linked_title:
            target = title_to_thread.get(linked_title.strip())
            if target:
                update_fields = {}
                if status == "payoff" and not target.get("resolved_chapter_id"):
                    update_fields["resolved_chapter_id"] = chapter["id"]
                    update_fields["status"] = "resolved"
                # 若已存在 payoff_chapter_id，避免覆盖
                if not target.get("payoff_chapter_id"):
                    update_fields["payoff_chapter_id"] = chapter["id"]
                if update_fields:
                    kb.update_thread(db, target["id"], **update_fields)
                    linked += 1
                    thread_record["linked_to"] = target["id"]
        out.append(thread_record)
    return {"ok": True, "added": added, "linked": linked, "threads": out}


def extract_relationships_for_chapter(db: Database, ai: AIClient, chapter_idx: int) -> dict:
    """抽取单章正文 → 人物关系 + 关系演变 + 角色里程碑，一次 AI 调用完成。

    输出三段 JSON 分别入库：
    - relationships: 按 (char_a, char_b) 双向去重，新关系 add_relationship
    - evolutions: 映射名字→id，关系不存在则先建，再 add_rel_evolution
    - milestones: 映射名字→id，add_milestone（自动推进 arc_progress）
    """
    chapter = kb.get_chapter_by_idx(db, chapter_idx)
    if not chapter:
        return {"ok": False, "error": f"第 {chapter_idx} 章不存在"}
    text = (chapter.get("final_text") or chapter.get("draft") or "").strip()
    if not text:
        return {"ok": False, "error": f"第 {chapter_idx} 章无正文"}
    ch_id = chapter["id"]

    # 角色清单（名字→id 映射 + 简要档案供 AI 匹配）
    name_to_id = kb.build_name_to_id_map(db, include_aliases=True)
    all_chars = kb.list_characters(db)
    char_brief_lines = []
    for c in all_chars:
        aliases = c.get("aliases") or []
        alias_hint = f"（别名：{'/'.join(aliases)}）" if aliases else ""
        char_brief_lines.append(f"- {c['name']}（{c.get('role', '')}）{alias_hint}：{(c.get('basic_info') or '')[:50]}")
    characters_brief = "\n".join(char_brief_lines) if char_brief_lines else "（暂无角色）"

    # 已有关系清单（供 AI 判断是新建还是更新）
    existing_rels = kb.list_relationships(db)
    id_to_name = {c["id"]: c["name"] for c in all_chars}
    rel_lines = []
    for r in existing_rels:
        a_name = id_to_name.get(r["char_a_id"], "?")
        b_name = id_to_name.get(r["char_b_id"], "?")
        rel_lines.append(f"  - #{r['id']} {a_name} ↔ {b_name}：{r['rel_type']}（{r.get('current_state', '')}）")
    existing_relationships_str = "\n".join(rel_lines) if rel_lines else "（暂无）"

    messages = [
        {"role": "system", "content": prompts.RELATIONSHIP_BATCH_SYSTEM},
        {"role": "user", "content": _format_user(prompts.RELATIONSHIP_BATCH_USER,
            chapter_idx=chapter["idx"],
            title=chapter.get("title", ""),
            word_count=len(text),
            characters_brief=characters_brief,
            existing_relationships=existing_relationships_str,
            chapter_text=text,
        )},
    ]
    try:
        data = ai.chat_json(messages, temperature=0.2)
    except AICallError as e:
        return {"ok": False, "error": f"LLM 调用失败: {e}"}
    if not isinstance(data, dict):
        return {"ok": False, "error": "LLM 返回格式异常", "raw": str(data)[:300]}

    rels_added = 0
    evos_added = 0
    miles_added = 0

    # 构建已有关系查找索引：(char_a_id, char_b_id) 和 (char_b_id, char_a_id) 双向
    rel_pair_index: dict[tuple[int, int], dict] = {}
    for r in existing_rels:
        rel_pair_index[(r["char_a_id"], r["char_b_id"])] = r
        rel_pair_index[(r["char_b_id"], r["char_a_id"])] = r

    def _resolve_pair(name_a: str, name_b: str) -> tuple[int | None, int | None]:
        """名字→id，未登记返回 None"""
        return name_to_id.get(name_a.strip()), name_to_id.get(name_b.strip())

    # --- 一、关系入库 ---
    for rel in (data.get("relationships") or []):
        if not isinstance(rel, dict):
            continue
        a_id, b_id = _resolve_pair(rel.get("char_a_name", ""), rel.get("char_b_name", ""))
        if not a_id or not b_id:
            continue  # 未登记角色，跳过（不像 events 那样自动建卡，关系需两端都有档案）
        rel_type = (rel.get("rel_type") or "秘密").strip()
        # 双向去重：已存在则只更新 current_state
        existing_r = rel_pair_index.get((a_id, b_id))
        if existing_r:
            new_state = (rel.get("current_state") or "").strip()
            if new_state and new_state != existing_r.get("current_state"):
                db.execute(
                    "UPDATE relationship SET current_state=? WHERE id=?",
                    (new_state, existing_r["id"]),
                )
            continue
        # 新关系
        try:
            new_id = kb.add_relationship(db, a_id, b_id, rel_type,
                description=rel.get("description", ""),
                current_state=rel.get("current_state", ""),
                established_chapter_id=ch_id)
            rels_added += 1
            # 更新索引（存入 id，让后续 evolutions 循环能找到而不重复建）
            new_r = {"id": new_id, "char_a_id": a_id, "char_b_id": b_id, "rel_type": rel_type}
            rel_pair_index[(a_id, b_id)] = new_r
            rel_pair_index[(b_id, a_id)] = new_r
        except Exception:
            continue

    # --- 二、关系演变入库 ---
    for evo in (data.get("evolutions") or []):
        if not isinstance(evo, dict):
            continue
        a_id, b_id = _resolve_pair(evo.get("char_a_name", ""), evo.get("char_b_name", ""))
        if not a_id or not b_id:
            continue
        # 找到关系 id，不存在则建一个 "秘密" 占位
        existing_r = rel_pair_index.get((a_id, b_id))
        if existing_r and "id" in existing_r:
            rel_id = existing_r["id"]
        else:
            try:
                rel_id = kb.add_relationship(db, a_id, b_id, "秘密",
                    established_chapter_id=ch_id)
                new_r = {"id": rel_id, "char_a_id": a_id, "char_b_id": b_id, "rel_type": "秘密"}
                rel_pair_index[(a_id, b_id)] = new_r
                rel_pair_index[(b_id, a_id)] = new_r
            except Exception:
                continue
        # 解析数值（AI 可能返回字符串）
        def _f(v, lo, hi, default=0.0):
            try:
                v = float(v)
                return max(lo, min(hi, v))
            except (TypeError, ValueError):
                return default
        try:
            kb.add_rel_evolution(db, rel_id, ch_id,
                intimacy=_f(evo.get("intimacy"), -1.0, 1.0),
                trust=_f(evo.get("trust"), -1.0, 1.0),
                conflict=_f(evo.get("conflict"), 0.0, 1.0),
                dynamics=evo.get("dynamics", ""),
                note=evo.get("note", ""))
            evos_added += 1
        except Exception:
            continue

    # --- 三、角色里程碑入库 ---
    for ms in (data.get("milestones") or []):
        if not isinstance(ms, dict):
            continue
        c_id = name_to_id.get((ms.get("character_name") or "").strip())
        if not c_id:
            continue
        ms_type = ms.get("milestone_type", "catalyst").strip()
        if ms_type not in ("starting_point", "catalyst", "crisis", "climax", "resolution", "ending"):
            ms_type = "catalyst"
        try:
            kb.add_milestone(db, c_id, ch_id,
                milestone_type=ms_type,
                description=ms.get("description", ""),
                dimension=ms.get("dimension", "personality"),
                before_state=ms.get("before_state", ""),
                after_state=ms.get("after_state", ""),
                quote=ms.get("quote", ""),
                importance=int(ms.get("importance") or 3))
            miles_added += 1
        except Exception:
            continue

    return {"ok": True, "relationships": rels_added, "evolutions": evos_added, "milestones": miles_added}


def extract_all(db: Database, ai: AIClient, only_chapters: list[int] | None = None) -> dict:
    """逐章跑 events + threads + relationships 抽取。返回汇总报告。"""
    chapters = kb.list_chapters(db)
    if only_chapters:
        chapters = [c for c in chapters if c["idx"] in only_chapters]

    report = {
        "total_chapters": len(chapters),
        "events": {"ok": 0, "failed": 0, "added": 0, "skipped": 0, "details": []},
        "threads": {"ok": 0, "failed": 0, "added": 0, "linked": 0, "details": []},
        "characters": {"ok": 0, "failed": 0, "relationships": 0, "evolutions": 0, "milestones": 0, "details": []},
    }
    for c in chapters:
        # 事件
        ev_r = extract_events_for_chapter(db, ai, c["idx"])
        if ev_r.get("ok"):
            report["events"]["ok"] += 1
            report["events"]["added"] += ev_r.get("added", 0)
            report["events"]["skipped"] += ev_r.get("skipped", 0)
        else:
            report["events"]["failed"] += 1
        report["events"]["details"].append({
            "chapter_idx": c["idx"],
            "title": c["title"],
            "added": ev_r.get("added", 0) if ev_r.get("ok") else 0,
            "error": ev_r.get("error"),
        })
        # 伏笔
        th_r = extract_threads_for_chapter(db, ai, c["idx"])
        if th_r.get("ok"):
            report["threads"]["ok"] += 1
            report["threads"]["added"] += th_r.get("added", 0)
            report["threads"]["linked"] += th_r.get("linked", 0)
        else:
            report["threads"]["failed"] += 1
        report["threads"]["details"].append({
            "chapter_idx": c["idx"],
            "title": c["title"],
            "added": th_r.get("added", 0) if th_r.get("ok") else 0,
            "linked": th_r.get("linked", 0) if th_r.get("ok") else 0,
            "error": th_r.get("error"),
        })
        # 人物关系 + 演变 + 里程碑
        dyn_r = extract_relationships_for_chapter(db, ai, c["idx"])
        if dyn_r.get("ok"):
            report["characters"]["ok"] += 1
            report["characters"]["relationships"] += dyn_r.get("relationships", 0)
            report["characters"]["evolutions"] += dyn_r.get("evolutions", 0)
            report["characters"]["milestones"] += dyn_r.get("milestones", 0)
        else:
            report["characters"]["failed"] += 1
        report["characters"]["details"].append({
            "chapter_idx": c["idx"],
            "title": c["title"],
            "relationships": dyn_r.get("relationships", 0) if dyn_r.get("ok") else 0,
            "evolutions": dyn_r.get("evolutions", 0) if dyn_r.get("ok") else 0,
            "milestones": dyn_r.get("milestones", 0) if dyn_r.get("ok") else 0,
            "error": dyn_r.get("error"),
        })
    return report


def extract_events_only(db: Database, ai: AIClient, only_chapters: list[int] | None = None) -> dict:
    """只抽 events, 不抽 threads. 给前端 /api/extract/events-all 用."""
    chapters = kb.list_chapters(db)
    if only_chapters:
        chapters = [c for c in chapters if c["idx"] in only_chapters]
    report = {"total_chapters": len(chapters), "events": {"ok": 0, "failed": 0, "added": 0, "skipped": 0, "details": []}}
    for c in chapters:
        ev_r = extract_events_for_chapter(db, ai, c["idx"])
        if ev_r.get("ok"):
            report["events"]["ok"] += 1
            report["events"]["added"] += ev_r.get("added", 0)
            if ev_r.get("skipped"):
                report["events"]["skipped"] += 1
        else:
            report["events"]["failed"] += 1
        report["events"]["details"].append({
            "chapter_idx": c["idx"],
            "title": c["title"],
            "added": ev_r.get("added", 0) if ev_r.get("ok") else 0,
            "error": ev_r.get("error"),
        })
    return report


def extract_threads_only(db: Database, ai: AIClient, only_chapters: list[int] | None = None) -> dict:
    """只抽 threads, 不抽 events. 给前端 /api/extract/threads-all 用."""
    chapters = kb.list_chapters(db)
    if only_chapters:
        chapters = [c for c in chapters if c["idx"] in only_chapters]
    report = {"total_chapters": len(chapters), "threads": {"ok": 0, "failed": 0, "added": 0, "linked": 0, "details": []}}
    for c in chapters:
        th_r = extract_threads_for_chapter(db, ai, c["idx"])
        if th_r.get("ok"):
            report["threads"]["ok"] += 1
            report["threads"]["added"] += th_r.get("added", 0)
            report["threads"]["linked"] += th_r.get("linked", 0)
        else:
            report["threads"]["failed"] += 1
        report["threads"]["details"].append({
            "chapter_idx": c["idx"],
            "title": c["title"],
            "added": th_r.get("added", 0) if th_r.get("ok") else 0,
            "linked": th_r.get("linked", 0) if th_r.get("ok") else 0,
            "error": th_r.get("error"),
        })
    return report


# ============================================================
# 4. 一致性检查
# ============================================================

def run_consistency_check(
    db: Database,
    ai: AIClient,
    chapter_idx: int,
    chapter_text: str,
) -> dict:
    ctx = retriever.build_consistency_context(db, chapter_idx, chapter_text)
    messages = [
        {"role": "system", "content": prompts.CONSISTENCY_SYSTEM},
        {"role": "user", "content": _format_user(prompts.CONSISTENCY_USER_TEMPLATE, **ctx)},
    ]
    try:
        data = ai.chat_json(messages, temperature=0.1, model=CONFIG.ai.mini_model)
        return data if isinstance(data, dict) else {"passed": False, "issues": [], "summary": "模型未返回 dict"}
    except Exception as e:
        # 编程错误（NameError/AttributeError/TypeError 等）不是"模型输出异常"，
        # 必须重新抛出，否则会把 bug 静默降级为"宽松放行"，掩盖真正的问题
        if isinstance(e, (NameError, AttributeError, TypeError, KeyError, ImportError)):
            raise
        # 一致性检查失败（如 AI 输出超长 JSON 被截断、API 超时）不应让整章正文丢失
        # 降级为"无法验证"，保证正文能落库
        return {
            "passed": True,  # 宽松放行：无法验证时不当成不通过
            "issues": [],
            "summary": f"一致性检查因模型输出异常跳过: {type(e).__name__}",
            "skipped": True,
        }


# ============================================================
# 5. 端到端流水线：生成 -> 摘要 -> 事件 -> 一致性 -> 写库
# ============================================================

def write_chapter_pipeline(
    db: Database,
    ai: AIClient,
    chapter_idx: int,
    target_words: int | None = None,
    auto_apply_facts: bool = False,
    auto_fix_retries: int | None = None,
    on_chunk=None,
    on_phase=None,
) -> dict:
    """
    完整流水线：
    1. 生成正文
    2. 摘要
    3. 事件抽取并入库
    4. 一致性检查
    5. 若失败且启用 auto_fix_retries，自动重写
    6. 写终稿、报告
    """
    auto_fix_retries = auto_fix_retries if auto_fix_retries is not None else CONFIG.writer.max_consistency_retries

    # Agentic 阶段 0：写前自主查询知识库（Hermes 风格）
    _research_supplement = ""
    try:
        ctx_preview = retriever.build_chapter_context(db, chapter_idx, recent_window=CONFIG.writer.recent_chapter_window)
        _research_supplement = _agentic_research(db, ai, chapter_idx, ctx_preview, on_phase=on_phase)
    except Exception:
        pass  # research 失败不影响写章

    if on_phase: on_phase("generate", "AI 正在写正文…")
    text = generate_chapter(db, ai, chapter_idx, target_words=target_words, on_chunk=on_chunk, on_phase=on_phase, research_supplement=_research_supplement)
    # 先把正文落库（保险）：即使后续摘要/事件/一致性步骤崩溃，正文也不丢
    _ch0 = kb.get_chapter_by_idx(db, chapter_idx)
    if _ch0:
        kb.update_chapter(db, _ch0["id"], draft=text, final_text=text, word_count=len(text))
    if on_phase: on_phase("summarize", "生成摘要…")
    summary = summarize_chapter(db, ai, chapter_idx, text)
    if on_phase: on_phase("events", "抽取事件…")
    events = extract_events(db, ai, chapter_idx, text, summary)

    # 入库事件
    chapter = kb.get_chapter_by_idx(db, chapter_idx)
    if not chapter:
        return {"error": f"第 {chapter_idx} 章在生成后意外消失", "retries": 0}
    ch_id = chapter["id"]
    ch_location = chapter.get("location", "")

    # BUG 修复：AI 返回的 cause_event_ids 是「本章内事件序号」(1-based)，不是 db id。
    # 与下方 auto-fix 重抽路径同逻辑：先占位插入收集真实 id，再重映射回填。
    # M2 修复：入库前先清旧事件，防止重写章节时事件翻倍
    db.execute("DELETE FROM event WHERE chapter_id=?", (ch_id,))
    seq_to_id: dict[int, int] = {}
    for i, ev in enumerate(events, 1):
        st = ev.get("story_time_offset")
        base_t = chapter.get("story_time_start") or 0
        t_end = chapter.get("story_time_end") or base_t
        actual_t = base_t + (t_end - base_t) * float(st or 0.5)
        new_id = kb.add_event(
            db,
            chapter_id=ch_id,
            story_time=actual_t,
            sequence_in_chapter=i,
            title=ev.get("title", f"事件{i}"),
            summary=ev.get("summary", ""),
            event_type=ev.get("event_type", "action"),
            location=ev.get("location", ch_location),
            cause_event_ids=[],  # 占位，下一步回填
            participants=ev.get("_participants_ids") or [],
            importance=int(ev.get("importance") or 3),
        )
        seq_to_id[i] = new_id
    # 回填：序号 -> 真实 db id
    for i, ev in enumerate(events, 1):
        if i not in seq_to_id:
            continue
        raw = ev.get("cause_event_ids") or []
        mapped = [seq_to_id[int(x)] for x in raw
                  if isinstance(x, (int, float, str)) and str(x).isdigit() and int(x) in seq_to_id]
        if mapped:
            db.execute(
                "UPDATE event SET cause_event_ids=? WHERE id=?",
                (Database.to_json(mapped), seq_to_id[i]),
            )

    # 自动回写 character.status + 出场频率（death/disappearance 事件 → 角色状态；所有事件 → 出场统计）
    _name_to_id = kb.build_name_to_id_map(db, include_aliases=True)
    kb.apply_status_from_events(db, events, _name_to_id)
    kb.update_appearances(db, events, _name_to_id, chapter_idx)

    # 伏笔线抽取（H修复：原 pipeline 只抽事件不抽伏笔，导致写章时伏笔系统完全失效）
    # 伏笔只在事件入库后抽取，保证 related_events 可链接
    if on_phase: on_phase("threads", "抽取伏笔线…")
    try:
        th_result = extract_threads_for_chapter(db, ai, chapter_idx)
        if on_phase:
            added = th_result.get("added", 0) if th_result.get("ok") else 0
            linked = th_result.get("linked", 0) if th_result.get("ok") else 0
            if added or linked:
                on_phase("threads", f"新增伏笔 {added} 条，关联 {linked} 条")
    except Exception:
        pass  # 伏笔抽取失败不影响写章主流程

    # 人物关系 + 演变 + 里程碑抽取（一次 AI 调用）
    if on_phase: on_phase("characters", "抽取人物动态…")
    try:
        dyn_result = extract_relationships_for_chapter(db, ai, chapter_idx)
        if on_phase and dyn_result.get("ok"):
            parts = []
            if dyn_result.get("relationships"): parts.append(f"关系{dyn_result['relationships']}")
            if dyn_result.get("evolutions"): parts.append(f"演变{dyn_result['evolutions']}")
            if dyn_result.get("milestones"): parts.append(f"里程碑{dyn_result['milestones']}")
            if parts:
                on_phase("characters", "新增 " + "，".join(parts))
    except Exception:
        pass  # 关系抽取失败不影响写章主流程

    # 一致性
    if on_phase: on_phase("consistency", "一致性检查…")
    report = run_consistency_check(db, ai, chapter_idx, text)
    issues = report.get("issues", []) or []

    # 自动尝试修复（仅对 high severity 触发）
    attempt = 0
    # H5 修复：累积每轮 consistency 检查发现的 new_facts（避免 auto-fix 覆盖丢失中途发现的事实）
    all_extracted_facts = []
    if isinstance(report, dict):
        for f in (report.get("new_facts_extracted") or []):
            all_extracted_facts.append(f)
    while attempt < auto_fix_retries and any(i.get("severity") == "high" for i in issues):
        attempt += 1
        # 在正文中标注问题位置，帮助 LLM 精确定位
        annotated_text = text
        high_issues = [i for i in issues if i.get("severity") == "high"]
        issue_annotations = []
        for i, iss in enumerate(high_issues, 1):
            loc = iss.get("location", "")
            cat = iss.get("category", "")
            expl = iss.get("explanation", "")
            fix = iss.get("fix_suggestion", "")
            issue_annotations.append(
                f"{i}. [{cat.upper()}] {expl}\n   修复方向：{fix}\n   原文位置：{loc}"
            )
        fix_prompt = (
            "请修改以下章节正文，消除所有 HIGH severity 问题。"
            "只修改涉及问题的段落，保留其他内容不变。\n\n"
            f"【原文章节正文】\n{annotated_text}\n\n"
            f"【待修复问题清单】\n" + "\n\n".join(issue_annotations) + "\n\n"
            "请输出修复后的完整章节正文（不含解释）。"
        )
        messages = [
            {"role": "system", "content": prompts.FIX_SYSTEM},
            {"role": "user", "content": fix_prompt},
        ]
        # H3 修复：auto-fix 重写失败时 break 保留当前 best text，不让异常冒泡
        try:
            text = ai.chat(messages, temperature=0.6).strip()
        except Exception:
            break  # 重写失败，保留当前 text 退出循环
        # 重新检查
        report = run_consistency_check(db, ai, chapter_idx, text)
        issues = report.get("issues", []) or []
        # H5：累积本轮发现的新事实
        if isinstance(report, dict):
            for f in (report.get("new_facts_extracted") or []):
                all_extracted_facts.append(f)

    # auto-fix 后重新生成摘要和事件（确保与最终正文一致）
    if attempt > 0:
        summary = summarize_chapter(db, ai, chapter_idx, text)
        # H2 修复：先抽取新事件，成功后再删旧事件（避免新抽崩溃导致事件净丢失）
        new_events = extract_events(db, ai, chapter_idx, text, summary)
        # 新事件抽取成功且非空，才安全删除旧事件；否则保留旧事件（避免净丢失）
        if new_events:
            db.execute("DELETE FROM event WHERE chapter_id=?", (ch_id,))
            events = new_events
        # 若 new_events 为空（LLM 失败降级），保留旧事件，events 变量沿用旧值
        # BUG 修复：AI 返回的 cause_event_ids 是「本章内的事件序号」(1-based)，
        # 不是数据库 id。先全部插入收集真实 id，再把序号重映射成 id 后回填，
        # 否则因果链图/逻辑扫描器会用错误的 id 拼边（events.find(x.id===seq) 永远找错）。
        seq_to_id: dict[int, int] = {}  # 序号(1-based) -> 真实 event.id
        for i, ev in enumerate(events, 1):
            st = ev.get("story_time_offset")
            base_t = chapter.get("story_time_start") or 0
            t_end = chapter.get("story_time_end") or base_t
            actual_t = base_t + (t_end - base_t) * float(st or 0.5)
            new_id = kb.add_event(
                db,
                chapter_id=ch_id,
                story_time=actual_t,
                sequence_in_chapter=i,
                title=ev.get("title", f"事件{i}"),
                summary=ev.get("summary", ""),
                event_type=ev.get("event_type", "action"),
                location=ev.get("location", ch_location),
                cause_event_ids=[],  # 占位，下一步回填真实 id
                participants=ev.get("_participants_ids") or [],
                importance=int(ev.get("importance") or 3),
            )
            seq_to_id[i] = new_id
        # 回填：把 AI 给的序号(1-based)映射成刚插入的真实 db id
        for i, ev in enumerate(events, 1):
            raw = ev.get("cause_event_ids") or []
            mapped = [seq_to_id[int(x)] for x in raw
                      if isinstance(x, (int, float, str)) and str(x).isdigit() and int(x) in seq_to_id]
            if mapped:
                db.execute(
                    "UPDATE event SET cause_event_ids=? WHERE id=?",
                    (Database.to_json(mapped), seq_to_id[i]),
                )

    # Agentic 阶段 10：写后自反思审查（Hermes 风格）
    try:
        text, was_revised = _agentic_reflect(db, ai, chapter_idx, text, report, on_phase=on_phase)
        if was_revised:
            # 反思修正了正文，重新更新字数
            kb.update_chapter(db, ch_id, draft=text, final_text=text, word_count=len(text))
    except Exception:
        pass  # 反思失败不影响写章结果

    # 写终稿（H4：同时写入 unfinished_action 供下一章承接）
    unfinished = (report.get("unfinished_action_at_end") or "").strip() if isinstance(report, dict) else ""
    kb.update_chapter(
        db, ch_id,
        draft=text,
        final_text=text,
        summary=summary,
        word_count=len(text),
        unfinished_action=unfinished or None,
    )

    # 持久化报告
    kb.save_consistency_report(
        db,
        chapter_id=ch_id,
        passed=bool(report.get("passed")),
        issues=issues,
        suggestions=report.get("summary", ""),
        raw_response=json.dumps(report, ensure_ascii=False),
    )

    # 抽取新事实（可选）— H5：使用 auto-fix 全程累积的 all_extracted_facts，按 content 去重
    new_facts = []
    if CONFIG.writer.enable_fact_extraction:
        seen_contents = set()
        deduped_facts = []
        for f in all_extracted_facts:
            c = (f.get("content") or "").strip()
            if c and c not in seen_contents:
                seen_contents.add(c)
                deduped_facts.append(f)
        for f in deduped_facts:
            try:
                # known_by 可能是 "public" 或角色名列表
                kb_list = f.get("known_by") or []
                if isinstance(kb_list, str):
                    kb_list = [kb_list]
                if "public" in kb_list or not kb_list:
                    known_ids: list[int] = []
                else:
                    char_map = {c["name"]: c["id"] for c in kb.list_characters(db)}
                    known_ids = [char_map[n] for n in kb_list if n in char_map]
                fid = kb.add_fact(
                    db,
                    content=f["content"],
                    category=f.get("category", "general"),
                    reliability=f.get("reliability", "reliable"),
                    known_by=known_ids,
                    established_chapter_id=ch_id,
                )
                new_facts.append({"id": fid, **f})
            except Exception:
                pass

    # 分层记忆：更新 volume synopsis（L2）和 book summary（L3）
    try:
        _update_layered_memory(db, ai, chapter_idx, ch_id, summary)
    except Exception:
        pass  # 记忆更新失败不影响写章结果

    # 缓存失效：本章改变了正文/事件/事实/伏笔/摘要/记忆，
    # 必须失效 retriever 缓存，否则下一章 build_chapter_context 在 60s 内读到旧数据
    retriever.invalidate_cache()

    return {
        "chapter_id": ch_id,
        "text": text,
        "summary": summary,
        "events": events,
        "consistency_report": report,
        "new_facts": new_facts,
        "retries": attempt,
    }


# ============================================================
# 分层记忆：更新 volume synopsis（L2）和 book summary（L3）
# ============================================================

def _update_layered_memory(db: Database, ai: AIClient, chapter_idx: int, ch_id: int, chapter_summary: str) -> None:
    """写章后更新分层记忆。
    L2: 把本章 summary 追加到当前卷 synopsis，超 800 字时压缩
    L3: 检测跨卷时，把上一卷 synopsis 并入全书摘要，超 600 字时压缩
    """
    chapter = kb.get_chapter_by_idx(db, chapter_idx)
    if not chapter:
        return
    vol_idx = chapter.get("volume_idx")
    if not vol_idx:
        # 无卷信息：用"默认卷"（vol_idx=1）存 L2，并写回 chapter.volume_idx
        vol_idx = 1
        db.execute("UPDATE chapter SET volume_idx=? WHERE id=?", (vol_idx, ch_id))
    vol = kb.get_volume_by_idx(db, vol_idx)
    if not vol:
        # 首次：建卷
        kb.add_volume(db, idx=vol_idx, title=f"第{vol_idx}卷")
        vol = kb.get_volume_by_idx(db, vol_idx)

    # L2: 更新本卷 synopsis
    vol_synopsis = (vol.get("synopsis") if vol else "") or ""
    ch_title = chapter.get("title", f"第{chapter_idx}章")
    new_line = f"第{chapter_idx}章《{ch_title}》:{(chapter_summary or '')[:150]}"
    if vol_synopsis and vol_synopsis != "（暂无）":
        vol_synopsis += f"\n{new_line}"
    else:
        vol_synopsis = new_line

    # 超 800 字时压缩（用 mini_model）
    if len(vol_synopsis) > 800:
        try:
            vol_synopsis = _compress_summary(ai, vol_synopsis, max_chars=600, desc="本卷进展")
        except Exception:
            vol_synopsis = vol_synopsis[-600:]  # 兜底：截断保留最近

    # 写回 volume.synopsis
    vol_id = kb.get_volume_by_idx(db, vol_idx)
    if vol_id:
        kb.update_volume(db, vol_id["id"], synopsis=vol_synopsis)

    # L3: 全书摘要——每章都维护（旧逻辑只在跨卷时触发，单卷小说永远没有 L3）
    # 策略：合并所有卷 synopsis → 超阈值时压缩 → 写 book_summary
    try:
        all_vols = kb.list_volumes(db) if hasattr(kb, "list_volumes") else []
        if not all_vols:
            # 兜底：至少把当前卷加进去
            all_vols = [vol] if vol else []
        vol_parts = []
        for v in all_vols:
            v_syn = (v.get("synopsis") if v else "") or ""
            if v_syn and v_syn != "（暂无）":
                vidx = v.get("idx", 1)
                vol_parts.append(f"第{vidx}卷:{v_syn}")
        combined = "\n\n".join(vol_parts).strip()
        if combined:
            # 超 600 字才压缩（避免每章都调 AI 浪费 token）
            if len(combined) > 600:
                try:
                    combined = _compress_summary(ai, combined, max_chars=500, desc="全书进展摘要")
                except Exception:
                    combined = combined[-500:]
            all_chapters = kb.list_chapters(db)
            max_idx = max((c["idx"] for c in all_chapters), default=chapter_idx)
            kb.save_book_summary(db, combined, chapter_range=f"1-{max_idx}")
    except Exception:
        pass  # L3 失败不影响写章


def _compress_summary(ai: AIClient, text: str, max_chars: int = 600, desc: str = "摘要") -> str:
    """用 mini_model 压缩摘要，保留关键剧情节点"""
    messages = [
        {"role": "system", "content": f"你是小说编辑。请把以下{desc}压缩到{max_chars}字以内，保留所有关键剧情节点、人物变化、伏笔进展，只删减重复和细节描写。直接输出压缩后的文本，不要解释。"},
        {"role": "user", "content": text},
    ]
    result = ai.chat(messages, temperature=0.3, model=CONFIG.ai.mini_model).strip()
    return result[:max_chars] if result else text[:max_chars]


# ============================================================
# Agentic Loop：写前自主查询 + 写后自反思（Hermes 风格）
# ============================================================

def _agentic_research(db, ai, chapter_idx, ctx, on_phase=None):
    """阶段 0：AI 写章前自主查询知识库。

    AI 看到大纲和上下文后，自主决定需要查哪些角色/伏笔/关系，
    查到的信息追加到 ctx 供写章 prompt 使用。
    provider 不支持 tools 时降级跳过（不影响现有流程）。
    返回追加到 ctx 的补充信息字符串（拼入 user prompt）。
    """
    if not CONFIG.writer.writer_agentic_research:
        return ""
    from . import tools as tools_mod

    if on_phase:
        on_phase("research", "AI 正在查询知识库…")

    # 构造规划 prompt：让 AI 看到大纲，决定查什么
    outline = ctx.get("outline", "")
    pov = ctx.get("pov_profile", "")[:200]
    research_prompt = (
        f"你即将写第 {chapter_idx} 章。以下是本章大纲和 POV 角色：\n\n"
        f"【大纲】{outline[:500]}\n\n"
        f"【POV 角色】{pov}\n\n"
        f"你可以调用工具查询知识库中的角色档案、伏笔状态、人物关系等，"
        f"帮你更好地理解前情和人物设定。如果已有信息足够，也可以不调工具。"
    )
    messages = [
        {"role": "system", "content": "你是小说写作助手。在动笔前，主动查询你需要的信息。"},
        {"role": "user", "content": research_prompt},
    ]

    # 工具调用循环（≤3 轮）
    research_findings = []
    max_rounds = CONFIG.writer.editor_max_tool_rounds
    for round_n in range(max_rounds):
        try:
            result = ai.chat_with_tools(
                messages, tools_mod.TOOL_DEFINITIONS,
                temperature=0.2, max_tokens=500,
            )
        except Exception:
            break  # 工具调用失败，降级跳过
        tool_calls = result.get("tool_calls") or []
        if not tool_calls:
            break  # AI 决定不需要再查
        messages.append(tools_mod.build_assistant_tool_message(tool_calls))
        for tc in tool_calls:
            tool_result = tools_mod.execute_tool(db, tc["name"], tc.get("arguments", {}))
            messages.append(tools_mod.build_tool_result_message(tc, tool_result))
            research_findings.append(f"[{tc['name']}] {tool_result[:200]}")
            if on_phase:
                on_phase("research", f"🔍 查询: {tc['name']}")

    if research_findings:
        findings_text = "\n\n【AI 自主查询补充信息】\n" + "\n".join(research_findings[:6])
        if on_phase:
            on_phase("research", f"查询完成（{len(research_findings)} 条）")
        return findings_text
    return ""


def _agentic_reflect(db, ai, chapter_idx, text, report, on_phase=None):
    """阶段 10：AI 写章后自反思审查。

    AI 审查自己的正文 + consistency 报告，自主决定是否需要修正。
    若 auto-fix 已修复所有 high 问题，则跳过。
    返回（修正后的正文, 是否修正了）。
    """
    if not CONFIG.writer.writer_agentic_reflect:
        return text, False
    # 如果 consistency 检查跳过了(skipped) 或没有 high/medium 问题，不反思
    # reflect 扩到 medium：auto-fix 只重写 high（成本高），reflect 审查 high+medium（成本低）
    issues = report.get("issues") or []
    review_issues = [i for i in issues if i.get("severity") in ("high", "medium")]
    if report.get("skipped") or not review_issues:
        return text, False  # 没问题或没检查，不反思

    if on_phase:
        on_phase("reflect", "AI 正在自审…")

    # 把 high/medium 问题和正文喂给 AI，让它判断是否需要修正
    issue_lines = []
    for i, iss in enumerate(review_issues[:8], 1):
        issue_lines.append(
            f"{i}. [{iss.get('severity','?')}/{iss.get('category','?')}] {iss.get('explanation','')}"
        )
    reflect_prompt = (
        f"你刚写完了第 {chapter_idx} 章的正文。一致性检查发现以下问题：\n\n"
        + "\n".join(issue_lines) + "\n\n"
        f"请审查你的正文，判断这些问题是否确实存在。如果确实需要修正，"
        f"请输出修正后的完整正文（只改有问题的段落，保留其他内容不变）。"
        f"如果这些问题是误报或已不存在，直接回复原正文。\n\n"
        f"【正文（末尾 {context_budget()['max_reflect_text']} 字）】\n{text[-context_budget()['max_reflect_text']:]}"
    )
    messages = [
        {"role": "system", "content": "你是小说编辑。请客观审查自己的作品，诚实地判断问题是否存在。"},
        {"role": "user", "content": reflect_prompt},
    ]
    try:
        revised = ai.chat(messages, temperature=0.4, model=CONFIG.ai.mini_model).strip()
        # 只有实质不同才采纳（防 AI 原样返回）
        if revised and revised != text and len(revised) > len(text) * 0.5:
            if on_phase:
                on_phase("reflect", "自审修正完成")
            return revised, True
    except Exception:
        pass  # 反思失败不影响结果
    if on_phase:
        on_phase("reflect", "自审完成（无需修正）")
    return text, False
