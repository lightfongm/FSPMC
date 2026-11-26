# -*- coding: utf-8 -*-
"""SAP计划订单 & 销售订单"""

import re

from flask import Blueprint, request, jsonify, send_file

from ..database import query_db, execute_db, transaction
from ..utils import gen_id, serialize_row, serialize_rows
from ..errors import err_payload

bp = Blueprint('sap_orders', __name__)


@bp.route('/api/sap/planned-orders')
def sap_planned_orders_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(plnum LIKE %s OR matnr LIKE %s OR maktx LIKE %s)")
        args.extend([f'%{search}%'] * 3)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM sap_planned_orders WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM sap_planned_orders WHERE {where} ORDER BY updated_at DESC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )
    return jsonify({'success': True, 'total': total, 'page': page, 'per_page': per_page, 'data': serialize_rows(rows)})


@bp.route('/api/sap/sales-orders')
def sap_sales_orders_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(vbeln LIKE %s OR kunnr LIKE %s OR name1 LIKE %s OR bstnk LIKE %s)")
        args.extend([f'%{search}%'] * 4)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM sap_sales_orders WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM sap_sales_orders WHERE {where} ORDER BY COALESCE(erdat,'99999999'), vdatu LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )
    return jsonify({'success': True, 'total': total, 'page': page, 'per_page': per_page, 'data': serialize_rows(rows)})


@bp.route('/api/sap/sales-orders/<vbeln>/items')
def sap_sales_order_items(vbeln):
    rows = query_db("SELECT * FROM sap_sales_order_items WHERE vbeln=%s ORDER BY COALESCE(vdatu,'99999999'), posnr", (vbeln,))
    return jsonify({'success': True, 'data': serialize_rows(rows)})


# --- 销售订单行项目 CRUD ---

SO_ITEM_FIELDS = ['vbeln', 'posnr', 'matnr', 'arktx', 'kwmeng', 'vrkme',
                  'lfimg', 'nokwmeng', 'netpr', 'kpein', 'kmein', 'werks', 'lgort', 'vdatu',
                  'note', 'gbsta', 'lfsta', 'pstyv']


def _so_item_float(v):
    """将请求中的数值安全转为 float，空/None/非法返回 None。"""
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


@bp.route('/api/sap/sales-orders/<vbeln>/items', methods=['POST'])
def sap_sales_order_items_create(vbeln):
    data = request.get_json() or {}
    # 自动计算未发数量 = 订单数量 - 已发货（填写已发货时）
    _kw = _so_item_float(data.get('kwmeng'))
    _lf = _so_item_float(data.get('lfimg'))
    if _lf is not None:
        data['nokwmeng'] = round(max(0.0, (_kw or 0) - _lf), 3)
    iid = gen_id()
    fields = ['id'] + SO_ITEM_FIELDS
    values = [iid, vbeln] + [data.get(f) for f in SO_ITEM_FIELDS[1:]]  # skip vbeln
    cols = ', '.join(fields)
    ph = ', '.join(['%s'] * len(fields))
    execute_db(f"INSERT INTO sap_sales_order_items ({cols}) VALUES ({ph})", values)
    # 更新主表行项目数
    cnt = query_db("SELECT COUNT(*) as cnt FROM sap_sales_order_items WHERE vbeln=%s", (vbeln,), one=True)['cnt']
    execute_db("UPDATE sap_sales_orders SET item_count=%s WHERE vbeln=%s", (cnt, vbeln))
    return jsonify({'success': True, 'id': iid, 'message': '创建成功'})


@bp.route('/api/sap/sales-orders/items/<iid>', methods=['GET'])
def sap_sales_order_items_get(iid):
    row = query_db("SELECT * FROM sap_sales_order_items WHERE id=%s", (iid,), one=True)
    if not row:
        return jsonify({'success': False, 'message': '行项目不存在'}), 404
    return jsonify({'success': True, 'data': serialize_row(row)})


