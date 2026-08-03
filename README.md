# NovelAI Writer — 长篇小说 AI 辅助写作系统

> 解决 AI 写长篇小说的核心痛点：**事件链断裂、时间线混乱、人物性格漂移、逻辑矛盾、信息把控失控**。

## 核心理念

不是把所有设定塞进 prompt（那样会爆 token、容易失焦、模型会幻觉），
而是用一个**结构化知识库 + 上下文检索引擎 + 程序化硬校验 + LLM 一致性审查**的组合拳。

```
┌────────────────────────────────────────────────────────┐
│                  知识库 (SQLite)                       │
│  人物 / 关系 / 世界观 / 事实(含 known_by) / 章节 /    │
│  事件(因果+时间) / 伏笔(状态机) / 一致性报告            │
└───────────────┬────────────────────────────────────────┘
                │
                ▼
┌────────────────────────────────────────────────────────┐
│  上下文检索引擎 (retriever.py)                         │
│  按章节上下文动态召回: POV 档案 / 信息边界 / 临近事件 /  │
│  相关伏笔 / 地点世界观 / 上一章未完成动作                │
└───────────────┬────────────────────────────────────────┘
                │
                ▼
┌────────────────────────────────────────────────────────┐
│  章节生成 (writer.py)                                   │
│  system prompt 强调 10 条铁律                          │
│  + user prompt 携带完整上下文                            │
└───────────────┬────────────────────────────────────────┘
                │
                ▼
┌────────────────────────────────────────────────────────┐
│  摘要 + 事件抽取                                        │
│  提取本章事件(因果链节点) / 摘要(含 UNFINISHED_ACTION)  │
└───────────────┬────────────────────────────────────────┘
                │
                ▼
┌────────────────────────────────────────────────────────┐
│  一致性双层校验                                          │
│  LLM 维度审查: 7 类问题(JSON 结构化输出)                 │
│  + 硬规则 (consistency.py): 信息边界/时间线/人名/未完成  │
└───────────────┬────────────────────────────────────────┘
                │
                ▼
        自动重写 / 写库 / 报告
```

## 四大一致性如何被保证

| 维度 | 机制 | 代码位置 |
|---|---|---|
| **事件链** | 每章末尾强制 `UNFINISHED_ACTION`；下一章 `retriever` 强制召回；事件表 `cause_event_ids` 显式记因果；硬校验 `_check_unfinished_continuity` 检查关键词回应 | `writer.py` / `consistency.py` |
| **时间顺序** | 事件表 `story_time` 单调；硬校验 `_check_timeline_monotonic`；摘要含时间戳；prompt 强调时序 | `db.py` event 表 / `consistency.py` |
| **人物性格** | `character` 表存 `personality / speech_style / arc`；上下文 prompt 强制引用；LLM 一致性审查"personality 漂移"维度 | `knowledge.py` / `prompts.py` CONSISTENCY_SYSTEM |
| **信息把控** | `fact.known_by` 字段记录谁知道；`retriever.facts_known_by` 给 POV 限定的 fact 清单；硬校验 `_check_info_leak` 关键词匹配检查 | `knowledge.py` / `consistency.py` |

## 项目结构

```
novel_writer/
├── novelai/
│   ├── __init__.py
│   ├── config.py          # 全局配置（环境变量 + .env）
│   ├── db.py              # SQLite 封装 + Schema
│   ├── knowledge.py       # 知识库 CRUD
│   ├── ai_client.py       # AI 适配（OpenAI/Anthropic/兼容）
│   ├── prompts.py         # 所有 prompt 模板
│   ├── retriever.py       # 上下文检索引擎
│   ├── writer.py          # 章节生成 + 流水线
│   ├── consistency.py     # 程序化硬校验（不依赖 LLM）
│   └── cli.py             # 交互式 CLI
├── examples/
│   └── seed_demo.py       # 演示数据（长安拾遗）
├── data/                  # SQLite 落盘
├── .env.example
├── requirements.txt
├── run.py
└── README.md
```

