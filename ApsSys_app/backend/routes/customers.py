# -*- coding: utf-8 -*-
"""客户管理

客户编码规则：KH + 三位流水号（KH001, KH002, ...），作为业务主键。
新增时由后端自动生成，编辑时编码只读不可改。
模块加载时执行一次性迁移：把原 id 主键改为 code 主键，并把历史非 KH 编码重排为 KH 序列。
"""

from flask import Blueprint, request, jsonify, send_file

from ..database import query_db, execute_db
from ..utils import gen_id, serialize_rows
from ..errors import err_payload

bp = Blueprint('customers', __name__)


_MIGRATED = False


def _migrate():
    """一次性迁移：确保 code 为客户主键，且历史编码统一为 KH 序列。幂等。

    🔴【P2-26 修复 · 2026-10-14】原实现被 5 个**请求路径**直接调用
    （list/get/create/update/import），等于每次请求都跑一遍
    `SELECT ... ORDER BY id` + `ALTER TABLE ... DROP PRIMARY KEY, ADD PRIMARY KEY`：
      · 热路径重 DDL，每次都拿表级元数据锁、重建聚簇索引，客户列表/导入等接口被显著拖慢；
      · 并发请求下多个 ALTER 互相排队，偶发锁等待超时；
      · 迁移早已完成后仍在无意义地重复执行。
    现加两道闸：① 模块级幂等标志，进程内只执行一次；
    ② 即使执行，也先用 `SHOW KEYS` 检查主键是否已是 code，是则**完全跳过 DDL**。
    """
    global _MIGRATED
    if _MIGRATED:
        return
    try:
        # 1. 把任何非 KH 前缀（含 NULL）的编码重排为 KH+序号，保证 code 非空且唯一
        rows = query_db(
            "SELECT id FROM customers WHERE code IS NULL OR code = '' OR code NOT LIKE 'KH%' ORDER BY id"
        )
        for i, r in enumerate(rows, start=1):
            execute_db("UPDATE customers SET code=%s WHERE id=%s", (f"KH{i:03d}", r['id']))

        # 2. 把主键从 id 改为 code（已迁移则跳过 DDL）
        pk = query_db("SHOW KEYS FROM customers WHERE Key_name='PRIMARY'", one=True)
        if pk and pk.get('Column_name') == 'code':
            return
        execute_db("ALTER TABLE customers DROP PRIMARY KEY, ADD PRIMARY KEY (code)")
    except Exception as e:
        # 表不存在或已迁移时静默忽略，避免影响启动
        try:
            import logging
            logging.getLogger(__name__).warning("customers _migrate skipped: %s", e)
        except Exception:
            pass
    finally:
        _MIGRATED = True


def _next_code():
    """生成下一个客户编码 KH+3位流水号（在现有 KH 前缀编码中取最大值 +1）"""
    rows = query_db("SELECT code FROM customers WHERE code LIKE 'KH%'")
    max_n = 0
    for r in rows:
        code = (r.get('code') or '').strip()
        if code.startswith('KH'):
            try:
                n = int(code[2:])
                if n > max_n:
                    max_n = n
            except ValueError:
                pass
    return f"KH{max_n + 1:03d}"


@bp.route('/api/customers/next-code')
def customers_next_code():
    _migrate()
    return jsonify({'success': True, 'code': _next_code()})


@bp.route('/api/customers')
def customers_list():
    _migrate()
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(name LIKE %s OR code LIKE %s OR sap_kunnr LIKE %s)")
        args.extend([f'%{search}%'] * 3)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM customers WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM customers WHERE {where} ORDER BY code LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )
    return jsonify({'success': True, 'total': total, 'page': page, 'per_page': per_page, 'data': serialize_rows(rows)})


@bp.route('/api/customers', methods=['POST'])
def customers_create():
    _migrate()
    data = request.get_json() or {}
    name = (data.get('name') or '').strip()
    if not name:
        return jsonify({'success': False, 'message': '客户名称不能为空'}), 400

    code = (data.get('code') or '').strip() or _next_code()
    if not code.startswith('KH') or not code[2:].isdigit():
        return jsonify({'success': False, 'message': '客户编码格式应为 KH+3位流水号'}), 400

    exists = query_db("SELECT 1 FROM customers WHERE code=%s", (code,), one=True)
    if exists:
        return jsonify({'success': False, 'message': f'客户编码 {code} 已存在'}), 400

    cid = gen_id()
    execute_db(
        "INSERT INTO customers (id, name, code, contact_person, phone, priority, note, email, address, city) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (cid, name, code, data.get('contact_person'), data.get('phone'),
         data.get('priority'), data.get('note'), data.get('email'),
         data.get('address'), data.get('city'))
    )
    return jsonify({'success': True, 'code': code, 'message': '创建成功'})


