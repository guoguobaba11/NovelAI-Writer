# -*- mode: python ; coding: utf-8 -*-
# PyInstaller spec for NovelAI Writer Desktop
# 用法（在 Windows 上）:
#   pip install -r requirements.txt -r requirements-desktop.txt
#   pyinstaller novelai_desktop.spec --clean
# 产出：dist/NovelAI Writer.exe（约 80-120MB）

import os
import sys
from pathlib import Path

block_cipher = None

# 项目根
ROOT = Path(SPECPATH).resolve()
ICON_PATH = ROOT / "assets" / "icon.ico"

a = Analysis(
    ['desktop.py'],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        # 把 web/static 一并打包（开发时 web/ 在根，打包后 _MEIPASS/novelai/web/static/）
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
        'uvicorn.lifespan',
        'uvicorn.lifespan.on',
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
        # 第三方
        'openai',
        'httpx',
        # .docx 导出用纯 stdlib 实现，不需要 python-docx
    ],
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

        # ===== 不能 exclude 的（setuptools vendored / PyInstaller hook alias） =====
        # 'setuptools', 'distutils', 'packaging', 'jaraco', 'more_itertools',
        # 'importlib_metadata', 'importlib_resources', 'zipp', 'wheel',
        # 'tomli', 'tomllib', 'backports', 'keyring', 'secretstorage',
        # 这些必须保留，否则 PyInstaller 钩子的 alias_module() 会冲突。
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,   # 关键：onedir 模式，binaries 放 COLLECT 里
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


coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='NovelAI Writer',
)
