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
    recent_chapter_window: int = 3
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
            db_path=Path(os.environ.get("NOVELAI_DB", str(DEFAULT_DB_PATH))),
        )


CONFIG = AppConfig.from_env()


def reload_config() -> None:
    """v1.19.26: 重新从环境读 config. 在 api_setup_ai 写 .env 后调用, 不重启进程也能用新 key/model.
    注意: 只更新 ai / db_path 字段 (其他字段未来如果有, 也要补上)
    """
    new = AppConfig.from_env()
    CONFIG.ai = new.ai
    CONFIG.db_path = new.db_path
