# -*- coding: utf-8 -*-
"""导出 Excel — 排程计算综合导出（7 个页签）"""

from io import BytesIO
from datetime import datetime
from decimal import Decimal, ROUND_HALF_UP
import re
import logging
from flask import Blueprint, send_file, request, jsonify
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side

from ..database import query_db
from ..routes.schedule import _get_shortage_rows
from ..utils import biz_today

bp = Blueprint('export_excel', __name__)

HEADER_FILL = PatternFill(start_color='4472C4', end_color='4472C4', fill_type='solid')
HEADER_FONT = Font(color='FFFFFF', bold=True, size=11)
THIN_BORDER = Border(
    left=Side(style='thin'), right=Side(style='thin'),
    top=Side(style='thin'), bottom=Side(style='thin')
)


def _write_header(ws, headers):
    for col, h in enumerate(headers, 1):
        cell = ws.cell(row=1, column=col, value=h)
        cell.font = HEADER_FONT
        cell.fill = HEADER_FILL
        cell.alignment = Alignment(horizontal='center', vertical='center')
        cell.border = THIN_BORDER


def _write_rows(ws, rows, start_row=2):
    for r, row in enumerate(rows, start_row):
        for c, val in enumerate(row, 1):
            cell = ws.cell(row=r, column=c, value=val)
            cell.border = THIN_BORDER


def _num(v, nd=2):
    """转成数值（保留 nd 位小数），空/非数值返回空串。"""
    if v is None or v == '':
        return ''
    try:
        return round(float(v), nd)
    except (TypeError, ValueError):
        return ''


def _fmt_date(v):
    """统一日期格式：YYYYMMDD → YYYY-MM-DD；含 T 的 datetime → 截取；其余原样。"""
    if v is None:
        return ''
    s = str(v).strip()
    if not s:
        return ''
    if 'T' in s:
        return s.replace('T', ' ')[:19]
    if re.fullmatch(r'\d{8}', s):
        return '%s-%s-%s' % (s[0:4], s[4:6], s[6:8])
    return s


# 缺料明细导出表头 —— 与前端"缺料明细"弹窗列顺序/名称完全一致
SHORTAGE_FORM_HEADERS = ['序号', '来源', '物料', '物料名称', '产品需求日期', '产品需求',
                         '子件编码', '子件名称', '子件需求', '子件需求日期', '延误',
                         '可用库存', '库存剩余', '净采购', '建议采购', '当前库存',
                         # 【P0 · 2026-10-20】缺口数量/最迟下单日/在途匹配/成员数（与缺料明细弹窗新增列一致）
                         '缺口数量', '最迟下单日', '在途匹配', '成员数',
                         '非限制', '质检', '冻结', '在途', '状态']


def _round2(v):
    """最多2位小数、四舍五入（ROUND_HALF_UP，与前端 Math.round 一致）。"""
    try:
        return float(Decimal(str(float(v))).quantize(Decimal('0.01'), rounding=ROUND_HALF_UP))
    except (TypeError, ValueError):
        return 0.0


def _shortage_export_rows(rows):
    """把缺料明细 dict 列表转成"与缺料明细表单完全一致"的 24 列（list of list）。

    子件需求列展现毛需求(required_qty)。
    导出展示所有真实物料需求行（子件需求>0），包含建议采购=0 的已覆盖需求行。
    """
    out = []
    seq = 0
    for st in rows:
        gross = float(st.get('required_qty') or 0)
        # 仅过滤无需求的行；建议采购=0 的已覆盖需求行仍导出
        if gross <= 0:
            continue
        seq += 1
        status = (st.get('stock_status') or '').replace('部分缺料', '部分')
        out.append([
            seq,
            _merge_source(st),
            st.get('material_code', ''),
            st.get('material_name', ''),
            st.get('demand_date', '') or '',
            round(float(st.get('demand_qty') or 0), 3),
            st.get('component_code', ''),
            st.get('component_name', ''),
            _round2(gross),
            st.get('component_demand_date', '') or '',
            (st.get('delayed') and '是') or '',  # 延误：与界面红点语义对称（暂不接逻辑，后端未返回 delayed 时为空）
            _round2(st.get('available_qty') or 0),
            _round2(st.get('stock_remaining') or 0),
            _round2(st.get('planned_purchase') or 0),
            _round2(st.get('suggested_purchase') or 0),
            round(float(st.get('current_stock') or 0), 2),
            # 【P0 · 2026-10-20】缺口数量/最迟下单日/在途匹配/成员数
            _round2(st.get('shortage_qty') if st.get('shortage_qty') is not None else st.get('planned_purchase') or 0),
            st.get('latest_order_date', '') or '',
            st.get('intransit_match', '') or '',
            st.get('member_count') or '',
            round(float(st.get('sap_labst') or 0), 2),
            round(float(st.get('sap_insme') or 0), 2),
            round(float(st.get('sap_speme') or 0), 2),
            round(float(st.get('transit_qty') or 0), 3),
            status,
        ])
    return out


