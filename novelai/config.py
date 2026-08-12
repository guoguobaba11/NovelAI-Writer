"""
novelai.config
全局配置：AI 后端、API key、模型选择、路径等。
支持环境变量或 .env 文件加载。
"""
from __future__ import annotations
import os
import sys
from pathlib import Path
from dataclasses import dataclass, field
from typing import Literal


def _project_root() -> Path:
    r"""返回项目根 / 用户可写目录。

    - 开发模式：源码目录（novelai/config.py → 上两级）
    - PyInstaller 打包：exe 同目录（可写、可重启保数据）
    - 不可写：回退到用户 home（~/NovelAIWriter/）— 跨设备 / 装在 C:\Program Files 等只读位置
    """
    if getattr(sys, "frozen", False):
        # PyInstaller one-file mode: sys.executable = .../NovelAI Writer.exe
        candidate = Path(sys.executable).resolve().parent
    else:
        candidate = Path(__file__).resolve().parent.parent
    # 测试是否可写
    try:
        test_dir = candidate / "data"
        test_dir.mkdir(parents=True, exist_ok=True)
        # 写一个临时文件测试
        test_file = test_dir / ".write_test"
        test_file.write_text("ok", encoding="utf-8")
        test_file.unlink()
        return candidate
    except (PermissionError, OSError):
        # 不可写：回退到用户 home
        home = Path.home() / "NovelAIWriter"
        home.mkdir(parents=True, exist_ok=True)
        print(f"[novelai] 警告：项目根不可写，已回退到 {home}", file=sys.stderr)
        return home


PROJECT_ROOT = _project_root()
DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_DB_PATH = DATA_DIR / "novel.db"
WORKSPACES_DIR = DATA_DIR / "workspaces"
_CURRENT_WS_FILE = DATA_DIR / "current_workspace.txt"
import re as _re
import sqlite3 as _sqlite3
import shutil as _shutil


def _load_env_file() -> None:
    """极简 .env 加载（不依赖 python-dotenv）

    查找顺序：
    1. 当前工作目录/.env
    2. 项目根/.env
    3. PyInstaller 临时目录/.env（兜底）
    """
    candidates = [
        Path.cwd() / ".env",
        PROJECT_ROOT / ".env",
    ]
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        candidates.append(Path(sys._MEIPASS) / ".env")

    env_path = None
    for c in candidates:
        if c.exists():
            env_path = c
            break
    if env_path is None:
        return
    try:
        for line in env_path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" not in line:
                continue
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
    except Exception:
        pass


_load_env_file()


@dataclass
class AIConfig:
    """AI 后端配置。"""
    provider: Literal["openai", "anthropic", "openai_compatible"] = "openai"
    api_key: str = ""
    base_url: str | None = None
    model: str = "gpt-4o-mini"
    # 用于一致性检查等轻量任务
    mini_model: str = "gpt-4o-mini"
    # 语义检索 embedding 模型（留空则自动按 provider 选默认值）
    embedding_model: str = ""
    # 是否启用语义检索（False=只用关键词匹配，省 embedding 调用费用）
    enable_embedding: bool = True
    temperature: float = 0.85
    max_tokens: int = 8000  # 单次 LLM 调用最大 token（8000 ≈ 4000-6000 中文字）
    timeout: int = 120


@dataclass
class WriterConfig:
    """检索/生成相关阈值。"""
    # 召回的最近章节摘要数
    recent_chapter_window: int = 5  # 分层记忆 L1：最近 N 章详细事件（从 3 扩到 5）
    # 单章目标字数（中文字符）
    target_chapter_words: int = 10000  # 单章目标字数（长篇小说默认 1 万，网文可达 2 万）
    # 一致性检查最大重写次数
    max_consistency_retries: int = 2
    # 事实抽取是否启用（需要强模型）
    enable_fact_extraction: bool = True
    # 编辑器 AI 改稿：是否在引入新高危问题时自动重试一次（自校验闭环）
    editor_self_retry: bool = True
    # 编辑器 AI 改稿：是否允许 AI 主动调用工具查询知识库（仅 openai/openai_compatible）
    editor_tool_use: bool = True
    # 编辑器 AI 改稿：工具调用最大轮数（防死循环）
    editor_max_tool_rounds: int = 3
    # Agentic Loop 写章：写前自主查询 + 写后自反思（Hermes 风格半自主决策）
    writer_agentic_research: bool = True   # 写章前 AI 自主调工具查知识库
    writer_agentic_reflect: bool = True    # 写章后 AI 自审+修正


