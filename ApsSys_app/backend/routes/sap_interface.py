# -*- coding: utf-8 -*-
"""SAP接口（模拟）+ SAP配置"""

from flask import Blueprint, request, jsonify

from ..helpers import get_config, set_config

bp = Blueprint('sap_interface', __name__)


def _not_connected():
    return {
        'success': False,
        'message': 'SAP接口未连接，请在系统配置中设置SAP连接信息。当前可使用本地数据库中的数据。'
    }


@bp.route('/api/sap/orders', methods=['POST'])
def sap_orders():
    return jsonify(_not_connected())


@bp.route('/api/sap/work-orders', methods=['POST'])
def sap_work_orders():
    return jsonify(_not_connected())


@bp.route('/api/sap/inventory', methods=['POST'])
def sap_inventory():
    return jsonify(_not_connected())


@bp.route('/api/sap/master-data', methods=['POST'])
def sap_master_data():
    return jsonify(_not_connected())


@bp.route('/api/sap/bom', methods=['POST'])
def sap_bom():
    return jsonify(_not_connected())


@bp.route('/api/sap/customers', methods=['POST'])
def sap_customers():
    return jsonify(_not_connected())


@bp.route('/api/sap/sales-orders/read', methods=['POST'])
def sap_sales_orders_read():
    return jsonify(_not_connected())


@bp.route('/api/config/sap')
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


@bp.route('/api/config/sap', methods=['POST'])
def config_sap_save():
    data = request.get_json() or {}
    for k, v in data.items():
        set_config(f'sap_{k}', v)
    return jsonify({'success': True, 'message': 'SAP配置已保存'})


@bp.route('/api/config/sap/test', methods=['POST'])
def config_sap_test():
    return jsonify({'success': False, 'message': 'SAP连接测试：未连接（需要网络环境支持）'})
