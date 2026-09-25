# -*- mode: python ; coding: utf-8 -*-
# omni-agent Windows 打包配置
# 用法(Windows 环境): pyinstaller haisnap_win.spec
# 产物: dist/omni-agent.exe (单文件, 含网页版前端资源)

block_cipher = None

a = Analysis(
    ['launcher.py'],
    pathex=[],
    binaries=[],
    datas=[
        ('omni_agent/web/index.html', 'omni_agent/web'),  # 网页版前端资源
    ],
    hiddenimports=[
        'omni_agent',
        'omni_agent.agent', 'omni_agent.cli', 'omni_agent.webserver',
        'omni_agent.envstore', 'omni_agent.logger', 'omni_agent.unifuncs',
        'omni_agent.llm', 'omni_agent.permission', 'omni_agent.tool_runner',
        'omni_agent.tools_schema', 'omni_agent.tool_policy',
        'omni_agent.hooks', 'omni_agent.mcp', 'omni_agent.connectors',
        'omni_agent.checkpoint', 'omni_agent.session',
        'omni_agent.skills', 'omni_agent.sources', 'omni_agent.lessons',
        'omni_agent.similarity', 'omni_agent.settings', 'omni_agent.scheduler',
        'omni_agent.browser', 'omni_agent.prompts', 'omni_agent.ui',
        'omni_agent.config',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['tkinter', 'matplotlib', 'numpy', 'pandas', 'PIL'],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name='omni-agent',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,          # 控制台窗口: CLI 模式交互 + WEB 模式日志展示
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=None,
)