def _merge_source(st):
    """缺料明细'来源'列 = 排程结果.计划订单号（与缺料明细界面、排程结果页签口径一致）。
    优先用后端统一计算的 source_display；回退到 s.aufnr/工单号。"""
    return (st.get('source_display') or st.get('sch_aufnr') or st.get('aufnr')
            or st.get('wo_order_no') or '').strip()


# ---------------------------------------------------------------------------
# 各页签数据构造
# ---------------------------------------------------------------------------

# 页签1：排程结果（沿用既有 V2 算法，与界面一致）
def _build_schedule_rows(stock_types):
    """V2.0 标准净需求算法"""
    from datetime import timedelta
    today = biz_today()

    inv_map = _get_inventory_map(stock_types)

    # WIP（仅 planned_end >= 今天）
    wo_rows = query_db("""
        SELECT id, material_code, COALESCE(confirm_qty,0)-COALESCE(completed_qty,0) as wip_qty, planned_end
        FROM work_orders WHERE status IN ('批准','执行')
    """)
    wip_by_mat = {}
    for w in wo_rows:
        pe = w.get('planned_end')
        pe_dt = None
        if pe:
            try:
                if isinstance(pe, datetime):
                    pe_dt = pe.date()
                else:
                    pe_dt = datetime.strptime(str(pe)[:10], '%Y-%m-%d').date()
            except (ValueError, TypeError):
                # 原为 `except: pass` 全吞异常。解析失败时 pe_dt 保持 None，
                # 该工单不参与 WIP 耗用；此处保留兜底语义，仅收窄异常类型。
                pe_dt = None
        if pe_dt and pe_dt < today:
            continue
        wip_by_mat.setdefault(w['material_code'], []).append({
            'id': w['id'], 'qty': float(w.get('wip_qty') or 0), 'planned_end': pe_dt
        })

    schedule_rows = query_db("""
        SELECT s.id, s.schedule_sequence, s.aufnr, s.material_code, s.material_name,
               s.batch_qty, s.batch_date_start, s.batch_date_end, s.plan_qty,
               s.ready_status, s.status_description, s.ready_components,
               s.partial_components, s.missing_components, s.overall_priority,
               s.suggest_order, s.source, s.order_no, s.sales_posnr, s.sales_qty
        FROM peacock_schedule s
        WHERE s.is_deleted=0
        ORDER BY COALESCE(s.batch_date_end,'99999999'),
                 CASE s.source WHEN 'WO' THEN 0 WHEN 'SCH' THEN 1 WHEN 'SEMI' THEN 2 ELSE 9 END,
                 s.material_code
    """)

    so_rows = query_db("""
        SELECT matnr, vbeln, posnr, SUM(kwmeng) as so_qty, SUM(COALESCE(lfimg,0)) as so_delivered
        FROM sap_sales_order_items GROUP BY matnr, vbeln, posnr
    """)
    so_map = {(r['matnr'], r['vbeln'], str(r['posnr'])): (float(r['so_qty'] or 0), float(r['so_delivered'] or 0)) for r in so_rows}
    # 物料级汇总（供非 SCH 行按 batch_qty 比例分摊已发货时回退使用）
    so_map_mat = {}
    for (_m, _v, _p), (_q, _d) in so_map.items():
        _q0, _d0 = so_map_mat.get(_m, (0.0, 0.0))
        so_map_mat[_m] = (_q0 + _q, _d0 + _d)

    prod_rows = query_db("SELECT material_code, COALESCE(production_cycle,0) as production_cycle FROM products")
    prod_cycle = {r['material_code']: float(r['production_cycle'] or 0) for r in prod_rows}

    mat_groups = {}
    for s in schedule_rows:
        mat_groups.setdefault(s['material_code'], []).append(s)

    result = []
    seq = 0
    for mat, items in mat_groups.items():
        items.sort(key=lambda r: r.get('batch_date_end') or r.get('aufnr') or '')
        stock_pool = inv_map.get(mat, 0)
        wip_list = wip_by_mat.get(mat, [])
        used_wip = set()
        # 🔴【P2-25 修复 · 2026-10-14】原实现把本行写在 `for s in items:` **循环外**，
        # 用的是上一个循环（`for s in schedule_rows`）**遗留的 s** —— 取到的是
        # **最后一张排程行**的 order_no/sales_posnr，而非本行自己的。
        # 后果：导出页签的「订单数量 / 已发货 / 未发货」取错行，`ratio` 分摊连带偏差，
        # 与明细口径不一致。现移入循环内、按**每一行自身**的单据号取值。

        for s in items:
            seq += 1
            so_qty_total, so_delivered_total = so_map.get(
                (mat, s.get('order_no'), str(s.get('sales_posnr'))), so_map_mat.get(mat, (0, 0)))
            ratio = float(s.get('batch_qty') or 0) / so_qty_total if so_qty_total else 0
            so_delivered = so_delivered_total * ratio
            so_undelivered = max(0, float(s.get('batch_qty') or 0) - so_delivered)

            batch_date = s.get('batch_date_end') or s.get('batch_date_start') or ''
            bd = None
            if batch_date:
                try:
                    bd = datetime.strptime(str(batch_date)[:8], '%Y%m%d').date()
                except (ValueError, TypeError):
                    # 原为 `except: pass`；排程日期列为 varchar(8) 'YYYYMMDD'，
                    # 解析失败时 bd 保持 None，该行不参与在途时间窗判定。
                    bd = None

            locked_wip = 0.0
            new_used = set()
            for i, w in enumerate(wip_list):
                if i in used_wip:
                    continue
                if w['planned_end'] and bd and w['planned_end'] <= bd:
                    locked_wip += w['qty']
                    new_used.add(i)
            used_wip.update(new_used)

            avail_stock = max(0, stock_pool)
            plan = max(0, so_undelivered - avail_stock - locked_wip)
            stock_pool = stock_pool - so_undelivered + locked_wip

            plan_qty_db = s.get('plan_qty')
            plan_qty_display = float(plan_qty_db) if plan_qty_db is not None else plan

            plan_produce_date = ''
            bds = s.get('batch_date_start', '') or ''
            if plan > 0 and bds:
                try:
                    plan_produce_date = datetime.strptime(str(bds)[:8], '%Y%m%d').strftime('%Y-%m-%d')
                except (ValueError, TypeError) as e:
                    # 【P2-19 修复】原为裸 `except:`（连 KeyboardInterrupt/SystemExit 都吞）且静默。
                    logging.getLogger(__name__).warning('导出：计划排产日期解析失败(%s)：%r', e, bds)
                    plan_produce_date = ''
            if not plan_produce_date and plan > 0:
                cycle = prod_cycle.get(s.get('material_code', ''), 0)
                batch_dt = s.get('batch_date_end', '') or ''
                if cycle > 0 and batch_dt:
                    try:
                        bd2 = datetime.strptime(str(batch_dt)[:8], '%Y%m%d')
                        plan_produce_date = (bd2 - timedelta(days=int(cycle))).strftime('%Y-%m-%d')
                    except (ValueError, TypeError) as e:
                        # 【P2-19 修复】同上：裸 except → 收窄 + 留痕
                        logging.getLogger(__name__).warning(
                            '导出：计划排产日期回退计算失败(%s)：%r', e, batch_dt)

            # 销售订单来源(SCH)：展示 订单数量(so_qty_total) 与 已发货/未发货；计划订单号=销售订单号-行号；计划排产=净缺口(batch_qty)
            if s.get('source') == 'SCH':
                _sales_qty = float(s.get('sales_qty') or 0)
                # 销售订单数量 = 订单数量(kwmeng, so_qty_total)；未发货 = 订单数量 - 已发货 = 未发数量
                _so_qty_disp = so_qty_total if so_qty_total > 0 else _sales_qty
                _so_del_disp = so_delivered_total
                _so_und_disp = max(0.0, _so_qty_disp - so_delivered_total)
                _wip_disp = sum(float(w['qty']) for w in wip_by_mat.get(mat, []))
                _aufnr_disp = s.get('order_no', '') or s.get('aufnr', '')
                if s.get('sales_posnr'):
                    _aufnr_disp = '%s-%s' % (_aufnr_disp, s.get('sales_posnr'))
                # SCH 行计划排产 = batch_qty(净缺口)，不用 V2.0 plan（避免 WIP 双扣）
                plan = float(s.get('batch_qty') or 0)
                plan_qty_display = plan
                # 已覆盖行状态
                if plan == 0 and _sales_qty > 0:
                    s['ready_status'] = '已覆盖'
                    s['status_description'] = '库存+工单已全额覆盖'
            else:
                _so_qty_disp = float(s.get('batch_qty') or 0)
                _so_del_disp = so_delivered
                _so_und_disp = so_undelivered
                _wip_disp = locked_wip
                _aufnr_disp = s.get('aufnr', '')

            result.append((
                seq,
                _aufnr_disp,
                s.get('material_code', ''),
                s.get('material_name', ''),
                round(_so_qty_disp, 2),
                round(_so_del_disp, 2),
                round(_so_und_disp, 2),
                round(avail_stock, 2),
                round(_wip_disp, 2),
                _fmt_date(s.get('batch_date_end', '')),
                plan_produce_date,
                round(plan, 2),
                plan_qty_display,
                (s.get('delayed') and '是') or '',  # 延误：与界面红点语义对称（暂不接逻辑，后端未返回 delayed 时为空）
                '%s %s' % (s.get('ready_status', ''), s.get('status_description', '')),
                '%s/%s/%s' % (s.get('ready_components', 0), s.get('partial_components', 0), s.get('missing_components', 0)),
            ))

    return result