## 快速开始

### 1. 安装

```bash
cd novel_writer
pip install -r requirements.txt
```

### 2. 配置 API key

```bash
cp .env.example .env
# 编辑 .env 填入 NOVELAI_API_KEY
```

支持的 AI 后端：

- **OpenAI** (默认) — 设 `NOVELAI_PROVIDER=openai`，模型 `gpt-4o-mini` 起
- **Anthropic** — 设 `NOVELAI_PROVIDER=anthropic`，模型 `claude-3-5-sonnet-latest` 等
- **OpenAI 兼容服务**（DeepSeek/Moonshot/智谱/Ollama 等）— 设 `NOVELAI_PROVIDER=openai_compatible`，并填 `NOVELAI_BASE_URL`

例：使用 DeepSeek：
```
NOVELAI_PROVIDER=openai
NOVELAI_BASE_URL=https://api.deepseek.com/v1
NOVELAI_API_KEY=sk-...
NOVELAI_MODEL=deepseek-chat
```

### 3. 灌入演示数据

```bash
python examples/seed_demo.py
```

### 4. 启动 CLI

```bash
python run.py
```

### 5. 启动实时进度面板（推荐）

```bash
# 方式 A：在 CLI 里输入
novel> web

# 方式 B：直接启动
python -m web.app

# 然后浏览器打开 http://127.0.0.1:8765
```

> Web 面板是把握**当前时间、事件、节奏推进过程**的实时看板，强烈建议和 CLI 一起用。

## CLI 命令速查

```text
# 元信息
init
set-synopsis <文本>        # 故事梗概
set-style <文本>           # 文风
set-pov 限知视角|全知视角
set-unit 日|小时|不定       # 故事内时间单位

# 知识库
add-character              # 交互式
add-character-json '{...}'
list-characters
show-character <name>
add-world <cat> <name> <content>
add-fact <content> --category=... --reliability=reliable|rumored|secret|false --known-by=1,2|public
add-relationship <a_name> <b_name> <type> [state]
add-thread <title> <desc> --type=... --status=planted|developing|payoff|resolved

# 章节
add-chapter <idx> <title> --outline=... --time=1~2 --pov=沈青砚 --location=...
list-chapters
show-chapter <idx>
show-events [idx]
timeline

# AI
generate-outline [target_chapters=30]   # 让 AI 帮你把大纲生出来
show-context <idx>                       # 调试：看本章被召回了什么
write-raw <idx> --words=3000             # 只生成正文，不检查
write-chapter <idx> --words=3000         # 端到端：生成→摘要→事件→一致性→入库
check <idx> <text-file>                  # 对外部文本做硬校验
hard-check <idx>                         # 对已入库章节做硬校验
```

## 推荐工作流

### 从零开始

```text
set-synopsis "你的故事一句话"
set-style "古朴冷峻，第三人称限知"
set-pov 限知视角
set-unit 日

# 把人物先建好
add-character
# ... 把所有主要人物建好

add-world 政治 "东宫" "..."
add-world 制度 "大理寺" "..."

# 建几条初始事实（注意 known_by 决定 POV 看不看到）
add-fact "三十年前曾发生 X 事" --category=历史 --reliability=reliable --known-by=public
add-fact "主角其实是皇孙" --category=人物 --reliability=secret --known-by=public
# 上面的写法让事实库里存在，但 POV 不知道——硬校验就会防止正文泄漏。

# 埋几条伏笔
add-thread "玉佩之谜" "死者所携玉佩来历" --type=mystery --status=planted

# 让 AI 帮你把大纲生出来
generate-outline 30

# 调大纲
show-chapter 1
add-chapter 1 "第1章 xxx" --outline=... --time=1~2 --pov=xxx --location=xxx

# 端到端生成第一章
write-chapter 1 --words=3000

# 不满意的话重写
write-chapter 1 --words=3000
```

