# -*- coding: utf-8 -*-
"""工单管理"""

import logging
import math
import os
import threading
import time
from datetime import date, datetime, timedelta

from flask import Blueprint, request, jsonify, send_file

from ..database import (query_db, execute_db, execute_many_db, execute_ddl,
                        in_transaction, transaction, safe_fields)
from ..errors import err_payload
from ..helpers import get_config
from ..services.scheduling_core import SchedContext, anneal as sched_anneal
from ..utils import gen_id, serialize_row, serialize_rows, biz_today, biz_now, biz_midnight

bp = Blueprint('work_orders', __name__)

# 「计划需求日期」开关（system_config.module_plan_req_date，默认 '0' 关闭）
PLAN_REQ_DATE_MODULE = 'plan_req_date'
# 开关启用且新增/编辑工单的计划日期早于当日时，计划开工时刻重置为当日 08:00:00
PLAN_REQ_START_HOUR = 8

# ===== 一键排产：并发保护与优化时间预算（S1）=====
# 单飞锁：同一时刻只允许一个排产请求。无锁时两人同时点击会互相覆盖排产结果。
# 【P3 说明 · 2026-10-14】进程内锁（与 schedule.py 的 _calc_lock 同前提）：
# 当前为单进程部署（`backend/run.py` → `app.run()`），进程内锁即可覆盖全部请求；
# 若改为多 worker 部署，需换成跨进程互斥，否则并发一键排产会互相覆盖落库结果。
_WO_SCHED_LOCK = threading.Lock()
# 优化阶段（minimax swap）时间预算（秒）：超预算即接受当前解，避免大批量时请求挂死。
# 只作用于 swap 优化阶段；主贪心阶段不受影响，保证每张单都有落点。
_WO_SCHED_BUDGET_SEC = float(os.environ.get('APS_SCHED_BUDGET_SEC', '3') or 3)


# 注：统一异常响应 `_err_payload` 已上提到共享模块 backend/errors.py（P1-2），
# 全项目统一使用 `err_payload(e)`；原先只有本模块能用，导致其它模块各自
# 直接返回 str(e)、完全不受 DEBUG 门控。


def _migrate():
    """存量表加列（幂等）：为「计划需求日期」功能引入 work_orders.delayed（延误标记）列。

    失败处理：记录 ERROR 日志但**不阻断后端启动**（仅该功能不可用）。
    这里刻意不抛出——启动时抛异常会导致整个后端起不来，代价大于缺列；
    与原先 `except: pass` 的区别是：现在会显式报错，不再"静默"。
    """
    try:
        cols = query_db(
            "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
            "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'work_orders'"
        )
        existing = {r['COLUMN_NAME'] for r in (cols or [])}
        if existing and 'delayed' not in existing:
            # 注意：DELAYED 是 MySQL 保留字（INSERT DELAYED 语法），列名必须用反引号包裹
            execute_ddl("ALTER TABLE work_orders ADD COLUMN `delayed` TINYINT(1) DEFAULT 0 "
                        "COMMENT '延误标记：1=计划开工/完工早于计算当日，已被强制调整为当日'")
        # material_status：原先在 _wo_ensure_cols()（排产请求路径）里执行 ALTER，
        # 属"业务请求触发 DDL"，且在事务内会隐式提交，已上提到启动迁移。
        if existing and 'material_status' not in existing:
            execute_ddl("ALTER TABLE work_orders ADD COLUMN material_status VARCHAR(20) DEFAULT 'OK' "
                        "COMMENT '物料齐套状态:OK/待料/未知（按缺料明细快照反推）'")
        # manual_locked（M6 计划稳定性）：人工排产过的「创建」单，一键排产不再改动它。
        # 用户拍板 2026-10-07：手工调整过的单不重排；不做前端「手排」标记。
        if existing and 'manual_locked' not in existing:
            execute_ddl("ALTER TABLE work_orders ADD COLUMN manual_locked TINYINT(1) DEFAULT 0 "
                        "COMMENT '人工排产锁定：1=已被人工排产(编辑计划日期/产线/拖到产线/甘特拖动)，一键排产不重排'")
        # 【M1】序列相关换型矩阵（SDST）：changeover(前物料, 后物料)。
        # 表为空时行为与改动前完全一致（回退物料自身 changeover_time），故可安全先建表。
        execute_ddl(
            "CREATE TABLE IF NOT EXISTS product_changeover ("
            " id VARCHAR(50) PRIMARY KEY,"
            " from_mat VARCHAR(100) NOT NULL COMMENT '换型前物料编码',"
            " to_mat VARCHAR(100) NOT NULL COMMENT '换型后物料编码',"
            " changeover_hours DECIMAL(10,2) DEFAULT 0 COMMENT '由 from_mat 换到 to_mat 所需小时',"
            " note LONGTEXT,"
            " updated_at DATETIME DEFAULT CURRENT_TIMESTAMP,"
            " UNIQUE KEY uk_changeover_ft (from_mat, to_mat),"
            " INDEX idx_changeover_from (from_mat),"
            " INDEX idx_changeover_to (to_mat)"
            ") ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='序列相关换型矩阵(SDST)：同物料连产默认0，未配置回退物料自身换型时间'")
    except Exception as e:
        logging.getLogger(__name__).error('[启动迁移] work_orders 加列失败，相关功能可能不可用：%s', e)


_migrate()


@bp.route('/api/work-orders')
def work_orders_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    statuses = request.args.getlist('status')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(order_no LIKE %s OR sap_aufnr LIKE %s OR material_code LIKE %s OR sap_matnr LIKE %s)")
        args.extend([f'%{search}%'] * 4)
    if statuses:
        placeholders = ','.join(['%s'] * len(statuses))
        conditions.append(f"status IN ({placeholders})")
        args.extend(statuses)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM work_orders WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM work_orders WHERE {where} ORDER BY material_code ASC, planned_start ASC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )
    return jsonify({'success': True, 'total': total, 'page': page, 'per_page': per_page, 'data': serialize_rows(rows)})


@bp.route('/api/work-orders', methods=['POST'])
def work_orders_create():
    data = request.get_json() or {}
    wid = gen_id()
    data['id'] = wid
    data = _wo_auto_end(data, None)  # 自动计算计划完工
    # 「计划需求日期」开关：新增工单时按新原则处理（日期<当日 → 开工=当日 08:00，完工按工期重算）
    data, _plan_adj = _wo_apply_plan_req_date(data, None)

    # 产能真实性 D：新建工单的计划窗口不得与设备「保养/维修」带重叠
    _tgt_eq = (data.get('workcenter_name') or '').strip()
    _ps = _wo_parse_dt(data.get('planned_start'))
    _pe = _wo_parse_dt(data.get('planned_end'))
    if _tgt_eq and _ps and _pe and _pe > _ps:
        _band = _wo_overlaps_maint(_tgt_eq, _ps, _pe)
        if _band:
            _ms, _me = _band
            return jsonify({'success': False, 'message':
                '计划窗口与设备「%s」的保养/维修时间带（%s ~ %s）重叠，无法创建。请调整计划开工/完工避开该时段。'
                % (_tgt_eq, _ms.strftime('%Y-%m-%d %H:%M'), _me.strftime('%Y-%m-%d %H:%M'))})

    data.setdefault('delayed', 0)   # 未触发延误调整时显式写 0，避免插入 NULL
    fields = ['id', 'order_no', 'product_id', 'quantity', 'start_date', 'end_date',
              'planned_start', 'planned_end',
              'status', 'source', 'priority', 'note', 'material_code', 'material_desc',
              'plant', 'workcenter_name', 'plan_qty', 'confirm_qty', 'completed_qty', 'unit',
              'batch', 'client_name', 'client_code', 'order_type', 'sales_order_id',
              'schedule_ref', 'delayed']

    cols = ', '.join(['`delayed`' if f == 'delayed' else f for f in fields])
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]

    execute_db(f"INSERT INTO work_orders ({cols}) VALUES ({placeholders})", values)
    resp = {'success': True, 'id': wid, 'message': '创建成功'}
    if _plan_adj:
        resp['plan_adjusted'] = True
        resp['planned_start'] = data.get('planned_start')
        resp['planned_end'] = data.get('planned_end')
        resp['message'] = ('创建成功（计划日期早于当日，已自动调整为 %s 并按工期重算完工，已标记延误）'
                           % (data.get('planned_start') or ''))
    return jsonify(resp)


@bp.route('/api/work-orders/<wid>', methods=['GET'])
def work_orders_get(wid):
    row = query_db("SELECT * FROM work_orders WHERE id=%s", (wid,), one=True)
    if not row:
        return jsonify({'success': False, 'message': '工单不存在'}), 404
    return jsonify({'success': True, 'data': serialize_row(row)})


@bp.route('/api/work-orders/<wid>', methods=['PUT'])
def work_orders_update(wid):
    data = request.get_json() or {}
    old = query_db(
        "SELECT material_code, planned_start, planned_end, confirm_qty, completed_qty, plan_qty FROM work_orders WHERE id=%s",
        (wid,), one=True)
    if not old:
        return jsonify({'success': False, 'message': '工单不存在'}), 404
    data = _wo_auto_end(data, old)  # 自动计算计划完工（覆盖）
    # 「计划需求日期」开关：编辑工单且显式提交了计划日期时按新原则处理。
    # only_if_provided=True —— 只改备注/状态等无关字段时不会顺带改动计划日期与延误标记。
    data, _plan_adj = _wo_apply_plan_req_date(data, old, only_if_provided=True)

    # 产能真实性 D：手工排产不得与设备「保养/维修」带重叠。
    # 仅当排产字段（计划开工/计划完工/产线）相对原值发生变化时才校验，
    # 避免阻断无关字段编辑与历史已重叠数据。
    _tgt_eq = (data.get('workcenter_name') or old.get('workcenter_name') or '').strip()
    _ps = _wo_parse_dt(data.get('planned_start') or old.get('planned_start'))
    _pe = _wo_parse_dt(data.get('planned_end') or old.get('planned_end'))
    _old_eq = (old.get('workcenter_name') or '').strip()
    _old_ps = _wo_parse_dt(old.get('planned_start'))
    _old_pe = _wo_parse_dt(old.get('planned_end'))
    _sched_changed = (_tgt_eq != _old_eq) or (_ps != _old_ps) or (_pe != _old_pe)
    if _sched_changed:
        _band = _wo_overlaps_maint(_tgt_eq, _ps, _pe)
        if _band:
            _ms, _me = _band
            return jsonify({'success': False, 'message':
                '计划窗口与设备「%s」的保养/维修时间带（%s ~ %s）重叠，无法保存。请调整计划开工/完工避开该时段。'
                % (_tgt_eq, _ms.strftime('%Y-%m-%d %H:%M'), _me.strftime('%Y-%m-%d %H:%M'))})
    # 【M6 计划稳定性】改动计划开工/完工/产线 ⇒ 视为人工排产，锁定该单：
    # 后续「一键排产」不再改写它（但仍作为占用墙，避免其它单压到它身上）。
    # 调用方显式传 manual_locked=0 时不覆盖 —— 那是"恢复自动排产"的解锁动作。
    if _sched_changed and data.get('manual_locked') is None:
        data['manual_locked'] = 1

    # 部分更新：只更新请求体中显式提供的字段，未提供的字段保持不变。
    # 修正：此前为全字段 UPDATE，未提供的字段被置 NULL（拖回待排产 / 批准 / 排产
    #       会清空工单号、物料编码、计划数量等），现改为仅更新传入字段。
    updatable = ['order_no', 'product_id', 'quantity', 'start_date', 'end_date',
                 'planned_start', 'planned_end',
                 'status', 'priority', 'note', 'material_code', 'material_desc',
                 'plant', 'workcenter_name', 'plan_qty', 'confirm_qty', 'completed_qty', 'unit',
                 'batch', 'client_name', 'client_code', 'order_type', 'sales_order_id',
                 'schedule_ref', 'delayed', 'manual_locked']
    fields = [f for f in updatable if f in data]
    if not fields:
        return jsonify({'success': True, 'message': '无字段变更'})
    sets = ', '.join([("`delayed`=%s" if f == 'delayed' else f"{f}=%s") for f in fields])
    values = [data.get(f) for f in fields] + [wid]
    execute_db(f"UPDATE work_orders SET {sets}, updated_at=NOW() WHERE id=%s", values)
    _locked_now = ('manual_locked' in data) and data.get('manual_locked')
    # 排产动作（指定了产线）后，按转产时间对该设备上的工单做前向顺排
    if data.get('workcenter_name'):
        _wo_reschedule_by_changeover(data.get('workcenter_name'))
    # 【M6】人工排产锁定提示（用户要求极简，前端不加「手排」标记，仅在保存提示中说明）
    _lock_note = '；已标记为人工排产，后续一键排产不再改动该单' if _locked_now else ''
    if _plan_adj:
        return jsonify({'success': True, 'plan_adjusted': True,
                        'planned_start': data.get('planned_start'),
                        'planned_end': data.get('planned_end'),
                        'manual_locked': 1 if _locked_now else None,
                        'message': '更新成功（计划日期早于当日，已自动调整为 %s 并按工期重算完工，已标记延误）%s'
                                   % (data.get('planned_start') or '', _lock_note)})
    return jsonify({'success': True, 'message': '更新成功' + _lock_note,
                    'manual_locked': 1 if _locked_now else None})


@bp.route('/api/work-orders/<wid>/unlock-schedule', methods=['PUT'])
def work_orders_unlock_schedule(wid):
    """【M6】恢复自动排产：清除人工排产锁定标记，使该单重新纳入「一键排产」的重排范围。

    锁定的副作用是：即使交期变了、物料到了也不会自动重排，故必须提供解锁入口。
    仅「创建」状态可解锁（批准/执行 本就不在一键排产范围内）。
    """
    wo = query_db("SELECT id, status FROM work_orders WHERE id=%s", (wid,), one=True)
    if not wo:
        return jsonify({'success': False, 'message': '工单不存在'}), 404
    if wo.get('status') != '创建':
        return jsonify({'success': False, 'message': '仅「创建」状态工单需要解锁'}), 400
    execute_db("UPDATE work_orders SET manual_locked=0, updated_at=NOW() WHERE id=%s", (wid,))
    return jsonify({'success': True, 'message': '已恢复自动排产，该单将在下次一键排产中重新参与排程'})


@bp.route('/api/work-orders/<wid>/assign-line', methods=['PUT'])
def work_orders_assign_line(wid):
    """拖拽『待排产工单』到『产线排产看板』指定设备下列：
    ① workcenter_name = 落点设备名；② status = '创建'；
    ③ 计划完工 = 计划开工 + 提前期(产量)，产量优先级：未完工(确认-已完工) > 计划数量。
    与 _wo_auto_end 口径一致（提前期=ceil(数量/日产量 production_time)，缺失回退 production_cycle）。"""
    data = request.get_json() or {}
    eq = (data.get('workcenter_name') or '').strip()
    old = query_db(
        "SELECT material_code, planned_start, plan_qty, confirm_qty, completed_qty FROM work_orders WHERE id=%s",
        (wid,), one=True)
    if not old:
        return jsonify({'success': False, 'message': '工单不存在'}), 404

    def _f(x):
        try:
            return float(x) if x not in (None, '', 'null') else 0.0
        except (ValueError, TypeError):
            return 0.0

    conf = old.get('confirm_qty')
    comp = old.get('completed_qty')
    unfinished = max(0.0, _f(conf) - _f(comp))
    plan = old.get('plan_qty')
    qty = unfinished if unfinished > 0 else (_f(plan) if plan not in (None, '', 'null') else 0.0)

    mat = old.get('material_code')
    start = old.get('planned_start')
    planned_end = None
    if start and qty > 0:
        # 设备已确定 → 工期按该设备产能换算（开关默认关 → 与日产量口径一致）
        lead = _wo_lead_days_eq(qty, mat, eq)
        if lead:
            try:
                s = str(start).replace('T', ' ').strip()
                ps = None
                for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d %H:%M', '%Y-%m-%d'):
                    try:
                        ps = datetime.strptime(s[:19] if ' ' in s else s[:10], fmt)
                        break
                    except (ValueError, TypeError):
                        ps = None
                if ps:
                    planned_end = (ps + timedelta(days=lead)).strftime('%Y-%m-%d %H:%M:%S')
            except (ValueError, TypeError):
                pass

    # 产能真实性 D：分配到产线后，计划窗口不得与设备保养/维修带重叠
    if eq and start and planned_end:
        _ps = _wo_parse_dt(start)
        _pe = _wo_parse_dt(planned_end)
        _band = _wo_overlaps_maint(eq, _ps, _pe)
        if _band:
            _ms, _me = _band
            return jsonify({'success': False, 'message':
                '该工单计划窗口与设备「%s」的保养/维修时间带（%s ~ %s）重叠，无法分配。请选择其它设备。'
                % (eq, _ms.strftime('%Y-%m-%d %H:%M'), _me.strftime('%Y-%m-%d %H:%M'))})

    # 【M6】拖到产线 = 人工排产动作 → 锁定，后续一键排产不再改写该单
    execute_db(
        "UPDATE work_orders SET workcenter_name=%s, status=%s, planned_end=%s, manual_locked=1, "
        "updated_at=NOW() WHERE id=%s",
        (eq or None, '创建', planned_end, wid))
    # 排产到产线后，按转产时间对该设备上的工单做前向顺排（计划开工不得早于上一单 计划完工+转产）
    _wo_reschedule_by_changeover(eq)
    return jsonify({'success': True, 'message': '已分配到产线；已标记为人工排产，后续一键排产不再改动该单',
                    'manual_locked': 1, 'planned_end': planned_end})


@bp.route('/api/work-orders/<wid>', methods=['DELETE'])
def work_orders_delete(wid):
    wo = query_db("SELECT source, status, schedule_ref FROM work_orders WHERE id=%s", (wid,), one=True)
    if not wo:
        return jsonify({'success': False, 'message': '工单不存在'}), 404
    if wo.get('status') != '创建':
        return jsonify({'success': False, 'message': '仅创建状态的工单可删除'}), 400
    execute_db("DELETE FROM work_orders WHERE id=%s", (wid,))
    # 如果该工单来自排程，恢复 wo_generated 标记（按稳定的 schedule_ref 匹配，避免显示用 aufnr 不匹配）
    ref = wo.get('schedule_ref') if wo else None
    if ref:
        execute_db("UPDATE peacock_schedule SET wo_generated=0 WHERE aufnr=%s", (ref,))
    return jsonify({'success': True, 'message': '删除成功'})


