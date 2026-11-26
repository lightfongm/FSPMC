# -*- coding: utf-8 -*-
"""库存管理"""

from flask import Blueprint, request, jsonify, send_file

from ..database import query_db, execute_db, safe_fields
from ..utils import gen_id, serialize_row, serialize_rows
from ..helpers import get_transit_by_material
from ..errors import err_payload

bp = Blueprint('inventory', __name__)


def _first_inventory_id_per_material():
    """同一 material_code 的第一行（id 最小）的 id，用于"SAP在途"仅显示在该物料的第一行。"""
    rows = query_db("SELECT material_code, MIN(id) AS first_id FROM inventory GROUP BY material_code")
    return {r['material_code']: r['first_id'] for r in rows}


@bp.route('/api/inventory')
def inventory_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        # 转换通配符：* → %, ? → _
        s = search.replace('*', '%').replace('?', '_')
        # 如果用户自己用了通配符，就不额外加 %；否则自动模糊匹配
        if '*' not in search and '?' not in search:
            s = f'%{s}%'
        conditions.append("(material_code LIKE %s OR name LIKE %s OR sap_matnr LIKE %s)")
        args.extend([s] * 3)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM inventory WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM inventory WHERE {where} ORDER BY material_code ASC, id ASC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )

    # SAP在途（采购在途）= 采购入库中状态为"在途"的该物料"计划到货数量"之和；
    # 仅显示在每一物料的第一行，其余行 SAP在途 记为 0（避免在途数量被按行重复累计）。
    transit = get_transit_by_material()
    first_ids = set(_first_inventory_id_per_material().values())
    for r in rows:
        if r.get('id') in first_ids:
            r['sap_umlme'] = transit.get(r.get('material_code'), 0.0)
        else:
            r['sap_umlme'] = 0.0

    return jsonify({'success': True, 'total': total, 'page': page, 'per_page': per_page, 'data': serialize_rows(rows)})