### 已经手写了若干章

```text
# 1) 把人物/设定/事实录入
# 2) 创建章节并粘贴你的草稿到 final_text：
#    (用 SQL 或扩展 add-chapter 命令)
# 3) hard-check <idx> 看问题清单
# 4) 让 AI 修订
write-raw 5
```

## 进阶：信息把控的正确姿势

`fact.known_by` 是这个系统的"灵魂字段"。它必须根据你**视角模式**严格设置：

- **限知视角 + POV=沈青砚**：
  - 沈青砚知道的事实：`known_by=[shen_id]`
  - 沈青砚不知道的事实（"上帝全知"但 POV 不知道）：`known_by=[]` 且 `reliability=secret|rumored`
  - 公开/江湖传言：`known_by=[]` 但你想让"谁都能知道"则将 reliability=reliable

- **硬校验触发条件**：`fact.known_by` 不为空且 **不包含 POV 角色**，且正文出现该 fact 关键词 → 标记 high 信息泄漏。

举例：

```text
# 主角身世：secret，且 POV 不知道
add-fact "沈青砚生父是皇孙" --reliability=secret --known-by=public
# 写正文时如果出现"沈青砚想到自己其实是皇孙" → 硬校验会报警。
```

## 进阶：手动修订一致性

如果 LLM 一致性检查没找出的问题你肉眼看到了，可以：

```text
# 1) 把不存在的角色登记
add-character name=陈三 role=supporting basic_info=...

# 2) 把新事实入库
add-fact "陈三是李琰线人" --reliability=reliable --known-by=public

# 3) 更新伏笔状态
# (目前需要直接 SQL，未来会加 update-thread 命令)
```

## 实时进度面板（Web UI）

Web 面板让你**时刻把握当前时间、事件、节奏的推进过程**。它是后端的可视化层，所有数据来自同一份 SQLite，所有改动双向同步。

### 启动

```bash
# 方式 A：CLI 里
novel> web

# 方式 B：直接启动
python -m web.app
# 浏览器打开 http://127.0.0.1:8765
```

### 面板布局

```
┌────────────────────────────────────────────────────────────────┐
│ 标题 | 故事内时间 | 当前章节 | 字数 | 状态 | [▶生成] [⟳刷新]   │
├──────────┬─────────────────────────────────┬──────────────────┤
│ 章节     │  Tab 切换：                      │ 详情             │
│ 人物     │   📅 时间线                     │  - 大纲/摘要     │
│ 伏笔     │   🔗 事件链                     │  - 事件列表      │
│ 事件     │   📈 节奏曲线                   │  - 伏笔状态      │
│          │   🕸 人物关系                   │  - 一致性问题    │
│          │   ⚠ 一致性                      │  - 硬校验按钮    │
│          │                                 │                  │
│          ├─────────────────────────────────┤                  │
│          │ 实时日志（WebSocket 推送）       │                  │
└──────────┴─────────────────────────────────┴──────────────────┘
```

### 五大视图

1. **📅 时间线**（ECharts 横向布局）
   - x 轴 = 故事内时间
   - 章节显示为彩色横向条
   - 事件显示为散点（按类型上色：动作/对话/揭示/转折/决定/发现）
   - 伏笔用 ◆ 埋设，用 ★ 揭晓
   - 鼠标悬停看详情

2. **🔗 事件链**（ECharts 关系图）
   - 节点 = 事件（按重要度决定大小，按类型上色）
   - 边 = `cause_event_ids` 显式因果
   - 支持拖拽、缩放

3. **📈 节奏曲线**（ECharts 多 series）
   - 柱：每章字数
   - 线：事件数 / 事件平均重要度 / 未解决伏笔数
   - 柱：每章新增伏笔数
   - 散点：每章一致性 high 问题数（红色 = 报警点）

