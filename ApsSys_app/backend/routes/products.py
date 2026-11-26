# -*- coding: utf-8 -*-
"""产品/物料管理"""

import logging
from datetime import datetime
from flask import Blueprint, request, jsonify, send_file

from ..database import query_db, execute_db, safe_fields
from ..utils import gen_id, serialize_row, serialize_rows
from ..errors import err_payload

bp = Blueprint('products', __name__)


def _migrate():
    """存量表加列（幂等）。"""
    cols = query_db(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'products'"
    )
    col_names = {r['COLUMN_NAME'] for r in cols}
    if 'min_purchase_qty' not in col_names:
        execute_db("ALTER TABLE products ADD COLUMN min_purchase_qty DECIMAL(18,3) DEFAULT NULL "
                   "COMMENT '最小采购量' AFTER batch_size")
    if 'production_equipment' not in col_names:
        execute_db("ALTER TABLE products ADD COLUMN production_equipment VARCHAR(500) DEFAULT NULL "
                   "COMMENT '可用生产设备(逗号分隔设备ID)' AFTER batch_size")
    if 'changeover_time' not in col_names:
        execute_db("ALTER TABLE products ADD COLUMN changeover_time DECIMAL(18,3) DEFAULT NULL "
                   "COMMENT '转产时间(小时)' AFTER production_time")
    if 'shelf_life' not in col_names:
        execute_db("ALTER TABLE products ADD COLUMN shelf_life DECIMAL(10,2) DEFAULT NULL "
                   "COMMENT '保质期(天)' AFTER production_cycle")


# 【P2-26 修复 · 2026-10-14】导入期 DDL 若无异常保护，一次 DB 故障会让**整个后端起不来**
# （所有页签全挂，而非仅物料页受影响）。迁移只做"存量表加列"，失败应降级而非致命。
try:
    _migrate()
except Exception as _e:
    logging.getLogger(__name__).warning('products 启动迁移失败（已忽略，不影响启动）: %s', _e)


@bp.route('/api/products')
def products_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    ptype = request.args.get('type', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(material_code LIKE %s OR name LIKE %s OR sap_matnr LIKE %s)")
        args.extend([f'%{search}%'] * 3)
    if ptype:
        conditions.append("type=%s")
        args.append(ptype)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM products WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM products WHERE {where} ORDER BY material_code ASC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )
    return jsonify({'success': True, 'total': total, 'page': page, 'per_page': per_page, 'data': serialize_rows(rows)})


@bp.route('/api/products/list')
def products_list_all():
    """获取所有物料列表（下拉框用，无分页）

    【P1-12 修复 · 2026-10-14】SELECT 增补 `production_cycle`、`type`、`priority`：
    前端工单表单 `_woLeadDays`（enhanced.html:3554）读 `m.production_cycle` 作为
    「日产量为空时」的工期回退，但该字段原不在返回里 → cycle 恒 0 → 自动算完工日
    恒失效，保存时直接报「计划完工日期为空且无法自动计算」，用户被迫手填
    （新增与编辑路径均受影响）。
    同时补 `type`（产成品/半成品/采购件判断）与 `shelf_life`（保质期 floor），
    避免前端为拿这两个字段再逐物料回查。
    """
    rows = query_db(
        "SELECT material_code, name, unit, production_time, production_cycle, type, "
        "priority, shelf_life FROM products ORDER BY material_code")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@bp.route('/api/products/equipment/<code>')
def products_equipment_by_code(code):
    """按物料编码返回其『生产设备』(production_equipment)，逗号分隔的设备ID。
    供『排程结果-生成订单』时自动默认生产线：仅 1 个设备→默认该设备；空/>1→留空（前端处理）。"""
    row = query_db("SELECT production_equipment FROM products WHERE material_code=%s", (code,), one=True)
    pe = row.get('production_equipment') if row else None
    return jsonify({'success': True, 'production_equipment': pe or ''})


