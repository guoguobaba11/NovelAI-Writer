"""测试 optimizer（mock AI 离线跑）"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from novelai.config import CONFIG
from novelai.db import Database
from novelai import knowledge as kb
from novelai import optimizer


class MockAI:
    """模拟 LLM 返回的优化建议"""
    ready = True
    def chat_json(self, messages, temperature=0.5, model=None):
        # 模拟建议
        return {
            "suggestions": [
                {
                    "title": "沈青砚 性格偏离 INTJ baseline",
                    "content": "在第 1-4 章中，沈青砚的对话过于直白简短（平均 8 字/句），缺乏 INTJ 典型的战略纵深。建议在第 2 章让他有一段关于'如何破局'的独白（100-200 字），展现 Ni 的远见 + Te 的逻辑推演。\n\n修改示例：\n原文：「此案甚怪。」\n改为：「此案有三处不合常理：尸者喉中无伤而心脉断，是药非刃；死者面带微笑，与被杀之人情状相悖；现场遗玉佩非民间物，指向宫禁。三点相合，背后必有大案。」\n\n副作用：让 POV 角色单方面长独白可能拖慢节奏。建议穿插婉娘的疑问作为节奏点。",
                    "priority": "high",
                    "evidence": "baseline_overlap=0.00；function_scores 显示 Te 得分 0.2（基线 0.5）",
                    "chapter_focus": "第 1-3 章",
                },
                {
                    "title": "劣势功能 Se 在第 4 章异常活跃（grip 压力）",
                    "content": "沈青砚是 INTJ（Ni-Te-Fi-Se），劣势功能是 Se。第 4 章'沈青砚忽然从地上站起来，看着远方的灯火，默默叹息'——这是 Se 的感官当下体验（站起来/灯火/叹息），符合 INTJ 在压力下的 grip 状态。\n\n但 grip 状态不应持续，建议：\n1. 第 5 章让他恢复 Ni-Te 主导（做一次逻辑推演或战略判断）\n2. 或者在第 4 章前增加 1-2 章的累积压力描写（被人追杀/同僚背叛），让 grip 有因可循",
                    "priority": "medium",
                    "evidence": "function_scores 中 Se=0.7（高于基线）",
                    "chapter_focus": "第 4-5 章",
                },
            ]
        }


db = Database(CONFIG.db_path)
ai = MockAI()
opt = optimizer.Optimizer(db, ai)

# 1) 性格优化
print("=== optimize_personality 沈青砚 ===")
sugs = opt.optimize_personality("沈青砚")
print(f"  生成 {len(sugs)} 条建议")
for s in sugs:
    print(f"  - [{s['priority']}] {s['title'][:30]}")
print()

# 2) 弧光优化
print("=== optimize_arc 沈青砚 ===")
sugs = opt.optimize_arc("沈青砚")
print(f"  生成 {len(sugs)} 条建议")
for s in sugs:
    print(f"  - [{s['priority']}] {s['title'][:30]}")
print()

# 3) 关系优化
print("=== optimize_relationship 沈青砚 李琰 ===")
sugs = opt.optimize_relationship("沈青砚", "李琰")
print(f"  生成 {len(sugs)} 条建议")
for s in sugs:
    print(f"  - [{s['priority']}] {s['title'][:30]}")
print()

# 4) 全局优化
print("=== optimize_all ===")
sugs = opt.optimize_all()
print(f"  生成 {len(sugs)} 条建议")
for s in sugs:
    print(f"  - [{s['priority']}] {s['title'][:30]}")
print()

# 5) 验证入库
print("=== list_suggestions ===")
all_sugs = kb.list_suggestions(db, status="open")
print(f"  open 状态: {len(all_sugs)} 条")
high_n = sum(1 for s in all_sugs if s.get("priority") == "high")
print(f"  high 优先级: {high_n} 条")

# 按类型分组
by_type = {}
for s in all_sugs:
    t = s.get("target_type", "global")
    by_type.setdefault(t, []).append(s)
for t, arr in by_type.items():
    print(f"  {t}: {len(arr)} 条")

# 6) 测试 apply / dismiss
if all_sugs:
    sid = all_sugs[0]["id"]
    kb.update_suggestion_status(db, sid, "applied")
    print(f"\n  标记 #{sid} 为 applied")
    s = kb.get_suggestion(db, sid)
    print(f"  status: {s['status']}, applied_at: {s.get('applied_at')}")

print()
print("ALL OPTIMIZER TESTS PASSED")
