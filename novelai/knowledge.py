"""
novelai.knowledge
知识库 CRUD：人物、世界观、关系、事实、章节、事件、伏笔。

所有方法都是显式的"显式操作"——LLM 不会自动修改它，
必须由一致性检查器或用户在确认后调用 update_* 方法。
这是为了避免 AI 幻觉污染知识库。
"""
from __future__ import annotations
import json
import time
import logging
from typing import Any
from .db import Database
from . import version_patch as vp

_log = logging.getLogger(__name__)


# ============== Status 关键词（统一维护，供 scanner/logic 复用） ==============
# 死亡状态关键词：character.status 含这些子串即视为"已死"，用于死人复活检测
DEAD_KEYWORDS: tuple[str, ...] = ("死", "亡", "殒", "逝")
# 失踪状态关键词
MISSING_KEYWORDS: tuple[str, ...] = ("失踪", "消失", "下落不明")
# status 规范白名单（防 AI 写"挂了/嗝屁"等变体导致子串匹配漏）
STATUS_WHITELIST: tuple[str, ...] = ("活", "已死", "死亡", "失踪", "重伤", "残废", "存活")


def is_dead_status(status: str | None) -> bool:
    """判断 status 是否表示角色已死亡（供 scanner/logic 和抽取流水线共用）。"""
    if not status:
        return False
    return any(k in status for k in DEAD_KEYWORDS)


def is_missing_status(status: str | None) -> bool:
    """判断 status 是否表示角色失踪。"""
    if not status:
        return False
    return any(k in status for k in MISSING_KEYWORDS)


# ============== Project ==============

def get_or_create_project(db: Database) -> dict:
    row = db.query_one("SELECT * FROM project ORDER BY id LIMIT 1")
    if row:
        return dict(row)
    now = Database.now()
    db.insert(
        "INSERT INTO project(title, synopsis, style, pov_mode, created_at) VALUES(?,?,?,?,?)",
        ("未命名小说", "", "", "限知视角", now),
    )
    row = db.query_one("SELECT * FROM project ORDER BY id LIMIT 1")
    if not row:
        raise RuntimeError("无法创建或查找项目记录（数据库可能损坏或磁盘已满）")
    return dict(row)


def _invalidate_retriever_cache() -> None:
    """数据写入后清检索器上下文缓存（避免生成时读到旧的人物/事实/设定）。
    延迟 import 规避 retriever→knowledge 的循环依赖。"""
    try:
        from . import retriever
        retriever.invalidate_cache()
    except Exception:
        pass  # 缓存清理失败不影响写入


def update_project(db: Database, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values())
    db.execute(f"UPDATE project SET {cols} WHERE id=(SELECT id FROM project LIMIT 1)", vals)
    _invalidate_retriever_cache()


# ============== Volume ==============

def add_volume(
    db: Database,
    idx: int,
    title: str,
    synopsis: str = "",
    style_notes: str = "",
) -> int:
    now = Database.now()
    return db.insert(
        """INSERT INTO volume(idx, title, synopsis, style_notes, created_at)
           VALUES(?,?,?,?,?)""",
        (idx, title, synopsis, style_notes, now),
    )


