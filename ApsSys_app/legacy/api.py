#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
孔雀排程系统 V3.0 — 后端 API (MySQL版)
Flask + PyMySQL
"""

import os
import json
import uuid
import math
import hashlib
from datetime import datetime, date, timedelta
from functools import wraps

import pymysql
from pymysql.cursors import DictCursor
from flask import Flask, request, jsonify, send_from_directory, Response

# =============================================
# 配置
# =============================================
APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, 'static')

DB_CONFIG = {
    'host': os.environ.get('DB_HOST', '127.0.0.1'),
    'port': int(os.environ.get('DB_PORT', 3306)),
    'user': os.environ.get('DB_USER', 'root'),
    'password': os.environ.get('DB_PASSWORD', ''),
    'database': os.environ.get('DB_NAME', 'schedule_system'),
    'charset': 'utf8mb4',
    'cursorclass': DictCursor,
}

app = Flask(__name__, static_folder=STATIC_DIR)
app.config['JSON_AS_ASCII'] = False
app.config['MAX_CONTENT_LENGTH'] = 100 * 1024 * 1024  # 100MB

# 注册蓝图路由（补充新功能接口）
from backend.routes import register_blueprints
register_blueprints(app)

# =============================================
# 数据库连接
# =============================================
def get_db():
    """获取数据库连接"""
    conn = pymysql.connect(**DB_CONFIG)
    return conn


def query_db(sql, args=None, one=False):
    """查询数据库，返回字典列表"""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        conn.commit()
        return rows[0] if one and rows else rows
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def execute_db(sql, args=None):
    """执行写操作，返回受影响行数"""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
        conn.commit()
        return cur.rowcount
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def execute_many_db(sql, args_list):
    """批量执行"""
    conn = get_db()
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, args_list)
        conn.commit()
        return cur.rowcount
    except Exception as e:
        conn.rollback()
        raise e
    finally:
        conn.close()


def gen_id():
    """生成唯一ID"""
    return str(uuid.uuid4())


def serialize_row(row):
    """序列化数据库行，处理日期和Decimal"""
    if row is None:
        return None
    result = {}
    for k, v in row.items():
        if isinstance(v, (datetime, date)):
            result[k] = v.isoformat() if isinstance(v, datetime) else v.isoformat()
        elif isinstance(v, bytes):
            result[k] = v.decode('utf-8', errors='replace') if v else None
        elif hasattr(v, '__float__') and not isinstance(v, float):
            result[k] = float(v)
        else:
            result[k] = v
    return result


def serialize_rows(rows):
    return [serialize_row(r) for r in rows]


# =============================================
# 工具函数
# =============================================
def get_config(key, default=None):
    """获取系统配置"""
    row = query_db("SELECT config_value FROM system_config WHERE config_key=%s", (key,), one=True)
    return row['config_value'] if row else default


def set_config(key, value):
    """设置系统配置"""
    execute_db(
        "INSERT INTO system_config (config_key, config_value) VALUES (%s, %s) "
        "ON DUPLICATE KEY UPDATE config_value=%s",
        (key, str(value), str(value))
    )


def get_unlimited_codes():
    """获取无限量物料编码列表"""
    rows = query_db("SELECT material_code FROM unlimited_materials")
    return [r['material_code'] for r in rows]


def get_inventory_map(warehouses=None, stock_types=None):
    """获取库存映射 {material_code: total_stock}"""
    conditions = []
    args = []
    if warehouses:
        placeholders = ','.join(['%s'] * len(warehouses))
        conditions.append(f"sap_lgort IN ({placeholders})")
        args.extend(warehouses)

    where = " AND ".join(conditions) if conditions else "1=1"

    # 按物料汇总库存
    if stock_types is None:
        stock_types = ['unrestricted']

    sql = f"""
        SELECT material_code,
               MAX(name) as name,
               MAX(unit) as unit,
               SUM(CASE WHEN 'unrestricted' IN ({','.join(['%s']*len(stock_types))}) THEN COALESCE(quantity,0) ELSE 0 END) as unrestricted_stock,
               SUM(CASE WHEN 'quality' IN ({','.join(['%s']*len(stock_types))}) THEN COALESCE(sap_insme,0) ELSE 0 END) as quality_stock,
               SUM(CASE WHEN 'blocked' IN ({','.join(['%s']*len(stock_types))}) THEN COALESCE(sap_speme,0) ELSE 0 END) as blocked_stock,
               SUM(CASE WHEN 'transit' IN ({','.join(['%s']*len(stock_types))}) THEN COALESCE(sap_umlme,0) ELSE 0 END) as transit_stock
        FROM inventory
        WHERE {where}
        GROUP BY material_code
    """
    all_args = list(stock_types) * 4 + args
    rows = query_db(sql, all_args)

    inv_map = {}
    for r in rows:
        total = float(r.get('unrestricted_stock') or 0) + float(r.get('quality_stock') or 0) + \
                float(r.get('blocked_stock') or 0) + float(r.get('transit_stock') or 0)
        inv_map[r['material_code']] = {
            'total': total,
            'name': r.get('name'),
            'unit': r.get('unit'),
            'unrestricted': float(r.get('unrestricted_stock') or 0),
            'quality': float(r.get('quality_stock') or 0),
            'blocked': float(r.get('blocked_stock') or 0),
            'transit': float(r.get('transit_stock') or 0),
        }
    return inv_map


def get_bom_children(parent_code):
    """获取BOM子件"""
    rows = query_db(
        "SELECT comp_matnr, comp_maktx, comp_menge, comp_meins, postp, lgort "
        "FROM bom WHERE parent_matnr=%s",
        (parent_code,)
    )
    return rows


# =============================================
# 静态文件路由
# =============================================
@app.route('/')
def index():
    return send_from_directory(STATIC_DIR, 'enhanced.html')


@app.route('/<path:filename>')
def static_files(filename):
    full_path = os.path.join(STATIC_DIR, filename)
    if os.path.isfile(full_path):
        return send_from_directory(STATIC_DIR, filename)
    return jsonify({'error': 'Not found'}), 404


@app.route('/favicon.ico')
def favicon():
    return '', 204


# =============================================
# 健康检查
# =============================================
@app.route('/api/health')
def health():
    try:
        row = query_db("SELECT VERSION() as version", one=True)
        return jsonify({
            'status': 'ok',
            'database': 'connected',
            'db_version': row['version'],
            'db_type': 'MySQL',
            'timestamp': datetime.now().isoformat()
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'database': 'disconnected',
            'error': str(e)
        }), 500


@app.route('/api/metadata/fields')
def metadata_fields():
    """字段元数据"""
    return jsonify({
        'products': ['id', 'material_code', 'name', 'type', 'category', 'workshop',
                      'production_time', 'production_cycle', 'batch_size', 'unit', 'priority'],
        'inventory': ['id', 'material_code', 'name', 'quantity', 'unit', 'location', 'min_stock'],
        'work_orders': ['id', 'order_no', 'material_code', 'material_desc', 'status', 'priority'],
        'customers': ['id', 'name', 'code', 'priority', 'sap_kunnr'],
        'bom': ['bom_key', 'parent_matnr', 'comp_matnr', 'comp_menge', 'comp_meins', 'postp'],
    })


# =============================================
# 排程计算 — 核心
# =============================================
@app.route('/api/calculate/readiness', methods=['POST'])
def calculate_readiness():
    """齐备套计算"""
    data = request.get_json() or {}
    plan_order_nums = data.get('plan_order_nums', [])
    limit = data.get('limit', 100)
    enabled_warehouses = data.get('enabled_warehouses', [])
    calc_actual_required = data.get('calc_actual_required', False)
    enabled_stock_types = data.get('enabled_stock_types', ['unrestricted'])

    try:
        # 获取计划订单
        if plan_order_nums:
            placeholders = ','.join(['%s'] * len(plan_order_nums))
            orders = query_db(
                f"SELECT * FROM sap_planned_orders WHERE plnum IN ({placeholders}) LIMIT %s",
                plan_order_nums + [limit]
            )
        else:
            orders = query_db("SELECT * FROM sap_planned_orders LIMIT %s", (limit,))

        if not orders:
            return jsonify({
                'success': True,
                'message': '没有需要计算的订单',
                'total': 0,
                'results': []
            })

        # 获取库存映射
        inv_map = get_inventory_map(enabled_warehouses, enabled_stock_types)
        unlimited_codes = get_unlimited_codes()

        results = []
        for order in orders:
            plnum = order['plnum']
            matnr = order['matnr']
            order_qty = float(order.get('gsmng') or 0)

            # 获取该订单的子件
            items = query_db(
                "SELECT * FROM sap_planned_order_items WHERE plnum=%s",
                (plnum,)
            )

            ready_count = 0
            partial_count = 0
            missing_count = 0
            component_details = []

            for item in items:
                comp_matnr = item.get('matnr')
                comp_qty = float(item.get('bdmng') or 0)

                if comp_matnr in unlimited_codes:
                    status = 'READY'
                    current_stock = max(comp_qty, float(inv_map.get(comp_matnr, {}).get('total', 0)))
                    ready_count += 1
                else:
                    stock = float(inv_map.get(comp_matnr, {}).get('total', 0))
                    current_stock = stock
                    if stock >= comp_qty:
                        status = 'READY'
                        ready_count += 1
                    elif stock > 0:
                        status = 'PARTIAL'
                        partial_count += 1
                    else:
                        status = 'MISSING'
                        missing_count += 1

                available_qty = current_stock
                remaining = current_stock - comp_qty
                actual_required = max(0, comp_qty - current_stock) if calc_actual_required else comp_qty

                component_details.append({
                    'component_code': comp_matnr,
                    'component_name': item.get('maktx'),
                    'required_qty': comp_qty,
                    'available_qty': available_qty,
                    'current_stock': current_stock,
                    'remaining': remaining,
                    'actual_required_qty': actual_required,
                    'status': status,
                    'unit': item.get('meins'),
                })

            # 订单整体状态
            if missing_count == 0 and partial_count == 0:
                ready_status = 'A'
                status_desc = '齐备'
            elif missing_count == 0:
                ready_status = 'B'
                status_desc = '部分齐备'
            else:
                ready_status = 'C'
                status_desc = '缺料'

            results.append({
                'plnum': plnum,
                'matnr': matnr,
                'maktx': order.get('maktx'),
                'gsmng': order_qty,
                'ready_status': ready_status,
                'status_description': status_desc,
                'ready_components': ready_count,
                'partial_components': partial_count,
                'missing_components': missing_count,
                'total_components': len(items),
                'components': component_details,
            })

        # 保存结果
        calc_id = gen_id()
        for i, r in enumerate(results):
            schedule_id = gen_id()
            execute_db(
                """INSERT INTO peacock_schedule
                   (id, aufnr, order_no, material_code, material_name, plan_qty,
                    ready_status, status_description, ready_components, partial_components,
                    missing_components, total_components, calculated_at, calculated_by)
                   VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)""",
                (schedule_id, r['plnum'], r['plnum'], r['matnr'], r['maktx'], r['gsmng'],
                 r['ready_status'], r['status_description'], r['ready_components'],
                 r['partial_components'], r['missing_components'], r['total_components'], 'system')
            )
            # 保存缺料明细
            for comp in r['components']:
                if comp['status'] in ('PARTIAL', 'MISSING'):
                    execute_db(
                        """INSERT INTO peacock_material_shortage
                           (id, schedule_id, aufnr, material_code, component_code, component_name,
                            required_qty, available_qty, current_stock, shortage_type, stock_status, need_qty, created_at, updated_at)
                           VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW())""",
                        (gen_id(), schedule_id, r['plnum'], r['matnr'],
                         comp['component_code'], comp['component_name'],
                         comp['required_qty'], comp['available_qty'], comp['current_stock'],
                         comp['status'], comp['status'], comp['actual_required_qty'])
                    )

        # 保存日志
        execute_db(
            "INSERT INTO peacock_calculation_logs (calculation_id, log_type, timestamp, description, details) "
            "VALUES (%s, %s, %s, %s, %s)",
            (calc_id, 'readiness', datetime.now().strftime('%Y-%m-%d %H:%M:%S'),
             f'齐备套计算完成，共{len(results)}个订单', json.dumps({'total': len(results)}, ensure_ascii=False))
        )

        return jsonify({
            'success': True,
            'message': f'计算完成，共处理{len(results)}个订单',
            'total': len(results),
            'results': results,
            'calculation_id': calc_id,
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/calculate/priority', methods=['POST'])
def calculate_priority():
    """优先级计算"""
    data = request.get_json() or {}
    calc_id = data.get('calculation_id')

    try:
        # 获取上次齐备套计算结果
        if calc_id:
            schedules = query_db(
                "SELECT * FROM peacock_schedule WHERE calculated_at > DATE_SUB(NOW(), INTERVAL 1 HOUR) "
                "ORDER BY calculated_at DESC LIMIT %s", (data.get('limit', 100),)
            )
        else:
            schedules = query_db(
                "SELECT * FROM peacock_schedule WHERE is_deleted=0 ORDER BY calculated_at DESC LIMIT %s",
                (data.get('limit', 100),)
            )

        if not schedules:
            return jsonify({'success': False, 'message': '请先执行齐备套计算'})

        # 计算优先级
        results = []
        for s in schedules:
            base_score = {'A': 100, 'B': 50, 'C': 0}.get(s.get('ready_status'), 0)
            qty = float(s.get('plan_qty') or 0)
            if qty >= 1000:
                qty_score = 10
            elif qty >= 500:
                qty_score = 8
            elif qty >= 100:
                qty_score = 5
            else:
                qty_score = 2

            # 交期紧迫度
            finish_date = s.get('finish_date')
            urgency_score = 0
            if finish_date:
                if isinstance(finish_date, str):
                    try:
                        finish_date = datetime.strptime(finish_date[:10], '%Y-%m-%d').date()
                    except:
                        finish_date = None
                if finish_date:
                    days_left = (finish_date - date.today()).days
                    if days_left < 0:
                        urgency_score = 30
                    elif days_left <= 3:
                        urgency_score = 25
                    elif days_left <= 7:
                        urgency_score = 20
                    elif days_left <= 14:
                        urgency_score = 15
                    elif days_left <= 30:
                        urgency_score = 10
                    else:
                        urgency_score = 5

            priority_score = base_score + urgency_score + qty_score
            suggest = 'YES' if s.get('ready_status') in ('A', 'B') else 'NO'

            results.append({
                'schedule_id': s['id'],
                'aufnr': s.get('aufnr'),
                'material_code': s.get('material_code'),
                'material_name': s.get('material_name'),
                'ready_status': s.get('ready_status'),
                'base_score': base_score,
                'urgency_score': urgency_score,
                'qty_score': qty_score,
                'priority_score': priority_score,
                'suggest_order': suggest,
            })

        # 排序
        results.sort(key=lambda x: x['priority_score'], reverse=True)

        # 更新排程表
        for i, r in enumerate(results):
            execute_db(
                "UPDATE peacock_schedule SET overall_priority=%s, schedule_sequence=%s, "
                "suggest_order=%s WHERE id=%s",
                (r['priority_score'], i + 1, r['suggest_order'], r['schedule_id'])
            )
            r['rank'] = i + 1

        return jsonify({
            'success': True,
            'message': f'优先级计算完成，共{len(results)}个订单',
            'results': results,
        })

    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/calculate/last-info')
def calculate_last_info():
    """获取上次计算信息"""
    try:
        row = query_db(
            "SELECT calculation_id, log_type, timestamp, description, details, created_at "
            "FROM peacock_calculation_logs ORDER BY created_at DESC LIMIT 1",
            one=True
        )
        if row:
            return jsonify({'success': True, 'data': serialize_row(row)})
        return jsonify({'success': False, 'message': '无计算记录'})
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/peacock/calculate', methods=['POST'])
def peacock_calculate():
    """一键排程计算（齐备套+优先级）"""
    data = request.get_json() or {}
    # 先执行齐备套计算
    resp1 = calculate_readiness()
    if resp1[0].status_code != 200:
        return resp1
    result1 = resp1[0].get_json()
    if not result1.get('success'):
        return jsonify(result1)

    # 再执行优先级计算
    data['calculation_id'] = result1.get('calculation_id')
    resp2 = calculate_priority()
    if resp2[0].status_code != 200:
        return resp2
    result2 = resp2[0].get_json()

    return jsonify({
        'success': True,
        'message': '一键排程计算完成',
        'readiness': result1,
        'priority': result2,
    })


@app.route('/api/peacock/schedules')
def peacock_schedules():
    """获取排程结果列表"""
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    status = request.args.get('status')
    offset = (page - 1) * per_page

    conditions = ["is_deleted=0"]
    args = []
    if status:
        conditions.append("peacock_status=%s")
        args.append(status)

    where = " AND ".join(conditions)
    total = query_db(f"SELECT COUNT(*) as cnt FROM peacock_schedule WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM peacock_schedule WHERE {where} ORDER BY schedule_sequence, overall_priority DESC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )

    return jsonify({
        'success': True,
        'total': total,
        'page': page,
        'per_page': per_page,
        'data': serialize_rows(rows)
    })


@app.route('/api/peacock/schedule/<schedule_id>/details')
def peacock_schedule_details(schedule_id):
    """排程详情"""
    schedule = query_db("SELECT * FROM peacock_schedule WHERE id=%s", (schedule_id,), one=True)
    if not schedule:
        return jsonify({'success': False, 'message': '未找到排程记录'}), 404
    shortages = query_db("SELECT * FROM peacock_material_shortage WHERE schedule_id=%s", (schedule_id,))
    return jsonify({
        'success': True,
        'schedule': serialize_row(schedule),
        'shortages': serialize_rows(shortages)
    })


@app.route('/api/peacock/logs')
def peacock_logs():
    """计算日志"""
    limit = int(request.args.get('limit', 50))
    rows = query_db(
        "SELECT * FROM peacock_calculation_logs ORDER BY created_at DESC LIMIT %s",
        (limit,)
    )
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@app.route('/api/peacock/statistics')
def peacock_statistics():
    """排程统计"""
    try:
        total = query_db("SELECT COUNT(*) as cnt FROM peacock_schedule WHERE is_deleted=0", one=True)['cnt']
        ready_a = query_db("SELECT COUNT(*) as cnt FROM peacock_schedule WHERE is_deleted=0 AND ready_status='A'", one=True)['cnt']
        ready_b = query_db("SELECT COUNT(*) as cnt FROM peacock_schedule WHERE is_deleted=0 AND ready_status='B'", one=True)['cnt']
        ready_c = query_db("SELECT COUNT(*) as cnt FROM peacock_schedule WHERE is_deleted=0 AND ready_status='C'", one=True)['cnt']
        shortages = query_db("SELECT COUNT(*) as cnt FROM peacock_material_shortage", one=True)['cnt']

        return jsonify({
            'success': True,
            'total': total,
            'ready_a': ready_a,
            'ready_b': ready_b,
            'ready_c': ready_c,
            'shortage_count': shortages,
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================
# 产品/物料管理
# =============================================
@app.route('/api/products')
def products_list():
    """产品列表"""
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
        f"SELECT * FROM products WHERE {where} ORDER BY updated_at DESC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )

    return jsonify({
        'success': True,
        'total': total,
        'page': page,
        'per_page': per_page,
        'data': serialize_rows(rows)
    })


@app.route('/api/products', methods=['POST'])
def products_create():
    """创建产品"""
    data = request.get_json() or {}
    pid = data.get('id') or gen_id()
    data['id'] = pid
    data['updated_at'] = datetime.now()

    fields = ['id', 'material_code', 'name', 'type', 'category', 'workshop', 'secret',
              'production_time', 'production_cycle', 'batch_size', 'energy_coeff', 'priority',
              'unit_price', 'safe_stock', 'unit', 'old_material_code', 'material_group',
              'material_trial_no', 'storage_condition', 'size_dimension', 'created_date', 'data_source']

    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]

    execute_db(f"INSERT INTO products ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': pid, 'message': '创建成功'})


@app.route('/api/products/<pid>', methods=['PUT'])
def products_update(pid):
    """更新产品"""
    data = request.get_json() or {}
    fields = ['material_code', 'name', 'type', 'category', 'workshop', 'secret',
              'production_time', 'production_cycle', 'batch_size', 'energy_coeff', 'priority',
              'unit_price', 'safe_stock', 'unit', 'old_material_code', 'material_group',
              'material_trial_no', 'storage_condition', 'size_dimension', 'data_source']

    sets = ', '.join([f"{f}=%s" for f in fields])
    values = [data.get(f) for f in fields] + [pid]
    execute_db(f"UPDATE products SET {sets}, updated_at=NOW() WHERE id=%s", values)
    return jsonify({'success': True, 'message': '更新成功'})


@app.route('/api/products/<pid>', methods=['DELETE'])
def products_delete(pid):
    """删除产品"""
    execute_db("DELETE FROM products WHERE id=%s", (pid,))
    return jsonify({'success': True, 'message': '删除成功'})


# =============================================
# 库存管理
# =============================================
@app.route('/api/inventory')
def inventory_list():
    """库存列表"""
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        # 转换通配符：* → %, ? → _
        s = search.replace('*', '%').replace('?', '_')
        # 如果用户自已用了通配符，就不额外加 %；否则自动模糊匹配
        if '*' not in search and '?' not in search:
            s = f'%{s}%'
        conditions.append("(material_code LIKE %s OR name LIKE %s OR sap_matnr LIKE %s)")
        args.extend([s] * 3)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM inventory WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM inventory WHERE {where} ORDER BY material_code ASC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )

    return jsonify({
        'success': True,
        'total': total,
        'page': page,
        'per_page': per_page,
        'data': serialize_rows(rows)
    })


@app.route('/api/inventory', methods=['POST'])
def inventory_create():
    """创建库存记录"""
    data = request.get_json() or {}
    pid = gen_id()
    data['id'] = pid

    fields = ['id', 'material_code', 'name', 'quantity', 'unit', 'location', 'min_stock']
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]

    execute_db(f"INSERT INTO inventory ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': pid, 'message': '创建成功'})


@app.route('/api/inventory/<iid>', methods=['PUT'])
def inventory_update(iid):
    """更新库存"""
    data = request.get_json() or {}
    fields = ['material_code', 'name', 'quantity', 'unit', 'location', 'min_stock']
    sets = ', '.join([f"{f}=%s" for f in fields])
    values = [data.get(f) for f in fields] + [iid]
    execute_db(f"UPDATE inventory SET {sets}, updated_at=NOW() WHERE id=%s", values)
    return jsonify({'success': True, 'message': '更新成功'})


@app.route('/api/inventory/<iid>', methods=['DELETE'])
def inventory_delete(iid):
    execute_db("DELETE FROM inventory WHERE id=%s", (iid,))
    return jsonify({'success': True, 'message': '删除成功'})


# =============================================
# 工单管理
# =============================================
@app.route('/api/work-orders')
def work_orders_list():
    """工单列表"""
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    status = request.args.get('status', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(order_no LIKE %s OR sap_aufnr LIKE %s OR material_code LIKE %s OR sap_matnr LIKE %s)")
        args.extend([f'%{search}%'] * 4)
    if status:
        conditions.append("status=%s")
        args.append(status)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM work_orders WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM work_orders WHERE {where} ORDER BY material_code ASC, planned_start ASC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )

    return jsonify({
        'success': True,
        'total': total,
        'page': page,
        'per_page': per_page,
        'data': serialize_rows(rows)
    })


@app.route('/api/work-orders', methods=['POST'])
def work_orders_create():
    """创建工单"""
    data = request.get_json() or {}
    wid = gen_id()
    data['id'] = wid

    fields = ['id', 'order_no', 'product_id', 'quantity', 'start_date', 'end_date',
              'status', 'priority', 'note', 'material_code', 'material_desc',
              'plant', 'workcenter_name', 'plan_qty', 'confirm_qty', 'unit',
              'batch', 'client_name', 'client_code', 'order_type', 'sales_order_id']

    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]

    execute_db(f"INSERT INTO work_orders ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': wid, 'message': '创建成功'})


@app.route('/api/work-orders/<wid>', methods=['PUT'])
def work_orders_update(wid):
    data = request.get_json() or {}
    fields = ['order_no', 'product_id', 'quantity', 'start_date', 'end_date',
              'status', 'priority', 'note', 'material_code', 'material_desc',
              'plant', 'workcenter_name', 'plan_qty', 'confirm_qty', 'unit',
              'batch', 'client_name', 'client_code', 'order_type', 'sales_order_id']
    sets = ', '.join([f"{f}=%s" for f in fields])
    values = [data.get(f) for f in fields] + [wid]
    execute_db(f"UPDATE work_orders SET {sets}, updated_at=NOW() WHERE id=%s", values)
    return jsonify({'success': True, 'message': '更新成功'})


@app.route('/api/work-orders/<wid>', methods=['DELETE'])
def work_orders_delete(wid):
    execute_db("DELETE FROM work_orders WHERE id=%s", (wid,))
    return jsonify({'success': True, 'message': '删除成功'})


@app.route('/api/work-orders/<wid>/items')
def work_order_items(wid):
    """获取工单组件明细"""
    rows = query_db("SELECT * FROM work_orders_items WHERE aufnr=%s", (wid,))
    return jsonify({'success': True, 'data': serialize_rows(rows)})


# =============================================
# 客户管理
# =============================================
@app.route('/api/customers')
def customers_list():
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
        f"SELECT * FROM customers WHERE {where} ORDER BY priority DESC, updated_at DESC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )

    return jsonify({
        'success': True,
        'total': total,
        'page': page,
        'per_page': per_page,
        'data': serialize_rows(rows)
    })


@app.route('/api/customers', methods=['POST'])
def customers_create():
    data = request.get_json() or {}
    cid = gen_id()
    data['id'] = cid
    fields = ['id', 'name', 'code', 'contact_person', 'phone', 'email', 'address', 'city', 'priority', 'note']
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]
    execute_db(f"INSERT INTO customers ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': cid, 'message': '创建成功'})


@app.route('/api/customers/<cid>', methods=['PUT'])
def customers_update(cid):
    data = request.get_json() or {}
    fields = ['name', 'code', 'contact_person', 'phone', 'email', 'address', 'city', 'priority', 'note']
    sets = ', '.join([f"{f}=%s" for f in fields])
    values = [data.get(f) for f in fields] + [cid]
    execute_db(f"UPDATE customers SET {sets}, updated_at=NOW() WHERE id=%s", values)
    return jsonify({'success': True, 'message': '更新成功'})


@app.route('/api/customers/<cid>', methods=['DELETE'])
def customers_delete(cid):
    execute_db("DELETE FROM customers WHERE id=%s", (cid,))
    return jsonify({'success': True, 'message': '删除成功'})


# =============================================
# BOM管理
# =============================================
@app.route('/api/bom')
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

    return jsonify({
        'success': True,
        'total': total,
        'page': page,
        'per_page': per_page,
        'data': serialize_rows(rows)
    })


@app.route('/api/bom', methods=['POST'])
def bom_create():
    data = request.get_json() or {}
    bom_key = data.get('bom_key')
    if not bom_key:
        bom_key = f"BOM-{data.get('parent_matnr','')}-{data.get('comp_matnr','')}-{data.get('werks','')}-{data.get('stlan','1')}-{data.get('stlal','1')}"
    data['bom_key'] = bom_key

    fields = ['bom_key', 'parent_matnr', 'parent_maktx', 'werks', 'stlan', 'stlal', 'posnr',
              'comp_matnr', 'comp_maktx', 'comp_menge', 'comp_meins', 'postp', 'lgort', 'ausch', 'sortf', 'data_source']
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]

    execute_db(
        f"INSERT INTO bom ({cols}) VALUES ({placeholders}) "
        f"ON DUPLICATE KEY UPDATE " + ', '.join([f"{f}=VALUES({f})" for f in fields if f != 'bom_key']),
        values
    )
    return jsonify({'success': True, 'bom_key': bom_key, 'message': '创建成功'})


@app.route('/api/bom/<bom_key>', methods=['DELETE'])
def bom_delete(bom_key):
    execute_db("DELETE FROM bom WHERE bom_key=%s", (bom_key,))
    return jsonify({'success': True, 'message': '删除成功'})


# =============================================
# SAP计划订单管理
# =============================================
@app.route('/api/sap/planned-orders')
def sap_planned_orders_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(plnum LIKE %s OR matnr LIKE %s OR maktx LIKE %s)")
        args.extend([f'%{search}%'] * 3)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM sap_planned_orders WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM sap_planned_orders WHERE {where} ORDER BY updated_at DESC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )

    return jsonify({
        'success': True,
        'total': total,
        'page': page,
        'per_page': per_page,
        'data': serialize_rows(rows)
    })


# =============================================
# 销售订单管理
# =============================================
@app.route('/api/sap/sales-orders')
def sap_sales_orders_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(vbeln LIKE %s OR kunnr LIKE %s OR name1 LIKE %s OR bstnk LIKE %s)")
        args.extend([f'%{search}%'] * 4)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM sap_sales_orders WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM sap_sales_orders WHERE {where} ORDER BY updated_at DESC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )

    return jsonify({
        'success': True,
        'total': total,
        'page': page,
        'per_page': per_page,
        'data': serialize_rows(rows)
    })


@app.route('/api/sap/sales-orders/<vbeln>/items')
def sap_sales_order_items(vbeln):
    rows = query_db("SELECT * FROM sap_sales_order_items WHERE vbeln=%s ORDER BY posnr", (vbeln,))
    return jsonify({'success': True, 'data': serialize_rows(rows)})


# =============================================
# 发货计划
# =============================================
@app.route('/api/delivery-plan/data-sources')
def delivery_plan_data_sources():
    try:
        so_count = query_db("SELECT COUNT(*) as cnt FROM sap_sales_orders", one=True)['cnt']
        wo_count = query_db("SELECT COUNT(*) as cnt FROM work_orders WHERE status!='CLSD'", one=True)['cnt']
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
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/delivery-plan/calculate', methods=['POST'])
def delivery_plan_calculate():
    """发货计划计算"""
    data = request.get_json() or {}
    priority_option = data.get('priority_option', 'stock_first')
    lead_days = int(data.get('lead_days', 7))

    try:
        # 获取销售订单行项目（有未发货数量的）
        items = query_db(
            "SELECT si.*, so.name1 as customer_name, so.vdatu, so.bstnk "
            "FROM sap_sales_order_items si "
            "JOIN sap_sales_orders so ON si.vbeln = so.vbeln "
            "WHERE si.nokwmeng > 0 OR (si.nokwmeng IS NULL AND si.kwmeng > 0) "
            "ORDER BY so.vdatu"
        )

        if not items:
            return jsonify({'success': True, 'message': '没有需要计算的发货计划', 'results': []})

        # 获取库存映射
        inv_map = get_inventory_map()

        results = []
        for item in items:
            matnr = item.get('matnr')
            need_qty = float(item.get('nokwmeng') or 0)
            if need_qty == 0:
                need_qty = float(item.get('kwmeng') or 0) - float(item.get('lfimg') or 0)

            stock = inv_map.get(matnr, {}).get('total', 0)

            if stock >= need_qty:
                fulfillment = '完全库存满足'
                can_deliver = '可发货'
            elif stock > 0:
                fulfillment = '库存部分满足'
                can_deliver = '不建议发货'
            else:
                fulfillment = '无法满足'
                can_deliver = '不可发货'

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
                'remaining_available': max(0, stock - need_qty),
            })

        # 清空旧结果并保存新结果
        execute_db("DELETE FROM delivery_plan_results")
        for i, r in enumerate(results):
            execute_db(
                """INSERT INTO delivery_plan_results
                   (line_no, created_date, delivery_date, material_code, material_desc, quantity, unit,
                    fulfillment_type, can_deliver, sales_order_no, customer_name, stock_qty,
                    remaining_available, due_date, lead_days, priority_option, calculation_batch)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (i + 1, datetime.now().strftime('%Y-%m-%d'), r.get('delivery_date'),
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
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/delivery-plan/results')
def delivery_plan_results():
    """发货计划结果"""
    rows = query_db("SELECT * FROM delivery_plan_results ORDER BY line_no")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


# =============================================
# 仓库仓位管理
# =============================================
@app.route('/api/warehouse-locations')
def warehouse_locations_list():
    rows = query_db("SELECT * FROM warehouse_locations ORDER BY warehouse_code")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@app.route('/api/warehouse-locations', methods=['POST'])
def warehouse_locations_create():
    data = request.get_json() or {}
    wid = gen_id()
    data['id'] = wid
    fields = ['id', 'warehouse_code', 'warehouse_name', 'warehouse_type', 'location_code', 'is_enabled', 'note']
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]
    execute_db(f"INSERT INTO warehouse_locations ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': wid, 'message': '创建成功'})


@app.route('/api/warehouse-locations/<wid>', methods=['DELETE'])
def warehouse_locations_delete(wid):
    execute_db("DELETE FROM warehouse_locations WHERE id=%s", (wid,))
    return jsonify({'success': True, 'message': '删除成功'})


# =============================================
# 条码库存管理
# =============================================
@app.route('/api/barcode-inventory')
def barcode_inventory_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    search = request.args.get('search', '')
    offset = (page - 1) * per_page

    conditions = []
    args = []
    if search:
        conditions.append("(material_code LIKE %s OR material_desc LIKE %s OR sap_batch LIKE %s)")
        args.extend([f'%{search}%'] * 3)

    where = " AND ".join(conditions) if conditions else "1=1"
    total = query_db(f"SELECT COUNT(*) as cnt FROM barcode_inventory WHERE {where}", args, one=True)['cnt']
    rows = query_db(
        f"SELECT * FROM barcode_inventory WHERE {where} ORDER BY created_at DESC LIMIT %s OFFSET %s",
        args + [per_page, offset]
    )

    return jsonify({
        'success': True,
        'total': total,
        'page': page,
        'per_page': per_page,
        'data': serialize_rows(rows)
    })


@app.route('/api/barcode-inventory', methods=['POST'])
def barcode_inventory_create():
    data = request.get_json() or {}
    bid = gen_id()
    data['id'] = bid
    fields = ['id', 'material_code', 'material_desc', 'quantity', 'unit',
              'warehouse_code', 'location_code', 'supplier_batch', 'sap_batch',
              'production_date', 'expiry_date', 'status', 'note']
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]
    execute_db(f"INSERT INTO barcode_inventory ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': bid, 'message': '创建成功'})


@app.route('/api/barcode-inventory/<bid>', methods=['DELETE'])
def barcode_inventory_delete(bid):
    execute_db("DELETE FROM barcode_inventory WHERE id=%s", (bid,))
    return jsonify({'success': True, 'message': '删除成功'})


# =============================================
# 无限量物料管理
# =============================================
@app.route('/api/unlimited-materials')
def unlimited_materials_list():
    rows = query_db("SELECT * FROM unlimited_materials ORDER BY created_at DESC")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@app.route('/api/unlimited-materials', methods=['POST'])
def unlimited_materials_create():
    data = request.get_json() or {}
    execute_db(
        "INSERT IGNORE INTO unlimited_materials (material_code, material_desc) VALUES (%s, %s)",
        (data.get('material_code'), data.get('material_desc'))
    )
    return jsonify({'success': True, 'message': '添加成功'})


@app.route('/api/unlimited-materials/<mid>', methods=['DELETE'])
def unlimited_materials_delete(mid):
    execute_db("DELETE FROM unlimited_materials WHERE id=%s", (mid,))
    return jsonify({'success': True, 'message': '删除成功'})


# =============================================
# 设备管理
# =============================================
@app.route('/api/equipment')
def equipment_list():
    rows = query_db("SELECT * FROM equipment ORDER BY workshop, name")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@app.route('/api/equipment', methods=['POST'])
def equipment_create():
    data = request.get_json() or {}
    eid = gen_id()
    data['id'] = eid
    fields = ['id', 'name', 'type', 'workshop', 'capacity', 'unit', 'status', 'note']
    cols = ', '.join(fields)
    placeholders = ', '.join(['%s'] * len(fields))
    values = [data.get(f) for f in fields]
    execute_db(f"INSERT INTO equipment ({cols}) VALUES ({placeholders})", values)
    return jsonify({'success': True, 'id': eid, 'message': '创建成功'})


@app.route('/api/equipment/<eid>', methods=['DELETE'])
def equipment_delete(eid):
    execute_db("DELETE FROM equipment WHERE id=%s", (eid,))
    return jsonify({'success': True, 'message': '删除成功'})


# =============================================
# 人员管理
# =============================================
@app.route('/api/staff')
def staff_list():
    rows = query_db("SELECT * FROM staff ORDER BY team, name")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@app.route('/api/staff', methods=['POST'])
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


@app.route('/api/staff/<sid>', methods=['DELETE'])
def staff_delete(sid):
    execute_db("DELETE FROM staff WHERE id=%s", (sid,))
    return jsonify({'success': True, 'message': '删除成功'})


# =============================================
# 数据库管理
# =============================================
@app.route('/api/database/test')
def database_test():
    try:
        row = query_db("SELECT VERSION() as version, DATABASE() as db", one=True)
        return jsonify({
            'success': True,
            'message': '数据库连接成功',
            'version': row['version'],
            'database': row['db'],
            'host': DB_CONFIG['host'],
            'port': DB_CONFIG['port'],
            'type': 'MySQL'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


@app.route('/api/database/status')
def database_status():
    try:
        tables = query_db("SHOW TABLES")
        table_list = [list(t.values())[0] for t in tables]
        return jsonify({
            'success': True,
            'connected': True,
            'database': DB_CONFIG['database'],
            'host': DB_CONFIG['host'],
            'port': DB_CONFIG['port'],
            'type': 'MySQL',
            'tables': table_list,
            'table_count': len(table_list)
        })
    except Exception as e:
        return jsonify({'success': False, 'connected': False, 'error': str(e)}), 500


@app.route('/api/database/query', methods=['POST'])
def database_query():
    data = request.get_json() or {}
    sql = data.get('sql', '').strip()
    if not sql:
        return jsonify({'success': False, 'message': 'SQL不能为空'}), 400

    # 安全限制：只允许SELECT
    if not sql.upper().startswith('SELECT') and not sql.upper().startswith('SHOW') and not sql.upper().startswith('DESC'):
        return jsonify({'success': False, 'message': '只允许查询操作(SELECT/SHOW/DESC)'}), 403

    try:
        rows = query_db(sql)
        return jsonify({
            'success': True,
            'row_count': len(rows),
            'data': serialize_rows(rows)
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)}), 500


# =============================================
# 系统配置
# =============================================
@app.route('/api/config/save', methods=['POST'])
def config_save():
    data = request.get_json() or {}
    for k, v in data.items():
        set_config(k, v)
    return jsonify({'success': True, 'message': '配置已保存'})


@app.route('/api/config/load')
def config_load():
    rows = query_db("SELECT config_key, config_value FROM system_config")
    config = {r['config_key']: r['config_value'] for r in rows}
    return jsonify({'success': True, 'config': config})


@app.route('/api/system/config')
def system_config():
    rows = query_db("SELECT config_key, config_value FROM system_config")
    config = {r['config_key']: r['config_value'] for r in rows}
    return jsonify({'success': True, 'config': config})


@app.route('/api/system/config/mac', methods=['POST'])
def set_mac():
    data = request.get_json() or {}
    mac = data.get('mac_address', '')
    machine_name = data.get('machine_name', '')
    execute_db(
        "INSERT INTO machine_configs (id, mac_address, machine_name, db_host, db_port, db_user, db_name, is_authorized, updated_at) "
        "VALUES (%s, %s, %s, %s, %s, %s, %s, 1, NOW()) "
        "ON DUPLICATE KEY UPDATE machine_name=%s, is_authorized=1, updated_at=NOW()",
        (gen_id(), mac, machine_name, DB_CONFIG['host'], DB_CONFIG['port'], DB_CONFIG['user'], DB_CONFIG['database'], machine_name)
    )
    return jsonify({'success': True, 'message': 'MAC地址已设置'})


@app.route('/api/system/verify-machine', methods=['POST'])
def verify_machine():
    return jsonify({'success': True, 'verified': True, 'message': '本机验证通过'})


# =============================================
# SAP接口 (模拟)
# =============================================
@app.route('/api/sap/orders', methods=['POST'])
def sap_orders():
    """模拟读取SAP计划订单"""
    return jsonify({
        'success': False,
        'message': 'SAP接口未连接，请在系统配置中设置SAP连接信息。当前可使用本地数据库中的计划订单进行排程计算。'
    })


@app.route('/api/sap/work-orders', methods=['POST'])
def sap_work_orders():
    return jsonify({
        'success': False,
        'message': 'SAP接口未连接。当前可使用本地数据库中的工单数据。'
    })


@app.route('/api/sap/inventory', methods=['POST'])
def sap_inventory():
    return jsonify({
        'success': False,
        'message': 'SAP接口未连接。当前可使用本地数据库中的库存数据。'
    })


@app.route('/api/sap/master-data', methods=['POST'])
def sap_master_data():
    return jsonify({
        'success': False,
        'message': 'SAP接口未连接。当前可使用本地数据库中的物料主数据。'
    })


@app.route('/api/sap/bom', methods=['POST'])
def sap_bom():
    return jsonify({
        'success': False,
        'message': 'SAP接口未连接。当前可使用本地数据库中的BOM数据。'
    })


@app.route('/api/sap/customers', methods=['POST'])
def sap_customers():
    return jsonify({
        'success': False,
        'message': 'SAP接口未连接。当前可使用本地数据库中的客户数据。'
    })


@app.route('/api/sap/sales-orders/read', methods=['POST'])
def sap_sales_orders_read():
    return jsonify({
        'success': False,
        'message': 'SAP接口未连接。当前可使用本地数据库中的销售订单数据。'
    })


@app.route('/api/config/sap')
def config_sap_get():
    config = {
        'prd_host': get_config('sap_prd_host', ''),
        'prd_port': get_config('sap_prd_port', ''),
        'prd4_host': get_config('sap_prd4_host', ''),
        'prd4_port': get_config('sap_prd4_port', ''),
        'qas_host': get_config('sap_qas_host', ''),
        'qas_port': get_config('sap_qas_port', ''),
        'user': get_config('sap_user', ''),
    }
    return jsonify({'success': True, 'config': config})


@app.route('/api/config/sap', methods=['POST'])
def config_sap_save():
    data = request.get_json() or {}
    for k, v in data.items():
        set_config(f'sap_{k}', v)
    return jsonify({'success': True, 'message': 'SAP配置已保存'})


@app.route('/api/config/sap/test', methods=['POST'])
def config_sap_test():
    return jsonify({'success': False, 'message': 'SAP连接测试：未连接（需要网络环境支持）'})


# =============================================
# 计算逻辑配置
# =============================================
@app.route('/api/calc-logic/config')
def calc_logic_config():
    return jsonify({
        'success': True,
        'config': {
            'base_score': {'A': 100, 'B': 50, 'C': 0},
            'urgency_score': {
                'overdue': 30, '3days': 25, '7days': 20,
                '14days': 15, '30days': 10, 'beyond': 5, 'none': 0
            },
            'qty_score': {'1000+': 10, '500+': 8, '100+': 5, '<100': 2},
        }
    })


@app.route('/api/calc-logic/presets')
def calc_logic_presets():
    return jsonify({
        'success': True,
        'data': [
            {'id': 'default', 'name': '默认方案', 'description': '标准优先级计算', 'active': True},
            {'id': 'urgent', 'name': '紧急优先', 'description': '交期紧迫度权重加倍', 'active': False},
            {'id': 'qty', 'name': '大批量优先', 'description': '数量权重加倍', 'active': False},
        ]
    })


# =============================================
# 数据管理
# =============================================
@app.route('/api/data/backup', methods=['POST'])
def data_backup():
    """数据备份"""
    snapshot_name = f"backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"

    tables = query_db("SHOW TABLES")
    all_data = {}
    for t in tables:
        table_name = list(t.values())[0]
        rows = query_db(f"SELECT * FROM `{table_name}`")
        all_data[table_name] = serialize_rows(rows)

    execute_db(
        "INSERT INTO data_snapshots (snapshot_name, snapshot_data, created_by) VALUES (%s, %s, %s)",
        (snapshot_name, json.dumps(all_data, ensure_ascii=False, default=str), 'system')
    )

    return jsonify({
        'success': True,
        'message': f'数据备份成功：{snapshot_name}',
        'snapshot_name': snapshot_name,
        'table_count': len(all_data)
    })


@app.route('/api/data/snapshots')
def data_snapshots():
    rows = query_db("SELECT id, snapshot_name, created_by, created_at FROM data_snapshots ORDER BY created_at DESC")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@app.route('/api/data/log')
def data_log():
    limit = int(request.args.get('limit', 50))
    rows = query_db("SELECT * FROM data_log ORDER BY created_at DESC LIMIT %s", (limit,))
    return jsonify({'success': True, 'data': serialize_rows(rows)})


# =============================================
# SAP订单类型列表
# =============================================
@app.route('/api/sap/sales-orders/auart-list')
def sap_auart_list():
    rows = query_db("SELECT DISTINCT auart FROM sap_sales_orders WHERE auart IS NOT NULL ORDER BY auart")
    return jsonify({'success': True, 'data': [r['auart'] for r in rows]})


@app.route('/api/delivery-plan/data-sources/detail/<source>')
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


@app.route('/api/delivery-plan/warehouses-enabled')
def delivery_plan_warehouses():
    rows = query_db("SELECT warehouse_code, warehouse_name FROM warehouse_locations WHERE is_enabled=1")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@app.route('/api/delivery-plan/material-details/<matnr>')
def delivery_plan_material_details(matnr):
    inv = query_db("SELECT * FROM inventory WHERE material_code=%s OR sap_matnr=%s", (matnr, matnr), one=True)
    bom_rows = query_db("SELECT * FROM bom WHERE parent_matnr=%s", (matnr,))
    return jsonify({
        'success': True,
        'inventory': serialize_row(inv),
        'bom': serialize_rows(bom_rows)
    })


@app.route('/api/delivery-plan/diag')
def delivery_plan_diag():
    return jsonify({
        'success': True,
        'sales_orders': query_db("SELECT COUNT(*) as cnt FROM sap_sales_orders", one=True)['cnt'],
        'work_orders': query_db("SELECT COUNT(*) as cnt FROM work_orders", one=True)['cnt'],
        'inventory': query_db("SELECT COUNT(*) as cnt FROM inventory", one=True)['cnt'],
    })


@app.route('/api/delivery-plan/read-all', methods=['POST'])
def delivery_plan_read_all():
    return jsonify({
        'success': False,
        'message': 'SAP接口未连接，请使用本地数据库数据。'
    })


# =============================================
# 销售订单（旧版接口）
# =============================================
@app.route('/api/sales-orders/list')
def sales_orders_list():
    page = int(request.args.get('page', 1))
    per_page = int(request.args.get('per_page', 20))
    offset = (page - 1) * per_page
    total = query_db("SELECT COUNT(*) as cnt FROM sap_sales_orders", one=True)['cnt']
    rows = query_db("SELECT * FROM sap_sales_orders ORDER BY updated_at DESC LIMIT %s OFFSET %s", (per_page, offset))
    return jsonify({'success': True, 'total': total, 'data': serialize_rows(rows)})


@app.route('/api/sales-orders')
def sales_orders():
    return sales_orders_list()


@app.route('/api/sales-orders/load-from-db')
def sales_orders_load_from_db():
    rows = query_db("SELECT * FROM sap_sales_orders ORDER BY updated_at DESC LIMIT 100")
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@app.route('/api/sales-orders/next-serial')
def sales_orders_next_serial():
    """返回下一个销售订单流水号 XS+3位"""
    row = query_db("SELECT MAX(vbeln) as max_no FROM sap_sales_orders WHERE vbeln LIKE 'XS%'", one=True)
    max_no = row.get('max_no') if row else None
    if max_no:
        try:
            num = int(max_no[2:]) + 1
        except:
            num = 1
    else:
        num = 1
    return jsonify({'success': True, 'order_no': f'XS{num:03d}'})


# =============================================
# 主入口
# =============================================
if __name__ == '__main__':
    port = int(get_config('flask_port', '5000'))
    print(f"""
╔══════════════════════════════════════════════╗
║       孔雀排程系统 V3.0 — 启动中              ║
║       数据库: MySQL ({DB_CONFIG['host']}:{DB_CONFIG['port']})
║       数据库名: {DB_CONFIG['database']}
║       端口: {port}
╚══════════════════════════════════════════════╝
    """)
    app.run(host='0.0.0.0', port=port, debug=False)
