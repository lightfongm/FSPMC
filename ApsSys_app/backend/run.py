# -*- coding: utf-8 -*-
"""
APS排程系统 — 启动入口
"""

import os

from . import create_app
from .config import DB_CONFIG, DEFAULT_PORT
from .helpers import get_config

app = create_app()


def _debug_enabled():
    """DEBUG 开关（配合 P2-15 修复）。

    🔴【P2-18 修复 · 2026-10-14】默认值由 **开** 改为 **关**（`'1'` → `'0'`）。
    原默认开意味着：任何直接 `python -m backend.run` / 双击启动器启动的实例都带
      · 异常响应返回 traceback（泄露服务器绝对路径、文件名、行号、局部变量名）；
      · Werkzeug 交互式调试器启用（带 PIN，被绕过即可在服务器执行任意代码）。
    这是"默认不安全"。现改为默认关；开发调试时显式设 `APS_DEBUG=1` 即可恢复。
    关闭后：① 异常响应仅回摘要（见 errors.err_payload）；② 调试器不启用。
    """
    return os.environ.get('APS_DEBUG', '0').strip().lower() not in ('0', 'false', 'no', 'off')


def _bind_host(debug):
    """监听地址（安全加固）：默认只监听本机 127.0.0.1。

    原实现固定 `host='0.0.0.0'`（监听所有网卡）。DEBUG 开启时 Werkzeug 交互式
    调试器虽带 PIN，一旦被绕过即可在服务器上执行任意代码 —— 对外暴露属高危。
    故规则为：
      - DEBUG 开启 → **强制** 127.0.0.1，忽略 APS_HOST（开发便利保留，风险不外泄）；
      - DEBUG 关闭 → 可用环境变量 APS_HOST 指定（如局域网访问设 APS_HOST=0.0.0.0）。
    """
    if debug:
        return '127.0.0.1'
    return os.environ.get('APS_HOST', '127.0.0.1').strip() or '127.0.0.1'


def main():
    port = int(get_config('flask_port', str(DEFAULT_PORT)))
    debug = _debug_enabled()
    host = _bind_host(debug)
    print(f"""
╔══════════════════════════════════════════════╗
║       APS排程系统 — 启动中                   ║
║       数据库: MySQL ({DB_CONFIG['host']}:{DB_CONFIG['port']})
║       数据库名: {DB_CONFIG['database']}
║       监听地址: {host}:{port}
║       DEBUG : {'开（异常响应含 traceback）' if debug else '关（异常仅返回摘要）'}
║               {'DEBUG 开时强制仅监听本机；局域网访问请设 APS_DEBUG=0 且 APS_HOST=0.0.0.0' if debug else ''}
╚══════════════════════════════════════════════╝
    """)
    app.run(host=host, port=port, debug=debug)


if __name__ == '__main__':
    main()
