# -*- coding: utf-8 -*-
"""健康检查与元数据"""

from flask import Blueprint, jsonify

from ..database import query_db
from ..utils import biz_now
from ..errors import err_payload

bp = Blueprint('health', __name__)


@bp.route('/api/health')
def health():
    try:
        row = query_db("SELECT VERSION() as version", one=True)
        return jsonify({
            'status': 'ok',
            'database': 'connected',
            'db_version': row['version'],
            'db_type': 'MySQL',
            'timestamp': biz_now().isoformat()
        })
    except Exception as e:
        # 【P2-20 修复】原为 `'error': str(e)`，不受 DEBUG 门控，
        # 会把 pymysql 原始异常串（含 SQL 片段、列名）原样回传前端。
        p = err_payload(e)
        p['status'] = 'error'
        p['database'] = 'disconnected'
        return jsonify(p), 500


@bp.route('/api/metadata/fields')
def metadata_fields():
    """字段元数据"""
    return jsonify({
        'products': ['id', 'material_code', 'name', 'type', 'category', 'workshop',
                      'production_time', 'production_cycle', 'batch_size', 'unit', 'priority'],
        'inventory': ['id', 'material_code', 'name', 'quantity', 'unit', 'location', 'min_stock'],
        'work_orders': ['id', 'order_no', 'material_code', 'material_desc', 'status', 'priority'],
        'customers': ['id', 'name', 'code', 'priority', 'sap_kunnr'],
        'bom': ['bom_key', 'parent_matnr', 'comp_matnr', 'comp_menge', 'comp_meins', 'postp'],
    })
