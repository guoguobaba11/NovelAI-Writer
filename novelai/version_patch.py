"""
novelai.version_patch
章节版本的增量 patch 引擎。

把"上一版正文 → 本版正文"的差异编码成紧凑的 JSON patch，
反向应用即可重建本版正文。用标准库 difflib，不引入新依赖。

patch 格式（两种 op）：
  {"op": "full", "text": "..."}              # 全量快照（基线版 / patch 反而更大时退化用）
  {"op": "patch", "ops": [
      ["e", i1, i2],          # equal: 保留 parent[i1:i2]
      ["r", i1, i2, "新文"],   # replace: 用 "新文" 替换 parent[i1:i2]
      ["i", i1, "插入文"],     # insert: 在 parent[i1] 位置前插入 "插入文"
      ["d", i1, i2],          # delete: 删除 parent[i1:i2]
  ]}

设计目标：
  1. 往返一致：apply_patch(parent, make_patch(parent, child)) == child
  2. 紧凑：连续 equal 段不存文本，只存 [i1,i2] 索引
  3. 自适应：patch 比 full 还大时（文本几乎全改）退化为 full，省空间
  4. 健壮：apply_patch 解析/索引错时抛异常，调用方负责 fallback
"""
from __future__ import annotations
import json
import difflib


def make_patch(parent_text: str, child_text: str) -> str:
    """生成 parent → child 的 patch（JSON 字符串）。

    parent_text 为空时直接退化为 full 快照。
    若 patch 编码后比 child_text 还大，也退化为 full（避免负优化）。
    """
    if not parent_text:
        return json.dumps({"op": "full", "text": child_text}, ensure_ascii=False)

    sm = difflib.SequenceMatcher(a=parent_text, b=child_text, autojunk=False)
    ops: list = []
    for tag, i1, i2, j1, j2 in sm.get_opcodes():
        if tag == "equal":
            # 只在确实有内容时记 equal，避免尾部空 equal
            if i2 > i1:
                ops.append(["e", i1, i2])
        elif tag == "replace":
            ops.append(["r", i1, i2, child_text[j1:j2]])
        elif tag == "insert":
            ops.append(["i", i1, child_text[j1:j2]])
        elif tag == "delete":
            ops.append(["d", i1, i2])

    patch_json = json.dumps({"op": "patch", "ops": ops}, ensure_ascii=False)
    full_json = json.dumps({"op": "full", "text": child_text}, ensure_ascii=False)
    # patch 更大就退化（*1.0 即严格比较；中文 difflib 已较优，留点余量用 1.0）
    if len(patch_json) >= len(full_json):
        return full_json
    return patch_json


def apply_patch(parent_text: str, patch_json: str) -> str:
    """应用 patch 重建 child_text。

    解析失败或索引越界时抛 (ValueError / IndexError / KeyError)，
    调用方应 try/except 并 fallback 到祖先版。
    """
    p = json.loads(patch_json)
    op = p.get("op")
    if op == "full":
        return p["text"]
    if op != "patch":
        raise ValueError(f"unknown patch op: {op!r}")

    out: list[str] = []
    n = len(parent_text)
    for entry in p["ops"]:
        kind = entry[0]
        if kind == "e":
            _, i1, i2 = entry
            if not (0 <= i1 <= i2 <= n):
                raise IndexError(f"equal range [{i1},{i2}] out of parent len {n}")
            out.append(parent_text[i1:i2])
        elif kind == "r":
            _, i1, i2, new = entry
            if not (0 <= i1 <= i2 <= n):
                raise IndexError(f"replace range [{i1},{i2}] out of parent len {n}")
            out.append(new)
        elif kind == "i":
            _, i1, new = entry
            if not (0 <= i1 <= n):
                raise IndexError(f"insert pos {i1} out of parent len {n}")
            out.append(new)
        elif kind == "d":
            _, i1, i2 = entry
            if not (0 <= i1 <= i2 <= n):
                raise IndexError(f"delete range [{i1},{i2}] out of parent len {n}")
            # 不 append parent 切片 = 删除
        else:
            raise ValueError(f"unknown op kind: {kind!r}")
    return "".join(out)


def is_full_snapshot(patch_json: str) -> bool:
    """快速判断该 patch 是否为全量快照（重建无需 parent）。"""
    try:
        return json.loads(patch_json).get("op") == "full"
    except Exception:
        return False
