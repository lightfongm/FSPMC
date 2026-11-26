# -*- coding: utf-8 -*-
"""采购入库管理

采购入库单号规则：CR + 四位流水号（CR0001, CR0002, ...），作为主键。
新增时由后端自动生成；供应商编码引用 suppliers.code，物料编码引用 products.material_code。
采购订单号（purchase_order_no）为引用原采购订单的自由文本字段，非空。
未到货数量按(采购订单号+物料编码)分组、逐笔计算，同一采购订单的同一物料才相互累计扣减，不同物料各自独立：
- 状态"在途"：某行"实际到货数量"为空(未填)时，按"计划到货日期"升序、用"计划到货数量"扣减；
             "实际到货数量"已填时，按"实际到货日期"升序、用"实际到货数量"扣减。
- 状态"关闭"：仅根据"实际到货数量"考虑未到货（不再使用计划到货数量扣减）；实际到货数量未填则视为 0。
- 状态"作废"：该行数据完全不参与未到货数量计算（其未到货数量置 0）。
每行未到货 = 该(采购订单号,物料)组订单总量(组内非作废行 order_qty 最大值) - 截至本笔(含)的扣减累计之和，保存时自动计算。
实际到货数量已填时，实际到货日期为必填。
采购入库列表默认按 采购订单号、物料编码、计划到货日期 排序。
"""

import logging
from flask import Blueprint, request, jsonify, send_file

from ..database import query_db, execute_db, transaction
from ..utils import serialize_rows, biz_today
from ..errors import err_payload

bp = Blueprint('purchase_orders', __name__)

# 状态枚举
VALID_STATUS = {'创建', '在途', '关闭', '作废'}


def _ensure_table():
    """确保 purchase_orders 表存在（应用启动时调用，幂等）"""
    execute_db("""
        CREATE TABLE IF NOT EXISTS purchase_orders (
            po_no VARCHAR(50) PRIMARY KEY COMMENT '采购入库单号 CR+4位流水号，主键',
            purchase_order_no VARCHAR(50) COMMENT '采购订单号（引用原采购订单，非空）',
            order_date DATE COMMENT '订单日期',
            supplier_code VARCHAR(50) COMMENT '供应商编码，引用 suppliers.code',
            supplier_name VARCHAR(200) COMMENT '供应商名称（冗余存储，便于列表展示）',
            material_code VARCHAR(50) COMMENT '物料编码，引用 products.material_code',
            material_name VARCHAR(200) COMMENT '物料名称（冗余存储）',
            order_qty DECIMAL(18,3) DEFAULT 0 COMMENT '订单数量',
            arrival_qty DECIMAL(18,3) DEFAULT 0 COMMENT '计划到货数量',
            actual_arrival_qty DECIMAL(18,3) DEFAULT NULL COMMENT '实际到货数量（未填则为 NULL）',
            actual_arrival_date DATE COMMENT '实际到货日期（实际到货数量已填时必填）',
            status VARCHAR(20) DEFAULT '创建' COMMENT '状态:创建/在途/关闭/作废',
            close_source VARCHAR(10) DEFAULT NULL COMMENT '关闭来源:auto自动关闭/manual手动关闭(手动关闭不自动重开)',
            unarrived_qty DECIMAL(18,3) DEFAULT 0 COMMENT '未到货数量（按采购订单号自动计算）',
            plan_arrival_date DATE COMMENT '计划到货日期',
            note VARCHAR(500) COMMENT '备注',
            updated_at DATETIME DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP
        ) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4
    """)


def _migrate():
    """存量表加列（幂等）：新增 purchase_order_no；po_no 前缀由 CG 切到 CR。"""
    # 加列（MySQL 无 IF NOT EXISTS，用 information_schema 判断后 ALTER）
    cols = query_db(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'purchase_orders'"
    )
    col_names = {r['COLUMN_NAME'] for r in cols}
    if 'purchase_order_no' not in col_names:
        execute_db("ALTER TABLE purchase_orders ADD COLUMN purchase_order_no VARCHAR(50) "
                   "COMMENT '采购订单号（引用原采购订单，非空）' AFTER po_no")
    if 'actual_arrival_qty' not in col_names:
        execute_db("ALTER TABLE purchase_orders ADD COLUMN actual_arrival_qty DECIMAL(18,3) DEFAULT NULL "
                   "COMMENT '实际到货数量' AFTER arrival_qty")
    if 'actual_arrival_date' not in col_names:
        execute_db("ALTER TABLE purchase_orders ADD COLUMN actual_arrival_date DATE "
                   "COMMENT '实际到货日期（实际到货数量已填时必填）' AFTER actual_arrival_qty")
    # 历史遗留：旧代码把"未填实际到货"存成 0，现规范为 NULL（0 视为已填，会强制要日期）。
    # 仅把"实际到货数量=0 且 实际到货日期为空"的存量行清成 NULL，避免误触发日期必填。
    # 历史遗留：旧规则允许实际到货数量=0，现规范为"不允许填0，未到货请留空"，
    # 故把存量 actual_arrival_qty=0 的行清成 NULL（并清掉可能伴随的日期）。
    execute_db("UPDATE purchase_orders SET actual_arrival_qty=NULL, actual_arrival_date=NULL "
               "WHERE actual_arrival_qty=0")
    if 'status' not in col_names:
        execute_db("ALTER TABLE purchase_orders ADD COLUMN status VARCHAR(20) DEFAULT '在途' "
                   "COMMENT '状态:在途/关闭/作废' AFTER actual_arrival_qty")
    if 'close_source' not in col_names:
        execute_db("ALTER TABLE purchase_orders ADD COLUMN close_source VARCHAR(10) DEFAULT NULL "
                   "COMMENT '关闭来源:auto自动关闭/manual手动关闭(手动关闭不自动重开)' AFTER status")
    # 历史 CG 前缀数据迁到 CR 前缀（表通常为空，仅作保险）
    if 'po_no' in col_names:
        rows = query_db("SELECT po_no FROM purchase_orders WHERE po_no LIKE 'CG%'")
        for r in rows:
            old = r['po_no']
            new = 'CR' + old[2:]
            # 仅当新编码不冲突时迁移
            if not query_db("SELECT 1 FROM purchase_orders WHERE po_no=%s", (new,), one=True):
                execute_db("UPDATE purchase_orders SET po_no=%s WHERE po_no=%s", (new, old))


    # 存量"关闭"行视为手动关闭（锁定，不参与自动重开）；其它状态 close_source 为 NULL
    execute_db("UPDATE purchase_orders SET close_source='manual' "
               "WHERE status='关闭' AND (close_source IS NULL OR close_source='')")

    # 状态"可用"重命名为"在途"（语义：采购已下达、货在途）；幂等
    execute_db("UPDATE purchase_orders SET status='在途' WHERE status='可用'")
    # 列默认值同步为"在途"（仅当当前默认仍是"可用"时才 ALTER，避免每次启动都改元数据）
    col_def = query_db(
        "SELECT COLUMN_DEFAULT FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA=DATABASE() AND TABLE_NAME='purchase_orders' AND COLUMN_NAME='status'",
        one=True)
    if col_def and (col_def.get('COLUMN_DEFAULT') or '') != '创建':
        execute_db("ALTER TABLE purchase_orders ALTER status SET DEFAULT '创建'")