@bp.route('/api/products/equipment-names/<code>')
def products_equipment_names(code):
    """按物料编码返回其『生产设备』对应的设备【名称】列表（供拖拽校验用）。
    仅取 type='生产设备' 且 状态≠'不可用' 的设备名称——与『产线排产看板』展示的设备列一致。
    前端拖拽待排产工单到看板时，只有落点设备名在此列表内才允许放置。"""
    row = query_db("SELECT production_equipment FROM products WHERE material_code=%s", (code,), one=True)
    pe = (row.get('production_equipment') if row else None) or ''
    ids = [x.strip() for x in pe.split(',') if x.strip()]
    names = []
    if ids:
        ph = ','.join(['%s'] * len(ids))
        eqs = query_db(
            f"SELECT name FROM equipment WHERE id IN ({ph}) AND type='生产设备' AND status<>'不可用'",
            ids)
        names = [e['name'] for e in eqs]
    return jsonify({'success': True, 'names': names})


@bp.route('/api/products/<pid>', methods=['GET'])
def products_get(pid):
    row = query_db("SELECT * FROM products WHERE id=%s", (pid,), one=True)
    if not row:
        return jsonify({'success': False, 'message': '物料不存在'}), 404
    return jsonify({'success': True, 'data': serialize_row(row)})


