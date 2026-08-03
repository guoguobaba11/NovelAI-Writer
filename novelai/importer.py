"""
novelai.importer
从 Markdown 格式手稿导入整本书。

支持两种模式：
1. **单文件模式**：一本书 = 一个 .md 文件，按"卷头 / 回头"自动切分
2. **目录模式**：目录 = 一卷，每文件 = 一回（文件名或文件内首行匹配）

识别规则（按优先级）：
- 卷头：
    `^# 第N卷` / `^# 卷N` / `^# Volume N`
- 回头：
    `^## 第N回` / `^## 第一回`（中文一二三...十百）/ `^## Chapter N` / `^# 第N章`

回内首段若是大纲/设定说明（非正文），会标 is_meta=True。
"""
from __future__ import annotations
import re
from pathlib import Path
from typing import Any, Callable
from .db import Database
from . import knowledge as kb


# ============================================================
# 文本解析
# ============================================================

# 中文数字 1-99
_CN_NUM = {
    "零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
    "六": 6, "七": 7, "八": 8, "九": 9, "十": 10,
    "百": 100, "千": 1000,
}


def _cn_to_int(s: str) -> int | None:
    """把"十二"/"二十"/"一百零五" 解析为 int。仅支持简单情形。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    if s in _CN_NUM:
        return _CN_NUM[s]
    # 十几
    if len(s) == 2 and s[0] == "十" and s[1] in _CN_NUM:
        return 10 + _CN_NUM[s[1]]
    # X十Y
    if "十" in s:
        parts = s.split("十")
        tens = _CN_NUM.get(parts[0], 1) if parts[0] else 1
        ones = _CN_NUM.get(parts[1], 0) if parts[1] else 0
        return tens * 10 + ones
    return None


# 各种回头正则
HEAD_PATTERNS = [
    # 第N回 / 第N章 / 第N节
    (re.compile(r"^#{1,4}\s*第\s*([0-9一二三四五六七八九十百千零两]+)\s*[回章节]\s*(.*)$"), "hui"),
    (re.compile(r"^#{1,4}\s*Chapter\s+(\d+)\s*[:：]?\s*(.*)$", re.IGNORECASE), "hui"),
    (re.compile(r"^#{1,4}\s*Chap\.?\s+(\d+)\s*[:：]?\s*(.*)$", re.IGNORECASE), "hui"),
]

# 各种卷头正则
VOLUME_PATTERNS = [
    (re.compile(r"^#{1,3}\s*第\s*([0-9一二三四五六七八九十百千零两]+)\s*卷\s*(.*)$"), "vol"),
    (re.compile(r"^#{1,3}\s*Volume\s+(\d+)\s*[:：]?\s*(.*)$", re.IGNORECASE), "vol"),
    (re.compile(r"^#{1,3}\s*Part\s+(\d+)\s*[:：]?\s*(.*)$", re.IGNORECASE), "vol"),
]

# 元段提示词——若回正文以这些词开头，认为是元数据
_META_HINTS = ("【", "（", "设定", "梗概", "本章大纲", "本章设定", "本章简介", "本章梗概", "Outline", "Synopsis")


def _parse_heading(line: str) -> tuple[str, int, str] | None:
    """尝试把一行解析为回头或卷头。返回 (kind, num, title) 或 None"""
    for pat, kind in VOLUME_PATTERNS:
        m = pat.match(line.strip())
        if m:
            num = _cn_to_int(m.group(1))
            if num is None:
                try:
                    num = int(m.group(1))
                except Exception:
                    num = 0
            return (kind, num, m.group(2).strip())
    for pat, kind in HEAD_PATTERNS:
        m = pat.match(line.strip())
        if m:
            num = _cn_to_int(m.group(1))
            if num is None:
                try:
                    num = int(m.group(1))
                except Exception:
                    num = 0
            return (kind, num, m.group(2).strip())
    return None


def parse_markdown_text(text: str) -> list[dict]:
    """
    把整本 MD 文本解析为 [{kind: 'volume'|'chapter', idx, title, body}, ...]
    """
    lines = text.splitlines()
    out: list[dict] = []
    cur_vol_idx = 0
    cur_vol_title = ""
    cur_hui_idx = 0
    cur_hui_title = ""
    cur_body: list[str] = []
    cur_kind: str | None = None
    in_meta = False  # 当前回内是否在元段

    def flush():
        nonlocal cur_hui_idx, cur_hui_title, cur_body, cur_kind
        if cur_kind == "chapter" and cur_hui_idx > 0:
            body = "\n".join(cur_body).strip()
            out.append({
                "kind": "chapter",
                "volume_idx": cur_vol_idx,
                "idx": cur_hui_idx,
                "title": cur_hui_title,
                "body": body,
                "is_meta": body.startswith(_META_HINTS) if body else False,
            })
        elif cur_kind == "volume" and cur_vol_idx > 0:
            body = "\n".join(cur_body).strip()
            out.append({
                "kind": "volume",
                "idx": cur_vol_idx,
                "title": cur_vol_title,
                "synopsis": body,
            })
        cur_hui_idx = 0
        cur_hui_title = ""
        cur_body = []
        cur_kind = None

    for line in lines:
        parsed = _parse_heading(line)
        if parsed:
            kind, num, title = parsed
            # 同一行如果是卷头，前面有未结束的回，先 flush 回
            if cur_kind == "chapter":
                flush()
            # 同一行是新的回/卷，前面是未结束的卷头，先 flush 卷
            if cur_kind == "volume" and kind == "hui":
                flush()
            if kind == "vol":
                if cur_kind == "volume":
                    flush()
                cur_vol_idx = num
                cur_vol_title = title
                cur_kind = "volume"
            elif kind == "hui":
                cur_hui_idx = num
                cur_hui_title = title
                cur_kind = "chapter"
        else:
            if cur_kind:
                cur_body.append(line)
    # 收尾
    flush()
    return out


# ============================================================
# 导入主流程
# ============================================================

def import_markdown(
    db: Database,
    path: str | Path,
    mode: str = "auto",
    project_title: str | None = None,
    story_time_unit: str = "回",
    progress_cb: Callable[[str, str], None] | None = None,
) -> dict:
    """
    mode:
      - auto: 自动识别（单文件 → 单文件模式；目录 → 目录模式）
      - single: 单文件
      - directory: 目录（每文件 = 一回；目录名/首行 = 卷）
    """
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(str(p))

    if progress_cb:
        progress_cb("import:start", f"导入 {p}")

    if mode == "auto":
        mode = "directory" if p.is_dir() else "single"

    if mode == "single":
        result = _import_single_file(db, p, project_title, story_time_unit, progress_cb)
    else:
        result = _import_directory(db, p, project_title, story_time_unit, progress_cb)

    if progress_cb:
        progress_cb("import:done", f"完成：{result['chapters']} 章，{result['words']} 字")
    return result


def _import_single_file(
    db: Database,
    path: Path,
    project_title: str | None,
    story_time_unit: str,
    progress_cb: Callable | None,
) -> dict:
    text = path.read_text(encoding="utf-8")
    if project_title:
        kb.update_project(db, title=project_title, story_time_unit=story_time_unit)
    parsed = parse_markdown_text(text)
    n_ch, n_words = _persist_parsed(db, parsed, import_source=str(path), progress_cb=progress_cb)
    return {"chapters": n_ch, "words": n_words, "volumes": sum(1 for x in parsed if x["kind"] == "volume")}


def _import_directory(
    db: Database,
    dirpath: Path,
    project_title: str | None,
    story_time_unit: str,
    progress_cb: Callable | None,
) -> dict:
    """目录模式：第一层子目录 = 卷；卷内文件 = 回"""
    if project_title:
        kb.update_project(db, title=project_title, story_time_unit=story_time_unit)
    md_files = sorted(dirpath.rglob("*.md"))
    if not md_files:
        raise ValueError(f"目录 {dirpath} 下没有 .md 文件")

    # 按父目录分组
    by_parent: dict[Path, list[Path]] = {}
    for f in md_files:
        by_parent.setdefault(f.parent, []).append(f)

    sorted_parents = sorted(by_parent.keys())
    total_chapters = 0
    total_words = 0
    total_vols = 0
    for vol_i, parent in enumerate(sorted_parents, 1):
        vol_name = parent.name
        # 卷表
        existing_vol = kb.get_volume_by_idx(db, vol_i)
        if existing_vol:
            kb.update_volume(db, existing_vol["id"], title=vol_name)
            vid = existing_vol["id"]
        else:
            vid = kb.add_volume(db, idx=vol_i, title=vol_name)
        total_vols += 1
        if progress_cb:
            progress_cb("import:volume", f"卷 {vol_i}: {vol_name}")
        files = sorted(by_parent[parent])
        # 卷内文件按"回"顺序：从文件名提中文/阿拉伯数字
        def _file_sort_key(p: Path) -> tuple[int, str]:
            m = re.search(r"第\s*([0-9一二三四五六七八九十百千零两]+)\s*回", p.stem)
            if m:
                num = _cn_to_int(m.group(1))
                if num is None:
                    try:
                        num = int(m.group(1))
                    except Exception:
                        num = 9999
            else:
                # 退化：找任何数字
                m2 = re.search(r"(\d+)", p.stem)
                num = int(m2.group(1)) if m2 else 9999
            return (num, p.name)
        files = sorted(files, key=_file_sort_key)
        # 从每个文件名提取"第N回"作为 idx（而非重置 1,2,3...）
        def _file_idx(p: Path) -> int:
            m = re.search(r"第\s*([0-9一二三四五六七八九十百千零两]+)\s*回", p.stem)
            if m:
                num = _cn_to_int(m.group(1))
                if num is None:
                    try:
                        num = int(m.group(1))
                    except Exception:
                        num = 0
                return num or 0
            return 0
        for f in files:
            hui_i = _file_idx(f)
            if hui_i <= 0:
                # B-新32: 之前用 `total_chapters := []` 永远空 list, 全章 idx 撞 1. 改用 total_chapters + 1
                hui_i = total_chapters + 1
            text = f.read_text(encoding="utf-8")
            # 尝试从首行取标题
            first_line = text.splitlines()[0] if text else ""
            heading = _parse_heading(first_line)
            if heading:
                title = heading[2]
            else:
                # 去 # 后的首行
                title = first_line.lstrip("#").strip() or f.stem
            # 入库
            existing = kb.get_chapter_by_idx(db, hui_i)
            if existing:
                import logging as _logging
                _logging.getLogger("novelai.importer").warning(
                    f"目录导入：第 {hui_i} 回({title}) 与已有章节序号冲突，将覆盖《{existing['title']}》（源文件：{f}）"
                )
                kb.update_chapter(
                    db, existing["id"],
                    title=title,
                    final_text=text,
                    draft=text,
                    word_count=len(text),
                    import_source=str(f),
                    volume_idx=vol_i,
                )
                cid = existing["id"]
            else:
                cid = kb.add_chapter(
                    db,
                    idx=hui_i, title=title,
                    outline="",
                    story_time_start=hui_i, story_time_end=hui_i,
                    location="",
                    pov_character_id=None,
                )
                kb.update_chapter(
                    db, cid,
                    final_text=text, draft=text, word_count=len(text),
                    import_source=str(f), volume_idx=vol_i,
                )
            total_chapters += 1
            total_words += len(text)
            if progress_cb:
                progress_cb("import:chapter", f"  第 {hui_i} 回：{title} ({len(text)} 字)")
    return {"chapters": total_chapters, "words": total_words, "volumes": total_vols}


def _persist_parsed(
    db: Database,
    parsed: list[dict],
    import_source: str,
    progress_cb: Callable | None,
) -> tuple[int, int]:
    """把 parse_markdown_text 的结果入库。"""
    cur_vol_id: int | None = None
    n_ch = 0
    n_words = 0
    for item in parsed:
        if item["kind"] == "volume":
            existing = kb.get_volume_by_idx(db, item["idx"])
            if existing:
                kb.update_volume(
                    db, existing["id"],
                    title=item["title"] or existing["title"],
                    synopsis=item["synopsis"],
                )
                cur_vol_id = existing["id"]
            else:
                cur_vol_id = kb.add_volume(
                    db, idx=item["idx"], title=item["title"] or f"第{item['idx']}卷",
                    synopsis=item["synopsis"],
                )
            if progress_cb:
                progress_cb("import:volume", f"卷 {item['idx']}: {item['title']}")
        elif item["kind"] == "chapter":
            body = item["body"]
            title = item["title"] or f"第{item['idx']}回"
            existing = kb.get_chapter_by_idx(db, item["idx"])
            payload = dict(
                title=title,
                final_text=body,
                draft=body,
                word_count=len(body),
                import_source=import_source,
                volume_idx=item["volume_idx"] or 0,
            )
            if existing:
                kb.update_chapter(db, existing["id"], **payload)
            else:
                cid = kb.add_chapter(
                    db,
                    idx=item["idx"], title=title,
                    outline="",
                    story_time_start=item["idx"], story_time_end=item["idx"],
                    location="",
                    pov_character_id=None,
                )
                kb.update_chapter(db, cid, **payload)
            n_ch += 1
            n_words += len(body)
            if progress_cb:
                progress_cb("import:chapter", f"  第 {item['idx']} 回：{title} ({len(body)} 字)")
    return n_ch, n_words