def _next_code():
    """生成下一个采购入库单号 CR+4位流水号（在现有 CR 前缀编码中取最大值 +1）"""
    rows = query_db("SELECT po_no FROM purchase_orders WHERE po_no LIKE 'CR%'")
    max_n = 0
    for r in rows:
        code = (r.get('po_no') or '').strip()
        if code.startswith('CR'):
            try:
                n = int(code[2:])
                if n > max_n:
                    max_n = n
            except ValueError:
                pass
    return f"CR{max_n + 1:04d}"


def _lookup_names(supplier_code, material_code, supplier_name_fb='', material_name_fb=''):
    """根据编码回填供应商名称 / 物料名称（以数据库为准；主表查不到时回退提交值）"""
    supplier_name = supplier_name_fb or ''
    material_name = material_name_fb or ''
    if supplier_code:
        s = query_db("SELECT supplier_name FROM suppliers WHERE supplier_code=%s", (supplier_code,), one=True)
        if s:
            supplier_name = s.get('supplier_name') or supplier_name_fb or ''
    if material_code:
        m = query_db("SELECT name FROM products WHERE material_code=%s", (material_code,), one=True)
        if m:
            material_name = m.get('name') or material_name_fb or ''
    return supplier_name, material_name


def _to_number(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return None


def _is_filled(v):
    """判断"实际到货数量"是否已填：None/空串/非数字/0 均视为未填。"""
    if v is None:
        return False
    if isinstance(v, str):
        v = v.strip()
        if not v:
            return False
        try:
            return float(v) != 0
        except (TypeError, ValueError):
            return False
    return True


def _fmt_date(v):
    """将库内 DATE / 字符串统一格式化为 'YYYY-MM-DD'，空值返回 ''（用于导出）。"""
    if v is None:
        return ''
    if hasattr(v, 'isoformat'):
        return v.isoformat()[:10]
    return str(v).strip()


def _norm_date(v):
    """将 xlsx 单元格（datetime / 字符串 / 空）规范为 'YYYY-MM-DD'，空值返回 ''（用于导入）。"""
    if v is None or v == '':
        return ''
    if hasattr(v, 'isoformat'):
        return v.isoformat()[:10]
    return str(v).strip()


def _recalc_unarrived(purchase_order_no):
    """按(采购订单号 + 物料编码)分组逐笔重算未到货数量。

    同一采购订单的同一物料才相互累计扣减；不同物料各自独立计算。
    状态决定扣减来源：
      - "作废"：该行完全不参与（不入组、其 unarrived_qty 置 0）
      - "关闭"：仅用"实际到货数量"扣减（未填则视为 0，不扣计划到货数量）；
                排序按"实际到货日期"（未填时回退计划到货日期，仅影响顺序不影响扣减）
      - "在途"：实际到货数量未填(NULL)→用"计划到货数量"、按计划到货日期升序；
                实际到货数量已填(非 NULL)→用"实际到货数量"、按实际到货日期升序
    每行未到货 = 该(采购订单号,物料)组订单总量(组内非作废行 order_qty 最大值)
                - 截至本笔(含)的扣减累计之和
    """
    if not purchase_order_no:
        return 0.0
    rows = query_db(
        "SELECT po_no, material_code, order_qty, arrival_qty, actual_arrival_qty, "
        "status, plan_arrival_date, actual_arrival_date "
        "FROM purchase_orders WHERE purchase_order_no=%s",
        (purchase_order_no,)
    )
    if not rows:
        return 0.0

    # 作废行：不参与计算，未到货数量直接置 0
    for r in rows:
        if (r.get('status') or '在途') == '作废':
            execute_db(
                "UPDATE purchase_orders SET unarrived_qty=0 WHERE po_no=%s",
                (r['po_no'],)
            )

    # 按物料分组（排除作废行）
    groups = {}
    for r in rows:
        if (r.get('status') or '在途') == '作废':
            continue
        groups.setdefault(r['material_code'], []).append(r)

    last = 0.0
    for mat, grp in groups.items():
        # 组内排序：已填实际到货→按实际到货日期；否则按计划到货日期；(NULL 排最后)
        def _sort_key(r):
            filled = _is_filled(r['actual_arrival_qty'])
            key = r['actual_arrival_date'] if filled else r['plan_arrival_date']
            return (key is None, key or '', r['po_no'])
        grp.sort(key=_sort_key)
        po_total = max(float(r['order_qty']) for r in grp)
        cum = 0.0
        for r in grp:
            st = (r.get('status') or '在途')
            if st == '关闭':
                # 仅根据实际到货数量；未填则视为 0（不再使用计划到货数量）
                ded = float(r['actual_arrival_qty']) if _is_filled(r['actual_arrival_qty']) else 0.0
            elif _is_filled(r['actual_arrival_qty']):
                ded = float(r['actual_arrival_qty'])
            else:
                ded = float(r['arrival_qty'] or 0)
            cum += ded
            unarrived = po_total - cum
            execute_db(
                "UPDATE purchase_orders SET unarrived_qty=%s WHERE po_no=%s",
                (unarrived, r['po_no'])
            )
            last = unarrived
    return last


def _auto_close(purchase_order_no):
    """保存"实际到货数量"后，按两步处理同(采购订单号 + 物料)组的关闭逻辑：

    第一步（本行关闭）：只要某行已填"实际到货数量"(非 NULL/0)，该行立即关闭
        (close_source='auto')——即"保存实际到货数量后本行状态改为关闭"。
    第二步（全部关闭）：汇总组内"实际入库数量"之和，当 和 >= 该组订单数量（到齐，含超收）
        时，把组内其余尚未关闭的"在途"行也自动改为"关闭"(close_source='auto')。

    - 作废行不参与汇总（与未到货计算口径一致：作废=无效到货）。
    - 不应关闭的行（既无实际到货数量、组又未到齐）中，仅"自动关闭"的行重开为"在途"；
      close_source='manual'(手动关闭) → 永不自动重开（用户明确锁定）。
    - 状态变更后再按新口径（关闭仅看实际到货数量）重算一次未到货。
    """
    if not purchase_order_no:
        return
    rows = query_db(
        "SELECT po_no, material_code, order_qty, actual_arrival_qty, status, close_source "
        "FROM purchase_orders WHERE purchase_order_no=%s",
        (purchase_order_no,)
    )
    if not rows:
        return
    groups = {}
    for r in rows:
        if (r.get('status') or '在途') == '作废':
            continue  # 作废不参与汇总
        groups.setdefault(r['material_code'], []).append(r)
    changed = False
    for mat, grp in groups.items():
        po_total = max(float(r['order_qty']) for r in grp)
        total_actual = sum(float(r['actual_arrival_qty']) for r in grp if _is_filled(r['actual_arrival_qty']))
        fully = (po_total > 0 and total_actual >= po_total)  # 同组实际之和 >= 订单数量 → 到齐
        for r in grp:
            has_actual = _is_filled(r['actual_arrival_qty'])
            # 是否应当"关闭"：
            #   1) 本行已填实际到货数量 → 立即关闭（保存实际到货数量即视为该收货行已处理）；
            #   2) 同(采购订单+物料)组到齐（实际之和 >= 订单数量）→ 全部关闭。
            should_close = has_actual or fully
            cur_status = r.get('status') or '在途'
            cur_src = r.get('close_source') or ''
            if should_close:
                if cur_status != '关闭':
                    # 系统触发关闭（填实际到货 / 到齐），标记 auto，以便清空实际到货后可自动重开
                    execute_db("UPDATE purchase_orders SET status='关闭', close_source='auto' WHERE po_no=%s",
                               (r['po_no'],))
                    changed = True
            else:
                # 不应关闭：仅"自动关闭"的行需重开为在途；手动关闭(close_source='manual')锁定不动
                if cur_status == '关闭' and cur_src == 'auto':
                    execute_db("UPDATE purchase_orders SET status='在途', close_source=NULL WHERE po_no=%s",
                               (r['po_no'],))
                    changed = True
    if changed:
        # 状态变更后，按新口径（关闭仅看实际到货数量）重算未到货
        _recalc_unarrived(purchase_order_no)


def _sync_group_order_qty(po_no, new_pon, new_mat, new_qty):
    """同(采购订单号+物料)组内订单数量一致性：把同组其他行的 order_qty 同步为本行新值。

    仅当目标(采购订单号,物料)有效时生效；若本次同时改了采购订单号/物料编码，
    则同步目标新组；若新组尚无其它行，则无影响（新组自然以本行新值为准）。
    """
    cur = query_db("SELECT purchase_order_no, material_code FROM purchase_orders WHERE po_no=%s", (po_no,), one=True)
    if not cur:
        return
    pon = (new_pon or '').strip() or cur['purchase_order_no']
    mat = (new_mat or '').strip() or cur['material_code']
    if not pon or not mat:
        return
    execute_db(
        "UPDATE purchase_orders SET order_qty=%s "
        "WHERE purchase_order_no=%s AND material_code=%s AND po_no<>%s",
        (new_qty, pon, mat, po_no)
    )


@bp.route('/api/purchase-orders/distinct-orders')
def po_distinct_orders():
    """返回未关闭的已有采购订单号及该订单的订单日期、供应商信息，供前端下拉选择。"""
    _ensure_table()
    rows = query_db(
        "SELECT purchase_order_no, MAX(order_date) AS order_date, "
        "MAX(supplier_code) AS supplier_code, MAX(supplier_name) AS supplier_name "
        "FROM purchase_orders "
        "WHERE status <> '关闭' AND purchase_order_no IS NOT NULL AND purchase_order_no <> '' "
        "GROUP BY purchase_order_no ORDER BY purchase_order_no"
    )
    data = []
    for r in rows:
        od = r['order_date']
        od_str = od.isoformat() if hasattr(od, 'isoformat') else (str(od) if od else '')
        data.append({
            'purchase_order_no': r['purchase_order_no'],
            'order_date': od_str,
            'supplier_code': r['supplier_code'] or '',
            'supplier_name': r['supplier_name'] or '',
        })
    return jsonify({'success': True, 'data': data})


@bp.route('/api/purchase-orders/materials')
def po_materials():
    """返回某采购订单号下已有物料（含该组订单数量），供选物料后自动带出订单数量。"""
    _ensure_table()
    pon = (request.args.get('purchase_order_no') or '').strip()
    if not pon:
        return jsonify({'success': True, 'data': []})
    rows = query_db(
        "SELECT DISTINCT material_code, material_name, "
        "(SELECT MAX(order_qty) FROM purchase_orders p2 "
        " WHERE p2.purchase_order_no=%s AND p2.material_code=p1.material_code) AS order_qty "
        "FROM purchase_orders p1 WHERE purchase_order_no=%s "
        "AND material_code IS NOT NULL AND material_code <> '' "
        "ORDER BY material_code",
        (pon, pon)
    )
    return jsonify({'success': True, 'data': [
        {'material_code': r['material_code'], 'material_name': r['material_name'],
         'order_qty': float(r['order_qty']) if r['order_qty'] is not None else None}
        for r in rows]})


@bp.route('/api/purchase-orders/next-code')
def po_next_code():
    _ensure_table()
    return jsonify({'success': True, 'code': _next_code()})


@bp.route('/api/purchase-orders')
def po_list():
    _ensure_table()
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(po_no LIKE %s OR purchase_order_no LIKE %s OR supplier_code LIKE %s "
                          "OR supplier_name LIKE %s OR material_code LIKE %s OR material_name LIKE %s)")
        args.extend([f'%{search}%'] * 6)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM purchase_orders WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM purchase_orders WHERE {where} "
        f"ORDER BY purchase_order_no, material_code, "
        f"(CASE WHEN actual_arrival_qty IS NOT NULL "
        f"THEN COALESCE(actual_arrival_date,'9999-12-31') "
        f"ELSE COALESCE(plan_arrival_date,'9999-12-31') END), po_no "
        f"LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )
    return jsonify({'success': True, 'total': total, 'page': page,
                    'per_page': per_page, 'data': serialize_rows(rows)})


@bp.route('/api/purchase-orders', methods=['POST'])
def po_create():
    _ensure_table()
    data = request.get_json() or {}

    purchase_order_no = (data.get('purchase_order_no') or '').strip()
    order_date = (data.get('order_date') or '').strip()
    supplier_code = (data.get('supplier_code') or '').strip()
    material_code = (data.get('material_code') or '').strip()
    plan_arrival_date = (data.get('plan_arrival_date') or '').strip()

    if not purchase_order_no:
        return jsonify({'success': False, 'message': '采购订单号不能为空'}), 400
    if not order_date:
        return jsonify({'success': False, 'message': '订单日期不能为空'}), 400
    if not supplier_code:
        return jsonify({'success': False, 'message': '供应商编码不能为空'}), 400
    if not material_code:
        return jsonify({'success': False, 'message': '物料编码不能为空'}), 400
    if not plan_arrival_date:
        return jsonify({'success': False, 'message': '计划到货日期不能为空'}), 400

    order_qty = _to_number(data.get('order_qty'))
    arrival_qty = _to_number(data.get('arrival_qty'))
    if order_qty is None:
        return jsonify({'success': False, 'message': '订单数量必须为数字'}), 400
    if arrival_qty is None:
        return jsonify({'success': False, 'message': '计划到货数量必须为数字'}), 400

    raw_actual = data.get('actual_arrival_qty')
    actual_arrival_qty = None  # 未填 / 填0 均视作"空"(NULL)
    if raw_actual not in (None, ''):
        actual_arrival_qty = _to_number(raw_actual)
        if actual_arrival_qty is None:
            return jsonify({'success': False, 'message': '实际到货数量必须为数字'}), 400
        if actual_arrival_qty == 0:
            actual_arrival_qty = None  # 填0 按空处理（默认为空）
    raw_actual_date = (data.get('actual_arrival_date') or '').strip()
    if actual_arrival_qty is None:
        raw_actual_date = ''  # 实际为空时，连带清空实际到货日期
    # 实际到货数量已填 → 实际到货日期必填
    if _is_filled(actual_arrival_qty) and not raw_actual_date:
        return jsonify({'success': False, 'message': '实际到货数量已填，实际到货日期不能为空'}), 400

    raw_status = (data.get('status') or '创建').strip()
    if raw_status not in VALID_STATUS:
        return jsonify({'success': False, 'message': '状态必须为：创建/在途/关闭/作废'}), 400

    po_no = (data.get('po_no') or '').strip() or _next_code()
    if not po_no.startswith('CR') or not po_no[2:].isdigit():
        return jsonify({'success': False, 'message': '采购入库单号格式应为 CR+4位流水号'}), 400
    if query_db("SELECT 1 FROM purchase_orders WHERE po_no=%s", (po_no,), one=True):
        return jsonify({'success': False, 'message': f'采购入库单号 {po_no} 已存在'}), 400

    supplier_name, material_name = _lookup_names(
        supplier_code, material_code,
        data.get('supplier_name') or '', data.get('material_name') or '')
    execute_db(
        "INSERT INTO purchase_orders "
        "(po_no, purchase_order_no, order_date, supplier_code, supplier_name, material_code, material_name, "
        "order_qty, arrival_qty, actual_arrival_qty, actual_arrival_date, status, unarrived_qty, plan_arrival_date, note) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (po_no, purchase_order_no, order_date, supplier_code, supplier_name, material_code, material_name,
         order_qty, arrival_qty, actual_arrival_qty, raw_actual_date or None, raw_status, 0, plan_arrival_date, data.get('note'))
    )
    unarrived = _recalc_unarrived(purchase_order_no)
    _auto_close(purchase_order_no)
    return jsonify({'success': True, 'po_no': po_no, 'unarrived_qty': unarrived, 'message': '创建成功'})


