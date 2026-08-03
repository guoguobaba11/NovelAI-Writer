"""
novelai 桌面应用入口
- 后台线程跑 uvicorn
- 主线程用 PyWebView 打开原生窗口（Win11/WebView2 / macOS/WebKit / Linux/GTK+WebKit）
- 窗口关闭时优雅关闭 uvicorn
- 跨设备兼容：WebView2 缺失时回退浏览器；首次启动检查 .env 缺失时引导用户填 API key

用法：
  python desktop.py [--port 8765] [--no-gui] [--browser]
  pyinstaller novelai_desktop.spec  (构建 .exe)
"""
from __future__ import annotations
import argparse
import sys
import os
import time
import socket
import threading
import webbrowser
import logging
from pathlib import Path


# 抑制 uvicorn / httpx 噪音日志（PyInstaller 打包后会写到 stderr，弹窗）
logging.getLogger("uvicorn.access").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)


def _setup_logging() -> None:
    """打包后 console=False 看不到错误，写到 data/novelai.log。
    失败时双写到 stderr 也不影响。
    """
    try:
        from novelai.config import DATA_DIR
        log_file = DATA_DIR / "novelai.log"
        handlers = [
            logging.FileHandler(str(log_file), encoding="utf-8"),
        ]
        # 打包后 stderr 通常也是黑的，但仍写一份兜底
        if sys.stderr:
            handlers.append(logging.StreamHandler(sys.stderr))
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            handlers=handlers,
            force=True,
        )
    except Exception as e:
        # 兜底：直接 stderr
        try:
            sys.stderr.write(f"[novelai] log setup failed: {e}\n")
            sys.stderr.flush()
        except Exception:
            pass


HOST = "127.0.0.1"
DEFAULT_PORT = 8765
STARTUP_TIMEOUT = 30  # 等待服务启动的最长秒数


def find_free_port(preferred: int) -> int:
    """优先用 preferred；占用则用 0 让系统分配。"""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((HOST, preferred))
            return preferred
    except OSError:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.bind((HOST, 0))
            return s.getsockname()[1]


def wait_for_server(url: str, timeout: int) -> bool:
    """轮询 HTTP 200 来确认服务起来了。"""
    import urllib.request, urllib.error
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            with urllib.request.urlopen(url, timeout=1) as r:
                if r.status == 200:
                    return True
        except (urllib.error.URLError, ConnectionResetError, OSError):
            time.sleep(0.3)
    return False


def run_server(port: int, ready_event: threading.Event):
    """在子线程跑 uvicorn。ready_event 启动后 set。"""
    import uvicorn
    from novelai.web.app import app
    config = uvicorn.Config(
        app, host=HOST, port=port,
        log_level="warning",
        access_log=False,
        log_config=None,
    )
    server = uvicorn.Server(config)

    def _signal_ready():
        for _ in range(100):
            if wait_for_server(f"http://{HOST}:{port}/", 0.5):
                ready_event.set()
                return
            time.sleep(0.3)
    threading.Thread(target=_signal_ready, daemon=True).start()
    server.run()


def _check_webview2_available() -> bool:
    """检测 Windows 是否有 WebView2 runtime（Win10 2019+ 默认有，早期版本没有）。"""
    if sys.platform != "win32":
        return True  # macOS / Linux 上 webview 走 WebKit/GTK，自带
    try:
        # PyWebView 在 Win 上默认尝试 edgechromium，找不到时 import 仍成功但 start 失败
        # 用注册表快速查 WebView2 Runtime
        import winreg
        for sub in (
            r"SOFTWARE\WOW6432Node\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
            r"SOFTWARE\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}",
        ):
            try:
                with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, sub) as k:
                    if winreg.QueryValueEx(k, "pv")[0]:
                        return True
            except OSError:
                continue
        return False
    except Exception:
        return True  # 检测失败时乐观地继续（让 webview.start 自己报错）


def open_native_window(url: str):
    """尝试用 PyWebView 开原生窗口；WebView2 缺失或失败则回退到浏览器。"""
    if not _check_webview2_available():
        logging.warning("[novelai] WebView2 runtime 未检测到，回退到浏览器模式（推荐安装 Microsoft Edge WebView2 Runtime）")
        print("[novelai] WebView2 runtime 未安装，回退到浏览器模式")
        print("[novelai] 下载地址: https://developer.microsoft.com/microsoft-edge/webview2/")
        webbrowser.open(url)
        _wait_for_enter_or_signal()
        return

    try:
        import webview
    except ImportError:
        print("[novelai] pywebview 未安装，回退到浏览器模式")
        webbrowser.open(url)
        _wait_for_enter_or_signal()
        return

    try:
        window = webview.create_window(
            title="NovelAI Writer · 作者工作台",
            url=url,
            width=2520,
            height=1680,
            min_size=(2520, 1680),
            background_color="#0e1014",
            text_select=True,
        )
        webview.start()
    except Exception as e:
        # webview.start 内部可能因各种原因失败（WebView2 实际没装、版本太旧、权限问题）
        logging.exception("[novelai] webview 启动失败，回退浏览器")
        print(f"[novelai] 原生窗口启动失败: {e}")
        print("[novelai] 回退到浏览器模式")
        webbrowser.open(url)
        _wait_for_enter_or_signal()


