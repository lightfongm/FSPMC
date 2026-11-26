# -*- coding: utf-8 -*-
"""一键排产 L3（只读沙盘试算内核）+ L4（TOC/DBR：鼓-缓冲-绳）回归基线。

设计原则与既有用例一致：
  · 纯函数直接单测（_wo_toc_drum / _wo_toc_upstream_targets）；
  · 涉及库的用例用临时工单（id 前缀 L34-），并**在 fixture 里必定清理**；
  · L3 的核心是"试算不写库"，故每条用例都做排产前后的数据快照比对。
"""

from datetime import datetime, timedelta

import pytest

from backend.routes import work_orders as wo


# --------------------------------------------------------------------------
# 临时可排工单（用完必删）
# --------------------------------------------------------------------------
_L34_WO = [
    ('L34-1', 'L34A1', 'C002', '20301220'),   # C002 候选设备唯一 → 制造瓶颈
    ('L34-2', 'L34A2', 'C002', '20301225'),
    ('L34-3', 'L34A3', 'C001', '20301222'),
]


@pytest.fixture
def client():
    from backend import create_app
    return create_app().test_client()


@pytest.fixture
def temp_orders():
    """插入 3 张「创建」工单 + 对应排程行，用后必定删除（不论用例是否失败）。"""
    for wid, aufnr, mat, due in _L34_WO:
        wo.execute_db(
            "INSERT INTO peacock_schedule (id, aufnr, material_code, batch_date_end) "
            "VALUES (%s, %s, %s, %s)", ('PS-' + aufnr, aufnr, mat, due))
        wo.execute_db(
            "INSERT INTO work_orders (id, order_no, product_id, material_code, status, source, "
            "plan_qty, quantity, schedule_ref, start_date, end_date, client_name, manual_locked) "
            "VALUES (%s, %s, %s, %s, '创建', 'L34TEST', 100, 100, %s, %s, %s, 'L34客户', 0)",
            (wid, 'SO-' + wid, mat, mat, aufnr, due, due))
    try:
        yield [r[0] for r in _L34_WO]
    finally:
        for wid, aufnr, _mat, _due in _L34_WO:
            wo.execute_db("DELETE FROM work_orders WHERE id=%s", (wid,))
            wo.execute_db("DELETE FROM peacock_schedule WHERE aufnr=%s", (aufnr,))


def _snapshot(ids):
    rows = wo.query_db(
        "SELECT id, workcenter_name, planned_start, planned_end, material_status "
        "FROM work_orders WHERE id IN (%s)" % ','.join(['%s'] * len(ids)), tuple(ids)) or []
    return {r['id']: tuple(str(r.get(k)) for k in
                           ('workcenter_name', 'planned_start', 'planned_end', 'material_status'))
            for r in rows}


# ==========================================================================
# L4·Drum：瓶颈识别
# ==========================================================================
def test_drum_picks_highest_load():
    assigned = [{'oid': 'A', 'eq': 'E1', 'lead': 5, 'cands': ['E1', 'E2']},
                {'oid': 'B', 'eq': 'E1', 'lead': 5, 'cands': ['E1']},
                {'oid': 'C', 'eq': 'E2', 'lead': 6, 'cands': ['E2']}]
    drum, rank = wo._wo_toc_drum(assigned, {})
    assert drum == 'E1'
    assert rank[0]['equipment'] == 'E1'
    assert rank[0]['load_days'] == 10.0


def test_drum_weighted_by_captive_jobs():
    """负荷略低但『无处可去』的设备才是真瓶颈 —— 专属度必须参与评分。"""
    assigned = [{'oid': 'A', 'eq': 'E1', 'lead': 10, 'cands': ['E1', 'E2']},
                {'oid': 'B', 'eq': 'E2', 'lead': 4, 'cands': ['E2']},
                {'oid': 'C', 'eq': 'E2', 'lead': 4, 'cands': ['E2']}]
    drum, rank = wo._wo_toc_drum(assigned, {})
    # E1 负荷 10 天但可外协；E2 负荷 8 天却有 2 张单无处可去 → E2 为鼓
    assert drum == 'E2'
    e1 = next(r for r in rank if r['equipment'] == 'E1')
    e2 = next(r for r in rank if r['equipment'] == 'E2')
    assert e1['load_days'] > e2['load_days']
    assert e2['captive_jobs'] == 2
    assert e2['score'] > e1['score']


def test_drum_includes_static_load():
    """静态占用（锁定单/未参与本轮的创建单）必须计入负荷，否则会漏掉"已被占满"的设备。"""
    assigned = [{'oid': 'A', 'eq': 'E1', 'lead': 1, 'cands': ['E1']}]
    static = {'E2': 30.0}
    drum, rank = wo._wo_toc_drum(assigned, static)
    assert drum == 'E2'
    assert next(r for r in rank if r['equipment'] == 'E2')['load_days'] == 30.0


def test_drum_empty_when_nothing_assigned():
    drum, rank = wo._wo_toc_drum([], {})
    assert drum is None
    assert rank == []


