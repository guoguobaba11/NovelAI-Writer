"""测试 importer"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from novelai.config import CONFIG
from novelai.db import Database
from novelai import importer

# 备份原 db
import shutil
src = CONFIG.db_path
backup = str(src) + ".pre_importer.bak"
if os.path.exists(src):
    shutil.copy(src, backup)
    print(f"已备份: {backup}")

# 清空 chapter / volume
# BUG 修复：必须按外键依赖顺序删（先子表后父表），否则 FK 约束报错
db = Database(src)
for t in [
    "consistency_report", "event", "editor_comment", "chapter_version",
    "character_milestone", "relationship_evolution", "ai_call_log",
    "optimization_suggestion", "plot_thread", "chapter",
    "relationship", "character", "volume", "world_setting", "style_rule",
]:
    try:
        db.execute(f"DELETE FROM {t}")
    except Exception:
        pass  # 表不存在则跳过（老库可能缺新表）

# 导入
md_path = os.path.join(os.path.dirname(__file__), "test_manuscript.md")
print(f"导入: {md_path}")
result = importer.import_markdown(
    db, md_path, mode="single",
    project_title="测试书",
    story_time_unit="回",
    progress_cb=lambda s, m: print(f"  [{s}] {m}"),
)
print(f"\n结果: {result}")

# 验证入库
print("\n=== 卷 ===")
for v in db.query("SELECT * FROM volume ORDER BY idx"):
    print(f"  [{v['id']}] 第{v['idx']}卷 {v['title']}")

print("\n=== 章节 ===")
for c in db.query("SELECT id, idx, title, word_count, volume_idx FROM chapter ORDER BY idx"):
    print(f"  [{c['id']}] 第{c['idx']}回 {c['title']} (vol={c['volume_idx']}, {c['word_count']}字)")

# 解析后再跑 parse_markdown_text 直接看看
print("\n=== 解析详情 ===")
text = open(md_path, encoding="utf-8").read()
parsed = importer.parse_markdown_text(text)
for it in parsed:
    print(f"  {it['kind']} #{it.get('idx')} title={it.get('title')!r} vol={it.get('volume_idx')} body_len={len(it.get('body',''))}")
