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
from .config import CONFIG
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
        messages = [
            {"role": "system", "content": prompts.CHAPTER_SYSTEM.format(target_words=target_words)},
            {"role": "user", "content": _format_user(prompts.CHAPTER_USER_TEMPLATE, **ctx)},
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
             f"【已写的前文最后 800 字】\n...{text[-800:]}\n\n"
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
        # 末尾补 UNFINISHED_ACTION 占位，让下一章承接逻辑有内容可读
        fallback = (chapter_text or "").strip().replace("\n", " ")[:200]
        log_msg = f"[summarize 降级 {type(e).__name__}] "
        try:
            from .errors import log_exception
            log_exception("summarize_chapter", e)
        except Exception:
            pass
        return f"{fallback}…\n\nUNFINISHED_ACTION：（摘要生成失败，未能提取）"


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


def extract_all(db: Database, ai: AIClient, only_chapters: list[int] | None = None) -> dict:
    """逐章跑 events + threads 抽取。返回汇总报告。"""
    chapters = kb.list_chapters(db)
    if only_chapters:
        chapters = [c for c in chapters if c["idx"] in only_chapters]

    report = {
        "total_chapters": len(chapters),
        "events": {"ok": 0, "failed": 0, "added": 0, "skipped": 0, "details": []},
        "threads": {"ok": 0, "failed": 0, "added": 0, "linked": 0, "details": []},
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
        # 一致性检查失败（如 AI 输出超长 JSON 被截断）不应让整章正文丢失
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
    if on_phase: on_phase("generate", "AI 正在写正文…")
    text = generate_chapter(db, ai, chapter_idx, target_words=target_words, on_chunk=on_chunk, on_phase=on_phase)
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

    # L3: 检测跨卷（上一章的 volume_idx 与本章不同）
    prev_ch = kb.get_prev_chapter(db, chapter_idx)
    if prev_ch and prev_ch.get("volume_idx") and prev_ch["volume_idx"] != vol_idx:
        # 跨卷：把上一卷 synopsis 并入全书摘要
        prev_vol = kb.get_volume_by_idx(db, prev_ch["volume_idx"])
        prev_vol_syn = (prev_vol.get("synopsis") if prev_vol else "") or ""
        if prev_vol_syn:
            book_sum = kb.get_book_summary(db)
            cur_book = (book_sum["summary"] if book_sum else "") or ""
            combined = f"{cur_book}\n\n第{prev_ch['volume_idx']}卷:{prev_vol_syn}".strip()
            if len(combined) > 600:
                try:
                    combined = _compress_summary(ai, combined, max_chars=500, desc="全书进展摘要")
                except Exception:
                    combined = combined[-500:]
            all_chapters = kb.list_chapters(db)
            max_idx = max((c["idx"] for c in all_chapters), default=chapter_idx)
            kb.save_book_summary(db, combined, chapter_range=f"1-{max_idx}")


def _compress_summary(ai: AIClient, text: str, max_chars: int = 600, desc: str = "摘要") -> str:
    """用 mini_model 压缩摘要，保留关键剧情节点"""
    messages = [
        {"role": "system", "content": f"你是小说编辑。请把以下{desc}压缩到{max_chars}字以内，保留所有关键剧情节点、人物变化、伏笔进展，只删减重复和细节描写。直接输出压缩后的文本，不要解释。"},
        {"role": "user", "content": text},
    ]
    result = ai.chat(messages, temperature=0.3, model=CONFIG.ai.mini_model).strip()
    return result[:max_chars] if result else text[:max_chars]
