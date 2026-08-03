"""
examples/seed_demo.py
快速灌入一份演示项目（《长安拾遗》片段），
让你可以立刻体验：开 CLI → write-chapter 1。
"""
import sys
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from novelai.config import CONFIG
from novelai.db import Database
from novelai import knowledge as kb
import json as _json  # 仅用于演示脚本中的 to_json 兜底


def main():
    db = Database(CONFIG.db_path)
    # 清空（演示用）
    for t in ["consistency_report", "event", "plot_thread", "chapter",
              "fact", "world_setting", "relationship", "character", "project"]:
        db.execute(f"DELETE FROM {t}")

    # 项目
    p = kb.get_or_create_project(db)
    kb.update_project(
        db,
        title="长安拾遗",
        synopsis=(
            "唐玄宗天宝十四年，安史之乱前夕。长安城大理寺评事沈青砚"
            "在破获一桩离奇连环杀人案时，发现所有死者都与一份三十年前的宫廷旧案有关。"
            "随着追查深入，他被卷入太子与右相的权力漩涡，并发现自己的身世远比想象中复杂。"
        ),
        style="古朴冷峻，第三人称限知视角，对话文言夹白话，节奏紧凑；多线叙事。",
        pov_mode="限知视角",
        story_time_unit="日",
    )

    # 世界观
    kb.add_world(db, "地理", "长安城", "百万人口的帝都，皇城居北，坊市制度严格。")
    kb.add_world(db, "政治", "右相李林甫", "权倾朝野的右相，掌控边将与京官任免。")
    kb.add_world(db, "政治", "太子李亨", "东宫太子，暗中积蓄力量。")
    kb.add_world(db, "制度", "大理寺", "掌刑狱复核，沈青砚为评事。")

    # 人物
    shen = kb.add_character(
        db, name="沈青砚", aliases=["沈评事"],
        role="protagonist",
        basic_info="28岁，大理寺评事，身形清瘦，眉目冷峻。出身寒门，父早亡，母改嫁。",
        personality="冷静内敛、逻辑缜密、嫉恶如仇，但面对恩师与旧友会动摇。",
        speech_style="言辞简洁，不喜寒暄，常用反问与短句；审问时多设陷阱。",
        abilities="精通律例、断案如神；剑术平平；观察力极强。",
        arc="从只信证据的孤胆法官 → 学会在政治漩涡中借力打力 → 终成断舍离的孤臣。",
        status="在长安，任大理寺评事",
    )
    lin = kb.add_character(
        db, name="林婉", aliases=["婉娘"],
        role="supporting",
        basic_info="22岁，太常寺乐师之女，沈青砚青梅竹马。会医术，性格温婉而坚韧。",
        personality="外柔内刚，观察细腻，对沈青砚有未言明的情愫。",
        speech_style="轻声细语，但涉及底线时措辞锋利。",
        abilities="懂医理、擅古琴、记忆力惊人。",
        arc="从被保护者 → 主动以身入局救沈青砚。",
        status="在长安，太常寺",
    )
    li = kb.add_character(
        db, name="李琰", aliases=["李公子"],
        role="antagonist",
        basic_info="30岁，右相李林甫义子，任大理寺司直。表面温文尔雅，实则心狠手辣。",
        personality="喜怒不形于色，善于伪装，对威胁会先示好再除之。",
        speech_style="温和有礼，常引经据典；讽刺时不带脏字。",
        abilities="武艺高强、人脉通达、熟读律法可用作伪证。",
        arc="从沈青砚的同窗 → 立场对立 → 终在安史之乱中做出选择。",
        status="在长安，大理寺司直",
    )

    # 关系（注意：seed_demo 未建李林甫这个人物，所以"义父子"用 description 描述）
    kb.add_relationship(db, shen, lin, "青梅竹马", "婉娘与沈青砚有婚约之约", current_state="婉娘待字闺中")
    kb.add_relationship(db, shen, li, "同窗故友", "同科出身，曾互相援手", current_state="因案立场对立的旧友")
    kb.add_relationship(db, shen, li, "受制于右相", "李琰为右相义子，沈青砚不知此事", current_state="李琰表面为友、暗中受右相指使")

    # 事实
    kb.add_fact(
        db, content="三十年前曾发生'承乾宫变'，太子被废，牵连者皆死。",
        category="历史", reliability="reliable",
        established_chapter_id=None,
    )
    kb.add_fact(
        db, content="承乾宫变主谋实际为右相李林甫之父李思训。",
        category="历史", reliability="secret",  # 秘密
        established_chapter_id=None,
    )
    kb.add_fact(
        db, content="沈青砚生父是承乾宫变中幸存的皇孙（化名沈怀远）。",
        category="人物", reliability="rumored",  # POV 暂时只知道传言
        established_chapter_id=None,
    )
    # 限制：沈青砚当前不知道自己是皇孙
    # 所以这条 fact 的 known_by 应为空（公开/上帝）但 reliability 是 rumored
    # 但 POV 角色不应"直接知道"——下面用一个 known_by=[]+reliability=rumored 表示传言
    # 信息边界的硬校验会跳过 reliability=rumored 的事实？目前不会。
    # 想要更严格：把 rumored 视为 POV 已知则需加 known_by=[shen]，否则不加。
    # 这里选择不加 known_by，POV 不直接知道；硬校验会触发 high 信息泄漏。
    # 因此实际写作时应确保沈青砚不会直接"想到"自己的身世。

    # 伏笔
    kb.add_thread(
        db, title="承乾宫变真相",
        description="三十年前承乾宫变的主谋到底是谁？真相对当今朝局有何影响？",
        thread_type="mystery", status="planted",
        related_characters=[li, shen],
    )
    kb.add_thread(
        db, title="沈青砚身世",
        description="沈青砚生父究竟是谁？",
        thread_type="mystery", status="planted",
        related_characters=[shen],
    )
    kb.add_thread(
        db, title="林婉的古琴",
        description="林婉所持古琴是承乾宫变中某位公主的遗物。",
        thread_type="foreshadow", status="planted",
        related_characters=[lin],
    )

    # 章节大纲（前 3 章，足够体验）
    ch1 = kb.add_chapter(
        db, idx=1, title="雨夜仵作",
        outline=(
            "天宝十四年四月十五，长安大雨。大理寺评事沈青砚受命勘查城西义宁坊的一具无名男尸。"
            "死者喉中无伤，心脉却断。沈青砚初步判断为罕见毒杀。"
            "现场遗留一枚断裂的玉佩，纹样是宫中才有的双鸾衔枝。"
            "沈青砚前往义宁坊周边访查时，与右相义子、大理寺司直李琰不期而遇。"
            "李琰看似关心案情，实则试探沈青砚是否注意到玉佩。"
            "沈青砚起疑，但未表露。"
        ),
        story_time_start=1, story_time_end=2,
        location="长安·城西义宁坊", pov_character_id=shen,
    )
    ch2 = kb.add_chapter(
        db, idx=2, title="琴声旧约",
        outline=(
            "次日，沈青砚拜访太常寺乐师之女林婉。婉娘为他疗伤，席间弹奏一曲《广陵散》。"
            "沈青砚注意到婉娘案上的古琴——琴底刻有一个不显眼的承乾年款。"
            "婉娘无意间说起这琴是母亲遗物，外祖母曾为宫中女官。"
            "沈青砚开始把玉佩与古琴联系起来，但因无证据未对婉娘说破。"
        ),
        story_time_start=3, story_time_end=3,
        location="长安·太常寺", pov_character_id=shen,
    )
    ch3 = kb.add_chapter(
        db, idx=3, title="虎嗅蔷薇",
        outline=(
            "第三日，李琰约沈青砚在平康坊酒肆密谈。"
            "李琰透露死者身份是右相府幕僚，并暗示此案涉及东宫。"
            "沈青砚不置可否。李琰临别时将一枚同样纹样的玉佩放在桌上，意味深长。"
            "沈青砚意识到自己已被卷入上层博弈。"
        ),
        story_time_start=4, story_time_end=4,
        location="长安·平康坊", pov_character_id=shen,
    )

    # 给前 3 章添加示例事件（这样 Web 面板能立即显示事件链/时间线）
    kb.add_event(db, chapter_id=ch1, story_time=1.3, sequence_in_chapter=1,
                 title="发现无名尸", summary="义宁坊雨夜发现一具男尸，喉中无伤心脉却断。",
                 event_type="discovery", location="城西义宁坊",
                 participants=[shen], importance=4)
    kb.add_event(db, chapter_id=ch1, story_time=1.7, sequence_in_chapter=2,
                 title="发现玉佩", summary="现场遗留一枚断裂的双鸾衔枝玉佩。",
                 event_type="discovery", location="城西义宁坊",
                 participants=[shen], importance=5)
    kb.add_event(db, chapter_id=ch1, story_time=2.0, sequence_in_chapter=3,
                 title="与李琰相遇", summary="坊间访查时偶遇李琰，对方试探玉佩之事。",
                 event_type="dialogue", location="城西义宁坊",
                 participants=[shen, li], importance=3)
    # cause_event_ids: 第三个事件因"发现玉佩"而生
    # (为简化, 这里我们用 1,2 的 id 拼, 但实际是 add_event 之后用 query 拿 id 比较稳)
    # 简化做法: 让 1, 2 号事件的 id 拼到 3 号的 cause
    e1 = db.query_one("SELECT id FROM event WHERE chapter_id=? AND sequence_in_chapter=1", (ch1,))["id"]
    e2 = db.query_one("SELECT id FROM event WHERE chapter_id=? AND sequence_in_chapter=2", (ch1,))["id"]
    e3 = db.query_one("SELECT id FROM event WHERE chapter_id=? AND sequence_in_chapter=3", (ch1,))["id"]
    db.execute("UPDATE event SET cause_event_ids=? WHERE id=?",
               (Database.to_json([e1, e2]), e3))

    kb.add_event(db, chapter_id=ch2, story_time=3.0, sequence_in_chapter=1,
                 title="婉娘弹琴", summary="林婉为沈青砚弹奏《广陵散》。",
                 event_type="action", location="太常寺",
                 participants=[shen, lin], importance=2)
    kb.add_event(db, chapter_id=ch2, story_time=3.5, sequence_in_chapter=2,
                 title="发现承乾年款", summary="沈青砚在古琴底部发现承乾年款。",
                 event_type="revelation", location="太常寺",
                 participants=[shen, lin], importance=5)
    kb.add_event(db, chapter_id=ch2, story_time=3.8, sequence_in_chapter=3,
                 title="婉娘身世暗示", summary="婉娘无意提及外祖母曾为宫中女官。",
                 event_type="revelation", location="太常寺",
                 participants=[lin], importance=4)

    kb.add_event(db, chapter_id=ch3, story_time=4.0, sequence_in_chapter=1,
                 title="李琰密谈", summary="李琰在平康坊酒肆约见沈青砚。",
                 event_type="dialogue", location="平康坊",
                 participants=[shen, li], importance=4)
    kb.add_event(db, chapter_id=ch3, story_time=4.5, sequence_in_chapter=2,
                 title="揭示死者身份", summary="李琰暗示死者是右相幕僚，案涉东宫。",
                 event_type="revelation", location="平康坊",
                 participants=[li], importance=5)
    kb.add_event(db, chapter_id=ch3, story_time=4.8, sequence_in_chapter=3,
                 title="玉佩再现", summary="李琰临别时留下第二枚同纹玉佩。",
                 event_type="turning_point", location="平康坊",
                 participants=[li], importance=5)

    print("Demo 项目已写入数据库！")
    print(f"DB: {CONFIG.db_path}")
    print("进入 CLI: python run.py")
    print("然后输入 write-chapter 1 体验端到端生成。")


if __name__ == "__main__":
    main()