4. **🕸 人物关系网**（ECharts 力导向图）
   - 节点 = 人物（主角/反派/配角按颜色区分）
   - 边 = `relationship` 表（带关系类型标签）
   - 可拖拽

5. **⚠ 一致性**：最近 20 条一致性报告，每条按章节列出 high/medium/low 问题数量

### 实时能力

- **WebSocket** 推送生成进度（生成章节时底部日志实时滚动）
- **▶ 生成当前章节** 按钮：从前端触发生成 + 检查 + 入库
- **硬校验** 按钮：选中章节后可直接在右侧跑程序化校验，弹窗显示问题清单
- **15s 兜底轮询**：任何外部修改（CLI 操作）也会自动反映到面板

### REST API（供外部脚本）

| 端点 | 说明 |
|---|---|
| `GET /api/project` | 项目元信息 |
| `GET /api/progress` | KPI 概览 |
| `GET /api/chapters` | 全部章节 |
| `GET /api/chapter/{idx}` | 章节详情（含事件） |
| `GET /api/events` | 全部事件 |
| `GET /api/threads` | 全部伏笔 |
| `GET /api/timeline` | 时间线数据 |
| `GET /api/rhythm` | 节奏曲线数据 |
| `GET /api/relationship_network` | 关系网数据 |
| `GET /api/recent_issues?limit=10` | 最近一致性问题 |
| `POST /api/regenerate/{idx}` | 异步重新生成章节 |
| `POST /api/hard_check/{idx}` | 硬校验 |
| `GET /api/scan/threads` | 伏笔扫描 |
| `GET /api/scan/logic` | 逻辑链扫描 |
| `GET /api/scan/style` | 文风漂移扫描 |
| `GET /api/volumes` | 卷列表 |
| `POST /api/import` | 导入 Markdown 手稿（body: `{path, title, story_time_unit}`） |
| `WS /api/ws` | 实时日志推送 |

## 修改模式：导入已有手稿

> 适合"已有初稿（≥几十万字）+ 修改阶段"的作者。

### 工作流

```bash
# 1) 准备手稿（按卷/回组织）
# 方式 A：单文件 .md，格式如下
#   # 第一卷 长安惊变
#   ## 第一回 雨夜仵作
#   （正文...）
#   ## 第二回 ...
#   # 第二卷 暗流涌动
#   ## 第三回 ...
# 方式 B：每回一个 .md 文件，按目录组织（一级子目录 = 一卷）

# 2) 启动 CLI 或 Web
novel> import-md examples/我的初稿.md --title=《长安拾遗》--unit=回

# 3) 跑扫描
novel> scan-threads     # 伏笔问题
novel> scan-logic       # 逻辑链问题
novel> scan-style       # 文风漂移
novel> scan-all         # 一键全扫
```

### 三个扫描器覆盖的痛点

#### 1. 伏笔扫描（`scan-threads`）
- **no_payoff**：埋了没解（伏笔在第 N 章埋设，至今未揭晓）
- **overdue**：超期（埋了太久还没解 → 暗示读者已忘记）
- **premature_payoff**：未埋先解（某个 chapter 被标为已解决但找不到埋设记录）
- **causality_reversed**：因果倒置（解决章节 < 埋设章节）
- **abandoned_important**：标记为 abandoned 但描述里说"重要"

#### 2. 逻辑链扫描（`scan-logic`）
- **💀 死人复活**：角色在第 N 章已死（fact 表），但后续章节正文里以"X 走/说/看..."等活人姿态出现
- **📍 地点冲突**：同一人物在同一章内极短时间内出现在两个不同地点
- **⏪ 因果倒置**：事件 B 的 cause_event_ids 包含 A，但 B 的 story_time < A
- **🕳 信息泄漏**：限知视角下，POV 角色使用了"他不应知道"的事实（关键词匹配 secret/rumored 事实）
- **🔌 事件链断裂**：相邻两章之间没有任何事件以对方章节为因

