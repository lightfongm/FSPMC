# -*- coding: utf-8 -*-
"""系统配置"""

from flask import Blueprint, request, jsonify

from ..database import query_db, execute_db
from ..utils import gen_id
from ..helpers import get_config, set_config
from ..config import DB_CONFIG

bp = Blueprint('system_config', __name__)

# 【P1-11 修复 · 2026-10-14】`/api/config/save` 的**键名白名单**。
# 原实现把请求体每个 key 原样 `set_config` —— 无任何校验，调用方可借此覆盖
# `merge_days` / `schedule_merge_days` / `module_*` 等业务开关与计算参数
# （这些键直接影响排程与缺料口径），属"配置注入"。
# 现按"来源"分三类放行，其余一律拒绝并在回执中列出：
#   ① 精确白名单：SAP 连接配置（前端 SAP 设置页）
#   ② 前缀白名单：sap_（SAP 相关扩展键）、module_（功能模块开关，由 /api/modules 走
#      calc_logic 的 set_config，此处仅为兼容保留）
#   ③ 业务键 schedule_merge_days / merge_days：**不允许**由本接口写入 ——
#      它们由「一键计算」按本次计算实际使用的参数持久化（schedule.py:2455-2456），
#      交由通用接口随意改会让"输入框显示值"与"库内数据口径"脱节。
#   ④ 【P0-2 · 2026-10-20】schedule_overdue_mode（过期归位模式 clamp_today/forward_capacity）：
#      排程计算页「过期归位模式」下拉即经本接口持久化，与 module_*/合并天数同属计算口径配置。
_CONFIG_SAVE_WHITELIST = {
    'sap_prd_host', 'sap_prd_port', 'sap_prd4_host', 'sap_prd4_port',
    'sap_qas_host', 'sap_qas_port', 'sap_user', 'sap_client', 'sap_system_number',
    'app_name', 'app_version', 'flask_port',
    'schedule_overdue_mode',
}
_CONFIG_SAVE_PREFIXES = ('sap_', 'module_')


@bp.route('/api/config/save', methods=['POST'])
def config_save():
    data = request.get_json() or {}
    saved, rejected = [], []
    for k, v in data.items():
        k = str(k)
        if k in _CONFIG_SAVE_WHITELIST or k.startswith(_CONFIG_SAVE_PREFIXES):
            set_config(k, v)
            saved.append(k)
        else:
            rejected.append(k)
    if rejected and not saved:
        return jsonify({'success': False,
                        'message': '以下配置键不允许保存：%s' % '、'.join(rejected)}), 400
    msg = '配置已保存'
    if rejected:
        msg += '（已忽略不允许保存的键：%s）' % '、'.join(rejected)
    return jsonify({'success': True, 'message': msg, 'saved': saved, 'rejected': rejected})


@bp.route('/api/config/load')
def config_load():
    rows = query_db("SELECT config_key, config_value FROM system_config")
    config = {r['config_key']: r['config_value'] for r in rows}
    return jsonify({'success': True, 'config': config})


@bp.route('/api/system/config')
def system_config():
    rows = query_db("SELECT config_key, config_value FROM system_config")
    config = {r['config_key']: r['config_value'] for r in rows}
    return jsonify({'success': True, 'config': config})


@bp.route('/api/system/config/mac', methods=['POST'])
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


@bp.route('/api/system/verify-machine', methods=['POST'])
def verify_machine():
    return jsonify({'success': True, 'verified': True, 'message': '本机验证通过'})
