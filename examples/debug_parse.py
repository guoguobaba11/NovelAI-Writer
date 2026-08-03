import sys
sys.path.insert(0, ".")
from novelai.importer import _parse_heading, _cn_to_int
lines = ['# 第一卷 长安惊变', '## 第一回 雨夜仵作', '# 第二卷 暗流涌动', '## 第三回 虎嗅蔷薇']
for l in lines:
    p = _parse_heading(l)
    print(repr(l), '->', p)
print()
print('cn_to_int one:', _cn_to_int('一'))
print('cn_to_int two:', _cn_to_int('二'))