#### 3. 文风漂移扫描（`scan-style`）
- 对每章正文提取 7 维指纹：
  - 平均句长 / 句长标准差
  - 对话占比 / 感叹/问句比例
  - 独白/内心戏比例 / 描写密度 / 句子数
- 默认以前 3 章为基线，每维度算 z-score
- |z| ≥ 2.0 报警；≥ 3.0 high
- 额外绘制"综合距离曲线"——一眼看出哪一章风格最飘

### 数据模型新增

- `volume` 表（idx, title, synopsis, style_notes, word_count）
- `chapter.volume_idx`（章节归属卷）
- `chapter.import_source`（记录是从哪个文件导入的）

## 数据备份

数据库就一个文件：`data/novel.db`。直接复制即可备份。

## 桌面应用（Windows .exe 打包）

把工具打包成单一 Windows .exe（≈80-120MB），双击启动原生窗口，不需浏览器。

### 准备（Windows 上）

1. 安装 **Python 3.10+**：[python.org](https://www.python.org/downloads/)（勾 "Add to PATH"）
2. 下载本项目，进入目录
3. 双击 `build.bat` —— 自动装依赖 + PyInstaller 打包（5-10 分钟）
4. 产物：`dist\NovelAI Writer.exe`

### 首次使用

1. 双击 `NovelAI Writer.exe` 启动
2. 同目录会自动创建 `.env` 文件，填入 `NOVELAI_API_KEY=sk-...`
3. 重新启动 .exe 即可
4. 窗口里默认深色主题——按 `Ctrl + .` 切浅色，按 `F` 进专注写作模式

### 启动模式

```bash
# 默认：原生窗口
python desktop.py
# 或已打包的 NovelAI Writer.exe

# 浏览器模式（不开原生窗口，用默认浏览器）
python desktop.py --no-gui
```

### 跨平台

`desktop.py` 在 macOS / Linux 也能用（PyWebView 用系统自带 WebKit），但 `.exe` 只能 Windows 上生成。

| 平台 | 启动方式 | 浏览器引擎 |
|---|---|---|
| **Windows** | `NovelAI Writer.exe`（双击） | WebView2（Win11 内置） |
| **macOS** | `python desktop.py` | WKWebView |
| **Linux** | `python desktop.py`（需装 `python3-gi gir1.2-webkit2-4.0`） | WebKitGTK |

### 打包参数调节

`novelai_desktop.spec` 顶部可调：

- `EXE(name='NovelAI Writer')` — 改 exe 文件名
- `EXE(console=False)` — True=显示控制台（调试用）
- `EXE(icon='...')` — 自定义图标（已默认 assets/icon.ico）
- `excludes=['matplotlib', ...]` — 删除的模块（减体积）

减小体积：装 [UPX](https://github.com/upx/upx/releases) 后放在 `C:\upx\` 即可，spec 已 `upx=True`。

## 常见问题

**Q: AI 没装能跑吗？**
A: 可以。`init` / `add-character` / `add-fact` / `add-chapter` / `hard-check` 这些命令都不需要 AI。先把知识库建好，再装 AI。

**Q: 中文 token 很贵，怎么办？**
A: 上下文工程已经做了精简。如果还嫌贵，可以：
- `target_words` 调小到 2000
- `recent_chapter_window` 调到 2
- 用 DeepSeek / 本地 Ollama 模型
- 主力模型用 `gpt-4o-mini`，强推理章节手动切到 `claude-3-5-sonnet`

**Q: 怎么判断信息泄漏是真的？**
A: 硬校验用关键词匹配是粗筛，LLM 一致性审查会精筛。两者都通过就比较稳。

---

## 后续可扩展方向

- **向量检索**：embedding + 混合检索
- **多本书管理**：当前一个 db 一本书
- **大纲版本管理**：保留大纲历史版本
- **多智能体协作**：大纲师 / 文笔师 / 审校师 / 修改师
- **导出长文格式**：EPUB / Word
