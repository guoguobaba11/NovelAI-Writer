# NovelAI Writer v2.0.0 — 产品白皮书

> **一个伟大的小说编辑器，跨时代的 AI 辅助编辑**
>
 NovelAI Writer 是一款面向中文网文作者的长篇小说 AI 辅助创作工具。它把叙事学理论（POV 信息边界、伏笔生命周期、事件因果链、MBTI 性格模型）工程化到数据结构和规则引擎里，用"规则筑底 + LLM 做模糊判断 + LLM 做创意优化"的三层架构，为长篇创作提供从构思到成稿的全链路支持。

---

## 1. 产品定位

### 目标用户
- **中文网文作者**：需要用国产模型（DeepSeek/智谱/SiliconFlow）、本地运行、数据不上传
- **严肃长篇创作者**：需要伏笔追踪、一致性检查、叙事结构分析

### 与竞品的核心差异

| 能力 | NovelAI Writer | Sudowrite | NovelCrafter | NovelForge |
|------|---------------|-----------|-------------|------------|
| POV 信息边界（限知视角） | ✅ fact.known_by + retriever 过滤 | ❌ | ❌ | ❌ |
| 伏笔生命周期 | ✅ 4 态 + 超期检测 | ❌ | 2 态 | stub |
| 一致性检查 | ✅ 规则引擎 + LLM 7 维度 | 仅 LLM | 仅 LLM | 仅 LLM |
| AI 工具调用 | ✅ AI 主动查知识库 | ❌ | ❌ | ❌ |
| 改稿 Harness 闭环 | ✅ 预分析→生成→验证→自校验 | ❌ | ❌ | ❌ |
| 本地优先 + 跨 provider | ✅ DeepSeek/智谱/Ollama 等 | ❌ SaaS | ❌ SaaS | ✅ |
| 玻璃态 UI（Apple 风格） | ✅ glassmorphism + SF Pro | ❌ | ❌ | ❌ |

---

## 2. 核心功能

### 2.1 AI 辅助创作（从零写新书）
1. **新建小说向导**：设项目（梗概/文风预设/视角/时间单位）→ 生成大纲（含爽点 hook 标注）→ 写第一章 → 批量生成
2. **文风预设模板**：玄幻/都市/科幻/古言/悬疑 + 自定义
3. **大纲生成**：AI 生成章节目录，每章含 hook（爽点/悬念）、伏笔安排、因果衔接
4. **AI 写章**：完整管线（生成正文→摘要→事件抽取→一致性检查→auto-fix），SSE 流式实时展示

### 2.2 AI 辅助编辑（改已有内容）
1. **AI 改稿 Harness**（5 阶段闭环）：
   - 预分析（规则扫描，具体问题类型如 info_leak/unknown_character）
   - 构建上下文（POV 信息边界 + 伏笔 + 承接 + 世界观 + 关系）
   - AI 工具调用（AI 主动查知识库：search_character/search_fact/search_thread/get_relationship）
   - 流式生成（SSE 实时流字 + 字符计数）
   - 后验证 + 自校验重试（检测引入问题 → 自动修正）
2. **选区 AI**：选中文本 → 浮动"AI 改这段" → 只改选中片段 → diff 卡
3. **计划模式**：AI 先出结构化修改计划（含 hook/context_refs）→ 用户逐项批准
4. **快捷命令**：润色/一致/紧凑/心理/风格 + AI 撰写（空章节从零生成）
5. **段落 diff 卡**：逐段字符级 diff + 逐段采纳/插入/再改/跳过
6. **透明度面板**：展示 AI 看到的上下文（人物/信息边界/伏笔/关系/世界观）

### 2.3 知识图谱
- **统一知识图谱**（5 类节点 + 6 种边）：
  - 节点：人物、事件、伏笔、事实、世界观
  - 边：人物↔人物（关系）、人物↔事件（参与）、事件↔事件（因果链）、人物↔伏笔、事件↔伏笔、人物↔事实（知情）
  - ECharts force 力导向图，按类型过滤，大图 LOD 截断
- **人物小传**：基础档案 + 事件时间线 + 里程碑 + 关系演变曲线 + 相关伏笔
- **关系网**：边宽=亲密度、颜色=信任、虚线=冲突、流动=强关系
- **记忆衰减检测**：超过 N 章未出场的角色提醒