@bp.route('/api/inventory', methods=['POST'])
def inventory_create():
    data = request.get_json() or {}
    pid = gen_id()
    data['id'] = pid

    # SAP库存字段（sap_umlme / SAP在途 为派生值，取自采购入库'在途'，不在此存储）
    sap_labst = float(data.get('sap_labst') or 0)
    sap_insme = float(data.get('sap_insme') or 0)
    sap_speme = float(data.get('sap_speme') or 0)
    quantity = sap_labst + sap_insme + sap_speme

    fields = ['id', 'material_code', 'name', 'quantity', 'unit', 'location', 'min_stock',
              'sap_labst', 'sap_insme', 'sap_speme']
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in ['id', 'material_code', 'name']]
    values += [quantity, data.get('unit'), data.get('location'), data.get('min_stock')]
    values += [sap_labst, sap_insme, sap_speme]

    execute_db(f"INSERT INTO inventory ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': pid, 'message': '创建成功'})


@bp.route('/api/inventory/<iid>', methods=['GET'])
def inventory_get(iid):
    row = query_db("SELECT * FROM inventory WHERE id=%s", (iid,), one=True)
    if not row:
        return jsonify({'success': False, 'message': '库存记录不存在'}), 404
    # 该物料的"采购在途"派生值（= 在途采购入库的计划到货数量之和），供前端只读展示；
    # 同时覆盖 sap_umlme，使详情/弹窗与列表一致（SAP在途 不再存手工值）。
    transit = get_transit_by_material()
    row['transit_sum'] = transit.get(row.get('material_code'), 0.0)
    row['sap_umlme'] = row['transit_sum']
    return jsonify({'success': True, 'data': serialize_row(row)})


@bp.route('/api/inventory/<iid>', methods=['PUT'])
def inventory_update(iid):
    data = request.get_json() or {}

    sap_labst = float(data.get('sap_labst') or 0)
    sap_insme = float(data.get('sap_insme') or 0)
    sap_speme = float(data.get('sap_speme') or 0)
    quantity = sap_labst + sap_insme + sap_speme

    fields = ['material_code', 'name', 'quantity', 'unit', 'location', 'min_stock',
              'sap_labst', 'sap_insme', 'sap_speme']
    sets = ', '.join([f"{f}=%s" for f in fields])
    values = [data.get('material_code'), data.get('name'), quantity, data.get('unit'),
              data.get('location'), data.get('min_stock'),
              sap_labst, sap_insme, sap_speme, iid]
    execute_db(f"UPDATE inventory SET {sets}, updated_at=NOW() WHERE id=%s", values)
    return jsonify({'success': True, 'message': '更新成功'})


@bp.route('/api/inventory/<iid>', methods=['DELETE'])
def inventory_delete(iid):
    execute_db("DELETE FROM inventory WHERE id=%s", (iid,))
    return jsonify({'success': True, 'message': '删除成功'})


@bp.route('/api/inventory/export-template')
def inventory_export_template():
    """导出库存导入模板（表头 + 示例/说明行），供批量填写后导入。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '库存导入模板'
    headers = ['物料编码', '物料名称', 'SAP非限制', 'SAP质检', 'SAP冻结', '单位', '仓库', '最小库存']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')

    # 示例行
    ws.append(['M001', '示例原料A', 100, 10, 5, 'KG', 'WH01', 20])
    # 说明行
    ws.append(['（以下为示例，请按实际填写；数量=非限制+质检+冻结 自动计算，SAP在途由系统派生无需填写）',
               '', '', '', '', '', '', ''])

    widths = [14, 16, 12, 10, 10, 8, 10, 10]
    for i, w in enumerate(widths, start=1):
        from openpyxl.utils import get_column_letter
        ws.column_dimensions[get_column_letter(i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='库存导入模板.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


_INVENTORY_IMPORT_COLMAP = {
    '物料编码': 'material_code', '物料名称': 'name', 'SAP非限制': 'sap_labst',
    'SAP质检': 'sap_insme', 'SAP冻结': 'sap_speme', '单位': 'unit',
    '仓库': 'location', '最小库存': 'min_stock',
}


@bp.route('/api/inventory/import', methods=['POST'])
def inventory_import():
    """批量导入库存（xlsx）。必需列：物料编码/物料名称。重复编码跳过；SAP字段缺省为0。"""
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
    idx = {cn: i for i, cn in enumerate(header) if cn in _INVENTORY_IMPORT_COLMAP}
    for req_col in ('物料编码', '物料名称'):
        if req_col not in idx:
            return jsonify({'success': False, 'error': '表头缺少必需列：' + req_col}), 400

    existing = {r['material_code'] for r in query_db(
        "SELECT material_code FROM inventory WHERE material_code IS NOT NULL")}

    def get_val(row, cn):
        i = idx.get(cn)
        return row[i] if (i is not None and i < len(row)) else None

    def to_num(v):
        if v is None or v == '':
            return 0
        try:
            return float(v)
        except (ValueError, TypeError):
            return 0

    created = 0
    skipped = 0
    errors = []
    for ri, raw in enumerate(raw_rows[1:], start=2):
        mat = get_val(raw, '物料编码')
        mat = str(mat).strip() if mat is not None else ''
        name = get_val(raw, '物料名称')
        name = str(name).strip() if name is not None else ''
        if not mat and not name:
            continue  # 空行跳过
        if not mat:
            errors.append({'row': ri, 'reason': '物料编码为空'}); continue
        if not name:
            errors.append({'row': ri, 'reason': '物料名称为空'}); continue
        if mat in existing:
            skipped += 1
            errors.append({'row': ri, 'reason': '编码 %s 已存在，跳过' % mat}); continue

        sap_labst = to_num(get_val(raw, 'SAP非限制'))
        sap_insme = to_num(get_val(raw, 'SAP质检'))
        sap_speme = to_num(get_val(raw, 'SAP冻结'))
        quantity = sap_labst + sap_insme + sap_speme

        data = {
            'id': gen_id(),
            'material_code': mat, 'name': name, 'quantity': quantity,
            'unit': str(get_val(raw, '单位') or '').strip(),
            'location': str(get_val(raw, '仓库') or '').strip(),
            'min_stock': to_num(get_val(raw, '最小库存')),
            'sap_labst': sap_labst, 'sap_insme': sap_insme, 'sap_speme': sap_speme,
        }
        fields = safe_fields(data, 'inventory')
        cols = ', '.join(fields)
        placeholders = ', '.join(['%s'] * len(fields))
        values = [data[f] for f in fields]
        execute_db(f"INSERT INTO inventory ({cols}) VALUES ({placeholders})", values)
        existing.add(mat)
        created += 1

    msg = '导入完成：新增 %d 条，跳过 %d 条（重复），错误 %d 条。' % (created, skipped, len(errors))
    return jsonify({'success': True, 'message': msg, 'created': created, 'skipped': skipped, 'errors': errors})


@bp.route('/api/inventory/export-data')
def inventory_export_data():
    """导出当前库存数据为 XLSX（含派生 SAP在途）。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    rows = query_db("""
        SELECT
            COALESCE(NULLIF(material_code,''), sap_matnr) as material_code,
            COALESCE(NULLIF(name,''), sap_maktx) as name,
            quantity, unit,
            COALESCE(NULLIF(location,''), sap_lgort) as location,
            min_stock,
            COALESCE(sap_labst,0) as sap_labst,
            COALESCE(sap_insme,0) as sap_insme,
            COALESCE(sap_speme,0) as sap_speme
        FROM inventory ORDER BY material_code
    """)
    transit = get_transit_by_material()

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '库存数据'
    headers = ['物料编码', '物料名称', '数量', '单位', '仓库', '最小库存',
               'SAP非限制', 'SAP质检', 'SAP冻结', 'SAP在途']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')

    for r in rows:
        mat = r.get('material_code') or ''
        ws.append([
            mat, r.get('name') or '',
            round(float(r.get('quantity') or 0), 3),
            r.get('unit') or '', r.get('location') or '',
            round(float(r.get('min_stock') or 0), 2),
            round(float(r.get('sap_labst') or 0), 2),
            round(float(r.get('sap_insme') or 0), 2),
            round(float(r.get('sap_speme') or 0), 2),
            round(transit.get(mat, 0.0), 2),
        ])

    widths = [14, 16, 12, 8, 10, 10, 12, 10, 10, 10]
    for i, w in enumerate(widths, start=1):
        from openpyxl.utils import get_column_letter
        ws.column_dimensions[get_column_letter(i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='库存数据.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')
