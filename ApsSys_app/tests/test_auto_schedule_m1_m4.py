# -*- coding: utf-8 -*-
"""M1 序列相关换型 / M2 倒排开关 / M3 自制件排程日 / M4 多目标权重 回归基线。

原则：不写业务数据（换型矩阵 CRUD 用例除外，用后自删）；新能力一律"缺省等价现状"。
"""

import pytest

from backend.database import execute_db, query_db
from backend.routes import work_orders as wo


# ===== M1：序列相关换型 =====
def test_changeover_between_same_material_is_zero(monkeypatch):
    """同物料连产不换型 —— M1 最直接的产能收益。"""
    monkeypatch.setattr(wo, '_co_matrix_enabled', lambda: True)
    monkeypatch.setattr(wo, '_wo_changeover_hours', lambda m: 3.0)
    assert wo._wo_changeover_between('C001', 'C001') == 0.0


def test_changeover_between_no_prev_is_zero(monkeypatch):
    """设备上第一张单之前无需换型。"""
    monkeypatch.setattr(wo, '_co_matrix_enabled', lambda: True)
    monkeypatch.setattr(wo, '_wo_changeover_hours', lambda m: 3.0)
    assert wo._wo_changeover_between(None, 'C001') == 0.0
    assert wo._wo_changeover_between('', 'C001') == 0.0


def test_changeover_between_uses_matrix(monkeypatch):
    monkeypatch.setattr(wo, '_co_matrix_enabled', lambda: True)
    monkeypatch.setattr(wo, '_wo_changeover_hours', lambda m: 3.0)
    monkeypatch.setattr(wo, '_CO_MATRIX', {('C001', 'C002'): 5.0})
    assert wo._wo_changeover_between('C001', 'C002') == 5.0
    # 反向未录 → 对称命中（矩阵未区分方向时只录一条即可）
    assert wo._wo_changeover_between('C002', 'C001') == 5.0


def test_changeover_between_falls_back_to_own_time(monkeypatch):
    """无矩阵记录 → 回退物料自身换型时间（与启用矩阵前完全一致，不劣化）。"""
    monkeypatch.setattr(wo, '_co_matrix_enabled', lambda: True)
    monkeypatch.setattr(wo, '_CO_MATRIX', {})
    monkeypatch.setattr(wo, '_wo_changeover_hours', lambda m: 2.5)
    assert wo._wo_changeover_between('C001', 'C002') == 2.5


def test_changeover_off_equals_legacy(monkeypatch):
    """开关关闭 → 恒等于改动前口径（连"同物料"也照加自身换型时间）。"""
    monkeypatch.setattr(wo, '_co_matrix_enabled', lambda: False)
    monkeypatch.setattr(wo, '_wo_changeover_hours', lambda m: 4.0)
    monkeypatch.setattr(wo, '_CO_MATRIX', {('C001', 'C002'): 99.0})
    assert wo._wo_changeover_between('C001', 'C001') == 4.0
    assert wo._wo_changeover_between('C001', 'C002') == 4.0


def test_changeover_matrix_crud_roundtrip():
    """换型矩阵表可用（建表迁移已生效）且 CRUD 走通；用例结束清理数据。"""
    from backend import create_app
    client = create_app().test_client()
    r = client.get('/api/changeover')
    assert r.status_code == 200 and r.get_json().get('success') is True

    r = client.post('/api/changeover', json={'from_mat': 'ZZTEST_A', 'to_mat': 'ZZTEST_B',
                                             'changeover_hours': 6})
    assert r.get_json().get('success') is True, r.get_json()
    cid = r.get_json().get('id')

    r = client.get('/api/changeover?search=ZZTEST_A')
    assert r.get_json().get('total') >= 1

    r = client.post('/api/changeover', json={'from_mat': 'ZZTEST_A', 'to_mat': 'ZZTEST_B',
                                             'changeover_hours': 9})
    assert r.get_json().get('success') is True and r.get_json().get('id') == cid  # upsert 不重复

    r = client.post('/api/changeover', json={'from_mat': 'ZZTEST_A', 'to_mat': 'ZZTEST_A'})
    assert r.get_json().get('success') is False      # 同物料无需录入

    r = client.delete('/api/changeover/' + cid)
    assert r.get_json().get('success') is True
    execute_db("DELETE FROM product_changeover WHERE from_mat LIKE 'ZZTEST%'")
    assert query_db("SELECT COUNT(*) AS c FROM product_changeover WHERE from_mat LIKE 'ZZTEST%'",
                    one=True)['c'] == 0


# ===== M3：自制件排程产出日 =====
def test_cover_date_accumulates():
    from datetime import datetime
    b = [(datetime(2026, 10, 10), 30.0), (datetime(2026, 10, 15), 30.0)]
    assert wo._wo_cover_date(b, 50.0) == datetime(2026, 10, 15)
    assert wo._wo_cover_date(b, 30.0) == datetime(2026, 10, 10)
    assert wo._wo_cover_date(b, 500.0) is None      # 排程产出覆盖不到
    assert wo._wo_cover_date([], 10.0) is None
    assert wo._wo_cover_date(b, 0) is None


def test_prod_pools_sorted_and_positive():
    pools = wo._wo_prod_pools()
    assert isinstance(pools, dict)
    for _mat, buckets in pools.items():
        assert buckets == sorted(buckets, key=lambda x: x[0])
        for (d, q) in buckets:
            assert d is not None and q > 0


# ===== M4：多目标权重 =====
def test_sched_weights_default_order():
    w = wo._wo_sched_weights()
    assert set(w) == {'tardy', 'changeover', 'balance', 'stability'}
    # 用户拍板序：拖期 > 换型 > 负荷均衡 > 计划稳定
    assert w['tardy'] >= w['changeover'] >= w['balance'] >= w['stability']
    assert all(v >= 0 for v in w.values())


# ===== M2：倒排开关默认关 =====
def test_jit_disabled_by_default():
    """默认正排 ASAP，倒排为显式开启项（未配置时不得生效）。"""
    assert wo._wo_jit_enabled() in (True, False)
    row = query_db("SELECT config_value FROM system_config WHERE config_key='module_schedule_jit'",
                   one=True)
    if row is None:
        assert wo._wo_jit_enabled() is False


if __name__ == '__main__':
    pytest.main([__file__, '-q'])
