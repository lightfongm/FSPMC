# -*- coding: utf-8 -*-
"""仓库仓位管理"""

from flask import Blueprint, request, jsonify, send_file

from ..database import query_db, execute_db
from ..utils import gen_id, serialize_rows

bp = Blueprint('warehouse', __name__)


@bp.route('/api/warehouse-locations')
def warehouse_locations_list():
    rows = query_db("SELECT * FROM warehouse_locations ORDER BY warehouse_code")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@bp.route('/api/warehouse-locations', methods=['POST'])
def warehouse_locations_create():
    data = request.get_json() or {}
    wid = gen_id()
    data['id'] = wid
    fields = ['id', 'warehouse_code', 'warehouse_name', 'warehouse_type', 'location_code', 'is_enabled', 'note']
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]
    execute_db(f"INSERT INTO warehouse_locations ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': wid, 'message': '创建成功'})


@bp.route('/api/warehouse-locations/<wid>', methods=['DELETE'])
def warehouse_locations_delete(wid):
    execute_db("DELETE FROM warehouse_locations WHERE id=%s", (wid,))
    return jsonify({'success': True, 'message': '删除成功'})


@bp.route('/api/warehouse-locations/export-template')
def warehouse_locations_export_template():
    """导出仓库仓位导入模板（表头 + 示例/说明行）。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '仓库仓位导入模板'
    headers = ['仓库编码', '仓库名称', '仓库类型', '仓位编码', '是否启用', '备注']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')
    ws.append(['WH001', '原料仓', '原料仓', 'A01', '启用', '示例'])
    ws.append(['（示例行，请按实际填写；是否启用填：启用/禁用）', '', '', '', '', ''])
    widths = [12, 16, 14, 12, 10, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='仓库仓位导入模板.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


_WH_COLMAP = {'仓库编码': 'warehouse_code', '仓库名称': 'warehouse_name', '仓库类型': 'warehouse_type',
              '仓位编码': 'location_code', '是否启用': 'is_enabled', '备注': 'note'}


@bp.route('/api/warehouse-locations/import', methods=['POST'])
def warehouse_locations_import():
    """批量导入仓库仓位（xlsx）。必需列：仓库编码/仓库名称。重复编码跳过。"""
    import openpyxl
    from io import BytesIO

    # 【P2-22 修复】走共享的安全读取：扩展名白名单 + 大小上限 + read_only 流式解析，
    # 避免恶意/损坏 xlsx（zip bomb）吃满内存 CPU 拖垮单进程服务。
    # 【P2-20】异常串改由 err_payload 统一处理（仅 DEBUG 才附详情）。
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
    idx = {cn: i for i, cn in enumerate(header) if cn in _WH_COLMAP}
    for req_col in ('仓库编码', '仓库名称'):
        if req_col not in idx:
            return jsonify({'success': False, 'error': '表头缺少必需列：' + req_col}), 400

    existing = {r['warehouse_code'] for r in
                query_db("SELECT warehouse_code FROM warehouse_locations WHERE warehouse_code IS NOT NULL")}

    def get_val(row, cn):
        i = idx.get(cn)
        return row[i] if (i is not None and i < len(row)) else None

    created = 0
    skipped = 0
    errors = []
    for ri, raw in enumerate(raw_rows[1:], start=2):
        code = str(get_val(raw, '仓库编码') or '').strip()
        name = str(get_val(raw, '仓库名称') or '').strip()
        if not code and not name:
            continue
        if not code:
            errors.append({'row': ri, 'reason': '仓库编码为空'}); continue
        if not name:
            errors.append({'row': ri, 'reason': '仓库名称为空'}); continue
        if code in existing:
            skipped += 1
            errors.append({'row': ri, 'reason': '仓库编码 %s 已存在，跳过' % code}); continue
        en = str(get_val(raw, '是否启用') or '').strip()
        is_enabled = 1 if en in ('启用', '1', '是', 'true', 'TRUE', 'yes', 'YES') else 0
        data = {
            'id': gen_id(),
            'warehouse_code': code, 'warehouse_name': name,
            'warehouse_type': str(get_val(raw, '仓库类型') or '').strip(),
            'location_code': str(get_val(raw, '仓位编码') or '').strip(),
            'is_enabled': is_enabled,
            'note': str(get_val(raw, '备注') or '').strip(),
        }
        fields = list(data.keys())
        cols = ', '.join(fields)
        placeholders = ', '.join(['%s'] * len(fields))
        values = [data[f] for f in fields]
        execute_db(f"INSERT INTO warehouse_locations ({cols}) VALUES ({placeholders})", values)
        existing.add(code)
        created += 1

    return jsonify({'success': True, 'created': created, 'skipped': skipped,
                    'error_count': len(errors), 'errors': errors[:50],
                    'message': '导入完成：新增 %d 条，跳过 %d 条（重复），问题 %d 条。' % (created, skipped, len(errors))})
