# -*- coding: utf-8 -*-
"""无限量物料管理"""

from flask import Blueprint, request, jsonify, send_file

from ..database import query_db, execute_db
from ..utils import serialize_rows

bp = Blueprint('unlimited', __name__)


@bp.route('/api/unlimited-materials')
def unlimited_materials_list():
    rows = query_db("SELECT * FROM unlimited_materials ORDER BY created_at DESC")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@bp.route('/api/unlimited-materials', methods=['POST'])
def unlimited_materials_create():
    data = request.get_json() or {}
    execute_db(
        "INSERT IGNORE INTO unlimited_materials (material_code, material_desc) VALUES (%s, %s)",
        (data.get('material_code'), data.get('material_desc'))
    )
    return jsonify({'success': True, 'message': '添加成功'})


@bp.route('/api/unlimited-materials/<mid>', methods=['DELETE'])
def unlimited_materials_delete(mid):
    execute_db("DELETE FROM unlimited_materials WHERE id=%s", (mid,))
    return jsonify({'success': True, 'message': '删除成功'})


@bp.route('/api/unlimited-materials/export-template')
def unlimited_materials_export_template():
    """导出无限量物料导入模板（表头 + 示例/说明行）。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '无限量物料导入模板'
    headers = ['物料编码', '物料描述']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')
    ws.append(['UM001', '示例无限量物料'])
    ws.append(['（示例行，请按实际填写；物料编码重复将被跳过）', ''])
    widths = [16, 24]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='无限量物料导入模板.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


_UN_COLMAP = {'物料编码': 'material_code', '物料描述': 'material_desc'}


@bp.route('/api/unlimited-materials/import', methods=['POST'])
def unlimited_materials_import():
    """批量导入无限量物料（xlsx）。必需列：物料编码。重复编码跳过。"""
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
    idx = {cn: i for i, cn in enumerate(header) if cn in _UN_COLMAP}
    if '物料编码' not in idx:
        return jsonify({'success': False, 'error': '表头缺少必需列：物料编码'}), 400

    existing = {r['material_code'] for r in
                query_db("SELECT material_code FROM unlimited_materials WHERE material_code IS NOT NULL")}

    def get_val(row, cn):
        i = idx.get(cn)
        return row[i] if (i is not None and i < len(row)) else None

    created = 0
    skipped = 0
    errors = []
    for ri, raw in enumerate(raw_rows[1:], start=2):
        code = str(get_val(raw, '物料编码') or '').strip()
        desc = str(get_val(raw, '物料描述') or '').strip()
        if not code and not desc:
            continue
        if not code:
            errors.append({'row': ri, 'reason': '物料编码为空'}); continue
        if code in existing:
            skipped += 1
            errors.append({'row': ri, 'reason': '物料编码 %s 已存在，跳过' % code}); continue
        execute_db(
            "INSERT INTO unlimited_materials (material_code, material_desc) VALUES (%s, %s)",
            (code, desc)
        )
        existing.add(code)
        created += 1

    return jsonify({'success': True, 'created': created, 'skipped': skipped,
                    'error_count': len(errors), 'errors': errors[:50],
                    'message': '导入完成：新增 %d 条，跳过 %d 条（重复），问题 %d 条。' % (created, skipped, len(errors))})
