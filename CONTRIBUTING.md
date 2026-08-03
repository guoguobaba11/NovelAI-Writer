# 贡献指南

感谢你对 NovelAI Writer 的兴趣！欢迎提交 Issue、Pull Request 或建议。

## 如何贡献

### 报告 Bug
1. 先搜索已有 Issue，避免重复
2. 用 Bug 报告模板提交，附上复现步骤和日志

### 提交代码
1. Fork 本仓库
2. 创建分支：`git checkout -b feature/你的功能名`
3. 提交更改：`git commit -m "添加了什么功能"`
4. 推送：`git push origin feature/你的功能名`
5. 发起 Pull Request

### 开发环境
```bash
git clone https://github.com/guoguobaba11/NovelAI-Writer.git
cd NovelAI-Writer
pip install -r requirements.txt -r requirements-desktop.txt
cp .env.example .env  # 填入你的 API key
python run.py
```

### 代码规范
- Python 后端：遵循 PEP 8
- JavaScript 前端：纯原生 JS，无框架
- CSS：使用 CSS 变量（design token），不硬编码颜色
- 提交信息：中文描述即可

## 项目结构
- `novelai/` — Python 后端（FastAPI + SQLite）
- `novelai/web/static/` — 前端（JS + CSS + HTML）
- `desktop.py` — PyWebView 桌面入口
- `docs/` — 文档