@bp.route('/api/work-orders/<wid>/items')
def work_order_items(wid):
    rows = query_db("SELECT * FROM work_orders_items WHERE aufnr=%s", (wid,))
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@bp.route('/api/work-orders/next-serial')
def work_orders_next_serial():
    """返回下一个工单流水号 GD+3位"""
    row = query_db("SELECT MAX(order_no) as max_no FROM work_orders WHERE order_no LIKE 'GD%'", one=True)
    max_no = row.get('max_no') if row else None
    if max_no:
        try:
            num = int(max_no[2:]) + 1
        except (ValueError, TypeError, IndexError) as e:
            # 【P2-19 修复】原为裸 `except:`（连 KeyboardInterrupt/SystemExit 都吞）且静默。
            # 单号非 'GD'+数字（历史脏数据）时回退 1，但必须留痕，否则"单号为何跳回 GD001"无从排查。
            logging.getLogger(__name__).warning('工单流水号解析失败，回退为 1：max_no=%r (%s)', max_no, e)
            num = 1
    else:
        num = 1
    return jsonify({'success': True, 'order_no': f'GD{num:03d}'})


@bp.route('/api/work-orders/export-template')
def work_orders_export_template():
    """导出工单导入模板（表头 + 示例/说明行），供批量填写后导入。"""
    from io import BytesIO
    import openpyxl
    from openpyxl.styles import Font, PatternFill, Alignment
    from openpyxl.utils import get_column_letter

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '工单导入模板'
    headers = ['工单号', '物料编码', '物料描述', '工厂', '生产线', '计划数量',
               '确认数量', '已完工数量', '单位', '计划开工', '计划完工',
               '状态', '来源', '优先级', '批次', '客户名称']
    ws.append(headers)
    hfill = PatternFill('solid', fgColor='4472C4')
    hfont = Font(color='FFFFFF', bold=True)
    for c in ws[1]:
        c.fill = hfill
        c.font = hfont
        c.alignment = Alignment(horizontal='center')

    # 示例行
    ws.append(['GD999', 'P001', '示例产品A', '1000', '产线1', 1000,
               1000, 0, 'KG', '2026-08-01', '2026-08-05',
               '创建', 'SCH', '高', 'B20260801', '示例客户'])
    # 说明行
    ws.append(['（以下为示例，请按实际填写；"工单号"为唯一标识，重复将跳过；'
               '日期格式 YYYY-MM-DD；状态限：创建/执行/完成/关闭/作废；'
               '“未完工数量”=确认数量-已完工数量，由系统计算，无需填写）',
               '', '', '', '', '', '', '', '', '', '', '', '', '', '', ''])

    widths = [12, 14, 18, 8, 12, 12, 12, 12, 8, 13, 13, 8, 8, 8, 14, 14]
    for i, w in enumerate(widths, start=1):
        ws.column_dimensions[get_column_letter(i)].width = w

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, as_attachment=True, download_name='工单导入模板.xlsx',
                     mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet')


# 导入：中文列名 -> DB 字段
_WO_IMPORT_COLMAP = {
    '工单号': 'order_no', '物料编码': 'material_code', '物料描述': 'material_desc',
    '工厂': 'plant', '生产线': 'workcenter_name', '计划数量': 'plan_qty',
    '确认数量': 'confirm_qty', '已完工数量': 'completed_qty', '单位': 'unit',
    '计划开工': 'planned_start', '计划完工': 'planned_end', '状态': 'status',
    '来源': 'source', '优先级': 'priority', '批次': 'batch', '客户名称': 'client_name',
}
_WO_STATUS_ENUM = {'创建', '执行', '完成', '关闭', '作废'}


def _wo_to_num(v):
    if v is None or v == '':
        return None
    try:
        return float(v)
    except (ValueError, TypeError):
        return None


def _wo_to_date(v):
    if v is None or v == '':
        return None
    if isinstance(v, (datetime, date)):
        return v.strftime('%Y-%m-%d')
    s = str(v).strip()
    if not s:
        return None
    return s[:10]


# ===== 一键排产性能优化（2026-09-01 修复 P1-9）=====
# 原 _wo_lead_days / _wo_changeover_hours / _wo_explode_components 每次调用都查 products / bom，
# 在「100 单 × 多循环」下形成上万次 DB 往返（N+1）。改为"一次加载、内存字典复用"：
# 在 work_orders_auto_schedule 开头调用 _reload_wo_caches() 刷新，后续均为 O(1) 字典查找。
_PROD_ROWS = None   # material_code -> products 行（含 production_time / production_cycle / changeover_time / type ...）
_BOM_ROWS = None    # parent_matnr -> [comp_matnr, ...]
_EQ_ROWS = None     # 设备名 -> equipment 行（含 id / status / capacity / unit / start_time / end_time）
_CO_MATRIX = {}     # 【M1】(from_mat, to_mat) -> 换型小时数
# 开关缓存：内层循环里逐次读 system_config 会形成 N+1 查库（退火 480 轮实测 3.1s 即由此而来），
# 故在每轮排产开始时（_reload_wo_caches）刷新一次，之后走内存。
_CO_MATRIX_ON = None   # 【M1】序列相关换型开关缓存
_EQ_CAP_ON = None      # 设备产能参与工期开关缓存
_SEMI_FLOOR_ON = None  # 【M3】自制件排程日回流开关缓存（P2-11：原为每物料查一次库）
# 【P2-23】缓存重建锁：串行化 _reload_wo_caches()，避免多线程读到"半构建"的缓存
_CACHE_LOCK = threading.RLock()


def _reload_wo_caches():
    """一次性加载 products / bom / equipment / 换型矩阵 到内存字典（每轮排产刷新一次，避免 N+1 逐次查库）。

    🔴【P2-23 修复 · 2026-10-14】原实现**原地重建**全局字典（先赋值空 dict 再逐条 fill，
    并直接 `global _PROD_ROWS` 后写）。Flask 多线程下，创建工单等请求不持有排产锁却会
    触发本函数 → 正在排产的线程可能读到**刚被清空、尚未填完**的矩阵/物料表，
    于是"试算"与"落库"用了两份不同的主数据（结果不一致）。
    现改为：全部先构建到**局部变量**，最后一次**原子替换**全局引用；并用 RLock 串行化，
    保证任一线程看到的都是完整的一代数据。
    """
    global _PROD_ROWS, _BOM_ROWS, _EQ_ROWS, _CO_MATRIX, _CO_MATRIX_ON, _EQ_CAP_ON
    global _TOC_ON, _TOC_BUFFER_DAYS          # 【L4】TOC/DBR 开关与缓冲天数
    global _SEMI_FLOOR_ON                     # 【M3 / P2-11】
    with _CACHE_LOCK:
        # 开关缓存置空 → 下一轮排产重新读配置（保证界面改开关即时生效）
        _CO_MATRIX_ON = None
        _EQ_CAP_ON = None
        _TOC_ON = None
        _TOC_BUFFER_DAYS = None
        _SEMI_FLOOR_ON = None

        prod_rows = {r['material_code']: r for r in query_db(
            "SELECT material_code, production_time, production_cycle, changeover_time, type, shelf_life, "
            "production_equipment FROM products")}
        bom_rows = {}
        for r in query_db("SELECT parent_matnr, comp_matnr FROM bom"):
            bom_rows.setdefault(r['parent_matnr'], []).append(r['comp_matnr'])
        eq_rows = {}
        for r in query_db("SELECT id, name, status, capacity, unit, start_time, end_time FROM equipment"):
            _nm = (r.get('name') or '').strip()
            if _nm:
                eq_rows[_nm] = r
        # 【M1】换型矩阵（表可能尚未建立 → 失败即视为空矩阵，行为同改动前）
        co_matrix = {}
        try:
            for r in query_db("SELECT from_mat, to_mat, changeover_hours FROM product_changeover"):
                _f = (r.get('from_mat') or '').strip()
                _t = (r.get('to_mat') or '').strip()
                if not _f or not _t:
                    continue
                try:
                    co_matrix[(_f, _t)] = float(r.get('changeover_hours') or 0)
                except (ValueError, TypeError):
                    continue
        except Exception as e:
            logging.getLogger(__name__).warning('[换型矩阵] 加载失败，将回退物料自身换型时间：%s', e)

        # 原子替换：构建完成后一次性切换引用
        _PROD_ROWS, _BOM_ROWS, _EQ_ROWS, _CO_MATRIX = prod_rows, bom_rows, eq_rows, co_matrix


def _wo_lead_days(qty, mat_code):
    """生产提前期（天）：优先 ceil(未完工数量 / 日产量)，日产量缺失/0 时回退固定生产周期。
    与缺料明细 schedule._lead 口径一致。返回整数天数；无法计算返回 None。
    【2026-09-01 修复 P1-9】改读内存缓存 _PROD_ROWS，避免每工单/每循环逐次查 products。"""
    global _PROD_ROWS
    if _PROD_ROWS is None:
        _reload_wo_caches()
    prod = _PROD_ROWS.get(mat_code)
    if not prod:
        return None
    try:
        daily = float(prod.get('production_time') or 0)
    except (ValueError, TypeError):
        daily = 0.0
    if daily > 0 and qty > 0:
        return math.ceil(qty / daily)
    try:
        cyc = float(prod.get('production_cycle') or 0)
    except (ValueError, TypeError):
        cyc = 0.0
    if cyc > 0:
        return cyc  # 固定周期天数（不除数量）
    return None


# ===== 设备「每小时最大产量」参与工期计算（2026-10-07，用户拍板：统一单位后用于工期）=====
# 开关 module_equipment_capacity 默认 '0' 关闭 → 工期沿用日产量口径（与改动前完全一致）。
# 打开后：工期(天) = ceil(数量 / (每小时产能 × 24))；设备产能缺失/非正 → 回退日产量口径（不劣化）。
# 前置条件（用户待办）：equipment.capacity 单位统一为产量单位、NULL 补齐；
# 未满足前保持关闭，否则单位不通约会把工期算错。
EQUIPMENT_CAPACITY_MODULE = 'equipment_capacity'
# 设备状态：不可用一律排除出候选设备；保养/维修不排除，但必须占用其时间带
EQ_STATUS_UNAVAILABLE = '不可用'
EQ_STATUS_MAINT = ('保养', '维修')

# 【M1】序列相关换型矩阵开关，默认 '1' 开启（表为空时行为与关闭完全一致）
CHANGEOVER_MATRIX_MODULE = 'changeover_matrix'
# 【M2】排产方向开关：module_schedule_jit = '1' 时做倒排 JIT（尽量贴近交期开工），默认 '0' 正排 ASAP
JIT_SCHEDULE_MODULE = 'schedule_jit'
# 【M3】自制件排程产出日是否作为父件开工 floor，默认 '1' 开启
SEMI_FLOOR_MODULE = 'semi_schedule_floor'
# 【L2】是否启用序列空间局部搜索（模拟退火），默认 '1' 开启。
#   关闭后仍走【L1】统一解码器，只是不做邻域搜索（等价改动前的纯贪心结果）。
SEQ_SEARCH_MODULE = 'schedule_search'
# 退火随机种子（system_config.sched_sa_seed）：固定种子 → 同输入同输出，回归可复现
SA_SEED_KEY = 'sched_sa_seed'


def _seq_search_enabled():
    """【L2】序列空间搜索开关，默认开启。"""
    try:
        return get_config('module_%s' % SEQ_SEARCH_MODULE, '1') == '1'
    except Exception:
        return True


def _seq_search_seed():
    try:
        return int(get_config(SA_SEED_KEY, '20261007'))
    except (ValueError, TypeError):
        return 20261007


def _wo_jit_enabled():
    """【M2】倒排 JIT 开关。默认关闭（正排 ASAP，与改动前完全一致）。"""
    try:
        return get_config('module_%s' % JIT_SCHEDULE_MODULE, '0') == '1'
    except Exception:
        return False


def _eq_capacity_enabled():
    """读取「设备产能参与工期」开关（系统配置 → 计算逻辑 → 排程模块开关）。

    【性能】`_wo_lead_days_eq()` 对「每张单 × 每个候选设备」调用本函数，
    逐次读库就是 N+1；故与换型开关同样做模块级缓存，由 `_reload_wo_caches()` 刷新。
    """
    global _EQ_CAP_ON
    if _EQ_CAP_ON is None:
        try:
            _EQ_CAP_ON = get_config('module_%s' % EQUIPMENT_CAPACITY_MODULE, '0') == '1'
        except Exception:
            _EQ_CAP_ON = False
    return _EQ_CAP_ON


def _eq_capacity_per_hour(eq_name):
    """设备每小时最大产量；设备不存在 / 产能为 NULL 或非正数 → 返回 None（调用方回退日产量口径）。"""
    global _EQ_ROWS
    if _EQ_ROWS is None:
        _reload_wo_caches()
    if not eq_name:
        return None
    r = (_EQ_ROWS or {}).get(eq_name)
    if not r:
        return None
    try:
        cap = float(r.get('capacity'))
    except (ValueError, TypeError):
        return None
    return cap if cap > 0 else None


def _co_matrix_enabled():
    """【M1】序列相关换型开关，默认 '1' 开启。

    关闭后回到改动前口径：换型恒等于物料自身 changeover_time（含"同物料也照加"）。

    【性能】本函数在解码/成本计算的内层循环里被**逐次**调用（每次换型判定一次），
    若每次都读 system_config 就是 N+1 查库——实测退火 480 轮耗 3.1 秒即由此而来。
    故做模块级缓存，由 `_reload_wo_caches()` 在每轮排产开始时刷新一次。
    """
    global _CO_MATRIX_ON
    if _CO_MATRIX_ON is None:
        try:
            _CO_MATRIX_ON = get_config('module_%s' % CHANGEOVER_MATRIX_MODULE, '1') == '1'
        except Exception:
            _CO_MATRIX_ON = True
    return _CO_MATRIX_ON


def _wo_changeover_between(prev_mat, cur_mat):
    """【M1 序列相关换型 SDST】由 prev_mat 换到 cur_mat 所需小时数。

    开关 module_changeover_matrix 关闭 → 直接返回 _wo_changeover_hours(cur_mat)（改动前口径）。
    开启时取数优先级（前两级是本次新增的产能收益，第三级保证不劣化）：
      ① prev 为空（设备上第一张单）或 prev == cur（同物料连产）→ **0**
         —— 这是食品行业最实在的收益：同物料连续生产不再白白预留一次清洗换型。
      ② 换型矩阵 product_changeover 有 (prev, cur) 记录 → 用矩阵值；
         查不到再查对称的 (cur, prev)（矩阵未区分方向时只录一条即可）。
      ③ 都没有 → 回退 cur 自身 changeover_time（**与改动前口径完全一致**）。

    注意：矩阵区分方向时（如浅色→深色 与 深色→浅色 不同），请只录入单向记录，
    不要同时录对称的两条，否则会被 ② 的对称查找命中到反向值。
    """
    if not _co_matrix_enabled():
        return _wo_changeover_hours(cur_mat)
    if not cur_mat or not prev_mat or prev_mat == cur_mat:
        return 0.0
    global _CO_MATRIX
    if _CO_MATRIX is None:
        _reload_wo_caches()
    m = _CO_MATRIX or {}
    if (prev_mat, cur_mat) in m:
        return float(m[(prev_mat, cur_mat)])
    if (cur_mat, prev_mat) in m:
        return float(m[(cur_mat, prev_mat)])
    return _wo_changeover_hours(cur_mat)


def _wo_sched_weights():
    """【M4】排产多目标权重（system_config 可配：sched_weight_<key>）。

    缺省按用户拍板的优先级序（2026-10-07）：
      拖期最小化(0.4) > 换型最少(0.3) > 负荷最均衡(0.2) > 计划最稳定(0.1)
    权重只影响 swap 优化阶段的方案择优，不影响贪心阶段每张单的落点。
    """
    def _w(key, default):
        try:
            v = float(get_config('sched_weight_%s' % key, str(default)))
        except (ValueError, TypeError):
            return default
        return v if v >= 0 else default

    return {
        'tardy': _w('tardy', 0.4),
        'changeover': _w('changeover', 0.3),
        'balance': _w('balance', 0.2),
        'stability': _w('stability', 0.1),
    }


def _wo_lead_days_eq(qty, mat_code, eq_name):
    """按设备差异化的生产提前期（天）。

    开关关闭（默认）→ 直接返回 _wo_lead_days(qty, mat_code)，与改动前**逐值一致**。
    开关开启 → 优先 ceil(数量 / (设备每小时产能 × 24))；产能取不到时回退日产量口径。
    """
    if not _eq_capacity_enabled():
        return _wo_lead_days(qty, mat_code)
    cap = _eq_capacity_per_hour(eq_name)
    if cap and qty > 0:
        return math.ceil(qty / (cap * 24.0))
    return _wo_lead_days(qty, mat_code)


def _wo_changeover_hours(mat_code):
    """取物料的转产时间(小时)。仅 产成品 / 半成品 计入转产占用；
    原辅料/包材及其它（或缺失）返回 0。
    【2026-09-01 修复 P1-9】改读内存缓存 _PROD_ROWS。"""
    global _PROD_ROWS
    if _PROD_ROWS is None:
        _reload_wo_caches()
    if not mat_code:
        return 0.0
    prod = _PROD_ROWS.get(mat_code)
    if not prod:
        return 0.0
    if (prod.get('type') or '') not in ('产成品', '半成品'):
        return 0.0
    try:
        v = prod.get('changeover_time')
        return float(v) if v not in (None, '', 'null') else 0.0
    except (ValueError, TypeError):
        return 0.0


# 物料齐套状态列：标记工单是否因 BOM 子件短缺而被推后开工（待料）。
_wo_cols_ensured = False
# ==========================================================================
# 【L4】TOC / DBR（约束理论：鼓 Drum - 缓冲 Buffer - 绳 Rope）
# --------------------------------------------------------------------------
# 食品行业的痛点：真正限制出货的往往只有一两台关键设备（反应釜/灌注线），
# 其余设备再跑也是堆 WIP、堆库存、吃货龄。TOC 的做法不是"每都想办法塞满"，
# 而是先认出系统的约束（鼓），让它的产出最大化，其余设备按鼓的节拍投料（绳），
# 并在关键位置留出保护时间（缓冲），把扰动吸收掉。
# ==========================================================================
TOC_MODULE = 'toc_dbr'
TOC_BUFFER_DAYS_KEY = 'sched_toc_buffer_days'
_TOC_ON = None            # 开关缓存
_TOC_BUFFER_DAYS = None   # 缓冲天数缓存


def _toc_enabled():
    """【L4】TOC/DBR 总开关（module_toc_dbr，默认 '1' 开启）。

    关闭后回到改动前口径：不识别瓶颈、不给鼓额外搜索预算、不做投料缓冲与绳牵引。
    【性能】与换型开关同理做模块级缓存，由 `_reload_wo_caches()` 每轮刷新一次，
    避免在内层循环里逐次读 system_config（该坑已在 M1 踩过一次：退火慢 65 倍）。
    """
    global _TOC_ON
    if _TOC_ON is None:
        try:
            _TOC_ON = get_config('module_%s' % TOC_MODULE, '1') == '1'
        except Exception:
            _TOC_ON = True
    return _TOC_ON