### 2.4 一致性保障系统
1. **规则引擎**（确定性检查，consistency.py + scanner/）：
   - 信息边界泄漏（POV 不应知的事实被提及）
   - 死人复活（status=死亡的角色在后续章节以活人姿态出现）
   - 因果倒置（结果事件先于原因事件）
   - 时间线单调性（章节 story_time 不递减）
   - 事件链断裂（相邻章节关键事件无衔接）
   - 未登记人名（正文出现未在知识库中的名字）
2. **LLM 7 维度审查**：info_leak / timeline / personality / causality / thread / setting / worldbuilding
3. **文风漂移检测**：6 维统计特征 z-score
4. **性格漂移检测**：MBTI 认知功能关键词分析

### 2.5 叙事结构分析
- 节奏曲线（每章字数/事件数/重要度）
- 三幕结构 + 起承转合 4 段分布
- 8 大结构问题检测（转折点缺失/前重后轻/节奏塌陷等）

---

## 3. 技术架构

### 3.1 技术栈
- **后端**：Python 3.13 + FastAPI + uvicorn + SQLite3（WAL 模式）
- **前端**：纯原生 JS SPA（无框架）+ ECharts 5.6
- **桌面**：PyWebView（Win11 WebView2，2520×1680）
- **打包**：PyInstaller onefile（~25MB）
- **AI**：OpenAI SDK（兼容 DeepSeek/SiliconFlow/Ollama 等）+ Anthropic SDK

### 3.2 数据库
- **18 张表**：project / volume / chapter / character / event / plot_thread / fact / world_setting / relationship / relationship_evolution / character_milestone / chapter_version / consistency_report / editor_comment / style_rule / optimization_suggestion / ai_call_log / embedding
- **28 个索引**
- **PRAGMA 优化**：WAL + synchronous=NORMAL + cache_size=8MB + mmap_size=256MB + temp_store=MEMORY

### 3.3 AI 编排（EditorHarness）
```
pre_analyze (规则扫描)
    ↓
build_context (retriever: POV 边界 + 伏笔 + 承接 + 世界观 + 关系)
    ↓
tool_call (AI 主动查知识库，仅 openai/openai_compatible)
    ↓
build_prompt (system + user，注入上下文 + 问题清单 + 编辑原则)
    ↓
chat_stream (SSE 流式，temperature=0.6，max_tokens=8000)
    ↓
post_validate (规则再扫描，对比前后)
    ↓
self_retry (若引入问题，自动修正一轮)
    ↓
report (fixed/introduced/context_summary)
```

### 3.4 AI 写章管线（write_chapter_pipeline）
```
generate_chapter (分段续写：第1段→续写→续写→收束，max 4 段)
    ↓
summarize_chapter (mini_model 压缩 + UNFINISHED_ACTION 标记)
    ↓
extract_events (LLM 抽取事件 + 因果链 + 参与人物映射)
    ↓
apply_status_from_events (death→已死 / disappearance→失踪)
    ↓
update_appearances (出场频率 + 自动分级 minor/supporting)
    ↓
run_consistency_check (LLM 7 维度)
    ↓
auto_fix (high severity 自动重写，max 2 次)
```

### 3.5 检索引擎（retriever）
- **POV 信息边界**：`fact.known_by` 按 POV 过滤，防 info_leak
- **伏笔触发**：planted/developing 状态 + 人物/地点匹配
- **上章承接**：UNFINISHED_ACTION 正则提取
- **下一章前瞻**：注入 N+1 章大纲，让 AI 铺垫
- **embedding 语义检索**：纯 Python cosine（无 numpy），provider 不支持时降级为 LIKE
- **token 预算**：top-K 排序裁断（protagonist > antagonist > major > supporting > minor）

---

## 4. 设计系统

### 4.1 视觉风格
- **Apple 玻璃态**（glassmorphism）：`backdrop-filter: blur(24px) saturate(180%)` + 半透明面板
- **渐变光斑背景**：深蓝紫渐变 + 蓝/紫/青光斑（玻璃面板透出）
- **圆角体系**：10px / 14px / 20px / pill(999px)
- **字体**：SF Pro Display/Text → PingFang SC → Microsoft YaHei
- **圆形图标**：btn-icon border-radius: 50%

