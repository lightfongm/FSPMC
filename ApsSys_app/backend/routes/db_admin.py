# -*- coding: utf-8 -*-
"""数据库管理"""

from flask import Blueprint, request, jsonify

from ..database import query_db
from ..config import DB_CONFIG
from ..utils import serialize_rows
from ..errors import err_payload

bp = Blueprint('db_admin', __name__)


@bp.route('/api/database/test')
def database_test():
    try:
        row = query_db("SELECT VERSION() as version, DATABASE() as db", one=True)
        return jsonify({
            'success': True,
            'message': '数据库连接成功',
            'version': row['version'],
            'database': row['db'],
            'host': DB_CONFIG['host'],
            'port': DB_CONFIG['port'],
            'type': 'MySQL'
        })
    except Exception as e:
        return jsonify(err_payload(e)), 500


@bp.route('/api/database/status')
def database_status():
    try:
        tables = query_db("SHOW TABLES")
        table_list = [list(t.values())[0] for t in tables]
        return jsonify({
            'success': True,
            'connected': True,
            'database': DB_CONFIG['database'],
            'host': DB_CONFIG['host'],
            'port': DB_CONFIG['port'],
            'type': 'MySQL',
            'tables': table_list,
            'table_count': len(table_list)
        })
    except Exception as e:
        p = err_payload(e)
        p['connected'] = False
        return jsonify(p), 500


@bp.route('/api/database/query', methods=['POST'])
def database_query():
    """【已下线·P0-2 修复 2026-10-14】任意 SQL 执行入口，**已删除实现**。

    原实现把请求体的 `sql` 原样交给 `query_db()` 执行，仅做 `SELECT/SHOW/DESC`
    前缀白名单，而本项目**全站无任何认证机制**，等价于：
      - 任何能访问 5000 端口的人可读取全部业务表；
      - `SELECT ... INTO OUTFILE/DUMPFILE` 可直接落盘写文件；
      - 前缀白名单挡不住子查询与函数探测（`SELECT (SELECT ...)`, `SELECT LOAD_FILE(...)`）。

    前端无调用（见复审报告 §五 孤儿接口清单），属纯风险面，故按方案 A 下线。
    保留路由返回 410，避免老客户端拿到 404 后反复重试；如需查库请直接用
    MySQL 客户端或另行实现"按 key 取预定义 SQL 白名单"的受控接口。
    """
    return jsonify({
        'success': False,
        'message': '该接口已下线（安全原因）：不再支持执行任意 SQL。如需排查请直接使用数据库客户端。'
    }), 410
