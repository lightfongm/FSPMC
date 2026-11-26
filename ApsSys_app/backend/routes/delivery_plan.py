# -*- coding: utf-8 -*-
"""发货计划"""

from flask import Blueprint, request, jsonify

from ..database import query_db, execute_db
from ..utils import gen_id, serialize_rows, biz_today
from ..helpers import get_inventory_map
from ..errors import err_payload

bp = Blueprint('delivery_plan', __name__)


@bp.route('/api/delivery-plan/data-sources')
def delivery_plan_data_sources():
    try:
        so_count = query_db("SELECT COUNT(*) as cnt FROM sap_sales_orders", one=True)['cnt']
        wo_count = query_db("SELECT COUNT(*) as cnt FROM work_orders WHERE status NOT IN ('关闭','作废')", one=True)['cnt']
        inv_count = query_db("SELECT COUNT(DISTINCT material_code) as cnt FROM inventory", one=True)['cnt']

        return jsonify({
            'success': True,
            'sources': [
                {'name': 'sales_orders', 'label': '销售订单(SD059)', 'count': so_count},
                {'name': 'work_orders', 'label': '生产工单(PP019)', 'count': wo_count},
                {'name': 'inventory', 'label': 'SAP库存(MM066)', 'count': inv_count},
            ]
        })
    except Exception as e:
        return jsonify(err_payload(e)), 500


@bp.route('/api/delivery-plan/calculate', methods=['POST'])
def delivery_plan_calculate():
    data = request.get_json() or {}
    priority_option = data.get('priority_option', 'stock_first')
    lead_days = int(data.get('lead_days', 7))

    try:
        items = query_db(
            "SELECT si.*, so.name1 as customer_name, so.vdatu, so.bstnk "
            "FROM sap_sales_order_items si "
            "JOIN sap_sales_orders so ON si.vbeln = so.vbeln "
            "WHERE si.nokwmeng > 0 OR (si.nokwmeng IS NULL AND si.kwmeng > 0) "
            "ORDER BY so.vdatu"
        )

        if not items:
            return jsonify({'success': True, 'message': '没有需要计算的发货计划', 'results': []})

        inv_map = get_inventory_map(stock_types=data.get('stock_types', ['unrestricted']))
        # MRP 净剩余库存：优先用 MRP 算完后剩余量，保证发货计划与缺料表口径一致
        mrp_net = {}
        try:
            net_rows = query_db("SELECT material_code, available FROM mrp_net_inventory")
            mrp_net = {r['material_code']: float(r.get('available') or 0) for r in net_rows}
        except Exception:
            mrp_net = {}
        # 库存池：递归消耗，后面的订单只能拿剩余量
        stock_pool = {}
        results = []

        for item in items:
            matnr = item.get('matnr')
            need_qty = float(item.get('nokwmeng') or 0)
            if need_qty == 0:
                need_qty = float(item.get('kwmeng') or 0) - float(item.get('lfimg') or 0)

            # 递归库存：首次读取总库存，后续使用剩余量
            # MRP 已分配的库存不再承诺给发货，保证 ATP 与缺料一致
            if matnr not in stock_pool:
                stock_pool[matnr] = mrp_net.get(matnr, inv_map.get(matnr, {}).get('available', 0))
            stock = stock_pool[matnr]

            if stock >= need_qty:
                fulfillment = '完全库存满足'
                can_deliver = '可发货'
            elif stock > 0:
                fulfillment = '库存部分满足'
                can_deliver = '不建议发货'
            else:
                fulfillment = '无法满足'
                can_deliver = '不可发货'

            remaining = max(0, stock - need_qty)
            stock_pool[matnr] = remaining

            results.append({
                'vbeln': item.get('vbeln'),
                'posnr': item.get('posnr'),
                'matnr': matnr,
                'arktx': item.get('arktx'),
                'quantity': need_qty,
                'unit': item.get('vrkme'),
                'stock_qty': stock,
                'customer_name': item.get('customer_name'),
                'delivery_date': item.get('vdatu'),
                'fulfillment_type': fulfillment,
                'can_deliver': can_deliver,
                'remaining_available': remaining,
            })

        execute_db("DELETE FROM delivery_plan_results")
        for i, r in enumerate(results):
            execute_db(
                """INSERT INTO delivery_plan_results
                   (line_no, created_date, delivery_date, material_code, material_desc, quantity, unit,
                    fulfillment_type, can_deliver, sales_order_no, customer_name, stock_qty,
                    remaining_available, due_date, lead_days, priority_option, calculation_batch)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (i + 1, biz_today().strftime('%Y-%m-%d'), r.get('delivery_date'),
                 r.get('matnr'), r.get('arktx'), r.get('quantity'), r.get('unit'),
                 r.get('fulfillment_type'), r.get('can_deliver'), r.get('vbeln'),
                 r.get('customer_name'), r.get('stock_qty'), r.get('remaining_available'),
                 r.get('delivery_date'), lead_days, priority_option, gen_id())
            )

        return jsonify({
            'success': True,
            'message': f'发货计划计算完成，共{len(results)}条',
            'results': results,
        })

    except Exception as e:
        return jsonify(err_payload(e)), 500


@bp.route('/api/delivery-plan/results')
def delivery_plan_results():
    rows = query_db("SELECT * FROM delivery_plan_results ORDER BY line_no")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@bp.route('/api/delivery-plan/data-sources/detail/<source>')
def delivery_plan_data_source_detail(source):
    if source == 'sales_orders':
        count = query_db("SELECT COUNT(*) as cnt FROM sap_sales_orders", one=True)['cnt']
    elif source == 'work_orders':
        count = query_db("SELECT COUNT(*) as cnt FROM work_orders", one=True)['cnt']
    elif source == 'inventory':
        count = query_db("SELECT COUNT(*) as cnt FROM inventory", one=True)['cnt']
    else:
        count = 0
    return jsonify({'success': True, 'source': source, 'count': count})


@bp.route('/api/delivery-plan/warehouses-enabled')
def delivery_plan_warehouses():
    rows = query_db("SELECT warehouse_code, warehouse_name FROM warehouse_locations WHERE is_enabled=1")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@bp.route('/api/delivery-plan/material-details/<matnr>')
def delivery_plan_material_details(matnr):
    inv = query_db("SELECT * FROM inventory WHERE material_code=%s OR sap_matnr=%s", (matnr, matnr), one=True)
    bom_rows = query_db("SELECT * FROM bom WHERE parent_matnr=%s", (matnr,))
    return jsonify({
        'success': True,
        'inventory': serialize_rows([inv]) if inv else [],
        'bom': serialize_rows(bom_rows)
    })


@bp.route('/api/delivery-plan/diag')
def delivery_plan_diag():
    return jsonify({
        'success': True,
        'sales_orders': query_db("SELECT COUNT(*) as cnt FROM sap_sales_orders", one=True)['cnt'],
        'work_orders': query_db("SELECT COUNT(*) as cnt FROM work_orders", one=True)['cnt'],
        'inventory': query_db("SELECT COUNT(*) as cnt FROM inventory", one=True)['cnt'],
    })


@bp.route('/api/delivery-plan/read-all', methods=['POST'])
def delivery_plan_read_all():
    return jsonify({
        'success': False,
        'message': 'SAP接口未连接，请使用本地数据库数据。'
    })