### 4.2 设计 Token
| Token | Dark | Light |
|-------|------|-------|
| --accent | rgba(255,255,255,0.95) | #1D1D1F |
| --accent-text | #1A1A2E | #FFFFFF |
| --bg-base | #1A1A2E | #F2F3F7 |
| --bg-glass | rgba(255,255,255,0.08) | rgba(255,255,255,0.65) |
| --blur | blur(24px) saturate(180%) | 同 |
| --radius | 14px | 14px |

### 4.3 字体层次
| 层级 | 用途 | font-size | font-weight |
|------|------|-----------|-------------|
| tool-title | 工具页标题 | 18px | 700 |
| brand-name | 应用名 | 14px | 700 |
| h4 | 区域标题 | 13px | 700 |
| body | 正文 | 14px | 400 |
| btn | 按钮文字 | 13px | 600 |
| ed-chip | 快捷命令 | 12px | 500 |
| placeholder | 占位提示 | 12px | italic |

---

## 5. 用户工作流

### 5.1 创作流程（从零写新书）
```
打开应用 → Dashboard → "开始写新小说"
  → 向导第1步：填书名/梗概/选文风预设/视角/时间单位 → 保存
  → 向导第2步：填目标章节数 → 生成大纲（每章含 hook 标注）→ 确认
  → 向导第3步：点"AI 写第 1 章"
    → 编辑器 AI 面板实时流字（"AI 正在写… 3500 / 10000 字"）
    → 进度气泡切换（生成→摘要→事件→一致性→完成）
    → 完成后"查看本章"按钮 → 自动刷新编辑器
  → 继续写下一章（章列表"+ 写第 N 回" / 工具栏"→"变"+"）
```

### 5.2 编辑流程（改已有内容）
```
编辑器 → 输入指令（或选中文字/快捷命令/AI 撰写）
  → 自动切到 AI tab → spinner 等待
  → 进度气泡（扫描：info_leak 2个 → 已加载：人物/伏笔/世界观）
  → 正文气泡实时流字 → 验证结果（修复 N / 引入 M）
  → 透明度面板（AI 看到的上下文，可折叠）
  → 段落 diff 卡（逐段采纳/跳过/再改）
  → 全部采纳/拒绝/重试
```

### 5.3 诊断流程
```
扫描（一键全扫 4 个扫描器）
  → 问题列表（伏笔/逻辑/文风/性格漂移）
  → AI 修改建议（综合优化）
  → 跳到编辑器修复
```

---

## 6. 配置说明

### .env 配置项
```bash
# AI 后端
NOVELAI_PROVIDER=openai_compatible        # openai / anthropic / openai_compatible
NOVELAI_BASE_URL=https://api.siliconflow.cn/v1
NOVELAI_API_KEY=sk-xxxx
NOVELAI_MODEL=zai-org/GLM-5.2
NOVELAI_MINI_MODEL=zai-org/GLM-5.2

# 语义检索（可选）
NOVELAI_EMBEDDING_MODEL=                  # 留空=自动选择
NOVELAI_ENABLE_EMBEDDING=true             # false=关闭语义检索（省钱）

# 数据库
NOVELAI_DB=data/novel.db
```

### 支持的 Provider
| Provider | base_url | 模型示例 | embedding | 工具调用 |
|----------|----------|---------|-----------|---------|
| SiliconFlow | api.siliconflow.cn/v1 | GLM-5.2 | BAAI/bge-m3（如套餐支持） | ✅ |
| DeepSeek | api.deepseek.com | deepseek-chat | ❌ 降级关键词 | ✅ |
| OpenAI | api.openai.com | gpt-4o-mini | text-embedding-3-small | ✅ |
| Anthropic | - | claude-3 | ❌ 降级关键词 | ❌ 走纯文本 |
| Ollama | localhost:11434/v1 | llama3 | ❌ 降级关键词 | 取决于模型 |

---

## 7. 错误处理体系

### 三层错误信息
1. **用户 toast**：自动识别 6 类常见错误（超时/认证/频率/网络/500/404）+ 修复建议
2. **后端日志**：`[操作名] 第N章 步骤 失败 [异常类型]: 消息` + 堆栈尾部
3. **Python logging**：完整堆栈供深度调试

### SSE 流式错误处理
- 写章 SSE：错误后显示"重试"按钮
- AI 改稿 SSE：错误后显示"重试"按钮（复用上次指令）
- 工具调用失败：静默降级为无工具的纯生成
- embedding 失败：静默降级为关键词匹配

