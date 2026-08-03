"""
novelai.db
SQLite 数据访问层。
表设计体现"事件链 / 时间顺序 / 人物性格 / 信息把控"四维一致性。
"""
from __future__ import annotations
import sqlite3
import json
import time
from pathlib import Path
from contextlib import contextmanager
from typing import Any, Iterable


SCHEMA = """
-- 项目级元信息（一本书一个 db；多本可分别建库）
CREATE TABLE IF NOT EXISTS project (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    synopsis TEXT,
    style TEXT,                 -- 文风 / 题材 / 视点说明
    pov_mode TEXT,              -- 限知视角 / 全知视角
    story_time_unit TEXT,       -- 时间单位：日 / 小时 / 不定
    created_at REAL NOT NULL
);

-- 卷（一部书可分多卷）
CREATE TABLE IF NOT EXISTS volume (
    id INTEGER PRIMARY KEY,
    idx INTEGER NOT NULL UNIQUE,  -- 卷序号 1,2,3...
    title TEXT NOT NULL,
    synopsis TEXT,                 -- 本卷梗概
    style_notes TEXT,              -- 本卷文风备注（可选）
    word_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_volume_idx ON volume(idx);

-- 人物
CREATE TABLE IF NOT EXISTS character (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,
    aliases TEXT,                -- JSON list
    role TEXT,                   -- protagonist / antagonist / major / supporting / minor
    basic_info TEXT,             -- 年龄/性别/外貌/职业/出身
    personality TEXT,            -- 性格关键词/价值观/恐惧/欲望
    speech_style TEXT,           -- 说话风格
    abilities TEXT,              -- 能力/技能
    arc TEXT,                    -- 人物弧光：起点→转折→终点
    status TEXT,                 -- 当前状态：活/死/失踪/位置等
    mbti TEXT,                   -- MBTI 16型，如 INTJ
    cognitive_stack TEXT,        -- 认知功能栈，如 Ni-Te-Fi-Se
    enneagram TEXT,              -- 九型人格，如 5w4
    arc_type TEXT,               -- 弧光类型：positive/negative/flat/circular
    arc_progress REAL DEFAULT 0.0,  -- 弧光进度 0.0~1.0
    baseline_keywords TEXT,      -- 性格 baseline 关键词（用于漂移检测）JSON list
    appearance_count INTEGER DEFAULT 0,  -- 出场次数（抽取流水线自动统计）
    first_appearance_chapter INTEGER,    -- 首次出场章节 idx
    last_appearance_chapter INTEGER,     -- 最后出场章节 idx
    extra TEXT                   -- 自由扩展 JSON
);
CREATE INDEX IF NOT EXISTS idx_character_name ON character(name);
CREATE INDEX IF NOT EXISTS idx_character_mbti ON character(mbti);
CREATE INDEX IF NOT EXISTS idx_character_role ON character(role);

-- 人物成长里程碑
CREATE TABLE IF NOT EXISTS character_milestone (
    id INTEGER PRIMARY KEY,
    character_id INTEGER NOT NULL,
    chapter_id INTEGER NOT NULL,
    milestone_type TEXT NOT NULL,    -- starting_point / catalyst / crisis / climax / resolution / ending
    dimension TEXT,                  -- personality / values / ability / relationship / belief / world
    description TEXT NOT NULL,
    before_state TEXT,               -- 变化前状态
    after_state TEXT,                -- 变化后状态
    quote TEXT,                       -- 原文引用（关键段落）
    importance INTEGER DEFAULT 3,
    created_at REAL NOT NULL,
    FOREIGN KEY(character_id) REFERENCES character(id),
    FOREIGN KEY(chapter_id) REFERENCES chapter(id)
);
CREATE INDEX IF NOT EXISTS idx_ms_char ON character_milestone(character_id);
CREATE INDEX IF NOT EXISTS idx_ms_chapter ON character_milestone(chapter_id);

-- 关系演变时间序列
CREATE TABLE IF NOT EXISTS relationship_evolution (
    id INTEGER PRIMARY KEY,
    relationship_id INTEGER NOT NULL,
    chapter_id INTEGER NOT NULL,
    intimacy REAL,        -- -1.0(敌对/恨) ~ 1.0(亲密/爱)
    trust REAL,           -- -1.0(怀疑) ~ 1.0(完全信任)
    conflict REAL,        -- 0.0 ~ 1.0 (冲突强度)
    dynamics TEXT,        -- 关系动态描述，如"试探""决裂""重逢"
    note TEXT,
    created_at REAL NOT NULL,
    FOREIGN KEY(relationship_id) REFERENCES relationship(id),
    FOREIGN KEY(chapter_id) REFERENCES chapter(id)
);
CREATE INDEX IF NOT EXISTS idx_re_rel ON relationship_evolution(relationship_id);
CREATE INDEX IF NOT EXISTS idx_re_chapter ON relationship_evolution(chapter_id);

-- LLM 优化建议
CREATE TABLE IF NOT EXISTS optimization_suggestion (
    id INTEGER PRIMARY KEY,
    target_type TEXT NOT NULL,   -- personality | arc | relationship | global
    target_id TEXT,              -- character_id | "a-b" | "project"
    target_label TEXT,           -- 人类可读：沈青砚 / 沈青砚↔林婉 / 全局
    title TEXT NOT NULL,         -- 简短标题
    content TEXT NOT NULL,       -- 详细建议
    priority TEXT,               -- high | medium | low
    evidence TEXT,               -- 依据：哪些数据/章节支撑这个建议
    chapter_focus TEXT,          -- 建议应用的章节范围，如"第5-10章"
    status TEXT DEFAULT 'open', -- open | applied | dismissed
    created_at REAL NOT NULL,
    applied_at REAL
);
CREATE INDEX IF NOT EXISTS idx_os_target ON optimization_suggestion(target_type, target_id);
CREATE INDEX IF NOT EXISTS idx_os_status ON optimization_suggestion(status);

-- 人物关系（动态可变）
CREATE TABLE IF NOT EXISTS relationship (
    id INTEGER PRIMARY KEY,
    char_a_id INTEGER NOT NULL,
    char_b_id INTEGER NOT NULL,
    rel_type TEXT NOT NULL,      -- 朋友/敌对/恋人/师徒/...
    description TEXT,
    current_state TEXT,          -- 当前关系状态
    established_chapter_id INTEGER,
    FOREIGN KEY(char_a_id) REFERENCES character(id),
    FOREIGN KEY(char_b_id) REFERENCES character(id)
);
CREATE INDEX IF NOT EXISTS idx_rel_a ON relationship(char_a_id);
CREATE INDEX IF NOT EXISTS idx_rel_b ON relationship(char_b_id);

-- 世界观/设定
CREATE TABLE IF NOT EXISTS world_setting (
    id INTEGER PRIMARY KEY,
    category TEXT NOT NULL,      -- 地理/历史/魔法体系/社会制度/宗教/科技
    name TEXT NOT NULL,
    content TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_world_cat ON world_setting(category);

-- 事实/设定条目（带"谁知道"约束，是信息把控的核心）
CREATE TABLE IF NOT EXISTS fact (
    id INTEGER PRIMARY KEY,
    category TEXT,               -- 人物事实/世界事实/历史事实/能力事实/事件事实
    content TEXT NOT NULL,
    reliability TEXT,            -- reliable / rumored / secret / false
    known_by TEXT,               -- JSON list of character_id；空 list 表示"公开/上帝全知"
    established_chapter_id INTEGER,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_fact_cat ON fact(category);

-- 章节
CREATE TABLE IF NOT EXISTS chapter (
    id INTEGER PRIMARY KEY,
    idx INTEGER NOT NULL UNIQUE, -- 章节序号 1,2,3...
    title TEXT NOT NULL,
    outline TEXT,                -- 本章大纲
    summary TEXT,                -- 自动生成的摘要
    story_time_start REAL,       -- 故事内时间（自定义单位）
    story_time_end REAL,
    location TEXT,
    pov_character_id INTEGER,
    draft TEXT,                  -- 草稿正文
    final_text TEXT,             -- 终稿正文
    word_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    FOREIGN KEY(pov_character_id) REFERENCES character(id)
);
CREATE INDEX IF NOT EXISTS idx_chapter_idx ON chapter(idx);

-- 事件（事件链 + 时间顺序的核心）
CREATE TABLE IF NOT EXISTS event (
    id INTEGER PRIMARY KEY,
    chapter_id INTEGER NOT NULL,
    story_time REAL NOT NULL,    -- 故事内时间
    sequence_in_chapter INTEGER NOT NULL, -- 章节内顺序
    title TEXT NOT NULL,
    summary TEXT NOT NULL,
    event_type TEXT,             -- action/dialogue/revelation/turning_point/...
    location TEXT,
    cause_event_ids TEXT,        -- JSON list
    participants TEXT,           -- JSON list of character_id
    importance INTEGER DEFAULT 3,
    FOREIGN KEY(chapter_id) REFERENCES chapter(id)
);
CREATE INDEX IF NOT EXISTS idx_event_chapter ON event(chapter_id);
CREATE INDEX IF NOT EXISTS idx_event_story_time ON event(story_time);
-- 注意：idx_event_participants 已移除（建在 JSON 文本列上无效）。
-- event.participants 查询走 Python 端遍历（单本小说事件 < 数千，O(N) 够用）。

-- 伏笔/线索
CREATE TABLE IF NOT EXISTS plot_thread (
    id INTEGER PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT,
    thread_type TEXT,            -- foreshadow/mystery/relationship/promise/secret
    status TEXT DEFAULT 'planted', -- planted/developing/payoff/resolved/abandoned
    planted_chapter_id INTEGER,
    payoff_chapter_id INTEGER,
    resolved_chapter_id INTEGER,
    related_characters TEXT,     -- JSON list
    related_events TEXT,         -- JSON list
    notes TEXT
);
CREATE INDEX IF NOT EXISTS idx_thread_status ON plot_thread(status);

-- 一致性检查记录
CREATE TABLE IF NOT EXISTS consistency_report (
    id INTEGER PRIMARY KEY,
    chapter_id INTEGER NOT NULL,
    passed INTEGER NOT NULL,     -- 0/1
    issues TEXT,                 -- JSON list
    suggestions TEXT,
    raw_response TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_cr_chapter ON consistency_report(chapter_id);

-- 风格指南（编辑自定义的写作规则，用于违例检测 + AI 改稿 prompt 注入）
CREATE TABLE IF NOT EXISTS style_rule (
    id INTEGER PRIMARY KEY,
    name TEXT NOT NULL,           -- 规则名（"禁用 6 字以上成语"）
    rule_type TEXT NOT NULL,      -- forbid_phrase | max_para_chars | max_dialogue_lines | min_sentence_chars | max_sentence_chars
    pattern TEXT,                 -- 词组（forbid_phrase 用）或数值（其它类型用）
    severity TEXT DEFAULT 'mid',  -- high | mid | low
    description TEXT,             -- 给 LLM 看的描述
    enabled INTEGER DEFAULT 1,    -- 0/1
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sr_enabled ON style_rule(enabled);

-- 批注（编辑红头批注）
CREATE TABLE IF NOT EXISTS editor_comment (
    id INTEGER PRIMARY KEY,
    chapter_id INTEGER NOT NULL,   -- chapter.id
    chapter_idx INTEGER NOT NULL,  -- 冗余存方便查
    anchor_start INTEGER NOT NULL, -- 选区起点（textarea 字符 index）
    anchor_end INTEGER NOT NULL,   -- 选区终点
    snippet TEXT NOT NULL,         -- 选中的文本快照（即使正文改了也能显示）
    body TEXT NOT NULL,            -- 批注内容
    author TEXT DEFAULT 'editor',  -- editor / author / system
    status TEXT DEFAULT 'open',    -- open / resolved
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ec_chapter ON editor_comment(chapter_id);
CREATE INDEX IF NOT EXISTS idx_ec_status ON editor_comment(status);

-- 章节版本历史（增量 patch 链；seq=0 为基线/原始版）
CREATE TABLE IF NOT EXISTS chapter_version (
    id INTEGER PRIMARY KEY,
    chapter_id INTEGER NOT NULL,
    seq INTEGER NOT NULL,              -- 该章内版本序号 0,1,2...（0 = 基线）
    parent_id INTEGER,                 -- 上一版的 id（链式重建用；基线为 NULL）
    patch TEXT NOT NULL,               -- 增量 patch JSON；基线或退化时为 {"op":"full","text":...}
    word_count INTEGER NOT NULL,
    source TEXT NOT NULL,              -- baseline|save|ai|replace|insert|named
    label TEXT,                        -- 显示标签
    name TEXT,                         -- 用户命名（named 版独有）
    accept_count INTEGER DEFAULT 0,    -- AI 改稿接受段数（统计用）
    reject_count INTEGER DEFAULT 0,
    created_at REAL NOT NULL,
    FOREIGN KEY(chapter_id) REFERENCES chapter(id),
    FOREIGN KEY(parent_id) REFERENCES chapter_version(id)
);
CREATE INDEX IF NOT EXISTS idx_cv_chapter ON chapter_version(chapter_id, seq);
CREATE INDEX IF NOT EXISTS idx_cv_parent ON chapter_version(parent_id);

-- AI 调用日志（token 计量 / 延迟 / 成败统计，咽喉点插桩写入）
CREATE TABLE IF NOT EXISTS ai_call_log (
    id INTEGER PRIMARY KEY,
    ts REAL NOT NULL,
    endpoint TEXT NOT NULL,           -- 哪个功能：editor_ai_edit / generate_chapter / ...
    model TEXT,
    provider TEXT,                    -- openai / anthropic
    prompt_tokens INTEGER DEFAULT 0,
    completion_tokens INTEGER DEFAULT 0,
    total_tokens INTEGER DEFAULT 0,
    latency_ms INTEGER DEFAULT 0,
    success INTEGER DEFAULT 1,
    chapter_id INTEGER,
    error TEXT
);
CREATE INDEX IF NOT EXISTS idx_acl_ts ON ai_call_log(ts);
CREATE INDEX IF NOT EXISTS idx_acl_endpoint ON ai_call_log(endpoint);

-- 语义检索向量缓存（embed() 结果，按 entity_type+entity_id 索引）
CREATE TABLE IF NOT EXISTS embedding (
    id INTEGER PRIMARY KEY,
    entity_type TEXT NOT NULL,        -- character / world_setting / plot_thread / fact
    entity_id INTEGER NOT NULL,
    text_hash TEXT NOT NULL,          -- 源文本的 hash，变了就重算
    vector_json TEXT NOT NULL,        -- json.dumps(list[float])，纯 Python cosine 读
    model TEXT,                       -- 用哪个 embedding 模型生成（换模型需失效）
    ts REAL NOT NULL,
    UNIQUE(entity_type, entity_id)
);
CREATE INDEX IF NOT EXISTS idx_emb_type ON embedding(entity_type);
"""


