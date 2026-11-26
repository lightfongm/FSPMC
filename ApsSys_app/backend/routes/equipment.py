# -*- coding: utf-8 -*-
"""设备管理"""

import logging
from datetime import datetime, date

import pymysql
from flask import Blueprint, request, jsonify, send_file

from ..database import query_db, execute_db, safe_fields
from ..utils import gen_id, serialize_row, serialize_rows
from ..errors import err_payload

bp = Blueprint('equipment', __name__)

# 状态为「保养/维修」时，开始时间、结束时间必填
_EQ_TIME_REQUIRED_STATUS = {'保养', '维修'}


def _migrate():
    """存量表加列（幂等）。"""
    cols = query_db("SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                    "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'equipment'")
    col_names = {r['COLUMN_NAME'] for r in cols}
    if 'start_time' not in col_names:
        execute_db("ALTER TABLE equipment ADD COLUMN start_time DATETIME DEFAULT NULL "
                   "COMMENT '开始时间（保养/维修时必填）'")
    if 'end_time' not in col_names:
        execute_db("ALTER TABLE equipment ADD COLUMN end_time DATETIME DEFAULT NULL "
                   "COMMENT '结束时间（保养/维修时必填）'")


# 【P2-26 修复 · 2026-10-14】导入期 DDL 加异常保护：DB 故障不应导致整个后端起不来。
try:
    _migrate()
except Exception as _e:
    logging.getLogger(__name__).warning('equipment 启动迁移失败（已忽略，不影响启动）: %s', _e)


def _eq_validate_time(data):
    """返回校验错误文案或 None。"""
    status = (data.get('status') or '').strip()
    if status in _EQ_TIME_REQUIRED_STATUS:
        if not data.get('start_time'):
            return '状态为「%s」时，开始时间必填' % status
        if not data.get('end_time'):
            return '状态为「%s」时，结束时间必填' % status
    return None


@bp.route('/api/equipment')
def equipment_list():
    rows = query_db("SELECT * FROM equipment ORDER BY code")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@bp.route('/api/equipment', methods=['POST'])
def equipment_create():
    data = request.get_json() or {}
    eid = gen_id()
    data['id'] = eid
    fields = ['id', 'name', 'code', 'type', 'workshop', 'capacity', 'unit', 'status', 'note',
              'start_time', 'end_time']
    err = _eq_validate_time(data)
    if err:
        return jsonify({'success': False, 'message': err}), 400
    # 可用/不可用时，开始/结束时间强制为空（仅保养/维修可填）
    if (data.get('status') or '').strip() not in _EQ_TIME_REQUIRED_STATUS:
        data['start_time'] = None
        data['end_time'] = None
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]
    try:
        execute_db(f"INSERT INTO equipment ({cols}) VALUES ({placeholders})", values)
    except pymysql.IntegrityError as e:
        msg = str(e)
        if 'uk_eq_name' in msg or 'name' in msg:
            return jsonify({'success': False, 'message': '设备名称已重复，请更换'}), 400
        return jsonify({'success': False, 'message': '设备编码已存在（重复），请更换'}), 400
    return jsonify({'success': True, 'id': eid, 'message': '创建成功'})


@bp.route('/api/equipment/<eid>', methods=['GET'])
def equipment_get(eid):
    row = query_db("SELECT * FROM equipment WHERE id=%s", (eid,), one=True)
    if not row:
        return jsonify({'success': False, 'message': '设备不存在'}), 404
    return jsonify({'success': True, 'data': serialize_row(row)})


@bp.route('/api/equipment/<eid>', methods=['PUT'])
def equipment_update(eid):
    data = request.get_json() or {}
    fields = ['name', 'code', 'type', 'workshop', 'capacity', 'unit', 'status', 'note',
              'start_time', 'end_time']
    err = _eq_validate_time(data)
    if err:
        return jsonify({'success': False, 'message': err}), 400
    # 可用/不可用时，开始/结束时间强制为空（仅保养/维修可填）
    if (data.get('status') or '').strip() not in _EQ_TIME_REQUIRED_STATUS:
        data['start_time'] = None
        data['end_time'] = None
    sets = ', '.join([f"{f}=%s" for f in fields])
    values = [data.get(f) for f in fields] + [eid]
    try:
        execute_db(f"UPDATE equipment SET {sets} WHERE id=%s", values)
    except pymysql.IntegrityError as e:
        msg = str(e)
        if 'uk_eq_name' in msg or 'name' in msg:
            return jsonify({'success': False, 'message': '设备名称已重复，请更换'}), 400
        return jsonify({'success': False, 'message': '设备编码已存在（重复），请更换'}), 400
    return jsonify({'success': True, 'message': '更新成功'})


@bp.route('/api/equipment/<eid>', methods=['DELETE'])
def equipment_delete(eid):
    execute_db("DELETE FROM equipment WHERE id=%s", (eid,))
    return jsonify({'success': True, 'message': '删除成功'})