@bp.route('/api/sap/sales-orders/items/<iid>', methods=['PUT'])
def sap_sales_order_items_update(iid):
    data = request.get_json() or {}
    fields = ['matnr', 'arktx', 'kwmeng', 'vrkme', 'lfimg', 'nokwmeng', 'vdatu',
              'werks', 'lgort', 'gbsta', 'lfsta']
    # 自动计算未发数量 = 订单数量 - 已发货（填写已发货时，且与订单数量同时提供）
    _kw = _so_item_float(data.get('kwmeng'))
    _lf = _so_item_float(data.get('lfimg'))
    if _lf is not None and _kw is not None:
        data['nokwmeng'] = round(max(0.0, _kw - _lf), 3)
    sets = ', '.join([f"{f}=%s" for f in fields])
    values = [data.get(f) for f in fields] + [iid]
    execute_db(f"UPDATE sap_sales_order_items SET {sets} WHERE id=%s", values)
    return jsonify({'success': True, 'message': '更新成功'})


@bp.route('/api/sap/sales-orders/items/<iid>', methods=['DELETE'])
def sap_sales_order_items_delete(iid):
    row = query_db("SELECT vbeln FROM sap_sales_order_items WHERE id=%s", (iid,), one=True)
    if not row:
        return jsonify({'success': False, 'message': '行项目不存在'}), 404
    vbeln = row['vbeln']
    execute_db("DELETE FROM sap_sales_order_items WHERE id=%s", (iid,))
    # 更新主表行项目数
    cnt = query_db("SELECT COUNT(*) as cnt FROM sap_sales_order_items WHERE vbeln=%s", (vbeln,), one=True)['cnt']
    execute_db("UPDATE sap_sales_orders SET item_count=%s WHERE vbeln=%s", (cnt, vbeln))
    return jsonify({'success': True, 'message': '删除成功'})


@bp.route('/api/sap/sales-orders/auart-list')
def sap_auart_list():
    rows = query_db("SELECT DISTINCT auart FROM sap_sales_orders WHERE auart IS NOT NULL ORDER BY auart")
    return jsonify({'success': True, 'data': [r['auart'] for r in rows]})


# --- 销售订单 CRUD（手动录入） ---

SO_FIELDS = ['vbeln', 'kunnr', 'name1', 'auart', 'erdat', 'vdatu',
             'bstnk', 'status', 'note', 'waerk', 'data_source']

# 销售订单导入列映射（扁平单表：一行=一个行项目，表头字段每行重复 + 行项目字段）
# 主表字段（sap_sales_orders）
_SO_HDR_COLMAP = {
    '销售订单号': 'vbeln', '客户号': 'kunnr', '客户名称': 'name1', '订单类型': 'auart',
    '创建日期': 'erdat', '订单交货日期': 'vdatu', '客户PO号': 'bstnk', '状态': 'status',
    '币别': 'waerk', '数据来源': 'data_source', '备注': 'note',
}
# 子表字段（sap_sales_order_items）
_SO_ITEM_COLMAP = {
    '行号': 'posnr', '物料编码': 'matnr', '物料描述': 'arktx', '订单数量': 'kwmeng',
    '单位': 'vrkme', '已发货': 'lfimg', '行交货日期': 'vdatu', '工厂': 'werks',
    '仓库': 'lgort', '行状态': 'gbsta', '交货状态': 'lfsta',
}
# 表头（导出模版 / 导入识别）使用的列顺序
_SO_FLAT_HEADERS = (list(_SO_HDR_COLMAP.keys()) + list(_SO_ITEM_COLMAP.keys()))

VALID_SO_STATUS = {'启用', '关闭', '作废'}


def _norm_so_date(v):
    """xlsx 单元格（datetime / 字符串 / 空）规范为 YYYYMMDD 字符串或 ''（用于导入）。"""
    if v is None or v == '':
        return ''
    if hasattr(v, 'strftime'):
        return v.strftime('%Y%m%d')
    return re.sub(r'\D', '', str(v))


@bp.route('/api/sap/sales-orders', methods=['POST'])
def sap_sales_orders_create():
    data = request.get_json() or {}
    sid = gen_id()
    fields = ['id'] + SO_FIELDS
    values = [sid] + [data.get(f) for f in SO_FIELDS]
    cols = ', '.join(fields)
    ph = ', '.join(['%s'] * len(fields))
    execute_db(f"INSERT INTO sap_sales_orders ({cols}) VALUES ({ph})", values)
    return jsonify({'success': True, 'id': sid, 'message': '创建成功'})