def _toc_buffer_days():
    """【L4·Buffer】缓冲天数（system_config.sched_toc_buffer_days，默认 1 天）。

    同时用作：① 给鼓供料的上游自制件必须提前完工的天数；② 交期缓冲红/黄/绿灯阈值。
    """
    global _TOC_BUFFER_DAYS
    if _TOC_BUFFER_DAYS is None:
        try:
            v = float(get_config(TOC_BUFFER_DAYS_KEY, '1'))
        except (ValueError, TypeError):
            v = 1.0
        _TOC_BUFFER_DAYS = v if v > 0 else 0.0
    return _TOC_BUFFER_DAYS


def _wo_empty_toc():
    """【P3 修复 · 2026-10-14】统一的 TOC 结果空壳。

    原实现在 `_wo_plan()` 的早退路径里手写了一个只含 3 个字段的 toc
    （`enabled/drum/ranking`），正常路径却有 8 个字段 → 前端读
    `toc.buffer_status` / `toc.buffer_days` 在"无单可排"时是 `undefined`。
    两条路径现共用本函数，字段恒同构。
    """
    return {'enabled': _toc_enabled(), 'drum': None, 'ranking': [],
            'buffer_days': _toc_buffer_days(), 'drum_refine': None,
            'upstream': [], 'rope': [], 'buffer_status': []}


def _wo_toc_drum(assigned, static_load):
    """【L4·Drum】识别瓶颈设备（鼓），返回 (鼓设备名, 全部设备排名)。

    评分 = 累计负荷(天) × (1 + 专属度)
      累计负荷 = 静态占用（锁定单 + 未参与本轮的创建单）+ 本轮派在该设备上的工期；
      专属度   = 本轮工单中「只能落在这台设备」的张数 / 本轮总张数。

    【为什么加专属度】纯负荷口径会漏掉另一类瓶颈：某台设备负荷不是最高，
    但一堆工单的候选设备里只有它（无处可去），需求排队全压在它身上 —— 这恰恰是
    最典型的约束资源。负荷 × 专属度，两者都高才是真正的鼓。
    """
    loads = dict(static_load or {})
    for r in (assigned or []):
        loads[r['eq']] = loads.get(r['eq'], 0.0) + float(r.get('lead') or 0)
    total = max(1, len(assigned or []))
    captive = {}
    for r in (assigned or []):
        cands = r.get('cands') or []
        if len(cands) == 1:
            captive[cands[0]] = captive.get(cands[0], 0) + 1
    # 【P3 修复 · 2026-10-14】排名必须覆盖**全部候选设备**（含本轮零负荷的），
    # 原实现只遍历 loads 字典 → 空闲设备根本不进榜，前端"瓶颈排名"看似全设备榜、实为子集。
    # 口径：静态占用 ∪ 本轮落点 ∪ 各工单候选设备，缺负荷按 0 计。
    all_eq = set(loads.keys())
    for r in (assigned or []):
        all_eq.add(r['eq'])
        for c in (r.get('cands') or []):
            all_eq.add(c)
    rank = []
    for eq in all_eq:
        ld = float(loads.get(eq, 0.0) or 0.0)
        cap = captive.get(eq, 0)
        rank.append({'equipment': eq, 'load_days': round(ld, 4), 'captive_jobs': cap,
                     'score': round(ld * (1.0 + cap / float(total)), 4)})
    rank.sort(key=lambda x: (-x['score'], str(x['equipment'])))
    # 无任何负荷时不应凭空选鼓（否则下游 Rope/缓冲会围着一台空设备空转）
    if not any(x['score'] > 0 for x in rank):
        return None, rank
    return rank[0]['equipment'], rank


def _wo_toc_upstream_targets(assigned, drum_eq, buffer_days):
    """【L4·Buffer】鼓的上游供给目标完工日：{上游工单id: 最晚完工 datetime}。

    对排在鼓上的工单展开 BOM，找出「由本轮其它**非鼓**工单产出」的自制件
    （半成品/产成品）——它们就是给鼓供料的上游。目标完工 = 消费它的鼓工单计划开工 − 缓冲天数；
    被多个鼓工单消费时取**最早**的那个限制。

    为什么要这层：鼓一旦缺料停线，损失的是整个系统的产出；
    而非瓶颈设备停线只是"少赚"。所以宁可让上游提前产、也不要让鼓饿着。
    """
    if not drum_eq or not assigned:
        return {}
    global _BOM_ROWS
    if _BOM_ROWS is None:
        _reload_wo_caches()
    bom = _BOM_ROWS or {}

    def _descendants(root):
        """root 的全部下级（**含中间层自制件**）。

        🔴 这里绝不能用 `_wo_explode_components`：那个函数刻意只返回叶子采购件
        （给『物料可用日』用的，中间自制件的产出由排程自己决定），
        而给鼓供料的恰恰是中间层半成品（C004→B001）—— 用叶子口径会把它们全部漏掉。
        """
        out = set()
        stack = list(bom.get(root) or [])
        seen = set()
        while stack:
            c = stack.pop()
            if not c or c in seen:
                continue
            seen.add(c)
            out.add(c)
            stack.extend(bom.get(c) or [])
        return out

    by_mat = {}
    for r in assigned:
        by_mat.setdefault(r.get('mat'), []).append(r)
    targets = {}
    for r in assigned:
        if r.get('eq') != drum_eq:
            continue
        for comp in _descendants(r.get('mat')):
            pt = (_PROD_ROWS or {}).get(comp) or {}
            if str(pt.get('type') or '') not in ('半成品', '产成品'):
                continue
            for up in by_mat.get(comp, []):
                if up['oid'] == r['oid'] or up.get('eq') == drum_eq:
                    continue
                limit = r['es'] - timedelta(days=buffer_days)
                old = targets.get(up['oid'])
                if old is None or limit < old:
                    targets[up['oid']] = limit
    return targets


def _wo_ensure_cols():
    """幂等保证 work_orders 含 material_status 列（OK/待料）。

    列补建已上提到启动迁移 `_migrate()`；这里仅作兜底：
      - 列已存在（常见情况）→ execute_ddl 命中 1060 忽略，不产生 DDL 锁；
      - **事务内直接跳过** —— DDL 会隐式提交事务，破坏原子性。
    原实现 `except Exception: pass` 静默吞掉所有异常，现改为记日志。
    """
    global _wo_cols_ensured
    if _wo_cols_ensured:
        return
    try:
        if not in_transaction():
            execute_ddl("ALTER TABLE work_orders ADD COLUMN material_status VARCHAR(20) DEFAULT 'OK' "
                        "COMMENT '物料齐套状态:OK/待料（按缺料明细快照反推）'")
    except Exception as e:
        logging.getLogger(__name__).warning('work_orders.material_status 列补建失败：%s', e)
    _wo_cols_ensured = True


# BOM 展开最大层数：防病态/自引用数据导致深度递归（正常制造业 BOM 远低于此值）
_WO_BOM_MAX_DEPTH = 20


def _wo_explode_components(mat_code, _seen=None, _depth=0):
    """多级 BOM 展开，返回 mat_code 的所有『叶子采购件』编码集合。
    某组件若自身也是父件则继续向下展开，否则视为叶子。
    用于反推『物料可用日』：只看最终要采购的叶子件是否短缺。

    防环说明（2026-09-01 复核结论：全局共享 _seen 是**正确且更优**的，不要改成按路径传参）：
    本函数返回的是『集合』。当某共享半成品（菱形 BOM，如 A→B→X→M001 且 A→C→X→M001）
    在兄弟分支被剪枝时，其叶子件已在该分支首次展开时并入结果集；而"从根可达的叶子集合"
    与访问路径无关（任何可达路径去掉环后都是简单路径）。
    已用 34163 组随机 BOM（其中 558 组含环）对比『全局 seen』与『按路径传参』两种实现，
    **结果 100% 一致**（原审查报告 P1-5 判定为 Bug，系误判，已撤回）。
    且全局共享可避免共享子树的重复展开，查询量更少。
    _depth 仅为防御超深链式 BOM 触发 RecursionError，正常 BOM 远达不到。
    【2026-09-01 修复 P1-9】改读内存缓存 _BOM_ROWS，避免递归逐层查 bom（每节点 2 次查询 → 指数级 N+1）。
    """
    global _BOM_ROWS
    if _BOM_ROWS is None:
        _reload_wo_caches()
    if not mat_code or _depth > _WO_BOM_MAX_DEPTH:
        return set()
    if _seen is None:
        _seen = set()
    if mat_code in _seen:
        return set()
    _seen.add(mat_code)
    comps = set()
    for c in (_BOM_ROWS.get(mat_code) or []):
        if not c:
            continue
        if c in _BOM_ROWS and _BOM_ROWS[c]:
            comps |= _wo_explode_components(c, _seen, _depth + 1)
        else:
            comps.add(c)
    return comps


def _wo_parse_dt(v):
    if not v:
        return None
    s = str(v).replace('T', ' ').strip()
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s[:19] if ' ' in s else s[:10], fmt)
        except (ValueError, TypeError):
            continue
    return None


_PRIORITY_RANK = {'高': 0, '中': 1, '低': 2}
def _wo_priority_rank(p):
    return _PRIORITY_RANK.get(p, 3)


def _wo_reschedule_by_changeover(eq_name):
    """同一产线/设备上的工单前向顺排（含转产占用）：
    按 计划开工 升序遍历，每单的 计划开工 不得早于上一单 (计划完工 + 上一单物料转产时间)；
    冲突则顺延其 计划开工 并随之重算 计划完工(= 计划开工 + 提前期)。

    关键约束：『批准/执行』为锁定单，仅作为"占用墙"(推后其后的新建单)，
    自身 planned_start/planned_end **永不改写**，符合规则6「只动创建、不动批准/执行」。

    注：【M1】相邻两单的间隔 = changeover(上一单物料, 本单物料)，同物料连产为 0，
        异物料取换型矩阵值，无矩阵记录回退本单自身 changeover_time（与改动前同口径）。"""
    if not eq_name:
        return
    # 【P1-8 修复 · 2026-10-14】SELECT 增补 manual_locked：
    # 原实现仅以 `status IN ('批准','执行')` 判定锁定，而 M6 的人工锁定
    # (`manual_locked=1`) 是**另一条独立入口**（编辑工单 :221 / 拖到产线 :315 /
    # 一键排产冲突兜底 :2325 置锁），本函数完全不认它 →
    # 用户手工调好的「创建」单会在**别人拖另一张单**时被悄悄推走且无提示，
    # 比"一键排产不重排"这条规则本身更隐蔽（直接违反 M6 契约）。
    # 与 `_wo_plan()` 的待排集口径 `COALESCE(wo.manual_locked,0)=0` 保持一致。
    rows = query_db(
        "SELECT id, material_code, planned_start, planned_end, confirm_qty, completed_qty, "
        "plan_qty, priority, status, COALESCE(manual_locked,0) AS manual_locked "
        "FROM work_orders WHERE workcenter_name=%s AND planned_start IS NOT NULL "
        "ORDER BY planned_start ASC, id ASC",
        (eq_name,))
    if not rows:
        return

    # 产能真实性 D：级联顺排时跳过设备「保养/维修」带
    maint = _wo_maintenance_occ().get(eq_name, [])

    def _skip_maint(dt):
        if dt is None:
            return dt
        changed = True
        while changed:
            changed = False
            for (ms, me) in maint:
                if ms <= dt < me:  # dt 落在保养/维修带内 → 顺延到带结束
                    dt = me
                    changed = True
        return dt

    def _f(x):
        try:
            return float(x) if x not in (None, '', 'null') else 0.0
        except (ValueError, TypeError):
            return 0.0

    # 【M1】占用墙改为「上一单完工 + changeover(上一单物料, 本单物料)」：
    # 同物料连产不再白白预留一次换型，异物料按换型矩阵（无矩阵回退本单自身换型时间）。
    avail_raw = None  # 上一单完工时刻（不含换型）
    prev_mat = None   # 上一单物料

    for r in rows:
        mat = r.get('material_code')
        ps0 = _wo_parse_dt(r.get('planned_start'))
        if ps0 is None:
            continue
        # 【P1-8】锁定判定包含 M6 人工锁定（manual_locked=1）
        is_locked = (r.get('status') in ('批准', '执行')) or (str(r.get('manual_locked') or 0) == '1')
        # 本单开工下限 = 上一单完工 + 序列相关换型时长
        if avail_raw is not None and prev_mat:
            floor_at = avail_raw + timedelta(hours=_wo_changeover_between(prev_mat, mat))
        else:
            floor_at = avail_raw

        # 锁定单：仅作为占用墙，更新墙，但绝不改写自身
        if is_locked:
            pe0 = _wo_parse_dt(r.get('planned_end'))
            le = pe0 if pe0 else ps0
            if avail_raw is None or le > avail_raw:
                avail_raw = _skip_maint(le)
                prev_mat = mat
            continue

        # 非锁定单（创建）：前向顺排，基于占用墙（含锁定单墙）
        ps = ps0
        pushed = False
        if floor_at is not None and ps < floor_at:
            ps = floor_at
            pushed = True
        ps = _skip_maint(ps)
        if ps != ps0:
            pushed = True
        # 计算计划完工
        conf = r.get('confirm_qty'); comp = r.get('completed_qty'); plan = r.get('plan_qty')
        unfinished = max(0.0, _f(conf) - _f(comp))
        qty = unfinished if unfinished > 0 else (_f(plan) if plan not in (None, '', 'null') else 0.0)
        # 设备即 eq_name → 工期按该设备产能换算（开关默认关 → 与日产量口径一致）
        lead = _wo_lead_days_eq(qty, mat, eq_name) if qty > 0 else None
        pe0 = _wo_parse_dt(r.get('planned_end'))
        if pushed or pe0 is None:
            pe = (ps + timedelta(days=lead)) if lead else (pe0 if pe0 is not None else ps)
        else:
            pe = pe0
        # 【M1】墙 = 本单完工（换型由下一单按序列相关自行叠加，见 floor_at）
        avail_raw = _skip_maint(pe)
        prev_mat = mat
        # 仅回写实际变化
        if ps != ps0 or pe != pe0:
            execute_db(
                "UPDATE work_orders SET planned_start=%s, planned_end=%s, updated_at=NOW() WHERE id=%s",
                (ps.strftime('%Y-%m-%d %H:%M:%S'), pe.strftime('%Y-%m-%d %H:%M:%S'), r['id']))


def _wo_auto_end(data, old=None):
    """根据「未完工数量 + 计划开工」自动计算 planned_end（计划完工）并覆盖传入值。
    未完工数量 = confirm_qty - completed_qty（NULL 当作 0）。
    仅当 未完工数量 / 计划开工 / 物料 任一变化（或新建）且 计划开工非空 时重算，避免无故覆盖手填值。
    """
    mat = data.get('material_code') or (old or {}).get('material_code')
    if not mat:
        return data

    def _f(x):
        try:
            return float(x) if x not in (None, '', 'null') else 0.0
        except (ValueError, TypeError):
            return 0.0

    conf = data.get('confirm_qty')
    comp = data.get('completed_qty')
    if conf is None and old:
        conf = old.get('confirm_qty')
    if comp is None and old:
        comp = old.get('completed_qty')
    unfinished = max(0.0, _f(conf) - _f(comp))
    # 未完工为 0（未录入 confirm_qty/completed_qty）时回退计划数量 —— 与一键排产
    # (`qty = unfinished if unfinished > 0 else plan_qty`) 及 _wo_apply_plan_req_date 同口径。
    # 原实现缺此回退 → 只改计划开工、未传计划完工时 lead 算不出来，完工保持旧值，
    # 产生「计划完工早于计划开工」的脏数据（M6 锁定后该错误窗口会被长期占位）。
    if unfinished <= 0:
        unfinished = _f(data.get('plan_qty') if data.get('plan_qty') is not None
                        else (old or {}).get('plan_qty'))

    start = data.get('planned_start')
    if start is None and old:
        start = old.get('planned_start')
    if not start:
        return data

    old_unfinished = None
    old_start = None
    old_mat = None
    if old:
        old_unfinished = max(0.0, _f(old.get('confirm_qty')) - _f(old.get('completed_qty')))
        old_start = old.get('planned_start')
        old_mat = old.get('material_code')
    recalc = (old is None) or (unfinished != old_unfinished) or (str(start) != str(old_start)) or (mat != old_mat)
    if not recalc:
        return data

    # 若调用方已显式传入计划完工（如排产甘特图拖动、编辑表单已按提前期补算），
    # 则尊重其传入值（含具体时分秒的时间戳），不再用 start+提前期 覆盖，避免冲掉拖动算出的含时间计划完工。
    if data.get('planned_end'):
        return data

    # 设备已确定时按该设备产能换算工期（开关默认关 → 与日产量口径一致）
    _eq_for_lead = (data.get('workcenter_name') or (old or {}).get('workcenter_name') or '').strip()
    lead = _wo_lead_days_eq(unfinished, mat, _eq_for_lead)
    if lead is None or lead <= 0:
        return data
    try:
        ps = datetime.strptime(str(start).replace('T', ' ')[:19], '%Y-%m-%d %H:%M:%S')
        pe = ps + timedelta(days=lead)
        data['planned_end'] = pe.strftime('%Y-%m-%d %H:%M:%S')
    except (ValueError, TypeError):
        pass
    return data


def _plan_req_date_enabled():
    """读取「计划需求日期」开关（系统配置 → 计算逻辑 → 排程模块开关）。"""
    try:
        return get_config('module_%s' % PLAN_REQ_DATE_MODULE, '0') == '1'
    except Exception:
        return False


