# NovelAI Writer v2.0.0

> **一个伟大的小说编辑器，跨时代的 AI 辅助编辑**

NovelAI Writer 是一款面向中文网文作者的长篇小说 AI 辅助创作工具。它把叙事学理论（POV 信息边界、伏笔生命周期、事件因果链、MBTI 性格模型）工程化到数据结构和规则引擎里，用"规则筑底 + LLM 做模糊判断 + LLM 做创意优化"的三层架构，为长篇创作提供从构思到成稿的全链路支持。

## 核心亮点

- **AI 辅助创作全流程**：设项目 → 生成大纲（含爽点 hook）→ AI 写章（分段续写，最长 2 万字/章）→ 批量生成
- **AI 改稿 Harness 闭环**：预分析 → 工具调用（AI 主动查知识库）→ 流式生成 → 后验证 → 自校验重试
- **POV 信息边界**：限知视角一致性保障，AI 改稿时绝不泄露角色不应知道的信息
- **知识图谱**：人物 / 事件 / 伏笔 / 事实 / 世界观 五类节点 + 六种关联边的统一可视化
- **伏笔生命周期**：planted → developing → payoff → resolved 四态追踪 + 超期检测
- **Apple 玻璃态 UI**：glassmorphism 设计 + SF Pro 字体 + 圆形图标 + 渐变光斑背景
- **本地优先 + 跨 Provider**：支持 SiliconFlow / DeepSeek / OpenAI / Ollama 等国产模型

## 快速开始

### 环境要求
- Python 3.10+
- Windows 10/11（推荐）/ macOS / Linux

### 安装

```bash
git clone https://github.com/你的用户名/NovelAI-Writer.git
cd NovelAI-Writer
pip install -r requirements.txt -r requirements-desktop.txt
```

### 配置 AI

编辑 `.env`（从 `.env.example` 复制）：

```bash
# SiliconFlow（推荐，支持工具调用 + 免费额度）
NOVELAI_PROVIDER=openai_compatible
NOVELAI_BASE_URL=https://api.siliconflow.cn/v1
NOVELAI_API_KEY=你的key
NOVELAI_MODEL=zai-org/GLM-5.2

# 或 DeepSeek
# NOVELAI_BASE_URL=https://api.deepseek.com
# NOVELAI_MODEL=deepseek-chat

# 或 OpenAI
# NOVELAI_PROVIDER=openai
# NOVELAI_MODEL=gpt-4o-mini
```

### 运行

```bash
# Web 模式（浏览器）
python run.py

# 桌面模式（原生窗口）
python desktop.py

# 打包为 exe
python -m PyInstaller novelai_desktop_onefile.spec --clean
```

## 功能列表

### AI 辅助创作
| 功能 | 说明 |
|------|------|
| 新建小说向导 | 梗概 → 文风预设（玄幻/都市/科幻/古言/悬疑）→ 生成大纲 → 写第一章 |
| 大纲生成 | AI 生成章节目录，每章含 hook（爽点/悬念）、伏笔安排、因果衔接 |
| AI 写章 | 完整管线（生成→摘要→事件→一致性→auto-fix），SSE 流式实时展示 |
| 分段续写 | 长章节自动分段（最多 4 段），支持 2 万字/章 |
| 批量生成 | 一键生成多章，进度推送 |
| 字数控制 | 前端可设每章目标字数（5千/1万/1.5万/2万快捷按钮） |

### AI 辅助编辑
| 功能 | 说明 |
|------|------|
| AI 改稿 Harness | 5 阶段闭环：预分析→上下文→工具调用→流式生成→验证+自校验 |
| AI 工具调用 | AI 主动查知识库（search_character/fact/thread/relationship） |
| 选区 AI | 选中文本 → 浮动按钮 → 只改选中片段 |
| 计划模式 | AI 先出结构化修改计划 → 用户逐项批准执行 |
| 段落 diff 卡 | 字符级 diff + 逐段采纳/插入/再改/跳过 |
| 透明度面板 | 展示 AI 看到的上下文（人物/信息边界/伏笔/关系/世界观） |
| 快捷命令 | 润色/一致/紧凑/心理/风格 + AI 撰写（空章节从零生成） |

