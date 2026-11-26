# -*- coding: utf-8 -*-
"""业务逻辑辅助函数"""

from .database import query_db, execute_db


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


def get_transit_by_material():
    """采购在途（SAP在途）= 采购入库中状态为'在途'的该物料'计划到货数量'(arrival_qty)之和。

    仅统计 status='在途'（已关闭/作废不计入在途）。返回 {material_code: float}。
    这是 SAP在途 的唯一真相源：库存页展示、MRP 在途库存类型、缺料/发货导出均从此派生，
    不再读取手工录入的 sap_umlme 列。
    """
    rows = query_db(
        "SELECT material_code, COALESCE(SUM(arrival_qty),0) AS s "
        "FROM purchase_orders WHERE status='在途' "
        "AND material_code IS NOT NULL AND material_code<>'' "
        "GROUP BY material_code"
    )
    return {r['material_code']: float(r.get('s') or 0) for r in rows}


def build_inventory_pool(stock_types=None, warehouses=None):
    """统一库存池（MRP / 缺料 / 发货计划共用真相源）。

    按物料汇总所选库存类型的可用量：GROUP BY material_code + SUM(列)。
    返回 {material_code: available(float)}。
    'transit'(SAP在途) 不再读手工列，统一取采购入库'在途'的计划到货数量之和。
    """
    stock_map = {
        'unrestricted': 'sap_labst',
        'quality': 'sap_insme',
        'blocked': 'sap_speme',
    }
    if stock_types is None:
        stock_types = ['unrestricted']
    cols = [f"COALESCE({stock_map[t]},0)" for t in stock_types if t in stock_map]
    expr = 'SUM(' + ' + '.join(cols) + ')' if cols else '0'

    conditions = []
    args = []
    if warehouses:
        placeholders = ','.join(['%s'] * len(warehouses))
        conditions.append(f"sap_lgort IN ({placeholders})")
        args.extend(warehouses)

    where = " AND ".join(conditions) if conditions else "1=1"
    sql = (
        f"SELECT material_code, ({expr}) as available "
        f"FROM inventory WHERE {where} GROUP BY material_code"
    )
    rows = query_db(sql, args)
    pool = {r['material_code']: float(r.get('available') or 0) for r in rows}

    # SAP在途（transit）：统一取采购入库'在途'的计划到货数量之和
    if 'transit' in stock_types:
        for mat, v in get_transit_by_material().items():
            pool[mat] = pool.get(mat, 0) + v
    return pool


def get_inventory_map(warehouses=None, stock_types=None):
    """获取库存映射 {material_code: stock_info}
       返回每个物料的 SAP 库存明细 + 按 stock_types 过滤的可用库存。
       可用量统一委托 build_inventory_pool，确保与 MRP 引擎同源。
    """
    if stock_types is None:
        stock_types = ['unrestricted']

    conditions = []
    args = []
    if warehouses:
        placeholders = ','.join(['%s'] * len(warehouses))
        conditions.append(f"sap_lgort IN ({placeholders})")
        args.extend(warehouses)
    where = " AND ".join(conditions) if conditions else "1=1"

    # 可用量口径与 MRP 完全一致
    pool = build_inventory_pool(stock_types, warehouses)

    sql = f"""
        SELECT material_code,
               MAX(name) as name,
               MAX(unit) as unit,
               SUM(sap_labst) as sap_labst,
               SUM(sap_insme) as sap_insme,
               SUM(sap_speme) as sap_speme
        FROM inventory
        WHERE {where}
        GROUP BY material_code
    """
    rows = query_db(sql, args)

    # SAP在途 单一真相源：采购入库'在途'的计划到货数量之和（不再读手工列）
    transit_map = get_transit_by_material()

    inv_map = {}
    for r in rows:
        labst = float(r.get('sap_labst') or 0)
        insme = float(r.get('sap_insme') or 0)
        speme = float(r.get('sap_speme') or 0)
        umlme = transit_map.get(r['material_code'], 0.0)
        avail = pool.get(r['material_code'], 0)
        inv_map[r['material_code']] = {
            'total': labst + insme + speme + umlme,
            'available': avail,
            'name': r.get('name'),
            'unit': r.get('unit'),
            'sap_labst': labst,
            'sap_insme': insme,
            'sap_speme': speme,
            'sap_umlme': umlme,
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
