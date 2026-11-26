# -*- coding: utf-8 -*-
"""一键排产 S1-S5 回归基线（不写业务数据，纯函数 + 只读校验 + 并发锁判定）。"""

from datetime import datetime, timedelta

import pytest

from backend.routes import work_orders as wo


def _today():
    return datetime(2026, 10, 7, 0, 0, 0)


# ===== S5：物料可用日优先用采购在途真实到货日 =====
def test_mat_ready_date_uses_transit_date():
    today0 = _today()
    pools = {'M1': [(datetime(2026, 10, 10), 100.0)]}
    assert wo._wo_mat_ready_date('M1', 50.0, pools, today0, 7) == datetime(2026, 10, 10)


def test_mat_ready_date_accumulates_partial_arrivals():
    """分批到货：累计到货量首次覆盖缺口的那一天才是可用日。"""
    today0 = _today()
    pools = {'M1': [(datetime(2026, 10, 10), 30.0), (datetime(2026, 10, 15), 30.0)]}
    assert wo._wo_mat_ready_date('M1', 50.0, pools, today0, 7) == datetime(2026, 10, 15)


def test_mat_ready_date_falls_back_to_lead_time():
    """在途覆盖不到缺口 → 回退『今天 + 采购提前期』（与改动前口径一致）。"""
    today0 = _today()
    pools = {'M1': [(datetime(2026, 10, 10), 10.0)]}
    assert wo._wo_mat_ready_date('M1', 500.0, pools, today0, 5) == today0 + timedelta(days=5)


def test_mat_ready_date_none_when_no_lead():
    today0 = _today()
    assert wo._wo_mat_ready_date('M1', 100.0, {}, today0, None) is None
    assert wo._wo_mat_ready_date('M1', 0.0, {}, today0, 5) is None


def test_transit_pools_excludes_missing_arrival_date():
    """无计划到货日期的在途单不计入（用户已拍板口径）。"""
    pools = wo._wo_transit_pools()
    for _mat, buckets in pools.items():
        for (d, q) in buckets:
            assert d is not None
            assert q > 0


def test_transit_pools_sorted_by_date():
    pools = wo._wo_transit_pools()
    for _mat, buckets in pools.items():
        assert buckets == sorted(buckets, key=lambda x: x[0])


# ===== S4：缺料快照新鲜度 =====
def test_shortage_snapshot_meta_shape():
    comp_need, meta = wo._wo_shortage_snapshot()
    assert isinstance(comp_need, dict)
    assert {'available', 'generated_at', 'age_days', 'rows'} <= set(meta.keys())
    assert isinstance(meta['available'], bool)
    assert isinstance(meta['rows'], int)
    if meta['available']:
        assert meta['generated_at'] is not None
        for v in comp_need.values():
            assert v >= 0


# ===== S2：时间比对工具 =====
def test_norm_dt_str():
    assert wo._wo_norm_dt_str(datetime(2026, 10, 7, 8, 0, 0)) == '2026-10-07 08:00:00'
    assert wo._wo_norm_dt_str('2026-10-07T08:00:00') == '2026-10-07 08:00:00'
    assert wo._wo_norm_dt_str(None) == ''


# ===== S3：KPI 口径（纯函数，不落库） =====
def test_kpi_on_time_rate_and_tardiness():
    today0 = _today()
    due = datetime(2026, 10, 20)
    assigned = [
        {'oid': 'w1', 'mat': 'C001', 'lead': 2, 'due': due, 'cust': 3, 'co_new': 0.0, 'eq': 'EQ1',
         'es': today0, 'pe': today0 + timedelta(days=2)},
        {'oid': 'w2', 'mat': 'C002', 'lead': 3, 'due': due, 'cust': 1, 'co_new': 0.0, 'eq': 'EQ1',
         'es': today0, 'pe': today0 + timedelta(days=30)},
    ]
    fmap = {
        'w1': {'id': 'w1', 'planned_start': '2026-10-07 00:00:00', 'planned_end': '2026-10-09 00:00:00'},
        'w2': {'id': 'w2', 'planned_start': '2026-10-09 00:00:00', 'planned_end': '2026-11-06 00:00:00'},
    }
    kpi = wo._wo_sched_kpi(assigned, fmap, {'EQ1': 32.0, 'EQ2': 4.0})
    assert kpi['scheduled'] == 2
    assert kpi['overdue'] == 1
    assert kpi['on_time_rate'] == 50.0
    assert kpi['tardiness_days'] == 17.0          # 11-06 完工 - 10-20 交期
    assert kpi['weighted_tardiness'] == 17.0 * 1  # cust=1 → 权重 1
    assert kpi['changeovers'] == 1                # 同设备相邻物料不同 → 1 次换型
    assert kpi['peak_equipment'] == 'EQ1'
    assert kpi['peak_load_days'] == 32.0
    assert kpi['load_spread_days'] == 28.0
    assert kpi['makespan_days'] == 30.0


def test_kpi_weighted_tardiness_uses_cust_level():
    today0 = _today()
    due = datetime(2026, 10, 8)
    assigned = [{'oid': 'w1', 'mat': 'C001', 'lead': 5, 'due': due, 'cust': 3, 'co_new': 0.0,
                 'eq': 'EQ1', 'es': today0, 'pe': today0 + timedelta(days=5)}]
    fmap = {'w1': {'id': 'w1', 'planned_start': '2026-10-07 00:00:00',
                   'planned_end': '2026-10-12 00:00:00'}}
    kpi = wo._wo_sched_kpi(assigned, fmap, {'EQ1': 5.0})
    assert kpi['overdue'] == 1
    assert kpi['tardiness_days'] == 4.0
    assert kpi['weighted_tardiness'] == 12.0      # 4 天 × 客户等级 3


# ===== S1：单飞锁 + 时间预算 =====
def test_auto_schedule_rejects_concurrent_request():
    """持锁时再请求必须返回 409，不能串行排队。"""
    from backend import create_app
    app = create_app()
    client = app.test_client()
    assert wo._WO_SCHED_LOCK.acquire(blocking=False)
    try:
        resp = client.post('/api/work-orders/auto-schedule', json={})
        assert resp.status_code == 409
        body = resp.get_json()
        assert body.get('success') is False
        assert '排产进行中' in (body.get('error') or '')
    finally:
        wo._WO_SCHED_LOCK.release()


def test_sched_budget_default():
    assert wo._WO_SCHED_BUDGET_SEC > 0


if __name__ == '__main__':
    pytest.main([__file__, '-q'])
