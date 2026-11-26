# -*- coding: utf-8 -*-
"""【M1】序列相关换型矩阵（SDST）维护

换型时长不再是"物料自身的固定值"，而是 changeover(前一单物料, 后一单物料)：
  · 同物料连产 = 0（连续生产无需清洗，这是最直接的产能释放）
  · 有矩阵记录 → 用矩阵值（食品行业典型：浅色→深色 与 深色→浅色 不对称，只录单向即可）
  · 无矩阵记录 → 回退该物料自身 changeover_time（与未启用矩阵时完全一致，不劣化）

表 product_changeover 由 work_orders._migrate() 幂等建表；本模块只做 CRUD。
"""

import logging

from flask import Blueprint, jsonify, request

from ..database import execute_db, query_db
from ..errors import err_payload
from ..utils import gen_id, serialize_rows

bp = Blueprint('changeover', __name__)


@bp.route('/api/changeover', methods=['GET'])
def changeover_list():
    """换型矩阵列表。search 可选，按 前物料/后物料 模糊匹配。"""
    try:
        search = (request.args.get('search') or '').strip()
        where = "1=1"
        args = []
        if search:
            where = "(from_mat LIKE %s OR to_mat LIKE %s)"
            args = ['%%%s%%' % search, '%%%s%%' % search]
        rows = query_db(
            "SELECT id, from_mat, to_mat, changeover_hours, note, updated_at "
            "FROM product_changeover WHERE %s ORDER BY from_mat ASC, to_mat ASC" % where, args)
        return jsonify({'success': True, 'data': serialize_rows(rows),
                        'total': len(rows or [])})
    except Exception as e:
        return jsonify(err_payload(e)), 500


@bp.route('/api/changeover', methods=['POST'])
def changeover_upsert():
    """新增或更新一条换型记录（同一 (from_mat, to_mat) 视为同一条）。

    小时数允许 0（同族/同色系免清洗）；为空或非数字按 0 处理。
    """
    try:
        data = request.get_json() or {}
        f = (data.get('from_mat') or '').strip()
        t = (data.get('to_mat') or '').strip()
        if not f or not t:
            return jsonify({'success': False, 'message': '换型前/后物料编码不能为空'}), 400
        if f == t:
            return jsonify({'success': False, 'message': '换型前后物料相同，系统已默认按 0 处理，无需录入'}), 400
        try:
            hours = float(data.get('changeover_hours') or 0)
        except (ValueError, TypeError):
            hours = 0.0
        if hours < 0:
            hours = 0.0
        note = data.get('note')
        exist = query_db("SELECT id FROM product_changeover WHERE from_mat=%s AND to_mat=%s",
                         (f, t), one=True)
        if exist:
            execute_db(
                "UPDATE product_changeover SET changeover_hours=%s, note=%s, updated_at=NOW() WHERE id=%s",
                (hours, note, exist['id']))
            return jsonify({'success': True, 'message': '已更新换型时长 %s → %s = %s 小时' % (f, t, hours),
                            'id': exist['id']})
        cid = gen_id()
        execute_db(
            "INSERT INTO product_changeover (id, from_mat, to_mat, changeover_hours, note) "
            "VALUES (%s, %s, %s, %s, %s)", (cid, f, t, hours, note))
        return jsonify({'success': True, 'message': '已新增换型时长 %s → %s = %s 小时' % (f, t, hours),
                        'id': cid})
    except Exception as e:
        return jsonify(err_payload(e)), 500


@bp.route('/api/changeover/<cid>', methods=['DELETE'])
def changeover_delete(cid):
    """删除一条换型记录（删除后该组合回退为物料自身 changeover_time）。"""
    try:
        row = query_db("SELECT id FROM product_changeover WHERE id=%s", (cid,), one=True)
        if not row:
            return jsonify({'success': False, 'message': '记录不存在'}), 404
        execute_db("DELETE FROM product_changeover WHERE id=%s", (cid,))
        return jsonify({'success': True, 'message': '已删除，该组合将回退为物料自身换型时间'})
    except Exception as e:
        return jsonify(err_payload(e)), 500


@bp.route('/api/changeover/materials', methods=['GET'])
def changeover_materials():
    """可选物料清单（产成品 + 半成品），供前端下拉使用。"""
    try:
        rows = query_db(
            "SELECT material_code, name FROM products WHERE type IN ('产成品','半成品') "
            "ORDER BY material_code ASC")
        return jsonify({'success': True, 'data': serialize_rows(rows)})
    except Exception as e:
        logging.getLogger(__name__).error('换型物料清单加载失败：%s', e)
        return jsonify(err_payload(e)), 500
