"""调试 unknown_char 检测"""
import re

STOPWORDS = set("""
的 了 在 是 我 你 他 她 它 我们 你们 他们 她们 它们 和 与 或 但 而
也 都 还 就 要 会 能 可以 不会 不曾 已经 仍然 正在 突然 终于 然后
于是 但是 不过 然而 啊 呢 吗 吧 嗯 哦 啊 哎 喂
这里 那里 那个 这个 什么 怎么 为什么 怎样 哪里 哪个
""".split())

text = "沈青砚与陈三在义宁坊相遇。陈三告诉他案子的真相。"

name_set = {"沈青砚", "林婉", "李琰"}
common_surnames = "王李张刘陈杨黄赵周吴徐孙马朱胡郭何高林罗宋郑谢韩唐冯于董萧程曹袁邓许傅沈曾彭吕苏卢蒋蔡贾丁魏薛叶阎余潘杜戴夏钟汪田任姜范方石姚谭廖邹熊金陆郝孔白崔康毛邱秦江史顾侯邵孟龙万段雷钱汤尹黎易常武乔贺赖龚文"

candidates = set(re.findall(r"[\u4e00-\u9fa5]{2,4}", text))
print("candidates:", candidates)
unknown = []
for cand in candidates:
    if cand in name_set:
        continue
    if cand in STOPWORDS:
        continue
    if cand[0] in common_surnames and cand not in name_set and len(cand) <= 3:
        unknown.append(cand)
print("unknown:", unknown)