def _wait_for_enter_or_signal():
    """打包后 console=False 时 input() 爆 EOFError。catch 后直接退出。"""
    try:
        input("按 Enter 退出...")
    except (EOFError, KeyboardInterrupt):
        pass


def _check_first_run_env() -> str:
    """首次启动检查：.env 缺失 → 写一份空模板 + 返回提示文案
    返回值是一个 HTML 页面 URL（如果新建了 .env）或空字符串
    """
    try:
        from novelai.config import PROJECT_ROOT
        env_path = PROJECT_ROOT / ".env"
        if env_path.exists():
            return ""
        # 写一个模板
        template = (
            "# NovelAI Writer 配置\n"
            "# 首次启动会自动生成。请填入你的 API key 后重启应用。\n"
            "\n"
            "# ===== AI 后端 =====\n"
            "# 选填，默认 deepseek（OpenAI 兼容协议）\n"
            "NOVELAI_PROVIDER=openai_compatible\n"
            "\n"
            "# ===== API Key（必填） =====\n"
            "# DeepSeek: https://platform.deepseek.com/api_keys\n"
            "# OpenAI: https://platform.openai.com/api-keys\n"
            "NOVELAI_API_KEY=\n"
            "\n"
            "# ===== API 地址 =====\n"
            "# DeepSeek: https://api.deepseek.com/v1\n"
            "# OpenAI: https://api.openai.com/v1\n"
            "NOVELAI_BASE_URL=https://api.deepseek.com/v1\n"
            "\n"
            "# ===== 模型 =====\n"
            "# DeepSeek: deepseek-chat (推荐)\n"
            "# OpenAI: gpt-4o-mini / gpt-4o\n"
            "NOVELAI_MODEL=deepseek-chat\n"
        )
        env_path.write_text(template, encoding="utf-8")
        logging.info(f"[novelai] 首次启动：已生成 .env 模板在 {env_path}")
        return str(env_path)
    except Exception as e:
        logging.exception(f"[novelai] 检查 .env 失败: {e}")
        return ""


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="NovelAI Writer",
        description="长篇小说 AI 编辑器 — 桌面版",
    )
    p.add_argument("--port", type=int, default=DEFAULT_PORT, help="后端服务端口（默认 8765）")
    p.add_argument("--no-gui", action="store_true", help="不创建原生窗口（仅起后端 + 打开浏览器）")
    p.add_argument("--browser", action="store_true", help="强制使用浏览器模式")
    return p.parse_args()


def main():
    args = parse_args()
    port = find_free_port(args.port)
    url = f"http://{HOST}:{port}/"

    # 首次启动：检查并生成 .env 模板（跨设备友好）
    env_hint = _check_first_run_env()
    if env_hint:
        print(f"[novelai] 首次启动：已在 {env_hint} 生成 .env 模板")
        print("[novelai] 请填入 NOVELAI_API_KEY 后重启应用")

    # 启动后端
    print(f"[novelai] 启动后端服务 {url} ...")
    ready = threading.Event()
    t = threading.Thread(target=run_server, args=(port, ready), daemon=True)
    t.start()
    if not ready.wait(timeout=STARTUP_TIMEOUT):
        print(f"[novelai] 错误：服务在 {STARTUP_TIMEOUT}s 内未启动")
        _wait_for_enter_or_signal()
        sys.exit(1)
    print(f"[novelai] 后端就绪 {url}")

    if args.no_gui or args.browser:
        webbrowser.open(url)
        print("[novelai] 浏览器模式：Ctrl+C 退出")
        try:
            t.join()
        except KeyboardInterrupt:
            sys.exit(0)
    else:
        open_native_window(url)


if __name__ == "__main__":
    _setup_logging()
    try:
        main()
    except SystemExit:
        raise
    except Exception as e:
        logging.exception("[novelai] fatal error in main()")
        try:
            sys.stderr.write(f"Fatal: {e}\n")
        except Exception:
            pass
        _wait_for_enter_or_signal()
        sys.exit(1)