def _get_inventory_map(stock_types):
    """获取库存映射（'transit' 取采购入库'在途'的计划到货数量之和，与 MRP 同源）"""
    from ..helpers import get_transit_by_material
    stock_map = {
        'unrestricted': 'sap_labst',
        'quality': 'sap_insme',
        'blocked': 'sap_speme',
    }
    cols = [f"COALESCE({stock_map[t]}, 0)" for t in stock_types if t in stock_map]
    expr = ' + '.join(cols) if cols else '0'
    rows = query_db(f"SELECT material_code, ({expr}) as available FROM inventory")
    pool = {r['material_code']: float(r['available'] or 0) for r in rows}
    if 'transit' in stock_types:
        for mat, v in get_transit_by_material().items():
            pool[mat] = pool.get(mat, 0) + v
    return pool


# 页签3：销售订单（抬头 + 销售明细行项目，一行一个明细；无明细的订单保留一行抬头）
def _build_sales_rows():
    rows = query_db("""
        SELECT
            h.vbeln, h.kunnr, h.name1, h.auart, h.erdat, h.vdatu AS h_vdatu,
            h.bstnk, h.item_count, h.status,
            i.posnr, i.matnr, i.arktx, i.kwmeng, i.vrkme,
            i.lfimg, i.nokwmeng, i.vdatu AS i_vdatu,
            i.werks, i.lgort, i.gbsta, i.lfsta, i.netpr, i.pstyv, i.note
        FROM sap_sales_orders h
        LEFT JOIN sap_sales_order_items i ON i.vbeln = h.vbeln
        ORDER BY h.vbeln,
                 COALESCE(NULLIF(i.vdatu,''), '99999999'),
                 CAST(COALESCE(NULLIF(i.posnr,''), '0') AS UNSIGNED),
                 i.posnr
    """)
    out = []
    for r in rows:
        kwmeng = r.get('kwmeng')
        lfimg = r.get('lfimg')
        nok = r.get('nokwmeng')
        # 未发数量：优先取库中值，为空时用 订单数量 - 已发货 兜底
        if nok is None and kwmeng is not None:
            nok = float(kwmeng or 0) - float(lfimg or 0)
        out.append([
            # --- 抬头 ---
            r.get('vbeln', ''),
            r.get('kunnr', ''),
            r.get('name1', ''),
            r.get('auart', ''),
            _fmt_date(r.get('erdat', '')),
            _fmt_date(r.get('h_vdatu', '')),
            r.get('bstnk', ''),
            _num(r.get('item_count'), 0),
            r.get('status', ''),
            # --- 销售明细 ---
            r.get('posnr', ''),
            r.get('matnr', ''),
            r.get('arktx', ''),
            _num(kwmeng, 3),
            r.get('vrkme', ''),
            _num(lfimg, 3),
            _num(nok, 3),
            _fmt_date(r.get('i_vdatu', '')),
            r.get('werks', ''),
            r.get('lgort', ''),
            r.get('gbsta', ''),
            r.get('lfsta', ''),
            _num(r.get('netpr'), 6),
            r.get('pstyv', ''),
            r.get('note', ''),
        ])
    return out


