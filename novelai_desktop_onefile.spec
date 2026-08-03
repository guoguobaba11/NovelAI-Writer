# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for NovelAI Writer Desktop —— 单文件(onefile)模式
# 用法（在 Windows 上）:
#   pyinstaller novelai_desktop_onefile.spec --clean
# 产出：dist/NovelAI Writer.exe（单文件，约 50-70MB，启动时解压到临时目录）

import os
import sys
from pathlib import Path
from PyInstaller.utils.hooks import collect_submodules

block_cipher = None

# 项目根
ROOT = Path(SPECPATH).resolve()
ICON_PATH = ROOT / "assets" / "icon.ico"

# websockets/wsproto 是动态引用的子包，PyInstaller 静态分析抓不全
# 用 collect_submodules 一次性抓所有子模块（解决打包后 WebSocket 连不上的问题）
_ws_extra_imports = collect_submodules('websockets') + collect_submodules('wsproto')

a = Analysis(
    ['desktop.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 把 web/static 一并打包（onefile 模式下解压到 _MEIPASS/novelai/web/static/）
        ('novelai/web/static', 'novelai/web/static'),
        # 把 .env.example 打包（用户首次启动时改名为 .env）
        ('.env.example', '.'),
    ],
    hiddenimports=[
        # uvicorn 子模块（PyInstaller 默认抓不全）
        'uvicorn.logging',
        'uvicorn.loops',
        'uvicorn.loops.auto',
        'uvicorn.protocols',
        'uvicorn.protocols.http',
        'uvicorn.protocols.http.auto',
        'uvicorn.protocols.websockets',
        'uvicorn.protocols.websockets.auto',
        'uvicorn.protocols.websockets.websockets_impl',
        'uvicorn.protocols.websockets.wsproto_impl',
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
        # WebSocket 底层库（uvicorn 的 WebSocket 依赖）
        'websockets',
        'websockets.frames',
        'websockets.protocol',
        'websockets.server',
        'websockets.connection',
        'websockets.http',
        'websockets.http11',
        'websockets.streams',
        'websockets.uri',
        'websockets.utils',
        'websockets.auth',
        'websockets.client',
        'websockets.exceptions',
        'websockets.datastructures',
        'websockets.headers',
        'websockets.imports',
        'websockets.legacy',
        'websockets.legacy.handshake',
        'websockets.legacy.server',
        'websockets.legacy.client',
        'websockets.legacy.auth',
        'websockets.legacy.framing',
        'websockets.legacy.protocol',
        'websockets.extensions',
        'websockets.extensions.base',
        'websockets.extensions.permessage_deflate',
        'wsproto',
        'wsproto.connection',
        'wsproto.events',
        'wsproto.extensions',
        'wsproto.handshake',
        'wsproto.utilities',
        # novelai 全部子模块（动态 import 不会被静态分析抓到）
        'novelai',
        'novelai.config',
        'novelai.db',
        'novelai.importer',
        'novelai.personality',
        'novelai.structure',
        'novelai.scanner',
        'novelai.scanner.threads',
        'novelai.scanner.logic',
        'novelai.scanner.style',
        'novelai.optimizer',
        'novelai.pipeline',
        'novelai.writer',
        'novelai.consistency',
        'novelai.ai_client',
        'novelai.prompts',
        'novelai.retriever',
        'novelai.knowledge',
        'novelai.cli',
        # web 子包（api 在 app 里被 from . import api 引用）
        'novelai.web',
        'novelai.web.app',
        'novelai.web.api',
        'novelai.version_patch',   # 章节版本树（增量 patch）
        'novelai.docx_writer',     # docx 导出
        'novelai.embeddings',      # embedding 语义检索（纯 Python cosine）
        'novelai.tools',           # AI 工具调用（function calling）
        'novelai.errors',          # 统一错误格式化
        # 第三方
        'openai',
        'httpx',
        # .docx 导出用纯 stdlib 实现，不需要 python-docx
    ] + _ws_extra_imports,  # websockets/wsproto 全部子模块（解决打包后 WebSocket 连不上）
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # ===== 标准库：项目用不上 =====
        'tkinter', 'tkinter.*', 'unittest', 'unittest.*',
        'pydoc', 'pydoc_data', 'doctest', 'test', 'tests',
        '_pytest', 'pytest', 'py',

        # ===== 科学计算 / 数据分析：项目用不上 =====
        'numpy', 'numpy.*',
        'scipy', 'scipy.*',
        'pandas', 'pandas.*',
        'matplotlib', 'matplotlib.*',
        'PIL', 'PIL.*', 'Pillow',
        'lxml', 'lxml.*',
        'openpyxl', 'openpyxl.*',
        'pyarrow',
        'xlrd', 'xlwt', 'xlsxwriter',

        # ===== IDE / 调试工具 =====
        'IPython', 'IPython.*',
        'jedi', 'jedi.*',
        'parso', 'parso.*',
        'pygments', 'pygments.*',
        'wcwidth',
        'astroid', 'astroid.*',
        'pylint', 'pylint.*',
        'debugpy', 'pydevd', 'pydevd.*',

        # ===== ORM / 数据库（项目用纯 sqlite） =====
        'sqlalchemy', 'sqlalchemy.*',
        'alembic',
        'MySQLdb', 'pymysql', 'psycopg2', 'pymongo',

        # ===== 不用的 GUI / 系统库 =====
        'gi', 'gi.*', 'gtk', 'gtk.*',
        'wx', 'wx.*',
        'psutil',

        # ===== pandas 链上的辅助库 =====
        'dateutil', 'python_dateutil',
        'pytz', 'tzdata',
        'orjson',

        # ===== 其他 =====
        'zmq', 'pyzmq', 'charset_normalizer',
        'pluggy',
        # 注意：不能 exclude 'platformdirs'，pkg_resources 在 runtime hook 里要它。
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

# 单文件模式：EXE 内联所有 binaries/datas（exclude_binaries=True 会生成需要外部 _internal 的 exe）
exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,               # 关键：单文件要把 datas 和 binaries 内联进 EXE
    [],
    name='NovelAI Writer',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,           # 不弹控制台窗口（错误写 logfile）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=str(ICON_PATH) if ICON_PATH.exists() else None,
)
