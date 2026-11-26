# -*- coding: utf-8 -*-
"""统一异常响应体（P1-2 提升为共享模块）。

背景：原先 `err_payload` 只定义在 `work_orders.py` 内部，其它模块无法复用，
于是各自写成 `{'success': False, 'error': str(e)}` 直接把原始异常串返回前端，
**完全不受 DEBUG 门控** —— 生产环境同样会泄露 SQL 片段、列名、内部路径等实现细节。

现提到本共享模块，全项目统一调用 `err_payload(e)`。
"""

from flask import current_app


def err_payload(e, prefix=''):
    """统一异常响应体（修复 P2-15；P1-2 提升为共享）。

    - **仅当 Flask 处于 DEBUG 模式**才附 traceback；
    - 非 DEBUG 只回传错误摘要 `str(e)`：既便于排障，又不泄露服务器绝对路径、
      文件名、行号、局部变量名等代码结构信息。
    - `prefix`：可选的中文上下文前缀（如 '文件解析失败：'）。【P2-20 修复】
      过去各模块写成 `'文件解析失败：' + str(e)`，绕开了 DEBUG 门控且把
      pymysql 原始异常串（含 SQL 片段、列名）原样回传；现统一走本函数，
      前缀照旧、异常摘要受门控。

    注意：必须在 `except` 块内调用，否则 `traceback.format_exc()` 取不到活动异常。
    """
    payload = {'success': False, 'error': (prefix or '') + str(e)}
    try:
        debug_on = bool(current_app.config.get('DEBUG', False))
    except Exception:
        # 无应用上下文（如单元测试直调）→ 按最严格口径：不带 trace
        debug_on = False
    if debug_on:
        import traceback
        payload['trace'] = traceback.format_exc()
    return payload