def _wo_apply_plan_req_date(data, old=None, only_if_provided=False):
    """「计划需求日期」开关启用时：**新增/编辑工单**这一步按新原则处理计划日期。

    用户拍板（2026-10-03）：
      ① 只是新增工单遵循新原则，以前工单不变 —— 一键计算永不改写已有工单（见
         schedule._enforce_plan_req_date，工单部分已移除），本函数只在新增/编辑动作时执行。
      ② 时分秒也要同步重新计算 —— 不再沿用原时分秒（会出现开工=完工、工期被压扁），
         改为：计划开工 = 当日 08:00:00；计划完工 = 计划开工 + 工期。

    处理规则：
      开工 < 当日 或 完工 < 当日 →
          新开工 = 当日 08:00:00（原开工已 >= 当日 则保留原开工时刻，仅重算完工）
          工期   = 优先 ceil(未完工数量 / 日产量 production_time)，缺失回退 production_cycle；
                   两者都无法计算时回退「原完工 − 原开工」时长（保证工期不被压缩为 0）
          新完工 = 新开工 + 工期
          delayed = 1（界面红点 / 导出"是"）
      开工、完工均 >= 当日 → delayed = 0（清除历史延误标记）

    :param only_if_provided: True 时仅当请求体显式包含 planned_start/planned_end 才处理
                             （PUT 部分更新用：避免"改个备注"顺带改动日期）
    :return: (data, adjusted) —— adjusted 表示是否发生了日期调整
    """
    if not _plan_req_date_enabled():
        return data, False
    if only_if_provided and not ('planned_start' in data or 'planned_end' in data):
        return data, False

    old = old or {}
    ps = _wo_parse_dt(data.get('planned_start') or old.get('planned_start'))
    pe = _wo_parse_dt(data.get('planned_end') or old.get('planned_end'))
    if ps is None and pe is None:
        return data, False
    if ps is None:
        ps = pe
    if pe is None:
        pe = ps

    # 【P1-5 修复·2026-10-14】「当日」基准统一为 biz_today()（固定 +08:00），
    # 与排程侧 _biz_today() 同源；原 date.today() 依赖 OS 时区，服务器非 +08:00
    # 时 WO 归位（delayed 标记/开工重置）会比排程归位早或晚一天。
    today = biz_today()
    if ps.date() >= today and pe.date() >= today:
        # 计划日期均已不早于当日 → 清除延误标记（仅开关启用时）
        data['delayed'] = 0
        return data, False

    # 新开工：原开工已 >= 当日则保留（含用户填的时分秒），否则重置为当日 08:00:00
    new_start = ps if ps.date() >= today else datetime(today.year, today.month, today.day,
                                                       PLAN_REQ_START_HOUR, 0, 0)

    # 工期：优先按 未完工数量 / 日产量 折算；无法计算则保持原工期时长
    def _f(x):
        try:
            return float(x) if x not in (None, '', 'null') else 0.0
        except (ValueError, TypeError):
            return 0.0

    mat = data.get('material_code') or old.get('material_code')
    conf = data.get('confirm_qty') if data.get('confirm_qty') is not None else old.get('confirm_qty')
    comp = data.get('completed_qty') if data.get('completed_qty') is not None else old.get('completed_qty')
    unfinished = max(0.0, _f(conf) - _f(comp))
    if unfinished <= 0:
        unfinished = _f(data.get('plan_qty') if data.get('plan_qty') is not None else old.get('plan_qty'))

    span = None
    # 设备已指定时按该设备产能换算工期（开关默认关 → 与日产量口径一致）
    _eq_here = (data.get('workcenter_name') or (old or {}).get('workcenter_name') or '').strip()
    lead = _wo_lead_days_eq(unfinished, mat, _eq_here)
    if lead and lead > 0:
        span = timedelta(days=lead)
    elif pe > ps:
        span = pe - ps          # 回退：保持原工期长度（避免被压成同一时刻）
    else:
        span = timedelta(0)
    new_end = new_start + span

    data['planned_start'] = new_start.strftime('%Y-%m-%d %H:%M:%S')
    data['planned_end'] = new_end.strftime('%Y-%m-%d %H:%M:%S')
    data['delayed'] = 1
    return data, True


def _wo_parse_date_flex(v):
    """宽松解析日期：支持 'YYYYMMDD' / 'YYYY-MM-DD' / 'YYYY-MM-DD HH:MM:SS' → datetime(00:00)。"""
    if not v:
        return None
    s = str(v).strip()
    if not s:
        return None
    if s.isdigit() and len(s) == 8:
        try:
            return datetime(int(s[0:4]), int(s[4:6]), int(s[6:8]))
        except (ValueError, TypeError):
            pass
    s2 = s.replace('T', ' ')
    for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%d'):
        try:
            return datetime.strptime(s2[:19] if ' ' in s2 else s2[:10], fmt)
        except (ValueError, TypeError):
            continue
    return None


def _wo_cust_level(cust_level, ps_customer_priority=None, prod_priority=None):
    """客户等级解析（用于排序）：优先取『客户管理』中的客户等级(customers.priority, 数值越大越优先)；
    缺失时退化为 peacock_schedule.customer_priority；再缺失为 products.priority(高/中/低) 等级；再缺失为 0。"""
    try:
        if cust_level is not None and str(cust_level).strip() != '':
            f = float(cust_level)
            if f > 0:
                return f
    except (ValueError, TypeError):
        pass
    try:
        if ps_customer_priority is not None and str(ps_customer_priority).strip() != '':
            return float(ps_customer_priority)
    except (ValueError, TypeError):
        pass
    return {'高': 3, '中': 2, '低': 1}.get((prod_priority or '').strip(), 0)


def _wo_earliest_start(base, lead_days, co_new, occ_list):
    """在 base 之后，找到第一个与占用区间不重叠的开工时刻（修复 P2-16）。

    occ_list 中各区间为 [start, end+转产]（半开区间语义：新工单占用
    [es, es+工期+转产)，与任一占用区间相交即冲突）。

    【P2-4 命名统一·2026-10-14】形参 co_new_h → co_new：与调用侧变量、
    计算规则.md 文档口径统一（换型时间，单位小时；"_h" 后缀曾造成
    "形参/调用变量/文档" 三名不一致）。

    【旧实现的两处缺陷】
    1. 复杂度 O(迭代数 × n)：「遇冲突→跳到该区间末尾→重试」，冲突密集时退化到万次循环。
    2. **达到 10000 次上限后静默返回当前 es** —— 该位置仍可能与占用重叠，
       排产结果悄悄变成「设备同一时段排了两张单」，而日志与界面**均无告警**。

    【新实现】
    - 先走 O(n) 快速路径：若 [base, base+工期+转产) 本就不冲突，直接返回 base（最常见场景，免排序）。
    - 冲突时才排序 + 合并 + 单趟扫描，整体 O(n log n)，且**必然返回真正的首个可行空隙**。
      时间轴向后无限长、occ_list 有限 → 解一定存在，不存在「超限失败」，故无需再抛异常。
    """
    span = timedelta(days=lead_days or 0, hours=co_new or 0)
    if span < timedelta(0):
        span = timedelta(0)

    # 过滤无效区间（None / 结构异常），保证后续比较安全
    occ = []
    for it in (occ_list or []):
        try:
            ls, le = it[0], it[1]
        except (TypeError, IndexError, ValueError):
            continue
        if ls is None or le is None:
            continue
        occ.append((ls, le))

    # 快速路径：base 处本就不冲突 → 直接返回，省去排序
    end0 = base + span
    if not any(base < le and end0 > ls for (ls, le) in occ):
        return base

    # 慢速路径：按起点排序 → 合并重叠/相接区间 → 单趟扫描找首个能容下 span 的空隙
    occ.sort(key=lambda x: x[0])
    merged = []
    for (ls, le) in occ:
        if merged and ls <= merged[-1][1]:
            if le > merged[-1][1]:
                merged[-1] = (merged[-1][0], le)
        else:
            merged.append((ls, le))

    cursor = base
    for (ls, le) in merged:
        if le <= cursor:
            continue                    # 区间整体在游标之前，已越过
        if (cursor + span) <= ls:
            return cursor               # 游标与该区间之间容得下整段工期
        cursor = le                     # 被挡住 → 推到该区间末尾继续找
    return cursor                       # 所有占用之后必有无限空间


def _wo_maintenance_occ():
    """设备『保养/维修』占用带（产能真实性 D）。

    取 equipment 中 status IN ('保养','维修') 且 start_time/end_time 完整的记录，
    按设备名（= work_orders.workcenter_name 同口径）归集为 [(start_dt, end_dt), ...]。
    任一排产（一键排产 / 手工排产 / 级联顺排）均不得与该带重叠。
    """
    rows = query_db(
        "SELECT name, start_time, end_time FROM equipment "
        "WHERE status IN ('保养','维修') AND start_time IS NOT NULL AND end_time IS NOT NULL")
    occ = {}
    for r in rows:
        nm = (r.get('name') or '').strip()
        if not nm:
            continue
        ps = _wo_parse_dt(r.get('start_time'))
        pe = _wo_parse_dt(r.get('end_time'))
        if ps and pe:
            occ.setdefault(nm, []).append((ps, pe))
    return occ


def _wo_overlaps_maint(eq_name, ps, pe, maint_occ=None):
    """若 [ps, pe] 与 eq_name 的任一保养/维修带重叠，返回该带 (ms, me)，否则返回 None。"""
    if not eq_name or ps is None or pe is None or pe <= ps:
        return None
    if maint_occ is None:
        maint_occ = _wo_maintenance_occ()
    for (ms, me) in maint_occ.get(eq_name, []):
        if ps < me and pe > ms:  # 两闭区间相交
            return (ms, me)
    return None


def _wo_norm_dt_str(v):
    """把日期/时间值统一成 'YYYY-MM-DD HH:MM:SS' 字符串，用于"是否被改写"比对。"""
    if v is None:
        return ''
    if isinstance(v, datetime):
        return v.strftime('%Y-%m-%d %H:%M:%S')
    return str(v).replace('T', ' ').strip()[:19]


def _wo_transit_pools():
    """采购在途桶：purchase_orders status='在途' 的（计划到货日期, 计划到货数量），按物料归集、按日期升序。

    口径与 schedule._load_transit_pools 一致，但显式排除 plan_arrival_date 为空的单：
    用户已拍板"无到货日期的在途单不计入可用"（见计算规则 §问题4，schedule.py:208/:1006 共用判定）。
    返回 {material_code: [(date, qty), ...]}。
    """
    rows = query_db(
        "SELECT material_code, plan_arrival_date, arrival_qty FROM purchase_orders "
        "WHERE status='在途' AND plan_arrival_date IS NOT NULL "
        "AND arrival_qty IS NOT NULL AND arrival_qty > 0")
    pools = {}
    for r in (rows or []):
        mat = (r.get('material_code') or '').strip()
        if not mat:
            continue
        d = _wo_parse_date_flex(r.get('plan_arrival_date'))
        if d is None:
            continue
        try:
            qty = float(r.get('arrival_qty') or 0)
        except (ValueError, TypeError):
            continue
        if qty <= 0:
            continue
        pools.setdefault(mat, []).append((d, qty))
    for mat in pools:
        pools[mat].sort(key=lambda x: x[0])
    return pools


def _wo_cover_date(buckets, need):
    """按 (日期, 数量) 升序累计，返回首次覆盖 need 的日期；无法覆盖返回 None。

    采购在途（S5）与自制件排程产出（M3）共用同一套"累计覆盖"口径。
    """
    if not buckets or not need or need <= 0:
        return None
    acc = 0.0
    for (d, q) in buckets:
        acc += q
        if acc >= need:
            return d
    return None


def _wo_prod_pools():
    """【M3】自制件（半成品/产成品）排程产出桶：{物料: [(计划完工, 可产出量), ...]}，按完工日升序。

    来源：work_orders 中 status IN ('创建','批准','执行') 且 planned_end 非空的工单。
    可产出量 = 未完工量（confirm_qty − completed_qty），≤0 时回退 plan_qty（与排产同口径）。

    用途：自制半成品的"可用日"应来自**它自己的排程完工日**，而不是只有采购件才参与齐套判定。
    这样父件才不会排到子件产出之前 —— 即 MRP 与排产的闭环（CTP）。
    """

    def _f(x):
        try:
            return float(x) if x not in (None, '', 'null') else 0.0
        except (ValueError, TypeError):
            return 0.0

    rows = query_db(
        "SELECT material_code, planned_end, confirm_qty, completed_qty, plan_qty FROM work_orders "
        "WHERE status IN ('创建','批准','执行') AND material_code IS NOT NULL AND material_code <> '' "
        "AND planned_end IS NOT NULL")
    pools = {}
    for r in (rows or []):
        mat = (r.get('material_code') or '').strip()
        if not mat:
            continue
        d = _wo_parse_dt(r.get('planned_end'))
        if d is None:
            continue
        unfinished = max(0.0, _f(r.get('confirm_qty')) - _f(r.get('completed_qty')))
        q = unfinished if unfinished > 0 else _f(r.get('plan_qty'))
        if q <= 0:
            continue
        pools.setdefault(mat, []).append((d, q))
    for mat in pools:
        pools[mat].sort(key=lambda x: x[0])
    return pools


def _wo_semi_floor_enabled():
    """【M3】自制件排程产出日是否回流为父件开工 floor（默认 '1' 开启）。

    关闭后回到改动前口径：只有采购类物料参与 mat_avail 推算。

    【性能 · P2-11 修复 2026-10-14】原实现**每次调用都读一次 system_config**，
    而它在「物料可用日」推算里对每个物料各调一次 → N+1 查库（与 `_co_matrix_enabled()`
    当年"退火慢 65 倍"同源）。现照抄后者的模块级缓存模式：由 `_reload_wo_caches()`
    在每轮排产开始时置空一次，之后全部走内存。
    """
    global _SEMI_FLOOR_ON
    if _SEMI_FLOOR_ON is None:
        try:
            _SEMI_FLOOR_ON = get_config('module_%s' % SEMI_FLOOR_MODULE, '1') == '1'
        except Exception:
            _SEMI_FLOOR_ON = True
    return _SEMI_FLOOR_ON


def _wo_shortage_snapshot():
    """缺料明细快照（由「一键计算」生成）取数 + 新鲜度。

    返回 (comp_need, meta)：
      comp_need : {component_code: 缺口合计}（shortage_type IN ('PARTIAL','MISSING')）
      meta      : {'available': bool, 'generated_at': str|None, 'age_days': int|None, 'rows': int}

    available=False 表示快照不存在 → 物料齐套约束不生效；此时严禁把工单判成『OK』（S4），
    否则用户会误以为物料齐套。
    """
    rows = query_db(
        "SELECT component_code, "
        "SUM(COALESCE(need_qty, GREATEST(COALESCE(required_qty, 0) - COALESCE(available_qty, 0), 0))) AS need "
        "FROM peacock_material_shortage "
        "WHERE shortage_type IN ('PARTIAL','MISSING') AND component_code IS NOT NULL AND component_code <> '' "
        "GROUP BY component_code")
    comp_need = {}
    for r in (rows or []):
        c = (r.get('component_code') or '').strip()
        if not c:
            continue
        try:
            comp_need[c] = max(0.0, float(r.get('need') or 0))
        except (ValueError, TypeError):
            comp_need[c] = 0.0
    meta_row = query_db(
        "SELECT COUNT(*) AS cnt, MAX(created_at) AS gen_at FROM peacock_material_shortage", one=True)
    cnt = int((meta_row or {}).get('cnt') or 0)
    gen_at = (meta_row or {}).get('gen_at')
    gen_str = None
    age_days = None
    if gen_at is not None:
        try:
            gen_dt = gen_at if isinstance(gen_at, datetime) else _wo_parse_dt(gen_at)
            if gen_dt is not None:
                gen_str = gen_dt.strftime('%Y-%m-%d %H:%M:%S')
                # 【P1-5 修复·2026-10-14】年龄基准用 +08:00 墙钟（biz_now），
                # 与 biz_today 同源；原 datetime.now() 依赖 OS 时区。
                age_days = (biz_now() - gen_dt).days
        except (ValueError, TypeError):
            pass
    return comp_need, {'available': cnt > 0, 'generated_at': gen_str,
                       'age_days': age_days, 'rows': cnt}


def _wo_mat_ready_date(comp, need, transit_pools, today0, lead_days):
    """组件 comp 的最早可用日（S5：优先用采购在途的真实计划到货日）。

      ① 在途桶按到货日期升序累计，取『累计到货量 ≥ 缺口』的最早日期 → 该日即可用；
      ② 在途不足或无在途 → 回退『今天 + 采购提前期』（与改动前口径一致，保证不会变差）；
      ③ 采购提前期也取不到 → 返回 None（不约束）。
    """
    if need is None or need <= 0:
        return None
    acc = 0.0
    for (d, qty) in ((transit_pools or {}).get(comp) or []):
        acc += qty
        if acc >= need - 1e-6:
            return d if d > today0 else today0
    if lead_days and lead_days > 0:
        return today0 + timedelta(days=lead_days)
    return None


def _wo_sched_kpi(assigned, fmap, eq_load):
    """排产 KPI（S3）：基于**落库后**的真实计划时间核算（不用优化阶段的评估值）。

    fmap: {工单id: 落库行}；eq_load: {设备名: 累计负荷(天)}（含锁定单与本轮派单）。
    """
    total = len(assigned or [])
    overdue = 0
    tardiness = 0.0
    weighted = 0.0
    starts = []
    ends = []
    for r in (assigned or []):
        fr = (fmap or {}).get(r['oid']) or {}
        ps = _wo_parse_dt(fr.get('planned_start'))
        pe = _wo_parse_dt(fr.get('planned_end'))
        r['_final_ps'] = ps
        r['_final_pe'] = pe
        if ps is not None:
            starts.append(ps)
        if pe is not None:
            ends.append(pe)
        due = r.get('due')
        if pe is not None and due is not None and pe > due:
            overdue += 1
            d = (pe - due).total_seconds() / 86400.0
            tardiness += d
            try:
                w = float(r.get('cust') or 0)
            except (ValueError, TypeError):
                w = 0.0
            weighted += d * (w if w > 0 else 1.0)
    changeovers = 0
    co_hours = 0.0
    by_eq = {}
    for r in (assigned or []):
        if r.get('_final_ps') is None:
            continue
        by_eq.setdefault(r.get('eq'), []).append(r)
    for _eq, lst in by_eq.items():
        lst.sort(key=lambda x: x['_final_ps'])
        prev_mat = None
        for r in lst:
            if prev_mat is not None and prev_mat != r.get('mat'):
                changeovers += 1
                try:
                    co_hours += float(r.get('co_new') or 0)
                except (ValueError, TypeError):
                    pass
            prev_mat = r.get('mat')
    loads = sorted(((eq_load or {}).values()), reverse=True)
    peak_eq = None
    if eq_load:
        peak_eq = max(eq_load, key=lambda k: eq_load[k])
    span = 0.0
    if starts and ends:
        span = (max(ends) - min(starts)).total_seconds() / 86400.0
    return {
        'scheduled': total,
        'overdue': overdue,
        'on_time_rate': (round((total - overdue) * 100.0 / total, 1) if total else 100.0),
        'tardiness_days': round(tardiness, 2),
        'weighted_tardiness': round(weighted, 2),
        'changeovers': changeovers,
        'changeover_hours': round(co_hours, 2),
        'makespan_days': round(span, 2),
        'peak_equipment': peak_eq,
        'peak_load_days': (round(loads[0], 2) if loads else 0.0),
        'load_spread_days': (round(loads[0] - loads[-1], 2) if len(loads) > 1 else 0.0),
    }