---

## 8. 性能指标

### API 响应时间（实测）
| 端点 | 耗时 |
|------|------|
| /api/dashboard | 72ms |
| /api/chapters | 31ms |
| /api/characters | 33ms |
| /api/editor/chapter/1 | 26ms |

### AI 调用
- AI 改稿：5 phases + 51 chunks + done（~30-60 秒）
- AI 写章：4 phases + 186 chunks（~60-120 秒，取决于字数）
- 大纲生成：~30-60 秒

### 数据库
- 18 表 / 28 索引 / WAL 模式
- 连续请求全 200（无锁冲突）
- 200+ 人物 / 500+ 事件场景下仍流畅（O(N) JSON 遍历 < 5ms）

---

## 9. 文件结构

```
novel_writer/
├── desktop.py                 # PyWebView 桌面入口
├── novelai_desktop_onefile.spec  # PyInstaller 打包配置
├── .env                       # AI 配置
├── novelai/
│   ├── config.py              # 配置（AIConfig / WriterConfig）
│   ├── db.py                  # SQLite 数据库（18 表 + 迁移 + PRAGMA 优化）
│   ├── knowledge.py           # 知识库 CRUD + 级联删除 + 状态自动维护
│   ├── ai_client.py           # AI 客户端（chat/chat_stream/chat_json/chat_with_tools/embed）
│   ├── writer.py              # 写章管线（generate_chapter 分段续写 + pipeline）
│   ├── retriever.py           # 上下文检索（POV 边界 + 伏笔 + embedding）
│   ├── consistency.py         # 规则引擎（info_leak/dead/timeline/continuity）
│   ├── personality.py         # MBTI 性格漂移检测
│   ├── structure.py           # 叙事结构分析（节奏/三幕/起承转合）
│   ├── prompts.py             # 12 个 prompt 模板
│   ├── tools.py               # AI 工具调用定义（4 个工具）
│   ├── embeddings.py          # 纯 Python embedding（cosine/index/search）
│   ├── errors.py              # 统一错误格式化
│   ├── scanner/
│   │   ├── threads.py         # 伏笔扫描（超期/时序/遗漏）
│   │   ├── logic.py           # 逻辑扫描（死人复活/因果/事件链）
│   │   └── style.py           # 文风扫描（6 维 z-score）
│   ├── version_patch.py       # 章节版本树（difflib 增量 patch）
│   ├── docx_writer.py         # Word 导出
│   └── web/
│       ├── app.py             # FastAPI 入口
│       ├── api.py             # 88+ 个 API 端点 + EditorHarness
│       └── static/
│           ├── index.html     # 单页应用 HTML
│           ├── app.js         # ~7500 行 前端逻辑
│           ├── style.css      # ~3400 行 Apple 玻璃态 CSS
│           ├── diff-worker.js # Web Worker 字符级 diff
│           └── echarts.min.js # ECharts 5.6
├── data/
│   └── novel.db               # SQLite 数据库
└── docs/
    └── NOVELAI_WRITER_v2.0.0.md  # 本文档
```

---

## 10. 版本历史

### v2.0.0（当前）
- ✅ AI 辅助创作全流程（向导 → 大纲 → 写章 → 批量）
- ✅ EditorHarness 5 阶段 AI 改稿闭环（预分析→工具调用→流式→验证→自校验）
- ✅ 知识图谱（5 类节点 + 6 种边 + 过滤 + LOD）
- ✅ 人物小传（时间线 + 里程碑 + 关系 + 伏笔）
- ✅ Apple 玻璃态 UI（glassmorphism + SF Pro + 圆形图标）
- ✅ SSE 流式写章（分段续写，最长 2 万字/章）
- ✅ 透明度面板（AI 上下文可审计）
- ✅ 爽点/钩子标注 + 文风预设
- ✅ 统一错误处理（3 层 + 友好翻译 + 重试按钮）
- ✅ 长篇小说规模化（200+ 人物分组折叠 + token 预算 + 查重 + 自动分级）
- ✅ WebSocket 进度推送修复
- ✅ 数据库优化（WAL + mmap + cache 8MB）

### v1.0.0
- 基础编辑器 + AI 改稿 + 扫描系统 + Nord 主题