### 知识图谱
| 功能 | 说明 |
|------|------|
| 统一知识图谱 | 5 类节点 + 6 种边，force 力导向图，按类型过滤 |
| 人物小传 | 事件时间线 + 里程碑 + 关系演变 + 相关伏笔 |
| 关系网 | 边宽=亲密度、颜色=信任、虚线=冲突、流动=强关系 |
| 记忆衰减 | 超过 N 章未出场的角色提醒 |
| 人物分组 | 200+ 人物按重要度分层（主角/反派/重要配角/常规配角/次要人物） |

### 一致性保障
| 功能 | 说明 |
|------|------|
| POV 信息边界 | fact.known_by + retriever 过滤，防 info_leak |
| 死人复活检测 | status=死亡的角色在后续章节以活人姿态出现 |
| 因果倒置检测 | 结果事件先于原因事件 |
| 时间线单调性 | 章节 story_time 不递减 |
| 事件链断裂 | 相邻章节关键事件无衔接 |
| 文风漂移 | 6 维统计特征 z-score |
| 性格漂移 | MBTI 认知功能关键词分析 |

### 叙事分析
| 功能 | 说明 |
|------|------|
| 节奏曲线 | 每章字数/事件数/重要度 |
| 三幕结构 | 按 0.25/0.75 切分 |
| 起承转合 | setup/development/climax/resolution 四段分布 |
| 8 大结构问题 | 转折点缺失/前重后轻/节奏塌陷等 |

## 技术栈

- **后端**：Python 3.13 + FastAPI + SQLite3（WAL）
- **前端**：纯原生 JS SPA + ECharts 5.6
- **桌面**：PyWebView（WebView2）
- **打包**：PyInstaller onefile（~25MB）
- **AI**：OpenAI SDK（兼容 DeepSeek/SiliconFlow/Ollama）+ Anthropic SDK

## 项目结构

```
novel_writer/
├── desktop.py                    # 桌面入口
├── novelai/
│   ├── config.py                 # 配置
│   ├── db.py                     # 数据库（18表 + PRAGMA优化）
│   ├── knowledge.py              # 知识库 CRUD
│   ├── ai_client.py              # AI 客户端（chat/stream/tools/embed）
│   ├── writer.py                 # 写章管线（分段续写）
│   ├── retriever.py              # 上下文检索（POV边界+伏笔+embedding）
│   ├── consistency.py            # 规则引擎
│   ├── prompts.py                # 12个prompt模板
│   ├── tools.py                  # AI工具调用定义
│   ├── embeddings.py             # 纯Python语义检索
│   ├── errors.py                 # 统一错误格式化
│   ├── scanner/                  # 扫描器（伏笔/逻辑/文风）
│   └── web/                      # FastAPI + 前端
└── docs/                         # 文档
```

## 配置说明

### 支持的 Provider

| Provider | 模型示例 | Embedding | 工具调用 |
|----------|---------|-----------|---------|
| SiliconFlow | GLM-5.2 | BAAI/bge-m3 | ✅ |
| DeepSeek | deepseek-chat | ❌ 降级 | ✅ |
| OpenAI | gpt-4o-mini | text-embedding-3-small | ✅ |
| Anthropic | claude-3 | ❌ 降级 | ❌ |
| Ollama | llama3 | ❌ 降级 | 取决于模型 |

### .env 配置项

```bash
NOVELAI_PROVIDER=openai_compatible
NOVELAI_BASE_URL=https://api.siliconflow.cn/v1
NOVELAI_API_KEY=你的key
NOVELAI_MODEL=zai-org/GLM-5.2
NOVELAI_MINI_MODEL=zai-org/GLM-5.2
NOVELAI_EMBEDDING_MODEL=          # 留空=自动选择
NOVELAI_ENABLE_EMBEDDING=true     # false=关闭语义检索
```

## 快捷键

| 快捷键 | 功能 |
|--------|------|
| Ctrl+. | 切换深色/浅色主题 |
| F | 专注写作模式（隐藏所有 chrome） |
| Ctrl+S | 保存章节 |
| Ctrl+Enter | 发送 AI 指令 |
| Ctrl+Z / Ctrl+Shift+Z | 撤销/重做 |
| Ctrl+F | 查找替换 |
| Alt+← / Alt+→ | 上一章/下一章 |
| G + E/S/O | 跳转编辑器/扫描/优化 |

## 开源协议

[MIT License](LICENSE) — 随便用，随便改。

## 致谢

- [ECharts](https://echarts.apache.org/) 数据可视化
- [FastAPI](https://fastapi.tiangolo.com/) 后端框架
- [PyWebView](https://pywebview.flowrl.com/) 桌面封装