def _wo_seq_conflicts(assigned, static_occ, maint_occ):
    """【L1】落库前对解码结果做硬不变式自检，返回存在冲突的设备集合。

    检查两条：
      ① 同一设备内按开工排序后，相邻两单满足
         开工[i] ≥ 完工[i-1] + changeover(物料[i-1], 物料[i])   （换型间隔）
      ② 任一单的 [开工, 完工] 不与本设备保养/维修带重叠

    解码器是**构造性**满足这两条的，故正常应返回空集；非空意味着解码口径
    被外部改动打破（或存量数据本身就有重叠），此时调用方应回退到级联顺排兜底。
    """
    bad = set()
    eps = timedelta(seconds=1)
    by_eq = {}
    for r in assigned:
        if r.get('es') is None or r.get('pe') is None or not r.get('eq'):
            continue
        by_eq.setdefault(r['eq'], []).append((r['es'], r['pe'], r.get('mat')))
    for eq, lst in by_eq.items():
        for (ls, le, lm) in (static_occ or {}).get(eq, []):
            lst.append((ls, le, lm))
        lst.sort(key=lambda x: (x[0], str(x[2] or '')))
        prev = None
        for (ps, pe, mat) in lst:
            if prev is not None:
                need = prev[1] + timedelta(hours=_wo_changeover_between(prev[2], mat))
                if ps + eps < need:
                    bad.add(eq)
                    break
            prev = (ps, pe, mat)
        if eq in bad:
            continue
        for (ms, me) in (maint_occ or {}).get(eq, []):
            if any(ps < me and pe > ms for (ps, pe, _m) in lst):
                bad.add(eq)
                break
    return bad


def _wo_invariant_check(assigned, fmap, locked_snap, maint_occ):
    """排产结果不变式自检（S2）：返回冲突描述列表（空列表 = 全部通过）。

      ① 本轮排产工单与同设备其它工单（含 批准/执行 锁定单）时间区间重叠；
      ② 本轮排产工单落在设备『保养/维修』带内；
      ③ 锁定单（批准/执行）的 planned_start/planned_end 被改写。
    """
    warns = []
    if not assigned:
        return warns
    eqs = sorted({r.get('eq') for r in assigned if r.get('eq')})
    for eq in eqs:
        others = query_db(
            "SELECT id, material_code, status, planned_start, planned_end FROM work_orders "
            "WHERE workcenter_name=%s AND planned_start IS NOT NULL AND planned_end IS NOT NULL",
            (eq,))
        for r in assigned:
            if r.get('eq') != eq:
                continue
            fr = (fmap or {}).get(r['oid'])
            if fr is None:
                continue
            ps = _wo_parse_dt(fr.get('planned_start'))
            pe = _wo_parse_dt(fr.get('planned_end'))
            if ps is None or pe is None:
                continue
            for o in (others or []):
                if o.get('id') == r['oid']:
                    continue
                ops = _wo_parse_dt(o.get('planned_start'))
                ope = _wo_parse_dt(o.get('planned_end'))
                if ops is None or ope is None:
                    continue
                if ps < ope and pe > ops:
                    warns.append('工单 %s 与工单 %s 在设备「%s」上时间重叠（%s ~ %s 对 %s ~ %s）' % (
                        r.get('order_no') or r['oid'], o.get('id'), eq,
                        ps.strftime('%Y-%m-%d %H:%M'), pe.strftime('%Y-%m-%d %H:%M'),
                        ops.strftime('%Y-%m-%d %H:%M'), ope.strftime('%Y-%m-%d %H:%M')))
                    break
            for (ms, me) in ((maint_occ or {}).get(eq) or []):
                if ps < me and pe > ms:
                    warns.append('工单 %s 落在设备「%s」保养/维修带内（%s ~ %s）' % (
                        r.get('order_no') or r['oid'], eq,
                        ms.strftime('%Y-%m-%d %H:%M'), me.strftime('%Y-%m-%d %H:%M')))
                    break
    if locked_snap:
        lids = list(locked_snap.keys())
        lph = ','.join(['%s'] * len(lids))
        cur = query_db("SELECT id, planned_start, planned_end FROM work_orders WHERE id IN (%s)" % lph,
                       tuple(lids))
        for r in (cur or []):
            old = locked_snap.get(r.get('id'))
            if old is None:
                continue
            if (_wo_norm_dt_str(r.get('planned_start')), _wo_norm_dt_str(r.get('planned_end'))) != old:
                warns.append('锁定工单 %s（批准/执行）的计划时间被改写，违反「只动创建」规则' % r.get('id'))
    return warns


