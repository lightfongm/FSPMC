# -*- coding: utf-8 -*-
"""供应商管理

供应商编码规则：G + 三位流水号（G001, G002, ...），作为业务主键。
新增时由后端自动生成，编辑时编码只读不可改。
"""

import logging
from flask import Blueprint, request, jsonify, send_file

from ..database import query_db, execute_db
from ..utils import serialize_rows

bp = Blueprint('suppliers', __name__)


def _ensure_table():
    """确保 suppliers 表存在（应用启动时调用，幂等）"""
    execute_db("""
        CREATE TABLE IF NOT EXISTS suppliers (
            supplier_code  VARCHAR(50)  PRIMARY KEY COMMENT '供应商编码 G+3位流水号',
            supplier_name  VARCHAR(200) NOT NULL COMMENT '供应商名称',
            contact_person VARCHAR(100) COMMENT '联系人',
            phone          VARCHAR(50)  COMMENT '电话',
            note           VARCHAR(500) COMMENT '备注',
            updated_at     DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def _next_code():
    """生成下一个供应商编码 G+3位流水号（在现有 G 前缀编码中取最大值 +1）"""
    rows = query_db("SELECT supplier_code FROM suppliers WHERE supplier_code LIKE 'G%'")
    max_n = 0
    for r in rows:
        code = (r.get('supplier_code') or '').strip()
        if code.startswith('G'):
            try:
                n = int(code[1:])
                if n > max_n:
                    max_n = n
            except ValueError:
                pass
    return f"G{max_n + 1:03d}"


@bp.route('/api/suppliers/next-code')
def suppliers_next_code():
    _ensure_table()
    return jsonify({'success': True, 'code': _next_code()})


@bp.route('/api/suppliers')
def suppliers_list():
    _ensure_table()
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(supplier_name LIKE %s OR supplier_code LIKE %s "
                          "OR contact_person LIKE %s OR phone LIKE %s)")
        args.extend([f'%{search}%'] * 4)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM suppliers WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM suppliers WHERE {where} ORDER BY supplier_code LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )
    return jsonify({'success': True, 'total': total, 'page': page,
                    'per_page': per_page, 'data': serialize_rows(rows)})


@bp.route('/api/suppliers', methods=['POST'])
def suppliers_create():
    _ensure_table()
    data = request.get_json() or {}
    name = (data.get('supplier_name') or '').strip()
    if not name:
        return jsonify({'success': False, 'message': '供应商名称不能为空'}), 400

    code = (data.get('supplier_code') or '').strip() or _next_code()
    if not code.startswith('G') or not code[1:].isdigit():
        return jsonify({'success': False, 'message': '供应商编码格式应为 G+3位流水号'}), 400

    exists = query_db("SELECT 1 FROM suppliers WHERE supplier_code=%s", (code,), one=True)
    if exists:
        return jsonify({'success': False, 'message': f'供应商编码 {code} 已存在'}), 400

    execute_db(
        "INSERT INTO suppliers (supplier_code, supplier_name, contact_person, phone) "
        "VALUES (%s, %s, %s, %s)",
        (code, name, data.get('contact_person'), data.get('phone'))
    )
    return jsonify({'success': True, 'supplier_code': code, 'message': '创建成功'})


@bp.route('/api/suppliers/<code>', methods=['PUT'])
def suppliers_update(code):
    data = request.get_json() or {}
    sets, vals = [], []
    if 'supplier_name' in data:
        name = (data.get('supplier_name') or '').strip()
        if not name:
            return jsonify({'success': False, 'message': '供应商名称不能为空'}), 400
        sets.append("supplier_name=%s")
        vals.append(name)
    if 'contact_person' in data:
        sets.append("contact_person=%s")
        vals.append(data.get('contact_person'))
    if 'phone' in data:
        sets.append("phone=%s")
        vals.append(data.get('phone'))
    if not sets:
        return jsonify({'success': False, 'message': '无更新字段'}), 400

    sets.append("updated_at=NOW()")
    vals.append(code)
    execute_db(f"UPDATE suppliers SET {', '.join(sets)} WHERE supplier_code=%s", vals)
    return jsonify({'success': True, 'message': '更新成功'})


@bp.route('/api/suppliers/<code>', methods=['DELETE'])
def suppliers_delete(code):
    execute_db("DELETE FROM suppliers WHERE supplier_code=%s", (code,))
    return jsonify({'success': True, 'message': '删除成功'})


@bp.route('/api/suppliers/export-template')
def suppliers_export_template():
    """导出供应商导入模板（表头 + 示例/说明行）。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '供应商导入模板'
    headers = ['供应商编码', '供应商名称', '联系人', '电话', '备注']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')
    ws.append(['G001', '示例供应商', '李四', '13900139000', '示例'])
    ws.append(['（示例行，请按实际填写；供应商编码留空则自动生成 G+3位流水号）', '', '', '', ''])
    widths = [14, 20, 12, 16, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='供应商导入模板.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


_SUP_COLMAP = {'供应商编码': 'supplier_code', '供应商名称': 'supplier_name',
               '联系人': 'contact_person', '电话': 'phone', '备注': 'note'}


@bp.route('/api/suppliers/import', methods=['POST'])
def suppliers_import():
    """批量导入供应商（xlsx）。必需列：供应商名称。编码重复跳过，留空自动生成。"""
    import openpyxl
    from io import BytesIO

    _ensure_table()
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
    idx = {cn: i for i, cn in enumerate(header) if cn in _SUP_COLMAP}
    if '供应商名称' not in idx:
        return jsonify({'success': False, 'error': '表头缺少必需列：供应商名称'}), 400

    existing = {r['supplier_code'] for r in query_db("SELECT supplier_code FROM suppliers")}

    def get_val(row, cn):
        i = idx.get(cn)
        return row[i] if (i is not None and i < len(row)) else None

    created = 0
    skipped = 0
    errors = []
    for ri, raw in enumerate(raw_rows[1:], start=2):
        name = str(get_val(raw, '供应商名称') or '').strip()
        if not name:
            continue
        code = str(get_val(raw, '供应商编码') or '').strip()
        if code:
            if not code.startswith('G') or not code[1:].isdigit():
                errors.append({'row': ri, 'reason': '供应商编码格式应为 G+3位流水号：%s' % code}); continue
        else:
            code = _next_code()
        if code in existing:
            skipped += 1
            errors.append({'row': ri, 'reason': '供应商编码 %s 已存在，跳过' % code}); continue
        execute_db(
            "INSERT INTO suppliers (supplier_code, supplier_name, contact_person, phone) "
            "VALUES (%s, %s, %s, %s)",
            (code, name, str(get_val(raw, '联系人') or '').strip(), str(get_val(raw, '电话') or '').strip())
        )
        existing.add(code)
        created += 1

    return jsonify({'success': True, 'created': created, 'skipped': skipped,
                    'error_count': len(errors), 'errors': errors[:50],
                    'message': '导入完成：新增 %d 条，跳过 %d 条（重复），问题 %d 条。' % (created, skipped, len(errors))})


# 应用启动时确保表存在
# 【P2-26 修复 · 2026-10-14】导入期建表加异常保护：DB 故障不应导致整个后端起不来。
try:
    _ensure_table()
except Exception as _e:
    logging.getLogger(__name__).warning('suppliers 启动建表失败（已忽略）: %s', _e)