@bp.route('/api/products', methods=['POST'])
def products_create():
    data = request.get_json() or {}
    pid = data.get('id') or gen_id()
    data['id'] = pid

    fields = ['id', 'material_code', 'name', 'type', 'category', 'workshop', 'secret',
              'production_time', 'changeover_time', 'production_cycle', 'shelf_life', 'min_purchase_qty', 'batch_size',
              'production_equipment',
              'energy_coeff', 'priority',
              'unit_price', 'safe_stock', 'unit', 'old_material_code', 'material_group',
              'material_trial_no', 'storage_condition', 'size_dimension', 'created_date', 'data_source']

    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]

    execute_db(f"INSERT INTO products ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': pid, 'message': '创建成功'})


@bp.route('/api/products/<pid>', methods=['PUT'])
def products_update(pid):
    data = request.get_json() or {}
    fields = ['material_code', 'name', 'type', 'category', 'workshop', 'secret',
              'production_time', 'changeover_time', 'production_cycle', 'shelf_life', 'min_purchase_qty', 'batch_size',
              'production_equipment',
              'energy_coeff', 'priority',
              'unit_price', 'safe_stock', 'unit', 'old_material_code', 'material_group',
              'material_trial_no', 'storage_condition', 'size_dimension', 'data_source']

    sets = ', '.join([f"{f}=%s" for f in fields])
    values = [data.get(f) for f in fields] + [pid]
    execute_db(f"UPDATE products SET {sets}, updated_at=NOW() WHERE id=%s", values)
    return jsonify({'success': True, 'message': '更新成功'})


@bp.route('/api/products/<pid>', methods=['DELETE'])
def products_delete(pid):
    execute_db("DELETE FROM products WHERE id=%s", (pid,))
    return jsonify({'success': True, 'message': '删除成功'})


@bp.route('/api/products/export-template')
def products_export_template():
    """导出物料导入模板（表头 + 示例/说明行），供批量填写后导入。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '物料导入模板'
    headers = ['物料编码', '物料名称', '类型', '分类', '作业中心', '单位',
               '日产量', '转产时间(小时)', '采购周期', '保质期(天)', '最小采购量', '包装量', '生产设备', '优先级']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')

    # 示例行
    ws.append(['P001', '示例产品A', '产成品', '类别1', '车间1', 'KG',
               300, 2, 7, 180, 100, 500, 'SB001,SB002', '高'])
    # 说明行
    ws.append(['（以下为示例，请按实际填写；类型限：原辅料/包材/半成品/产成品；'
               '“生产设备”填设备编码，多个用逗号分隔，如 SB001,SB002）',
               '', '', '', '', '', '', '', '', '', '', '', '', ''])

    widths = [14, 16, 10, 10, 12, 8, 10, 12, 10, 12, 12, 10, 22, 8]
    for i, w in enumerate(widths, start=1):
        from openpyxl.utils import get_column_letter
        ws.column_dimensions[get_column_letter(i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='物料导入模板.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# 导入：中文列名 -> DB 字段
_PRODUCT_IMPORT_COLMAP = {
    '物料编码': 'material_code', '物料名称': 'name', '类型': 'type', '分类': 'category',
    '作业中心': 'workshop', '单位': 'unit', '日产量': 'production_time',
    '转产时间(小时)': 'changeover_time', '采购周期': 'production_cycle', '保质期(天)': 'shelf_life', '最小采购量': 'min_purchase_qty',
    '包装量': 'batch_size', '生产设备': 'production_equipment', '优先级': 'priority',
}
_PRODUCT_TYPE_ENUM = {'原辅料', '包材', '半成品', '产成品'}


@bp.route('/api/products/import', methods=['POST'])
def products_import():
    """批量导入物料（xlsx）。必需列：物料编码/物料名称/类型/单位。
    重复编码跳过；必填缺失或类型非法记入 errors；生产设备按编码(或名称)映射到设备ID。"""
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
    idx = {cn: i for i, cn in enumerate(header) if cn in _PRODUCT_IMPORT_COLMAP}
    for req_col in ('物料编码', '物料名称', '类型', '单位'):
        if req_col not in idx:
            return jsonify({'success': False, 'error': '表头缺少必需列：' + req_col}), 400

    existing = {r['material_code'] for r in query_db(
        "SELECT material_code FROM products WHERE material_code IS NOT NULL")}
    equip = query_db("SELECT id, code, name FROM equipment")
    code2id = {e['code']: e['id'] for e in equip if e['code']}
    name2id = {e['name']: e['id'] for e in equip if e['name']}

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
        mat = (get_val(raw, '物料编码') or '')
        mat = str(mat).strip() if mat is not None else ''
        name = (get_val(raw, '物料名称') or '')
        name = str(name).strip() if name is not None else ''
        typ = (get_val(raw, '类型') or '')
        typ = str(typ).strip() if typ is not None else ''
        unit = (get_val(raw, '单位') or '')
        unit = str(unit).strip() if unit is not None else ''

        if not mat and not name:
            continue  # 空行跳过
        if not mat:
            errors.append({'row': ri, 'reason': '物料编码为空'}); continue
        if not name:
            errors.append({'row': ri, 'reason': '物料名称为空'}); continue
        if not typ:
            errors.append({'row': ri, 'reason': '类型为空'}); continue
        if typ not in _PRODUCT_TYPE_ENUM:
            errors.append({'row': ri, 'reason': '类型非法：%s（限 原辅料/包材/半成品/产成品）' % typ}); continue
        if not unit:
            errors.append({'row': ri, 'reason': '单位为空'}); continue
        if mat in existing:
            skipped += 1
            errors.append({'row': ri, 'reason': '编码 %s 已存在，跳过' % mat}); continue

        pe = get_val(raw, '生产设备')
        pe_ids = []
        if pe is not None and str(pe).strip():
            for tok in str(pe).split(','):
                tok = tok.strip()
                if not tok:
                    continue
                eid = code2id.get(tok) or name2id.get(tok)
                if eid:
                    pe_ids.append(eid)
                else:
                    errors.append({'row': ri, 'reason': '生产设备未匹配：%s（已忽略）' % tok})

        data = {
            'id': gen_id(),
            'material_code': mat, 'name': name, 'type': typ,
            'category': str(get_val(raw, '分类') or '').strip(),
            'workshop': str(get_val(raw, '作业中心') or '').strip(),
            'unit': unit,
            'production_time': to_num(get_val(raw, '日产量')),
            'changeover_time': to_num(get_val(raw, '转产时间(小时)')),
            'production_cycle': to_num(get_val(raw, '采购周期')),
            'shelf_life': to_num(get_val(raw, '保质期(天)')),
            'min_purchase_qty': to_num(get_val(raw, '最小采购量')),
            'batch_size': to_num(get_val(raw, '包装量')),
            'production_equipment': ','.join(pe_ids),
            'priority': str(get_val(raw, '优先级') or '').strip(),
            'data_source': 'import',
        }
        fields = safe_fields(data, 'products')
        cols = ', '.join(fields)
        placeholders = ', '.join(['%s'] * len(fields))
        values = [data[f] for f in fields]
        execute_db(f"INSERT INTO products ({cols}) VALUES ({placeholders})", values)
        existing.add(mat)
        created += 1

    return jsonify({
        'success': True,
        'created': created,
        'skipped': skipped,
        'error_count': len(errors),
        'errors': errors[:50],
        'message': '导入完成：新增 %d 条，跳过 %d 条（重复），问题 %d 条。' % (created, skipped, len(errors)),
    })