@bp.route('/api/equipment/export-template')
def equipment_export_template():
    """导出设备导入模板（表头 + 示例/说明行）。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '设备导入模板'
    headers = ['设备编码', '设备名称', '类型', '作业中心', '产能', '单位', '状态', '备注', '开始时间', '结束时间']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')
    ws.append(['SB001', '示例设备', '生产设备', '车间1', 120, 'KG', '可用', '示例说明', '', ''])
    ws.append(['（示例行，请按实际填写；类型限：生产设备/包装设备/清洗设备/其他设备；'
               '状态限：可用/维修/保养/不可用；当状态为「保养」或「维修」时，开始时间/结束时间必填，格式 YYYY-MM-DD HH:MM:SS）',
               '', '', '', '', '', '', '', '', ''])
    widths = [12, 16, 12, 12, 10, 8, 10, 20, 20, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='设备导入模板.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@bp.route('/api/equipment/export')
def equipment_export():
    """导出设备全部数据（含开始/结束时间）。支持 ?status= 状态筛选。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    status = (request.args.get('status') or '').strip()
    sql = "SELECT * FROM equipment"
    params = []
    if status:
        sql += " WHERE status = %s"
        params.append(status)
    sql += " ORDER BY code"
    rows = query_db(sql, params)
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '设备数据'
    headers = ['设备编码', '设备名称', '类型', '作业中心', '产能', '单位', '状态', '备注', '开始时间', '结束时间']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')

    def fmt_dt(v):
        if not v:
            return ''
        if isinstance(v, (datetime, date)):
            return v.strftime('%Y-%m-%d %H:%M:%S')
        return str(v)

    for r in rows:
        ws.append([
            r.get('code') or '', r.get('name') or '', r.get('type') or '',
            r.get('workshop') or '',
            r.get('capacity') if r.get('capacity') is not None else '',
            r.get('unit') or '', r.get('status') or '', r.get('note') or '',
            fmt_dt(r.get('start_time')), fmt_dt(r.get('end_time')),
        ])
    widths = [12, 16, 12, 12, 10, 8, 10, 20, 20, 20]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='设备数据.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


_EQ_COLMAP = {'设备编码': 'code', '设备名称': 'name', '类型': 'type', '作业中心': 'workshop',
              '产能': 'capacity', '单位': 'unit', '状态': 'status', '备注': 'note',
              '开始时间': 'start_time', '结束时间': 'end_time'}
_EQ_TYPE_ENUM = {'生产设备', '包装设备', '清洗设备', '其他设备'}
_EQ_STATUS_ENUM = {'可用', '维修', '保养', '不可用'}


def _eq_to_datetime(v):
    """xlsx 单元格 -> MySQL DATETIME 字符串；空返回 None。"""
    if v is None or v == '':
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime('%Y-%m-%d %H:%M:%S')
    s = str(v).strip().replace('T', ' ').replace('Z', '')
    if not s:
        return None
    return s


@bp.route('/api/equipment/import', methods=['POST'])
def equipment_import():
    """批量导入设备（xlsx）。必需列：设备编码/设备名称。重复编码跳过。"""
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
    idx = {cn: i for i, cn in enumerate(header) if cn in _EQ_COLMAP}
    for req_col in ('设备编码', '设备名称'):
        if req_col not in idx:
            return jsonify({'success': False, 'error': '表头缺少必需列：' + req_col}), 400

    existing = {r['code'] for r in query_db("SELECT code FROM equipment WHERE code IS NOT NULL")}

    def get_val(row, cn):
        i = idx.get(cn)
        return row[i] if (i is not None and i < len(row)) else None

    def to_num(v):
        if v is None or v == '':
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    created = 0
    skipped = 0
    errors = []
    for ri, raw in enumerate(raw_rows[1:], start=2):
        code = str(get_val(raw, '设备编码') or '').strip()
        name = str(get_val(raw, '设备名称') or '').strip()
        if not code and not name:
            continue
        if not code:
            errors.append({'row': ri, 'reason': '设备编码为空'}); continue
        if not name:
            errors.append({'row': ri, 'reason': '设备名称为空'}); continue
        if code in existing:
            skipped += 1
            errors.append({'row': ri, 'reason': '设备编码 %s 已存在，跳过' % code}); continue
        typ = str(get_val(raw, '类型') or '').strip()
        if typ and typ not in _EQ_TYPE_ENUM:
            errors.append({'row': ri, 'reason': '类型非法：%s（限 生产设备/包装设备/清洗设备/其他设备）' % typ}); continue
        status = str(get_val(raw, '状态') or '').strip()
        if status and status not in _EQ_STATUS_ENUM:
            errors.append({'row': ri, 'reason': '状态非法：%s（限 可用/维修/保养/不可用）' % status}); continue
        start_time = _eq_to_datetime(get_val(raw, '开始时间'))
        end_time = _eq_to_datetime(get_val(raw, '结束时间'))
        if status in _EQ_TIME_REQUIRED_STATUS:
            if not start_time:
                errors.append({'row': ri, 'reason': '状态为「%s」时，开始时间必填' % status}); continue
            if not end_time:
                errors.append({'row': ri, 'reason': '状态为「%s」时，结束时间必填' % status}); continue
        data = {
            'id': gen_id(),
            'code': code, 'name': name,
            'type': typ or '生产设备',
            'workshop': str(get_val(raw, '作业中心') or '').strip(),
            'capacity': to_num(get_val(raw, '产能')),
            'unit': str(get_val(raw, '单位') or '').strip(),
            'status': status or '可用',
            'note': str(get_val(raw, '备注') or '').strip(),
            'start_time': start_time,
            'end_time': end_time,
        }
        fields = safe_fields(data, 'equipment')
        cols = ', '.join(fields)
        placeholders = ', '.join(['%s'] * len(fields))
        values = [data[f] for f in fields]
        try:
            execute_db(f"INSERT INTO equipment ({cols}) VALUES ({placeholders})", values)
        except pymysql.IntegrityError as e:
            msg = str(e)
            if 'uk_eq_name' in msg:
                errors.append({'row': ri, 'reason': '设备名称已重复：%s' % name}); continue
            errors.append({'row': ri, 'reason': '保存失败：%s' % msg}); continue
        existing.add(code)
        created += 1

    return jsonify({'success': True, 'created': created, 'skipped': skipped,
                    'error_count': len(errors), 'errors': errors[:50],
                    'message': '导入完成：新增 %d 条，跳过 %d 条（重复），问题 %d 条。' % (created, skipped, len(errors))})
