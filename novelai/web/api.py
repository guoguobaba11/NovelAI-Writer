"""
web.api
把所有 read-only 查询聚合成 REST 端点。写入操作保留在 CLI/AI 流水线里。
"""
from __future__ import annotations
from typing import Any
from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect, Request, Path as ApiPath, Query, Body
from pathlib import Path
import re
from datetime import datetime
import json
import time
import io
import asyncio
import os

from novelai.config import CONFIG, _project_root
from novelai.db import Database
from novelai import knowledge as kb
from novelai.ai_client import AIClient, AICallError
from novelai import writer, retriever, consistency as cons_mod, prompts
from novelai.errors import err_detail, log_exception, friendly_hint
from novelai import scanner
from novelai import importer
from novelai import personality
from novelai import optimizer
from novelai import structure
from novelai import pipeline
from novelai.docx_writer import build_chapter_docx, build_book_docx
import threading
import time

router = APIRouter(prefix="/api")

# B-31: import-content 限 50MB, 防 100MB 文本 OOM (在 app.py 加 middleware, APIRouter 没这方法)


def _safe_int(v: Any, default: int = 0) -> int:
    """安全 int 转换：非数字/None 返回 default，防 ValueError→500。"""
    try:
        return int(v)
    except (TypeError, ValueError):
        return default

# 单例 DB
_DB: Database | None = None


def get_db() -> Database:
    global _DB
    if _DB is None:
        _DB = Database(CONFIG.db_path)
    return _DB


# 需要同时重置的结构分析单例
_STRUCT_ANA = None


def reset_db() -> None:
    """切换工作区后调用：重建 _DB + 清所有缓存单例，无需重启进程"""
    global _DB, _STRUCT_ANA
    _DB = None
    _STRUCT_ANA = None
    _LAST_PIPELINE.clear()
    with _PROGRESS_LOCK:
        _PROGRESS["running"] = False
        _PROGRESS["stage"] = "idle"
        _PROGRESS["log"].clear()
        _PROGRESS["last_error"] = None
    try:
        retriever.invalidate_cache()
    except Exception:
        pass

# 全局进度状态
_PROGRESS: dict[str, Any] = {
    "running": False,
    "stage": "idle",
    "chapter_idx": None,
    "log": [],
    "log_seq": 0,   # 单调递增日志序号：log 裁剪到 100 条后 WS 仍能识别新日志
    "last_update": 0.0,
    "last_error": None,   # B-23/B-28: 任务失败时记录, 前端可读
}
_PROGRESS_LOCK = threading.Lock()


def _log(stage: str, msg: str) -> None:
    with _PROGRESS_LOCK:
        _PROGRESS["stage"] = stage
        _PROGRESS["log_seq"] += 1
        _PROGRESS["log"].append({
            "seq": _PROGRESS["log_seq"],
            "t": time.time(), "stage": stage, "msg": msg,
        })
        _PROGRESS["log"] = _PROGRESS["log"][-100:]
        _PROGRESS["last_update"] = time.time()


@router.get("/progress_live")
def get_progress_live() -> dict:
    with _PROGRESS_LOCK:
        return dict(_PROGRESS)


@router.get("/progress")
def get_progress() -> dict:
    """关键 KPI 概览。"""
    db = get_db()
    chapters = kb.list_chapters(db)
    events = kb.list_events(db)
    threads = kb.list_threads(db)
    characters = kb.list_characters(db)
    total_words = sum((c.get("word_count") or 0) for c in chapters)
    written = [c for c in chapters if c.get("final_text") or c.get("draft")]
    cur_chapter = None
    if written:
        cur_chapter = max(written, key=lambda c: c.get("idx") or 0)
    cur_time = max((e.get("story_time") for e in events), default=None)
    thread_stats = {"planted": 0, "developing": 0, "payoff": 0, "resolved": 0, "abandoned": 0}
    for t in threads:
        s = t.get("status", "planted")
        thread_stats[s] = thread_stats.get(s, 0) + 1
    return {
        "total_chapters": len(chapters),
        "written_chapters": len(written),
        "total_events": len(events),
        "total_characters": len(characters),
        "total_words": total_words,
        "current_chapter": cur_chapter,
        "current_story_time": cur_time,
        "thread_stats": thread_stats,
    }


@router.get("/dashboard")
def get_dashboard() -> dict:
    """仪表盘聚合：项目概况 + 健康度 + 待办 + 最近活动 + 引导状态"""
    db = get_db()
    proj = kb.get_or_create_project(db)
    chapters = kb.list_chapters(db)
    events = kb.list_events(db)
    characters = kb.list_characters(db)
    relationships = kb.list_relationships(db)
    threads = kb.list_threads(db)
    volumes = kb.list_volumes(db)
    total_words = sum((c.get("word_count") or 0) for c in chapters)
    written = [c for c in chapters if c.get("final_text") or c.get("draft")]
    # 跑 4 个扫描
    thread_issues = scanner.scan_threads(db)
    logic_result = scanner.scan_logic(db)
    logic_total = logic_result.get("summary", {}).get("total", 0) if isinstance(logic_result.get("summary"), dict) else 0
    style_result = scanner.scan_style(db, baseline_first_n=3, z_threshold=2.0)
    style_issues_count = len(style_result.get("drift_issues", []))
    chars_with_mbti = [c for c in characters if c.get("mbti")]
    drift_results = personality.scan_personality_drift(db, chars_with_mbti)
    drift_total = sum(1 for r in drift_results if r.get("drift_signals"))
    # 总体健康度
    high_count = sum(1 for it in thread_issues if it.get("severity") == "high")
    high_count += logic_result.get("summary", {}).get("by_severity", {}).get("high", 0) if isinstance(logic_result.get("summary"), dict) else 0
    high_count += sum(1 for it in style_result.get("drift_issues", []) if it.get("severity") == "high")
    has_any_issue = (len(thread_issues) + logic_total + style_issues_count + drift_total) > 0
    if has_any_issue and high_count == 0:
        health = "yellow"
    elif high_count > 5:
        health = "red"
    elif high_count > 0:
        health = "yellow"
    else:
        health = "green"
    # 待办：open + high
    open_high = kb.list_suggestions(db, status="open")
    high_sugs = [s for s in open_high if s.get("priority") == "high"]
    high_sugs.sort(key=lambda s: s.get("id", 0), reverse=True)
    # 引导状态：只要有章节就视为"已开张"（不再卡 MBTI，避免示例项目无 MBTI 时永远进不了主界面）
    onboarding_done = len(chapters) > 0
    # 最近章节
    recent = sorted(
        [c for c in chapters if c.get("updated_at")],
        key=lambda c: c["updated_at"],
        reverse=True,
    )[:5]
    return {
        "project": {
            "title": proj.get("title", ""),
            "synopsis": proj.get("synopsis", ""),
            "style": proj.get("style", ""),
            "pov_mode": proj.get("pov_mode", ""),
            "story_time_unit": proj.get("story_time_unit", "日"),
            "volumes": len(volumes),
        },
        "kpis": {
            "chapters_total": len(chapters),
            "chapters_written": len(written),
            "words_total": total_words,
            "events": len(events),
            "characters": len(characters),
            "characters_with_mbti": len(chars_with_mbti),
            "relationships": len(relationships),
            "threads_total": len(threads),
            "volumes": len(volumes),
            "current_story_time": max((e.get("story_time") for e in events), default=None),
            "current_chapter_idx": max((c.get("idx") for c in written), default=None) if written else None,
        },
        "health": {
            "overall": health,
            "high_issues": high_count,
            "thread_issues": len(thread_issues),
            "logic_issues": logic_total,
            "style_issues": style_issues_count,
            "drift_signals": drift_total,
        },
        "todos": {
            "open_suggestions": len(open_high),
            "high_priority_suggestions": high_sugs[:5],
        },
        "recent_chapters": [
            {
                "id": c["id"],
                "idx": c["idx"],
                "title": c["title"],
                "word_count": c.get("word_count") or 0,
                "updated_at": c.get("updated_at"),
            }
            for c in recent
        ],
        "onboarding_done": onboarding_done,
    }


@router.post("/regenerate/{idx}")
def regenerate_chapter(
    idx: int = ApiPath(ge=1, description="章节号, ≥1"),
    target_words: int = Query(3000, ge=100, le=50000, description="目标字数, 100-50000"),
) -> dict:
    """异步生成 + 一致性检查 + 入库。返回 task_id。"""
    db = get_db()
    ai = AIClient()
    if not ai.ready:
        raise HTTPException(400, "AI 未配置 API key")

    # BUG 修复：在路由层（持锁）完成「检查 + 置位」，避免已有任务时仍返回 started=True
    with _PROGRESS_LOCK:
        if _PROGRESS["running"]:
            return {"started": False, "error": "已有任务在运行", "chapter_idx": idx}
        _PROGRESS["running"] = True
        _PROGRESS["chapter_idx"] = idx
        _PROGRESS["log"] = []

    def task():
        try:
            # 委托给完整管线（含 auto-fix），消除与 writer.py 的重复逻辑
            _log("starting", f"开始重写第 {idx} 章（完整管线，含 auto-fix）…")
            result = writer.write_chapter_pipeline(
                db, ai, idx, target_words=target_words, auto_fix_retries=2,
            )
            if result.get("error"):
                _log("error", f"第{idx}章重写失败：{result['error']}")
            else:
                wc = len(result.get("text", ""))
                retries = result.get("retries", 0)
                _log("done", f"完成。字数={wc}，一致性重试={retries}")
        except Exception as e:
            import traceback
            hint = friendly_hint(e)
            _log("error", err_detail('重写管线', idx=idx, e=e))
            _log("error", f"堆栈：{traceback.format_exc()[-400:]}")
            with _PROGRESS_LOCK:
                _PROGRESS["last_error"] = f"{err_detail('重写', idx=idx, e=e)} | {hint}"
        finally:
            retriever.invalidate_cache()
            with _PROGRESS_LOCK:
                _PROGRESS["running"] = False

    t = threading.Thread(target=task, daemon=True)
    t.start()
    return {"started": True, "chapter_idx": idx}


# ============== AI 辅助创作工作流（暴露 writer.py 的生成能力到 Web） ==============

# 网文文风预设模板（前端下拉选项同步）
STYLE_PRESETS = {
    "玄幻": "热血升级、金手指、境界突破、宗门争斗；节奏明快，爽点密集，章末留悬念",
    "都市": "现代背景、商战/职场/情感纠葛；对话生活化，节奏中等，贴近现实",
    "科幻": "硬科幻设定、科技冲突、文明博弈；理性冷静，逻辑严密，宏大叙事",
    "古言": "古风雅致、权谋宫斗、情感细腻；半文半白，节奏沉稳，氛围浓厚",
    "悬疑": "层层反转、线索铺排、心理博弈；节奏紧凑，伏笔密集，章末钩子",
    "自定义": "",
}


@router.post("/project/setup")
def api_project_setup(req: dict) -> dict:
    """新建/更新项目元信息（梗概/文风/视角/时间单位）—— 创作工作流的起点。

    若 req.reset=True，先清除所有章节数据（章节/事件/伏笔/关系/里程碑/版本/注释），
    让用户真正"重新开始"写一本新书（而非在旧项目上叠加）。
    """
    db = get_db()
    project = kb.get_or_create_project(db)

    # 可选：清除旧数据（写新书时）
    if req.get("reset"):
        try:
            # 按依赖顺序清除（保留 project 表 + character 表，让用户可复用人物设定）
            db.execute("DELETE FROM chapter_version")
            db.execute("DELETE FROM editor_comment")
            db.execute("DELETE FROM consistency_report")
            db.execute("DELETE FROM event")
            db.execute("DELETE FROM plot_thread")
            db.execute("DELETE FROM character_milestone")
            db.execute("DELETE FROM relationship_evolution")
            db.execute("DELETE FROM relationship")
            db.execute("DELETE FROM ai_call_log")
            db.execute("DELETE FROM optimization_suggestion")
            db.execute("DELETE FROM chapter")
            # 重置角色的出场统计
            db.execute("UPDATE character SET appearance_count=0, first_appearance_chapter=NULL, last_appearance_chapter=NULL, status=''")
            # 清除 embedding 缓存
            db.execute("DELETE FROM embedding")
            retriever.invalidate_cache()
        except Exception as e:
            pass  # 表可能不存在，忽略

    updates = {}
    for field in ("synopsis", "style", "pov_mode", "story_time_unit", "title"):
        if field in req and req[field] is not None:
            updates[field] = str(req[field]).strip()
    # 文风预设：若 style 是预设 key（如"玄幻"），展开为完整描述
    if "style" in updates and updates["style"] in STYLE_PRESETS:
        updates["style"] = STYLE_PRESETS[updates["style"]]
    if not updates:
        raise HTTPException(400, "没有要更新的字段")
    kb.update_project(db, **updates)
    retriever.invalidate_cache()
    return {"ok": True, "project": kb.get_or_create_project(db)}


@router.post("/outline/generate")
def api_outline_generate(req: dict) -> dict:
    """生成章节大纲（暴露 writer.generate_outline）。同步调用，返回完整大纲。"""
    ai = AIClient()
    if not ai.ready:
        raise HTTPException(400, "AI 未配置 API key")
    target = _safe_int(req.get("target_chapters"), 30)
    target = max(3, min(500, target))  # 3-500 章
    db = get_db()
    try:
        data = writer.generate_outline(db, ai, target_chapters=target)
        retriever.invalidate_cache()
        chapters = data.get("chapters", [])
        return {
            "ok": True,
            "chapters": chapters,
            "structural_notes": data.get("structural_notes", ""),
            "count": len(chapters),
        }
    except Exception as e:
        hint = friendly_hint(e)
        raise HTTPException(500, f"{err_detail('大纲生成', e=e)} | {hint}")


@router.post("/chapter/new")
def api_chapter_new(req: dict) -> dict:
    """新建空章节（手动建章，再调 /chapter/{idx}/write 让 AI 写）。"""
    db = get_db()
    idx = _safe_int(req.get("idx"), 0)
    if idx < 1:
        raise HTTPException(400, "idx 必须 ≥ 1")
    existing = kb.get_chapter_by_idx(db, idx)
    if existing:
        raise HTTPException(409, f"第 {idx} 章已存在")
    title = (req.get("title") or f"第{idx}章").strip()
    outline = (req.get("outline") or "").strip()
    location = (req.get("location") or "").strip()
    cid = kb.add_chapter(db, idx=idx, title=title, outline=outline, location=location)
    retriever.invalidate_cache()
    return {"ok": True, "id": cid, "idx": idx}


@router.post("/chapter/{idx}/write")
async def api_chapter_write(
    idx: int = ApiPath(ge=1, description="章节号, ≥1"),
    req: dict = Body(default_factory=dict),
):
    """一键写章——SSE 流式，用户在编辑器内实时看到 AI 正在写的正文。

    用同步 generator（非 async）避免 asyncio.to_thread 在子线程环境死锁。
    """
    from fastapi.responses import StreamingResponse
    ai = AIClient()
    if not ai.ready:
        raise HTTPException(400, "AI 未配置 API key")
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404, f"第 {idx} 章不存在，请先新建章节")

    target_words = _safe_int(req.get("target_words"), 4000)
    target_words = max(500, min(50000, target_words))
    auto_fix = req.get("auto_fix", True)

    import queue as _qmod
    import threading as _tmod

    def stream():
        """同步 generator——Starlette 线程池执行，queue.get 阻塞等待子线程推送。"""
        q: _qmod.Queue = _qmod.Queue()
        done_flag = {"error": None, "result": None}

        def on_chunk(piece):
            q.put(("chunk", piece))
        def on_phase(phase, msg):
            q.put(("phase", {"phase": phase, "msg": msg}))

        def _run():
            try:
                result = writer.write_chapter_pipeline(
                    db, ai, idx,
                    target_words=target_words,
                    auto_fix_retries=2 if auto_fix else 0,
                    on_chunk=on_chunk,
                    on_phase=on_phase,
                )
                done_flag["result"] = result
            except Exception as e:
                import traceback
                tb = traceback.format_exc()
                hint = friendly_hint(e)
                done_flag["error"] = f"{err_detail('写章', idx=idx, e=e)} | {hint}"
                _log("error", err_detail('写章管线', idx=idx, e=e))
                _log("error", f"堆栈：{tb[-400:]}")
            finally:
                retriever.invalidate_cache()
                q.put(("done", None))

        t = _tmod.Thread(target=_run, daemon=True)
        t.start()

        try:
            while True:
                kind, payload = q.get(timeout=600)
                if kind == "chunk":
                    yield f"data: {json.dumps({'chunk': payload}, ensure_ascii=False)}\n\n"
                elif kind == "phase":
                    yield f"data: {json.dumps({'phase': payload['phase'], 'msg': payload['msg']}, ensure_ascii=False)}\n\n"
                elif kind == "done":
                    if done_flag["error"]:
                        yield f"data: {json.dumps({'error': done_flag['error']}, ensure_ascii=False)}\n\n"
                    else:
                        r = done_flag["result"] or {}
                        wc = len(r.get("text", ""))
                        yield f"data: {json.dumps({'done': True, 'word_count': wc, 'retries': r.get('retries', 0)}, ensure_ascii=False)}\n\n"
                    break
        except Exception as e:
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/book/generate")
def api_book_generate(req: dict) -> dict:
    """批量生成多章正文（循环调 write_chapter_pipeline）。异步任务 + 进度推送。"""
    ai = AIClient()
    if not ai.ready:
        raise HTTPException(400, "AI 未配置 API key")
    db = get_db()
    from_idx = _safe_int(req.get("from_idx"), 1)
    to_idx = _safe_int(req.get("to_idx"), from_idx)
    target_words = _safe_int(req.get("target_words"), 4000)
    if to_idx < from_idx:
        raise HTTPException(400, "to_idx 必须 >= from_idx")

    with _PROGRESS_LOCK:
        if _PROGRESS["running"]:
            return {"started": False, "error": "已有任务在运行"}
        _PROGRESS["running"] = True
        _PROGRESS["chapter_idx"] = from_idx
        _PROGRESS["log"] = []

    def task():
        try:
            total = to_idx - from_idx + 1
            for i, idx in enumerate(range(from_idx, to_idx + 1), 1):
                with _PROGRESS_LOCK:
                    _PROGRESS["chapter_idx"] = idx
                _log("batch_progress", f"批量生成 {i}/{total}：第 {idx} 章…")
                chapter = kb.get_chapter_by_idx(db, idx)
                if not chapter:
                    _log("skip", f"第 {idx} 章不存在（无大纲），跳过")
                    continue
                try:
                    result = writer.write_chapter_pipeline(
                        db, ai, idx, target_words=target_words, auto_fix_retries=2,
                    )
                    wc = len(result.get("text", "")) if not result.get("error") else 0
                    _log("chapter_done", f"第 {idx} 章完成（{wc} 字）")
                except Exception as ce:
                    hint = friendly_hint(ce)
                    _log("error", f"{err_detail('批量写章', idx=idx, e=ce)} | {hint}")
                    continue  # 单章失败不中断批量
            _log("done", f"批量生成完成（{from_idx}-{to_idx}）")
        except Exception as e:
            import traceback
            _log("error", err_detail('批量生成整体', e=e))
            _log("error", f"堆栈：{traceback.format_exc()[-400:]}")
        finally:
            retriever.invalidate_cache()
            with _PROGRESS_LOCK:
                _PROGRESS["running"] = False

    t = threading.Thread(target=task, daemon=True)
    t.start()
    return {"started": True, "from_idx": from_idx, "to_idx": to_idx}


