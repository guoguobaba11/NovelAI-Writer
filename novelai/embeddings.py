"""纯 Python embedding 语义检索（无 numpy/torch 依赖，兼容 onefile 打包）。

设计：
- 向量由 AIClient.embed() 生成（调 OpenAI embeddings API）
- 存 db.embedding 表（entity_type, entity_id, text_hash, vector_json）
- 查询时全表读内存算 cosine top-k（单本小说实体 < 数千，O(N) 够用）
- provider 不支持 embeddings 时抛 NotImplementedError，调用方降级回 LIKE

失效策略：实体的源文本变了（text_hash 不匹配）就重算；retriever.invalidate_cache()
在实体增删改时调用，可同步清理对应 embedding 行。
"""
from __future__ import annotations

import hashlib
import json
import math
import time
from typing import Any, Callable, Iterable

from .db import Database


def _text_hash(text: str) -> str:
    """稳定的文本指纹（用于判断源文本是否变化）。"""
    return hashlib.md5(text.encode("utf-8")).hexdigest()


def cosine(a: list[float], b: list[float]) -> float:
    """纯 Python 余弦相似度（不引 numpy）。"""
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = 0.0
    na = 0.0
    nb = 0.0
    for x, y in zip(a, b):
        dot += x * y
        na += x * x
        nb += y * y
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def _get_all(db: Database, entity_type: str) -> list[dict]:
    """读某类型的所有缓存向量。"""
    rows = db.query(
        "SELECT entity_id, text_hash, vector_json, model FROM embedding WHERE entity_type = ?",
        (entity_type,),
    )
    out = []
    for r in rows:
        try:
            vec = json.loads(r["vector_json"])
            out.append({
                "entity_id": r["entity_id"],
                "text_hash": r["text_hash"],
                "vector": vec,
                "model": r["model"],
            })
        except (json.JSONDecodeError, TypeError):
            continue  # 损坏的向量跳过
    return out


def ensure_indexed(
    db: Database,
    ai,
    entity_type: str,
    items: list[dict],
    text_fn: Callable[[dict], str],
    id_fn: Callable[[dict], int] = lambda x: x["id"],
    model: str = "text-embedding-3-small",
) -> bool:
    """确保一批实体的 embedding 已索引（源文本变了才重算）。

    返回 True 表示全部成功索引；False 表示 provider 不支持（已抛 NotImplementedError 由调用方捕获）。
    items: 实体列表（dict）
    text_fn: 从实体取"可嵌入文本"的函数（通常是 name+aliases+description）
    id_fn: 从实体取 id
    """
    if not items:
        return True
    cached = {r["entity_id"]: r for r in _get_all(db, entity_type)}
    # 找出需要（重新）计算的：无缓存 / text_hash 变了 / 模型变了
    to_embed: list[tuple[int, str]] = []
    for it in items:
        eid = id_fn(it)
        text = (text_fn(it) or "").strip()
        if not text:
            continue
        th = _text_hash(text)
        c = cached.get(eid)
        if not c or c["text_hash"] != th or c.get("model") != model:
            to_embed.append((eid, text))

    if not to_embed:
        return True  # 全部命中缓存

    # 批量生成 embedding（一次 API 调用）
    texts = [t for _, t in to_embed]
    vectors = ai.embed(texts, model=model)  # 可能抛 NotImplementedError
    if len(vectors) != len(to_embed):
        # 数量不匹配（某些 provider 返回不规整），部分写入
        vectors = vectors[: len(to_embed)] + [None] * (len(to_embed) - len(vectors))

    now = time.time()
    for (eid, text), vec in zip(to_embed, vectors):
        if not vec:
            continue
        th = _text_hash(text)
        db.execute(
            """INSERT INTO embedding (entity_type, entity_id, text_hash, vector_json, model, ts)
               VALUES (?, ?, ?, ?, ?, ?)
               ON CONFLICT(entity_type, entity_id) DO UPDATE SET
                 text_hash=excluded.text_hash, vector_json=excluded.vector_json,
                 model=excluded.model, ts=excluded.ts""",
            (entity_type, eid, th, json.dumps(vec), model, now),
        )
    return True


def search(
    db: Database,
    query_vec: list[float],
    entity_type: str,
    top_k: int = 10,
) -> list[tuple[int, float]]:
    """在已索引的实体中按余弦相似度检索 top_k。

    返回 [(entity_id, score), ...] 按 score 降序。空 query_vec 返回 []。
    """
    if not query_vec:
        return []
    cached = _get_all(db, entity_type)
    scored = [(r["entity_id"], cosine(query_vec, r["vector"])) for r in cached]
    scored.sort(key=lambda x: -x[1])
    return scored[:top_k]


def invalidate(db: Database, entity_type: str | None = None, entity_id: int | None = None) -> None:
    """失效 embedding 缓存。

    无参数：清空全部（切主题/换模型时用）。
    只给 entity_type：清某类型全部。
    都给：清具体一条。
    """
    if entity_type is None:
        db.execute("DELETE FROM embedding")
    elif entity_id is None:
        db.execute("DELETE FROM embedding WHERE entity_type = ?", (entity_type,))
    else:
        db.execute(
            "DELETE FROM embedding WHERE entity_type = ? AND entity_id = ?",
            (entity_type, entity_id),
        )
