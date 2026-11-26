# -*- coding: utf-8 -*-
"""BOM管理"""

from flask import Blueprint, request, jsonify

from ..database import query_db, execute_db, safe_fields
from ..utils import serialize_row, serialize_rows
from ..errors import err_payload

bp = Blueprint('bom', __name__)

# BOM的所有业务字段（不含bom_key）
BOM_FIELDS = ['parent_matnr', 'parent_maktx', 'werks', 'stlan', 'stlal', 'posnr',
              'comp_matnr', 'comp_maktx', 'comp_menge', 'comp_meins', 'postp', 'lgort',
              'ausch', 'sortf', 'yield_qty', 'data_source']


@bp.route('/api/bom')
def bom_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 50))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(parent_matnr LIKE %s OR comp_matnr LIKE %s OR parent_maktx LIKE %s OR comp_maktx LIKE %s)")
        args.extend([f'%{search}%'] * 4)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM bom WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM bom WHERE {where} ORDER BY parent_matnr, posnr LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )
    return jsonify({'success': True, 'total': total, 'page': page, 'per_page': per_page, 'data': serialize_rows(rows)})


@bp.route('/api/bom/<bom_key>', methods=['GET'])
def bom_get(bom_key):
    row = query_db("SELECT * FROM bom WHERE bom_key=%s", (bom_key,), one=True)
    if not row:
        return jsonify({'success': False, 'message': 'BOM不存在'}), 404
    return jsonify({'success': True, 'data': serialize_row(row)})


@bp.route('/api/bom', methods=['POST'])
def bom_create():
    data = request.get_json() or {}
    bom_key = data.get('bom_key')
    if not bom_key:
        bom_key = f"BOM-{data.get('parent_matnr','')}-{data.get('comp_matnr','')}-{data.get('werks','')}-{data.get('stlan','1')}-{data.get('stlal','1')}"
    data['bom_key'] = bom_key

    fields = ['bom_key'] + BOM_FIELDS
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]

    execute_db(
        f"INSERT INTO bom ({cols}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE " + ', '.join([f"{f}=VALUES({f})" for f in fields if f != 'bom_key']),
        values
    )
    return jsonify({'success': True, 'bom_key': bom_key, 'message': '创建成功'})


@bp.route('/api/bom/<bom_key>', methods=['PUT'])
def bom_update(bom_key):
    data = request.get_json() or {}
    sets = ', '.join([f"{f}=%s" for f in BOM_FIELDS])
    values = [data.get(f) for f in BOM_FIELDS] + [bom_key]
    execute_db(f"UPDATE bom SET {sets}, updated_at=NOW() WHERE bom_key=%s", values)
    return jsonify({'success': True, 'message': '更新成功'})


@bp.route('/api/bom/<bom_key>', methods=['DELETE'])
def bom_delete(bom_key):
    execute_db("DELETE FROM bom WHERE bom_key=%s", (bom_key,))
    return jsonify({'success': True, 'message': '删除成功'})


# ===================== 导入 / 导出模板 =====================
import openpyxl
from io import BytesIO
from flask import send_file
from openpyxl.styles import Font, PatternFill, Alignment
from openpyxl.utils import get_column_letter