@dataclass
class AppConfig:
    ai: AIConfig = field(default_factory=AIConfig)
    writer: WriterConfig = field(default_factory=WriterConfig)
    db_path: Path = DEFAULT_DB_PATH

    @classmethod
    def from_env(cls) -> "AppConfig":
        provider = os.environ.get("NOVELAI_PROVIDER", "openai")
        api_key = os.environ.get("NOVELAI_API_KEY", "")
        base_url = os.environ.get("NOVELAI_BASE_URL") or None
        model = os.environ.get("NOVELAI_MODEL", "gpt-4o-mini")
        mini_model = os.environ.get("NOVELAI_MINI_MODEL", model)
        embedding_model = os.environ.get("NOVELAI_EMBEDDING_MODEL", "")
        enable_embedding = os.environ.get("NOVELAI_ENABLE_EMBEDDING", "true").lower() in ("true", "1", "yes")
        # db_path 用占位符，模块加载完后由 _init_workspaces() 修正为当前工作区路径
        db_str = os.environ.get("NOVELAI_DB", "__WORKSPACE__")
        return cls(
            ai=AIConfig(
                provider=provider,
                api_key=api_key,
                base_url=base_url,
                model=model,
                mini_model=mini_model,
                embedding_model=embedding_model,
                enable_embedding=enable_embedding,
            ),
            db_path=Path(db_str) if db_str != "__WORKSPACE__" else DEFAULT_DB_PATH,
        )


CONFIG = AppConfig.from_env()


# ============================================================
# 工作区管理（每本小说一个独立数据库）
# ============================================================

def _slugify(title: str) -> str:
    """书名 → 文件系统安全的目录名（保留中文，去特殊符号）"""
    s = _re.sub(r'[<>:"/\\|?*\x00-\x1f]', "", title or "未命名").strip()
    s = _re.sub(r"\s+", "_", s)
    return s[:40] or "未命名"


def _read_project_title(db_path: Path) -> str:
    """从 db 读 project.title（用于列表展示），失败返回文件名"""
    try:
        conn = _sqlite3.connect(str(db_path))
        row = conn.execute("SELECT title FROM project ORDER BY id LIMIT 1").fetchone()
        conn.close()
        return row[0] if row else "未命名小说"
    except Exception:
        return db_path.parent.name


def _migrate_legacy_db() -> None:
    """一次性迁移：把旧的 data/novel.db 移入工作区目录"""
    if not DEFAULT_DB_PATH.exists():
        return  # 没有旧库，无需迁移
    if WORKSPACES_DIR.exists() and any(WORKSPACES_DIR.iterdir()):
        return  # 工作区目录已有内容，说明已迁移过
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    title = _read_project_title(DEFAULT_DB_PATH)
    slug = _slugify(title)
    ws_dir = WORKSPACES_DIR / slug
    if ws_dir.exists():
        ws_dir = WORKSPACES_DIR / f"{slug}_{int(__import__('time').time())}"
    ws_dir.mkdir(parents=True, exist_ok=True)
    # 移动 db + WAL/SHM sidecar
    for suffix in ["", "-wal", "-shm"]:
        src = Path(str(DEFAULT_DB_PATH) + suffix)
        if src.exists():
            _shutil.move(str(src), str(ws_dir / src.name))
    _CURRENT_WS_FILE.write_text(slug, encoding="utf-8")
    print(f"[novelai] 已迁移旧数据库到工作区: {slug}")