def get_volume(db: Database, volume_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM volume WHERE id=?", (volume_id,))
    return dict(row) if row else None


def get_volume_by_idx(db: Database, idx: int) -> dict | None:
    row = db.query_one("SELECT * FROM volume WHERE idx=?", (idx,))
    return dict(row) if row else None


def list_volumes(db: Database) -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM volume ORDER BY idx")]


def update_volume(db: Database, volume_id: int, **fields: Any) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [volume_id]
    db.execute(f"UPDATE volume SET {cols} WHERE id=?", vals)


# ============== Book Summary（分层记忆 L3） ==============

def get_book_summary(db: Database) -> dict | None:
    """读最新一条全书摘要"""
    return db.query_one("SELECT * FROM book_summary ORDER BY id DESC LIMIT 1")


def save_book_summary(db: Database, summary: str, chapter_range: str = "") -> int:
    """写入新的全书摘要（保留历史版本，读时取最新）"""
    return db.insert(
        "INSERT INTO book_summary(summary, chapter_range, updated_at) VALUES(?,?,?)",
        (summary, chapter_range, Database.now()),
    )


# ============== Character ==============

def add_character(
    db: Database,
    name: str,
    aliases: list[str] | None = None,
    role: str = "supporting",
    basic_info: str = "",
    personality: str = "",
    speech_style: str = "",
    abilities: str = "",
    arc: str = "",
    status: str = "",
    mbti: str = "",
    cognitive_stack: str = "",
    enneagram: str = "",
    arc_type: str = "",
    arc_progress: float | None = None,
    baseline_keywords: list[str] | None = None,
    extra: dict | None = None,
) -> int:
    # 查重：name 已存在则返回已有 id（防重复入库，尤其抽取流水线自动建小人物时）
    existing = find_character_by_name(db, name)
    if existing:
        return existing["id"]
    now = Database.now()
    cid = db.insert(
        """INSERT INTO character(name, aliases, role, basic_info, personality,
                                  speech_style, abilities, arc, status,
                                  mbti, cognitive_stack, enneagram, arc_type, arc_progress,
                                  baseline_keywords, extra)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (
            name,
            Database.to_json(aliases or []),
            role,
            basic_info,
            personality,
            speech_style,
            abilities,
            arc,
            status,
            mbti,
            cognitive_stack,
            enneagram,
            arc_type,
            arc_progress if arc_progress is not None else 0.0,
            Database.to_json(baseline_keywords or []),
            Database.to_json(extra or {}),
        ),
    )
    _invalidate_retriever_cache()
    return cid


def get_character(db: Database, char_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM character WHERE id=?", (char_id,))
    if not row:
        return None
    d = dict(row)
    d["aliases"] = Database.from_json(d.get("aliases")) or []
    d["extra"] = Database.from_json(d.get("extra")) or {}
    return d


def find_character_by_name(db: Database, name: str) -> dict | None:
    row = db.query_one("SELECT * FROM character WHERE name=? LIMIT 1", (name,))
    if not row:
        return None
    return get_character(db, row["id"])


def build_name_to_id_map(db: Database, include_aliases: bool = True) -> dict[str, int]:
    """构建 name→id 映射（含别名），供抽取流水线的 participants 映射用。

    修复 bug #12：旧 char_name_to_id 只用 c["name"]，不含 aliases，
    导致 LLM 输出别名时 participants 被丢弃。
    """
    mapping: dict[str, int] = {}
    for c in list_characters(db):
        mapping[c["name"]] = c["id"]
        if include_aliases:
            for a in (c.get("aliases") or []):
                if a and a not in mapping:
                    mapping[a] = c["id"]
    return mapping


def list_characters(db: Database) -> list[dict]:
    rows = db.query("SELECT * FROM character ORDER BY id")
    out = []
    for r in rows:
        d = dict(r)
        d["aliases"] = Database.from_json(d.get("aliases")) or []
        d["extra"] = Database.from_json(d.get("extra")) or {}
        d["baseline_keywords"] = Database.from_json(d.get("baseline_keywords")) or []
        out.append(d)
    return out


# ============== Style Rule (风格指南规则) ==============

_ALLOWED_RULE_TYPES = {"forbid_phrase", "max_para_chars", "max_dialogue_lines", "min_sentence_chars", "max_sentence_chars"}
_ALLOWED_SEVERITY = {"high", "mid", "low"}

def add_style_rule(
    db: Database,
    name: str,
    rule_type: str,
    pattern: str = None,
    severity: str = "mid",
    description: str = None,
    enabled: bool = True,
) -> int:
    # B-新126: 防御任意 rule_type/severity 写入 db
    if rule_type not in _ALLOWED_RULE_TYPES:
        raise ValueError(f"add_style_rule: rule_type 必须是 {_ALLOWED_RULE_TYPES}, 收到 {rule_type!r}")
    if severity not in _ALLOWED_SEVERITY:
        raise ValueError(f"add_style_rule: severity 必须是 {_ALLOWED_SEVERITY}, 收到 {severity!r}")
    now = time.time()
    return db.insert(
        """INSERT INTO style_rule(name, rule_type, pattern, severity, description, enabled, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (name, rule_type, pattern, severity, description, 1 if enabled else 0, now),
    )


def list_style_rules(db: Database, enabled_only: bool = False) -> list[dict]:
    sql = "SELECT * FROM style_rule ORDER BY id"
    if enabled_only:
        sql = "SELECT * FROM style_rule WHERE enabled=1 ORDER BY id"
    return [dict(r) for r in db.query(sql)]


def get_style_rule(db: Database, rule_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM style_rule WHERE id=?", (rule_id,))
    return dict(row) if row else None


def update_style_rule(db: Database, rule_id: int, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields.keys())
    vals = list(fields.values()) + [rule_id]
    db.execute(f"UPDATE style_rule SET {cols} WHERE id=?", vals)


def delete_style_rule(db: Database, rule_id: int) -> None:
    db.execute("DELETE FROM style_rule WHERE id=?", (rule_id,))


# ============== Editor Comment (编辑批注) ==============

def add_comment(
    db: Database,
    chapter_id: int,
    chapter_idx: int,
    anchor_start: int,
    anchor_end: int,
    snippet: str,
    body: str,
    author: str = "editor",
) -> int:
    now = time.time()
    return db.insert(
        """INSERT INTO editor_comment(chapter_id, chapter_idx, anchor_start, anchor_end, snippet, body, author, status, created_at, updated_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, 'open', ?, ?)""",
        (chapter_id, chapter_idx, anchor_start, anchor_end, snippet, body, author, now, now),
    )


def list_comments(db: Database, chapter_id: int = None, status: str = None) -> list[dict]:
    sql = "SELECT * FROM editor_comment"
    params = []
    conds = []
    if chapter_id is not None:
        conds.append("chapter_id=?")
        params.append(chapter_id)
    if status is not None:
        conds.append("status=?")
        params.append(status)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY chapter_idx, anchor_start, id"
    return [dict(r) for r in db.query(sql, params)]


def get_comment(db: Database, comment_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM editor_comment WHERE id=?", (comment_id,))
    return dict(row) if row else None


def update_comment(db: Database, comment_id: int, **fields) -> None:
    if not fields:
        return
    # B-新125: 防御 status 自由值 (允许 open/resolved)
    if "status" in fields:
        if fields["status"] not in ("open", "resolved"):
            raise ValueError(f"update_comment: status 必须是 open/resolved, 收到 {fields['status']!r}")
    # 防御锚点越界 (虽然 UI 上不会, 防 API 直接调)
    for k in ("anchor_start", "anchor_end"):
        if k in fields and not isinstance(fields[k], int):
            raise ValueError(f"update_comment: {k} 必须是 int, 收到 {fields[k]!r}")
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields.keys())
    vals = list(fields.values()) + [comment_id]
    db.execute(f"UPDATE editor_comment SET {cols} WHERE id=?", vals)


def delete_comment(db: Database, comment_id: int) -> None:
    db.execute("DELETE FROM editor_comment WHERE id=?", (comment_id,))


# ============== Character Milestone (成长里程碑) ==============

def add_milestone(
    db: Database,
    character_id: int,
    chapter_id: int,
    milestone_type: str,
    description: str,
    dimension: str = "personality",
    before_state: str = "",
    after_state: str = "",
    quote: str = "",
    importance: int = 3,
) -> int:
    return db.insert(
        """INSERT INTO character_milestone(character_id, chapter_id, milestone_type,
                                            dimension, description, before_state,
                                            after_state, quote, importance, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            character_id, chapter_id, milestone_type, dimension, description,
            before_state, after_state, quote, importance, Database.now(),
        ),
    )


def list_milestones(
    db: Database,
    character_id: int | None = None,
    chapter_id: int | None = None,
) -> list[dict]:
    sql = "SELECT * FROM character_milestone WHERE 1=1"
    params = []
    if character_id is not None:
        sql += " AND character_id=?"
        params.append(character_id)
    if chapter_id is not None:
        sql += " AND chapter_id=?"
        params.append(chapter_id)
    sql += " ORDER BY character_id, chapter_id, id"
    return [dict(r) for r in db.query(sql, params)]


# ============== Relationship Evolution (关系演变) ==============

def add_rel_evolution(
    db: Database,
    relationship_id: int,
    chapter_id: int,
    intimacy: float | None = None,
    trust: float | None = None,
    conflict: float | None = None,
    dynamics: str = "",
    note: str = "",
) -> int:
    return db.insert(
        """INSERT INTO relationship_evolution(relationship_id, chapter_id, intimacy,
                                              trust, conflict, dynamics, note, created_at)
           VALUES(?,?,?,?,?,?,?,?)""",
        (relationship_id, chapter_id, intimacy, trust, conflict, dynamics, note, Database.now()),
    )


def list_rel_evolution(
    db: Database,
    relationship_id: int | None = None,
) -> list[dict]:
    sql = "SELECT * FROM relationship_evolution WHERE 1=1"
    params = []
    if relationship_id is not None:
        sql += " AND relationship_id=?"
        params.append(relationship_id)
    sql += " ORDER BY relationship_id, chapter_id, id"
    return [dict(r) for r in db.query(sql, params)]


# ============== 关系查询补充 ==============

def get_relationship(db: Database, rel_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM relationship WHERE id=?", (rel_id,))
    return dict(row) if row else None


def update_character(db: Database, char_id: int, **fields: Any) -> None:
    """更新人物档案。任何字段变化（如 status 死→活）都通过此方法。"""
    if not fields:
        return
    # status 白名单校验：非空时检查是否含规范关键词（防 AI 写"挂了"等变体导致检测漏）
    if "status" in fields and fields["status"]:
        sv = str(fields["status"]).strip()
        if not any(k in sv for k in STATUS_WHITELIST):
            _log.warning("status 值 %r 不在白名单 %r，仍存储但可能影响检测", sv, STATUS_WHITELIST)
    # JSON 字段需要序列化
    json_fields = {"aliases", "extra", "baseline_keywords"}
    for jf in json_fields:
        if jf in fields and not isinstance(fields[jf], str):
            fields[jf] = Database.to_json(fields[jf])
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [char_id]
    db.execute(f"UPDATE character SET {cols} WHERE id=?", vals)
    _invalidate_retriever_cache()


def delete_character(db: Database, char_id: int) -> bool:
    """删除人物 + 级联清理所有关联数据（手动级联，因建表时未加 ON DELETE CASCADE）。
    处理：milestone / relationship(+evolution) / chapter.pov(置NULL) / event.participants / fact.known_by。
    返回是否删除成功。"""
    ch = get_character(db, char_id)
    if not ch:
        return False
    # 1. 删 milestone
    db.execute("DELETE FROM character_milestone WHERE character_id=?", (char_id,))
    # 2. 删 relationship + 其 evolution（该人物参与的每段关系）
    rel_rows = db.query(
        "SELECT id FROM relationship WHERE char_a_id=? OR char_b_id=?", (char_id, char_id)
    )
    for r in rel_rows:
        db.execute("DELETE FROM relationship_evolution WHERE relationship_id=?", (r["id"],))
    db.execute("DELETE FROM relationship WHERE char_a_id=? OR char_b_id=?", (char_id, char_id))
    # 3. chapter.pov_character_id 置 NULL（不删章节）
    db.execute("UPDATE chapter SET pov_character_id=NULL WHERE pov_character_id=?", (char_id,))
    # 4. event.participants（JSON数组）移除该 id
    for ev in db.query("SELECT id, participants FROM event"):
        parts = Database.from_json(ev["participants"]) or []
        if char_id in parts:
            parts = [p for p in parts if p != char_id]
            db.execute("UPDATE event SET participants=? WHERE id=?", (Database.to_json(parts), ev["id"]))
    # 5. fact.known_by（JSON数组）移除该 id
    for f in db.query("SELECT id, known_by FROM fact"):
        kb_list = Database.from_json(f["known_by"]) or []
        if char_id in kb_list:
            kb_list = [k for k in kb_list if k != char_id]
            db.execute("UPDATE fact SET known_by=? WHERE id=?", (Database.to_json(kb_list), f["id"]))
    # 6. 删人物本体
    db.execute("DELETE FROM character WHERE id=?", (char_id,))
    _invalidate_retriever_cache()
    return True


# ============== Relationship ==============

# 受控关系类型枚举（借鉴 NovelForge 31 种 + StoryForge 26 种，精简为中文版）
# 分 5 组，供前端图按 rel_type 分组着色 + 抽取 prompt 注入让 AI 从中选
RELATION_TYPE_GROUPS = {
    "人际": ("朋友", "敌对", "恋人", "师徒", "亲属", "主仆", "同窗"),
    "组织": ("隶属", "领导", "创立", "同盟"),
    "因果": ("恩人", "仇人", "盟友", "竞争对手", "债务"),
    "物品": ("持有", "传承", "赠送"),
    "叙事": ("镜像", "对照", "秘密"),
}
RELATION_TYPES = [t for group in RELATION_TYPE_GROUPS.values() for t in group]
# rel_type → 所属组（前端着色用）
RELATION_TYPE_TO_GROUP = {t: g for g, types in RELATION_TYPE_GROUPS.items() for t in types}


def add_relationship(
    db: Database,
    char_a_id: int,
    char_b_id: int,
    rel_type: str,
    description: str = "",
    current_state: str = "",
    established_chapter_id: int | None = None,
) -> int:
    # 关系类型校验：非枚举值记 warning（借鉴 NovelForge 的受控类型设计），不拒绝存储
    if rel_type and rel_type not in RELATION_TYPES:
        _log.warning("rel_type %r 不在受控枚举 %r 中，仍存储但影响分组统计", rel_type, RELATION_TYPES[:6])
    rid = db.insert(
        """INSERT INTO relationship(char_a_id, char_b_id, rel_type, description,
                                    current_state, established_chapter_id)
           VALUES(?,?,?,?,?,?)""",
        (char_a_id, char_b_id, rel_type, description, current_state, established_chapter_id),
    )
    _invalidate_retriever_cache()
    return rid


def get_relationships_for(db: Database, char_id: int) -> list[dict]:
    rows = db.query(
        """SELECT * FROM relationship
           WHERE char_a_id=? OR char_b_id=?""",
        (char_id, char_id),
    )
    return [dict(r) for r in rows]


def list_relationships(db: Database) -> list[dict]:
    return [dict(r) for r in db.query("SELECT * FROM relationship")]


def list_relationships_by_character(db: Database, char_id: int) -> list[dict]:
    """查某人物参与的所有关系（带 WHERE 过滤，避免全表扫描后 Python 过滤）。"""
    return [dict(r) for r in db.query(
        "SELECT * FROM relationship WHERE char_a_id=? OR char_b_id=?",
        (char_id, char_id),
    )]


# ============== World Setting ==============

def add_world(db: Database, category: str, name: str, content: str) -> int:
    rid = db.insert(
        "INSERT INTO world_setting(category, name, content) VALUES(?,?,?)",
        (category, name, content),
    )
    _invalidate_retriever_cache()
    return rid


def list_world(db: Database, category: str | None = None) -> list[dict]:
    if category:
        rows = db.query("SELECT * FROM world_setting WHERE category=? ORDER BY id", (category,))
    else:
        rows = db.query("SELECT * FROM world_setting ORDER BY category, id")
    return [dict(r) for r in rows]


def search_world(db: Database, keyword: str) -> list[dict]:
    rows = db.query(
        "SELECT * FROM world_setting WHERE name LIKE ? OR content LIKE ?",
        (f"%{keyword}%", f"%{keyword}%"),
    )
    return [dict(r) for r in rows]


# ============== Fact (含信息边界) ==============

def add_fact(
    db: Database,
    content: str,
    category: str = "general",
    reliability: str = "reliable",
    known_by: list[int] | None = None,
    established_chapter_id: int | None = None,
) -> int:
    now = Database.now()
    fid = db.insert(
        """INSERT INTO fact(category, content, reliability, known_by,
                            established_chapter_id, created_at)
           VALUES(?,?,?,?,?,?)""",
        (
            category,
            content,
            reliability,
            Database.to_json(known_by or []),
            established_chapter_id,
            now,
        ),
    )
    _invalidate_retriever_cache()
    return fid


def get_fact(db: Database, fact_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM fact WHERE id=?", (fact_id,))
    if not row:
        return None
    d = dict(row)
    d["known_by"] = Database.from_json(d.get("known_by")) or []
    return d


def list_facts(db: Database, category: str | None = None) -> list[dict]:
    if category:
        rows = db.query("SELECT * FROM fact WHERE category=? ORDER BY id", (category,))
    else:
        rows = db.query("SELECT * FROM fact ORDER BY id")
    out = []
    for r in rows:
        d = dict(r)
        d["known_by"] = Database.from_json(d.get("known_by")) or []
        out.append(d)
    return out


def facts_known_by(db: Database, char_id: int) -> list[dict]:
    """POV 角色的'信息边界'——这是限知视角一致性检查的核心。

    规则：
    - 公开事实（reliable/false 且 known_by 为空）：POV 知道
    - secret/rumored 且 known_by 为空：视为"无人知道"，POV 不知道
    - known_by 非空且含 POV：POV 知道
    """
    rows = db.query("SELECT * FROM fact")
    out = []
    for r in rows:
        d = dict(r)
        d["known_by"] = Database.from_json(d.get("known_by")) or []
        rel = d.get("reliability") or "reliable"
        # 公开事实 (reliable/false) 且 known_by 为空 → 全员知道
        if not d["known_by"]:
            if rel in ("secret", "rumored"):
                continue  # secret/rumored 且未指定 known_by → 无人知道
            out.append(d)
        elif char_id in d["known_by"]:
            out.append(d)
    return out


# ============== Chapter ==============

def add_chapter(
    db: Database,
    idx: int,
    title: str,
    outline: str = "",
    story_time_start: float | None = None,
    story_time_end: float | None = None,
    location: str = "",
    pov_character_id: int | None = None,
) -> int:
    # B-新38: idx 必须 ≥ 1 (与前端 Path(ge=1) 强约束一致, 防止 0 走通入库)
    if idx is None or not isinstance(idx, int) or idx < 1:
        raise ValueError(f"add_chapter: idx 必须 ≥ 1, 收到 {idx!r}")
    now = Database.now()
    return db.insert(
        """INSERT INTO chapter(idx, title, outline, story_time_start, story_time_end,
                               location, pov_character_id, created_at, updated_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (idx, title, outline, story_time_start, story_time_end, location,
         pov_character_id, now, now),
    )


def get_chapter(db: Database, chapter_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM chapter WHERE id=?", (chapter_id,))
    return dict(row) if row else None


def get_chapter_by_idx(db: Database, idx: int) -> dict | None:
    row = db.query_one("SELECT * FROM chapter WHERE idx=?", (idx,))
    return dict(row) if row else None


def get_prev_chapter(db: Database, idx: int) -> dict | None:
    """返回 idx 之前（按 idx 排序）最近的一章。章节 idx 可能跳号，不能简单 -1。"""
    row = db.query_one(
        "SELECT * FROM chapter WHERE idx<? ORDER BY idx DESC LIMIT 1", (idx,))
    return dict(row) if row else None


def list_chapters(db: Database, volume_idx: int | None = None) -> list[dict]:
    if volume_idx is not None:
        rows = db.query(
            "SELECT * FROM chapter WHERE volume_idx=? ORDER BY idx",
            (volume_idx,),
        )
    else:
        rows = db.query("SELECT * FROM chapter ORDER BY idx")
    return [dict(r) for r in rows]


def update_chapter(db: Database, chapter_id: int, **fields: Any) -> None:
    fields["updated_at"] = Database.now()
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [chapter_id]
    db.execute(f"UPDATE chapter SET {cols} WHERE id=?", vals)


def delete_chapter(db: Database, chapter_id: int) -> bool:
    """删除章节 + 级联清理所有关联数据（手动级联）。
    处理：event / consistency_report / editor_comment / chapter_version /
          character_milestone / relationship_evolution（删）/ plot_thread（章节引用置NULL）。
    返回是否删除成功。"""
    ch = db.query_one("SELECT * FROM chapter WHERE id=?", (chapter_id,))
    if not ch:
        return False
    db.execute("DELETE FROM event WHERE chapter_id=?", (chapter_id,))
    db.execute("DELETE FROM consistency_report WHERE chapter_id=?", (chapter_id,))
    db.execute("DELETE FROM editor_comment WHERE chapter_id=?", (chapter_id,))
    db.execute("DELETE FROM chapter_version WHERE chapter_id=?", (chapter_id,))
    db.execute("DELETE FROM character_milestone WHERE chapter_id=?", (chapter_id,))
    db.execute("DELETE FROM relationship_evolution WHERE chapter_id=?", (chapter_id,))
    # plot_thread 的 planted/payoff/resolved_chapter_id 置 NULL（保留伏笔本身，只断开章节关联）
    db.execute(
        "UPDATE plot_thread SET planted_chapter_id=NULL WHERE planted_chapter_id=?",
        (chapter_id,),
    )
    db.execute(
        "UPDATE plot_thread SET payoff_chapter_id=NULL WHERE payoff_chapter_id=?",
        (chapter_id,),
    )
    db.execute(
        "UPDATE plot_thread SET resolved_chapter_id=NULL WHERE resolved_chapter_id=?",
        (chapter_id,),
    )
    db.execute("DELETE FROM chapter WHERE id=?", (chapter_id,))
    _invalidate_retriever_cache()
    return True


# ============== Event ==============

def add_event(
    db: Database,
    chapter_id: int,
    story_time: float,
    sequence_in_chapter: int,
    title: str,
    summary: str,
    event_type: str = "action",
    location: str = "",
    cause_event_ids: list[int] | None = None,
    participants: list[int] | None = None,
    importance: int = 3,
) -> int:
    return db.insert(
        """INSERT INTO event(chapter_id, story_time, sequence_in_chapter, title,
                             summary, event_type, location, cause_event_ids,
                             participants, importance)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            chapter_id, story_time, sequence_in_chapter, title, summary,
            event_type, location, Database.to_json(cause_event_ids or []),
            Database.to_json(participants or []), importance,
        ),
    )


def list_events(db: Database, chapter_id: int | None = None) -> list[dict]:
    if chapter_id:
        rows = db.query(
            "SELECT * FROM event WHERE chapter_id=? ORDER BY story_time, sequence_in_chapter",
            (chapter_id,),
        )
    else:
        rows = db.query(
            "SELECT * FROM event ORDER BY story_time, sequence_in_chapter"
        )
    out = []
    for r in rows:
        d = dict(r)
        d["cause_event_ids"] = Database.from_json(d.get("cause_event_ids")) or []
        d["participants"] = Database.from_json(d.get("participants")) or []
        out.append(d)
    return out


def events_in_window(db: Database, t_start: float, t_end: float) -> list[dict]:
    """时间窗口查询——检查事件链时用。"""
    rows = db.query(
        """SELECT * FROM event
           WHERE story_time >= ? AND story_time <= ?
           ORDER BY story_time, sequence_in_chapter""",
        (t_start, t_end),
    )
    out = []
    for r in rows:
        d = dict(r)
        d["cause_event_ids"] = Database.from_json(d.get("cause_event_ids")) or []
        d["participants"] = Database.from_json(d.get("participants")) or []
        out.append(d)
    return out


def latest_story_time(db: Database) -> float | None:
    row = db.query_one("SELECT MAX(story_time) AS t FROM event")
    if not row or row["t"] is None:
        return None
    return float(row["t"])


def list_events_by_character(db: Database, char_id: int) -> list[dict]:
    """查某人物参与的所有事件（按 story_time 排序），供人物小传时间线用。

    event.participants 存的是 int 角色 id（见 add_event / writer.py 的 pids）。
    """
    out = []
    for ev in list_events(db):
        parts = ev.get("participants") or []
        if char_id in parts:
            out.append(ev)
    # 按 story_time 排序（None 排最后）
    out.sort(key=lambda e: (e.get("story_time") is None, e.get("story_time") or 0))
    return out


def apply_status_from_events(
    db: Database,
    events: list[dict],
    char_name_to_id: dict[str, int],
) -> int:
    """根据事件 event_type 自动回写 character.status。

    遍历事件，对 death/disappearance 类型，把 participants 里的角色 status 更新。
    返回更新的角色数（供日志/报告用）。
    """
    updated = 0
    STATUS_MAP = {
        "death": "已死",
        "disappearance": "失踪",
    }
    for ev in events:
        et = (ev.get("event_type") or "").strip().lower()
        if et not in STATUS_MAP:
            continue
        new_status = STATUS_MAP[et]
        parts = ev.get("participants") or []
        for pname in parts:
            cid = char_name_to_id.get(pname)
            if not cid:
                continue
            c = get_character(db, cid)
            if not c:
                continue
            # 已有更高优先级 status（如已死）不覆盖
            cur = c.get("status") or ""
            if is_dead_status(cur) and et != "death":
                continue  # 死了不再改回失踪
            if cur == new_status:
                continue  # 无变化
            update_character(db, cid, status=new_status)
            _log.info("角色 %s status 自动更新为 %s（来自 %s 事件）", pname, new_status, et)
            updated += 1
    return updated


def update_appearances(
    db: Database,
    events: list[dict],
    char_name_to_id: dict[str, int],
    chapter_idx: int,
) -> int:
    """事件抽取后，更新角色的出场频率 + 首末章节 + 自动分级。

    - appearance_count += 1（每个角色本章算 1 次，不按事件数累加）
    - first/last_appearance_chapter 更新
    - 自动分级：出场 1 次且无弧光且当前非主角 → role="minor"
    返回更新的角色数。
    """
    # 收集本章所有参与角色 id（去重）
    seen_ids: set[int] = set()
    for ev in events:
        for pname in (ev.get("participants") or []):
            cid = char_name_to_id.get(pname)
            if cid:
                seen_ids.add(cid)
    if not seen_ids:
        return 0
    updated = 0
    for cid in seen_ids:
        c = get_character(db, cid)
        if not c:
            continue
        new_count = (c.get("appearance_count") or 0) + 1
        first_ap = c.get("first_appearance_chapter")
        last_ap = c.get("last_appearance_chapter")
        updates = {
            "appearance_count": new_count,
            "last_appearance_chapter": chapter_idx,
        }
        if first_ap is None or chapter_idx < first_ap:
            updates["first_appearance_chapter"] = chapter_idx
        if last_ap is None or chapter_idx > last_ap:
            updates["last_appearance_chapter"] = chapter_idx
        # 自动分级：出场 1 次且无弧光且非主角 → 标记 minor（不覆盖 protagonist/antagonist）
        cur_role = c.get("role", "supporting")
        if new_count == 1 and not c.get("arc") and cur_role not in ("protagonist", "antagonist"):
            updates["role"] = "minor"
        update_character(db, cid, **updates)
        updated += 1
    return updated


# ============== Plot Thread ==============

def add_thread(
    db: Database,
    title: str,
    description: str,
    thread_type: str = "foreshadow",
    status: str = "planted",
    planted_chapter_id: int | None = None,
    payoff_chapter_id: int | None = None,
    related_characters: list[int] | None = None,
    related_events: list[int] | None = None,
    confidence: float = 0.7,
    notes: str = "",
) -> int:
    return db.insert(
        """INSERT INTO plot_thread(title, description, thread_type, status,
                                   planted_chapter_id, payoff_chapter_id,
                                   related_characters, related_events,
                                   confidence, notes)
           VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (title, description, thread_type, status, planted_chapter_id,
         payoff_chapter_id,
         Database.to_json(related_characters or []),
         Database.to_json(related_events or []),
         confidence, notes),
    )


def list_threads(
    db: Database,
    status: str | None = None,
    thread_type: str | None = None,
) -> list[dict]:
    sql = "SELECT * FROM plot_thread WHERE 1=1"
    params = []
    if status:
        sql += " AND status=?"
        params.append(status)
    if thread_type:
        sql += " AND thread_type=?"
        params.append(thread_type)
    sql += " ORDER BY id"
    rows = db.query(sql, params)
    out = []
    for r in rows:
        d = dict(r)
        d["related_characters"] = Database.from_json(d.get("related_characters")) or []
        d["related_events"] = Database.from_json(d.get("related_events")) or []
        out.append(d)
    return out


def update_thread(db: Database, thread_id: int, **fields: Any) -> None:
    json_fields = {"related_characters", "related_events"}
    for jf in json_fields:
        if jf in fields and not isinstance(fields[jf], str):
            fields[jf] = Database.to_json(fields[jf])
    cols = ", ".join(f"{k}=?" for k in fields)
    vals = list(fields.values()) + [thread_id]
    db.execute(f"UPDATE plot_thread SET {cols} WHERE id=?", vals)


# ============== Consistency Report ==============

def save_consistency_report(
    db: Database,
    chapter_id: int,
    passed: bool,
    issues: list[dict],
    suggestions: str,
    raw_response: str,
) -> int:
    return db.insert(
        """INSERT INTO consistency_report(chapter_id, passed, issues,
                                          suggestions, raw_response, created_at)
           VALUES(?,?,?,?,?,?)""",
        (
            chapter_id, 1 if passed else 0,
            Database.to_json(issues), suggestions, raw_response,
            Database.now(),
        ),
    )


# ============== Optimization Suggestion (LLM 优化建议) ==============

def add_suggestion(
    db: Database,
    target_type: str,
    target_id: str,
    target_label: str,
    title: str,
    content: str,
    priority: str = "medium",
    evidence: str = "",
    chapter_focus: str = "",
) -> int:
    return db.insert(
        """INSERT INTO optimization_suggestion(target_type, target_id, target_label,
                                                 title, content, priority, evidence,
                                                 chapter_focus, created_at)
           VALUES(?,?,?,?,?,?,?,?,?)""",
        (
            target_type, target_id, target_label, title, content, priority,
            evidence, chapter_focus, Database.now(),
        ),
    )


def add_suggestions_bulk(db: Database, suggestions: list[dict]) -> list[int]:
    ids = []
    for s in suggestions:
        ids.append(add_suggestion(
            db,
            target_type=s.get("target_type", "global"),
            target_id=s.get("target_id", ""),
            target_label=s.get("target_label", ""),
            title=s.get("title", ""),
            content=s.get("content", ""),
            priority=s.get("priority", "medium"),
            evidence=s.get("evidence", ""),
            chapter_focus=s.get("chapter_focus", ""),
        ))
    return ids


def list_suggestions(
    db: Database,
    target_type: str | None = None,
    target_id: str | None = None,
    status: str | None = None,
) -> list[dict]:
    sql = "SELECT * FROM optimization_suggestion WHERE 1=1"
    params = []
    if target_type:
        sql += " AND target_type=?"
        params.append(target_type)
    if target_id:
        sql += " AND target_id=?"
        params.append(target_id)
    if status:
        sql += " AND status=?"
        params.append(status)
    sql += " ORDER BY CASE priority WHEN 'high' THEN 1 WHEN 'medium' THEN 2 WHEN 'low' THEN 3 ELSE 4 END, id DESC"
    return [dict(r) for r in db.query(sql, params)]


def get_suggestion(db: Database, sug_id: int) -> dict | None:
    row = db.query_one("SELECT * FROM optimization_suggestion WHERE id=?", (sug_id,))
    return dict(row) if row else None


def update_suggestion_status(db: Database, sug_id: int, status: str) -> None:
    """open | applied | dismissed"""
    # B-新124: 防御任意 status 字符串写入 db
    if status not in ("applied", "dismissed", "open"):
        raise ValueError(f"update_suggestion_status: status 必须是 applied/dismissed/open, 收到 {status!r}")
    if status == "applied":
        db.execute(
            "UPDATE optimization_suggestion SET status=?, applied_at=? WHERE id=?",
            (status, Database.now(), sug_id),
        )
    else:
        db.execute(
            "UPDATE optimization_suggestion SET status=? WHERE id=?",
            (status, sug_id),
        )


# ============== Chapter Version（章节版本树，增量 patch 链） ==============

# 触发清理的版本数阈值（超过则跑一次时间衰减 prune）
_PRUNE_THRESHOLD = 60
_SEVEN_DAYS = 7 * 86400


def add_chapter_version(
    db: Database,
    chapter_id: int,
    text: str,
    source: str = "save",
    label: str | None = None,
    name: str | None = None,
    accept_count: int = 0,
    reject_count: int = 0,
    parent_id: int | None = None,
) -> int:
    """新增一章的一个版本。

    自动算增量 patch：parent = parent_id 指定版 / 否则取该章最新版的正文作为 parent。
    若该章无任何版本，则作为基线版（seq=0, parent_id=NULL, patch=full）。
    返回新版本 id。

    text 为本版完整正文（调用方传全文，patch 在内部算）。
    """
    now = Database.now()
    # 解析 parent：显式 > 最新版
    parent_row = None
    if parent_id is not None:
        parent_row = db.query_one(
            "SELECT id, seq, patch FROM chapter_version WHERE id=? AND chapter_id=?",
            (parent_id, chapter_id),
        )
    if parent_row is None:
        parent_row = db.query_one(
            "SELECT id, seq, patch FROM chapter_version WHERE chapter_id=? ORDER BY seq DESC LIMIT 1",
            (chapter_id,),
        )

    if parent_row is None:
        # 基线版：seq=0, patch=full
        patch = vp.make_patch("", text)
        seq = 0
        parent_id_used = None
    else:
        parent_text = get_chapter_version_full_text(db, parent_row["id"])
        patch = vp.make_patch(parent_text, text)
        seq = parent_row["seq"] + 1
        parent_id_used = parent_row["id"]

    vid = db.insert(
        """INSERT INTO chapter_version
           (chapter_id, seq, parent_id, patch, word_count, source, label, name,
            accept_count, reject_count, created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
        (chapter_id, seq, parent_id_used, patch, len(text), source, label, name,
         accept_count, reject_count, now),
    )
    # 版本数超阈值则清理（best-effort，失败不影响写入）
    try:
        n = db.query_one("SELECT COUNT(*) AS n FROM chapter_version WHERE chapter_id=?", (chapter_id,))["n"]
        if n > _PRUNE_THRESHOLD:
            prune_chapter_versions(db, chapter_id)
    except Exception as e:
        _log.warning("prune after add version failed: %s", e)
    return vid


def list_chapter_versions(db: Database, chapter_id: int, limit: int = 200) -> list[dict]:
    """列出某章所有版本（最新在前）。不返 patch（列表页用不到正文）。"""
    rows = db.query(
        """SELECT id, seq, parent_id, word_count, source, label, name,
                  accept_count, reject_count, created_at
           FROM chapter_version WHERE chapter_id=?
           ORDER BY seq DESC LIMIT ?""",
        (chapter_id, limit),
    )
    return [dict(r) for r in rows]


def get_chapter_version(db: Database, version_id: int) -> dict | None:
    """取单版原始行（含 patch）。"""
    row = db.query_one("SELECT * FROM chapter_version WHERE id=?", (version_id,))
    return dict(row) if row else None


def get_latest_chapter_version(db: Database, chapter_id: int) -> dict | None:
    """取某章最新版（含 patch）。无版本返回 None。"""
    row = db.query_one(
        "SELECT * FROM chapter_version WHERE chapter_id=? ORDER BY seq DESC LIMIT 1",
        (chapter_id,),
    )
    return dict(row) if row else None


def get_chapter_version_full_text(db: Database, version_id: int) -> str:
    """重建指定版本的完整正文。

    沿 parent_id 链回溯到基线，正序应用 patch。
    任一 patch 应用失败 → fallback：
      1) 该版自身是 full → 用它；
      2) 否则找链上最近一个 full 版作为兜底文本；
      3) 都没有 → 返回空串（绝不抛异常给上层）。
    """
    # 收集链上所有 (id, patch)，从目标版到基线
    chain: list[tuple[int, str]] = []
    cur_id = version_id
    seen = set()
    while cur_id is not None:
        if cur_id in seen:
            break  # 防环
        seen.add(cur_id)
        row = db.query_one("SELECT id, parent_id, patch FROM chapter_version WHERE id=?", (cur_id,))
        if row is None:
            break
        chain.append((row["id"], row["patch"]))
        cur_id = row["parent_id"]

    if not chain:
        return ""

    # chain[0] = 目标版，chain[-1] = 基线。正序应用需反转：从基线开始
    chain.reverse()  # 现在 chain[0] = 基线（最旧），chain[-1] = 目标（最新）

    text = ""
    last_full_text = ""
    for vid, patch in chain:
        try:
            text = vp.apply_patch(text, patch)
            if vp.is_full_snapshot(patch):
                last_full_text = text
        except Exception as e:
            _log.warning("apply patch failed for version %s: %s — fallback", vid, e)
            # 从此版往后链断了。回退到最近能用的 full 文本
            if last_full_text:
                text = last_full_text
            else:
                # 看本 patch 是否是 full（即便 apply 抛错也试试直接取 text 字段）
                try:
                    p = json.loads(patch)
                    if p.get("op") == "full":
                        text = p.get("text", "")
                        last_full_text = text
                except Exception:
                    text = ""
            # 继续尝试应用后续 patch（基于 fallback 文本），尽量还原最新
    return text


def _relink_children_for_delete(db: Database, deleted_row: dict) -> None:
    """删除某版本前，把它的子版本重接到被删版的 parent 上。

    BUG 修复：不能只改 child.parent_id —— child.patch 是相对「被删版正文」
    计算的增量，重接后相对新 parent 应用会索引错位/越界，导致正文错乱或
    静默 fallback 成旧文本。必须先重建 child 与新 parent 的全文，重算 patch。
    """
    new_parent_id = deleted_row.get("parent_id")
    if new_parent_id is not None:
        new_parent_text = get_chapter_version_full_text(db, new_parent_id)
    else:
        new_parent_text = ""
    children = db.query(
        "SELECT id FROM chapter_version WHERE parent_id=?", (deleted_row["id"],))
    for ch in children:
        child_text = get_chapter_version_full_text(db, ch["id"])
        new_patch = vp.make_patch(new_parent_text, child_text)
        db.execute(
            "UPDATE chapter_version SET parent_id=?, patch=? WHERE id=?",
            (new_parent_id, new_patch, ch["id"]),
        )


def prune_chapter_versions(db: Database, chapter_id: int) -> int:
    """时间衰减清理：7 天内全留；7 天前只留命名版 + 每天最新一版；基线(seq=0)永远留。

    删除时把 child 重接到被删版的 parent 并重算 patch（见 _relink_children_for_delete）。
    返回删除数。
    """
    now = Database.now()
    versions = list_chapter_versions(db, chapter_id, limit=10000)  # 最新在前
    if len(versions) <= 5:
        return 0  # 太少不清理

    keep_ids: set[int] = set()
    by_day_oldest: dict[str, int] = {}  # 每天保留最新一版 = versions 里第一条（已倒序）

    for v in versions:
        age = now - v["created_at"]
        if age <= _SEVEN_DAYS:
            keep_ids.add(v["id"])                       # 7 天内全留
        elif v["source"] == "named":
            keep_ids.add(v["id"])                       # 命名版永远留
        else:
            day = time.strftime("%Y-%m-%d", time.gmtime(v["created_at"]))
            if day not in by_day_oldest:                # 7天前的非命名：每天只留最新一版
                by_day_oldest[day] = v["id"]
                keep_ids.add(v["id"])

    # 基线版（seq=0）强制保留
    baseline = db.query_one(
        "SELECT id FROM chapter_version WHERE chapter_id=? AND seq=0", (chapter_id,)
    )
    if baseline:
        keep_ids.add(baseline["id"])

    to_delete = [v for v in versions if v["id"] not in keep_ids]
    deleted = 0
    for v in to_delete:
        # 重接 child 到 v.parent_id 并重算 patch（保链不断且增量不错位）
        _relink_children_for_delete(db, v)
        db.execute("DELETE FROM chapter_version WHERE id=?", (v["id"],))
        deleted += 1
    return deleted


def delete_chapter_version(db: Database, version_id: int) -> bool:
    """删单版（重接 child 到其 parent 并重算 patch）。基线版拒绝删除。返回是否删除成功。"""
    row = get_chapter_version(db, version_id)
    if not row:
        return False
    if row["seq"] == 0:
        return False  # 基线版不可删
    _relink_children_for_delete(db, row)
    db.execute("DELETE FROM chapter_version WHERE id=?", (version_id,))
    return True


# ============== AI 调用日志（token 计量 / 延迟统计） ==============

def log_ai_call(
    db: Database,
    endpoint: str,
    usage: dict | None = None,
    chapter_id: int | None = None,
    success: bool = True,
    error: str | None = None,
) -> None:
    """记录一次 AI 调用到 ai_call_log。

    usage 形如 {prompt_tokens, completion_tokens, total_tokens, latency_ms, model, provider}，
    来自 ai_client 的咽喉点插桩（self.last_usage）。任一字段缺失填 0，不影响写入。
    """
    u = usage or {}
    try:
        db.insert(
            """INSERT INTO ai_call_log
               (ts, endpoint, model, provider, prompt_tokens, completion_tokens,
                total_tokens, latency_ms, success, chapter_id, error)
               VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
            (Database.now(), endpoint, u.get("model"), u.get("provider"),
             int(u.get("prompt_tokens", 0) or 0), int(u.get("completion_tokens", 0) or 0),
             int(u.get("total_tokens", 0) or 0), int(u.get("latency_ms", 0) or 0),
             1 if success else 0, chapter_id, error),
        )
    except Exception as e:
        _log.warning("log_ai_call 写入失败（不影响主流程）: %s", e)


def ai_call_stats(db: Database, since_ts: float | None = None) -> dict:
    """聚合 AI 调用统计。

    since_ts 非 None 时只统计该时间戳之后的记录（用于"今日"统计）。
    返回 {calls, prompt_tokens, completion_tokens, total_tokens, avg_latency_ms,
          by_endpoint: {endpoint: {calls, tokens, avg_latency_ms}}}。
    无数据时各计数为 0。
    """
    where = "WHERE ts >= ?" if since_ts is not None else ""
    params: tuple = (since_ts,) if since_ts is not None else ()
    row = db.query_one(
        f"""SELECT COUNT(*) AS calls,
                   COALESCE(SUM(prompt_tokens), 0) AS pt,
                   COALESCE(SUM(completion_tokens), 0) AS ct,
                   COALESCE(SUM(total_tokens), 0) AS tt,
                   COALESCE(AVG(latency_ms), 0) AS al
            FROM ai_call_log {where}""",
        params,
    )
    calls = row["calls"] if row else 0
    by_endpoint: dict[str, dict] = {}
    if calls:
        ep_rows = db.query(
            f"""SELECT endpoint,
                       COUNT(*) AS calls,
                       COALESCE(SUM(total_tokens), 0) AS tokens,
                       COALESCE(AVG(latency_ms), 0) AS al
                FROM ai_call_log {where}
                GROUP BY endpoint ORDER BY calls DESC""",
            params,
        )
        for er in ep_rows:
            by_endpoint[er["endpoint"]] = {
                "calls": er["calls"],
                "tokens": er["tokens"],
                "avg_latency_ms": round(er["al"]),
            }
    return {
        "calls": calls,
        "prompt_tokens": row["pt"] if row else 0,
        "completion_tokens": row["ct"] if row else 0,
        "total_tokens": row["tt"] if row else 0,
        "avg_latency_ms": round(row["al"]) if row else 0,
        "by_endpoint": by_endpoint,
    }


def recent_ai_calls(db: Database, limit: int = 10) -> list[dict]:
    """最近 N 次 AI 调用（最新在前）。"""
    rows = db.query(
        """SELECT id, ts, endpoint, model, total_tokens, latency_ms, success, chapter_id
           FROM ai_call_log ORDER BY ts DESC LIMIT ?""",
        (limit,),
    )
    return [dict(r) for r in rows]