# ==========================================================================
# L4·Buffer：上游供给目标完工日
# ==========================================================================
def _mk_assigned(oid, eq, mat, es_days_from_base):
    es = datetime(2026, 10, 10) + timedelta(days=es_days_from_base)
    return {'oid': oid, 'eq': eq, 'mat': mat, 'es': es, 'pe': es + timedelta(days=2),
            'lead': 2, 'cands': [eq], 'due': es + timedelta(days=10), 'order_no': 'X-' + oid}


def test_upstream_target_is_drum_start_minus_buffer(monkeypatch):
    """上游自制件的目标完工 = 消费它的鼓工单计划开工 − 缓冲天数。"""
    monkeypatch.setattr(wo, '_BOM_ROWS', {'C001': ['B001']})
    monkeypatch.setattr(wo, '_PROD_ROWS', {'B001': {'type': '半成品'}})
    assigned = [_mk_assigned('DRUM1', 'EQ鼓', 'C001', 0),      # 开工 10-10
                _mk_assigned('UP1', 'EQ其它', 'B001', -5)]
    targets = wo._wo_toc_upstream_targets(assigned, 'EQ鼓', 1.0)
    assert list(targets.keys()) == ['UP1']
    assert targets['UP1'] == datetime(2026, 10, 9)            # 10-10 − 1 天


def test_upstream_skips_purchased_and_same_drum(monkeypatch):
    """原辅料不参与（不存在对应工单）；同在鼓上的自制件也不构成上下游关系。"""
    monkeypatch.setattr(wo, '_BOM_ROWS', {'C001': ['M001', 'B001'], 'B001': ['M003']})
    monkeypatch.setattr(wo, '_PROD_ROWS', {'M001': {'type': '原辅料'},
                                           'M003': {'type': '原辅料'},
                                           'B001': {'type': '半成品'}})
    assigned = [_mk_assigned('DRUM1', 'EQ鼓', 'C001', 0),
                _mk_assigned('SAMEDRUM', 'EQ鼓', 'B001', -3),   # 也在鼓上 → 跳过
                _mk_assigned('UP1', 'EQ其它', 'B001', -5)]
    targets = wo._wo_toc_upstream_targets(assigned, 'EQ鼓', 1.0)
    assert list(targets.keys()) == ['UP1']


def test_upstream_finds_intermediate_semi_not_only_leaves(monkeypatch):
    """🔴 回归防线：给鼓供料的是**中间层自制件**（C004→B001→B002）。

    `_wo_explode_components` 只返回叶子采购件，用它做上游识别会把 B001/B002 全漏掉，
    于是"绳"把上游推到接近交期、而鼓却提前开工 → 瓶颈被饿死。本用例即防此退化的。
    """
    monkeypatch.setattr(wo, '_BOM_ROWS', {'C004': ['B001', 'M001'], 'B001': ['B002', 'M003'],
                                          'B002': ['M001']})
    monkeypatch.setattr(wo, '_PROD_ROWS', {'B001': {'type': '半成品'},
                                           'B002': {'type': '半成品'},
                                           'M001': {'type': '原辅料'},
                                           'M003': {'type': '原辅料'}})
    assigned = [_mk_assigned('DRUM1', 'EQ鼓', 'C004', 0),      # 10-10 开工
                _mk_assigned('UP_SEMI', 'EQ其它', 'B001', -9),
                _mk_assigned('UP_SUB', 'EQ其它2', 'B002', -9)]
    targets = wo._wo_toc_upstream_targets(assigned, 'EQ鼓', 1.0)
    assert set(targets.keys()) == {'UP_SEMI', 'UP_SUB'}       # 两层自制件都识别到
    assert all(v == datetime(2026, 10, 9) for v in targets.values())


def test_upstream_uses_earliest_consumer(monkeypatch):
    """多个鼓工单消费同一自制件时，取最早的那个限制。"""
    monkeypatch.setattr(wo, '_BOM_ROWS', {'C001': ['B001']})
    monkeypatch.setattr(wo, '_PROD_ROWS', {'B001': {'type': '半成品'}})
    assigned = [_mk_assigned('DRUM1', 'EQ鼓', 'C001', 0),      # 10-10
                _mk_assigned('DRUM2', 'EQ鼓', 'C001', 4),      # 10-14
                _mk_assigned('UP1', 'EQ其它', 'B001', -6)]
    targets = wo._wo_toc_upstream_targets(assigned, 'EQ鼓', 2.0)
    assert targets['UP1'] == datetime(2026, 10, 8)             # 取 10-10 那条


def test_upstream_empty_without_drum():
    assert wo._wo_toc_upstream_targets([_mk_assigned('A', 'E1', 'C001', 0)], None, 1.0) == {}