# 页签4：采购入库
def _build_purchase_rows():
    rows = query_db("""
        SELECT po_no, purchase_order_no, order_date, supplier_code, supplier_name,
               material_code, material_name, order_qty, arrival_qty, unarrived_qty,
               plan_arrival_date, actual_arrival_qty, actual_arrival_date, status
        FROM purchase_orders ORDER BY po_no
    """)
    out = []
    for r in rows:
        out.append([
            r.get('po_no', ''),
            r.get('purchase_order_no', ''),
            _fmt_date(r.get('order_date', '')),
            r.get('supplier_code', ''),
            r.get('supplier_name', ''),
            r.get('material_code', ''),
            r.get('material_name', ''),
            _num(r.get('order_qty'), 3),
            _num(r.get('arrival_qty'), 3),
            _num(r.get('unarrived_qty'), 3),
            _fmt_date(r.get('plan_arrival_date', '')),
            _num(r.get('actual_arrival_qty'), 3),
            _fmt_date(r.get('actual_arrival_date', '')),
            r.get('status', ''),
        ])
    return out


# 页签5：SAP库存管理
def _build_inventory_rows():
    rows = query_db("""
        SELECT
            COALESCE(NULLIF(material_code,''), sap_matnr) as material_code,
            COALESCE(NULLIF(name,''), sap_maktx) as name,
            quantity, unit,
            COALESCE(NULLIF(location,''), sap_lgort) as location,
            min_stock,
            COALESCE(sap_labst,0) as sap_labst,
            COALESCE(sap_insme,0) as sap_insme,
            COALESCE(sap_speme,0) as sap_speme,
            COALESCE(sap_umlme,0) as sap_umlme
        FROM inventory ORDER BY material_code
    """)
    out = []
    for r in rows:
        out.append([
            r.get('material_code', ''),
            r.get('name', ''),
            _num(r.get('quantity'), 3),
            r.get('unit', ''),
            r.get('location', ''),
            _num(r.get('min_stock'), 2),
            _num(r.get('sap_labst'), 2),
            _num(r.get('sap_insme'), 2),
            _num(r.get('sap_speme'), 2),
            _num(r.get('sap_umlme'), 2),
        ])
    return out