def _wo_plan():
    """【L3】排产内核（**只读试算**）：算出「怎么排」，但**不写库**。

    返回 dict；empty=True 表示没有可排工单。
    落库 / 落库后复检 / KPI / 回执组装，一律由调用方负责：
      · work_orders_auto_schedule() —— 一键排产（落库 + 复检 + 回执）
      · work_orders_what_if()       —— 沙盘试算（只读，返回与现状的差异）

    【为什么抽出来】改动前这段 500+ 行计算与 HTTP 写库混在路由里，任何
    "先看效果再决定"的诉求（沙盘、方案对比、参数回放）都只能真跑一次排产、
    把结果写进 work_orders 再看 —— 试错成本等于污染生产数据。
    """
    # 【P1-5 修复·2026-10-14】排产「今日」基准统一为 biz_midnight()（+08:00 当日 00:00），
    # 与排程侧同源；原 datetime.now() 依赖 OS 时区，时区不同则 JIT/缓冲窗口整体偏移。
    today0 = biz_midnight()
    _wo_ensure_cols()
    _reload_wo_caches()  # P1-9：一次性加载 products/bom 到内存，后续查询走缓存
    # 【L4】TOC/DBR 结果容器（即便无可排单也要返回，供调用方展示）
    # 🔴【P2-10 修复 · 2026-10-14】原实现把本块放在 `_reload_wo_caches()` **之前**，
    # 而 `_toc_enabled()/_toc_buffer_days()` 读的是 system_config。若这些开关/配置是
    # 本轮刚被用户改过、且依赖缓存，则本轮排产用的是**上一轮的值** →
    # 表现为"改了 TOC 开关第一次排产没反应，第二次才生效"。
    # 现把缓存刷新上移，保证开关读取发生在最新缓存之后。
    toc = _wo_empty_toc()

    # 设备 ID -> 看板设备名（_reload_wo_caches 已加载 equipment，避免重复查库）
    eq_name_by_id = {r['id']: nm for nm, r in (_EQ_ROWS or {}).items() if r.get('id')}
    # ===== 不可用设备不参与排产（2026-10-07 用户拍板）=====
    #  · status='不可用'：一律排除（设备已不能使用，派单上去现场执行不了）；
    #  · status IN ('保养','维修')：不排除，但必须占用其时间带（见 maint_occ）；
    #    缺 start_time/end_time 的设备无法界定可用窗口 → 一并排除并回执告警。
    # 原实现完全未过滤 equipment.status，维修/保养/不可用设备都可能被派单。
    excluded_eq = set()
    for nm, r in (_EQ_ROWS or {}).items():
        st = (r.get('status') or '').strip()
        if st == EQ_STATUS_UNAVAILABLE:
            excluded_eq.add(nm)
        elif st in EQ_STATUS_MAINT and not (_wo_parse_dt(r.get('start_time'))
                                            and _wo_parse_dt(r.get('end_time'))):
            excluded_eq.add(nm)

    # 锁定工单（批准/执行）占用区间：[start, end + 转产]，按设备归集
    # 【P2-15 修复 · 2026-10-14】原 WHERE 要求 `planned_end IS NOT NULL`，
    # 使"有开工、无完工"的锁定单**完全脱离占用墙**且无任何告警 —— 同设备同时段
    # 可被排进另一张单并落库（时间重叠），现场执行时才发现冲突。
    # 现改为只要求 planned_start 非空；planned_end 缺失时按"开工 + 该物料工期"兜底，
    # 工期也算不出则退化为"开工起 0 长度"（至少占位不重叠），并记录一次告警。
    locked = query_db(
        "SELECT id, material_code, workcenter_name, planned_start, planned_end "
        "FROM work_orders WHERE status IN ('批准','执行') AND workcenter_name IS NOT NULL "
        "AND planned_start IS NOT NULL")
    locked_occ = {}
    # 【S2】锁定单排产前的计划时间快照，用于校验"批准/执行 永不被改写"
    locked_snap = {r['id']: (_wo_norm_dt_str(r.get('planned_start')),
                             _wo_norm_dt_str(r.get('planned_end'))) for r in (locked or [])}
    # 【M1】区间改为 (开工, 完工, 物料)：尾部换型不再在构造时固定，
    # 而是在拼接占用墙时按「候选单物料」动态计算 changeover(本单物料, 候选单物料)
    # —— 同物料接续时为 0，这才拿得到"同物料连产零换型"的产能收益。
    for r in locked:
        eq = r['workcenter_name']
        ps = _wo_parse_dt(r['planned_start'])
        if not ps:
            continue
        pe = _wo_parse_dt(r['planned_end'])
        if not pe:
            # planned_end 缺失 → 用"开工 + 该物料工期"兜底，保证不脱离占用墙
            _q = max(0.0, float(r.get('confirm_qty') or 0) - float(r.get('completed_qty') or 0)) \
                if (r.get('confirm_qty') is not None or r.get('completed_qty') is not None) else 0.0
            _ld = _wo_lead_days(_q, r.get('material_code')) if _q > 0 else None
            pe = ps + timedelta(days=_ld) if _ld else ps
            logging.getLogger(__name__).warning(
                '锁定工单 %s 缺 planned_end，占用墙按「开工+工期%s天」兜底（%s）；'
                '建议补全该工单的计划完工日期', r.get('id'), _ld or 0,
                pe.strftime('%Y-%m-%d %H:%M:%S'))
        locked_occ.setdefault(eq, []).append((ps, pe, r['material_code']))

    # 已排创建单占用区间：[id, start, end + 转产]，按设备归集。
    # 作用：二次运行 / 含手动调整过的创建单时，把它们也当作占用墙，
    # 避免本轮新单从其 today0 起排而压到旧窗口上造成重叠。
    # 放置该单“自身”时按 id 排除其自身窗口（见设备循环 occ 拼接），避免自锁。
    create_occ = {}
    # 【P2-15】同上：已排创建单也改为"只要求开工非空"，缺完工时按工期兜底，
    # 否则一张只有开工的创建单不进占用墙 → 本轮新单会压到它身上造成重叠。
    for r in query_db(
        "SELECT id, material_code, workcenter_name, planned_start, planned_end "
        "FROM work_orders WHERE status='创建' AND workcenter_name IS NOT NULL "
        "AND planned_start IS NOT NULL"):
        eq = r['workcenter_name']
        ps = _wo_parse_dt(r['planned_start'])
        if not ps:
            continue
        pe = _wo_parse_dt(r['planned_end'])
        if not pe:
            _q = max(0.0, float(r.get('confirm_qty') or 0) - float(r.get('completed_qty') or 0)) \
                if (r.get('confirm_qty') is not None or r.get('completed_qty') is not None) else 0.0
            _ld = _wo_lead_days(_q, r.get('material_code')) if _q > 0 else None
            pe = ps + timedelta(days=_ld) if _ld else ps
        create_occ.setdefault(eq, []).append((r['id'], ps, pe, r['material_code']))

    def _occ_for_eq(eq, mat, excl_wid=None):
        """设备 eq 上、针对「待排物料 mat」的占用墙，区间为 [开工, 完工 + changeover(该单物料, mat)]。

        【M1】换型时长按前后物料序列相关计算：
          · 同物料 → 尾部预留 0（连产无需清洗），可直接接在上一单完工之后；
          · 异物料 → 用换型矩阵值，无矩阵记录回退该单自身 changeover_time（同改动前）。
        """
        occ = []
        for (ls, le, lm) in locked_occ.get(eq, []):
            occ.append((ls, le + timedelta(hours=_wo_changeover_between(lm, mat))))
        for (wid, ls, le, lm) in create_occ.get(eq, []):
            if excl_wid is not None and wid == excl_wid:
                continue
            occ.append((ls, le + timedelta(hours=_wo_changeover_between(lm, mat))))
        occ.extend(maint_occ.get(eq, []))
        return occ

    # 保养/维修占用带（产能真实性 D）：设备处于「保养/维修」且起止完整的，
    # 作为占用墙，任一排产均不得与其重叠（与 workcenter_name 同口径，按设备名归集）。
    maint_occ = _wo_maintenance_occ()

    # 待排工单（创建），关联交期/客户等级/设备/货架期(保质期)
    orders = query_db(
        "SELECT wo.id, wo.order_no, wo.material_code, wo.plan_qty, wo.confirm_qty, wo.completed_qty, "
        "wo.schedule_ref, wo.source, wo.client_name, wo.client_code, ps.batch_date_end, ps.customer_priority, "
        "cu.priority AS cust_level, "
        "p.production_equipment, p.type, p.priority AS prod_priority, p.shelf_life "
        "FROM work_orders wo "
        "LEFT JOIN peacock_schedule ps ON wo.schedule_ref = ps.aufnr "
        "LEFT JOIN products p ON wo.material_code = p.material_code "
        "LEFT JOIN customers cu ON (wo.client_name = cu.name OR wo.client_code = cu.code) "
        # 【M6】人工排产过的单（编辑过计划日期/产线、拖到产线、甘特图拖动）不参与重排。
        # 但它们**仍留在占用墙**（create_occ 不区分锁定），否则后续单会压到它们身上。
        "WHERE wo.status='创建' AND COALESCE(wo.manual_locked,0)=0")
    # 被锁定的创建单张数（仅用于回执提示，不参与本轮排产）
    locked_skipped = (query_db(
        "SELECT COUNT(*) AS cnt FROM work_orders "
        "WHERE status='创建' AND COALESCE(manual_locked,0)=1", one=True) or {}).get('cnt') or 0
    if not orders:
        return {'empty': True, 'today0': today0,
                'message': ('无可排工单：「创建」状态 %d 张均已被人工排产锁定' % locked_skipped)
                           if locked_skipped else '无「创建」状态工单可排',
                'locked_skipped': locked_skipped, 'assigned': [], 'decisions': [],
                'touched': set(), 'conflict_eqs': set(), 'eq_load': {}, 'maint_occ': {},
                'search_info': {}, 'missing_master': {}, 'excluded_eq': excluded_eq,
                'blocked_by_eq': 0, 'locked_snap': {},
                'mat_meta': {'available': False, 'generated_at': None,
                             'age_days': None, 'rows': 0},
                'stats': {'scheduled': 0, 'unscheduled': 0, 'overdue': 0, 'total': 0},
                'toc': _wo_empty_toc()}   # 【P3】与正常路径字段同构

    # ===== 物料可用日（方案C：软约束 + 可靠供给floor + 待料标记）=====
    # 从缺料明细快照(peacock_material_shortage)反推：哪些组件短缺(PARTIAL/MISSING)。
    # 注：快照由「一键计算」生成；若未跑过则 comp_short 为空→不约束（退化同原逻辑）。
    # 采购提前期取 products.production_cycle（物料管理.采购周期，仅 原辅料/包材/备品备件 有意义；
    # 产成品/半成品 此字段为空，不参与物料可用日推算）。
    # 【2026-09-01 修正】原条件为 IN ('PARTIAL','SHORT')，但 shortage_type 实际取值只有
    #   OK / PARTIAL / MISSING（见 schedule.py 写入处），'SHORT' 从未被写入 → 该条件永远只能
    #   命中 PARTIAL，导致"完全缺料(MISSING)的组件"反而不参与 mat_avail 推算、排产认为物料齐套。
    # 【S4】缺料快照取数 + 新鲜度。available=False（从未跑过一键计算）时：
    #       物料齐套约束不生效，工单物料状态一律记『未知』，**不得**显示『OK』，避免误判齐套。
    comp_need, mat_meta = _wo_shortage_snapshot()
    comp_short = set(comp_need.keys())
    # 【S5】采购在途的真实计划到货日（按物料归集、按日期升序），优先于“今天+采购提前期”粗算。
    transit_pools = _wo_transit_pools()
    # 【M3】自制件（半成品/产成品）的排程产出日，作为父件开工 floor 的另一类来源
    prod_pools = _wo_prod_pools()
    # 仅采购类物料（原辅料/包材/备品备件）参与“到货日”推算；lead_by_comp 已过滤类型。
    lead_rows = query_db(
        "SELECT material_code, production_cycle FROM products "
        "WHERE production_cycle IS NOT NULL AND production_cycle > 0 "
        "AND type IN ('原辅料','包材','备品备件')")
    lead_by_comp = {r['material_code']: float(r['production_cycle']) for r in lead_rows}

    _mat_avail_cache = {}  # 按物料缓存 BOM 展开结果（见 _mat_avail_for 内注释）

    def _mat_avail_for(mat):
        """反推物料 mat 的最早可开工日（物料齐套视角）：
        展开 BOM 取所有叶子件；仅『采购类(原辅料/包材/备品备件) + 短料』的叶子参与推算，
        到货日 = 今天 + 采购提前期；取各短料件到货日之 max。
        产成品/半成品 叶子（即便误作 BOM 末端）一律跳过——其采购周期为空，
        若纳入会算出 lead=0 的假“今天可开工”，故显式排除（选项B）。
        无采购类短料返回 None（即不约束）。"""
        # 同一次排产内 BOM 与缺料快照不变，按物料缓存展开结果；
        # 抵消 P1-5 改为“按路径展开”后共享子树的重复遍历开销（缓解 P1-9）。
        if mat in _mat_avail_cache:
            return _mat_avail_cache[mat]
        comps = _wo_explode_components(mat)
        if not comps:
            _mat_avail_cache[mat] = None
            return None
        ma = None
        semi_on = _wo_semi_floor_enabled()
        for c in comps:
            if c not in comp_short:
                continue
            if c in lead_by_comp:
                # 采购类（S5）：优先用采购在途的真实到货日累计覆盖缺口；覆盖不到才回退“今天+采购提前期”
                d = _wo_mat_ready_date(c, comp_need.get(c), transit_pools, today0, lead_by_comp[c])
            elif semi_on:
                # 自制件（M3）：可用日 = 它自己排程产出的累计覆盖日
                b = prod_pools.get(c) or []
                d = _wo_cover_date(b, comp_need.get(c))
                if d is None and b:
                    # 排程产出量不足以覆盖缺口 → 保守取最后一批完工日（等全部产出）
                    d = b[-1][0]
            else:
                continue      # 非采购类且开关关闭：改动前口径，跳过
            if d is not None and (ma is None or d > ma):
                ma = d
        _mat_avail_cache[mat] = ma
        return ma

    def _f(x):
        try:
            return float(x) if x not in (None, '', 'null') else 0.0
        except (ValueError, TypeError):
            return 0.0

    parsed = []
    best_row_for = {}  # 客户多匹配时，保留客户等级最高的那一行，避免随机取数
    seen_ids = set()
    for o in orders:
        oid = o.get('id')
        if oid in seen_ids:
            if _f(o.get('cust_level')) > _f(best_row_for[oid].get('cust_level')):
                best_row_for[oid] = o
            continue
        seen_ids.add(oid)
        best_row_for[oid] = o
    for o in best_row_for.values():
        mat = o['material_code']
        cands = []
        pe_raw = o.get('production_equipment')
        if pe_raw:
            for eid in str(pe_raw).split(','):
                eid = eid.strip()
                nm = eq_name_by_id.get(eid)
                # 不可用 / 缺时间带的保养维修设备，不作为候选
                if nm and nm not in cands and nm not in excluded_eq:
                    cands.append(nm)
        due = _wo_parse_date_flex(o.get('batch_date_end'))
        cust = _wo_cust_level(o.get('cust_level'), o.get('customer_priority'), o.get('prod_priority'))
        sh = _f(o.get('shelf_life'))
        shelf_days = sh if sh > 0 else 0
        shelf_floor = (due - timedelta(days=shelf_days)) if (shelf_days > 0 and due) else None
        mat_avail = _mat_avail_for(mat)  # 物料齐套视角最早可开工日；None=不约束
        parsed.append({'o': o, 'mat': mat, 'cands': cands,
                       'due': due, 'cust': cust, 'shelf_floor': shelf_floor,
                       'mat_avail': mat_avail,
                       # 物料有指定设备、但全被排除 → 用于区分"无可用设备"的原因
                       'cands_blocked': bool(pe_raw) and not cands})

    # 排序：客户等级 DESC，交期 ASC（同客户等级交期越近越先排）
    # 末位排序键用 str(id) 而非 id or 0：id 是 varchar(50) 主键（非数字），
    # 若与 int 0 混排会在 Python3 抛 TypeError；统一字符串比较同时保证排序确定性。
    parsed.sort(key=lambda x: (-x['cust'], x['due'] or datetime.max,
                               str(x['o'].get('order_no') or ''), str(x['o'].get('id') or '')))

    # 【M1】队尾跟踪拆成三份，支持"同物料连产零换型"：
    #   eq_tail      = 队尾截止（完工 + 自身转产预留）—— 异物料接续时用
    #   eq_tail_raw  = 队尾完工（不含转产预留）      —— 同物料接续时用（直接接上）
    #   eq_tail_mat  = 队尾物料                      —— 用于求 changeover(队尾物料, 本单物料)
    eq_tail = {}
    eq_tail_raw = {}
    eq_tail_mat = {}
    # 建议G 负载均衡：累计占用负荷(天)。预置 = 锁定单 + 已排创建单 在各设备上的(完工-开工)之和；
    # 本轮每派一张即累加该单 lead(天)。设备选择时优先把工单派给"派单后负荷最低"的设备。
    eq_load = {}
    # 【P2-13】本轮待排的创建单 id 集合：它们的旧窗口**不能**计入"设备已有负荷"，
    # 否则同一张单既作为历史窗口被预置、又在派单时（下方 :eq_load累加）再计一次 → 双计。
    _round_ids = {it['o'].get('id') for it in parsed}

    def _dur_days(ps, pe):
        return (pe - ps).total_seconds() / 86400.0 if (ps and pe and pe > ps) else 0.0
    for eq, lst in locked_occ.items():
        for (ps, pe, _m) in lst:
            eq_load[eq] = eq_load.get(eq, 0.0) + _dur_days(ps, pe)
    for eq, lst in create_occ.items():
        for (wid, ps, pe, _m) in lst:
            if wid in _round_ids:
                continue
            eq_load[eq] = eq_load.get(eq, 0.0) + _dur_days(ps, pe)
    stats = {'scheduled': 0, 'unscheduled': 0, 'overdue': 0, 'total': len(parsed)}
    touched = set()
    decisions = []  # 收集本次排产写入，循环结束后原子提交（避免中途异常留半截）
    missing_master = {}  # 主数据缺失收集（P1-8）：material_code -> set(缺失项说明)
    assigned = []   # 本轮已分配工单明细（含 eq/es/pe/lead/cands 等），供 swap pass 重排
    blocked_by_eq = 0   # 因候选设备不可用/缺维护时间带而被排除、导致排不上的张数

    for it in parsed:
        o = it['o']
        mat = it['mat']
        if not it['cands']:
            stats['unscheduled'] += 1
            if it.get('cands_blocked'):
                blocked_by_eq += 1
            continue
        unfinished = max(0.0, _f(o.get('confirm_qty')) - _f(o.get('completed_qty')))
        qty = unfinished if unfinished > 0 else _f(o.get('plan_qty'))
        if qty <= 0:  # 无数量（未完工=0 且无计划量）无法排产，标未排
            stats['unscheduled'] += 1
            continue
        lead = _wo_lead_days(qty, mat)
        # P1-8：主数据缺失仍按现有逻辑计算（lead 兜底 0），仅收集"缺什么"用于事后提醒
        if lead is None:
            missing_master.setdefault(mat, set()).add(
                '生产提前期无法计算：日产量(production_time)与生产周期(production_cycle)均缺失')
        lead = lead or 0
        # 设备产能差异化工期：开关默认关闭 → 各设备取同一值（与改动前逐值一致）；
        # 打开后同物料在不同设备上按每小时产能换算工期，缺产能回退日产量口径。
        lead_by_eq = {eq: (_wo_lead_days_eq(qty, mat, eq) or 0) for eq in it['cands']}
        co_new = _wo_changeover_hours(mat)
        if co_new in (None, 0):
            _pm = (_PROD_ROWS or {}).get(mat)
            if _pm and str(_pm.get('type') or '') in ('产成品', '半成品'):
                missing_master.setdefault(mat, set()).add(
                    '转产时间(changeover_time)缺失，排产将不预留转产间隔')
        due = it['due']
        shelf_floor = it['shelf_floor']

        mat_avail = it['mat_avail']

        best = None
        for eq in it['cands']:
            lead_eq = lead_by_eq.get(eq, lead)
            # 【P2-21·勿"顺手统一"起点】此处起点取 eq_tail（该设备最新占用截止）是**有意的**：
            # 贪心阶段按（客户等级 DESC, 交期 ASC）排好优先级后逐个追加，必须接在已有队尾之后，
            # 才能保证高优先级工单不被后来的单插队抢占。下方 _try_place 用 today0 起步是 swap
            # 优化阶段，目的本就不同（允许插空隙以提升负载均衡），两者**口径刻意不一致**。
            # 若把这里改成 today0 → 低优先级单会插队到高优先级单之前（明确回归）；
            # 若把 _try_place 改成 eq_tail → 负载均衡优化彻底失效。
            # 【M1】同物料接续 → 直接接在队尾完工之后（换型 0）；异物料 → 等队尾换型预留
            if eq_tail_mat.get(eq) == mat:
                base = eq_tail_raw.get(eq)
                co_new = 0.0
            else:
                base = eq_tail.get(eq)
                co_new = _wo_changeover_between(eq_tail_mat.get(eq), mat)
            init = base if base else today0
            if shelf_floor and init < shelf_floor:
                init = shelf_floor
            if init < today0:
                init = today0
            # 物料齐套约束（方案C）：最早可开工日不得早于 mat_avail（短缺件采购到货日）
            if mat_avail and init < mat_avail:
                init = mat_avail
            # 占用墙 = 锁定单 + 已排创建单（排除自身窗口，避免自锁）+ 保养/维修带；
            # 【M1】尾部换型按「已排单物料 → 本单物料」序列相关动态计算（见 _occ_for_eq）
            occ = _occ_for_eq(eq, mat, excl_wid=o['id'])
            es = _wo_earliest_start(init, lead_eq, co_new, occ)
            pe = es + timedelta(days=lead_eq)
            feasible = (due is None) or (pe <= due)
            # 建议G 负载均衡：feasible 候选中选"派单后累计负荷(天)最小"的设备；同负荷再取最早完工
            post_load = eq_load.get(eq, 0.0) + (lead_eq or 0)
            # 【P1-6 修复】co_new 是本候选设备对应的换型时长，**必须随候选记录一起保存**：
            # 它是循环内变量，退出循环后保留的是"最后一个候选"的值，而非中选设备 best['eq'] 的。
            rec = {'eq': eq, 'es': es, 'pe': pe, 'feasible': feasible,
                   'post_load': post_load, 'lead': lead_eq, 'co_new': co_new or 0.0}
            if best is None:
                best = rec
            elif rec['feasible'] and not best['feasible']:
                best = rec
            elif rec['feasible'] and best['feasible']:
                # 都可行：累计负荷最小优先，同负荷取最早完工
                if rec['post_load'] < best['post_load'] or \
                   (rec['post_load'] == best['post_load'] and rec['pe'] < best['pe']):
                    best = rec
            elif (not rec['feasible']) and (not best['feasible']):
                # 都不可行(全超交期)：保留原"尽量少超"——取最早完工，同完工取负荷最小
                if rec['pe'] < best['pe'] or \
                   (rec['pe'] == best['pe'] and rec['post_load'] < best['post_load']):
                    best = rec

        if best is None:
            stats['unscheduled'] += 1
            continue

        # 【P1-6 修复】取**中选设备**的换型时长，而非循环遗留的最后一个候选值。
        # 影响下游两处：assigned 记录的 'co_new'、以及队尾预留 eq_tail = pe + co_new。
        # 当前 products.changeover_time 全 NULL → 换型恒 0 不发作；一旦补齐 M1 换型矩阵，
        # 旧写法会使队尾预留与 KPI changeover_hours 统计系统性错位（潜伏缺陷）。
        co_new = best.get('co_new') or 0.0

        # 待料标记：对比“仅设备+货架期”的乐观开工，判断是否被物料约束推后
        base = eq_tail_raw.get(best['eq']) if eq_tail_mat.get(best['eq']) == mat else eq_tail.get(best['eq'])
        opt_init = base if base else today0
        if shelf_floor and opt_init < shelf_floor:
            opt_init = shelf_floor
        if opt_init < today0:
            opt_init = today0
        # 乐观开工同样需避开 锁定单 + 已排创建单（排除自身）+ 保养/维修带，否则待料判定会失真
        occ_best = _occ_for_eq(best['eq'], mat, excl_wid=o['id'])
        opt_es = _wo_earliest_start(opt_init, best['lead'], 0.0, occ_best)
        material_delayed = bool(mat_avail and best['es'] > opt_es)
        # 【S4】快照不可用（从未跑一键计算）时不判『OK』，记『未知』——没算过 ≠ 齐套
        material_status = '待料' if material_delayed else ('OK' if mat_meta['available'] else '未知')

        assigned.append({'oid': o['id'], 'mat': mat, 'lead': best['lead'], 'due': due,
                         'shelf_floor': shelf_floor, 'mat_avail': mat_avail,
                         'cands': it['cands'], 'co_new': co_new,
                         'eq': best['eq'], 'es': best['es'], 'pe': best['pe'],
                         'feasible': best['feasible'], 'material_status': material_status,
                         'cust': it['cust'], 'order_no': o.get('order_no'),
                         'lead_by_eq': lead_by_eq})
        # 【M1】队尾三态更新：raw = 完工；tail = 完工 + 自身转产（给下一单预留，下一单物料此时未知）
        if best['eq'] not in eq_tail_raw or best['pe'] > eq_tail_raw[best['eq']]:
            eq_tail_raw[best['eq']] = best['pe']
            eq_tail[best['eq']] = best['pe'] + timedelta(hours=co_new)
            eq_tail_mat[best['eq']] = mat
        eq_load[best['eq']] = eq_load.get(best['eq'], 0.0) + (best['lead'] or 0)  # 建议G 累计负荷
        touched.add(best['eq'])
        stats['scheduled'] += 1
        if not best['feasible']:
            stats['overdue'] += 1

    # 【P2-13 修复 · 2026-10-14】上面为消除"本轮单双计"跳过了 _round_ids 的旧窗口，
    # 但**排不上的单**（无可用设备/数量为 0）其库内时间仍真实占用设备，必须补回，
    # 否则 KPI 的峰值负荷与极差被系统性低估（与"高估"同样是错的）。
    _assigned_ids = {r['oid'] for r in assigned}
    for eq, lst in create_occ.items():
        for (wid, ps, pe, _m) in lst:
            if wid in _round_ids and wid not in _assigned_ids:
                eq_load[eq] = eq_load.get(eq, 0.0) + _dur_days(ps, pe)

    # ===== 【L1】序列编码 + 统一解码器；【L2】序列空间模拟退火 =====
    # 【改动前的范式缺陷】优化对象曾是 es/pe 时间戳本身：swap 用 _try_place 从 today0
    #   插空隙改 es/pe，落库后 _wo_reschedule_by_changeover 又按 planned_start 紧凑重排一次，
    #   两套口径不同 → 优化阶段评的是一个**不会落库**的时间方案。
    # 【现在】决策变量 = {设备: [工单序列]}；时间只由内核解码器产出一次，
    #   成本评估 / JIT / 落库 / KPI / 待料判定全部同源。搜索只在序列空间进行。
    search_info = {}
    _conflict_eqs = set()   # 【L1】解码结果自检：存在硬冲突的设备（正常应为空）
    if assigned:
        assigned_ids = {r['oid'] for r in assigned}
        W = _wo_sched_weights()

        def _static_occ():
            """静态占用墙：锁定单 + 未参与本轮的创建单（三元组含物料，换型由解码器算）。

            【修复】原 _occ_of 未排除 assigned_ids → 本轮单的**旧窗口**仍占着墙，
            二次排产时新方案被自己的历史落点挡住，无法排回同一时段。
            """
            occ = {}
            for eq, lst in locked_occ.items():
                occ.setdefault(eq, []).extend([(ps, pe, m) for (ps, pe, m) in lst])
            for eq, lst in create_occ.items():
                for (wid, ps, pe, m) in lst:
                    if wid in assigned_ids:
                        continue
                    occ.setdefault(eq, []).append((ps, pe, m))
            return occ

        # 静态负荷(天)：仅锁定单 + 未参与本轮的创建单；本轮单由解码结果实时累加
        _static_load = {}
        for eq, lst in locked_occ.items():
            for (ps, pe, _m) in lst:
                _static_load[eq] = _static_load.get(eq, 0.0) + _dur_days(ps, pe)
        for eq, lst in create_occ.items():
            for (wid, ps, pe, _m) in lst:
                if wid in assigned_ids:
                    continue
                _static_load[eq] = _static_load.get(eq, 0.0) + _dur_days(ps, pe)

        jobs = {}
        for r in assigned:
            jobs[r['oid']] = {'mat': r['mat'], 'cands': list(r['cands']),
                              'due': r['due'], 'cust': r.get('cust'),
                              'shelf_floor': r.get('shelf_floor'),
                              'mat_avail': r.get('mat_avail'),
                              'lead': r['lead'],
                              'lead_by_eq': r.get('lead_by_eq') or {}}

        # 初始序列 = 贪心落点（贪心本就是按优先级 append 到队尾）→ 按开工时间定序
        seqs = {}
        for r in assigned:
            seqs.setdefault(r['eq'], [])
        for r in sorted(assigned, key=lambda x: (x['es'], str(x['oid']))):
            seqs[r['eq']].append(r['oid'])

        ctx = SchedContext(today0=today0, jobs=jobs, static_occ=_static_occ(),
                           maint_occ=maint_occ, static_load=_static_load,
                           earliest_start=_wo_earliest_start,
                           changeover_between=_wo_changeover_between,
                           lead_on=lambda j, eq: (j.get('lead_by_eq') or {}).get(eq, j['lead']),
                           weights=W)
        # 【M4】贪心首轮**解码**结果 = 「计划最稳定」项的基线：偏离它越远，稳定项成本越高
        ctx.base_es = {oid: p['es'] for oid, p in ctx.decode(seqs).items()}

        def _occ_of(eq, excl_oid, mat):
            """设备 eq 上针对「物料 mat」的占用墙（静态 + 本轮已派单，排除自身）。

            【M1】尾部换型按 changeover(占用单物料, mat) 序列相关计算：同物料连产 → 0。
            """
            occ = []
            for (ls, le, lm) in locked_occ.get(eq, []):
                occ.append((ls, le + timedelta(hours=_wo_changeover_between(lm, mat))))
            for (wid, ls, le, lm) in create_occ.get(eq, []):
                if wid == excl_oid or wid in assigned_ids:
                    continue
                occ.append((ls, le + timedelta(hours=_wo_changeover_between(lm, mat))))
            occ.extend(maint_occ.get(eq, []))
            for r in assigned:
                if r['oid'] == excl_oid:
                    continue
                if r['eq'] == eq:
                    occ.append((r['es'], r['pe'] + timedelta(hours=_wo_changeover_between(r['mat'], mat))))
            return occ

        def _lead_on(rec, eq):
            """该工单落到设备 eq 上的工期（天）。开关关闭时恒等于 rec['lead']。"""
            return (rec.get('lead_by_eq') or {}).get(eq, rec['lead'])
        def _opt_es_of(rec):
            """『乐观最早开工』：只考虑 设备占用 + 转产 + 保养/维修带 + 货架期，
            **不含**物料齐套约束（mat_avail）。用于判定工单是否被物料推后。"""
            occ = _occ_of(rec['eq'], rec['oid'], rec['mat'])
            init = today0
            if rec['shelf_floor'] and init < rec['shelf_floor']:
                init = rec['shelf_floor']
            if init < today0:
                init = today0
            return _wo_earliest_start(init, _lead_on(rec, rec['eq']), 0.0, occ)
        def _mat_status(rec):
            # 暂存 opt_es：最终顺排后会据此复检待料标记（见下方 P1-7 修复）
            rec['_opt_es'] = _opt_es_of(rec)
            return '待料' if (rec['mat_avail'] and rec['es'] > rec['_opt_es']) \
                else ('OK' if mat_meta['available'] else '未知')
        # 【L2】序列空间模拟退火：邻域 = relocate / swap / shift，
        # 接受准则 Metropolis（允许暂时变差以逃离局部最优），受 S1 时间预算约束。
        # 开关 module_schedule_search 关闭时不做搜索，仅保留 L1 统一解码。
        if _seq_search_enabled():
            seqs, _best_cost, search_info = sched_anneal(
                ctx, seqs, budget_sec=_WO_SCHED_BUDGET_SEC, seed=_seq_search_seed())
            search_info['cost_parts'] = ctx.cost_parts(ctx.decode(seqs))

        # ===== 【L4·Drum】给瓶颈设备额外的序列优化预算 =====
        # 全局退火对所有设备一视同仁：一台设备上 8 张单与另一台上 2 张单拿到同样的期望迭代次数，
        # 但系统的产出恰恰由最挤的那台决定 —— 把搜索预算再往鼓上压一次，边际收益最高。
        # 做法：只在鼓这一条序列上再跑一轮受限退火，**然后回来用全局成本校验**；
        #       唯有全局成本真的下降才采纳，避免"局部更好、整体更差"。
        if toc['enabled'] and _seq_search_enabled():
            _drum_pre, ranking = _wo_toc_drum(assigned, _static_load)
            toc['drum'] = _drum_pre
            toc['ranking'] = ranking
            if _drum_pre and len(seqs.get(_drum_pre) or []) >= 2:
                _before = ctx.cost(ctx.decode(seqs))
                _sub, _cost_sub, _si = sched_anneal(
                    ctx, {_drum_pre: list(seqs[_drum_pre])},
                    budget_sec=max(0.1, _WO_SCHED_BUDGET_SEC * 0.3),
                    seed=_seq_search_seed() + 1)
                _trial = {k: list(v) for k, v in seqs.items()}
                _trial[_drum_pre] = list(_sub[_drum_pre])
                _after = ctx.cost(ctx.decode(_trial))
                if _after < _before - 1e-9:      # 有净收益才采纳
                    seqs = _trial
                    search_info['drum'] = {'equipment': _drum_pre, 'applied': True,
                                           'iters': _si.get('iters'),
                                           'cost_from': round(_before, 6),
                                           'cost_to': round(_after, 6)}
                else:
                    search_info['drum'] = {'equipment': _drum_pre, 'applied': False,
                                           'iters': _si.get('iters'),
                                           'cost_from': round(_before, 6),
                                           'cost_to': round(_after, 6)}
                toc['drum_refine'] = search_info['drum']
        # 时间**只由解码器产出一次**：后续 JIT / 落库 / KPI / 待料判定全部基于此，口径统一
        _pl = ctx.decode(seqs)
        for r in assigned:
            p = _pl.get(r['oid'])
            if not p:
                continue
            r['eq'] = p['eq']
            r['es'] = p['es']
            r['pe'] = p['pe']
            r['lead'] = p['lead']
            r['feasible'] = (r['due'] is None) or (r['pe'] <= r['due'])
            touched.add(r['eq'])
        # 落库前自检：解码结果是否满足硬不变式（解码器构造性保证，正常应为空集）
        _conflict_eqs = _wo_seq_conflicts(assigned, _static_occ(), maint_occ)

        # ===== 【L4·Drum】按**最终落点**重算瓶颈 =====
        # 上面那次识别用的是贪心落点，只为给鼓分配额外搜索预算；这里才是上报口径。
        if toc['enabled']:
            toc['drum'], toc['ranking'] = _wo_toc_drum(assigned, _static_load)

        def _right_shift(eq_pred=None, due_cap=None):
            """右移算子：把选定工单尽量推迟到"再晚就赶不上交期 / 挡住后一单"的位置。

            设备内按开工**倒序**逐张处理，保证右移一张不会撞上已落位的后续单。
            eq_pred : 过滤哪些单参与（None = 全部），【L4·Rope】用 `!= 鼓` 排除瓶颈；
            due_cap : {工单id: 最晚完工} 【L4·Buffer】给鼓供料的上游不许右移超过它。
            返回实际发生移动的明细（便于回执说明"为什么这张单不是最晚开工"）。
            """
            moved = []
            by_eq_j = {}
            for r in assigned:
                if eq_pred is not None and not eq_pred(r):
                    continue
                by_eq_j.setdefault(r['eq'], []).append(r)
            for _eq, lst in by_eq_j.items():
                lst.sort(key=lambda x: x['es'])
                for i in range(len(lst) - 1, -1, -1):
                    r = lst[i]
                    ld = r['lead'] or 0
                    latest = (r['due'] - timedelta(days=ld)) if (r.get('due') is not None) else None
                    if i + 1 < len(lst):
                        nxt = lst[i + 1]
                        # 🔴【P1-7 修复 · 2026-10-14】`latest` 语义是「最晚**开工**」，
                        # 后继约束必须**再减去本单工期 ld**（与上面 due 分支 `due − ld`、
                        # 下面 due_cap 分支 `cap − ld` 同口径）。
                        # 旧写法漏减 ld → new_pe = nxt.es − co + ld，只要 ld > co 就必然与
                        # 后继单重叠，随后被下方「撞占用则放弃」守卫拒绝 →
                        # **凡有后继单的右移 100% 被放弃**，M2 倒排 JIT 与 L4 Rope
                        # 只对每台设备的最后一张单生效（默认 TOC 开，故已大面积失效）。
                        gap = (nxt['es'] - timedelta(hours=_wo_changeover_between(r['mat'], nxt['mat']))
                               - timedelta(days=ld))
                        latest = gap if latest is None else min(latest, gap)
                    # 【L4·Buffer】给鼓供料的自制件：不得右移到「鼓开工 − 缓冲天数」之后完工
                    if due_cap:
                        cap = due_cap.get(r['oid'])
                        if cap is not None:
                            cap_latest = cap - timedelta(days=ld)
                            latest = cap_latest if latest is None else min(latest, cap_latest)
                    if latest is None:
                        continue
                    lows = [v for v in (today0, r.get('mat_avail'), r.get('shelf_floor'), r['es'])
                            if v is not None]
                    floor = max(lows) if lows else r['es']
                    if latest <= floor:
                        continue        # 已经是最晚可行位置，无需右移
                    new_es = latest
                    new_pe = new_es + timedelta(days=ld)
                    occ_j = _occ_of(_eq, r['oid'], r['mat'])
                    if any(new_es < le and new_pe > ls for (ls, le) in occ_j):
                        continue        # 右移后会撞上后续占用 → 放弃
                    moved.append({'id': r['oid'], 'equipment': _eq,
                                  'from': r['es'].strftime('%Y-%m-%d %H:%M:%S'),
                                  'to': new_es.strftime('%Y-%m-%d %H:%M:%S'),
                                  'shift_hours': round((new_es - r['es']).total_seconds() / 3600.0, 2)})
                    r['es'] = new_es
                    r['pe'] = new_pe
            return moved

        # ===== 【L4·Buffer】先算鼓的上游目标完工日（供下面两种右移共用）=====
        # 🔴【P2-12 修复 · 2026-10-14】原实现把 up_targets 只放在 TOC 分支里，
        # 且 JIT 开启时**完全跳过**带 due_cap 的右移（`if not _wo_jit_enabled()`），
        # 于是：开 JIT 就丢掉鼓的投料缓冲约束 → 上游被一路右移到位，鼓等料停产，
        # 而 `toc['upstream'][i]['violated']` 却报 True（无修正动作）。
        # 现把 up_targets 提前计算，JIT 右移也带上 due_cap（缓冲成为硬上限，不是被跳过）。
        _up_targets = {}
        if toc['enabled']:
            _up_targets = _wo_toc_upstream_targets(assigned, toc['drum'], toc['buffer_days']) or {}

        # ===== 【M2】倒排 JIT（开关 module_schedule_jit，默认关）=====
        # 食品保质期场景：过早完工 = 库存积压 + 货龄风险。正排 ASAP 会把单尽量往前压，
        # JIT 则把每张单尽量右移到"再晚就赶不上交期/挡住后一单"的位置。
        if _wo_jit_enabled():
            _right_shift(due_cap=_up_targets or None)

        # ===== 【L4·Buffer + Rope】=====
        # Rope（绳）：非瓶颈设备**不做提前投产**。TOC 的第一原则——非瓶颈的产能是虚的，
        #   多产出来的不是利润，是 WIP 和库存货龄风险（糖果行业尤其致命）。
        #   故对非鼓设备执行右移；鼓自身保持 ASAP 满负荷，以最大化系统有效产出。
        # Buffer（缓冲）：给鼓供料的上游自制件必须在「鼓开工 − 缓冲天数」前完工，
        #   这里通过 due_cap 变成右移时的**硬上限**——右移只会让完工更早，
        #   所以这层约束不会新增任何交期风险，只是不许把安全余量让出去。
        if toc['enabled']:
            up_targets = _up_targets
            if up_targets:
                _by_id = {x['oid']: x for x in assigned}
                for k, v in up_targets.items():
                    _r = _by_id.get(k) or {}
                    toc['upstream'].append({
                        'id': k, 'target_end': v.strftime('%Y-%m-%d %H:%M:%S'),
                        'actual_end': (_r.get('pe').strftime('%Y-%m-%d %H:%M:%S')
                                       if _r.get('pe') is not None else None),
                        'violated': bool(_r.get('pe') is not None and _r['pe'] > v)})
            # JIT 开启时上方已对**全部**单做过一次带 due_cap 的右移（含非鼓设备），
            # Rope 的语义已被覆盖，故不重复执行（toc['rope'] 留空，改看 JIT 结果）。
            if not _wo_jit_enabled():
                toc['rope'] = _right_shift(eq_pred=lambda r: r['eq'] != toc['drum'],
                                           due_cap=up_targets or None)
            # 【L4·Buffer 管理】红/黄/绿灯：交期缓冲 = 交期 − 计划完工
            #   绿 = ≥2 倍缓冲；黄 = 落入 1~2 倍缓冲（需盯）；红 = 不足 1 倍（随时超期）
            buf_days = toc['buffer_days']
            for r in assigned:
                if r.get('due') is None or r.get('pe') is None:
                    continue
                left = (r['due'] - r['pe']).total_seconds() / 86400.0
                lv = 'red' if left < buf_days else ('yellow' if left < 2 * buf_days else 'green')
                toc['buffer_status'].append({'id': r['oid'], 'equipment': r['eq'],
                                             'material': r.get('mat'),
                                             'buffer_days': round(left, 2), 'level': lv,
                                             'on_drum': r['eq'] == toc['drum']})


        # 重算物料待料状态（基于最终 placement）
        for rec in assigned:
            rec['material_status'] = _mat_status(rec)
        # 重建写入列表（用 swap 后的最终分配）
        # 注：material_status 不在此处写入——它会随最终顺排变化，见下方 P1-7 复检后再单独落库。
        decisions = [(r['eq'], r['es'].strftime('%Y-%m-%d %H:%M:%S'),
                      r['pe'].strftime('%Y-%m-%d %H:%M:%S'), r['oid'])
                     for r in assigned]

    # 🔴【P2-9 修复 · 2026-10-14】`stats['overdue']` 原本停在**贪心期**的判定值，
    # 而后续的序列解码、JIT 右移、TOC Rope 都会改变实际 es/pe —— 结果：
    # 试算（what-if）响应的 `stats.overdue` 与同一响应里的 `kpi.overdue`（落库后按最终
    # 时间算）**自相矛盾**，违反"试算与落库同源"的设计契约。现按**最终** es/pe 重算。
    stats['overdue'] = sum(
        1 for r in assigned
        if r.get('due') is not None and r.get('pe') is not None and r['pe'] > r['due'])

    # 🔴【P1-5 修复 · 2026-10-14】return 原在 `if assigned:` 块内（8 空格缩进），
    # 函数级无兜底 → `orders` 非空但一张都排不上时（工单未维护 production_equipment 是常态）
    # 函数**隐式返回 None**，调用方 `plan.get('empty')` 抛 AttributeError →
    # 一键排产与沙盘试算直接 500，且"未排 N 张"的真实原因被吞掉。
    # 现提到函数级：assigned 为空时 decisions 保持 :1872 的初始 []，本轮即"无落点"。
    return {'empty': False, 'today0': today0, 'assigned': assigned, 'stats': stats,
            'missing_master': missing_master, 'decisions': decisions, 'touched': touched,
            'search_info': search_info, 'conflict_eqs': _conflict_eqs,
            'excluded_eq': excluded_eq, 'mat_meta': mat_meta,
            'locked_skipped': locked_skipped, 'blocked_by_eq': blocked_by_eq,
            'locked_snap': locked_snap, 'eq_load': eq_load, 'maint_occ': maint_occ,
            'toc': toc}


