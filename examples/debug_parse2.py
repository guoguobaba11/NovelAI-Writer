import sys
sys.path.insert(0, ".")
from novelai.importer import parse_markdown_text
text = open("examples/test_manuscript.md", encoding="utf-8").read()
parsed = parse_markdown_text(text)
print("Total parsed:", len(parsed))
for i, it in enumerate(parsed):
    print(f"  [{i}] kind={it['kind']} idx={it.get('idx')} title={it.get('title')!r} vol={it.get('volume_idx')}")
