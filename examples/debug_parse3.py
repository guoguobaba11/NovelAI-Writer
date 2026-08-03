import sys
sys.path.insert(0, ".")
import re
from novelai.importer import _parse_heading, _cn_to_int

text = open("examples/test_manuscript.md", encoding="utf-8").read()
lines = text.splitlines()

# 复制 parse_markdown_text 但加 print
out = []
cur_vol_idx = 0
cur_vol_title = ""
cur_hui_idx = 0
cur_hui_title = ""
cur_body = []
cur_kind = None

def flush():
    global cur_hui_idx, cur_hui_title, cur_body, cur_kind, cur_vol_idx, cur_vol_title
    print(f"  FLUSH cur_kind={cur_kind} vol_idx={cur_vol_idx} hui_idx={cur_hui_idx} body_len={len(cur_body)}")
    if cur_kind == "chapter" and cur_hui_idx > 0:
        body = "\n".join(cur_body).strip()
        out.append({"kind": "chapter", "volume_idx": cur_vol_idx, "idx": cur_hui_idx,
                    "title": cur_hui_title, "body": body,
                    "is_meta": body.startswith(("【","（","设定")) if body else False})
    elif cur_kind == "volume" and cur_vol_idx > 0:
        body = "\n".join(cur_body).strip()
        out.append({"kind": "volume", "idx": cur_vol_idx, "title": cur_vol_title, "synopsis": body})
    cur_hui_idx = 0
    cur_hui_title = ""
    cur_body = []
    cur_kind = None

for i, line in enumerate(lines):
    parsed = _parse_heading(line)
    if parsed:
        kind, num, title = parsed
        print(f"  line[{i}] {line!r} -> kind={kind} num={num} title={title!r} | cur_kind={cur_kind}")
        if cur_kind == "chapter":
            flush()
        if cur_kind == "volume" and kind == "chapter":
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
flush()
print("---")
print("Total out:", len(out))
for o in out:
    print(" ", o["kind"], o.get("idx"), o.get("title"))