def _create_from_shortage_item(data):
    """根据单条缺料数据生成一张采购入库单（缺料明细 → 采购入库）。

    返回 (po_no, purchase_order_no, error_message)：成功时 error_message 为 None，
    失败时 po_no/purchase_order_no 为 None、error_message 为原因字符串。
    data 字段：material_code, material_name, order_qty, arrival_qty,
              plan_arrival_date, supplier_code, supplier_name
    对应关系：订单日期=今天、物料=缺料子件、订单数量=建议采购、
             计划到货日期=子件需求日期、状态=创建（采购订单号自动生成唯一申请号）。
    """
    material_code = (data.get('material_code') or '').strip()
    material_name = (data.get('material_name') or '').strip()
    plan_arrival_date = (data.get('plan_arrival_date') or '').strip()
    order_qty = _to_number(data.get('order_qty'))

    if not material_code:
        return None, None, '物料编码不能为空'
    if not plan_arrival_date:
        return None, None, '计划到货日期(子件需求日期)不能为空'
    if order_qty is None or order_qty <= 0:
        return None, None, '建议采购数量必须为正数'

    # 防重：同一缺料行(子件编码 + 子件需求日期)已生成过采购入库单则不再生成
    # （与缺料明细 po_generated 置灰口径一致，兼容 'YYYYMMDD' / 'YYYY-MM-DD' 两种日期写法）
    dup = query_db(
        "SELECT po_no FROM purchase_orders WHERE note='缺料明细生成' AND material_code=%s "
        "AND LEFT(REPLACE(CAST(plan_arrival_date AS CHAR),'-',''),8)=LEFT(REPLACE(%s,'-',''),8) LIMIT 1",
        (material_code, plan_arrival_date), one=True
    )
    if dup:
        return None, None, '该缺料行已生成采购入库单(%s)，无需重复生成' % dup.get('po_no')

    # 自动字段
    order_date = biz_today().strftime('%Y-%m-%d')
    status = '创建'
    po_no = _next_code()
    # 采购订单号：缺料生成无原采购订单，生成唯一申请号便于后续追踪
    purchase_order_no = f"SHORT-{po_no}"

    # 供应商可选（缺料生成时通常尚未选定供应商，留空由用户后续补全）
    supplier_code = (data.get('supplier_code') or '').strip()
    supplier_name = (data.get('supplier_name') or '').strip()
    if supplier_code:
        supplier_name, _ = _lookup_names(supplier_code, None, supplier_name, '')
    # 物料名称优先用传入的缺料子件名称，缺失时回查 products
    if not material_name:
        _, material_name = _lookup_names(None, material_code, '', '')
    # 计划到货数量默认等于订单数量
    arrival_qty = _to_number(data.get('arrival_qty'))
    if arrival_qty is None:
        arrival_qty = order_qty

    execute_db(
        "INSERT INTO purchase_orders "
        "(po_no, purchase_order_no, order_date, supplier_code, supplier_name, material_code, material_name, "
        "order_qty, arrival_qty, actual_arrival_qty, actual_arrival_date, status, unarrived_qty, plan_arrival_date, note) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
        (po_no, purchase_order_no, order_date, supplier_code, supplier_name, material_code, material_name,
         order_qty, arrival_qty, None, None, status, 0, plan_arrival_date, '缺料明细生成')
    )
    _recalc_unarrived(purchase_order_no)
    _auto_close(purchase_order_no)
    return po_no, purchase_order_no, None


