"""AI 工具调用定义 —— 让编辑器 AI 能主动查询知识库。

工具用 OpenAI function-calling schema 声明，执行时薄封装 knowledge/retriever 现有函数。
仅 openai/openai_compatible provider 启用（anthropic 格式不同，走纯文本）。
"""
from __future__ import annotations

import json
from typing import Any

from .db import Database
from . import knowledge as kb


# ---------- 工具声明（OpenAI tools schema） ----------

TOOL_DEFINITIONS = [
    {
        "type": "function",
        "function": {
            "name": "search_character",
            "description": "按名字或别名查找人物档案（性格、说话风格、MBTI、当前状态等）。当你需要确认某人物的设定或不确定某名字指谁时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "name": {"type": "string", "description": "人物名字或别名"},
                },
                "required": ["name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_fact",
            "description": "查找世界观事实条目（含可靠性：reliable/rumored/secret/false，以及谁知道）。当你需要确认某个设定的真伪或某人物是否知情时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "要查找的事实关键词"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "search_thread",
            "description": "查找伏笔/线索（planted/developing/payoff/resolved 状态 + 描述）。当你需要确认某伏笔的当前状态或避免遗忘线索时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "伏笔关键词"},
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_relationship",
            "description": "查询两个人物之间的关系（类型、当前状态）。当你需要确认人物互动的基调时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "char_a": {"type": "string", "description": "人物 A 名字"},
                    "char_b": {"type": "string", "description": "人物 B 名字"},
                },
                "required": ["char_a", "char_b"],
            },
        },
    },
    # 写章 agentic 专用：AI 自审一致性
    {
        "type": "function",
        "function": {
            "name": "check_self_consistency",
            "description": "对本章正文做快速一致性检查（信息泄漏/时间线/性格漂移）。写完正文后自审时调用，返回发现的问题清单。",
            "parameters": {
                "type": "object",
                "properties": {
                    "chapter_idx": {"type": "integer", "description": "章节号"},
                },
                "required": ["chapter_idx"],
            },
        },
    },
]


# ---------- 工具执行（薄封装 knowledge） ----------

def _find_character_by_name(db: Database, name: str) -> dict | None:
    """按名字或别名精确/模糊匹配人物。"""
    if not name:
        return None
    all_chars = kb.list_characters(db)
    name_lower = name.strip().lower()
    # 1) 精确匹配 name 或 aliases
    for c in all_chars:
        if c["name"] == name or name in (c.get("aliases") or []):
            return c
    # 2) 包含匹配（兜底）
    for c in all_chars:
        aliases = c.get("aliases") or []
        if name_lower and (name_lower in c["name"].lower() or any(name_lower in a.lower() for a in aliases)):
            return c
    return None


def execute_tool(db: Database, tool_name: str, arguments: dict) -> str:
    """执行一个工具调用，返回给 AI 的文本结果。"""
    try:
        if tool_name == "search_character":
            name = arguments.get("name", "")
            c = _find_character_by_name(db, name)
            if not c:
                return f"未找到名为「{name}」的人物。"
            parts = [f"# {c['name']}（{c.get('role', 'supporting')}）"]
            if c.get("mbti"):
                parts.append(f"MBTI: {c['mbti']}")
            if c.get("basic_info"):
                parts.append(f"基础信息: {c['basic_info']}")
            if c.get("personality"):
                parts.append(f"性格: {c['personality']}")
            if c.get("speech_style"):
                parts.append(f"说话风格: {c['speech_style']}")
            if c.get("status"):
                parts.append(f"当前状态: {c['status']}")
            return "\n".join(parts)

        if tool_name == "search_fact":
            query = arguments.get("query", "")
            if not query:
                return "请提供查询关键词。"
            facts = kb.list_facts(db)
            matched = [f for f in facts if query in (f.get("content") or "") or query in (f.get("category") or "")]
            if not matched:
                return f"未找到与「{query}」相关的事实。"
            lines = []
            for f in matched[:8]:
                known_by = f.get("known_by") or []
                lines.append(
                    f"- [{f.get('reliability', '?')}] {f.get('content', '')}"
                    + (f"（知情者: {len(known_by)} 人）" if known_by else "")
                )
            return "\n".join(lines)

        if tool_name == "search_thread":
            query = arguments.get("query", "")
            if not query:
                return "请提供查询关键词。"
            threads = kb.list_threads(db)
            matched = [t for t in threads if query in (t.get("title") or "") or query in (t.get("description") or "")]
            if not matched:
                return f"未找到与「{query}」相关的伏笔。"
            lines = []
            for t in matched[:6]:
                lines.append(f"- [{t.get('thread_type')}|{t.get('status')}] {t.get('title', '')} —— {t.get('description', '')}")
            return "\n".join(lines)

        if tool_name == "get_relationship":
            ca = arguments.get("char_a", "")
            cb = arguments.get("char_b", "")
            char_a = _find_character_by_name(db, ca)
            char_b = _find_character_by_name(db, cb)
            if not char_a or not char_b:
                return f"未找到人物：{ca if not char_a else ''} {cb if not char_b else ''}".strip()
            rels = kb.list_relationships(db)
            for r in rels:
                if (r["char_a_id"] == char_a["id"] and r["char_b_id"] == char_b["id"]) or \
                   (r["char_a_id"] == char_b["id"] and r["char_b_id"] == char_a["id"]):
                    return f"{ca} ↔ {cb}：{r.get('rel_type', '?')}（{r.get('current_state', '未明确')}）"
            return f"{ca} 与 {cb} 之间暂无明确关系记录。"

        if tool_name == "check_self_consistency":
            from . import consistency as cons_mod
            chapter_idx = arguments.get("chapter_idx", 1)
            chapter = kb.get_chapter_by_idx(db, chapter_idx)
            if not chapter:
                return "章节不存在"
            text = chapter.get("final_text") or chapter.get("draft") or ""
            if not text.strip():
                return "本章无正文"
            issues = cons_mod.hard_check(db, chapter_idx, text)
            if not issues:
                return "一致性检查通过，未发现硬性问题。"
            lines = [f"发现 {len(issues)} 个问题："]
            for i, iss in enumerate(issues[:8], 1):
                lines.append(f"{i}. [{iss.get('severity','?')}] {iss.get('category','?')}: {iss.get('explanation','')[:80]}")
            return "\n".join(lines)

        return f"未知工具: {tool_name}"
    except Exception as e:
        return f"工具执行出错: {e}"


def build_tool_result_message(tool_call: dict, result: str) -> dict:
    """构造 role=tool 的回复消息（OpenAI 格式）。"""
    return {
        "role": "tool",
        "tool_call_id": tool_call.get("id", ""),
        "content": result,
    }


def build_assistant_tool_message(tool_calls: list[dict]) -> dict:
    """构造带 tool_calls 的 assistant 消息（用于多轮消息历史）。"""
    return {
        "role": "assistant",
        "content": "",
        "tool_calls": [
            {
                "id": tc.get("id", ""),
                "type": "function",
                "function": {"name": tc["name"], "arguments": json.dumps(tc.get("arguments", {}), ensure_ascii=False)},
            }
            for tc in tool_calls
        ],
    }