class Database:
    """轻量 SQLite 封装。"""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(str(self.path))
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL;")
        conn.execute("PRAGMA synchronous=NORMAL;")
        conn.execute("PRAGMA busy_timeout=5000;")
        conn.execute("PRAGMA cache_size=-8000;")  # 8MB 内存缓存（默认 2MB 太小）
        conn.execute("PRAGMA temp_store=MEMORY;")  # 临时表用内存而非磁盘
        conn.execute("PRAGMA mmap_size=268435456;")  # 256MB 内存映射（大库查询更快）
        return conn

    def _init_schema(self) -> None:
        try:
            self._do_init_schema()
        except Exception as e:
            import logging
            logging.getLogger(__name__).warning("schema init partial failure (continuing): %s", e)

    def _do_init_schema(self) -> None:
        with self.connect() as conn:
            # 1. 先 SCHEMA（CREATE TABLE IF NOT EXISTS 创建所有表）
            conn.executescript(SCHEMA)
            # 2. 然后 ALTER 老列（只在表存在时）
            def _table_exists(name: str) -> bool:
                return conn.execute(
                    "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?",
                    (name,),
                ).fetchone() is not None

            if _table_exists("character"):
                char_cols = {r["name"] for r in conn.execute("PRAGMA table_info(character)")}
                new_char_cols = {
                    "mbti": "TEXT",
                    "cognitive_stack": "TEXT",
                    "enneagram": "TEXT",
                    "arc_type": "TEXT",
                    "arc_progress": "REAL DEFAULT 0.0",
                    "baseline_keywords": "TEXT",
                    "appearance_count": "INTEGER DEFAULT 0",
                    "first_appearance_chapter": "INTEGER",
                    "last_appearance_chapter": "INTEGER",
                }
                for col, decl in new_char_cols.items():
                    if col not in char_cols:
                        try:
                            conn.execute(f"ALTER TABLE character ADD COLUMN {col} {decl}")
                        except Exception:
                            pass  # 已存在（并发初始化或部分迁移），跳过
            if _table_exists("chapter"):
                ch_cols = {r["name"] for r in conn.execute("PRAGMA table_info(chapter)")}
                for col, decl in {"volume_idx": "INTEGER", "import_source": "TEXT"}.items():
                    if col not in ch_cols:
                        try:
                            conn.execute(f"ALTER TABLE chapter ADD COLUMN {col} {decl}")
                        except Exception:
                            pass

    @contextmanager
    def connect(self):
        conn = self._connect()
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def execute(self, sql: str, params: Iterable[Any] = ()) -> None:
        with self.connect() as conn:
            conn.execute(sql, tuple(params))

    def insert(self, sql: str, params: Iterable[Any] = ()) -> int:
        """执行 INSERT 并在**同一连接**内返回 lastrowid。"""
        with self.connect() as conn:
            cur = conn.execute(sql, tuple(params))
            return int(cur.lastrowid or 0)

    def executemany(self, sql: str, seq: Iterable[Iterable[Any]]) -> None:
        with self.connect() as conn:
            conn.executemany(sql, [tuple(p) for p in seq])

    def query(self, sql: str, params: Iterable[Any] = ()) -> list[sqlite3.Row]:
        with self.connect() as conn:
            return list(conn.execute(sql, tuple(params)))

    def query_one(self, sql: str, params: Iterable[Any] = ()) -> sqlite3.Row | None:
        rows = self.query(sql, params)
        return rows[0] if rows else None

    @staticmethod
    def now() -> float:
        return time.time()

    @staticmethod
    def to_json(v: Any) -> str:
        return json.dumps(v, ensure_ascii=False)

    @staticmethod
    def from_json(v: str | None) -> Any:
        if not v:
            return None
        try:
            return json.loads(v)
        except Exception:
            return None