@bp.route('/api/purchase-orders/from-shortage', methods=['POST'])
def po_create_from_shortage():
    """缺料明细单条生成采购入库单（接口保持兼容）。"""
    _ensure_table()
    data = request.get_json(silent=True) or {}
    po_no, purchase_order_no, err = _create_from_shortage_item(data)
    if err:
        return jsonify({'success': False, 'message': err}), 400
    unarrived = _recalc_unarrived(purchase_order_no)
    return jsonify({'success': True, 'po_no': po_no, 'unarrived_qty': unarrived, 'message': '已生成采购入库单'})


@bp.route('/api/purchase-orders/from-shortage/batch', methods=['POST'])
def po_create_from_shortage_batch():
    """缺料明细一键批量生成采购入库单。

    请求体：{ "items": [ {material_code, material_name, order_qty, arrival_qty, plan_arrival_date}, ... ] }
    逐条调用 _create_from_shortage_item；任一条失败（含异常）不影响其他行（独立插入、立即提交）。
    返回：created(成功列表，含 po_no/物料/数量)、skipped(跳过列表，含原因)、汇总 message。
    """
    _ensure_table()
    data = request.get_json(silent=True) or {}
    items = data.get('items') or []
    if not isinstance(items, list) or len(items) == 0:
        return jsonify({'success': False, 'message': '未提供任何待生成行'}), 400

    created, skipped = [], []
    for idx, item in enumerate(items):
        try:
            po_no, purchase_order_no, err = _create_from_shortage_item(item)
        except Exception as e:
            # 【P2-20】异常摘要统一经 err_payload（仅 DEBUG 附详情），
            # 避免把 pymysql 原始异常串（含 SQL 片段/列名）原样回传前端。
            skipped.append({
                'index': idx,
                'material_code': (item.get('material_code') or '').strip(),
                'reason': '生成异常: ' + (err_payload(e).get('error') or '')
            })
            continue
        if err:
            skipped.append({
                'index': idx,
                'material_code': (item.get('material_code') or '').strip(),
                'reason': err
            })
        else:
            created.append({
                'index': idx,
                'po_no': po_no,
                'purchase_order_no': purchase_order_no,
                'material_code': (item.get('material_code') or '').strip(),
                'order_qty': _to_number(item.get('order_qty')) or 0
            })
    return jsonify({
        'success': True,
        'created': created,
        'skipped': skipped,
        'created_count': len(created),
        'skipped_count': len(skipped),
        'message': '已生成 %d 张采购入库单' % len(created) + (f'，跳过 {len(skipped)} 行' if skipped else '')
    })