@bp.route('/api/sap/sales-orders/<sid>', methods=['GET'])
def sap_sales_orders_get(sid):
    row = query_db("SELECT * FROM sap_sales_orders WHERE id=%s", (sid,), one=True)
    if not row:
        return jsonify({'success': False, 'message': '销售订单不存在'}), 404
    return jsonify({'success': True, 'data': serialize_row(row)})


@bp.route('/api/sap/sales-orders/<sid>', methods=['PUT'])
def sap_sales_orders_update(sid):
    data = request.get_json() or {}
    sets = ', '.join([f"{f}=%s" for f in SO_FIELDS])
    values = [data.get(f) for f in SO_FIELDS] + [sid]
    execute_db(f"UPDATE sap_sales_orders SET {sets}, updated_at=NOW() WHERE id=%s", values)
    return jsonify({'success': True, 'message': '更新成功'})


@bp.route('/api/sap/sales-orders/<sid>', methods=['DELETE'])
def sap_sales_orders_delete(sid):
    """删除销售订单。

    🔴【P2-24 修复 · 2026-10-14】原实现**只删主表、不级联删行项目**
    （`sap_sales_order_items`）。而毛需求查询是 `si LEFT JOIN so`：
    行项目靠 `si.vbeln` 关联，主表记录删掉后 LEFT JOIN 仍能取到行项目
    → **已删除的订单仍继续驱动排产与缺料**（幽灵需求），且界面上查不到来源，极难排查。
    现：先按 vbeln 删行项目、再删主表，两步包在一个事务里；
    同时按 id 精确删除行项目（若有 order_id 外键列则一并处理）。
    """
    try:
        with transaction():
            order = query_db("SELECT vbeln FROM sap_sales_orders WHERE id=%s", (sid,), one=True)
            vbeln = (order or {}).get('vbeln')
            if vbeln:
                execute_db("DELETE FROM sap_sales_order_items WHERE vbeln=%s", (vbeln,))
            # 兼容：部分历史数据可能用 order_id 关联主表主键
            try:
                execute_db("DELETE FROM sap_sales_order_items WHERE order_id=%s", (sid,))
            except Exception:
                pass
            execute_db("DELETE FROM sap_sales_orders WHERE id=%s", (sid,))
    except Exception as e:
        return jsonify(err_payload(e)), 500
    return jsonify({'success': True, 'message': '删除成功（含行项目）'})


# --- 旧版销售订单接口 ---

@bp.route('/api/sales-orders/list')
def sales_orders_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    offset = (page - 1) * per_page
    total = query_db("SELECT COUNT(*) as cnt FROM sap_sales_orders", one=True)['cnt']
    rows = query_db("SELECT * FROM sap_sales_orders ORDER BY COALESCE(erdat,'99999999'), vdatu LIMIT %s OFFSET %s", (per_page, offset))
    return jsonify({'success': True, 'total': total, 'data': serialize_rows(rows)})


@bp.route('/api/sales-orders')
def sales_orders():
    return sales_orders_list()


@bp.route('/api/sales-orders/load-from-db')
def sales_orders_load_from_db():
    rows = query_db("SELECT * FROM sap_sales_orders ORDER BY COALESCE(erdat,'99999999'), vdatu LIMIT 100")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@bp.route('/api/sales-orders/next-serial')
def sales_orders_next_serial():
    row = query_db("SELECT MAX(vbeln) as max_no FROM sap_sales_orders WHERE vbeln LIKE 'XS%'", one=True)
    max_no = row.get('max_no') if row else None
    if max_no:
        try: num = int(max_no[2:]) + 1
        except: num = 1
    else:
        num = 1
    return jsonify({'success': True, 'order_no': f'XS{num:03d}'})


# --- 销售订单导入 / 导出模版（扁平单表：一行=一个行项目） ---