def list_workspaces() -> list[dict]:
    """列出所有工作区，每项 {id, title, db_path, created_at}"""
    _migrate_legacy_db()
    if not WORKSPACES_DIR.exists():
        return []
    out = []
    for ws_dir in sorted(WORKSPACES_DIR.iterdir()):
        if not ws_dir.is_dir():
            continue
        db_path = ws_dir / "novel.db"
        if not db_path.exists():
            continue
        out.append({
            "id": ws_dir.name,
            "title": _read_project_title(db_path),
            "db_path": str(db_path),
            "created_at": db_path.stat().st_mtime,
        })
    return out


def get_current_workspace_id() -> str | None:
    """读当前工作区 id；不存在时自动选第一个"""
    _migrate_legacy_db()
    if _CURRENT_WS_FILE.exists():
        ws_id = _CURRENT_WS_FILE.read_text(encoding="utf-8").strip()
        if (WORKSPACES_DIR / ws_dir.name).exists() if False else (WORKSPACES_DIR / ws_id).exists():
            return ws_id
    # 回退：选第一个工作区
    wss = list_workspaces()
    if wss:
        _CURRENT_WS_FILE.write_text(wss[0]["id"], encoding="utf-8")
        return wss[0]["id"]
    return None


def get_current_db_path() -> Path:
    """返回当前工作区的 db 路径；无工作区时回退默认"""
    ws_id = get_current_workspace_id()
    if ws_id:
        return WORKSPACES_DIR / ws_id / "novel.db"
    return DEFAULT_DB_PATH


def create_workspace(title: str) -> dict:
    """建新工作区（生成 slug、建目录、空 db、切换为当前）。返回 {id, db_path}"""
    WORKSPACES_DIR.mkdir(parents=True, exist_ok=True)
    slug = _slugify(title)
    ws_dir = WORKSPACES_DIR / slug
    # slug 冲突加序号
    n = 2
    while ws_dir.exists():
        ws_dir = WORKSPACES_DIR / f"{slug}_{n}"
        n += 1
    ws_dir.mkdir(parents=True, exist_ok=True)
    db_path = ws_dir / "novel.db"
    # 切换为当前（db 文件由 Database 初始化时创建）
    _CURRENT_WS_FILE.write_text(ws_dir.name, encoding="utf-8")
    return {"id": ws_dir.name, "db_path": str(db_path)}


def switch_workspace(ws_id: str) -> str:
    """切换当前工作区，返回 db_path。工作区不存在则抛错"""
    ws_dir = WORKSPACES_DIR / ws_id
    if not ws_dir.exists() or not (ws_dir / "novel.db").exists():
        raise FileNotFoundError(f"工作区不存在: {ws_id}")
    _CURRENT_WS_FILE.write_text(ws_id, encoding="utf-8")
    return str(ws_dir / "novel.db")


def delete_workspace(ws_id: str) -> None:
    """删除工作区（不能删当前）"""
    current = get_current_workspace_id()
    if ws_id == current:
        raise ValueError("不能删除当前工作区")
    ws_dir = WORKSPACES_DIR / ws_id
    if ws_dir.exists():
        _shutil.rmtree(str(ws_dir))


# 模块加载完成：若无 NOVELAI_DB 环境变量，用当前工作区路径修正 CONFIG.db_path
if not os.environ.get("NOVELAI_DB"):
    try:
        _ws_path = get_current_db_path()
        if _ws_path != DEFAULT_DB_PATH or not DEFAULT_DB_PATH.exists():
            CONFIG.db_path = _ws_path
    except Exception:
        pass


def reload_config() -> None:
    """v1.19.26: 重新从环境读 config. 在 api_setup_ai 写 .env 后调用, 不重启进程也能用新 key/model.
    注意: 只更新 ai / db_path 字段 (其他字段未来如果有, 也要补上)
    """
    new = AppConfig.from_env()
    CONFIG.ai = new.ai
    CONFIG.db_path = new.db_path