@bp.route('/api/work-orders/auto-schedule', methods=['POST'])
def work_orders_auto_schedule():
    """一键排产：把『创建』状态工单按既定规则自动分配到生产设备并排出计划开工/完工。

    规则（用户确认版）：
      1. 作用对象：仅 status='创建' 的工单；已批准/执行 锁定不动。
      2. 产线：取 products.production_equipment（逗号分隔设备ID）→ equipment.name（看板设备列名）。
        3. 不允许逾期：计划完工 ≤ 交期(batch_date_end)。
         · 首选设备不可行时，改投同物料允许的其它有空闲产能设备（谨慎选择，尽量减少逾期发生）。
         · 若所有允许设备都超产能，则落最早可完工者（仍不主动与 批准/执行 重叠，落点会被推至其占用之后）。
      4. 不早于 交期 − 货架期 开工（货架期取 products.shelf_life(保质期/天)；无则不起 floor）。
      5. 排序：客户等级 DESC，交期 ASC（同客户等级交期越近越先排）。
         客户等级取『客户管理』客户等级(customers.priority)；缺失时 fallback：
         peacock_schedule.customer_priority → products.priority(高/中/低) → 0。
      6. 保持现有逻辑：只动 创建，不动 批准/执行。
      7. 产成品 + 半成品 都参与排产。

    【S1】并发保护：加单飞锁，同一时刻只允许一个排产请求，冲突返回 409。
    【S1】时间预算：minimax swap 优化阶段受 _WO_SCHED_BUDGET_SEC 约束，超预算即接受当前解。
    """
    if not _WO_SCHED_LOCK.acquire(blocking=False):
        return jsonify({'success': False, 'error': '排产进行中，请稍后再试'}), 409
    try:
        plan = _wo_plan()
        if plan.get('empty'):
            return jsonify({'success': True, 'message': plan['message'],
                            'scheduled': 0, 'unscheduled': 0, 'overdue': 0, 'total': 0,
                            'toc': plan.get('toc') or {},
                            'locked_skipped': plan.get('locked_skipped', 0)})
        today0 = plan['today0']
        assigned = plan['assigned']
        stats = plan['stats']
        decisions = plan['decisions']
        touched = plan['touched']
        search_info = plan['search_info']
        _conflict_eqs = plan['conflict_eqs']
        excluded_eq = plan['excluded_eq']
        mat_meta = plan['mat_meta']
        missing_master = plan['missing_master']
        locked_skipped = plan['locked_skipped']
        blocked_by_eq = plan['blocked_by_eq']
        locked_snap = plan['locked_snap']
        eq_load = plan['eq_load']
        maint_occ = plan['maint_occ']
        toc = plan['toc']
        kpi = {}
        warnings = []

        # 🔴【P1-4 修复 · 2026-10-14】原注释声称"一个事务，避免中途异常留下半截排产"，
        # 但**实际是 3 段独立提交**：①本处 execute_many_db(decisions)、②级联顺排内
        # 的逐行 UPDATE、③material_status 回写；且本文件此前**从未 import transaction**
        # → 注释与实现不符。后果：级联顺排或物料状态回写失败时，已提交的"设备/开工/完工"
        # 不回滚 → 工单被排到设备上但物料状态缺失/过期（半截排产）。
        # 现把这三段整体包进一个事务，任一失败整体回滚。
        with transaction():
            if decisions:
                execute_many_db(
                    "UPDATE work_orders SET workcenter_name=%s, planned_start=%s, planned_end=%s, "
                    "updated_at=NOW() WHERE id=%s",
                    decisions)

            # ===== 【L1】落库后不再无条件二次顺排 =====
            # 解码器已**构造性**保证「序列内不重叠 + 换型间隔 + 避开保养带」，
            # 正常情况无需再用另一套口径重写已评估过的时间（那正是本次要根除的问题）。
            # 仅当自检真的发现冲突时才回退到级联顺排兜底，并写入 warnings 让用户可见。
            for eq in sorted(touched):
                if eq in _conflict_eqs:
                    _wo_reschedule_by_changeover(eq)
                    warnings.append('设备「%s」落库结果存在时间冲突，已回退至级联顺排兜底' % eq)

            # ===== 顺排后复检（2026-09-01 修复 P1-7）=====
            # 最终顺排会强制插入转产间隔并跳过保养/维修带，可能把 planned_start/planned_end 推后，
            # 而 stats['overdue'] 与 material_status 均在此【前】判定 → 超交期张数低于实际值、
            # 待料标记也可能失真。排产越紧凑、转产时间越长，失真越大。
            # 故此处回读实际落库时间，以最终结果为准复检交期与待料标记。
            if assigned:
                _rec_map = {r['oid']: r for r in assigned}
                _ids = list(_rec_map.keys())
                _ph = ','.join(['%s'] * len(_ids))
                _final = query_db(
                    "SELECT id, planned_start, planned_end FROM work_orders WHERE id IN (%s)" % _ph,
                    tuple(_ids))
                _fmap = {r['id']: r for r in (_final or [])}
                _overdue = 0
                _ms_updates = []
                for _fr in (_final or []):
                    _rec = _rec_map.get(_fr.get('id'))
                    if _rec is None:
                        continue
                    _ps = _wo_parse_dt(_fr.get('planned_start'))
                    _pe = _wo_parse_dt(_fr.get('planned_end'))
                    _due = _rec.get('due')
                    # ① 交期复检：以最终落库的计划完工为准
                    if _pe is not None and _due is not None and _pe > _due:
                        _overdue += 1
                    # ② 待料复检：最终开工 晚于 乐观开工(不含物料约束) → 被物料推后
                    _opt = _rec.get('_opt_es')
                    if _ps is not None and _opt is not None:
                        _rec['material_status'] = ('待料' if (_rec.get('mat_avail') and _ps > _opt)
                                                   else ('OK' if mat_meta['available'] else '未知'))
                    _ms_updates.append((_rec.get('material_status') or 'OK', _fr.get('id')))
                stats['overdue'] = _overdue
                if _ms_updates:
                    execute_many_db(
                        "UPDATE work_orders SET material_status=%s, updated_at=NOW() WHERE id=%s",
                        _ms_updates)
                # 【S3】KPI 与【S2】不变式自检：一律基于**落库后**的真实时间，不用优化阶段的评估值
                kpi = _wo_sched_kpi(assigned, _fmap, eq_load)
                # 【P2-16 修复】原为 `warnings = _wo_invariant_check(...)` —— **赋值覆盖**，
                # 会把上面级联顺排兜底产生的告警整批丢掉，使"L1 自检失效、结果被重写"
                # 这件大事被静默。现改为 extend 追加。
                warnings.extend(_wo_invariant_check(assigned, _fmap, locked_snap, maint_occ))

        # 主数据缺失提醒（P1-8）：按现有逻辑已排完，此处仅汇总"哪些物料缺什么"交由前端弹窗确认
        master_data_warnings = [{'material_code': m, 'reasons': sorted(v)}
                                for m, v in missing_master.items()]
        msg = '一键排产完成：已排 %d 张，未排(无可用设备) %d 张，超交期 %d 张。' % (
            stats['scheduled'], stats['unscheduled'], stats['overdue'])
        # 【M6】告知本轮跳过了多少人工锁定单，避免"怎么少排了几张"的困惑
        if locked_skipped:
            msg += ' 另有 %d 张已人工排产，未参与重排（如需重排请在该工单上「恢复自动排产」）。' % locked_skipped
        # 不可用设备排除说明：排不上的原因要能说清是"设备不能用"还是"没配设备"
        if blocked_by_eq:
            msg += ' 有 %d 张工单的指定设备不可用（或保养/维修缺起止时间），已排除出候选导致无法排产。' % blocked_by_eq
        # 【S4】物料齐套依据的快照缺失/过旧时明确告知，避免用户误以为"已按齐套排产"
        if not mat_meta['available']:
            msg += ' 注意：未找到缺料明细快照，物料齐套约束未生效，工单物料状态记为「未知」，请先执行「一键计算」。'
        elif mat_meta['age_days'] is not None and mat_meta['age_days'] >= 3:
            msg += ' 提示：物料齐套依据的缺料快照生成于 %s（%d 天前），建议重跑「一键计算」。' % (
                mat_meta['generated_at'], mat_meta['age_days'])
        return jsonify({'success': True, 'message': msg,
                        'master_data_warnings': master_data_warnings,
                        'kpi': kpi, 'warnings': warnings,
                        'search': search_info,          # 【L2】搜索过程可观测（迭代/接受/成本变化）
                        'toc': toc,                     # 【L4】瓶颈/缓冲/绳
                        'material_snapshot': mat_meta,
                        'locked_skipped': locked_skipped,
                        'excluded_equipment': sorted(excluded_eq), **stats})
    except Exception as e:
        return jsonify(err_payload(e)), 500
    finally:
        _WO_SCHED_LOCK.release()