@bp.route('/api/purchase-orders/<po_no>', methods=['GET', 'PUT', 'DELETE'])
def po_detail(po_no):
    _ensure_table()
    if request.method == 'GET':
        row = query_db("SELECT * FROM purchase_orders WHERE po_no=%s", (po_no,), one=True)
        if not row:
            return jsonify({'success': False, 'message': '未找到采购入库'}), 404
        return jsonify({'success': True, 'data': serialize_rows([row])[0]})

    if request.method == 'PUT':
        data = request.get_json() or {}
        sets, vals = [], []
        recalc_target = None  # 需要重算未到货的采购订单号
        trigger_auto_close = False  # 是否因"实际到货数量"变更触发自动关闭
        # 取当前行，用于校验"实际到货数量已填 → 实际到货日期必填"
        cur = query_db(
            "SELECT actual_arrival_qty, actual_arrival_date, purchase_order_no, material_code "
            "FROM purchase_orders WHERE po_no=%s", (po_no,), one=True
        )
        if not cur:
            return jsonify({'success': False, 'message': '未找到采购入库'}), 404

        if 'purchase_order_no' in data:
            pon = (data.get('purchase_order_no') or '').strip()
            if not pon:
                return jsonify({'success': False, 'message': '采购订单号不能为空'}), 400
            sets.append("purchase_order_no=%s")
            vals.append(pon)
            recalc_target = pon

        if 'order_date' in data:
            if not (data.get('order_date') or '').strip():
                return jsonify({'success': False, 'message': '订单日期不能为空'}), 400
            sets.append("order_date=%s")
            vals.append(data.get('order_date'))

        if 'supplier_code' in data:
            if not (data.get('supplier_code') or '').strip():
                return jsonify({'success': False, 'message': '供应商编码不能为空'}), 400
            sets.append("supplier_code=%s")
            vals.append(data.get('supplier_code'))
            sn, _ = _lookup_names(data.get('supplier_code'), None, data.get('supplier_name') or '', '')
            sets.append("supplier_name=%s")
            vals.append(sn)

        if 'material_code' in data:
            if not (data.get('material_code') or '').strip():
                return jsonify({'success': False, 'message': '物料编码不能为空'}), 400
            sets.append("material_code=%s")
            vals.append(data.get('material_code'))
            _, mn = _lookup_names(None, data.get('material_code'), '', data.get('material_name') or '')
            sets.append("material_name=%s")
            vals.append(mn)
            # 物料变动会使该行在同采购订单号下切换分组，需重算
            recalc_target = recalc_target or _current_pon(po_no)

        for f in ['order_qty', 'arrival_qty']:
            if f in data:
                v = _to_number(data.get(f))
                if v is None:
                    return jsonify({'success': False, 'message': f'{f} 必须为数字'}), 400
                sets.append(f"{f}=%s")
                vals.append(v)
                recalc_target = recalc_target or _current_pon(po_no)
                if f == 'order_qty':
                    # 同(采购订单号+物料)组内订单数量一致性：同步同组其他行
                    new_pon = data.get('purchase_order_no') if 'purchase_order_no' in data else None
                    new_mat = data.get('material_code') if 'material_code' in data else None
                    _sync_group_order_qty(po_no, new_pon, new_mat, v)
                    trigger_auto_close = True

        if 'actual_arrival_qty' in data:
            av_raw = data.get('actual_arrival_qty')
            av = None  # 清空 → NULL
            if av_raw not in (None, ''):
                av = _to_number(av_raw)
                if av is None:
                    return jsonify({'success': False, 'message': '实际到货数量必须为数字'}), 400
                if av == 0:
                    av = None  # 填0 按空处理（默认为空）
            sets.append("actual_arrival_qty=%s")
            vals.append(av)
            if av is None:
                # 清空实际到货数量时，连带清空实际到货日期，避免"有日期无数量"的不一致
                sets.append("actual_arrival_date=%s")
                vals.append(None)
                data.pop('actual_arrival_date', None)
            recalc_target = recalc_target or _current_pon(po_no)
            trigger_auto_close = True

        if 'actual_arrival_date' in data:
            ad = (data.get('actual_arrival_date') or '').strip()
            sets.append("actual_arrival_date=%s")
            vals.append(ad or None)
            recalc_target = recalc_target or _current_pon(po_no)

        if 'status' in data:
            st = (data.get('status') or '').strip()
            if st not in VALID_STATUS:
                return jsonify({'success': False, 'message': '状态必须为：创建/在途/关闭/作废'}), 400
            sets.append("status=%s")
            vals.append(st)
            # 关闭来源标记：手动改为"关闭"→ manual（锁定，不自动重开）；
            # 改为"在途/作废"→ 清 NULL（重新参与自动逻辑）
            if st == '关闭':
                sets.append("close_source=%s")
                vals.append('manual')
            else:
                sets.append("close_source=%s")
                vals.append(None)
            # 状态变更会影响未到货计算口径（关闭/作废），需触发重算
            recalc_target = recalc_target or _current_pon(po_no)

        if 'plan_arrival_date' in data:
            if not (data.get('plan_arrival_date') or '').strip():
                return jsonify({'success': False, 'message': '计划到货日期不能为空'}), 400
            sets.append("plan_arrival_date=%s")
            vals.append(data.get('plan_arrival_date'))

        if 'note' in data:
            sets.append("note=%s")
            vals.append(data.get('note'))

        # 实际到货数量已填 → 实际到货日期必填（取本次更新后的值）
        eff_actual = data.get('actual_arrival_qty') if 'actual_arrival_qty' in data else cur['actual_arrival_qty']
        if _is_filled(eff_actual):
            eff_date = data.get('actual_arrival_date') if 'actual_arrival_date' in data else cur['actual_arrival_date']
            # eff_date 可能为字符串或 datetime.date（从库直取）；仅当为空/空串时才拦截
            eff_date_empty = eff_date is None or (isinstance(eff_date, str) and not eff_date.strip())
            if eff_date_empty:
                return jsonify({'success': False, 'message': '实际到货数量已填，实际到货日期不能为空'}), 400

        if not sets:
            return jsonify({'success': False, 'message': '无更新字段'}), 400

        sets.append("updated_at=NOW()")
        vals.append(po_no)
        # 【P2-29 修复 · 2026-10-14】"改单 → 重算未到货 → 自动关闭"是一组多步写：
        # 原实现每步独立提交，UPDATE 成功而重算失败 → 未到货数量与状态口径分裂
        # （界面显示的数量与实际库存口径不一致）。现整体包进事务。
        with transaction():
            execute_db(f"UPDATE purchase_orders SET {', '.join(sets)} WHERE po_no=%s", vals)

            # 若改动了会影响未到货的字段，重算（含改采购订单号：旧号+新号都要重算）
            if recalc_target:
                _recalc_unarrived(recalc_target)
                if trigger_auto_close:
                    _auto_close(recalc_target)
            # 改采购订单号时，用更新前的旧号重算（cur 取到的是更新前的值）
            old_pon = (cur.get('purchase_order_no') or '').strip()
            if old_pon and old_pon != recalc_target:
                _recalc_unarrived(old_pon)
                if trigger_auto_close:
                    _auto_close(old_pon)

        return jsonify({'success': True, 'message': '更新成功'})

    # DELETE
    # 【P2-29】同上：删除与重算包在一个事务里，避免"单已删、未到货量未重算"。
    old_pon = _current_pon(po_no)
    with transaction():
        execute_db("DELETE FROM purchase_orders WHERE po_no=%s", (po_no,))
        if old_pon:
            _recalc_unarrived(old_pon)
    return jsonify({'success': True, 'message': '删除成功'})