@router.post("/hard_check/{idx}")
def hard_check(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> dict:
    db = get_db()
    ch = kb.get_chapter_by_idx(db, idx)
    if not ch or not (ch.get("final_text") or ch.get("draft")):
        raise HTTPException(404, "章节无正文")
    text = ch.get("final_text") or ch.get("draft")
    # B-新72: hard_check 内部已 try/except, 任何子查函数挂返 [] (不会 500)
    return {"issues": cons_mod.hard_check(db, idx, text)}


# ============== 扫描器 ==============

@router.get("/scan/threads")
def api_scan_threads() -> dict:
    return {"issues": scanner.scan_threads(get_db())}


@router.get("/scan/logic")
def api_scan_logic() -> dict:
    return scanner.scan_logic(get_db())


@router.get("/scan/style")
def api_scan_style(baseline: int = Query(3, ge=1, le=100, description="基线前N章, 1-100"), threshold: float = Query(2.0, ge=0.1, le=10.0, description="z阈值, 0.1-10")) -> dict:
    return scanner.scan_style(get_db(), baseline_first_n=baseline, z_threshold=threshold)


@router.get("/recent_issues")
def get_recent_issues(limit: int = Query(10, ge=1, le=500, description="返回条数, 1-500")) -> list[dict]:
    db = get_db()
    rows = db.query("SELECT * FROM consistency_report ORDER BY id DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        d = dict(r)
        try:
            d["issues"] = json.loads(d.get("issues") or "[]")
        except Exception:
            d["issues"] = []
        ch = kb.get_chapter(db, d["chapter_id"])
        d["chapter_idx"] = ch["idx"] if ch else None
        d["chapter_title"] = ch["title"] if ch else None
        out.append(d)  # B-08: ch=None 已用三元保护, 不会 NPE
    return out


@router.get("/volumes")
def get_volumes() -> list[dict]:
    return kb.list_volumes(get_db())


# ============== 基础列表（前端必需） ==============

@router.get("/chapters")
def get_chapters_list() -> list[dict]:
    # B-新112: 不返 final_text/draft (50MB×40=2GB JSON), 端点列表只返元信息; 正文走 /editor/chapter/{idx}
    rows = kb.list_chapters(get_db())
    for r in rows:
        r.pop("final_text", None)
        r.pop("draft", None)
    return rows


@router.get("/characters")
def get_characters_list() -> list[dict]:
    return kb.list_characters(get_db())


# ============== MBTI / 人物维度 ==============

@router.get("/character_matrix")
def api_character_matrix() -> dict:
    chars = [c for c in kb.list_characters(get_db()) if c.get("mbti")]
    return personality.build_character_matrix(chars)


@router.get("/character_arcs")
def api_character_arcs() -> dict:
    """返回所有人物的成长线 + 里程碑"""
    db = get_db()
    out = []
    for c in kb.list_characters(db):
        ms = kb.list_milestones(db, character_id=c["id"])
        out.append({
            "id": c["id"],
            "name": c["name"],
            "mbti": c.get("mbti", ""),
            "arc_type": c.get("arc_type", ""),
            "arc_progress": c.get("arc_progress") or 0.0,
            "milestones": ms,
        })
    return {"characters": out}


@router.post("/character/add")
def api_add_character(req: dict) -> dict:
    """添加人物到知识库（修复死表单问题：之前只显示 CLI 命令不入库）。"""
    db = get_db()
    name = (req.get("name") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    role = (req.get("role") or "supporting").strip()
    basic_info = (req.get("basic_info") or "").strip()
    personality = (req.get("personality") or "").strip()
    mbti = (req.get("mbti") or "").strip().upper() or None
    cid = kb.add_character(db, name, role=role, basic_info=basic_info or None,
                           personality=personality or None, mbti=mbti)
    retriever.invalidate_cache()  # 人物变更后生成/校验应立刻用新数据
    return {"ok": True, "id": cid, "name": name}


@router.post("/character/set_mbti")
def api_set_mbti(req: dict) -> dict:
    name = req.get("name")
    mbti = (req.get("mbti") or "").upper()
    if not name or not mbti:
        raise HTTPException(400, "name and mbti required")
    c = kb.find_character_by_name(get_db(), name)
    if not c:
        raise HTTPException(404, f"character not found: {name}")
    if mbti not in personality.MBTI_STACK:
        raise HTTPException(400, f"unknown MBTI: {mbti}")
    stack = personality.get_stack(mbti)
    kws = personality.mbti_to_keywords(mbti)
    kb.update_character(
        get_db(), c["id"],
        mbti=mbti,
        cognitive_stack="-".join(stack),
        baseline_keywords=kws,
    )
    retriever.invalidate_cache()
    return {"ok": True, "stack": stack, "keywords_count": len(kws)}


@router.post("/character/set_status")
def api_set_status(req: dict) -> dict:
    """手动设置人物 status（活/已死/失踪/重伤），供用户修正 AI 抽取的错误状态。"""
    name = req.get("name")
    status = (req.get("status") or "").strip()
    if not name:
        raise HTTPException(400, "name required")
    db = get_db()
    c = kb.find_character_by_name(db, name)
    if not c:
        raise HTTPException(404, f"character not found: {name}")
    kb.update_character(db, c["id"], status=status)
    retriever.invalidate_cache()
    return {"ok": True, "name": name, "status": status}


@router.get("/character/{char_id}/profile")
def api_character_profile(char_id: int = ApiPath(ge=1, description="人物 ID")) -> dict:
    """人物小传聚合端点：基础档案 + 事件时间线 + 里程碑 + 关系演变 + 相关伏笔。

    一次请求返回小传所需的全部数据，避免前端多次往返。
    """
    db = get_db()
    c = kb.get_character(db, char_id)
    if not c:
        raise HTTPException(404, f"character {char_id} not found")
    chapters = {ch["id"]: ch for ch in kb.list_chapters(db)}

    # 事件时间线（该人物参与的事件，按 story_time 排序）
    events = []
    for ev in kb.list_events_by_character(db, char_id):
        ch = chapters.get(ev.get("chapter_id"))
        events.append({
            "id": ev["id"],
            "chapter_idx": ch["idx"] if ch else None,
            "chapter_title": ch.get("title", "") if ch else "",
            "story_time": ev.get("story_time"),
            "event_type": ev.get("event_type", "action"),
            "title": ev.get("title", ""),
            "summary": ev.get("summary", ""),
            "importance": ev.get("importance", 3),
        })

    # 成长里程碑
    milestones = []
    for m in kb.list_milestones(db, character_id=char_id):
        ch = chapters.get(m.get("chapter_id"))
        milestones.append({
            "chapter_idx": ch["idx"] if ch else None,
            "milestone_type": m.get("milestone_type", ""),
            "description": m.get("description", ""),
        })

    # 关系演变（性能优化：用 WHERE 过滤而非全表扫描 + Python 过滤）
    relationships = []
    my_rels = kb.list_relationships_by_character(db, char_id)
    # 只为需要的 other 角色查名字（避免全量 list_characters）
    other_ids = {r["char_b_id"] if r["char_a_id"] == char_id else r["char_a_id"] for r in my_rels}
    name_map = {}
    for oid in other_ids:
        oc = kb.get_character(db, oid)
        if oc:
            name_map[oid] = oc["name"]
    for r in my_rels:
        other_id = r["char_b_id"] if r["char_a_id"] == char_id else r["char_a_id"]
        evos = kb.list_rel_evolution(db, relationship_id=r["id"])
        latest = evos[-1] if evos else None
        relationships.append({
            "other_name": name_map.get(other_id, "?"),
            "rel_type": r.get("rel_type", ""),
            "current_state": r.get("current_state", ""),
            "intimacy": latest.get("intimacy") if latest else None,
            "trust": latest.get("trust") if latest else None,
            "conflict": latest.get("conflict") if latest else None,
            "evolution_points": len(evos),
        })

    # 相关伏笔
    threads = []
    for t in kb.list_threads(db):
        rel_chars = t.get("related_characters") or []
        if char_id in rel_chars:
            threads.append({
                "thread_type": t.get("thread_type", ""),
                "status": t.get("status", ""),
                "title": t.get("title", ""),
                "description": t.get("description", ""),
            })

    return {
        "character": c,
        "events": events,
        "milestones": milestones,
        "relationships": relationships,
        "threads": threads,
    }


@router.post("/character/add_milestone")
def api_add_milestone(req: dict) -> dict:
    db = get_db()
    name = req.get("name")
    chapter_idx = req.get("chapter_idx")
    mtype = req.get("milestone_type")
    desc = req.get("description")
    if not all([name, chapter_idx is not None, mtype, desc]):
        raise HTTPException(400, "name, chapter_idx, milestone_type, description required")
    # B-新121: 强校验 chapter_idx 必为 int ≥ 1, 防 int("abc") 抛 500
    if not isinstance(chapter_idx, int) or chapter_idx < 1:
        raise HTTPException(422, f"chapter_idx 必须 ≥1, 收到 {chapter_idx!r}")
    char = kb.find_character_by_name(db, name)
    if not char:
        raise HTTPException(404, f"character not found: {name}")
    ch = kb.get_chapter_by_idx(db, chapter_idx)
    if not ch:
        raise HTTPException(404, f"chapter not found: {chapter_idx}")
    mid = kb.add_milestone(
        db, character_id=char["id"], chapter_id=ch["id"],
        milestone_type=mtype, description=desc,
        dimension=req.get("dimension", "personality"),
        before_state=req.get("before_state", ""),
        after_state=req.get("after_state", ""),
        quote=req.get("quote", ""),
        importance=_safe_int(req.get("importance", 3), 3),
    )
    # 自动推进 arc_progress
    cur = char.get("arc_progress") or 0.0
    new_prog = min(1.0, cur + 0.1)
    kb.update_character(db, char["id"], arc_progress=new_prog)
    return {"ok": True, "id": mid, "arc_progress": new_prog}


@router.delete("/character/{char_id}")
def api_delete_character(char_id: int = ApiPath(ge=1, description="人物ID, ≥1")) -> dict:
    """删除人物 + 级联清理关联数据（milestone/relationship/chapter.pov/event.participants/fact.known_by）。
    需前端二次确认（不可恢复）。"""
    db = get_db()
    ch = kb.get_character(db, char_id)
    if not ch:
        raise HTTPException(404, f"人物 id={char_id} 不存在")
    ok = kb.delete_character(db, char_id)
    if not ok:
        raise HTTPException(500, "删除失败")
    addLog("info", f"[char] 已删除人物「{ch['name']}」(id={char_id}) 及其关联数据")
    return {"ok": True, "deleted": char_id, "name": ch["name"]}


@router.post("/relationship/add_evolution")
def api_add_rel_evol(req: dict) -> dict:
    db = get_db()
    a = req.get("a")
    b = req.get("b")
    ch_idx = req.get("chapter_idx")
    if not all([a, b, ch_idx is not None]):
        raise HTTPException(400, "a, b, chapter_idx required")
    # B-新122: 强校验 ch_idx
    if not isinstance(ch_idx, int) or ch_idx < 1:
        raise HTTPException(422, f"chapter_idx 必须 ≥1, 收到 {ch_idx!r}")
    ca = kb.find_character_by_name(db, a)
    cb = kb.find_character_by_name(db, b)
    if not ca or not cb:
        raise HTTPException(404, "character not found")
    ch = kb.get_chapter_by_idx(db, int(ch_idx))
    if not ch:
        raise HTTPException(404, f"chapter not found: {ch_idx}")
    rels = kb.get_relationships_for(db, ca["id"])
    target = next(
        (r for r in rels if r["char_a_id"] == cb["id"] or r["char_b_id"] == cb["id"]),
        None,
    )
    if not target:
        rid = kb.add_relationship(db, ca["id"], cb["id"], "未分类", description="自动创建")
        target = kb.get_relationship(db, rid)
    rev_id = kb.add_rel_evolution(
        db, relationship_id=target["id"], chapter_id=ch["id"],
        intimacy=req.get("intimacy"), trust=req.get("trust"),
        conflict=req.get("conflict"), dynamics=req.get("dynamics", ""),
    )
    return {"ok": True, "id": rev_id}


@router.get("/relationship_evolution")
def api_relationship_evolution() -> dict:
    """返回所有关系演变时间序列，供前端绘曲线"""
    db = get_db()
    out = []
    char_by_id = {c["id"]: c for c in kb.list_characters(db)}
    for r in kb.list_relationships(db):
        evols = kb.list_rel_evolution(db, relationship_id=r["id"])
        a = char_by_id.get(r["char_a_id"])
        b = char_by_id.get(r["char_b_id"])
        if not a or not b:
            continue
        out.append({
            "relationship_id": r["id"],
            "a": a["name"],
            "b": b["name"],
            "rel_type": r.get("rel_type", ""),
            "current_state": r.get("current_state", ""),
            "evolutions": [
                {
                    "chapter_id": e["chapter_id"],
                    "intimacy": e.get("intimacy"),
                    "trust": e.get("trust"),
                    "conflict": e.get("conflict"),
                    "dynamics": e.get("dynamics", ""),
                } for e in evols
            ],
        })
    return {"series": out}


@router.get("/personality_drift")
def api_personality_drift(window: int | None = Query(None, ge=1, le=50, description="窗口, 1-50")) -> dict:
    """性格漂移分析（不依赖 LLM）"""
    db = get_db()
    chars = [c for c in kb.list_characters(db) if c.get("mbti")]
    results = personality.scan_personality_drift(db, chars, chapter_window=window)
    return {"results": results}


# ============== LLM 优化建议 ==============

@router.get("/suggestions")
def api_list_suggestions(target_type: str | None = None, status: str | None = None) -> list[dict]:
    return kb.list_suggestions(get_db(), target_type=target_type, status=status)


@router.post("/suggestion/apply/{sid}")
def api_apply_suggestion(sid: int = ApiPath(ge=1, description="建议ID, ≥1")) -> dict:
    kb.update_suggestion_status(get_db(), sid, "applied")
    return {"ok": True}


@router.post("/suggestion/dismiss/{sid}")
def api_dismiss_suggestion(sid: int = ApiPath(ge=1, description="建议ID, ≥1")) -> dict:
    kb.update_suggestion_status(get_db(), sid, "dismissed")
    return {"ok": True}


def _ai_error(e: Exception) -> dict:
    """AI 调用失败的统一降级响应（替代 500，前端可友好展示）。"""
    msg = str(e) or type(e).__name__
    addLog("error", f"[ai] {msg[:200]}")
    return {"ok": False, "error": msg[:500]}


@router.post("/optimize/personality")
def api_optimize_personality(req: dict) -> dict:
    name = req.get("name")
    if not name:
        raise HTTPException(400, "name required")
    try:
        opt = optimizer.Optimizer(get_db(), _require_ai())
        sugs = opt.optimize_personality(name)
        return {"ok": True, "count": len(sugs), "suggestions": sugs}
    except HTTPException:
        raise
    except Exception as e:
        return _ai_error(e)


@router.post("/optimize/arc")
def api_optimize_arc(req: dict) -> dict:
    name = req.get("name")
    if not name:
        raise HTTPException(400, "name required")
    try:
        opt = optimizer.Optimizer(get_db(), _require_ai())
        sugs = opt.optimize_arc(name)
        return {"ok": True, "count": len(sugs), "suggestions": sugs}
    except HTTPException:
        raise
    except Exception as e:
        return _ai_error(e)


@router.post("/optimize/relationship")
def api_optimize_relationship(req: dict) -> dict:
    a, b = req.get("a"), req.get("b")
    if not a or not b:
        raise HTTPException(400, "a and b required")
    try:
        opt = optimizer.Optimizer(get_db(), _require_ai())
        sugs = opt.optimize_relationship(a, b)
        return {"ok": True, "count": len(sugs), "suggestions": sugs}
    except HTTPException:
        raise
    except Exception as e:
        return _ai_error(e)


def _require_ai() -> AIClient:
    ai = AIClient()
    if not ai.ready:
        raise HTTPException(400, "AI 未配置 API key。请在 .env 中设置 NOVELAI_API_KEY")
    return ai


@router.post("/optimize/all")
def api_optimize_all() -> dict:
    try:
        opt = optimizer.Optimizer(get_db(), _require_ai())
        sugs = opt.optimize_all()
        return {"ok": True, "count": len(sugs), "suggestions": sugs}
    except HTTPException:
        raise
    except Exception as e:
        return _ai_error(e)


# ============== LLM 抽取（事件 / 伏笔） ==============

@router.post("/extract/events/{idx}")
def api_extract_events(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> dict:
    try:
        return writer.extract_events_for_chapter(get_db(), _require_ai(), idx)
    except HTTPException:
        raise
    except Exception as e:
        return _ai_error(e)


@router.post("/extract/threads/{idx}")
def api_extract_threads(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> dict:
    try:
        return writer.extract_threads_for_chapter(get_db(), _require_ai(), idx)
    except HTTPException:
        raise
    except Exception as e:
        return _ai_error(e)


@router.post("/extract/all")
def api_extract_all() -> dict:
    try:
        return writer.extract_all(get_db(), _require_ai())
    except HTTPException:
        raise
    except Exception as e:
        return _ai_error(e)


@router.post("/extract/events-all")
def api_extract_events_all() -> dict:
    try:
        return writer.extract_events_only(get_db(), _require_ai())
    except HTTPException:
        raise
    except Exception as e:
        return _ai_error(e)


@router.post("/extract/threads-all")
def api_extract_threads_all() -> dict:
    try:
        return writer.extract_threads_only(get_db(), _require_ai())
    except HTTPException:
        raise
    except Exception as e:
        return _ai_error(e)


# ============== 叙事结构分析 ==============

def _get_struct() -> structure.StructureAnalyzer:
    global _STRUCT_ANA
    if _STRUCT_ANA is None:
        _STRUCT_ANA = structure.StructureAnalyzer(get_db())
    return _STRUCT_ANA


@router.get("/structure/full")
def api_structure_full() -> dict:
    return _get_struct().analyze_full()


@router.get("/structure/volume/{idx}")
def api_structure_volume(idx: int = ApiPath(ge=1, description="卷号, ≥1")) -> dict:
    return _get_struct().analyze_volume(idx)


@router.get("/structure/chapter/{idx}")
def api_structure_chapter(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> dict:
    return _get_struct().analyze_chapter(idx)


@router.get("/structure/summary")
def api_structure_summary() -> dict:
    return _get_struct().full_issues_summary()


# ============== 可视化视图直接查询端点 (B-新131) ==============
# 之前前端调 /api/events /threads /timeline /rhythm /relationship_network 一直 404,
# 因为只有 /api/structure/full 返回大数据; 这次补齐 5 个细粒度端点供 ECharts 直接拉.

@router.get("/events")
def api_list_events() -> list[dict]:
    """全部事件列表 (供事件因果链 / 事件-伏笔视图)"""
    return kb.list_events(get_db())


@router.get("/threads")
def api_list_threads() -> list[dict]:
    """全部伏笔列表 (供伏笔列表视图)"""
    return kb.list_threads(get_db())


@router.get("/timeline")
def api_timeline() -> dict:
    """时间线: chapter_ranges (条形图章范围) + event_points (散点图事件)
    字段名/数据结构对齐前端 ECharts renderTimeline / renderChain 期望.
    """
    db = get_db()
    chapters = kb.list_chapters(db)
    events = kb.list_events(db)
    # 章范围: [start_time, end_time, y_pos=3, ...]
    chapter_ranges = []
    for c in chapters:
        s = c.get("story_time_start")
        e = c.get("story_time_end")
        if s is None or e is None:
            continue
        chapter_ranges.append({
            "name": f"第{c['idx']}章 {c.get('title','')}",
            "value": [float(s), float(e), 0, 3],  # y_pos=3 (章节)
        })
    # 事件点: [time, y_pos=1, importance, importance, event_type]
    event_points = []
    for ev in events:
        t = ev.get("story_time")
        if t is None:
            continue
        ch = kb.get_chapter(db, ev["chapter_id"])
        if not ch:
            continue
        event_points.append({
            "name": ev.get("title", ""),
            "value": [
                float(t),         # x: 时间
                1,                # y: 事件层
                0,                # z: 不重要
                ev.get("importance") or 3,  # 控制符号大小
                ev.get("event_type") or "action",  # 控制颜色
                ch.get("idx"),    # chapter idx (备用)
            ],
        })
    return {
        "chapter_ranges": chapter_ranges,
        "event_points": event_points,
    }


@router.get("/rhythm")
def api_rhythm() -> dict:
    """节奏曲线: 每章字数 / 事件数 / 事件平均重要度 / 新增伏笔 / 未解决伏笔 / 一致性 high
    字段名对齐前端 ECharts renderRhythm 期望.
    """
    db = get_db()
    chapters = kb.list_chapters(db)
    all_events = kb.list_events(db)
    all_threads = kb.list_threads(db)
    # 一致性 high: 查每章最近一条 consistency_report.issues 中 severity=high 的数量
    idx_list = []
    words = []
    event_count = []
    event_importance_avg = []
    threads_new = []
    threads_unresolved = []
    consistency_high = []
    for c in chapters:
        idx_list.append(c["idx"])
        text = c.get("final_text") or c.get("draft") or ""
        words.append(len(text))
        evs = [e for e in all_events if e["chapter_id"] == c["id"]]
        event_count.append(len(evs))
        if evs:
            avg = sum((e.get("importance") or 3) for e in evs) / len(evs)
            event_importance_avg.append(round(avg, 2))
        else:
            event_importance_avg.append(0)
        # 本章新种伏笔
        threads_new.append(sum(1 for t in all_threads if t.get("planted_chapter_id") == c["id"]))
        # 截至本章未解决的伏笔 (planted/developing, planted 在本章及之前)
        unresolved = sum(
            1 for t in all_threads
            if t.get("status") in ("planted", "developing")
            and t.get("planted_chapter_id")
            and kb.get_chapter(db, t["planted_chapter_id"])
            and kb.get_chapter(db, t["planted_chapter_id"])["idx"] <= c["idx"]
        )
        threads_unresolved.append(unresolved)
        # 本章一致性 high (查最新报告)
        cr = db.query_one(
            "SELECT issues FROM consistency_report WHERE chapter_id=? ORDER BY id DESC LIMIT 1",
            (c["id"],),
        )
        high_n = 0
        if cr:
            try:
                issues = json.loads(cr["issues"] or "[]")
                high_n = sum(1 for it in issues if it.get("severity") == "high")
            except Exception:
                pass
        consistency_high.append(high_n)
    return {
        "idx": idx_list,
        "words": words,
        "event_count": event_count,
        "event_importance_avg": event_importance_avg,
        "threads_new": threads_new,
        "threads_unresolved": threads_unresolved,
        "consistency_high": consistency_high,
    }


@router.get("/relationship_network")
def api_relationship_network() -> dict:
    """人物关系网: nodes (含 category/role) + edges (source/target + 关系强度数据)。

    边携带最新演变的 intimacy/trust/conflict，供前端做视觉编码（借鉴 StoryForge）：
    - 边宽度 = |intimacy|、边颜色 = trust、conflict 高用虚线、强关系加流动光点。
    """
    db = get_db()
    characters = kb.list_characters(db)
    relationships = kb.list_relationships(db)
    char_by_id = {c["id"]: c for c in characters}
    # 节点：按 role 分类 + symbolSize 反映出场频率（借鉴 StoryForge 的 importance 编码）
    nodes = [
        {
            "id": c["id"],
            "name": c["name"],
            "category": c.get("role") or "supporting",
            "symbolSize": 25 + min(40, (c.get("appearance_count") or 0) * 3) + (len(c.get("aliases") or []) * 3),
            "status": c.get("status") or "",
        }
        for c in characters
    ]
    edges = []
    for r in relationships:
        if r.get("char_a_id") in char_by_id and r.get("char_b_id") in char_by_id:
            # 取最新一条演变的数值（若有）—— 借鉴 StoryForge 的 strength 驱动，我们三维更强
            evos = kb.list_rel_evolution(db, relationship_id=r["id"])
            latest = evos[-1] if evos else None
            edges.append({
                "source": char_by_id[r["char_a_id"]]["name"],
                "target": char_by_id[r["char_b_id"]]["name"],
                "rel_type": r.get("rel_type", ""),
                "description": r.get("description", ""),
                "intimacy": latest.get("intimacy") if latest else None,
                "trust": latest.get("trust") if latest else None,
                "conflict": latest.get("conflict") if latest else None,
                "has_evolution": len(evos) > 0,
            })
    return {"nodes": nodes, "edges": edges}


@router.get("/knowledge_graph")
def api_knowledge_graph() -> dict:
    """统一知识图谱：5 类节点（人物/事件/伏笔/事实/世界观）+ 6 种边。

    返回 ECharts graph 格式 {nodes, links, categories}，带前缀唯一 id 防重名。
    """
    db = get_db()
    nodes = []
    links = []

    # --- 节点 ---
    # 人物
    chars = kb.list_characters(db)
    char_ids = set()
    for c in chars:
        char_ids.add(c["id"])
        app_count = c.get("appearance_count") or 0
        nodes.append({
            "id": f"char_{c['id']}",
            "name": c["name"],
            "nodeType": "character",
            "category": "人物",
            "symbolSize": 25 + min(30, app_count * 3),
            "info": f"{c.get('role','?')} · {c.get('mbti') or '?'} · 出场{app_count}次 · {c.get('status') or '活'}",
        })

    # 事件
    events = kb.list_events(db)
    event_id_map = {}  # db_id -> echarts_id
    for ev in events:
        eid = f"event_{ev['id']}"
        event_id_map[ev["id"]] = eid
        imp = ev.get("importance") or 3
        nodes.append({
            "id": eid,
            "name": ev.get("title", "")[:12],
            "nodeType": "event",
            "category": "事件",
            "symbolSize": 12 + imp * 4,
            "info": f"[{ev.get('event_type','action')}] {ev.get('title','')} — {ev.get('summary','')[:40]}",
        })

    # 伏笔
    threads = kb.list_threads(db)
    for t in threads:
        nodes.append({
            "id": f"thread_{t['id']}",
            "name": t.get("title", "")[:12],
            "nodeType": "thread",
            "category": "伏笔",
            "symbolSize": 16,
            "info": f"[{t.get('thread_type','?')}|{t.get('status','?')}] {t.get('title','')} — {t.get('description','')[:40]}",
        })

    # 事实
    facts = kb.list_facts(db)
    for f in facts:
        nodes.append({
            "id": f"fact_{f['id']}",
            "name": (f.get("content", "")[:10] + "…"),
            "nodeType": "fact",
            "category": "事实",
            "symbolSize": 14,
            "info": f"[{f.get('reliability','?')}] {f.get('content','')[:50]}",
        })

    # 世界观
    worlds = kb.list_world(db)
    for w in worlds:
        nodes.append({
            "id": f"world_{w['id']}",
            "name": w.get("name", "")[:12],
            "nodeType": "world",
            "category": "世界观",
            "symbolSize": 18,
            "info": f"[{w.get('category','?')}] {w.get('name','')}: {w.get('content','')[:40]}",
        })

    # --- 边 ---
    def _link(src, tgt, edge_type):
        links.append({"source": src, "target": tgt, "edgeType": edge_type})

    # 人物↔人物（关系）
    for r in kb.list_relationships(db):
        if r["char_a_id"] in char_ids and r["char_b_id"] in char_ids:
            _link(f"char_{r['char_a_id']}", f"char_{r['char_b_id']}", "relationship")

    # 人物↔事件（参与）+ 事件↔事件（因果）
    for ev in events:
        ev_id = f"event_{ev['id']}"
        for pid in (ev.get("participants") or []):
            if pid in char_ids:
                _link(f"char_{pid}", ev_id, "participates")
        for cause_id in (ev.get("cause_event_ids") or []):
            if cause_id in event_id_map:
                _link(event_id_map[cause_id], ev_id, "causes")

    # 人物↔伏笔 + 事件↔伏笔
    for t in threads:
        tid = f"thread_{t['id']}"
        for cid in (t.get("related_characters") or []):
            if cid in char_ids:
                _link(f"char_{cid}", tid, "thread_char")
        for eid in (t.get("related_events") or []):
            if eid in event_id_map:
                _link(event_id_map[eid], tid, "thread_event")

    # 人物↔事实（知情）
    for f in facts:
        fid = f"fact_{f['id']}"
        for cid in (f.get("known_by") or []):
            if cid in char_ids:
                _link(f"char_{cid}", fid, "knows")

    categories = [
        {"name": "人物"},
        {"name": "事件"},
        {"name": "伏笔"},
        {"name": "事实"},
        {"name": "世界观"},
    ]

    return {"nodes": nodes, "links": links, "categories": categories,
            "counts": {"characters": len(chars), "events": len(events),
                       "threads": len(threads), "facts": len(facts), "worlds": len(worlds),
                       "links": len(links)}}


@router.get("/character_retention")
def api_character_retention() -> dict:
    """记忆衰减检测（借鉴 StoryForge 的 RetentionReport）。

    返回每个角色的出场频率 + 距最后出场的章节数 + 优先级：
    - normal: 最近出场
    - warning: 超过 8 章未出场
    - forgotten: 超过 15 章未出场
    """
    db = get_db()
    chapters = kb.list_chapters(db)
    max_idx = max((ch["idx"] for ch in chapters), default=0)
    out = []
    for c in kb.list_characters(db):
        last = c.get("last_appearance_chapter")
        app_count = c.get("appearance_count") or 0
        if last is None:
            gap = max_idx  # 从未出场：按距第一章算
        else:
            gap = max_idx - last
        # 优先级判定（主角/反派更宽容）
        role = c.get("role", "supporting")
        warn_threshold = 12 if role in ("protagonist", "antagonist") else 8
        forgotten_threshold = 25 if role in ("protagonist", "antagonist") else 15
        if app_count == 0:
            priority = "forgotten" if max_idx > 3 else "new"
        elif gap > forgotten_threshold:
            priority = "forgotten"
        elif gap > warn_threshold:
            priority = "warning"
        else:
            priority = "normal"
        if priority != "normal":  # 只返回需关注的
            out.append({
                "id": c["id"],
                "name": c["name"],
                "role": role,
                "appearance_count": app_count,
                "last_appearance_chapter": last,
                "chapters_since_last": gap,
                "priority": priority,
            })
    # 按严重度排序
    order = {"forgotten": 0, "warning": 1, "new": 2}
    out.sort(key=lambda x: (order.get(x["priority"], 9), -x["chapters_since_last"]))
    return {"characters": out, "total_chapters": max_idx}


@router.post("/optimize/structure")
def api_optimize_structure(req: dict) -> dict:
    level = req.get("level", "full")
    # B-新120: level 强校验, 防任意字符串走通到 LLM 调 (白花钱)
    if level not in ("full", "volume", "chapter"):
        raise HTTPException(400, f"level 必须是 full/volume/chapter, 收到 {level!r}")
    idx = req.get("idx")
    if level in ("volume", "chapter") and idx is not None:
        if not isinstance(idx, int) or idx < 1:
            raise HTTPException(422, f"idx 必须 ≥1, 收到 {idx!r}")
    try:
        opt = optimizer.Optimizer(get_db(), AIClient())
        sugs = opt.optimize_structure(level, idx)
        return {"ok": True, "count": len(sugs), "suggestions": sugs}
    except HTTPException:
        raise
    except Exception as e:
        return _ai_error(e)


# ============== 修改流水线 ==============

@router.get("/pipeline/quick")
def api_pipeline_quick() -> dict:
    try:
        return pipeline.run_quick_pipeline(get_db())
    except Exception as e:
        return _ai_error(e)


# 最近一次流水线结果（保存在内存）
_LAST_PIPELINE: dict[str, Any] = {}


@router.post("/pipeline/full")
def api_pipeline_full() -> dict:
    """异步跑完整流水线；用 _PROGRESS 推进度"""
    global _LAST_PIPELINE
    # 防并发：已在运行则拒绝新请求
    with _PROGRESS_LOCK:
        if _PROGRESS["running"]:
            raise HTTPException(409, "已有任务在运行，请等待完成")
        _PROGRESS["running"] = True
    db = get_db()
    ai = AIClient()

    def task():
        global _LAST_PIPELINE
        _log("pipeline", "开始完整流水线…")
        def cb(stage, msg):
            _log(stage, msg)
        try:
            t0 = time.time()
            r = pipeline.run_full_pipeline(db, ai, progress_cb=cb)
            r["elapsed_total_seconds_full"] = time.time() - t0
            _LAST_PIPELINE = r
            _log("pipeline", f"完成：{r['summary']['roadmap_items']} 项路线图 / {r['summary']['llm_suggestions']} 条 LLM 建议")
        except Exception as e:
            _log("error", f"流水线失败: {e}")
            with _PROGRESS_LOCK:
                _PROGRESS["last_error"] = f"流水线: {e}"
        finally:
            with _PROGRESS_LOCK:
                _PROGRESS["running"] = False

    t = threading.Thread(target=task, daemon=True)
    t.start()
    return {"started": True}


@router.get("/pipeline/last")
def api_pipeline_last() -> dict:
    return _LAST_PIPELINE if _LAST_PIPELINE else {"empty": True}


@router.get("/pipeline/roadmap")
def api_pipeline_roadmap(limit: int = Query(30, ge=1, le=500, description="返回条数, 1-500")) -> dict:
    """基于最近一次 quick + 现有 LLM 建议（已入库）生成路线图"""
    global _LAST_PIPELINE
    if not _LAST_PIPELINE or "quick" not in _LAST_PIPELINE:
        return {"error": "请先跑 quick 或 full 流水线"}
    open_sugs = kb.list_suggestions(get_db(), status="open")
    roadmap = pipeline.build_roadmap(_LAST_PIPELINE["quick"], llm_suggestions=open_sugs)
    return {"roadmap": roadmap[:limit], "total": len(roadmap)}


@router.get("/ai-stats")
def api_ai_stats() -> dict:
    """AI 调用统计：今日 + 全部两份快照。

    返回 {today: {...}, all: {...}, recent: [...]}
    每份快照含 {calls, prompt_tokens, completion_tokens, total_tokens, avg_latency_ms, by_endpoint}。
    """
    db = get_db()
    import time as _t
    import datetime as _dt
    # 今日 = 当地时间当天 0 点起的 ts
    today_start = _t.mktime(_dt.date.today().timetuple())
    return {
        "today": kb.ai_call_stats(db, since_ts=today_start),
        "all": kb.ai_call_stats(db),
        "recent": kb.recent_ai_calls(db, limit=10),
    }


# ============== 编辑器（3 栏 + 底栏）==============

@router.get("/editor/chapter/{idx}")
def api_editor_chapter(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> dict:
    """编辑器需要的全量章节信息"""
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404, f"第 {idx} 章不存在")
    events = kb.list_events(db, chapter_id=chapter["id"])
    threads = kb.list_threads(db)
    ch_thread_rels = [
        t for t in threads
        if t.get("planted_chapter_id") == chapter["id"]
        or t.get("resolved_chapter_id") == chapter["id"]
        or t.get("payoff_chapter_id") == chapter["id"]
    ]
    consistency_row = db.query_one(
        "SELECT * FROM consistency_report WHERE chapter_id=? ORDER BY id DESC LIMIT 1",
        (chapter["id"],),
    )
    consistency_data = None
    if consistency_row:
        try:
            issues = json.loads(consistency_row["issues"] or "[]")
        except Exception:
            issues = []
        consistency_data = {
            "passed": bool(consistency_row["passed"]),
            "issues": issues,
            "summary": consistency_row["suggestions"] or "",
            "created_at": consistency_row["created_at"],
        }
    characters = kb.list_characters(db)
    all_chapters = kb.list_chapters(db)
    prev_idx = next_idx = None
    for i, c in enumerate(all_chapters):
        if c["idx"] == idx:
            if i > 0:
                prev_idx = all_chapters[i - 1]["idx"]
            if i < len(all_chapters) - 1:
                next_idx = all_chapters[i + 1]["idx"]
            break
    return {
        "chapter": chapter,
        "events": events,
        "threads": ch_thread_rels,
        "consistency": consistency_data,
        "characters": characters,
        "prev_idx": prev_idx,
        "next_idx": next_idx,
        "text": (chapter.get("final_text") or chapter.get("draft") or ""),
        # 编辑器侧栏用：本章要点（POV + 事件涉及角色 + 关键事件清单）
        "outline": _build_chapter_outline(chapter, events, characters),
    }


def _build_chapter_outline(chapter: dict, events: list, characters: list) -> dict:
    """组装编辑侧栏用的'本章要点'：POV 角色 + 事件参与角色 + 关键事件"""
    char_by_id = {c["id"]: c for c in characters}

    # 涉及角色：POV ∪ 所有事件的 participants
    related_char_ids = set()
    if chapter.get("pov_character_id"):
        related_char_ids.add(chapter["pov_character_id"])
    for e in events:
        for pid in (e.get("participants") or []):
            related_char_ids.add(pid)
    related_chars = [char_by_id[i] for i in related_char_ids if i in char_by_id]

    # 关键事件（按 importance 降序 + 出现顺序）
    sorted_events = sorted(events, key=lambda e: (-(e.get("importance") or 0), e.get("sequence_in_chapter") or 0))

    # 卷信息
    volume = None
    if chapter.get("volume_idx"):
        vol = kb.get_volume_by_idx(get_db(), chapter["volume_idx"])
        if vol:
            volume = {"idx": vol.get("idx"), "title": vol.get("title")}

    return {
        "outline": chapter.get("outline") or "",
        "summary": chapter.get("summary") or "",
        "location": chapter.get("location") or "",
        "pov_character": char_by_id.get(chapter.get("pov_character_id")) if chapter.get("pov_character_id") else None,
        "related_characters": related_chars,
        "key_events": sorted_events,
        "volume": volume,
    }


@router.post("/editor/chapter/{idx}/save")
def api_editor_save(idx: int = ApiPath(ge=1, description="章节号, ≥1"), req: dict = Body(default_factory=dict)) -> dict:
    """保存编辑器修改
    v1.19.26: 允许 text="" (用户清空正文), 拒绝对应 req 字段缺失 (None / 未传)
    版本树: 保存后自动建一版 (source=save), 与上一版内容相同则跳过
    """
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404)
    if "text" not in req:
        raise HTTPException(400, "text field required")
    text = req.get("text") or ""  # None / 缺失 视为空字符串, 不再 400
    summary = req.get("summary", "")
    kb.update_chapter(
        db, chapter["id"],
        final_text=text, draft=text, word_count=len(text),
        summary=summary if summary else chapter.get("summary", ""),
    )
    # 版本树：与上一版内容不同才建版（避免重复保存产生冗余版本）
    new_vid = None
    try:
        latest = kb.get_latest_chapter_version(db, chapter["id"])
        prev_text = kb.get_chapter_version_full_text(db, latest["id"]) if latest else ""
        if text != prev_text:
            new_vid = kb.add_chapter_version(
                db, chapter["id"], text,
                source="save",
                label=f"💾 保存（{len(text)} 字）",
            )
    except Exception as e:
        _log("version", f"save 建版失败（不影响保存）: {e}")
    retriever.invalidate_cache()  # 正文已变，避免后续生成/校验用 ≤60s 旧缓存
    return {"ok": True, "word_count": len(text), "version_id": new_vid}


@router.post("/editor/chapter/{idx}/reindex")
def api_editor_reindex(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> dict:
    """手动编辑后重新抽取：事件 + 伏笔 + 摘要 + 更新分层记忆。
    让 AI 的记忆库与编辑后的正文保持同步。
    """
    ai = _require_ai()
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404, "章节不存在")
    text = (chapter.get("final_text") or chapter.get("draft") or "").strip()
    if not text:
        raise HTTPException(400, "章节无正文，无法抽取")
    ch_id = chapter["id"]
    results = {"events": 0, "threads": 0, "summary_updated": False, "memory_updated": False}

    # 1. 重抽摘要
    try:
        summary = writer.summarize_chapter(db, ai, idx, text)
        kb.update_chapter(db, ch_id, summary=summary)
        results["summary_updated"] = True
    except Exception as e:
        log_exception("reindex-summarize", e)

    # 2. 重抽事件（先删旧再抽新）
    try:
        db.execute("DELETE FROM event WHERE chapter_id=?", (ch_id,))
        events = writer.extract_events(db, ai, idx, text, summary)
        seq_to_id = {}
        for i, ev in enumerate(events, 1):
            if not isinstance(ev, dict):
                continue
            st = ev.get("story_time_offset")
            base_t = chapter.get("story_time_start") or 0
            t_end = chapter.get("story_time_end") or base_t
            actual_t = base_t + (t_end - base_t) * float(st or 0.5)
            new_id = kb.add_event(
                db, chapter_id=ch_id, story_time=actual_t, sequence_in_chapter=i,
                title=ev.get("title", f"事件{i}"), summary=ev.get("summary", ""),
                event_type=ev.get("event_type", "action"),
                location=ev.get("location", chapter.get("location", "")),
                cause_event_ids=[], participants=ev.get("_participants_ids") or [],
                importance=int(ev.get("importance") or 3),
            )
            seq_to_id[i] = new_id
        # 回填因果链
        for i, ev in enumerate(events, 1):
            if not isinstance(ev, dict) or i not in seq_to_id:
                continue
            raw = ev.get("cause_event_ids") or []
            mapped = [seq_to_id[int(x)] for x in raw
                      if isinstance(x, (int, float, str)) and str(x).isdigit() and int(x) in seq_to_id]
            if mapped:
                db.execute("UPDATE event SET cause_event_ids=? WHERE id=?", (Database.to_json(mapped), seq_to_id[i]))
        results["events"] = len(events)
    except Exception as e:
        log_exception("reindex-events", e)

    # 3. 重抽伏笔
    try:
        thread_result = writer.extract_threads_for_chapter(db, ai, idx)
        results["threads"] = thread_result.get("added", 0) + thread_result.get("linked", 0)
    except Exception as e:
        log_exception("reindex-threads", e)

    # 4. 更新分层记忆
    try:
        writer._update_layered_memory(db, ai, idx, ch_id, summary)
        results["memory_updated"] = True
    except Exception as e:
        log_exception("reindex-memory", e)

    retriever.invalidate_cache()
    results["ok"] = True
    return results


@router.delete("/chapter/{idx}")
def api_delete_chapter(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> dict:
    """删除章节 + 级联清理（event/report/comment/version/milestone/rel_evolution），
    plot_thread 的章节引用置 NULL（保留伏笔本身）。需前端二次确认。"""
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404, f"第 {idx} 章不存在")
    title = chapter.get("title", "")
    ok = kb.delete_chapter(db, chapter["id"])
    if not ok:
        raise HTTPException(500, "删除失败")
    addLog("info", f"[chapter] 已删除第 {idx} 章「{title}」及其关联数据")
    return {"ok": True, "deleted_idx": idx, "title": title}


# ============== 章节版本树（增量 patch 持久化） ==============

@router.get("/editor/chapter/{idx}/versions")
def api_list_versions(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> list[dict]:
    """列出某章所有版本（最新在前）。不返 patch 正文，列表页用元数据即可。"""
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404, f"第 {idx} 章不存在")
    return kb.list_chapter_versions(db, chapter["id"])


@router.get("/editor/chapter/{idx}/versions/{vid}")
def api_get_version(idx: int = ApiPath(ge=1), vid: int = ApiPath(ge=1)) -> dict:
    """取单版完整正文（后端沿 patch 链重建，前端零成本）。"""
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404, f"第 {idx} 章不存在")
    v = kb.get_chapter_version(db, vid)
    if not v or v["chapter_id"] != chapter["id"]:
        raise HTTPException(404, f"版本 {vid} 不属于第 {idx} 章")
    text = kb.get_chapter_version_full_text(db, vid)
    return {
        "id": v["id"],
        "seq": v["seq"],
        "chapter_idx": idx,
        "source": v["source"],
        "label": v.get("label"),
        "name": v.get("name"),
        "word_count": v["word_count"],
        "accept_count": v.get("accept_count", 0),
        "reject_count": v.get("reject_count", 0),
        "created_at": v["created_at"],
        "text": text,
    }


@router.post("/editor/chapter/{idx}/versions")
def api_create_named_version(idx: int = ApiPath(ge=1), req: dict = Body(default_factory=dict)) -> dict:
    """建一个版本（命名存档或语义动作点存档）。

    body: {
      name?: str,            # 命名版的名字（命名版必填）
      source?: str,          # save|ai|replace|insert|named（默认 named）
      label?: str,           # 显示标签
      current_text?: str,    # 本版正文（必填，否则用数据库正文）
      accept_count?: int,
      reject_count?: int,
    }
    与上一版内容相同则跳过建版（返回 version_id=null, skipped=true）。
    返回 {ok, version_id, skipped}
    """
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404, f"第 {idx} 章不存在")
    source = (req.get("source") or "named").strip()
    if source not in ("save", "ai", "replace", "insert", "named"):
        raise HTTPException(400, f"invalid source: {source}")
    name = (req.get("name") or "").strip()
    if source == "named" and not name:
        raise HTTPException(400, "name required for named version")
    # 优先用前端传的当前编辑器正文；否则用数据库 final_text/draft
    text = req.get("current_text")
    if text is None:
        text = chapter.get("final_text") or chapter.get("draft") or ""
    # 与上一版相同则跳过（避免重复）
    try:
        latest = kb.get_latest_chapter_version(db, chapter["id"])
        prev_text = kb.get_chapter_version_full_text(db, latest["id"]) if latest else ""
        if text == prev_text:
            return {"ok": True, "version_id": None, "skipped": True}
    except Exception:
        pass
    label = req.get("label") or (f"⭐ {name}" if source == "named" else source)
    vid = kb.add_chapter_version(
        db, chapter["id"], text,
        source=source,
        label=label,
        name=name if source == "named" else None,
        accept_count=int(req.get("accept_count") or 0),
        reject_count=int(req.get("reject_count") or 0),
    )
    return {"ok": True, "version_id": vid, "skipped": False}


@router.delete("/editor/chapter/{idx}/versions/{vid}")
def api_delete_version(idx: int = ApiPath(ge=1), vid: int = ApiPath(ge=1)) -> dict:
    """删单版（child 重接到 parent，链不断；基线版 seq=0 拒绝删除）。"""
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404, f"第 {idx} 章不存在")
    v = kb.get_chapter_version(db, vid)
    if not v or v["chapter_id"] != chapter["id"]:
        raise HTTPException(404, f"版本 {vid} 不属于第 {idx} 章")
    if v["seq"] == 0:
        raise HTTPException(400, "基线版不可删除")
    ok = kb.delete_chapter_version(db, vid)
    return {"ok": ok}


@router.post("/editor/chapter/{idx}/versions/clear")
def api_clear_versions(idx: int = ApiPath(ge=1), req: dict = Body(default_factory=dict)) -> dict:
    """清空该章历史版本。keep_named=True 时保留命名版（默认）；否则全清非基线版。"""
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404, f"第 {idx} 章不存在")
    keep_named = req.get("keep_named", True)
    versions = kb.list_chapter_versions(db, chapter["id"], limit=10000)
    deleted = 0
    for v in versions:
        if v["seq"] == 0:
            continue  # 基线版永远留
        if keep_named and v["source"] == "named":
            continue
        if kb.delete_chapter_version(db, v["id"]):
            deleted += 1
    return {"ok": True, "deleted": deleted}


@router.post("/editor/chapter/{idx}/analyze")
def api_editor_analyze(idx: int = ApiPath(ge=1, description="章节号, ≥1"), req: dict = Body(default_factory=dict)) -> dict:
    """基于当前文本跑一次硬规则分析"""
    from novelai import consistency as cons_mod
    db = get_db()
    text = req.get("text", "")
    if not text:
        raise HTTPException(400, "text required")
    # 临时覆盖章节 final_text 跑硬规则
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404)
    try:
        issues = cons_mod.hard_check(db, idx, text)
    except Exception as e:
        return _ai_error(e)
    return {
        "ok": True,
        "issues": issues,
        "n_issues": len(issues),
        "by_severity": {
            "high": sum(1 for it in issues if it.get("severity") == "high"),
            "medium": sum(1 for it in issues if it.get("severity") == "medium"),
            "low": sum(1 for it in issues if it.get("severity") == "low"),
        },
    }