@bp.route('/api/customers/<cid>', methods=['GET', 'PUT', 'DELETE'])
def customers_detail(cid):
    _migrate()
    if request.method == 'GET':
        row = query_db("SELECT * FROM customers WHERE code=%s", (cid,), one=True)
        if not row:
            return jsonify({'success': False, 'message': '未找到客户'}), 404
        return jsonify({'success': True, 'data': serialize_rows([row])[0]})

    if request.method == 'PUT':
        data = request.get_json() or {}
        sets, vals = [], []
        if 'name' in data:
            name = (data.get('name') or '').strip()
            if not name:
                return jsonify({'success': False, 'message': '客户名称不能为空'}), 400
            sets.append("name=%s")
            vals.append(name)
        for f in ['contact_person', 'phone', 'email', 'address', 'city', 'priority', 'note']:
            if f in data:
                sets.append(f"{f}=%s")
                vals.append(data.get(f))
        if not sets:
            return jsonify({'success': False, 'message': '无更新字段'}), 400
        sets.append("updated_at=NOW()")
        vals.append(cid)
        execute_db(f"UPDATE customers SET {', '.join(sets)} WHERE code=%s", vals)
        return jsonify({'success': True, 'message': '更新成功'})

    # DELETE
    execute_db("DELETE FROM customers WHERE code=%s", (cid,))
    return jsonify({'success': True, 'message': '删除成功'})


@bp.route('/api/customers/export-template')
def customers_export_template():
    """导出客户导入模板（表头 + 示例/说明行）。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '客户导入模板'
    headers = ['客户编码', '客户名称', '联系人', '电话', '优先级', '邮箱', '地址', '城市', '备注']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')
    ws.append(['KH001', '示例客户', '王五', '13700137000', 1, 'a@b.com', '某路1号', '某市', '示例'])
    ws.append(['（示例行，请按实际填写；客户编码留空则自动生成 KH+3位流水号）', '', '', '', '', '', '', '', ''])
    widths = [12, 18, 12, 16, 10, 20, 18, 10, 18]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w
    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='客户导入模板.xlsx',
                    mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


_CUS_COLMAP = {'客户编码': 'code', '客户名称': 'name', '联系人': 'contact_person',
               '电话': 'phone', '优先级': 'priority', '邮箱': 'email',
               '地址': 'address', '城市': 'city', '备注': 'note'}


@bp.route('/api/customers/import', methods=['POST'])
def customers_import():
    """批量导入客户（xlsx）。必需列：客户名称。编码重复跳过，留空自动生成。"""
    import openpyxl
    from io import BytesIO

    _migrate()
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
    idx = {cn: i for i, cn in enumerate(header) if cn in _CUS_COLMAP}
    if '客户名称' not in idx:
        return jsonify({'success': False, 'error': '表头缺少必需列：客户名称'}), 400

    existing = {r['code'] for r in query_db("SELECT code FROM customers")}

    def get_val(row, cn):
        i = idx.get(cn)
        return row[i] if (i is not None and i < len(row)) else None

    created = 0
    skipped = 0
    errors = []
    for ri, raw in enumerate(raw_rows[1:], start=2):
        name = str(get_val(raw, '客户名称') or '').strip()
        if not name:
            continue
        code = str(get_val(raw, '客户编码') or '').strip()
        if code:
            if not code.startswith('KH') or not code[2:].isdigit():
                errors.append({'row': ri, 'reason': '客户编码格式应为 KH+3位流水号：%s' % code}); continue
        else:
            code = _next_code()
        if code in existing:
            skipped += 1
            errors.append({'row': ri, 'reason': '客户编码 %s 已存在，跳过' % code}); continue
        prio = get_val(raw, '优先级')
        try:
            priority = int(prio) if prio not in (None, '') else 1
        except (ValueError, TypeError):
            priority = 1
        cid = gen_id()
        execute_db(
            "INSERT INTO customers (id, name, code, contact_person, phone, priority, note, email, address, city) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (cid, name, code, str(get_val(raw, '联系人') or '').strip(), str(get_val(raw, '电话') or '').strip(),
             priority, str(get_val(raw, '备注') or '').strip(), str(get_val(raw, '邮箱') or '').strip(),
             str(get_val(raw, '地址') or '').strip(), str(get_val(raw, '城市') or '').strip())
        )
        existing.add(code)
        created += 1

    return jsonify({'success': True, 'created': created, 'skipped': skipped,
                    'error_count': len(errors), 'errors': errors[:50],
                    'message': '导入完成：新增 %d 条，跳过 %d 条（重复），问题 %d 条。' % (created, skipped, len(errors))})