def _current_pon(po_no):
    """取某采购入库单当前的采购订单号（用于重算判定）"""
    row = query_db("SELECT purchase_order_no FROM purchase_orders WHERE po_no=%s", (po_no,), one=True)
    return (row.get('purchase_order_no') or '').strip() if row else ''


_PO_IMPORT_COLMAP = {
    '采购订单号': 'purchase_order_no', '订单日期': 'order_date', '供应商编码': 'supplier_code',
    '供应商名称': 'supplier_name', '物料编码': 'material_code', '物料名称': 'material_name',
    '订单数量': 'order_qty', '计划到货数量': 'arrival_qty', '计划到货日期': 'plan_arrival_date',
    '实际到货数量': 'actual_arrival_qty', '实际到货日期': 'actual_arrival_date', '状态': 'status',
    '备注': 'note',
}


@bp.route('/api/purchase-orders/export-template')
def po_export_template():
    """导出采购入库导入模板（表头 + 示例/说明行），供批量填写后导入。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '采购入库导入模板'
    headers = ['采购订单号', '订单日期', '供应商编码', '供应商名称', '物料编码', '物料名称',
               '订单数量', '计划到货数量', '计划到货日期', '实际到货数量', '实际到货日期', '状态', '备注']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')

    ws.append(['PO2026-001', '2026-08-13', 'S001', '示例供应商', 'M001', '示例原料A',
               100, 100, '2026-08-20', '', '', '创建', ''])
    ws.append(['（采购入库单号/未到货数量由系统生成无需填写；实际到货数量留空=未到货；状态可选：创建/在途/关闭/作废）',
               '', '', '', '', '', '', '', '', '', '', '', ''])

    widths = [14, 12, 12, 14, 12, 14, 10, 12, 12, 12, 12, 8, 16]
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='采购入库导入模板.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


@bp.route('/api/purchase-orders/import', methods=['POST'])
def po_import():
    """批量导入采购入库（xlsx）。

    必需列：采购订单号/订单日期/供应商编码/物料编码/计划到货日期；订单数量/计划到货数量须为数字。
    重复判定：相同"采购订单号+物料编码+计划到货日期"的行跳过（不覆盖已有数据）。
    实际到货数量空/0 视作未到货(NULL)；已填且实际到货日期空 → 填当天（与前端一致）。
    导入后按采购订单号重算未到货数量并触发自动关闭。
    """
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
    idx = {cn: i for i, cn in enumerate(header) if cn in _PO_IMPORT_COLMAP}
    for req_col in ('采购订单号', '订单日期', '供应商编码', '物料编码', '计划到货日期'):
        if req_col not in idx:
            return jsonify({'success': False, 'error': '表头缺少必需列：' + req_col}), 400

    exist_rows = query_db(
        "SELECT purchase_order_no, material_code, plan_arrival_date FROM purchase_orders")
    existing_keys = set()
    for r in exist_rows:
        pon = (r.get('purchase_order_no') or '').strip()
        mat = (r.get('material_code') or '').strip()
        pad = _fmt_date(r.get('plan_arrival_date'))
        existing_keys.add((pon, mat, pad))

    def get_val(row, cn):
        i = idx.get(cn)
        return row[i] if (i is not None and i < len(row)) else None

    created = 0
    skipped = 0
    errors = []
    touched_pons = set()

    for ri, raw in enumerate(raw_rows[1:], start=2):
        pon = str(get_val(raw, '采购订单号') or '').strip()
        order_date = _norm_date(get_val(raw, '订单日期'))
        supplier_code = str(get_val(raw, '供应商编码') or '').strip()
        material_code = str(get_val(raw, '物料编码') or '').strip()
        plan_arrival_date = _norm_date(get_val(raw, '计划到货日期'))

        if not (pon and order_date and supplier_code and material_code and plan_arrival_date):
            errors.append({'row': ri, 'reason': '必填项缺失（采购订单号/订单日期/供应商编码/物料编码/计划到货日期）'})
            continue

        order_qty = _to_number(get_val(raw, '订单数量'))
        arrival_qty = _to_number(get_val(raw, '计划到货数量'))
        if order_qty is None:
            errors.append({'row': ri, 'reason': '订单数量必须为数字'}); continue
        if arrival_qty is None:
            errors.append({'row': ri, 'reason': '计划到货数量必须为数字'}); continue

        key = (pon, material_code, plan_arrival_date)
        if key in existing_keys:
            skipped += 1
            errors.append({'row': ri, 'reason': '采购订单号+物料+计划到货日期 已存在，跳过'})
            continue

        av_raw = get_val(raw, '实际到货数量')
        actual_arrival_qty = None
        if av_raw not in (None, ''):
            actual_arrival_qty = _to_number(av_raw)
            if actual_arrival_qty is None:
                errors.append({'row': ri, 'reason': '实际到货数量必须为数字'}); continue
            if actual_arrival_qty == 0:
                actual_arrival_qty = None
        actual_arrival_date = _norm_date(get_val(raw, '实际到货日期')) or None
        # 实际到货数量已填但日期空 → 填当天（与前端"填非0且日期空→前端填当天"一致）
        if _is_filled(actual_arrival_qty) and not actual_arrival_date:
            actual_arrival_date = biz_today().strftime('%Y-%m-%d')

        raw_status = str(get_val(raw, '状态') or '').strip() or '创建'
        if raw_status not in VALID_STATUS:
            errors.append({'row': ri, 'reason': '状态非法：%s（应为创建/在途/关闭/作废）' % raw_status}); continue

        supplier_name, material_name = _lookup_names(
            supplier_code, material_code,
            str(get_val(raw, '供应商名称') or '').strip(),
            str(get_val(raw, '物料名称') or '').strip())
        note = str(get_val(raw, '备注') or '').strip()

        po_no = _next_code()
        execute_db(
            "INSERT INTO purchase_orders "
            "(po_no, purchase_order_no, order_date, supplier_code, supplier_name, material_code, material_name, "
            "order_qty, arrival_qty, actual_arrival_qty, actual_arrival_date, status, unarrived_qty, plan_arrival_date, note) "
            "VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)",
            (po_no, pon, order_date, supplier_code, supplier_name, material_code, material_name,
             order_qty, arrival_qty, actual_arrival_qty, actual_arrival_date, raw_status, 0, plan_arrival_date, note)
        )
        existing_keys.add(key)
        touched_pons.add(pon)
        created += 1

    for pon in touched_pons:
        _recalc_unarrived(pon)
        _auto_close(pon)

    msg = '导入完成：新增 %d 条，跳过 %d 条（重复），错误 %d 条。' % (created, skipped, len(errors))
    return jsonify({'success': True, 'message': msg, 'created': created, 'skipped': skipped, 'errors': errors})


@bp.route('/api/purchase-orders/export-data')
def po_export_data():
    """导出当前采购入库数据为 XLSX（含系统计算的未到货数量）。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment

    rows = query_db(
        "SELECT po_no, purchase_order_no, order_date, supplier_code, supplier_name, "
        "material_code, material_name, order_qty, arrival_qty, unarrived_qty, "
        "plan_arrival_date, actual_arrival_qty, actual_arrival_date, status, note "
        "FROM purchase_orders ORDER BY purchase_order_no, material_code, plan_arrival_date, po_no"
    )
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '采购入库数据'
    headers = ['采购入库单号', '采购订单号', '订单日期', '供应商编码', '供应商名称', '物料编码', '物料名称',
               '订单数量', '计划到货数量', '未到货数量', '计划到货日期', '实际到货数量', '实际到货日期', '状态', '备注']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')

    for r in rows:
        ws.append([
            r.get('po_no') or '',
            r.get('purchase_order_no') or '',
            _fmt_date(r.get('order_date')),
            r.get('supplier_code') or '',
            r.get('supplier_name') or '',
            r.get('material_code') or '',
            r.get('material_name') or '',
            round(float(r.get('order_qty') or 0), 3),
            round(float(r.get('arrival_qty') or 0), 3),
            round(float(r.get('unarrived_qty') or 0), 3),
            _fmt_date(r.get('plan_arrival_date')),
            round(float(r.get('actual_arrival_qty') or 0), 3),
            _fmt_date(r.get('actual_arrival_date')),
            r.get('status') or '',
            r.get('note') or '',
        ])

    widths = [14, 14, 12, 12, 14, 12, 14, 10, 12, 10, 12, 12, 12, 8, 16]
    from openpyxl.utils import get_column_letter
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='采购入库数据.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# 应用启动时确保表存在并迁移
# 【P2-26 修复 · 2026-10-14】导入期建表/加列加异常保护：DB 故障不应导致整个后端起不来
# （原实现裸调用，MySQL 不可达时后端直接崩，所有页签全挂）。
try:
    _ensure_table()
    _migrate()
except Exception as _e:
    logging.getLogger(__name__).warning('purchase_orders 启动建表/迁移失败（已忽略）: %s', _e)