# ============== 每日编辑简报 (Phase 1) ==============
# 打开编辑器时一次性返三类信息: 硬关联 (上一章/本章/人物/伏笔) + LLM 编辑建议 (3-5条) + 自动疑点 (规则引擎)
# 缓存 10 分钟, 避免快速切章反复打 LLM

_DAILY_BRIEF_CACHE: dict[int, tuple[float, dict]] = {}
_DAILY_BRIEF_TTL = 600  # 10 分钟

import re as _re_daily


def _build_hard_context(db: Database, chapter: dict) -> dict:
    """硬关联: 不调 LLM, 纯数据驱动"""
    # 上一章结尾未完成动作
    unfinished = None
    prev = kb.get_prev_chapter(db, chapter["idx"])  # idx 可能跳号，取实际上一章
    if prev and prev.get("summary"):
        m = _re_daily.search(r"UNFINISHED_ACTION[:：]\s*(.+)", prev["summary"])
        if m:
            unfinished = m.group(1).strip()[:300]

    # 本章时间线位置
    all_chapters = kb.list_chapters(db)
    n = len(all_chapters)
    ch_pos = {c["idx"]: i + 1 for i, c in enumerate(all_chapters)}
    my_pos = ch_pos.get(chapter["idx"], 0)
    pos_pct = round(my_pos / max(1, n) * 100, 1)
    my_t_start = chapter.get("story_time_start")
    my_t_end = chapter.get("story_time_end")

    # 本章涉及人物 + 各自最近 3 回状态
    events = kb.list_events(db, chapter_id=chapter["id"])
    related_char_ids = set()
    if chapter.get("pov_character_id"):
        related_char_ids.add(chapter["pov_character_id"])
    for e in events:
        for pid in (e.get("participants") or []):
            related_char_ids.add(pid)
    characters = kb.list_characters(db)
    char_by_id = {c["id"]: c for c in characters}
    related_chars = []
    for cid in related_char_ids:
        c = char_by_id.get(cid)
        if not c:
            continue
        # 该角色最近 3 次出现的章节
        recent = []
        for cc in all_chapters:
            if cc["idx"] == chapter["idx"]:
                continue
            if cc["idx"] > chapter["idx"]:
                break
            cc_events = kb.list_events(db, chapter_id=cc["id"])
            for ev in cc_events:
                if cid in (ev.get("participants") or []):
                    recent.append({
                        "chapter_idx": cc["idx"],
                        "title": cc.get("title", ""),
                        "event_title": ev.get("title", ""),
                    })
        recent = recent[-3:]
        related_chars.append({
            "id": cid,
            "name": c["name"],
            "role": c.get("role", ""),
            "mbti": c.get("mbti", ""),
            "recent_appearances": recent,
        })

    # 本章关联伏笔
    threads = kb.list_threads(db)
    ch_thread_rels = [
        t for t in threads
        if t.get("planted_chapter_id") == chapter["id"]
        or t.get("resolved_chapter_id") == chapter["id"]
        or t.get("payoff_chapter_id") == chapter["id"]
    ]

    return {
        "prev_unfinished_action": unfinished,
        "timeline_position": {
            "chapter_idx": chapter["idx"],
            "position": my_pos,
            "total": n,
            "pct": pos_pct,
            "story_time_range": [my_t_start, my_t_end] if my_t_start is not None else None,
        },
        "related_characters": related_chars,
        "related_threads": [
            {
                "id": t["id"],
                "title": t.get("title", ""),
                "status": t.get("status", ""),
                "thread_type": t.get("thread_type", ""),
                "description": (t.get("description") or "")[:120],
                "relation": (
                    "planted" if t.get("planted_chapter_id") == chapter["id"]
                    else "payoff" if t.get("payoff_chapter_id") == chapter["id"] or t.get("resolved_chapter_id") == chapter["id"]
                    else "other"
                ),
            }
            for t in ch_thread_rels
        ],
    }