@bp.route('/api/bom/export-template')
def bom_export_template():
    """导出 BOM 导入模板（表头 + 示例/说明行）。"""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = 'BOM导入模板'
    headers = ['父项物料编码', '父项物料描述', '工厂', 'BOM用途', 'BOM替代', '项目号',
               '子项物料编码', '子项物料描述', '组件数量', '组件单位', '项目类别', '库存地点', '报废率', '排序串', '产出数量']
    ws.append(headers)
    for c in ws[1]:
        c.fill = PatternFill('solid', fgColor='4472C4')
        c.font = Font(color='FFFFFF', bold=True)
        c.alignment = Alignment(horizontal='center')
    ws.append(['P001', '父项A', '1000', '1', '1', '0010', 'C001', '子项B', 2, 'KG', 'L', '0001', 0, '', 1])
    ws.append(['（示例行；必填：父项物料编码、子项物料编码；相同「父项+子项+工厂+用途+替代」组合将跳过）'] + [''] * 14)
    for i, w in enumerate([16, 16, 8, 8, 8, 8, 16, 16, 10, 8, 8, 10, 8, 8, 8], start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='BOM导入模板.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


_BOM_IMPORT_COLMAP = {
    '父项物料编码': 'parent_matnr', '父项物料描述': 'parent_maktx', '工厂': 'werks', 'BOM用途': 'stlan', 'BOM替代': 'stlal',
    '项目号': 'posnr', '子项物料编码': 'comp_matnr', '子项物料描述': 'comp_maktx', '组件数量': 'comp_menge',
    '组件单位': 'comp_meins', '项目类别': 'postp', '库存地点': 'lgort', '报废率': 'ausch', '排序串': 'sortf', '产出数量': 'yield_qty',
}


@bp.route('/api/bom/import', methods=['POST'])
def bom_import():
    """批量导入 BOM（xlsx）。重复组合（父项+子项+工厂+用途+替代）跳过。"""
    # 【P2-22 修复】走共享的安全读取：扩展名白名单 + 大小上限 + read_only 流式解析，
    # 避免恶意/损坏 xlsx（zip bomb）吃满内存 CPU 拖垮单进程服务。
    from ..utils import read_upload_excel
    f = request.files.get('file')
    wb, err = read_upload_excel(f)
    if err:
        return jsonify({'success': False, 'error': err}), 400

    ws = wb.active
    raw = list(ws.iter_rows(values_only=True))
    if not raw:
        return jsonify({'success': False, 'error': '文件为空'}), 400
    header = [str(h).strip() if h is not None else '' for h in raw[0]]
    idx = {cn: i for i, cn in enumerate(header) if cn in _BOM_IMPORT_COLMAP}
    for req in ('父项物料编码', '子项物料编码'):
        if req not in idx:
            return jsonify({'success': False, 'error': '表头缺少必需列：' + req}), 400

    existing = {r['bom_key'] for r in query_db("SELECT bom_key FROM bom WHERE bom_key IS NOT NULL")}

    def gv(row, cn):
        i = idx.get(cn)
        return row[i] if (i is not None and i < len(row)) else None

    def num(v):
        if v is None or v == '':
            return None
        try:
            return float(v)
        except (ValueError, TypeError):
            return None

    created = skipped = 0
    errors = []
    for ri, row in enumerate(raw[1:], start=2):
        pm = str(gv(row, '父项物料编码') or '').strip()
        cm = str(gv(row, '子项物料编码') or '').strip()
        if not pm and not cm:
            continue
        if not pm:
            errors.append({'row': ri, 'reason': '父项物料编码为空'}); continue
        if not cm:
            errors.append({'row': ri, 'reason': '子项物料编码为空'}); continue
        werks = str(gv(row, '工厂') or '').strip()
        stlan = str(gv(row, 'BOM用途') or '1').strip() or '1'
        stlal = str(gv(row, 'BOM替代') or '1').strip() or '1'
        bom_key = f"BOM-{pm}-{cm}-{werks}-{stlan}-{stlal}"
        if bom_key in existing:
            skipped += 1
            errors.append({'row': ri, 'reason': '组合已存在，跳过'}); continue
        data = {
            'bom_key': bom_key,
            'parent_matnr': pm,
            'parent_maktx': str(gv(row, '父项物料描述') or '').strip(),
            'werks': werks, 'stlan': stlan, 'stlal': stlal,
            'posnr': str(gv(row, '项目号') or '').strip(),
            'comp_matnr': cm,
            'comp_maktx': str(gv(row, '子项物料描述') or '').strip(),
            'comp_menge': num(gv(row, '组件数量')),
            'comp_meins': str(gv(row, '组件单位') or '').strip(),
            'postp': str(gv(row, '项目类别') or '').strip(),
            'lgort': str(gv(row, '库存地点') or '').strip(),
            'ausch': num(gv(row, '报废率')),
            'sortf': str(gv(row, '排序串') or '').strip(),
            'yield_qty': num(gv(row, '产出数量')),
            'data_source': 'import',
        }
        fields = safe_fields(data, 'bom')
        cols = ', '.join(fields)
        placeholders = ', '.join(['%s'] * len(fields))
        values = [data[k] for k in fields]
        execute_db(f"INSERT INTO bom ({cols}) VALUES ({placeholders})", values)
        existing.add(bom_key)
        created += 1
    return jsonify({
        'success': True,
        'created': created,
        'skipped': skipped,
        'error_count': len(errors),
        'errors': errors[:50],
        'message': '导入完成：新增 %d 条，跳过 %d 条（重复），问题 %d 条。' % (created, skipped, len(errors)),
    })
