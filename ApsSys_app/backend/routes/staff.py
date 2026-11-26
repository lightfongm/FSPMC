# -*- coding: utf-8 -*-
"""人员管理"""

from flask import Blueprint, request, jsonify, send_file

from ..database import query_db, execute_db, safe_fields
from ..utils import gen_id, serialize_rows

bp = Blueprint('staff', __name__)


@bp.route('/api/staff')
def staff_list():
    rows = query_db("SELECT * FROM staff ORDER BY team, name")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@bp.route('/api/staff', methods=['POST'])
def staff_create():
    data = request.get_json() or {}
    sid = gen_id()
    data['id'] = sid
    fields = ['id', 'name', 'role', 'team', 'shift', 'phone', 'status', 'note']
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]
    execute_db(f"INSERT INTO staff ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': sid, 'message': '创建成功'})


@bp.route('/api/staff/<sid>', methods=['DELETE'])
def staff_delete(sid):
    execute_db("DELETE FROM staff WHERE id=%s", (sid,))
    return jsonify({'success': True, 'message': '删除成功'})


@bp.route('/api/staff/export-template')
def staff_export_template():
    """导出人员导入模板（表头 + 示例/说明行）。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '人员导入模板'
    headers = ['姓名', '角色', '班组', '班次', '电话', '状态', '备注']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')
    ws.append(['张三', '操作员', '甲班', '白班', '13800138000', 'active', '示例'])
    ws.append(['（示例行，请按实际填写；状态限：active(在职)/inactive(离职)）', '', '', '', '', '', ''])
    widths = [12, 12, 10, 10, 16, 12, 18]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='人员导入模板.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


_STAFF_COLMAP = {'姓名': 'name', '角色': 'role', '班组': 'team', '班次': 'shift',
                 '电话': 'phone', '状态': 'status', '备注': 'note'}


@bp.route('/api/staff/import', methods=['POST'])
def staff_import():
    """批量导入人员（xlsx）。必需列：姓名。"""
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
    idx = {cn: i for i, cn in enumerate(header) if cn in _STAFF_COLMAP}
    if '姓名' not in idx:
        return jsonify({'success': False, 'error': '表头缺少必需列：姓名'}), 400

    def get_val(row, cn):
        i = idx.get(cn)
        return row[i] if (i is not None and i < len(row)) else None

    created = 0
    errors = []
    for ri, raw in enumerate(raw_rows[1:], start=2):
        name = str(get_val(raw, '姓名') or '').strip()
        if not name:
            continue
        data = {
            'id': gen_id(),
            'name': name,
            'role': str(get_val(raw, '角色') or '').strip(),
            'team': str(get_val(raw, '班组') or '').strip(),
            'shift': str(get_val(raw, '班次') or '').strip(),
            'phone': str(get_val(raw, '电话') or '').strip(),
            'status': str(get_val(raw, '状态') or '').strip() or 'active',
            'note': str(get_val(raw, '备注') or '').strip(),
        }
        fields = safe_fields(data, 'staff')
        cols = ', '.join(fields)
        placeholders = ', '.join(['%s'] * len(fields))
        values = [data[f] for f in fields]
        execute_db(f"INSERT INTO staff ({cols}) VALUES ({placeholders})", values)
        created += 1

    return jsonify({'success': True, 'created': created, 'skipped': 0,
                    'error_count': len(errors), 'errors': errors[:50],
                    'message': '导入完成：新增 %d 条，问题 %d 条。' % (created, len(errors))})