# 页签6：工单管理
def _build_workorder_rows():
    rows = query_db("""
        SELECT
            COALESCE(NULLIF(order_no,''), sap_aufnr) as order_no,
            COALESCE(NULLIF(material_code,''), sap_matnr) as material_code,
            COALESCE(NULLIF(material_desc,''), sap_maktx) as material_desc,
            COALESCE(NULLIF(plant,''), sap_werks) as plant,
            COALESCE(NULLIF(workcenter_name,''), sap_arbpl) as workcenter_name,
            COALESCE(plan_qty, sap_gamng) as plan_qty,
            COALESCE(confirm_qty,0) as confirm_qty,
            COALESCE(completed_qty,0) as completed_qty,
            COALESCE(unit, sap_meins) as unit,
            planned_start, planned_end, status, COALESCE(source,'手动') as source
        FROM work_orders ORDER BY order_no
    """)
    out = []
    for r in rows:
        confirm = float(r.get('confirm_qty') or 0)
        completed = float(r.get('completed_qty') or 0)
        out.append([
            r.get('order_no', ''),
            r.get('material_code', ''),
            r.get('material_desc', ''),
            r.get('plant', ''),
            r.get('workcenter_name', ''),
            _num(r.get('plan_qty'), 3),
            _num(confirm, 3),
            _num(completed, 3),
            _num(confirm - completed, 3),
            r.get('unit', ''),
            _fmt_date(r.get('planned_start', '')),
            _fmt_date(r.get('planned_end', '')),
            r.get('status', ''),
            r.get('source', ''),
        ])
    return out


