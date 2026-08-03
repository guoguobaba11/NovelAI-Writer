"""
novelai.web.app — FastAPI 应用入口
用法：
  uvicorn novelai.web.app:app --reload --port 8765
或：
  python -m novelai.web.app
"""
from __future__ import annotations
import sys
from pathlib import Path
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse

from . import api


def _static_dir() -> Path:
    """静态文件目录：开发模式走源码；PyInstaller 模式走 _MEIPASS。

    spec 里 datas 把 'novelai/web/static' 打包到 _MEIPASS/novelai/web/static/。
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS) / "novelai" / "web" / "static"
    return Path(__file__).parent / "static"


STATIC_DIR = _static_dir()
if not STATIC_DIR.exists():
    # 防御性日志：打包后白屏时方便定位
    print(f"[novelai] WARNING: STATIC_DIR does not exist: {STATIC_DIR}", file=sys.stderr)

app = FastAPI(title="NovelAI Writer - 实时进度面板", version="0.1.0")
app.include_router(api.router)

# B-31: import-content 限 50MB, 防 100MB 文本 OOM
_MAX_BODY_BYTES = 50 * 1024 * 1024

@app.middleware("http")
async def _limit_body_size(request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > _MAX_BODY_BYTES:
        from fastapi.responses import JSONResponse
        return JSONResponse({"error": f"请求体超过 {_MAX_BODY_BYTES // 1024 // 1024}MB 限制"}, status_code=413)
    return await call_next(request)

# 静态文件
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")


@app.get("/")
def index() -> FileResponse:
    return FileResponse(str(STATIC_DIR / "index.html"))


def run(port: int = 8765) -> None:
    """CLI 集成入口。port 可由 CLI `web <port>` 指定。"""
    import uvicorn
    uvicorn.run(
        "novelai.web.app:app",
        host="127.0.0.1",
        port=port,
        reload=False,
        log_level="info",
    )


if __name__ == "__main__":
    run()