def _build_daily_brief_llm(db: Database, chapter: dict, hard_context: dict, issues: list[dict]) -> list[dict]:
    """LLM 编辑建议 (3-5 条)
    失败 / 未配 key → 返空 list, 不阻塞前端
    """
    from novelai import prompts
    try:
        ai = AIClient()
        if not ai.ready:
            return []
    except Exception:
        return []

    char_brief = "\n".join(
        f"- {c['name']}（{c.get('role','')}/{c.get('mbti','')}）" for c in hard_context["related_characters"]
    ) or "（无）"
    thread_brief = "\n".join(
        f"- [{t['status']}] {t['title']}：{t['description']}" for t in hard_context["related_threads"]
    ) or "（无）"
    issue_brief = "\n".join(
        f"- [{it.get('severity','?')}] {it.get('category','?')}: {it.get('explanation','')[:120]}"
        for it in issues[:8]
    ) or "（无）"

    system = (
        "你是一位长篇小说资深编辑，正在帮工作室编辑准备每日的'本章编辑简报'。"
        "任务：基于用户提供的硬关联上下文（上一章未完成动作、本章位置、涉及人物、关联伏笔、自动疑点），"
        "给出 3-5 条**可操作的编辑建议**。\n"
        "要求：\n"
        "1. 每条 ≤ 60 字, 一句话点出'做什么'+'为什么'\n"
        "2. 优先针对疑点（信息泄漏/时间矛盾/未登记人名）\n"
        "3. 不要重复硬关联已经给的内容, 给的是'硬关联看不到的'角度（如节奏、人物内心、对白密度）\n"
        "4. 输出 JSON 数组, 每项 {type, suggestion}, type ∈ {pacing|character|dialogue|consistency|其他}"
    )
    user = (
        f"## 上一章未完成动作\n{hard_context['prev_unfinished_action'] or '（无）'}\n\n"
        f"## 本章时间线位置\n第{chapter['idx']}回 / 共{hard_context['timeline_position']['total']}回 ({hard_context['timeline_position']['pct']}%)\n"
        f"故事内时间: {hard_context['timeline_position']['story_time_range']}\n\n"
        f"## 涉及人物\n{char_brief}\n\n"
        f"## 关联伏笔\n{thread_brief}\n\n"
        f"## 自动疑点 (规则引擎已扫)\n{issue_brief}\n\n"
        f"## 本章标题 / 字数\n{chapter.get('title','')} / {chapter.get('word_count') or 0} 字\n\n"
        "请给 3-5 条编辑建议 (JSON 数组):"
    )
    try:
        data = ai.chat_json(
            [{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.4,
        )
        if isinstance(data, list):
            return [it for it in data if isinstance(it, dict) and it.get("suggestion")]
        if isinstance(data, dict) and isinstance(data.get("suggestions"), list):
            return [it for it in data["suggestions"] if isinstance(it, dict) and it.get("suggestion")]
        return []
    except Exception as e:
        # B-新131: LLM 失败不能阻塞简报
        return [{"type": "_meta", "suggestion": f"(LLM 建议暂不可用: {type(e).__name__})"}]


def _build_daily_brief_issues(db: Database, chapter: dict, text: str) -> list[dict]:
    """自动疑点: 复用 consistency.hard_check (info_leak / 未登记人名 / 时间矛盾 / 风格违例)
    已经做了 try/except 隔离, 任何子查挂都不影响整体
    """
    from novelai import consistency as cons_mod
    if not text.strip():
        return []
    issues = cons_mod.hard_check(db, chapter["idx"], text)
    # 加 style-rule 违例 (单章节文本级)
    try:
        rules = kb.list_style_rules(db, enabled_only=True)
        violations = _check_style_rules(text, rules)
        for v in violations:
            issues.append({
                "severity": v.get("severity", "low"),
                "category": "style",
                "location": v.get("location", ""),
                "explanation": v.get("explanation", ""),
                "fix_suggestion": v.get("fix_suggestion", ""),
            })
    except Exception:
        pass
    return issues


@router.get("/editor/chapter/{idx}/daily-brief")
def api_editor_daily_brief(
    idx: int = ApiPath(ge=1, description="章节号, ≥1"),
    refresh: int = Query(0, description="1=强制刷新, 跳过缓存"),
) -> dict:
    """每日编辑简报: 进入编辑器时一次性返三类信息
    - hard_context: 硬关联 (无 LLM, 即时)
    - llm_suggestions: LLM 编辑建议 (3-5 条, 5-10s)
    - issues: 自动疑点 (规则引擎, < 500ms)
    缓存 10 分钟; ?refresh=1 跳过缓存.
    """
    import time as _t
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404, f"第 {idx} 章不存在")

    # 缓存命中 (不强制刷新)
    now = _t.time()
    if not refresh:
        cached = _DAILY_BRIEF_CACHE.get(idx)
        if cached and (now - cached[0]) < _DAILY_BRIEF_TTL:
            payload = dict(cached[1])
            payload["cached"] = True
            payload["cache_age_seconds"] = int(now - cached[0])
            return payload

    text = (chapter.get("final_text") or chapter.get("draft") or "")
    t0 = _t.time()
    hard_context = _build_hard_context(db, chapter)
    t_hard = round((_t.time() - t0) * 1000)

    t0 = _t.time()
    issues = _build_daily_brief_issues(db, chapter, text)
    t_issues = round((_t.time() - t0) * 1000)

    t0 = _t.time()
    llm_suggestions = _build_daily_brief_llm(db, chapter, hard_context, issues)
    t_llm = round((_t.time() - t0) * 1000)

    payload = {
        "chapter_idx": idx,
        "chapter_title": chapter.get("title", ""),
        "hard_context": hard_context,
        "llm_suggestions": llm_suggestions,
        "issues": {
            "items": issues,
            "n_total": len(issues),
            "by_severity": {
                "high": sum(1 for it in issues if it.get("severity") == "high"),
                "medium": sum(1 for it in issues if it.get("severity") == "medium"),
                "low": sum(1 for it in issues if it.get("severity") == "low"),
            },
        },
        "elapsed_ms": {
            "hard": t_hard,
            "issues": t_issues,
            "llm": t_llm,
        },
        "cached": False,
    }
    # 写缓存
    _DAILY_BRIEF_CACHE[idx] = (now, dict(payload))
    return payload


def _buf_splice(parent: str, start: int, end: int, new_segment: str) -> str:
    """把 parent 的 [start, end) 区间替换为 new_segment。
    用于 inline 模式：AI 输出的只是选区新版本，需拼回整章才能跑硬校验。
    """
    if start < 0:
        start = 0
    if end > len(parent):
        end = len(parent)
    if start > end:
        start = end
    return parent[:start] + new_segment + parent[end:]


class EditorHarness:
    """AI 编辑完整流程编排器。

    生命周期:
      1. pre_analyze()  → 扫描当前章节问题 (硬校验 + 风格检查)
      2. build_context() → 构建完整编辑上下文 (人物/世界观/事实/关系)
      3. build_prompt()  → 构造结构化编辑 prompt（支持 inline selection 模式）
      4. execute()       → 流式调用 LLM
      5. post_validate() → 运行硬校验，对比修复前后
      6. report()        → 返回完整报告
    """

    def __init__(self, db: Database, chapter_idx: int, current_text: str):
        self.db = db
        self.chapter_idx = chapter_idx
        self.current_text = current_text
        self.chapter = kb.get_chapter_by_idx(db, chapter_idx)
        self.project = kb.get_or_create_project(db)
        self.characters = kb.list_characters(db)
        self._pre_issues: list[dict] = []
        self._post_issues: list[dict] = []
        self._context: dict[str, str] = {}

    # ---- Step 1: 预分析 ----
    def pre_analyze(self) -> dict:
        """扫描当前章节，找出已有问题。返回 {issues, by_severity, n_total}"""
        from novelai import consistency as cons_mod
        hard_issues = cons_mod.hard_check(self.db, self.chapter_idx, self.current_text)
        # 风格规则检查
        rules = kb.list_style_rules(self.db, enabled_only=True)
        style_violations = _check_style_rules(self.current_text, rules)
        for v in style_violations:
            hard_issues.append({
                "severity": v.get("severity", "low"),
                "category": "style",
                "explanation": v.get("explanation", ""),
                "fix_suggestion": v.get("fix_suggestion", ""),
            })
        self._pre_issues = hard_issues
        by_sev = {"high": 0, "medium": 0, "low": 0}
        for it in hard_issues:
            sev = it.get("severity", "low")
            by_sev[sev] = by_sev.get(sev, 0) + 1
        return {"issues": hard_issues, "by_severity": by_sev, "n_total": len(hard_issues)}

    # ---- Step 2: 构建上下文 ----
    def build_context(self) -> dict[str, str]:
        """收集编辑所需的所有上下文。

        复用 retriever.build_chapter_context —— 与"写新章"路径用同一套精细上下文：
        POV 信息边界（防 info_leak）、相关伏笔触发、上章承接、近章事件、地点匹配的世界观。
        比旧的"无差别 [:15]/[:20] 截断"在长篇一致性上显著更强。
        """
        # 调用 retriever 拿精细上下文（与 writer.py 写章管线同源）
        rctx = retriever.build_chapter_context(self.db, self.chapter_idx)

        ctx: dict[str, str] = {}
        # 项目设定（键名与 build_prompt 对齐）
        ctx["synopsis"] = (rctx.get("synopsis") or "")[:300]
        ctx["style"] = (rctx.get("style") or "")[:200]
        ctx["pov_mode"] = rctx.get("pov_mode") or "限知视角"

        # 人物档案：POV 档案 + 其他出场人物档案（retriever 已按重要度排序+top-K 裁剪）
        char_parts = []
        if rctx.get("pov_profile") and rctx["pov_profile"] != "（无明确 POV 角色 / 全知视角）":
            char_parts.append(rctx["pov_profile"])
        if rctx.get("other_characters_profiles"):
            char_parts.append(rctx["other_characters_profiles"])
        char_text = "\n".join(char_parts) or "（无）"
        # token 预算保护：超长截断（200+ 人物场景下 retriever 已裁剪，这里是兜底）
        MAX_CHAR_PROFILE_CHARS = 6000
        if len(char_text) > MAX_CHAR_PROFILE_CHARS:
            char_text = char_text[:MAX_CHAR_PROFILE_CHARS] + "\n…（人物档案过长，已截断）"
        ctx["character_profiles"] = char_text

        # 世界观（retriever 已按章节地点匹配，无匹配则退化全量）
        ctx["world"] = rctx.get("world_settings") or "（无）"

        # 核心事实 = POV 已知事实边界（限知视角防泄漏的关键）
        ctx["facts"] = rctx.get("known_facts") or "（无）"

        # 关系（retriever 已按 POV 相关过滤）
        ctx["relationships"] = rctx.get("relationships") or "（无）"

        # 新增：伏笔/承接/近章事件（旧 build_context 完全缺失）
        ctx["relevant_threads"] = rctx.get("relevant_threads") or "（无）"
        ctx["prev_chapter_summary"] = rctx.get("prev_chapter_summary") or "（这是第一章）"
        ctx["prev_chapter_unfinished"] = rctx.get("prev_chapter_unfinished") or "（无）"
        ctx["recent_event_summaries"] = rctx.get("recent_event_summaries") or "（无）"

        # 风格规则（编辑器独有，retriever 不负责）
        rules = kb.list_style_rules(self.db, enabled_only=True)
        ctx["style_rules"] = _build_style_rules_prompt(rules) if rules else ""

        self._context = ctx
        return ctx

    def context_summary(self) -> dict[str, str]:
        """生成给前端"透明度面板"展示的上下文摘要（精简版，每字段截断）。"""
        ctx = self._context or {}
        def _short(key: str, n: int = 200) -> str:
            v = ctx.get(key, "")
            return (v[:n] + "…") if isinstance(v, str) and len(v) > n else (v or "—")
        return {
            "pov_mode": ctx.get("pov_mode", "限知视角"),
            "characters": _short("character_profiles", 240),
            "facts": _short("facts", 200),
            "relationships": _short("relationships", 200),
            "world": _short("world", 200),
            "threads": _short("relevant_threads", 200),
            "prev_unfinished": _short("prev_chapter_unfinished", 120),
            "recent_events": _short("recent_event_summaries", 200),
            "style_rules": ctx.get("style_rules", ""),
        }

    # ---- Step 3: 构建 prompt ----
    def build_prompt(self, instruction: str, diagnosis: str = "", selection: dict | None = None) -> tuple[list[dict], str]:
        """构造编辑 prompt，含完整上下文 + 预分析结果。

        selection 非 None 时进入 inline 模式：AI 只输出选中片段的新版本（不重写整章）。
          selection = {"text": str, "start": int, "end": int}
        """
        ctx = self._context
        pre_issues = self._pre_issues

        # 预分析问题摘要
        issue_block = ""
        if pre_issues:
            highs = [i for i in pre_issues if i.get("severity") == "high"]
            meds = [i for i in pre_issues if i.get("severity") == "medium"]
            issue_lines = []
            if highs:
                issue_lines.append(f"\n### 🔴 高危问题 ({len(highs)} 条，必须修复)\n")
                for i, iss in enumerate(highs[:5], 1):
                    issue_lines.append(
                        f"{i}. [{iss.get('category','?')}] {iss.get('explanation','')}\n"
                        f"   修复方向: {iss.get('fix_suggestion', '自行判断')}"
                    )
            if meds:
                issue_lines.append(f"\n### 🟡 中危问题 ({len(meds)} 条，建议修复)\n")
                for i, iss in enumerate(meds[:5], 1):
                    issue_lines.append(
                        f"{i}. [{iss.get('category','?')}] {iss.get('explanation','')}"
                    )
            issue_block = "\n".join(issue_lines)

        is_inline = bool(selection and selection.get("text"))
        if is_inline:
            output_instruction = (
                "## 输出（重要）\n"
                "**只输出用户选中片段修改后的版本**——不要整章、不要解释、不要引号包裹、不要前后缀。"
                "长度与原文相近，保持与上下文人物性格/文风/视角一致。"
            )
            edit_principles = (
                "## 编辑原则\n"
                "- 只改选中的片段，不要改写整章\n"
                "- 文风一致：修改后读起来应是同一个人写的\n"
                "- 问题优先：如有诊断问题，尽量在选中片段内修复\n"
                "- 规则必遵：如有风格规则，严格遵守\n"
                "- 上下文约束：不创造新人物/新地点/新规则\n"
                "- 视角一致：修改时**不得新增** POV 不应知道的信息泄漏（见‘核心事实’边界）；但原文已存在的遗留内容不强制删除\n"
                "- 伏笔推进：如列出了 developing 状态的相关伏笔，修改时在不偏离用户指令的前提下自然推进，不要遗忘\n"
            )
        else:
            output_instruction = (
                "## 输出\n"
                "仅输出修改后的完整章节正文（Markdown），不要任何解释。"
            )
            edit_principles = (
                "## 编辑原则\n"
                "- 精准修改：只改用户要求改的部分，不动其他文字\n"
                "- 文风一致：修改后读起来应是同一个人写的\n"
                "- 问题优先：如有诊断问题，优先修复\n"
                "- 规则必遵：如有风格规则，严格遵守\n"
                "- 上下文约束：不创造新人物/新地点/新规则\n"
                "- 视角一致：修改时**不得新增** POV 不应知道的信息泄漏（见‘核心事实’边界）；但原文已存在的遗留内容不强制删除\n"
                "- 伏笔推进：如列出了 developing 状态的相关伏笔，修改时在不偏离用户指令的前提下自然推进，不要遗忘\n"
            )

        # 伏笔/承接块（仅在有内容时注入，避免"（无）"污染 prompt）
        continuity_block = ""
        if ctx.get("relevant_threads") and ctx["relevant_threads"] != "（无）":
            continuity_block += f"\n## 相关伏笔（developing 优先推进，planted 可视情况呼应）\n{ctx['relevant_threads']}\n"
        if ctx.get("prev_chapter_unfinished") and ctx["prev_chapter_unfinished"] != "（无）":
            continuity_block += f"\n## 上一章未完成动作（修改时保持承接）\n{ctx['prev_chapter_unfinished']}\n"

        system = (
            "你是一位资深长篇小说编辑。你拥有完整的项目上下文和预分析结果。\n\n"
            f"## 项目设定\n"
            f"- 梗概: {ctx['synopsis']}\n"
            f"- 文风: {ctx['style']}\n"
            f"- 视角: {ctx['pov_mode']}\n\n"
            f"## 人物档案\n{ctx['character_profiles']}\n\n"
            f"## 世界观\n{ctx['world']}\n\n"
            f"## 核心事实（POV 信息边界 —— 修改时不得新增对这些事实以外秘密的泄漏）\n{ctx['facts']}\n\n"
            f"## 人物关系\n{ctx['relationships']}\n"
            f"{continuity_block}\n"
            f"{ctx['style_rules']}\n"
            f"{diagnosis}\n"
            f"{issue_block}\n\n"
            f"{edit_principles}\n"
            f"{output_instruction}"
        )
        if is_inline:
            sel_text = selection["text"]
            user = (
                f"## 用户指令\n{instruction}\n\n"
                f"## 需要修改的选中片段（{len(sel_text)} 字，只改这一段）\n"
                f"{sel_text}\n\n"
                f"## 所在章节上下文（仅供参考，**不要修改**这部分）\n{self.current_text}\n\n"
                "请只输出选中片段修改后的版本。"
            )
        else:
            # 整章模式：长章节截断防超 token（保留开头+结尾，中间省略）
            full_text = _smart_text_preview(self.current_text, instruction, max_chars=8000)
            user = (
                f"## 用户指令\n{instruction}\n\n"
                f"## 当前章节正文\n{full_text}\n\n"
                "请输出修改后的完整章节正文。"
            )
        self._full_prompt = system + "\n\n" + user
        return [{"role": "system", "content": system}, {"role": "user", "content": user}], self._full_prompt

    # ---- Step 3b: Plan 模式 prompt（输出结构化计划，不改正文）----
    def _format_issues(self) -> str:
        """把 pre_analyze 发现的硬校验问题格式化为文本块（供 plan prompt 引用）。"""
        if not self._pre_issues:
            return "（规则引擎未发现问题）"
        lines = []
        for i, iss in enumerate(self._pre_issues[:10], 1):
            sev = iss.get("severity", "low")
            lines.append(
                f"{i}. [{sev}] {iss.get('category', '?')}: {iss.get('explanation', '')[:100]}"
                f" → 修复方向: {iss.get('fix_suggestion', '自行判断')}"
            )
        return "\n".join(lines)

    def build_plan_prompt(self, instruction: str) -> list[dict]:
        """构造 plan 模式 prompt：AI 输出结构化修改计划（不直接改正文）。

        复用 self._context + self._pre_issues（调用前需先 pre_analyze + build_context）。
        """
        ctx = self._context
        # 伏笔/承接块（与 build_prompt 同源）
        continuity_block = ""
        if ctx.get("relevant_threads") and ctx["relevant_threads"] != "（无）":
            continuity_block += f"\n## 相关伏笔\n{ctx['relevant_threads']}\n"
        if ctx.get("prev_chapter_unfinished") and ctx["prev_chapter_unfinished"] != "（无）":
            continuity_block += f"\n## 上一章未完成动作\n{ctx['prev_chapter_unfinished']}\n"
        system = (
            "你是一位资深小说编辑。任务：分析章节，输出**修改计划**（不直接改正文）。\n\n"
            f"## 项目设定\n梗概: {ctx.get('synopsis', '')}\n文风: {ctx.get('style', '')}\n视角: {ctx.get('pov_mode', '')}\n\n"
            f"## 人物档案\n{ctx.get('character_profiles', '')}\n\n"
            f"## 核心事实（POV 信息边界 —— 计划修改时不得新增对这些事实以外秘密的泄漏）\n{ctx.get('facts', '')}\n\n"
            f"## 人物关系\n{ctx.get('relationships', '')}\n"
            f"{continuity_block}\n"
            f"{ctx.get('style_rules', '')}\n\n"
            f"## 规则引擎已发现的问题\n{self._format_issues()}\n\n"
            "## 输出要求（重要）\n"
            "输出 JSON：{\"items\": [{\"what\": \"做什么(一句话,具体可执行)\", "
            "\"why\": \"为什么(一句话)\", \"where\": \"在文中哪里(引用原文片段或描述位置)\", "
            "\"context_refs\": \"本项依据了哪些上下文(如:人物X/伏笔Y/规则Z, 没有则留空)\", "
            "\"severity\": \"high|medium|low\"}]}\n"
            "- what 必须具体（如\"把第3段'他想起父王曾说'删掉\"，而非\"修复信息泄漏\"）\n"
            "- where 尽量引用原文片段，让用户能定位\n"
            "- context_refs 让用户能审计每项修改的依据\n"
            "- 最多 6 项，按严重度排序（high 在前）\n"
            "- 如果没有需要修改的，返回 {\"items\": []}\n"
            "- 只输出 JSON，不要任何解释或代码块标记"
        )
        # 智能截断：超长章节保留开头 + 结尾 + 用户指令相关段（而非粗暴 [:6000] 砍掉后文）
        text_preview = _smart_text_preview(self.current_text, instruction, max_chars=6000)
        user = (
            f"## 用户意图\n{instruction}\n\n"
            f"## 本章正文\n{text_preview}\n\n"
            "请输出修改计划(JSON):"
        )
        return [{"role": "system", "content": system}, {"role": "user", "content": user}]

    # ---- Step 5: 后验证 ----
    def post_validate(self, edited_text: str) -> dict:
        """对编辑后文本运行硬校验，对比前后变化。返回 {issues, fixed, introduced, before_n, after_n}"""
        from novelai import consistency as cons_mod
        self._post_issues = cons_mod.hard_check(self.db, self.chapter_idx, edited_text)

        # 计算修复和引入
        pre_cats = {(i.get("category"), i.get("explanation", "")[:50]) for i in self._pre_issues}
        post_cats = {(i.get("category"), i.get("explanation", "")[:50]) for i in self._post_issues}

        fixed = pre_cats - post_cats
        introduced = post_cats - pre_cats

        by_sev = {"high": 0, "medium": 0, "low": 0}
        for it in self._post_issues:
            sev = it.get("severity", "low")
            by_sev[sev] = by_sev.get(sev, 0) + 1

        return {
            "issues": self._post_issues,
            "by_severity": by_sev,
            "n_total": len(self._post_issues),
            "fixed_count": len(fixed),
            "introduced_count": len(introduced),
            "before_n": len(self._pre_issues),
            "after_n": len(self._post_issues),
            "improvement": len(self._pre_issues) - len(self._post_issues),
        }

    # ---- Step 6: 完整报告 ----
    def report(self, edited_text: str, post: dict) -> dict:
        return {
            "chapter_idx": self.chapter_idx,
            "edited_text": edited_text,
            "before": {"n_issues": len(self._pre_issues), "by_severity": {
                s: sum(1 for i in self._pre_issues if i.get("severity") == s)
                for s in ("high", "medium", "low")
            }},
            "after": {
                "n_issues": post["n_total"],
                "by_severity": post["by_severity"],
            },
            "delta": {
                "fixed": post["fixed_count"],
                "introduced": post["introduced_count"],
                "improvement": post["improvement"],
            },
        }


@router.post("/editor/chapter/{idx}/ai-edit")
async def api_editor_ai_edit(idx: int = ApiPath(ge=1, description="章节号, ≥1"), req: dict = Body(default_factory=dict)):
    """SSE 流式 AI 修改 — 完整 Harness 编排"""
    from fastapi.responses import StreamingResponse
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404)
    current_text = req.get("current_text", "") or (chapter.get("final_text") or chapter.get("draft") or "")
    instruction = req.get("instruction", "").strip()
    if not instruction:
        raise HTTPException(400, "instruction required")
    if not current_text.strip():
        raise HTTPException(400, "current_text required")

    harness = EditorHarness(db, idx, current_text)

    # inline 模式：selection = {"text": str, "start": int, "end": int}
    # 有 selection 时 AI 只输出选区新版本，后端拼回整章再校验
    selection = req.get("selection") or None
    if selection:
        if not isinstance(selection, dict) or not selection.get("text"):
            selection = None  # 非法 selection 退化为整章模式
        else:
            try:
                s = max(0, int(selection.get("start", 0)))
                e = min(len(current_text), int(selection.get("end", s)))
            except (TypeError, ValueError):
                selection = None  # start/end 非数字，退化为整章模式
            else:
                if e <= s:
                    selection = None  # 空选区退化
                else:
                    selection = {"text": selection["text"], "start": s, "end": e}

    async def stream():
        ai = AIClient()
        if not ai.ready:
            yield f"data: {json.dumps({'error': 'AI 未配置 .env 中的 NOVELAI_API_KEY'}, ensure_ascii=False)}\n\n"
            return
        try:
            import asyncio
            import queue
            import threading
            import time as _time

            # Phase 1: 预分析
            t0 = _time.time()
            yield f"data: {json.dumps({'phase': 'analyze', 'msg': '🔍 正在扫描章节问题…'}, ensure_ascii=False)}\n\n"
            pre = harness.pre_analyze()
            # 把发现的问题类型告诉用户（比"N 个问题"更具体）
            pre_cats = {}
            for iss in (pre.get("issues") or []):
                cat = iss.get("category", "?")
                pre_cats[cat] = pre_cats.get(cat, 0) + 1
            pre_summary = "、".join(f"{k} {v}个" for k, v in pre_cats.items()) if pre_cats else "无明显问题"
            yield f"data: {json.dumps({'phase': 'analyze_done', 'pre_analysis': pre, 'msg': f'扫描完成：{pre_summary}（{(_time.time()-t0)*1000:.0f}ms）', 'elapsed_ms': int((_time.time()-t0)*1000)}, ensure_ascii=False)}\n\n"

            # Phase 2: 构建上下文
            yield f"data: {json.dumps({'phase': 'context', 'msg': '📚 正在收集上下文…'}, ensure_ascii=False)}\n\n"
            harness.build_context()
            ctx = harness._context
            # 告诉用户收集到了什么
            ctx_summary_parts = []
            if ctx.get("character_profiles") and ctx["character_profiles"] != "（无）":
                ctx_summary_parts.append("人物档案")
            if ctx.get("relevant_threads") and ctx["relevant_threads"] != "（无）":
                ctx_summary_parts.append("伏笔")
            if ctx.get("prev_chapter_unfinished") and ctx["prev_chapter_unfinished"] != "（无）":
                ctx_summary_parts.append("上章承接")
            if ctx.get("facts") and ctx["facts"] != "（无）" and ctx["facts"] != "（全知视角）":
                ctx_summary_parts.append("信息边界")
            if ctx.get("world") and ctx["world"] != "（无）":
                ctx_summary_parts.append("世界观")
            ctx_msg = "已加载：" + "、".join(ctx_summary_parts) if ctx_summary_parts else "上下文较少"
            yield f"data: {json.dumps({'phase': 'context_done', 'msg': ctx_msg}, ensure_ascii=False)}\n\n"

            # Phase 2b: AI 主动工具调用（让 AI 决定还需要查哪些知识库细节）
            diagnosis = (req.get("diagnosis") or "").strip()
            tool_results_text = ""
            if CONFIG.writer.editor_tool_use and CONFIG.ai.provider in ("openai", "openai_compatible"):
                try:
                    from novelai import tools as tools_mod
                    # 构造轻量探测 prompt：给 AI 工具列表 + 用户意图，让它决定查什么
                    probe_messages = [
                        {"role": "system", "content": "你是小说编辑助手。根据用户修改意图，决定是否需要查询知识库以获得更准确的上下文。只查你不确定的，已有上下文里明确的不用再查。"},
                        {"role": "user", "content": f"用户意图：{instruction}\n\n请决定是否需要调用工具查询人物/事实/伏笔/关系的细节。如不需要，直接回复'无需查询'。"},
                    ]
                    rounds = 0
                    accumulated_results = []
                    while rounds < CONFIG.writer.editor_max_tool_rounds:
                        rounds += 1
                        result = ai.chat_with_tools(probe_messages, tools_mod.TOOL_DEFINITIONS, temperature=0.2, max_tokens=500)
                        if not result["tool_calls"]:
                            break  # AI 不需要查询了
                        # 执行每个工具调用
                        probe_messages.append(tools_mod.build_assistant_tool_message(result["tool_calls"]))
                        for tc in result["tool_calls"]:
                            tc_name = tc["name"]
                            tc_args = tc.get("arguments", {})
                            # 把查询参数也展示给用户（如"查询人物：风清扬"）
                            arg_str = "、".join(f"{k}={v}" for k, v in tc_args.items()) if tc_args else ""
                            tool_msg = f"AI 查询{tc_name}({arg_str})" if arg_str else f"AI 查询{tc_name}"
                            yield f"data: {json.dumps({'phase': 'tool_call', 'msg': '🔍 ' + tool_msg}, ensure_ascii=False)}\n\n"
                            tool_result = tools_mod.execute_tool(db, tc["name"], tc.get("arguments", {}))
                            accumulated_results.append(f"[查询 {tc['name']}({tc.get('arguments',{})})]\n{tool_result}")
                            probe_messages.append(tools_mod.build_tool_result_message(tc, tool_result))
                    if accumulated_results:
                        tool_results_text = "\n\n".join(accumulated_results)
                except Exception as _te:
                    # 工具调用失败不阻塞主流程（降级为无工具的纯生成）
                    pass

            # Phase 3: 构建 prompt + 流式生成
            # 如有工具查询结果，追加到 diagnosis（作为补充上下文注入 prompt）
            effective_diagnosis = diagnosis
            if tool_results_text:
                effective_diagnosis = (diagnosis + "\n\n## AI 主动查询到的补充信息\n" + tool_results_text).strip()
            messages, _ = harness.build_prompt(instruction, diagnosis=effective_diagnosis, selection=selection)
            gen_msg = "✍️ AI 正在修改选中片段…" if selection else "✍️ AI 正在修改…"
            yield f"data: {json.dumps({'phase': 'generate', 'msg': gen_msg}, ensure_ascii=False)}\n\n"

            q: queue.Queue = queue.Queue()
            def _gen():
                try:
                    for chunk in ai.chat_stream(messages, temperature=0.6, max_tokens=max(CONFIG.ai.max_tokens, 8000)):
                        q.put(("chunk", chunk))
                    q.put(("done", None))
                except Exception as e:
                    q.put(("error", str(e)))
            t = threading.Thread(target=_gen, daemon=True)
            t.start()
            buf = ""
            while True:
                kind, payload = await asyncio.to_thread(q.get, timeout=180)
                if kind == "chunk":
                    buf += payload
                    yield f"data: {json.dumps({'chunk': payload}, ensure_ascii=False)}\n\n"
                elif kind == "done":
                    break
                elif kind == "error":
                    _err_msg = err_detail("AI 改稿", idx=idx, step="生成阶段") + ": " + str(payload)
                    yield f"data: {json.dumps({'error': _err_msg}, ensure_ascii=False)}\n\n"
                    return
            t.join(timeout=5)

            # Phase 4: 后验证
            yield f"data: {json.dumps({'phase': 'validate', 'msg': '🔬 正在验证修改结果…'}, ensure_ascii=False)}\n\n"
            if selection:
                # inline: buf 是选区新版本，拼回整章再跑硬校验
                spliced = _buf_splice(current_text, selection["start"], selection["end"], buf)
                post = harness.post_validate(spliced)
                report = harness.report(spliced, post)
                report["mode"] = "inline"
                report["selection"] = selection
                report["rewritten_selection"] = buf  # 给前端做 diff（= done.text）
            else:
                # 整章模式（原逻辑）
                post = harness.post_validate(buf)
                report = harness.report(buf, post)
                report["mode"] = "full"

            # Phase 4b: 自校验重试（若引入了新高危问题，自动再改一次）
            retries_done = 0
            final_introduced = report.get("delta", {}).get("introduced", 0)
            if (CONFIG.writer.editor_self_retry
                    and final_introduced > 0):
                # 收集本轮引入的问题作为修正指令
                introduced_issues = [i for i in harness._post_issues
                                     if (i.get("category"), i.get("explanation", "")[:50])
                                     not in {(x.get("category"), x.get("explanation", "")[:50]) for x in harness._pre_issues}
                                     and i.get("severity") in ("high", "medium")]
                if introduced_issues:
                    issue_list = "; ".join(f"[{i.get('category','?')}] {i.get('explanation','')[:80]}" for i in introduced_issues[:4])
                    fix_hint = "；".join(i.get("fix_suggestion", "") for i in introduced_issues[:4] if i.get("fix_suggestion"))
                    retry_instruction = (
                        f"原指令：{instruction}\n\n"
                        f"⚠️ 自检发现上一版引入了这些问题，请在保持修改意图的前提下修正：\n{issue_list}"
                        + (f"\n修正方向：{fix_hint}" if fix_hint else "")
                    )
                    # 把具体问题展示给用户
                    issue_detail = "；".join(f"[{i.get('category','?')}] {i.get('explanation','')[:40]}" for i in introduced_issues[:3])
                    yield f"data: {json.dumps({'phase': 'self_check', 'msg': f'🔬 自检发现 {len(introduced_issues)} 处新问题：{issue_detail}，正在修正…'}, ensure_ascii=False)}\n\n"

                    # 以本轮结果为新 current_text，重建 prompt 再生成一轮
                    retry_base = spliced if selection else buf
                    # 临时替换 current_text 为本轮结果（build_prompt 用 self.current_text 作为"当前正文"）
                    saved_text = harness.current_text
                    harness.current_text = retry_base
                    retry_messages, _ = harness.build_prompt(retry_instruction, diagnosis=diagnosis, selection=None)
                    harness.current_text = saved_text

                    q2: queue.Queue = queue.Queue()
                    def _gen2():
                        try:
                            for chunk in ai.chat_stream(retry_messages, temperature=0.5, max_tokens=max(CONFIG.ai.max_tokens, 8000)):
                                q2.put(("chunk", chunk))
                            q2.put(("done", None))
                        except Exception as e:
                            q2.put(("error", str(e)))
                    t2 = threading.Thread(target=_gen2, daemon=True)
                    t2.start()
                    # 标记这是自校验重试的流（前端可以区分显示）
                    yield f"data: {json.dumps({'phase': 'retry_generate', 'msg': '🔄 正在重新生成修正版…'}, ensure_ascii=False)}\n\n"
                    buf2 = ""
                    while True:
                        kind2, payload2 = await asyncio.to_thread(q2.get, timeout=180)
                        if kind2 == "chunk":
                            buf2 += payload2
                            yield f"data: {json.dumps({'chunk': payload2}, ensure_ascii=False)}\n\n"
                        elif kind2 == "done":
                            break
                        elif kind2 == "error":
                            yield f"data: {json.dumps({'error': '自校验重试失败: ' + payload2}, ensure_ascii=False)}\n\n"
                            break
                    t2.join(timeout=5)

                    # 对重试结果再验证，取引入更少的版本
                    if buf2.strip():
                        post2 = harness.post_validate(buf2)
                        report2 = harness.report(buf2, post2)
                        report2["mode"] = "full"
                        if report2["delta"]["introduced"] <= report["delta"]["introduced"]:
                            # 重试版更好或持平，采用
                            buf = buf2
                            report = report2
                            retries_done = 1
                            final_introduced = report["delta"]["introduced"]
                        # 否则保留第一版（重试没改善）

            report["retries"] = retries_done
            report["final_introduced"] = final_introduced

            # 透明度：把 AI 用到的上下文摘要传给前端（用户可审计 AI 的"依据"）
            report["context_summary"] = harness.context_summary()

            # Phase 5: 完成
            # text 始终是 AI 原始输出：整章模式=完整正文；inline 模式=选区新版本
            # 落库 AI 调用计量（此时 ai.last_usage 已被 chat_stream 填好，子线程已结束）
            try:
                kb.log_ai_call(db, "editor_ai_edit", ai.last_usage, chapter_id=chapter["id"])
            except Exception:
                pass  # 计量失败不影响主流程
            yield f"data: {json.dumps({'done': True, 'text': buf, 'report': report}, ensure_ascii=False)}\n\n"

        except Exception as e:
            # 异常时也记一笔失败调用（latency 仍可读，usage 可能为 None）
            try:
                kb.log_ai_call(db, "editor_ai_edit", ai.last_usage,
                               chapter_id=chapter["id"], success=False, error=str(e)[:200])
            except Exception:
                pass
            hint = friendly_hint(e)
            _err_msg = err_detail("AI 改稿", idx=idx, e=e) + " | " + hint
            yield f"data: {json.dumps({'error': _err_msg}, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


