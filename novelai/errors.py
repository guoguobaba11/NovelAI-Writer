"""统一的错误格式化工具——让每个错误消息都包含"在哪一步、操作什么、出了什么问题"。

用法：
    from novelai.errors import err_detail, log_error
    try:
        ...
    except Exception as e:
        msg = err_detail("AI 改稿", idx=3, step="生成阶段", e=e)
        _log("error", msg)
"""
from __future__ import annotations
import traceback
import logging

_log = logging.getLogger("novelai.errors")


def err_detail(
    operation: str,
    *,
    idx: int | None = None,
    chapter_title: str | None = None,
    step: str | None = None,
    entity: str | None = None,
    e: Exception | None = None,
    extra: str = "",
) -> str:
    """格式化详细的错误消息。

    返回形如：
    [AI 改稿] 第3章《长安惊变》生成阶段失败 [AICallError]: timeout
    提示：可能是 AI 模型响应慢，请重试或增大超时

    参数：
        operation: 操作名（"AI 改稿"/"写章"/"抽取事件"/"大纲生成"等）
        idx: 章节号（可选）
        chapter_title: 章节标题（可选）
        step: 具体步骤（"生成正文"/"摘要"/"事件抽取"/"一致性检查"等，可选）
        entity: 涉及的实体名（人物名/伏笔标题等，可选）
        e: 异常对象（可选）
        extra: 额外信息
    """
    parts = [f"[{operation}]"]

    # 章节/实体上下文
    if idx is not None:
        title_str = f"《{chapter_title}》" if chapter_title else ""
        parts.append(f"第{idx}章{title_str}")

    if entity:
        parts.append(f"实体「{entity}」")

    # 步骤
    if step:
        parts.append(f"{step}")

    # 错误类型和消息
    if e:
        exc_type = type(e).__name__
        exc_msg = str(e)[:200]
        if exc_msg:
            parts.append(f"失败 [{exc_type}]: {exc_msg}")
        else:
            parts.append(f"失败 [{exc_type}]")
    else:
        parts.append("失败")

    if extra:
        parts.append(f"| {extra}")

    return " ".join(parts)


def log_exception(
    operation: str,
    e: Exception,
    *,
    idx: int | None = None,
    step: str | None = None,
    logger=None,
) -> str:
    """记录详细异常到日志，返回格式化的用户可见消息。

    日志里包含完整堆栈（方便开发者定位），返回值只包含摘要（给用户看）。
    """
    msg = err_detail(operation, idx=idx, step=step, e=e)
    tb = traceback.format_exc()

    # 完整堆栈写到 Python logging（开发者查 logfile）
    log = logger or _log
    log.error("%s\n%s", msg, tb)

    # 同时返回精简消息（给前端/用户）
    return msg


def friendly_hint(e: Exception) -> str:
    """根据异常类型给出用户友好的修复建议。"""
    msg = str(e).lower()
    exc_type = type(e).__name__

    if "timeout" in msg or "timed out" in msg:
        return "AI 模型响应超时，请重试或检查网络"
    if "401" in msg or "authentication" in msg or "api key" in msg or "unauthorized" in msg:
        return "API Key 无效或已过期，请检查 .env 中的 NOVELAI_API_KEY"
    if "429" in msg or "rate limit" in msg or "quota" in msg:
        return "请求频率过高或额度用尽，请稍后重试"
    if "connection" in msg or "fetch" in msg or "network" in msg or "unreachable" in msg:
        return "网络连接失败，请检查网络或代理设置"
    if "json" in msg or "parse" in msg or "decode" in msg:
        return f"AI 返回的内容格式异常（{exc_type}），可能是模型输出不稳定"
    if "not found" in msg or "不存在" in msg or "no such" in msg:
        return "请求的数据不存在，可能已被删除或未创建"
    if "duplicate" in msg or "already exists" in msg or "已存在" in msg:
        return "数据已存在，请勿重复创建"
    if "operational" in msg and "database" in msg.lower():
        return "数据库操作异常，可能正在被其他操作锁定，请稍后重试"
    return f"未知错误 [{exc_type}]，请查看日志了解详情"