def _wo_diff_vs_db(assigned):
    """【L3】把排产内核的结果与库内现状逐单比对，返回差异明细（**只读**，不改库）。

    之所以要拿 this 与库内现状比，是为了让用户在点「一键排产」之前就能看到：
    哪些单会动、从什么时候挪到什么时候、会不会因此超期。
    """
    ids = [r['oid'] for r in (assigned or [])]
    if not ids:
        return []
    ph = ','.join(['%s'] * len(ids))
    cur = query_db(
        "SELECT id, material_code, workcenter_name, planned_start, planned_end "
        "FROM work_orders WHERE id IN (%s)" % ph, tuple(ids)) or []
    curmap = {r['id']: r for r in cur}
    out = []
    for r in assigned:
        c = curmap.get(r['oid']) or {}
        c_ps = _wo_parse_dt(c.get('planned_start'))
        c_pe = _wo_parse_dt(c.get('planned_end'))
        out.append({
            'id': r['oid'], 'order_no': r.get('order_no'), 'material': r.get('mat'),
            'current': {'equipment': c.get('workcenter_name'),
                        'planned_start': _wo_norm_dt_str(c.get('planned_start')),
                        'planned_end': _wo_norm_dt_str(c.get('planned_end'))},
            'planned': {'equipment': r.get('eq'),
                        'planned_start': (r['es'].strftime('%Y-%m-%d %H:%M:%S')
                                          if r.get('es') is not None else None),
                        'planned_end': (r['pe'].strftime('%Y-%m-%d %H:%M:%S')
                                        if r.get('pe') is not None else None)},
            'shift_hours': (round((r['es'] - c_ps).total_seconds() / 3600.0, 2)
                            if (c_ps is not None and r.get('es') is not None) else None),
            'due': _wo_norm_dt_str(r.get('due')),
            'overdue': bool(r.get('due') is not None and r.get('pe') is not None
                            and r['pe'] > r['due']),
            'material_status': r.get('material_status'),
            'changed': (c.get('workcenter_name') != r.get('eq')) or (c_ps != r.get('es'))
                       or (c_pe != r.get('pe'))})
    return out


@bp.route('/api/work-orders/what-if', methods=['POST'])
def work_orders_what_if():
    """【L3】排产沙盘试算（**只读**）：按当前规则把单排一遍，返回方案与差异，**不写库**。

    入参：无（空 JSON 即可）。
    返回：plan（每单计划设备/开工/完工）+ diff（与库内现状逐单差异）
          + 预估 KPI + toc（瓶颈/缓冲/绳）+ search（搜索过程）+ stats。

    【关键】与一键排产共用同一个 `_wo_plan()` 内核 —— 试算看到的，就是落库会得到的。
    改动前计算写在路由里，"先看效果"只能真跑一次把结果写进 work_orders，看不对还得回滚。
    """
    if not _WO_SCHED_LOCK.acquire(blocking=False):
        return jsonify({'success': False, 'error': '排产/试算进行中，请稍后再试'}), 409
    try:
        plan = _wo_plan()
        if plan.get('empty'):
            return jsonify({'success': True, 'dry_run': True, 'message': plan['message'],
                            'plan': [], 'diff': [], 'kpi': {}, 'toc': plan.get('toc') or {},
                            'stats': plan.get('stats') or {}})
        assigned = plan['assigned']
        diff = _wo_diff_vs_db(assigned)
        # 预估 KPI：把计划时间伪装成 fmap 喂给同一套 KPI 算法（口径与落库后完全一致）
        _fmap = {d['id']: {'id': d['id'],
                           'planned_start': d['planned']['planned_start'],
                           'planned_end': d['planned']['planned_end']} for d in diff}
        kpi = _wo_sched_kpi(assigned, _fmap, plan['eq_load'])
        drum = (plan['toc'] or {}).get('drum')
        rows = [{'id': r['oid'], 'order_no': r.get('order_no'), 'material': r.get('mat'),
                 'equipment': r['eq'],
                 'planned_start': r['es'].strftime('%Y-%m-%d %H:%M:%S') if r.get('es') else None,
                 'planned_end': r['pe'].strftime('%Y-%m-%d %H:%M:%S') if r.get('pe') else None,
                 'lead_days': round(float(r.get('lead') or 0), 4),
                 'due': _wo_norm_dt_str(r.get('due')),
                 'overdue': bool(r.get('due') and r.get('pe') and r['pe'] > r['due']),
                 'material_status': r.get('material_status'),
                 'on_drum': r['eq'] == drum}
                for r in sorted(assigned, key=lambda x: (str(x['eq']), x['es']))]
        changed = [d for d in diff if d['changed']]
        red = [b for b in ((plan['toc'] or {}).get('buffer_status') or []) if b['level'] == 'red']
        msg = ('沙盘试算完成（**未写入**）：本轮 %d 张，其中 %d 张与库内现状不同；'
               '超交期 %d 张，缓冲红灯 %d 张。' % (
                   len(rows), len(changed), kpi.get('overdue', 0), len(red)))
        return jsonify({'success': True, 'dry_run': True, 'message': msg,
                        'plan': rows, 'diff': diff, 'kpi': kpi, 'toc': plan['toc'],
                        'search': plan['search_info'], 'material_snapshot': plan['mat_meta'],
                        'master_data_warnings': [{'material_code': m, 'reasons': sorted(v)}
                                                 for m, v in (plan['missing_master'] or {}).items()],
                        'locked_skipped': plan['locked_skipped'],
                        'excluded_equipment': sorted(plan['excluded_eq'] or []),
                        'changed_count': len(changed),
                        'conflict_equipment': sorted(plan['conflict_eqs'] or []),
                        **plan['stats']})
    except Exception as e:
        return jsonify(err_payload(e)), 500
    finally:
        _WO_SCHED_LOCK.release()


@bp.route('/api/work-orders/device-load', methods=['GET'])
def work_orders_device_load():
    """设备负荷汇总（只读，按窗口）：展示所有「非不可用」的生产设备/包装设备（基准列表），
    并附「未定生产设备」（存在未分配设备工单时）。各列按「起始日期 + 显示天数」窗口计算：
    工单数/创建/批准/执行、总时间(=days*24h)、等待时间(维修/保养占窗小时)、
    有效时间(=总时间-等待)、工单时间(窗内工单交集小时)、负荷占比(=工单时间/有效时间)。
    窗口参数： ?start=YYYY-MM-DD&days=N （缺省 start=今天, days=7）。无任何写操作。"""
    try:
        start_s = request.args.get('start')
        days_s = request.args.get('days')
        try:
            # 【P1-5 修复·2026-10-14】窗口起点「今天」统一为 biz_midnight()
            # （+08:00 当日 00:00），与排程侧同源；原 datetime.now() 依赖 OS 时区。
            win_start = datetime.strptime(start_s, '%Y-%m-%d') if start_s else biz_midnight()
        except Exception:
            win_start = biz_midnight()
        win_start = win_start.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            days = int(days_s) if days_s else 7
        except Exception:
            days = 7
        if days <= 0:
            days = 7
        win_end = win_start + timedelta(days=days)
        win_hours = float(days) * 24.0

        def _inter_hours(a0, a1, b0, b1):
            lo = max(a0, b0)
            hi = min(a1, b1)
            if hi <= lo:
                return 0.0
            return (hi - lo).total_seconds() / 3600.0

        rows = query_db(
            "SELECT workcenter_name, status, planned_start, planned_end "
            "FROM work_orders WHERE status IN ('创建','批准','执行') "
            "AND planned_start IS NOT NULL AND planned_end IS NOT NULL")
        equip_rows = query_db(
            "SELECT code, name, type, status FROM equipment "
            "WHERE type IN ('生产设备','包装设备') AND (status IS NULL OR status <> '不可用') "
            "AND name IS NOT NULL AND name <> ''")
        maint = query_db(
            "SELECT name, start_time, end_time FROM equipment "
            "WHERE status IN ('保养','维修') AND start_time IS NOT NULL AND end_time IS NOT NULL")

        maint_by_name = {}
        for m in maint:
            nm = (m.get('name') or '').strip()
            if not nm:
                continue
            ms = _wo_parse_dt(m['start_time'])
            me = _wo_parse_dt(m['end_time'])
            if not ms or not me:
                continue
            if me <= win_start or ms >= win_end:
                continue
            maint_by_name[nm] = maint_by_name.get(nm, 0.0) + _inter_hours(ms, me, win_start, win_end)

        by_dev = {}
        unassigned = {'order_count': 0, 'created': 0, 'approved': 0, 'executing': 0, 'order_hours': 0.0}
        has_unassigned = False

        def _dev(nm):
            return by_dev.setdefault(nm, {'workcenter_name': nm, 'order_count': 0,
                                          'created': 0, 'approved': 0, 'executing': 0,
                                          'order_hours': 0.0})

        for r in rows:
            eq = (r['workcenter_name'] or '').strip()
            ps = _wo_parse_dt(r['planned_start'])
            pe = _wo_parse_dt(r['planned_end'])
            if not ps or not pe:
                continue
            if pe <= win_start or ps >= win_end:
                continue
            st = r['status']
            if not eq:
                has_unassigned = True
                unassigned['order_count'] += 1
                if st == '创建':
                    unassigned['created'] += 1
                elif st == '批准':
                    unassigned['approved'] += 1
                elif st == '执行':
                    unassigned['executing'] += 1
                unassigned['order_hours'] += _inter_hours(ps, pe, win_start, win_end)
                continue
            d = _dev(eq)
            d['order_count'] += 1
            if st == '创建':
                d['created'] += 1
            elif st == '批准':
                d['approved'] += 1
            elif st == '执行':
                d['executing'] += 1
            d['order_hours'] += _inter_hours(ps, pe, win_start, win_end)

        result = []
        for e in equip_rows:
            nm = (e['name'] or '').strip()
            code = (e.get('code') or '').strip()
            etype = (e.get('type') or '').strip()
            wait = maint_by_name.get(nm, 0.0)
            d = by_dev.get(nm, {'order_count': 0, 'created': 0, 'approved': 0,
                                'executing': 0, 'order_hours': 0.0})
            effective = max(0.0, win_hours - wait)
            load_ratio = (d['order_hours'] / effective) if effective > 0 else 0.0
            result.append({
                'workcenter_name': nm,
                'equipment_code': code,
                'equipment_type': etype,
                'order_count': d['order_count'],
                'created': d['created'],
                'approved': d['approved'],
                'executing': d['executing'],
                'total_hours': round(win_hours, 2),
                'wait_hours': round(wait, 2),
                'effective_hours': round(effective, 2),
                'order_hours': round(d['order_hours'], 2),
                'load_ratio': round(load_ratio, 4),
            })

        if has_unassigned:
            effective = max(0.0, win_hours)
            load_ratio = (unassigned['order_hours'] / effective) if effective > 0 else 0.0
            result.append({
                'workcenter_name': '未定生产设备',
                'equipment_code': '',
                'equipment_type': '',
                'order_count': unassigned['order_count'],
                'created': unassigned['created'],
                'approved': unassigned['approved'],
                'executing': unassigned['executing'],
                'total_hours': round(win_hours, 2),
                'wait_hours': 0.0,
                'effective_hours': round(effective, 2),
                'order_hours': round(unassigned['order_hours'], 2),
                'load_ratio': round(load_ratio, 4),
            })

        def _sort_key(x):
            if x['workcenter_name'] == '未定生产设备':
                return (0, '')
            t = x.get('equipment_type', '')
            grp = 1 if t == '生产设备' else (2 if t == '包装设备' else 3)
            return (grp, x.get('equipment_code') or '')
        result.sort(key=_sort_key)

        return jsonify({'success': True, 'devices': result,
                        'window': {'start': win_start.strftime('%Y-%m-%d'),
                                   'days': days, 'total_hours': round(win_hours, 2)}})
    except Exception as e:
        return jsonify(err_payload(e)), 500


@bp.route('/api/work-orders/import', methods=['POST'])
def work_orders_import():
    """批量导入工单（xlsx）。必需列：工单号/物料编码。
    重复工单号跳过；必填缺失或状态非法记入 errors；product_id 按物料编码自动关联。"""
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
    idx = {cn: i for i, cn in enumerate(header) if cn in _WO_IMPORT_COLMAP}
    for req_col in ('工单号', '物料编码'):
        if req_col not in idx:
            return jsonify({'success': False, 'error': '表头缺少必需列：' + req_col}), 400

    existing = {r['order_no'] for r in
                query_db("SELECT order_no FROM work_orders WHERE order_no IS NOT NULL")}
    prod = query_db("SELECT id, material_code FROM products")
    code2pid = {p['material_code']: p['id'] for p in prod if p.get('material_code')}

    def get_val(row, cn):
        i = idx.get(cn)
        return row[i] if (i is not None and i < len(row)) else None

    created = 0
    skipped = 0
    adjusted = 0   # 「计划需求日期」开关：被自动调整到当日并标记延误的行数
    errors = []
    seen_in_batch = set()
    # 【P3 修复 · 2026-10-14】整批导入包进一个事务：
    # 原实现逐条 INSERT 自动提交，中途某行报错（如列值超长/类型不符）会留下
    # "已写入前 N 条"的半截结果；用户重试时这 N 条全被判为"工单号已存在"跳过，
    # 表现为"导入失败后再导入就少了一截"。现任一失败整体回滚。
    with transaction():
      for ri, raw in enumerate(raw_rows[1:], start=2):
          order_no = (get_val(raw, '工单号') or '')
          order_no = str(order_no).strip() if order_no is not None else ''
          mat = (get_val(raw, '物料编码') or '')
          mat = str(mat).strip() if mat is not None else ''

          if not order_no and not mat:
              continue  # 空行跳过
          if not order_no:
              errors.append({'row': ri, 'reason': '工单号为空'}); continue
          if not mat:
              errors.append({'row': ri, 'reason': '物料编码为空'}); continue
          if order_no in existing or order_no in seen_in_batch:
              skipped += 1
              errors.append({'row': ri, 'reason': '工单号 %s 已存在，跳过' % order_no}); continue

          status = str(get_val(raw, '状态') or '').strip() or '创建'
          if status not in _WO_STATUS_ENUM:
              errors.append({'row': ri, 'reason': '状态非法：%s（限 创建/执行/完成/关闭/作废）' % status}); continue

          data = {
              'id': gen_id(),
              'order_no': order_no,
              'product_id': code2pid.get(mat),
              'material_code': mat,
              'material_desc': str(get_val(raw, '物料描述') or '').strip(),
              'plant': str(get_val(raw, '工厂') or '').strip(),
              'workcenter_name': str(get_val(raw, '生产线') or '').strip(),
              'plan_qty': _wo_to_num(get_val(raw, '计划数量')),
              'confirm_qty': _wo_to_num(get_val(raw, '确认数量')),
              'completed_qty': _wo_to_num(get_val(raw, '已完工数量')),
              'unit': str(get_val(raw, '单位') or '').strip(),
              'planned_start': _wo_to_date(get_val(raw, '计划开工')),
              'planned_end': _wo_to_date(get_val(raw, '计划完工')),
              'status': status,
              'source': str(get_val(raw, '来源') or '').strip(),
              'priority': str(get_val(raw, '优先级') or '').strip(),
              'batch': str(get_val(raw, '批次') or '').strip(),
              'client_name': str(get_val(raw, '客户名称') or '').strip(),
          }
          # 「计划需求日期」开关：导入新增的工单与手工新增同样按新原则处理
          data, _adj = _wo_apply_plan_req_date(data, None)
          if _adj:
              adjusted += 1
          data.setdefault('delayed', 0)
          # 【P3 修复 · 2026-10-14】列名无法参数化，必须过表列白名单；
          # 原实现直接 `', '.join(data.keys())`，一旦 data 混入任意键即为注入面。
          fields = safe_fields(data, 'work_orders')
          cols = ', '.join('`%s`' % f for f in fields)
          placeholders = ', '.join(['%s'] * len(fields))
          values = [data[f] for f in fields]
          execute_db(f"INSERT INTO work_orders ({cols}) VALUES ({placeholders})", values)
          existing.add(order_no)
          seen_in_batch.add(order_no)
          created += 1

    return jsonify({
        'success': True,
        'created': created,
        'skipped': skipped,
        'error_count': len(errors),
        'errors': errors[:50],
        'adjusted': adjusted,
        'message': '导入完成：新增 %d 条，跳过 %d 条（重复），问题 %d 条。' % (created, skipped, len(errors))
                   + (('其中 %d 条计划日期早于当日，已自动调整为当日并标记延误。' % adjusted) if adjusted else ''),
    })