# 页签7：齐备套结果（齐备状态视角，聚焦 peacock_schedule 的齐备套信息）
def _build_readiness_rows():
    rows = query_db("""
        SELECT aufnr, material_code, material_name, order_no, batch_date_end,
               ready_status, ready_components, partial_components, missing_components,
               total_components, overall_priority, suggest_order, suggest_reason
        FROM peacock_schedule
        WHERE is_deleted=0
        ORDER BY COALESCE(batch_date_end,'99999999'), aufnr
    """)
    out = []
    for r in rows:
        out.append([
            r.get('aufnr', ''),
            r.get('material_code', ''),
            r.get('material_name', ''),
            r.get('order_no', ''),
            _fmt_date(r.get('batch_date_end', '')),
            r.get('ready_status', ''),
            _num(r.get('ready_components'), 0),
            _num(r.get('partial_components'), 0),
            _num(r.get('missing_components'), 0),
            _num(r.get('total_components'), 0),
            _num(r.get('overall_priority'), 0),
            r.get('suggest_order', ''),
            r.get('suggest_reason', ''),
        ])
    return out


SHEET_SPECS = [
    ('排程结果', ['序号', '计划订单号', '物料编码', '物料名称', '销售订单数量',
                 '已发货数量', '未发货数量', '可用库存数量', 'WIP在制',
                 '交货日期',                  '计划排产日期', '计划排产数量', '排产数量', '延误', '齐备状态',
                 '齐备/部分/缺料'],
     lambda st: _build_schedule_rows(st)),
    ('销售订单', ['销售订单号', '客户号', '客户名称', '订单类型', '创建日期', '交货日期',
                 '客户PO号', '行项目数', '状态',
                 '行号', '物料编码', '物料描述', '订单数量', '单位', '已发货', '未发数量',
                 '行交货日期', '工厂', '仓库', '行状态', '交货状态', '单价', '行项目类别', '备注'],
     lambda st: _build_sales_rows()),
    ('采购入库', ['采购入库单号', '采购订单号', '订单日期', '供应商编码', '供应商名称',
                 '物料编码', '物料名称', '订单数量', '计划到货数量', '未到货数量',
                 '计划到货日期', '实际到货数量', '实际到货日期', '状态'],
     lambda st: _build_purchase_rows()),
    ('SAP库存管理', ['物料编码', '物料名称', '数量', '单位', '仓库', '最小库存',
                   'SAP非限制', 'SAP质检', 'SAP冻结', 'SAP在途'],
     lambda st: _build_inventory_rows()),
    ('工单管理', ['工单号', '物料编码', '物料描述', '工厂', '生产线', '计划数量',
                 '确认数量', '已完工数量', '未完工数量', '单位', '计划开工', '计划完工',
                 '状态', '来源'],
     lambda st: _build_workorder_rows()),
    ('齐备套结果', ['计划订单号', '物料编码', '物料名称', '销售订单', '交货日期',
                   '齐备状态', '齐备组件', '部分齐备组件', '缺料组件', '总组件数',
                   '优先级', '建议下单', '建议原因'],
     lambda st: _build_readiness_rows()),
]


