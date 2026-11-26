# -*- coding: utf-8 -*-
"""条码库存管理"""

from flask import Blueprint, request, jsonify

from ..database import query_db, execute_db
from ..utils import gen_id, serialize_rows

bp = Blueprint('barcode', __name__)


@bp.route('/api/barcode-inventory')
def barcode_inventory_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(material_code LIKE %s OR material_desc LIKE %s OR sap_batch LIKE %s)")
        args.extend([f'%{search}%'] * 3)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM barcode_inventory WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM barcode_inventory WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )
    return jsonify({'success': True, 'total': total, 'page': page, 'per_page': per_page, 'data': serialize_rows(rows)})


@bp.route('/api/barcode-inventory', methods=['POST'])
def barcode_inventory_create():
    data = request.get_json() or {}
    bid = gen_id()
    data['id'] = bid
    fields = ['id', 'material_code', 'material_desc', 'quantity', 'unit',
              'warehouse_code', 'location_code', 'supplier_batch', 'sap_batch',
              'production_date', 'expiry_date', 'status', 'note']
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]
    execute_db(f"INSERT INTO barcode_inventory ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': bid, 'message': '创建成功'})


@bp.route('/api/barcode-inventory/<bid>', methods=['DELETE'])
def barcode_inventory_delete(bid):
    execute_db("DELETE FROM barcode_inventory WHERE id=%s", (bid,))
    return jsonify({'success': True, 'message': '删除成功'})