@bp.route('/api/sap/sales-orders/export-template')
def so_export_template():
    """导出销售订单导入模板（扁平单表：表头字段 + 行项目字段，一行=一个行项目）。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '销售订单导入模板'
    ws.append(list(_SO_FLAT_HEADERS))
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')

    # 示例：同一销售订单 SO2026-001 的两行行项目
    ws.append(['SO2026-001', 'C001', '示例客户', 'ZOR', '20260813', '20260820',
               'PO123', '启用', 'CNY', '手动', '示例备注',
               '000010', 'M001', '示例物料A', 100, 'KG', 0, '20260820', '1000', '2000', '', ''])
    ws.append(['SO2026-001', 'C001', '示例客户', 'ZOR', '20260813', '20260820',
               'PO123', '启用', 'CNY', '手动', '示例备注',
               '000020', 'M002', '示例物料B', 200, 'KG', 50, '20260822', '1000', '2000', '', ''])
    ws.append(['（销售订单号+行号 重复的行项目自动跳过；日期格式 YYYYMMDD；'
               '同一订单的多行只需重复填写表头字段即可）',
               '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '', ''])

    widths = [14, 10, 14, 8, 12, 12, 10, 8, 8, 10, 14, 10, 12, 14, 10, 6, 10, 8, 8, 8, 8]
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='销售订单导入模板.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


def _so_next_posnr(vbeln):
    """取某销售订单当前最大行号 +10（4 位补零），用于自动生成行号。"""
    rows = query_db("SELECT posnr FROM sap_sales_order_items WHERE vbeln=%s", (vbeln,))
    max_n = 0
    for r in rows:
        code = (r.get('posnr') or '').strip()
        try:
            n = int(code)
            if n > max_n:
                max_n = n
        except (TypeError, ValueError):
            pass
    return '%06d' % (max_n + 10)


@bp.route('/api/sap/sales-orders/import', methods=['POST'])
def so_import():
    """批量导入销售订单（扁平单表 xlsx，一行=一个行项目）。

    表头字段（销售订单号/客户号/…）每行重复；行项目字段（物料编码/订单数量/已发货…）为实际需求。
    必需列：销售订单号、物料编码、订单数量。
    去重：相同"销售订单号+行号"的行项目跳过（不覆盖）；销售订单主表不存在时按表头字段自动创建。
    """
    import openpyxl
    from io import BytesIO

    # 【P2-22 修复】走共享的安全读取：扩展名白名单 + 大小上限 + read_only 流式解析，
    # 避免恶意/损坏 xlsx（zip bomb）吃满内存 CPU 拖垮单进程服务。
    from ..utils import read_upload_excel
    f = request.files.get('file')
    wb, err = read_upload_excel(f)
    if err:
        return jsonify({'success': False, 'error': err}), 400

    ws = wb.active
    raw_rows = list(ws.iter_rows(values_only=True))
    if not raw_rows:
        return jsonify({'success': False, 'error': '文件为空'}), 400

    header = [str(h).strip() if h is not None else '' for h in raw_rows[0]]
    idx = {cn: i for i, cn in enumerate(header) if cn in _SO_HDR_COLMAP or cn in _SO_ITEM_COLMAP}
    for req_col in ('销售订单号', '物料编码', '订单数量'):
        if req_col not in idx:
            return jsonify({'success': False, 'error': '表头缺少必需列：' + req_col}), 400

    exist_orders = {(r.get('vbeln') or '').strip() for r in query_db("SELECT vbeln FROM sap_sales_orders")}
    # 已存在的"销售订单号+行号"组合（用于跳过重复行项目）
    exist_item_keys = {
        ((r.get('vbeln') or '').strip(), (r.get('posnr') or '').strip())
        for r in query_db("SELECT vbeln, posnr FROM sap_sales_order_items")
    }

    def get_val(row, cn):
        i = idx.get(cn)
        return row[i] if (i is not None and i < len(row)) else None

    def to_num(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    created_orders = 0
    created_items = 0
    skipped = 0
    errors = []
    # 本次批次内已自动创建的主表，避免同一文件里同一订单重复建主表
    batch_orders = set()

    for ri, raw in enumerate(raw_rows[1:], start=2):
        vbeln = str(get_val(raw, '销售订单号') or '').strip()
        matnr = str(get_val(raw, '物料编码') or '').strip()
        kwmeng = to_num(get_val(raw, '订单数量'))
        if not vbeln or not matnr or kwmeng is None:
            errors.append({'row': ri, 'reason': '必填缺失（销售订单号/物料编码/订单数量）'})
            continue

        # 主表：DB 已有 或 本批次已创建 → 跳过建主表；否则按表头字段创建
        if vbeln not in exist_orders and vbeln not in batch_orders:
            kunnr = str(get_val(raw, '客户号') or '').strip()
            name1 = str(get_val(raw, '客户名称') or '').strip()
            auart = str(get_val(raw, '订单类型') or '').strip()
            erdat = _norm_so_date(get_val(raw, '创建日期'))
            h_vdatu = _norm_so_date(get_val(raw, '订单交货日期'))
            bstnk = str(get_val(raw, '客户PO号') or '').strip()
            status = str(get_val(raw, '状态') or '').strip() or '启用'
            if status not in VALID_SO_STATUS:
                errors.append({'row': ri, 'reason': '状态非法：%s（应为启用/关闭/作废）' % status})
                continue
            waerk = str(get_val(raw, '币别') or '').strip()
            data_source = str(get_val(raw, '数据来源') or '').strip() or '手动'
            note = str(get_val(raw, '备注') or '').strip()
            sid = gen_id()
            execute_db(
                "INSERT INTO sap_sales_orders "
                "(id, vbeln, kunnr, name1, auart, erdat, vdatu, bstnk, status, note, waerk, data_source, item_count) "
                "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, 0)",
                (sid, vbeln, kunnr, name1, auart, erdat, h_vdatu, bstnk, status, note, waerk, data_source)
            )
            batch_orders.add(vbeln)
            created_orders += 1

        # 行号：用户提供则用之，否则自动生成
        posnr = str(get_val(raw, '行号') or '').strip()
        if not posnr:
            posnr = _so_next_posnr(vbeln)
        key = (vbeln, posnr)
        if key in exist_item_keys:
            skipped += 1
            errors.append({'row': ri, 'reason': '销售订单号 %s + 行号 %s 已存在，跳过' % key})
            continue

        arktx = str(get_val(raw, '物料描述') or '').strip()
        vrkme = str(get_val(raw, '单位') or '').strip()
        lfimg = to_num(get_val(raw, '已发货')) or 0
        i_vdatu = _norm_so_date(get_val(raw, '行交货日期'))
        werks = str(get_val(raw, '工厂') or '').strip()
        lgort = str(get_val(raw, '仓库') or '').strip()
        gbsta = str(get_val(raw, '行状态') or '').strip()
        lfsta = str(get_val(raw, '交货状态') or '').strip()
        nokwmeng = round(max(0.0, kwmeng - lfimg), 3)

        iid = gen_id()
        execute_db(
            "INSERT INTO sap_sales_order_items "
            "(id, vbeln, posnr, matnr, arktx, kwmeng, vrkme, lfimg, nokwmeng, vdatu, werks, lgort, gbsta, lfsta) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (iid, vbeln, posnr, matnr, arktx, kwmeng, vrkme, lfimg, nokwmeng, i_vdatu, werks, lgort, gbsta, lfsta)
        )
        exist_item_keys.add(key)
        # 更新主表行项目数
        cnt = query_db("SELECT COUNT(*) as cnt FROM sap_sales_order_items WHERE vbeln=%s", (vbeln,), one=True)['cnt']
        execute_db("UPDATE sap_sales_orders SET item_count=%s WHERE vbeln=%s", (cnt, vbeln))
        created_items += 1

    msg = '导入完成：新增订单 %d 个，新增行项目 %d 条，跳过 %d 条（重复），错误 %d 条。' % (
        created_orders, created_items, skipped, len(errors))
    return jsonify({'success': True, 'message': msg, 'created_orders': created_orders,
                    'created_items': created_items, 'skipped': skipped, 'errors': errors})