# ==========================================================================
# L3：排产内核只读
# ==========================================================================
def test_plan_returns_full_contract():
    plan = wo._wo_plan()
    for k in ('empty', 'today0', 'assigned', 'stats', 'decisions', 'touched',
              'search_info', 'conflict_eqs', 'excluded_eq', 'mat_meta',
              'locked_skipped', 'eq_load', 'maint_occ', 'toc'):
        assert k in plan, 'plan 缺少字段 %s' % k
    if plan['empty']:
        assert plan['assigned'] == [] and plan['decisions'] == []


def test_plan_is_read_only(temp_orders):
    """【L3 核心】跑完排产内核，库里一张单都不许变。"""
    before = _snapshot(temp_orders)
    plan = wo._wo_plan()
    after = _snapshot(temp_orders)
    assert before == after, '排产内核写库了 —— 沙盘试算必须是只读的'
    assert plan['empty'] is False
    assert len(plan['assigned']) >= len(temp_orders)


def test_plan_toc_structure(temp_orders):
    """【L4】TOC 三件套的回执结构必须完整可读。"""
    plan = wo._wo_plan()
    toc = plan['toc']
    assert isinstance(toc['enabled'], bool)
    assert isinstance(toc['buffer_days'], (int, float))
    assert isinstance(toc['ranking'], list)
    assert isinstance(toc['rope'], list)
    assert isinstance(toc['upstream'], list)
    for b in toc['buffer_status']:
        assert b['level'] in ('red', 'yellow', 'green')
        # 红灯 = 交期缓冲不足 1 倍缓冲天数；绿灯 = 富余 ≥ 2 倍
        if b['level'] == 'red':
            assert b['buffer_days'] < toc['buffer_days']
        elif b['level'] == 'green':
            assert b['buffer_days'] >= 2 * toc['buffer_days']
    if toc['ranking']:
        assert toc['ranking'][0]['equipment'] == toc['drum']


def test_plan_buffer_targets_never_violate_drum_order(temp_orders):
    """【L4·Buffer】凡是能右移的都已被约束：声称未违规的上游单，完工必须不晚于目标。"""
    plan = wo._wo_plan()
    if not plan['toc']['enabled']:
        pytest.skip('TOC 开关未启用')
    pe_by_id = {r['oid']: r['pe'] for r in plan['assigned']}
    for u in plan['toc']['upstream']:
        if u['violated']:
            continue                       # 明说了违规（受物料/货架期推动），用户可见即可
        if u['actual_end'] and pe_by_id.get(u['id']):
            assert pe_by_id[u['id']] <= datetime.strptime(u['target_end'], '%Y-%m-%d %H:%M:%S')


def test_plan_decisions_match_assigned(temp_orders):
    """decisions 是组装好的写入参数，必须与 assigned 的最终落点逐值一致。"""
    plan = wo._wo_plan()
    by_id = {r['oid']: r for r in plan['assigned']}
    assert len(plan['decisions']) == len(plan['assigned'])
    for eq, ps, pe, oid in plan['decisions']:
        r = by_id[oid]
        assert eq == r['eq']
        assert ps == r['es'].strftime('%Y-%m-%d %H:%M:%S')
        assert pe == r['pe'].strftime('%Y-%m-%d %H:%M:%S')


def test_diff_vs_db_empty_input():
    assert wo._wo_diff_vs_db([]) == []


# ==========================================================================
# L3：沙盘试算接口（路由级，只读）
# ==========================================================================
def test_what_if_route_is_read_only(client, temp_orders):
    """【L3】试算接口：给结果，但不许改库（这是它存在的全部意义）。"""
    before = _snapshot(temp_orders)
    resp = client.post('/api/work-orders/what-if', json={})
    assert resp.status_code == 200
    data = resp.get_json()
    assert data['success'] is True
    assert data['dry_run'] is True
    assert _snapshot(temp_orders) == before, '试算接口写库了'


def test_what_if_returns_plan_diff_and_kpi(client, temp_orders):
    resp = client.post('/api/work-orders/what-if', json={})
    data = resp.get_json()
    assert data['success'] is True
    assert data['changed_count'] >= 1                 # 新单没有旧计划 → 必然有差异
    assert len(data['plan']) == len(data['diff'])
    for row in data['plan']:
        assert row['planned_start'] and row['planned_end']
        assert row['equipment']
    for d in data['diff']:
        assert set(('current', 'planned', 'changed', 'shift_hours')) <= set(d.keys())
    # 预估 KPI 用的是与落库后同一套算法
    for k in ('scheduled', 'overdue', 'on_time_rate', 'changeovers', 'changeover_hours'):
        assert k in data['kpi']
    # 试算与落库必须同源：再跑一次内核，排出来的时间应完全一致（固定种子）
    plan2 = wo._wo_plan()
    def key(r):
        return (r['id'], r['planned']['planned_start'], r['planned']['planned_end'])
    assert sorted([key(d) for d in data['diff']], key=str) == \
           sorted([(r['oid'], r['es'].strftime('%Y-%m-%d %H:%M:%S'),
                    r['pe'].strftime('%Y-%m-%d %H:%M:%S')) for r in plan2['assigned']],
                  key=str)
