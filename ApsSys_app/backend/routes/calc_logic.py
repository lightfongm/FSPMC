# -*- coding: utf-8 -*-
"""计算逻辑配置"""

from flask import Blueprint, jsonify, request

from ..database import query_db, execute_db
from ..helpers import get_config, set_config

bp = Blueprint('calc_logic', __name__)


@bp.route('/api/calc-logic/config')
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


@bp.route('/api/calc-logic/presets')
def calc_logic_presets():
    active_id = get_config('calc_preset', 'default')
    return jsonify({
        'success': True,
        'data': [
            {'id': 'default', 'name': '默认方案', 'description': '标准优先级计算', 'active': active_id == 'default'},
            {'id': 'urgent', 'name': '紧急优先', 'description': '交期紧迫度权重加倍', 'active': active_id == 'urgent'},
            {'id': 'qty', 'name': '大批量优先', 'description': '数量权重加倍', 'active': active_id == 'qty'},
        ]
    })


@bp.route('/api/calc-logic/presets/activate', methods=['POST'])
def calc_logic_presets_activate():
    data = request.get_json() or {}
    preset_id = data.get('preset_id', 'default')
    set_config('calc_preset', preset_id)
    return jsonify({'success': True, 'message': f'已切换至「{preset_id}」方案'})


@bp.route('/api/calc-logic/modules')
def calc_logic_modules():
    """获取排程模块开关状态"""
    modules = [
        {'id': 'readiness', 'name': '齐备套计算', 'desc': '逐工单判断子件齐备状态'},
        {'id': 'priority', 'name': '客户等级计算', 'desc': '计算优先级分数并排序'},
        {'id': 'delivery', 'name': '发货计划', 'desc': '库存满足分析并生成发货建议'},
        {'id': 'shortage', 'name': '缺料分析', 'desc': '物料短缺分析与预警'},
        {'id': 'capacity', 'name': '产能分析', 'desc': '设备与人员产能评估'},
        {'id': 'auto_order', 'name': '自动排产', 'desc': '自动生成生产工单建议'},
        {'id': 'notification', 'name': '通知提醒', 'desc': '缺料/逾期自动通知'},
        {'id': 'plan_req_date', 'name': '计划需求日期', 'desc': '需求日期必须大于等于当日', 'default': '0'},
        {'id': 'equipment_capacity', 'name': '设备产能工期',
         'desc': '工期=数量/(设备每小时产能×24)；需先统一产能单位并补齐空值，缺产能回退日产量', 'default': '0'},
        {'id': 'changeover_matrix', 'name': '序列相关换型',
         'desc': '换型时长取「前一物料→后一物料」矩阵；同物料连产为0，未配置回退物料自身换型时间', 'default': '1'},
        {'id': 'schedule_jit', 'name': '倒排JIT',
         'desc': '尽量贴近交期开工（减少成品库存与货龄风险）；关闭为正排ASAP', 'default': '0'},
        {'id': 'semi_schedule_floor', 'name': '自制件排程日回流',
         'desc': '半成品/产成品的排程完工日作为父件开工下限（MRP与排产闭环）', 'default': '1'},
        {'id': 'schedule_search', 'name': '排产局部搜索(L2)',
         'desc': '在设备作业序列上做模拟退火优化（迁移/换位/换序）；关闭则只用贪心结果', 'default': '1'},
        # 【L4】TOC/DBR：认出约束资源（鼓）、给足搜索预算、上游配套缓冲、非瓶颈按鼓节拍释放
        {'id': 'toc_dbr', 'name': '瓶颈识别TOC(L4)',
         'desc': '识别瓶颈设备(鼓)并给足优化预算；上游自制件按「鼓开工-缓冲天数」锁完工；'
                 '非瓶颈设备不做提前投产(绳)，减少WIP与库存货龄', 'default': '1'},
    ]
    for m in modules:
        m['enabled'] = get_config(f'module_{m["id"]}', m.get('default', '1')) == '1'
    return jsonify({'success': True, 'data': modules})


@bp.route('/api/calc-logic/modules/toggle', methods=['POST'])
def calc_logic_modules_toggle():
    data = request.get_json() or {}
    mid = data.get('module_id')
    enabled = data.get('enabled', True)
    if mid:
        set_config(f'module_{mid}', '1' if enabled else '0')
        return jsonify({'success': True, 'message': f'模块「{mid}」已{"启用" if enabled else "禁用"}'})
    return jsonify({'success': False, 'message': '缺少module_id'}), 400