@router.post("/editor/chapter/{idx}/ai-plan")
async def api_editor_ai_plan(idx: int = ApiPath(ge=1, description="章节号, ≥1"), req: dict = Body(default_factory=dict)):
    """Plan 模式 SSE：AI 分析问题输出结构化修改计划，不改正文。
    复用 EditorHarness 的 pre_analyze + build_context（无 LLM），再调一次 chat_json 出计划。
    返回 SSE：phase 进度 → done{plan: {items, pre_analysis}}
    """
    from fastapi.responses import StreamingResponse
    db = get_db()
    chapter = kb.get_chapter_by_idx(db, idx)
    if not chapter:
        raise HTTPException(404)
    current_text = req.get("current_text", "") or (chapter.get("final_text") or chapter.get("draft") or "")
    instruction = (req.get("instruction") or "").strip()
    if not instruction:
        raise HTTPException(400, "instruction required")
    if not current_text.strip():
        raise HTTPException(400, "current_text required")

    harness = EditorHarness(db, idx, current_text)
    chapter_id = chapter["id"]

    async def stream():
        ai = AIClient()
        if not ai.ready:
            yield f"data: {json.dumps({'error': 'AI 未配置 .env 中的 NOVELAI_API_KEY'}, ensure_ascii=False)}\n\n"
            return
        try:
            import time as _time
            # Phase 1: 预分析（复用，无 LLM）
            t0 = _time.time()
            yield f"data: {json.dumps({'phase': 'analyze', 'msg': '🔍 正在扫描章节问题…'}, ensure_ascii=False)}\n\n"
            pre = harness.pre_analyze()
            yield f"data: {json.dumps({'phase': 'analyze_done', 'pre_analysis': pre, 'elapsed_ms': int((_time.time()-t0)*1000)}, ensure_ascii=False)}\n\n"

            # Phase 2: 构建上下文（复用，无 LLM）
            yield f"data: {json.dumps({'phase': 'context', 'msg': '📚 正在收集上下文…'}, ensure_ascii=False)}\n\n"
            harness.build_context()

            # Phase 3: chat_json 生成计划
            yield f"data: {json.dumps({'phase': 'plan', 'msg': '📋 AI 正在制定修改计划…'}, ensure_ascii=False)}\n\n"
            messages = harness.build_plan_prompt(instruction)
            items = []
            try:
                data = ai.chat_json(messages, temperature=0.3)
                # 容错：接受 {"items":[...]} 或 [...] 两种形状
                if isinstance(data, dict):
                    items = data.get("items", [])
                elif isinstance(data, list):
                    items = data
                # 验证 + 清洗每项
                valid_items = []
                for i, it in enumerate(items[:8]):
                    if not isinstance(it, dict):
                        continue
                    what = (it.get("what") or "").strip()
                    if not what:
                        continue
                    valid_items.append({
                        "id": i,
                        "what": what,
                        "why": (it.get("why") or "").strip(),
                        "where": (it.get("where") or "").strip(),
                        "severity": it.get("severity", "medium") if it.get("severity") in ("high", "medium", "low") else "medium",
                        "approved": False,
                    })
                items = valid_items
            except Exception as e:
                yield f"data: {json.dumps({'error': f'计划生成失败: {e}'}, ensure_ascii=False)}\n\n"
                return

            # 落库 AI 调用计量
            try:
                kb.log_ai_call(db, "ai_plan", ai.last_usage, chapter_id=chapter_id)
            except Exception:
                pass

            # Phase 4: 完成，返回计划
            yield f"data: {json.dumps({'done': True, 'plan': {'items': items, 'pre_analysis': pre}}, ensure_ascii=False)}\n\n"

        except Exception as e:
            try:
                kb.log_ai_call(db, "ai_plan", ai.last_usage, chapter_id=chapter_id, success=False, error=str(e)[:200])
            except Exception:
                pass
            yield f"data: {json.dumps({'error': str(e)}, ensure_ascii=False)}\n\n"

    return StreamingResponse(stream(), media_type="text/event-stream")


# ============== 手稿导入 ==============

@router.post("/import")
def api_import_md(req: dict) -> dict:
    """同步导入（适合中小型手稿；20 万字也能秒级）"""
    path = req.get("path")
    if not path:
        raise HTTPException(400, "missing path")
    title = req.get("title")
    unit = req.get("story_time_unit", "回")

    # 安全：限制可读取路径在项目根下，防止任意文件读取
    p = Path(path).resolve()
    allowed_roots = [
        _project_root().resolve(),
        (_project_root() / "data").resolve(),
        (_project_root() / "examples").resolve(),
        Path.home().resolve() / "Desktop",
        Path.home().resolve() / "Documents",
    ]
    if not any(str(p).startswith(str(r)) for r in allowed_roots):
        raise HTTPException(403, f"路径不在允许范围内: {path}")

    if not p.exists():
        raise HTTPException(404, f"file not found: {path}")

    def cb(stage: str, msg: str) -> None:
        _log(stage, f"[import] {msg}")

    result = importer.import_markdown(
        get_db(), str(p),
        project_title=title,
        story_time_unit=unit,
        progress_cb=cb,
    )
    retriever.invalidate_cache()
    return result


@router.post("/import-content")
def api_import_content(req: dict) -> dict:
    """从前端直接导入文件内容（用于 webview 文件选择器 / 拖拽上传）。

    req: {
      filename: "第N回_xxx.md",
      content:  "整文件 UTF-8 文本",
      title:    "项目标题" (可选),
      mode:     "single" | "directory" (单文件/合并为单文件),
    }
    """
    filename = (req.get("filename") or "").strip()
    content = req.get("content")
    title = req.get("title")
    unit = req.get("story_time_unit", "回")
    if not filename or content is None:
        raise HTTPException(400, "missing filename or content")

    # 写到 DATA_DIR 下的 tmp 子目录
    DATA_DIR = _project_root() / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = DATA_DIR / "tmp_import"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # 防冲突：filename 加时间戳
    safe_name = re.sub(r'[<>:"/\\|?*]', '_', filename)
    target = tmp_dir / f"{int(time.time()*1000)}_{safe_name}"
    target.write_text(content, encoding="utf-8")

    def cb(stage: str, msg: str) -> None:
        _log(stage, f"[import] {msg}")

    try:
        result = importer.import_markdown(
            get_db(), target,
            project_title=title,
            story_time_unit=unit,
            progress_cb=cb,
        )
        retriever.invalidate_cache()
        return result
    except Exception as e:
        raise HTTPException(500, f"import failed: {e}")
    finally:
        # 无论成功或失败都清理临时文件
        try:
            target.unlink(missing_ok=True)
        except Exception:
            pass


@router.post("/import-directory")
def api_import_directory(req: dict) -> dict:
    """从前端导入多个文件（目录拖拽）。

    req: {
      files: [{filename, content}, ...],
      title: "项目标题" (可选),
    }
    """
    files = req.get("files") or []
    if not files:
        raise HTTPException(400, "no files")
    title = req.get("title")
    unit = req.get("story_time_unit", "回")

    DATA_DIR = _project_root() / "data"
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp_dir = DATA_DIR / "tmp_import"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    # 用一个时间戳目录，模拟"目录"
    batch_dir = tmp_dir / f"batch_{int(time.time()*1000)}"
    batch_dir.mkdir(parents=True, exist_ok=True)
    for f in files:
        fname = (f.get("filename") or "").strip()
        content = f.get("content")
        if not fname or content is None:
            continue
        safe_name = re.sub(r'[<>:"/\\|?*]', '_', fname)
        (batch_dir / safe_name).write_text(content, encoding="utf-8")

    def cb(stage: str, msg: str) -> None:
        _log(stage, f"[import] {msg}")

    try:
        result = importer.import_markdown(
            get_db(), batch_dir,
            mode="directory",
            project_title=title,
            story_time_unit=unit,
            progress_cb=cb,
        )
        retriever.invalidate_cache()
        return result
    except Exception as e:
        raise HTTPException(500, f"import failed: {e}")
    finally:
        # B-32: 无论成功失败, 临时 batch 目录都清 (避免磁盘泄漏)
        import shutil
        try: shutil.rmtree(batch_dir, ignore_errors=True)
        except: pass


# ============== 导出 ==============