@bp.route('/api/export/excel')
def export_excel():
    stock_types = request.args.getlist('stock_types') or ['unrestricted']
    # 是否包含在途由「可用库存（多选）」的在途选项控制（与缺料明细弹窗一致）
    include_transit = 'transit' in stock_types

    wb = Workbook()
    # Sheet 1: 排程结果
    ws1 = wb.active
    ws1.title = '排程结果'
    sched_headers = ['序号', '计划订单号', '物料编码', '物料名称', '销售订单数量',
                     '已发货数量', '未发货数量', '可用库存数量', 'WIP在制',
                     '交货日期', '计划排产日期', '计划排产数量', '排产数量', '延误', '齐备状态',
                     '齐备/部分/缺料']
    _write_header(ws1, sched_headers)
    _write_rows(ws1, _build_schedule_rows(stock_types))

    # Sheet 2: 缺料明细（表头/列顺序与前端缺料明细弹窗完全一致）
    ws2 = wb.create_sheet(title='缺料明细')
    _write_header(ws2, SHORTAGE_FORM_HEADERS)
    rows2 = _get_shortage_rows(stock_types, include_transit)
    _write_rows(ws2, _shortage_export_rows(rows2))

    # Sheet 3~7：销售订单 / 采购入库 / SAP库存管理 / 工单管理 / 齐备套结果
    for title, headers, builder in SHEET_SPECS[1:]:
        ws = wb.create_sheet(title=title)
        _write_header(ws, headers)
        _write_rows(ws, builder(stock_types))

    # Auto-width
    for ws in wb.worksheets:
        for col in ws.columns:
            max_len = 0
            for cell in col:
                if cell.value is not None:
                    max_len = max(max_len, len(str(cell.value)))
            letter = col[0].column_letter
            ws.column_dimensions[letter].width = min(max_len + 4, 32)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(
        buf,
        mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
        as_attachment=True,
        download_name='APS排程计算导出.xlsx',
    )


@bp.route('/api/export/shortage-excel')
def export_shortage_excel():
    """导出缺料明细（表头/列顺序与前端缺料明细弹窗完全一致）"""
    from io import BytesIO
    import openpyxl
    # 是否包含在途由「可用库存（多选）」的在途选项控制（与缺料明细弹窗一致）
    stock_types = request.args.getlist('stock_types') or ['unrestricted']
    include_transit = 'transit' in stock_types

    rows = _get_shortage_rows(stock_types, include_transit)
    export_rows = _shortage_export_rows(rows)
    if not export_rows:
        return jsonify({'success': False, 'error': '无数据'}), 400

    wb = openpyxl.Workbook()
    ws = wb.active
    ws.title = '缺料明细'
    _write_header(ws, SHORTAGE_FORM_HEADERS)
    _write_rows(ws, export_rows)
    # Auto-width
    for col in ws.columns:
        max_len = 0
        for cell in col:
            if cell.value:
                max_len = max(max_len, len(str(cell.value)))
        ws.column_dimensions[col[0].column_letter].width = min(max_len + 4, 30)

    buf = BytesIO()
    wb.save(buf)
    buf.seek(0)
    return send_file(buf, mimetype='application/vnd.openxmlformats-officedocument.spreadsheetml.sheet',
                     as_attachment=True, download_name='缺料明细.xlsx')