@router.get("/export/chapter/{idx}.docx")
def api_export_chapter_docx(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> "Response":
    """导出单个章节为 .docx（纯 stdlib 实现）"""
    from fastapi.responses import Response
    db = get_db()
    ch = kb.get_chapter_by_idx(db, idx)
    if not ch:
        raise HTTPException(404, f"chapter {idx} not found")
    proj = kb.get_or_create_project(db)
    book_title = proj.get("title", "未命名") or "未命名"
    # 拉本章所有批注（OpenXML 原生批注导出）
    comments = kb.list_comments(db, chapter_id=ch["id"])
    docx_bytes = build_chapter_docx(ch, book_title=book_title, comments=comments)
    safe_book = re.sub(r'[<>:"/\\|?*]', '_', book_title)[:50]
    safe_ch = re.sub(r'[<>:"/\\|?*]', '_', ch.get("title", f"第{ch['idx']}回"))[:50]
    filename = f"{safe_book}_第{ch['idx']}回_{safe_ch}.docx"
    from urllib.parse import quote
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/export/all.docx")
def api_export_all_docx() -> "Response":
    """导出整本小说为单个 .docx（纯 stdlib 实现）"""
    from fastapi.responses import Response
    db = get_db()
    chapters = kb.list_chapters(db)
    volumes = kb.list_volumes(db)
    proj = kb.get_or_create_project(db)
    # 拉每章批注（OpenXML 原生批注导出）
    comments_by_chapter = {}
    all_comments = kb.list_comments(db)
    for c in all_comments:
        comments_by_chapter.setdefault(c["chapter_id"], []).append(c)
    docx_bytes = build_book_docx(chapters, volumes, proj, comments_by_chapter=comments_by_chapter)
    book_title = proj.get("title", "未命名") or "未命名"
    safe = re.sub(r'[<>:"/\\|?*]', '_', book_title)[:50]
    filename = f"{safe}_全本.docx"
    from urllib.parse import quote
    return Response(
        content=docx_bytes,
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# ===== Markdown 备份导出（用户多一份纯文本备份） =====

def _chapter_to_markdown(ch: dict, volume_title: str = None) -> str:
    """单章 → Markdown"""
    lines = []
    title = ch.get("title", f"第{ch['idx']}回")
    if volume_title:
        lines.append(f"## {volume_title}")
    lines.append(f"# {title}")
    lines.append("")
    # B-新131: kb.list_chapters() 返原始 chapter dict (字段 final_text/draft), 不是 /editor/chapter/{idx} 的封装 (字段 text)
    # 旧代码 ch.get("text") 永远空, 导致导出 .md 没正文.
    text = ch.get("final_text") or ch.get("draft") or ""
    # 简单段落切分（按空行）—— 用户原手稿就是按段落写的
    paragraphs = re.split(r'\n\s*\n', text.strip())
    for p in paragraphs:
        if p.strip():
            lines.append(p.strip())
            lines.append("")
    # 元数据
    lines.append("---")
    lines.append(f"*字数：{len(text)} · 导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')}*")
    return "\n".join(lines)


@router.get("/export/chapter/{idx}.md")
def api_export_chapter_md(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> "Response":
    """导出单个章节为 .md（Markdown 备份）"""
    from fastapi.responses import Response
    db = get_db()
    ch = kb.get_chapter_by_idx(db, idx)
    if not ch:
        raise HTTPException(404, f"chapter {idx} not found")
    proj = kb.get_or_create_project(db)
    book_title = proj.get("title", "未命名") or "未命名"
    # 找卷标题（如果有）
    volume_title = None
    if ch.get("volume_idx"):
        vol = kb.get_volume_by_idx(db, ch["volume_idx"])
        if vol:
            volume_title = vol.get("title")
    md = _chapter_to_markdown(ch, volume_title=volume_title)
    safe_book = re.sub(r'[<>:"/\\|?*]', '_', book_title)[:50]
    safe_ch = re.sub(r'[<>:"/\\|?*]', '_', ch.get("title", f"第{ch['idx']}回"))[:50]
    filename = f"{safe_book}_第{ch['idx']}回_{safe_ch}.md"
    from urllib.parse import quote
    return Response(
        content=md.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


@router.get("/export/all.md")
def api_export_all_md() -> "Response":
    """导出整本小说为单个 .md（备份所有章节到 1 个文件）"""
    from fastapi.responses import Response
    db = get_db()
    chapters = kb.list_chapters(db)
    volumes = kb.list_volumes(db)
    proj = kb.get_or_create_project(db)
    book_title = proj.get("title", "未命名") or "未命名"
    vol_by_id = {v["idx"]: v.get("title") for v in volumes}
    parts = [f"# 《{book_title}》", "", f"*导出时间：{datetime.now().strftime('%Y-%m-%d %H:%M')} · 共 {len(chapters)} 回*", "", "---", ""]
    for ch in chapters:
        vol_title = vol_by_id.get(ch.get("volume_idx"))
        parts.append(_chapter_to_markdown(ch, volume_title=vol_title))
        parts.append("\n---\n")
    md = "\n".join(parts)
    safe = re.sub(r'[<>:"/\\|?*]', '_', book_title)[:50]
    filename = f"{safe}_全本.md"
    from urllib.parse import quote
    return Response(
        content=md.encode("utf-8"),
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(filename)}"},
    )


# ============== WebSocket：实时进度推送 ==============

@router.websocket("/ws")
async def websocket_endpoint(ws: WebSocket):
    await ws.accept()
    # B-新100: server → client 周期 ping, client 不响应则断开 (防半开连接假死)
    ping_interval = 25  # 秒
    last_ping = 0.0
    try:
        last_log_seq = 0
        while True:
            # 处理 client → server 消息 (非阻塞): client 发了 text 就收 (不依赖消息体)
            if ws.client_state.name != "CONNECTED":
                break
            with _PROGRESS_LOCK:
                cur = dict(_PROGRESS)
            logs = cur.get("log", [])
            # 用单调 seq 而非 len(logs) 判断新日志：log 裁到 100 条后 len 恒定，旧逻辑会停更
            new_entries = [e for e in logs if e.get("seq", 0) > last_log_seq]
            for entry in new_entries:
                await ws.send_json({"type": "log", "data": entry})
            if new_entries:
                last_log_seq = new_entries[-1].get("seq", last_log_seq)
            await ws.send_json({
                "type": "status",
                "data": {
                    "running": cur.get("running"),
                    "stage": cur.get("stage"),
                    "chapter_idx": cur.get("chapter_idx"),
                },
            })
            # 周期 ping (server 主动发 ping frame; client browser 自动回 pong)
            import time as _t
            if _t.time() - last_ping > ping_interval:
                try:
                    await ws.send_text('{"type":"ping"}')
                    last_ping = _t.time()
                except Exception:
                    break
            await asyncio.sleep(0.7)
    except WebSocketDisconnect:
        pass
    except Exception:
        # 任何异常静默断连, 前端 WS onclose 触发指数退避重连
        pass


# ============== 跨章节用词一致性 ==============

import re as _re_vocab

# "X总/老板/先生/女士" 模式 → 按 X 聚合
_HONORIFIC_PATTERNS = [
    (_re_vocab.compile(r"([\u4e00-\u9fa5]{2,4})总"), "总"),
    (_re_vocab.compile(r"([\u4e00-\u9fa5]{2,4})老板"), "老板"),
    (_re_vocab.compile(r"([\u4e00-\u9fa5]{2,4})先生"), "先生"),
    (_re_vocab.compile(r"([\u4e00-\u9fa5]{2,4})女士"), "女士"),
    (_re_vocab.compile(r"([\u4e00-\u9fa5]{2,4})经理"), "经理"),
    (_re_vocab.compile(r"([\u4e00-\u9fa5]{2,4})主任"), "主任"),
    (_re_vocab.compile(r"([\u4e00-\u9fa5]{2,4})董"), "董"),
]


@router.get("/vocab-consistency")
def api_vocab_consistency() -> dict:
    """跨章节用词一致性扫描：
    1. 角色名使用分布（每角色在每章出现几次、用哪个名字）
    2. 敬称聚合（"X总/X老板/X先生" → 按 X 聚合，找出同一人的不同叫法）
    """
    db = get_db()
    chapters = kb.list_chapters(db)
    characters = kb.list_characters(db)
    # 章节 idx → 文本
    ch_texts = {}
    for c in chapters:
        ch_texts[c["idx"]] = c.get("final_text") or c.get("draft") or ""
    # ============ 1) 角色名使用分布 ============
    role_usage = []
    for ch in characters:
        names = [ch["name"]] + (ch.get("aliases") or [])
        # 每章节计数
        per_chapter = []
        total_count = 0
        for c in chapters:
            txt = ch_texts[c["idx"]]
            chapter_hits = {}  # 哪个名字 hit 几次
            for n in names:
                if not n:
                    continue
                cnt = txt.count(n)
                if cnt > 0:
                    chapter_hits[n] = chapter_hits.get(n, 0) + cnt
                    total_count += cnt
            if chapter_hits:
                per_chapter.append({
                    "chapter_idx": c["idx"],
                    "chapter_title": c.get("title", f"第{c['idx']}回"),
                    "names": chapter_hits,
                })
        role_usage.append({
            "character_id": ch["id"],
            "name": ch["name"],
            "aliases": ch.get("aliases") or [],
            "total_count": total_count,
            "chapter_count": len(per_chapter),
            "chapters": per_chapter,
        })
    # 按出场次数排序
    role_usage.sort(key=lambda r: -r["total_count"])
    # ============ 2) 敬称聚合 ============
    honorific_buckets = {}  # surname → {"surname": str, "variants": {str: {chapters:set()}}, "total": int}
    stop_surnames = {"这个", "那个", "我们", "你们", "他们", "谁", "什么", "怎么", "公司", "项目", "部门", "客户"}
    for c in chapters:
        txt = ch_texts[c["idx"]]
        for pat, suffix in _HONORIFIC_PATTERNS:
            for m in pat.finditer(txt):
                surname = m.group(1)
                if surname in stop_surnames or len(surname) < 2:
                    continue
                full = surname + suffix
                if surname not in honorific_buckets:
                    honorific_buckets[surname] = {"surname": surname, "variants": {}, "total": 0}
                bucket = honorific_buckets[surname]
                if full not in bucket["variants"]:
                    bucket["variants"][full] = {"count": 0, "chapters": set()}
                bucket["variants"][full]["count"] += 1
                bucket["variants"][full]["chapters"].add(c["idx"])
                bucket["total"] += 1
    # 转 set → list
    honorifics = []
    for b in honorific_buckets.values():
        if len(b["variants"]) >= 2:  # 至少 2 种叫法才报告
            variants = []
            for vname, vdata in sorted(b["variants"].items(), key=lambda x: -x[1]["count"]):
                variants.append({
                    "name": vname,
                    "count": vdata["count"],
                    "chapters": sorted(vdata["chapters"]),
                })
            honorifics.append({
                "surname": b["surname"],
                "total": b["total"],
                "variant_count": len(variants),
                "variants": variants,
            })
    honorifics.sort(key=lambda h: -h["total"])
    return {
        "chapters_total": len(chapters),
        "characters_total": len(characters),
        "role_usage": role_usage,
        "honorifics": honorifics,
    }


# ============== 风格指南 (Style Rule) ==============

@router.get("/style-rules")
def api_list_style_rules(enabled_only: bool = False) -> dict:
    """列出所有风格规则"""
    return {"rules": kb.list_style_rules(get_db(), enabled_only=enabled_only)}


# ============== 系统设置 / AI 配置 ==============

@router.get("/system/info")
def api_system_info() -> dict:
    """系统信息：版本、路径、AI 是否就绪、数据位置"""
    import sys
    from novelai.config import CONFIG, PROJECT_ROOT, DATA_DIR
    # B-新131: 旧代码硬编码 "1.15.0", 实际版本号应来自 novelai.__version__
    return {
        "version": getattr(__import__("novelai", fromlist=["__version__"]), "__version__", "dev"),
        "platform": sys.platform,
        "frozen": getattr(sys, "frozen", False),
        "project_root": str(PROJECT_ROOT),
        "data_dir": str(DATA_DIR),
        "ai": {
            "ready": bool(CONFIG.ai.api_key),
            "provider": CONFIG.ai.provider,
            "model": CONFIG.ai.model,
            "base_url": CONFIG.ai.base_url,
        },
        "db_path": str(CONFIG.db_path),
    }


@router.post("/system/setup-ai")
def api_setup_ai(req: dict) -> dict:
    """首次启动：保存用户的 API key 到 .env
    req: {api_key, provider?, base_url?, model?}
    """
    from novelai.config import PROJECT_ROOT
    api_key = (req.get("api_key") or "").strip()
    if not api_key:
        raise HTTPException(400, "API key 必填")
    provider = (req.get("provider") or "openai_compatible").strip()
    base_url = (req.get("base_url") or "").strip() or None
    model = (req.get("model") or "deepseek-chat").strip()
    env_path = PROJECT_ROOT / ".env"
    # 保留已有内容，只更新相关行
    lines = []
    if env_path.exists():
        try:
            lines = env_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            lines = []
    # 移除已有相关行
    skip_keys = {"NOVELAI_PROVIDER", "NOVELAI_API_KEY", "NOVELAI_BASE_URL", "NOVELAI_MODEL"}
    lines = [l for l in lines if not any(l.startswith(k + "=") for k in skip_keys)]
    # 追加新值
    lines.append(f"NOVELAI_PROVIDER={provider}")
    lines.append(f"NOVELAI_API_KEY={api_key}")
    if base_url:
        lines.append(f"NOVELAI_BASE_URL={base_url}")
    lines.append(f"NOVELAI_MODEL={model}")
    try:
        # 原子写入：先写临时文件，再 replace，避免进程崩溃时损坏 .env
        import os as _os
        tmp_path = env_path.with_suffix(".env.tmp")
        tmp_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        _os.replace(str(tmp_path), str(env_path))
    except Exception as e:
        raise HTTPException(500, f"写 .env 失败: {e}")
    # 同步到当前进程环境
    os.environ["NOVELAI_PROVIDER"] = provider
    os.environ["NOVELAI_API_KEY"] = api_key
    if base_url:
        os.environ["NOVELAI_BASE_URL"] = base_url
    os.environ["NOVELAI_MODEL"] = model
    # v1.19.26: 重载 CONFIG (之前只重置 _client 但 cfg 仍缓存旧值, model 切换不生效)
    try:
        from novelai.config import reload_config
        reload_config()
    except Exception:
        pass
    # 重置 AIClient 单例
    try:
        from novelai import ai_client as _ai_client_mod
        _ai_client_mod._client = None  # type: ignore[attr-defined]
    except Exception:
        pass
    return {"ok": True, "path": str(env_path)}


# ============== 跨设备数据迁移 ==============

@router.post("/system/load-sample")
def api_load_sample() -> dict:
    """首次启动时一键创建示例项目（3 章 + 几个角色 + 几个事件 + 一致性报告）。
    让用户不用准备 .md 就能立即看到完整编辑界面。
    """
    db = get_db()
    if kb.list_chapters(db):
        return {"ok": False, "error": "项目已有章节，不覆盖（先到《导入》页清理数据再试）"}

    # 1) 项目元信息
    proj = kb.get_or_create_project(db)
    # B-新186: load-sample 也设 story_time_unit, 否则 dashboard 顶栏 meta 显示 "null"
    db.execute(
        "UPDATE project SET title=?, synopsis=?, pov_mode=?, story_time_unit=? WHERE id=?",
        ("示例：长安遗事（演示用）",
         "一位落魄商人在长安街头重新找回自我的故事。展示用项目，可随时删除。",
         "全知", "回", proj["id"]),
    )

    # 2) 角色
    sample_chars = [
        ("李长安", "protagonist", "长安落魄商人，30岁", "沉静/机敏/隐忍",
         "短句多，反问多，偶尔沉默后突然犀利",
         "主角被骗后回长安重新站起的故事"),
        ("老金", "supporting", "李长安的老友，酒楼老板", "豪爽/重义气/世故",
         "口头禅'兄弟'、'说白了'",
         "在长安开小酒楼，是李长安的精神支柱"),
        ("孙掌柜", "supporting", "布庄老板", "精明/谨慎/逐利",
         "说话绕弯子，喜欢用'您看'、'这样'开头",
         "李长安的生意对手"),
        ("小六", "supporting", "酒楼跑堂", "机灵/嘴甜/勤快",
         "短句多，常带'哎'、'哟'",
         "老金店里的小伙计"),
    ]
    char_ids = []
    for name, role, basic, personality, speech, arc in sample_chars:
        cid = kb.add_character(
            db, name=name, role=role, basic_info=basic,
            personality=personality, speech_style=speech, arc=arc,
        )
        char_ids.append(cid)

    # 3) 章节 + 文本
    sample_chapters = [
        (1, "长安春雨", "vol1", 1,
         "# 第一回 长安春雨\n\n"
         "## 地点：长安城·西市\n## 时间：开元十二年·春\n\n"
         "李长安把最后一锭银子递出去的时候，街上正下着雨。\n\n"
         "春雨细密如织，从终南山的云层里漏下来，落在西市的青石板上，溅不起水花。酒旗在风里半卷着，老金的酒楼门口拴着一头老驴，正甩着尾巴打苍蝇。\n\n"
         "「孙掌柜的布，这回押了三个月。」李长安心想，「再押不回来，铺子就要关张了。」\n\n"
         "他咳了一声，把油纸伞往肩上挪了挪，沿着西市的廊檐往东走。",
         "西市", "李长安把最后一锭银子押给了孙掌柜的布庄"),
        (2, "酒馆夜话", "vol1", 1,
         "# 第二回 酒馆夜话\n\n"
         "## 地点：长安城·老金酒楼\n## 时间：开元十二年·春\n\n"
         "「兄弟，」老金往李长安面前推了一碗黄酒，「这事儿我听明白了。」\n\n"
         "「你听明白了？」李长安苦笑，「老金，你是开酒楼的人，这商场上的事儿——」\n\n"
         "「商场的事儿我不懂，」老金摆摆手，「但人的事儿我懂。孙掌柜那小子眼睛里有鬼，你看不出来？」\n\n"
         "李长安低头看着碗里晃动的酒花，半晌没说话。\n\n"
         "「哎！」跑堂的小六从楼梯口探出头来，「李掌柜，孙掌柜那边又派人来问您的话。」",
         "老金酒楼", "李长安找老金喝酒倾诉"),
        (3, "西市晨雾", "vol1", 1,
         "# 第三回 西市晨雾\n\n"
         "## 地点：长安城·西市\n## 时间：开元十二年·春\n\n"
         "次日清晨，西市在雾里醒来。\n\n"
         "李长安推开自家布庄的门闩，店堂里还残留着昨夜的潮气。柜台上压着孙掌柜送来的一张纸条，墨迹未干。\n\n"
         "「老孙这是要赶尽杀绝。」他喃喃。\n\n"
         "远处传来报晓的鼓声，鼓声一响，西市就要开张了。他把纸条塞进袖中，整理了一下衣冠，准备出门。",
         "西市布庄", "李长安发现孙掌柜的纸条"),
    ]
    # 用 idx 索引 location/outline
    sample_chapters_loc = {c[0]: c[5] for c in sample_chapters}
    sample_chapters_outline = {c[0]: c[6] for c in sample_chapters}
    chapter_ids = []
    for c in sample_chapters:
        idx, title, vol, vol_idx, text, loc, outline_text = c
        ch_id = kb.add_chapter(db, idx=idx, title=title)
        # add_chapter 没 volume_idx/final_text，用 SQL 直接补
        db.execute(
            "UPDATE chapter SET final_text=?, draft=?, word_count=?, volume_idx=?, location=?, outline=? WHERE id=?",
            (text, text, len(text), vol_idx, loc, outline_text, ch_id),
        )
        chapter_ids.append((ch_id, idx, title))

    # 4) 事件
    sample_events = [
        (chapter_ids[0][0], 1, "长安春雨开篇", "李长安把最后一锭银子押给了孙掌柜的布庄。", "revelation", "西市", [char_ids[0], char_ids[2]], 4),
        (chapter_ids[0][0], 2, "雨夜独行", "李长安沿西市廊檐独行，心事重重。", "action", "西市", [char_ids[0]], 2),
        (chapter_ids[1][0], 1, "老金点破", "老金在酒楼里点破孙掌柜的鬼心思。", "revelation", "酒楼", [char_ids[0], char_ids[1]], 5),
        (chapter_ids[1][0], 2, "小六传话", "跑堂小六上楼传话孙掌柜又派人来。", "action", "酒楼", [char_ids[0], char_ids[3]], 3),
        (chapter_ids[2][0], 1, "晨雾纸条", "李长安在店里发现孙掌柜的纸条，气氛紧张。", "turning_point", "布庄", [char_ids[0], char_ids[2]], 4),
    ]
    for ch_id, seq, title, summary, etype, loc, parts, imp in sample_events:
        kb.add_event(
            db, chapter_id=ch_id, story_time=float(seq), sequence_in_chapter=seq,
            title=title, summary=summary, event_type=etype, location=loc,
            participants=parts, importance=imp,
        )

    # 5) 一致性报告 (通过的)
    for ch_id, idx, title in chapter_ids:
        report = {
            "issues": [],
            "suggestions": "示例数据：未发现一致性问题（演示用）。",
        }
        db.execute(
            "INSERT INTO consistency_report(chapter_id, passed, issues, suggestions, raw_response, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (ch_id, 1, json.dumps(report["issues"], ensure_ascii=False), report["suggestions"], "", time.time()),
        )

    return {
        "ok": True,
        "created": {
            "chapters": len(chapter_ids),
            "characters": len(char_ids),
            "events": len(sample_events),
        },
        "first_chapter": chapter_ids[0][1],  # 跳到第 1 章
    }


@router.get("/system/export-pack")
def api_export_pack() -> "Response":
    """导出完整数据 pack（含 .env + novel.db + novelai.log）到 zip
    跨设备：把 pack 拷到另一台电脑 → 导入 → 完整数据恢复
    """
    from fastapi.responses import Response
    import zipfile
    from datetime import datetime
    from novelai.config import PROJECT_ROOT, DATA_DIR
    buf = io.BytesIO()
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    proj = kb.get_or_create_project(get_db())
    proj_title = proj.get("title") or "未命名"
    safe_title = re.sub(r'[<>:"/\\|?*]', '_', proj_title)[:30]
    zip_name = f"novelai-writer-{safe_title}-{ts}.novelpack"
    # B-30: 先 checkpoint WAL, 把内存里所有改动落盘, 避免导出落后
    try:
        get_db().execute("PRAGMA wal_checkpoint(TRUNCATE);")
    except Exception:
        pass
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        # 1. .env（如果有）
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            z.write(str(env_path), ".env")
        # 2. novel.db（核心数据：章/人物/事件/批注/风格规则/版本/批注/AI 配置）
        # B-新105: 旧代码 ApiPath(... get("__db_path", ...)) 错! ApiPath 是 fastapi.Path, 跑 pathlib.Path, project dict 没 __db_path 这 key, 总返默认值. 实际 db path 用 CONFIG
        from novelai.config import CONFIG
        if CONFIG.db_path.exists():
            z.write(str(CONFIG.db_path), "novel.db")
        # B-30: 也把 WAL/SHM 一起打包 (Checkpoint 后通常不会再有, 但容错)
        for suffix in ("-wal", "-shm"):
            sidecar = str(CONFIG.db_path) + suffix
            if os.path.exists(sidecar):
                z.write(sidecar, f"novel.db{suffix}")
        # 3. manifest.json（导出元信息）
        chapters = kb.list_chapters(get_db())
        characters = kb.list_characters(get_db())
        events = []
        for c in chapters:
            events.extend(kb.list_events(get_db(), chapter_id=c["id"]))
        manifest = {
            "exported_at": datetime.now().isoformat(),
            # B-新108: 旧写死 "1.15.0", 现在拿 novelai.__version__ 真实版本
            "app_version": getattr(__import__("novelai", fromlist=["__version__"]), "__version__", "dev"),
            "project_title": proj_title,
            "chapters_count": len(chapters),
            "characters_count": len(characters),
            "events_count": len(events),
            "total_words": sum(c.get("word_count") or 0 for c in chapters),
            "has_env": env_path.exists(),
        }
        z.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    buf.seek(0)
    from urllib.parse import quote
    # B-新107: 校验 zip 体积, 防止 zlib bomb / 误操作超大文件
    zip_bytes = buf.getvalue()
    if len(zip_bytes) > 500 * 1024 * 1024:
        raise HTTPException(500, f"导出包过大 ({len(zip_bytes)} bytes), 请检查数据库")
    return Response(
        content=zip_bytes,
        media_type="application/zip",
        headers={"Content-Disposition": f"attachment; filename*=UTF-8''{quote(zip_name)}"},
    )


@router.post("/system/import-pack")
async def api_import_pack(request: Request) -> dict:
    """导入 novelpack zip — 备份当前 db → 解压覆盖 .env + novel.db
    req: multipart/form-data: file=@xxx.novelpack
    """
    import zipfile
    import shutil
    from datetime import datetime
    from novelai.config import PROJECT_ROOT, CONFIG
    form = await request.form()
    f = form.get("file")
    if not f or not hasattr(f, "filename"):
        raise HTTPException(400, "未上传文件")
    raw = await f.read()
    if not raw:
        raise HTTPException(400, "文件为空")
    # 备份当前
    backup_dir = CONFIG.db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    if CONFIG.db_path.exists():
        backup_path = backup_dir / f"novel.before-import.{ts}.bak"
        shutil.copy2(str(CONFIG.db_path), str(backup_path))
    env_path = PROJECT_ROOT / ".env"
    if env_path.exists():
        env_backup = backup_dir / f".env.before-import.{ts}.bak"
        shutil.copy2(str(env_path), str(env_backup))
    # 解压
    summary = {"env_imported": False, "db_imported": False, "manifest": None, "backup": []}
    try:
        z = zipfile.ZipFile(io.BytesIO(raw))
        names = z.namelist()
        # B-34: zip slip 防御: 拒绝 ../ 或绝对路径条目 (注意: B-新31 Path 命名覆盖, 这里用 pathlib.Path 不是 ApiPath)
        for name in names:
            if name.startswith("/") or ".." in Path(name).parts:
                raise HTTPException(400, f"非法条目 (zip slip 防护): {name}")
        if "novel.db" in names:
            # B-19: atomic 写: 先写 .tmp, fsync, os.replace
            tmp_db = str(CONFIG.db_path) + ".tmp"
            try:
                with z.open("novel.db") as src, open(tmp_db, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    try: os.fsync(dst.fileno())
                    except (AttributeError, OSError): pass  # Windows 上 fsync 可能抛
                # B-新110: 校验 db 完整性 (大小 + SQLite 头), 防 0 字节/损坏 db 覆盖
                _db_size = os.path.getsize(tmp_db)
                if _db_size < 16:
                    raise HTTPException(400, f"novel.db 过小 ({_db_size} bytes), 疑似损坏/空, 已拒绝导入")
                with open(tmp_db, "rb") as f:
                    _db_head = f.read(16)
                if not _db_head.startswith(b"SQLite format 3"):
                    raise HTTPException(400, f"novel.db 不是合法 SQLite 文件 (头 16 字节={_db_head!r}), 已拒绝导入")
                os.replace(tmp_db, str(CONFIG.db_path))
            except Exception:
                if os.path.exists(tmp_db):
                    try: os.remove(tmp_db)
                    except: pass
                raise
            summary["db_imported"] = True
        if ".env" in names:
            tmp_env = str(env_path) + ".tmp"
            try:
                with z.open(".env") as src, open(tmp_env, "wb") as dst:
                    shutil.copyfileobj(src, dst)
                    dst.flush()
                    try: os.fsync(dst.fileno())
                    except (AttributeError, OSError): pass
                os.replace(tmp_env, str(env_path))
            except Exception:
                if os.path.exists(tmp_env):
                    try: os.remove(tmp_env)
                    except: pass
                raise
            summary["env_imported"] = True
        if "manifest.json" in names:
            try:
                # B-新106: 限 manifest ≤ 1MB, 防恶意 zip 内放 100MB manifest OOM
                _MANIFEST_MAX = 1 * 1024 * 1024
                _manifest_info = z.getinfo("manifest.json")
                if _manifest_info.file_size > _MANIFEST_MAX:
                    summary["manifest"] = {"error": "manifest.json 超过 1MB 上限, 已忽略"}
                else:
                    _m = json.loads(z.read("manifest.json").decode("utf-8"))
                    # B-新109: 强制 manifest 是 dict, 防止 list/null 撑爆前端
                    summary["manifest"] = _m if isinstance(_m, dict) else {"error": "manifest 非 dict"}
            except Exception:
                pass
        summary["backup"] = [str(p) for p in backup_dir.glob(f"*.before-import.{ts}.*")]
    except zipfile.BadZipFile as e:
        raise HTTPException(400, f"不是有效的 zip / novelpack 文件: {e}")
    # 重新加载环境 + 同步 os.environ
    if summary["env_imported"]:
        try:
            from novelai.config import _load_env_file
            _load_env_file()
        except Exception:
            pass
    # 重置 AIClient 单例
    try:
        from novelai import ai_client as _ai_client_mod
        _ai_client_mod._client = None  # type: ignore[attr-defined]
    except Exception:
        pass
    return summary


@router.post("/style-rules")
def api_add_style_rule(req: dict) -> dict:
    """新增一条风格规则"""
    name = (req.get("name") or "").strip()
    rule_type = (req.get("rule_type") or "").strip()
    if not name or not rule_type:
        raise HTTPException(400, "name 和 rule_type 必填")
    if rule_type not in ("forbid_phrase", "max_para_chars", "max_dialogue_lines", "min_sentence_chars", "max_sentence_chars"):
        raise HTTPException(400, f"未知 rule_type: {rule_type}")
    rid = kb.add_style_rule(
        get_db(),
        name=name,
        rule_type=rule_type,
        pattern=(req.get("pattern") or "").strip() or None,
        # B-新126: severity 防御 (kb.add_style_rule 也兜, 这里返 400 友好)
        severity=req.get("severity", "mid") if req.get("severity") in ("high", "mid", "low") else "mid",
        description=(req.get("description") or "").strip() or None,
        enabled=req.get("enabled", True),
    )
    return {"id": rid}


@router.put("/style-rules/{rule_id}")
def api_update_style_rule(rule_id: int = ApiPath(ge=1, description="规则ID, ≥1"), req: dict = Body(default_factory=dict)) -> dict:
    """更新规则"""
    allowed = {"name", "pattern", "severity", "description", "enabled"}
    fields = {k: v for k, v in req.items() if k in allowed}
    if "enabled" in fields:
        fields["enabled"] = 1 if fields["enabled"] else 0
    kb.update_style_rule(get_db(), rule_id, **fields)
    return {"ok": True}


@router.delete("/style-rules/{rule_id}")
def api_delete_style_rule(rule_id: int = ApiPath(ge=1, description="规则ID, ≥1")) -> dict:
    """删除规则"""
    kb.delete_style_rule(get_db(), rule_id)
    return {"ok": True}


@router.post("/style-rules/suggest")
def api_style_rules_suggest(req: dict) -> dict:
    """LLM 基于前 N 章文本 → 建议要加哪些风格规则
    req: {max_chapters: int (默认 5), style_focus: str (可选)}
    返回 [{rule_type, name, pattern, severity, description, rationale}]
    """
    db = get_db()
    try:
        max_chapters = int(req.get("max_chapters", 5))
    except (TypeError, ValueError):
        max_chapters = 5
    style_focus = (req.get("style_focus") or "").strip()
    chapters = kb.list_chapters(db)[:max_chapters]
    # 收集样本 (前 10 章 × 前 1500 字)
    samples = []
    for c in chapters:
        text = (c.get("final_text") or c.get("draft") or "")[:1500]
        if text.strip():
            samples.append(f"### 第 {c['idx']} 回《{c.get('title','')}》\n{text}")
    if not samples:
        return {"suggestions": [], "error": "没有可分析的章节文本"}
    # 已有规则 (避免重复)
    existing = kb.list_style_rules(db, enabled_only=True)
    existing_summary = "\n".join(
        f"- [{r['rule_type']}] {r['name']}: {r.get('pattern','')}" for r in existing
    ) or "(无)"
    # LLM 评判
    user = (
        "## 你是一位资深长篇小说编辑。基于以下章节文本片段，分析当前文本的写作风格，建议要加入风格指南的规则。\n\n"
        "## 分析维度：\n"
        "1. **禁用词组**：文本中重复出现的不规范 / 网络化 / 不适合文学作品的表达（如 '非常'、'然而'、'于是就' 等）\n"
        "2. **段落长度**：是否有大段无段落的长段，需要限制单段最长 N 字\n"
        "3. **对话格式**：是否有对话过长、对话格式不统一的问题\n"
        "4. **句子节奏**：是否有句子过长 / 过短 / 句式单调\n"
        "5. **风格一致性**：用词、称谓、语气是否有不一致\n\n"
        "## 输出要求：\n"
        "- 严格 JSON 数组（不要任何额外文字）\n"
        "- 每条建议包含: rule_type, name, pattern, severity (high/mid/low), description, rationale (为什么建议)\n"
        "- 只输出**新增**建议（不输出已有规则）\n"
        "- 最多 5 条，按价值降序\n"
        "- 如果没有发现需要新加的规则，返回空数组 []\n\n"
        "## 规则类型枚举：\n"
        "- forbid_phrase (pattern=词组)\n"
        "- max_para_chars (pattern=数字)\n"
        "- max_dialogue_lines (pattern=数字)\n"
        "- min_sentence_chars (pattern=数字)\n"
        "- max_sentence_chars (pattern=数字)\n\n"
        f"## 已有规则（不要重复建议）\n{existing_summary}\n\n"
        f"## 章节样本（{len(samples)} 章 × 前 1500 字）\n\n" + "\n\n".join(samples)
    )
    if style_focus:
        user += f"\n\n## 编辑额外关注点：{style_focus}"
    system = "你是一位长篇小说资深编辑。输出严格 JSON，不要任何额外文字。"
    ai = AIClient()
    if not ai.ready:
        raise HTTPException(503, "AI 未配置 .env")
    try:
        raw = ai.chat(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.5,
            max_tokens=1500,
        )
        # 稳健 JSON 提取：优先用 json.loads 直接解析（处理 markdown fence）
        suggestions = _extract_json_array(raw)
        if suggestions is None:
            # 退化到旧正则
            m = re.search(r'\[.*\]', raw, re.DOTALL)
            if m:
                try:
                    suggestions = json.loads(m.group())
                except Exception:
                    suggestions = None
        if suggestions is None:
            return {"suggestions": [], "raw": raw, "error": "无法解析 JSON"}
        # 校验
        valid_types = {"forbid_phrase", "max_para_chars", "max_dialogue_lines", "min_sentence_chars", "max_sentence_chars"}
        out = []
        for s in suggestions:
            if not isinstance(s, dict):
                continue
            if s.get("rule_type") not in valid_types:
                continue
            out.append({
                "rule_type": s["rule_type"],
                "name": s.get("name", "未命名规则"),
                "pattern": str(s.get("pattern", "")),
                "severity": s.get("severity", "mid"),
                "description": s.get("description", ""),
                "rationale": s.get("rationale", ""),
            })
        return {"suggestions": out, "analyzed_chapters": len(samples)}
    except Exception as e:
        return {"suggestions": [], "error": str(e)}


# ============== 编辑批注 ==============

@router.get("/editor/chapter/{idx}/comments")
def api_list_chapter_comments(idx: int = ApiPath(ge=1, description="章节号, ≥1"), status: str = Query(None, description="过滤状态: open/resolved")) -> dict:
    """列出某章的批注"""
    db = get_db()
    ch = kb.get_chapter_by_idx(db, idx)
    if not ch:
        raise HTTPException(404)
    return {"comments": kb.list_comments(db, chapter_id=ch["id"], status=status)}


@router.post("/editor/chapter/{idx}/comments")
def api_add_chapter_comment(idx: int = ApiPath(ge=1, description="章节号, ≥1"), req: dict = Body(default_factory=dict)) -> dict:
    """在某章加一条批注"""
    db = get_db()
    ch = kb.get_chapter_by_idx(db, idx)
    if not ch:
        raise HTTPException(404)
    body = (req.get("body") or "").strip()
    if not body:
        raise HTTPException(400, "body 必填")
    snippet = (req.get("snippet") or "").strip()
    try:
        anchor_start = int(req.get("anchor_start") or 0)
        anchor_end = int(req.get("anchor_end") or 0)
    except (TypeError, ValueError):
        raise HTTPException(400, "anchor 必须是整数")
    if anchor_end < anchor_start:
        anchor_start, anchor_end = anchor_end, anchor_start
    cid = kb.add_comment(
        db,
        chapter_id=ch["id"],
        chapter_idx=idx,
        anchor_start=anchor_start,
        anchor_end=anchor_end,
        snippet=snippet,
        body=body,
        author=req.get("author", "editor"),
    )
    return {"id": cid}


@router.put("/editor/comments/{cid}")
def api_update_comment(cid: int = ApiPath(ge=1, description="批注ID, ≥1"), req: dict = Body(default_factory=dict)) -> dict:
    """更新批注（编辑正文 / 改状态）"""
    allowed = {"body", "status", "snippet"}
    fields = {k: v for k, v in req.items() if k in allowed}
    if not fields:
        return {"ok": True}
    kb.update_comment(get_db(), cid, **fields)
    return {"ok": True}


@router.delete("/editor/comments/{cid}")
def api_delete_comment(cid: int = ApiPath(ge=1, description="批注ID, ≥1")) -> dict:
    """删除批注"""
    kb.delete_comment(get_db(), cid)
    return {"ok": True}


@router.get("/editor/comments/summary")
def api_comments_summary() -> dict:
    """全本批注统计：每章 open/resolved 数 + 全本总数"""
    db = get_db()
    rows = db.query("SELECT chapter_idx, status, COUNT(*) AS cnt FROM editor_comment GROUP BY chapter_idx, status ORDER BY chapter_idx")
    by_chapter = {}
    for r in rows:
        r = dict(r)
        by_chapter.setdefault(r["chapter_idx"], {"open": 0, "resolved": 0, "total": 0})
        by_chapter[r["chapter_idx"]][r["status"]] = r["cnt"]
        by_chapter[r["chapter_idx"]]["total"] += r["cnt"]
    total_open = sum(c.get("open", 0) for c in by_chapter.values())
    total_all = sum(c.get("total", 0) for c in by_chapter.values())
    return {
        "by_chapter": by_chapter,
        "total_open": total_open,
        "total_resolved": total_all - total_open,
        "total": total_all,
    }


# ============== 角色声音分析 ==============

# 说话动词（用于"X 动"识别说话人）
_VOICE_VERBS = (
    "道|说|问|答|笑|叹|怒|喊|叫|嚷|应|接|讲|话|听|答|默|吼|低语|"
    "笑道|怒道|叹道|问道|答道|喊道|叫道|嚷道|应道|接道|讲道|答道|"
    "低声道|沉声道|轻声道|朗声道|冷声道|缓声道|厉声道|笑道|摇摇头|点点头|"
    "开口|忍不住|低声|高声|正色|严肃|慢慢|轻轻|缓缓|突然|冷冷|淡淡|默默|"
    "冷笑|微笑|苦笑|叹了口气|摇了摇头|点了点头"
)
# 引号字符（中文 + 英文）
_QUOTE_CHARS = "\u201c\u201d\"'「」『』"
# 人称代词 + 模糊指代 + 常见短语（fallback 时不当人名）
_PRONOUNS = {
    # 人称代词
    "我", "你", "他", "她", "它", "我们", "你们", "他们", "她们", "它们", "自己",
    "谁", "什么", "哪", "这", "那", "这", "那", "这个", "那个", "这些", "那些",
    # 模糊指代
    "老人家", "老爷子", "老太太", "老头", "老夫人", "小姑娘", "小伙子",
    "小家伙", "小毛孩", "老大爷", "老头子", "老太婆", "那位", "这位",
    "经理", "先生", "女士", "同事", "老师", "同学", "顾客", "司机",
    # 4 字常见短语
    "怎么会", "怎么不", "不知道", "不清楚", "不记得", "没想到", "我怎么会",
    "我不知", "我不知道", "我怎么会", "我不",
    "你以为", "你觉得", "他认为", "她认为", "我觉得", "我想", "我想说",
    "话说", "却说", "只说", "他说", "她说", "我说", "你说",
    # 常见短语 / 副词
    "然后", "接着", "随后", "突然", "忽然", "立刻", "马上",
    "正常", "正常来", "有回", "有个", "有次", "有时",
    "第一", "第一个", "第一", "第二", "第三",
    "我先", "他先", "我先说", "他先说",
    # 否定
    "没有", "没人", "没法", "没事", "没想", "没什",
    "没有", "没什", "没什", "没什",
    # 短句（fallback 容易被误抓的 2-3 字）
    "正常", "正常来", "我说说", "正常", "我说", "你说", "她说", "他说",
    "这样", "这样", "这样", "就这样", "于是", "最后", "终于",
    "然后", "接着", "于是", "于是", "于是", "于是",
    # 助词 / 语气
    "我觉", "我想", "我真", "我竟", "我恍", "我忽", "我猛", "我豁",
    "他觉", "他想", "他真", "他竟", "他恍", "他忽", "他猛", "他豁",
    "她觉", "她想", "她真", "她竟", "她恍", "她忽", "她猛", "她豁",
    "你觉", "你想", "你真", "你竟", "你恍", "你忽", "你猛", "你豁",
    "其实", "其实就", "其实", "其实", "其实", "其实", "其实", "其实",
    "其实", "其实", "其实", "其实", "其实", "其实", "其实", "其实",
    "不", "不", "不", "不", "不", "不", "不", "不", "不", "不", "不",
}


def _extract_dialogues(text: str, known_names: list = None) -> list:
    """从文本中提取对话段，启发式归类说话人。
    返回 [{start, end, text, speaker, speaker_method}] 列表
    """
    # 找所有引号包裹的对话段
    pattern = re.compile(f"[{_QUOTE_CHARS}]([^{_QUOTE_CHARS}\\n]{{2,300}})[{_QUOTE_CHARS}]")
    dialogues = []
    # 名字优先级：长名 > 短名（避免"王"匹配到"王老板"前面的"老"）
    sorted_names = sorted(known_names or [], key=lambda n: -len(n)) if known_names else []
    name_alt = "|".join(re.escape(n) for n in sorted_names) if sorted_names else None
    # 说话动词模式（前面是名字或代词）
    speaker_pattern = re.compile(
        rf"(?:^|[，。；：、\s])({name_alt or '[\\u4e00-\\u9fa5]{2,4}'})({_VOICE_VERBS})[，：。\s]",
        re.MULTILINE
    ) if name_alt else None
    # 通用兜底：要求前面是标点/句首，避免把"我不知道"误识别
    # 注意：限制名字 2-3 字（4 字中文人名极少，复姓如"令狐冲"是 3 字不是 4）
    fallback_speaker = re.compile(
        rf"(?:^|[，。；：、\s])([\u4e00-\u9fa5]{{2,3}})({_VOICE_VERBS})[，：。\s]",
        re.MULTILINE,
    )

    for m in pattern.finditer(text):
        d_start, d_end = m.start(), m.end()
        d_text = m.group(1)
        speaker = None
        method = "none"
        look_start = max(0, d_start - 200)
        window = text[look_start:d_start]
        # 1) 先用已知名匹配
        if name_alt:
            last = None
            for sm in speaker_pattern.finditer(window):
                last = sm
            if last:
                cand = last.group(1)
                if cand not in _PRONOUNS:
                    speaker = cand
                    method = "known"
        # 2) 没匹配到 → 用 fallback 通用模式（找 2-4 字 + 说话动词）
        if not speaker:
            last = None
            for sm in fallback_speaker.finditer(window):
                last = sm
            if last:
                cand = last.group(1)
                if cand not in _PRONOUNS:
                    speaker = cand
                    method = "fallback"
        if not speaker:
            speaker = "叙述者"
            method = "narrator"
        # 把 known_names 里的别名归一化到正名
        if known_names and speaker != "叙述者":
            for n in known_names:
                if speaker == n or speaker in (n,):
                    speaker = n
                    break
        dialogues.append({
            "start": d_start,
            "end": d_end,
            "text": d_text,
            "speaker": speaker,
            "method": method,
            "char_count": len(d_text),
        })
    return dialogues


def _aggregate_by_speaker(dialogues: list, characters: list, min_count: int = 2) -> list:
    """按角色聚合对话。单次出现的临时说话人合并到"叙述者"（避免误识别带来的噪声）"""
    char_by_name = {c["name"]: c for c in characters}
    # 也建一个 "all_names" 集合，用于把别名/叫法归一到正名
    name_to_char = {}
    for c in characters:
        name_to_char[c["name"]] = c
        for a in (c.get("aliases") or []):
            if a:
                name_to_char[a] = c
    # 聚合
    bucket = {}
    for d in dialogues:
        c = name_to_char.get(d["speaker"])
        canonical = c["name"] if c else (d["speaker"] if d["speaker"] == "叙述者" else d["speaker"])
        if canonical not in bucket:
            bucket[canonical] = {
                "name": canonical,
                "is_named_char": c is not None,
                "char_id": c["id"] if c else None,
                "dialogue_count": 0,
                "char_count": 0,
                "samples": [],
            }
        b = bucket[canonical]
        b["dialogue_count"] += 1
        b["char_count"] += d["char_count"]
        if len(b["samples"]) < 3:
            b["samples"].append(d["text"][:60])
    # 过滤：单次出现的非已登记角色合并到叙述者
    out = []
    for b in bucket.values():
        if b["dialogue_count"] < min_count and not b["is_named_char"]:
            # 合并到"叙述者"（把字符数加到 叙述者 上, 但保留这个 bucket 不参与排序, 我们用特例）
            out.append({**b, "_merge_to_narrator": True})
        else:
            out.append(b)
    # 把 _merge_to_narrator 的字数和段数累加到 "叙述者"
    narrator = next((b for b in out if b["name"] == "叙述者"), None)
    for b in out:
        if b.get("_merge_to_narrator"):
            if narrator is None:
                narrator = {
                    "name": "叙述者", "is_named_char": False, "char_id": None,
                    "dialogue_count": 0, "char_count": 0, "samples": [],
                }
                out.append(narrator)
            narrator["dialogue_count"] += b["dialogue_count"]
            narrator["char_count"] += b["char_count"]
    out = [b for b in out if not b.get("_merge_to_narrator")]
    out.sort(key=lambda b: -b["char_count"])
    return out


@router.get("/voice-analysis")
def api_voice_analysis(chapter: int = Query(None, ge=1, description="章节号, None=全本, ≥1=单章")) -> dict:
    """角色声音分析：对话量分布
    chapter=None → 全本聚合
    chapter=N → 单章
    """
    db = get_db()
    characters = kb.list_characters(db)
    known_names = [c["name"] for c in characters] + sum([c.get("aliases") or [] for c in characters], [])
    chapters = kb.list_chapters(db)
    if chapter is not None:
        ch = kb.get_chapter_by_idx(db, chapter)
        if not ch:
            raise HTTPException(404)
        text = ch.get("final_text") or ch.get("draft") or ""
        dialogues = _extract_dialogues(text, known_names)
        agg = _aggregate_by_speaker(dialogues, characters)
        return {
            "scope": "chapter",
            "chapter_idx": chapter,
            "chapter_title": ch.get("title", f"第{chapter}回"),
            "dialogue_total": len(dialogues),
            "dialogue_chars": sum(d["char_count"] for d in dialogues),
            "by_speaker": agg,
            "characters_total": len(characters),
        }
    # 全本聚合
    all_dialogues = []
    for c in chapters:
        text = c.get("final_text") or c.get("draft") or ""
        if not text:
            continue
        dials = _extract_dialogues(text, known_names)
        for d in dials:
            d["chapter_idx"] = c["idx"]
        all_dialogues.extend(dials)
    agg = _aggregate_by_speaker(all_dialogues, characters)
    # 按章节 × 角色的二维矩阵
    # 预建 name_to_char 映射（只建一次，不要在循环里重建 O(n*m) 次）
    name_to_char = {c["name"]: c["name"] for c in characters}
    for c in characters:
        for a in (c.get("aliases") or []):
            if a:
                name_to_char[a] = c["name"]
    char_names_set = {c["name"] for c in characters}
    matrix = {}  # char_name -> {chapter_idx: dialogue_count}
    for d in all_dialogues:
        ch_idx = d.get("chapter_idx")
        if ch_idx is None:
            continue
        cn = name_to_char.get(d["speaker"], d["speaker"] if d["speaker"] == "叙述者" else d["speaker"])
        if cn not in matrix:
            matrix[cn] = {"name": cn, "is_named_char": cn in char_names_set, "chapters": {}}
        matrix[cn]["chapters"][ch_idx] = matrix[cn]["chapters"].get(ch_idx, 0) + 1
    return {
        "scope": "all",
        "chapters_total": len(chapters),
        "dialogue_total": len(all_dialogues),
        "dialogue_chars": sum(d["char_count"] for d in all_dialogues),
        "by_speaker": agg,
        "matrix": list(matrix.values()),
        "characters_total": len(characters),
    }


@router.post("/voice-analysis/distinguish")
def api_voice_distinguish(req: dict) -> dict:
    """LLM 评判：选 2-N 个角色 + 它们在全书的对话样本 → 评"说话像不像同一个人"（0-100）+ 反馈"""
    chapter_idx = req.get("chapter_idx")  # None = 全本
    character_ids = req.get("character_ids") or []
    # B-新111: 限 ≤ 8 角色, 防 LLM 调浪费钱
    if not isinstance(character_ids, list):
        raise HTTPException(400, "character_ids 必须是 list")
    if len(character_ids) > 8:
        raise HTTPException(400, f"最多 8 个角色 (收到 {len(character_ids)})")
    # B-新111: chapter_idx 强校验 (之前 -1 走全本, 误导)
    if chapter_idx is not None:
        if not isinstance(chapter_idx, int) or chapter_idx < 1:
            raise HTTPException(422, f"chapter_idx 必须 ≥1 或 None, 收到 {chapter_idx!r}")
    db = get_db()
    characters = kb.list_characters(db)
    chars_by_id = {c["id"]: c for c in characters}
    selected = [chars_by_id[i] for i in character_ids if i in chars_by_id]
    if len(selected) < 2:
        raise HTTPException(400, "至少选 2 个角色")
    # 收集每个角色的对话样本
    known_names = [c["name"] for c in characters] + sum([c.get("aliases") or [] for c in characters], [])
    if chapter_idx is not None:
        ch = kb.get_chapter_by_idx(db, chapter_idx)
        if not ch:
            raise HTTPException(404)
        text = ch.get("final_text") or ch.get("draft") or ""
        all_dialogues = _extract_dialogues(text, known_names)
    else:
        all_dialogues = []
        for c in kb.list_chapters(db):
            text = c.get("final_text") or c.get("draft") or ""
            if text:
                all_dialogues.extend(_extract_dialogues(text, known_names))
    name_to_char = {c["name"]: c for c in characters}
    for c in characters:
        for a in (c.get("aliases") or []):
            if a:
                name_to_char[a] = c
    # 按角色聚合样本（最多 8 段 × 80 字）
    samples_by_char = {c["name"]: [] for c in selected}
    for d in all_dialogues:
        c = name_to_char.get(d["speaker"])
        if not c:
            continue
        if c["id"] not in character_ids:
            continue
        if len(samples_by_char[c["name"]]) >= 8:
            continue
        samples_by_char[c["name"]].append(d["text"][:80])
    # 检查是否有角色没样本
    empty = [c["name"] for c in selected if not samples_by_char[c["name"]]]
    if empty:
        return {
            "score": None,
            "feedback": f"以下角色在文本中找不到对话样本：{', '.join(empty)}。可能这章他们没出场。",
            "samples_by_char": samples_by_char,
            "selected": [{"id": c["id"], "name": c["name"], "aliases": c.get("aliases") or []} for c in selected],
        }
    # 拼 prompt
    char_blocks = []
    for c in selected:
        samps = samples_by_char[c["name"]]
        char_blocks.append(f"### {c['name']}（{c.get('role','')}；{c.get('mbti','')}）\n设定：{(c.get('basic_info','') or '')[:80]}\n对话样本：\n" + "\n".join(f"  {i+1}. \"{s}\"" for i, s in enumerate(samps)))
    system = (
        "你是一位资深文学编辑，专门分析长篇小说中角色对话的'声音区分度'。\n"
        "你的任务：判断这几个角色说话是不是'像同一个人'，评分 0-100（100=完全不同，0=完全一样）。\n"
        "评分维度：\n"
        "1. 用词习惯（口头禅、专业术语、年代感）\n"
        "2. 句式结构（长句/短句、倒装、省略）\n"
        "3. 情感表达（直白/含蓄、爆裂/冷静）\n"
        "4. 思维模式（逻辑型/跳跃型/自省型）\n\n"
        "输出严格 JSON 格式（不要任何额外文字）：\n"
        '{"score": <0-100 int>, "feedback": "<3-5 句具体反馈，指出谁最像谁、最像哪个维度、最需要差异化>", "pairs": [{"a":"<名字>","b":"<名字>","similarity":<0-100 int>,"note":"<1 句>"}]}'
    )
    user = f"## 角色对话样本\n\n" + "\n\n".join(char_blocks) + "\n\n## 请输出 JSON"
    ai = AIClient()
    if not ai.ready:
        raise HTTPException(503, "AI 未配置 .env")
    try:
        raw = ai.chat(
            messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            temperature=0.4,
            max_tokens=1200,
        )
        # 解析 JSON（可能被 ```json 包裹）
        data = _extract_json_object(raw)
        if data is None:
            # 退化到旧正则
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if m:
                try:
                    data = json.loads(m.group())
                except Exception:
                    data = None
        if data is None:
            return {"raw": raw, "error": "无法解析 JSON", "samples_by_char": samples_by_char, "selected": [{"id": c["id"], "name": c["name"], "aliases": c.get("aliases") or []} for c in selected]}
        return {
            "score": data.get("score"),
            "feedback": data.get("feedback", ""),
            "pairs": data.get("pairs", []),
            "samples_by_char": samples_by_char,
            "selected": [{"id": c["id"], "name": c["name"], "aliases": c.get("aliases") or []} for c in selected],
        }
    except Exception as e:
        return {"error": str(e), "samples_by_char": samples_by_char, "selected": [{"id": c["id"], "name": c["name"], "aliases": c.get("aliases") or []} for c in selected]}


def _extract_json_object(raw: str) -> dict | None:
    """稳健 JSON 对象提取：先尝试去 markdown fence 后整体 json.loads，再括号计数匹配"""
    # 去掉 markdown fence
    stripped = re.sub(r'```(?:json)?\s*\n?(.*?)\n?```', r'\1', raw, flags=re.DOTALL).strip()
    try:
        return json.loads(stripped)
    except Exception:
        pass
    # 括号计数匹配第一个完整 JSON 对象
    start = raw.find('{')
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(raw)):
        c = raw[i]
        if escape_next:
            escape_next = False
            continue
        if c == '\\':
            escape_next = True
            continue
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '{':
            depth += 1
        elif c == '}':
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(raw[start:i+1])
                except Exception:
                    return None
    return None


def _extract_json_array(raw: str) -> list | None:
    """稳健 JSON 数组提取：先 markdown fence，再括号计数匹配"""
    stripped = re.sub(r'```(?:json)?\s*\n?(.*?)\n?```', r'\1', raw, flags=re.DOTALL).strip()
    try:
        result = json.loads(stripped)
        if isinstance(result, list):
            return result
    except Exception:
        pass
    start = raw.find('[')
    if start < 0:
        return None
    depth = 0
    in_string = False
    escape_next = False
    for i in range(start, len(raw)):
        c = raw[i]
        if escape_next:
            escape_next = False
            continue
        if c == '\\':
            escape_next = True
            continue
        if c == '"' and not escape_next:
            in_string = not in_string
            continue
        if in_string:
            continue
        if c == '[':
            depth += 1
        elif c == ']':
            depth -= 1
            if depth == 0:
                try:
                    result = json.loads(raw[start:i+1])
                    if isinstance(result, list):
                        return result
                except Exception:
                    return None
    return None


def _check_style_rules(text: str, rules: list) -> list:
    """对照一组规则检查文本，返回违例列表"""
    violations = []
    paragraphs = [p for p in re.split(r'\n\s*\n', text.strip()) if p.strip()]
    for rule in rules:
        rt = rule["rule_type"]
        sev = rule.get("severity") or "mid"
        name = rule["name"]
        desc = rule.get("description") or ""
        if rt == "forbid_phrase":
            phrase = (rule.get("pattern") or "").strip()
            if not phrase:
                continue
            for i, p in enumerate(paragraphs):
                if phrase in p:
                    # 找到上下文
                    idx = p.find(phrase)
                    ctx_start = max(0, idx - 20)
                    ctx_end = min(len(p), idx + len(phrase) + 20)
                    ctx = p[ctx_start:ctx_end].replace("\n", " ")
                    violations.append({
                        "rule_id": rule["id"],
                        "rule_name": name,
                        "rule_type": rt,
                        "severity": sev,
                        "paragraph_idx": i,
                        "phrase": phrase,
                        "context": ("..." if ctx_start > 0 else "") + ctx + ("..." if ctx_end < len(p) else ""),
                    })
        elif rt == "max_para_chars":
            try:
                limit = int(rule.get("pattern") or "200")
            except ValueError:
                continue
            for i, p in enumerate(paragraphs):
                if len(p) > limit:
                    violations.append({
                        "rule_id": rule["id"],
                        "rule_name": name,
                        "rule_type": rt,
                        "severity": sev,
                        "paragraph_idx": i,
                        "para_len": len(p),
                        "limit": limit,
                        "preview": p[:30] + "...",
                    })
        elif rt == "max_dialogue_lines":
            # 简单判定：连续 6+ 字 + " 开头算对话段，统计连续对话段数
            try:
                limit = int(rule.get("pattern") or "3")
            except ValueError:
                continue
            dial_para_count = 0
            max_dial_run = 0
            current_run = 0
            for p in paragraphs:
                p_strip = p.strip()
                # 启发式：开引号 + 收引号 或 "开头
                if (p_strip.startswith('"') or p_strip.startswith('"') or p_strip.startswith("「")
                    or p_strip.startswith("『")):
                    current_run += 1
                    max_dial_run = max(max_dial_run, current_run)
                else:
                    current_run = 0
            if max_dial_run > limit:
                violations.append({
                    "rule_id": rule["id"],
                    "rule_name": name,
                    "rule_type": rt,
                    "severity": sev,
                    "max_run": max_dial_run,
                    "limit": limit,
                })
    return violations


@router.post("/style-rules/check")
def api_check_style_rules(req: dict) -> dict:
    """对照所有启用的规则检查一个章节文本（用于编辑器实时检查）"""
    text = req.get("text", "")
    if not isinstance(text, str):
        raise HTTPException(400, "text 必须是 str")
    # B-新127: 限 text 长度 ≤ 50MB, 防 OOM
    if len(text) > 50 * 1024 * 1024:
        raise HTTPException(413, f"text 超过 50MB ({len(text)} chars), 已拒绝")
    chapter_idx = req.get("chapter_idx")
    if chapter_idx is not None and (not isinstance(chapter_idx, int) or chapter_idx < 1):
        raise HTTPException(422, f"chapter_idx 必须 ≥1 或 None, 收到 {chapter_idx!r}")
    rules = [r for r in kb.list_style_rules(get_db(), enabled_only=True)]
    violations = _check_style_rules(text, rules)
    return {"violations": violations, "rule_count": len(rules)}


def _build_style_rules_prompt(rules: list) -> str:
    """把风格规则转成 LLM 看的 prompt 段"""
    if not rules:
        return ""
    lines = ["## 编辑设定的写作规则（必须遵守）"]
    for r in rules:
        desc = r.get("description") or ""
        if r["rule_type"] == "forbid_phrase":
            lines.append(f"- 🚫 禁用词组：`{r.get('pattern') or ''}`" + (f"（{desc}）" if desc else ""))
        elif r["rule_type"] == "max_para_chars":
            lines.append(f"- 📏 单段最长 {r.get('pattern') or '?'} 字" + (f"（{desc}）" if desc else ""))
        elif r["rule_type"] == "max_dialogue_lines":
            lines.append(f"- 💬 连续对话不超过 {r.get('pattern') or '?'} 段" + (f"（{desc}）" if desc else ""))
        elif r["rule_type"] == "min_sentence_chars":
            lines.append(f"- 📏 句子至少 {r.get('pattern') or '?'} 字" + (f"（{desc}）" if desc else ""))
        elif r["rule_type"] == "max_sentence_chars":
            lines.append(f"- 📏 句子最多 {r.get('pattern') or '?'} 字" + (f"（{desc}）" if desc else ""))
    return "\n".join(lines)


def _smart_text_preview(text: str, instruction: str = "", max_chars: int = 6000) -> str:
    """智能截取长文本预览：保留开头 + 结尾 + 用户指令相关段。

    优于粗暴的 [:max_chars]——后者会砍掉后文，导致 plan 模式看不到章末的伏笔/承接。
    策略：总长 ≤ max_chars 直接返回；否则取 head(max_chars*0.5) + tail(max_chars*0.4)，
    中间若 instruction 出现在某段则额外保留该段（让 AI 看到"要改的位置"）。
    """
    if not text or len(text) <= max_chars:
        return text or ""
    head_n = int(max_chars * 0.5)
    tail_n = int(max_chars * 0.4)
    head = text[:head_n]
    tail = text[-tail_n:]
    # 尝试在中间区域找用户指令关键词所在的段落（提升定位准确性）
    middle_slice = ""
    if instruction:
        # 提取指令里较长的词作为锚点（≥3 字符，取前 3 个）
        import re as _re_sp
        anchors = [w for w in _re_sp.split(r"[\s,，。；;、的]", instruction) if len(w) >= 3][:3]
        mid_start = head_n
        mid_end = len(text) - tail_n
        mid_region = text[mid_start:mid_end]
        for anchor in anchors:
            idx = mid_region.find(anchor)
            if idx >= 0:
                # 取该锚点前后各 300 字
                s = max(0, idx - 300)
                e = min(len(mid_region), idx + 300)
                middle_slice = mid_region[s:e]
                break
    parts = [head]
    if middle_slice:
        parts.append("\n…（中段，含用户指令相关内容）…\n" + middle_slice)
    parts.append("\n…（省略中段）…\n" + tail)
    return "".join(parts)


# ============== 审稿进度看板 ==============

@router.get("/editor/review-status")
def api_review_status() -> dict:
    """每章审稿状态：未改 / 编辑已审 / 终审通过 / 退改"""
    db = get_db()
    chapters = kb.list_chapters(db)
    chapters_status = []
    summary = {
        "total": len(chapters),
        "untouched": 0,
        "editor_reviewed": 0,
        "final_approved": 0,
        "rejected": 0,
    }
    for c in chapters:
        ch_id = c["id"]
        # 批注统计
        open_n = db.query_one(
            "SELECT COUNT(*) AS n FROM editor_comment WHERE chapter_id=? AND status='open'",
            (ch_id,),
        )["n"]
        resolved_n = db.query_one(
            "SELECT COUNT(*) AS n FROM editor_comment WHERE chapter_id=? AND status='resolved'",
            (ch_id,),
        )["n"]
        # 最近一致性报告
        cr = db.query_one(
            "SELECT * FROM consistency_report WHERE chapter_id=? ORDER BY id DESC LIMIT 1",
            (ch_id,),
        )
        # 最近 AI 评审
        rev = db.query_one(
            "SELECT * FROM chapter_review WHERE chapter_id=? ORDER BY id DESC LIMIT 1",
            (ch_id,),
        )
        # 章节文本
        final = (c.get("final_text") or "").strip()
        draft = (c.get("draft") or "").strip()
        untouched = (not final) or (final == draft)
        # 判定状态
        if untouched and open_n == 0 and resolved_n == 0:
            status = "untouched"
        elif open_n > 0 and not cr:
            status = "rejected"
        elif open_n == 0 and cr and cr["passed"]:
            status = "final_approved"
        elif resolved_n > 0:
            status = "editor_reviewed"
        else:
            status = "untouched"
        summary[status] = summary.get(status, 0) + 1
        # AI 评审 high 级问题数
        rev_high = None
        if rev and rev["issues"]:
            try:
                rev_issues = json.loads(rev["issues"])
                rev_high = sum(1 for it in rev_issues if it.get("severity") == "high")
            except Exception:
                rev_high = None
        chapters_status.append({
            "idx": c["idx"],
            "title": c.get("title", f"第{c['idx']}回"),
            "word_count": c.get("word_count") or len(final or draft),
            "status": status,
            "open_comments": open_n,
            "resolved_comments": resolved_n,
            "consistency_passed": bool(cr["passed"]) if cr else None,
            "consistency_at": cr["created_at"] if cr else None,
            "ai_review_score": round(rev["overall_score"], 1) if rev and rev["overall_score"] is not None else None,
            "ai_review_at": rev["created_at"] if rev else None,
            "ai_review_high": rev_high,
        })
    return {"summary": summary, "chapters": chapters_status}


# ============== AI 评审（审稿看板单章） ==============

@router.get("/editor/chapter/{idx}/ai-review")
def api_get_ai_review(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> dict:
    """取该章最近一次 AI 评审结果"""
    db = get_db()
    ch = kb.get_chapter_by_idx(db, idx)
    if not ch:
        raise HTTPException(404, "章节不存在")
    rev = db.query_one(
        "SELECT * FROM chapter_review WHERE chapter_id=? ORDER BY id DESC LIMIT 1",
        (ch["id"],),
    )
    if not rev:
        return {"ok": True, "review": None}
    return {
        "ok": True,
        "review": {
            "overall_score": round(rev["overall_score"], 1) if rev["overall_score"] is not None else None,
            "overall_comment": rev["overall_comment"],
            "dimensions": _safe_json_list(rev["dimensions"]),
            "strengths": _safe_json_list(rev["strengths"]),
            "issues": _safe_json_list(rev["issues"]),
            "suggestions": _safe_json_list(rev["suggestions"]),
            "created_at": rev["created_at"],
        },
    }


@router.post("/editor/chapter/{idx}/ai-review")
def api_run_ai_review(idx: int = ApiPath(ge=1, description="章节号, ≥1")) -> dict:
    """对单章跑一次 AI 多维度评审，结果入库"""
    db = get_db()
    ch = kb.get_chapter_by_idx(db, idx)
    if not ch:
        raise HTTPException(404, "章节不存在")
    text = (ch.get("final_text") or ch.get("draft") or "").strip()
    if not text:
        raise HTTPException(400, "章节无正文，无法评审")
    try:
        ctx = retriever.build_consistency_context(db, idx, text)
        characters = "\n\n".join(filter(None, [
            ctx.get("pov_profile"),
            ctx.get("other_characters_profiles"),
        ]))
        user = prompts.AI_REVIEW_USER_TEMPLATE.format(
            style=ctx.get("style") or "未设定",
            synopsis=ctx.get("synopsis") or "（无梗概）",
            outline=ctx.get("outline") or "（无大纲）",
            chapter_text=text,
            characters=characters or "（无人物档案）",
            threads=ctx.get("relevant_threads") or "（无）",
        )
        messages = [
            {"role": "system", "content": prompts.AI_REVIEW_SYSTEM},
            {"role": "user", "content": user},
        ]
        data = AIClient().chat_json(messages, temperature=0.3, model=CONFIG.ai.mini_model)
        if not isinstance(data, dict):
            raise AICallError("评审模型未返回结构化 JSON")
        # 归一化字段
        dims = data.get("dimensions") or []
        if isinstance(dims, list):
            for d in dims:
                d["score"] = max(0, min(10, float(d.get("score", 0))))
        score = float(data.get("overall_score", 0) or 0)
        score = max(0, min(100, round(score, 1)))
        db.execute(
            "INSERT INTO chapter_review(chapter_id, overall_score, overall_comment, dimensions, strengths, issues, suggestions, raw_response, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                ch["id"],
                score,
                (data.get("overall_comment") or "").strip(),
                json.dumps(dims, ensure_ascii=False),
                json.dumps(data.get("strengths") or [], ensure_ascii=False),
                json.dumps(data.get("issues") or [], ensure_ascii=False),
                json.dumps(data.get("suggestions") or [], ensure_ascii=False),
                json.dumps(data, ensure_ascii=False),
                time.time(),
            ),
        )
        return {
            "ok": True,
            "review": {
                "overall_score": score,
                "overall_comment": (data.get("overall_comment") or "").strip(),
                "dimensions": dims,
                "strengths": data.get("strengths") or [],
                "issues": data.get("issues") or [],
                "suggestions": data.get("suggestions") or [],
                "created_at": time.time(),
            },
        }
    except Exception as e:
        log_exception("ai-review", e)
        raise HTTPException(502, f"AI 评审失败: {friendly_hint(e)}") from e


def _safe_json_list(raw) -> list:
    """json 字段安全解析，失败返回空列表"""
    if raw is None:
        return []
    if isinstance(raw, list):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return []


# ============== 工作区管理（每本小说独立数据库） ==============

@router.get("/workspaces")
def api_list_workspaces() -> dict:
    """列出所有工作区，含章节数/字数统计"""
    from novelai.config import list_workspaces, get_current_workspace_id
    wss = list_workspaces()
    cur = get_current_workspace_id()
    for ws in wss:
        ws["is_current"] = (ws["id"] == cur)
        # 读章节数/字数
        try:
            from novelai.db import Database
            db = Database(ws["db_path"])
            row = db.query_one("SELECT COUNT(*) AS n, COALESCE(SUM(word_count),0) AS w FROM chapter")
            ws["chapter_count"] = row["n"] if row else 0
            ws["word_count"] = row["w"] if row else 0
        except Exception:
            ws["chapter_count"] = 0
            ws["word_count"] = 0
    return {"workspaces": wss, "current": cur}


@router.get("/workspaces/current")
def api_current_workspace() -> dict:
    from novelai.config import get_current_workspace_id, get_current_db_path
    ws_id = get_current_workspace_id()
    return {"id": ws_id, "db_path": str(get_current_db_path())}


@router.post("/workspaces/create")
def api_create_workspace(req: dict = Body(default_factory=dict)) -> dict:
    """建新工作区 + 切换 + 初始化 project"""
    from novelai.config import create_workspace
    title = (req.get("title") or "未命名小说").strip()
    info = create_workspace(title)
    # 切换 CONFIG.db_path + 重置缓存
    CONFIG.db_path = Path(info["db_path"])
    reset_db()
    # 初始化 project 表
    db = get_db()
    kb.get_or_create_project(db)
    kb.update_project(db, title=title)
    return {"ok": True, "id": info["id"], "db_path": info["db_path"]}


@router.post("/workspaces/switch")
def api_switch_workspace(req: dict = Body(default_factory=dict)) -> dict:
    """切换当前工作区"""
    from novelai.config import switch_workspace as _switch
    ws_id = req.get("id")
    if not ws_id:
        raise HTTPException(400, "id required")
    try:
        new_path = _switch(ws_id)
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    CONFIG.db_path = Path(new_path)
    reset_db()
    return {"ok": True, "id": ws_id}


@router.delete("/workspaces/{ws_id}")
def api_delete_workspace(ws_id: str) -> dict:
    from novelai.config import delete_workspace as _delete
    try:
        _delete(ws_id)
    except (ValueError, FileNotFoundError) as e:
        raise HTTPException(400, str(e))
    return {"ok": True}
