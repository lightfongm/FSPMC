# -*- coding: utf-8 -*-
"""M6 / 设备产能 / 不可用设备 回归基线（不写业务数据：纯函数 + 只读校验 + 404 分支）。

覆盖：
  · 设备产能开关关闭（默认）时工期与改动前逐值一致；
  · 开关打开后按 ceil(数量 / (每小时产能 × 24))，缺产能回退日产量口径；
  · 不可用设备常量与库内实际状态一致；
  · manual_locked 迁移列存在；
  · 解锁接口对不存在/非创建工单的拒绝分支；
  · _wo_auto_end 在未完工为 0 时回退 plan_qty（修复「完工早于开工」脏数据）。
"""

import pytest

from backend.database import query_db
from backend.routes import work_orders as wo


# ===== 设备产能参与工期（开关默认关）=====
def test_lead_days_eq_off_equals_legacy(monkeypatch):
    """开关关闭 → 各设备工期恒等于日产量口径（保证不劣化）。"""
    monkeypatch.setattr(wo, '_eq_capacity_enabled', lambda: False)
    for eq in ('反应釜1号', '反应釜2号', '反应釜3号', None, ''):
        assert wo._wo_lead_days_eq(100, 'C001', eq) == wo._wo_lead_days(100, 'C001')


def test_lead_days_eq_on_uses_capacity(monkeypatch):
    """开关打开 → 工期 = ceil(数量 / (每小时产能 × 24))。"""
    monkeypatch.setattr(wo, '_eq_capacity_enabled', lambda: True)
    monkeypatch.setattr(wo, '_EQ_ROWS', {'EQ_A': {'name': 'EQ_A', 'capacity': 500.0}})
    # 100 / (500 × 24) = 0.0083 → ceil = 1
    assert wo._wo_lead_days_eq(100, 'C001', 'EQ_A') == 1
    monkeypatch.setattr(wo, '_EQ_ROWS', {'EQ_B': {'name': 'EQ_B', 'capacity': 10.0}})
    assert wo._wo_lead_days_eq(1000, 'C001', 'EQ_B') == 5      # 1000 / 240 = 4.17 → 5


def test_lead_days_eq_falls_back_when_capacity_missing(monkeypatch):
    """设备不存在 / 产能 NULL 或非正数 → 回退日产量口径（不因缺数据而排不出）。"""
    monkeypatch.setattr(wo, '_eq_capacity_enabled', lambda: True)
    monkeypatch.setattr(wo, '_EQ_ROWS', {
        'NULL_CAP': {'name': 'NULL_CAP', 'capacity': None},
        'ZERO_CAP': {'name': 'ZERO_CAP', 'capacity': 0.0},
    })
    for eq in ('NULL_CAP', 'ZERO_CAP', '不存在的设备', None, ''):
        assert wo._wo_lead_days_eq(100, 'C001', eq) == wo._wo_lead_days(100, 'C001')


def test_eq_capacity_per_hour_guards():
    assert wo._eq_capacity_per_hour(None) is None
    assert wo._eq_capacity_per_hour('') is None


# ===== 不可用设备不参与排产 =====
def test_unavailable_status_constant_matches_db():
    """库内 equipment.status 取值应落在已知枚举内，且「不可用」设备确实存在（回归哨兵）。"""
    rows = query_db("SELECT name, status FROM equipment")
    assert rows, 'equipment 表为空，无法校验'
    enums = {wo.EQ_STATUS_UNAVAILABLE} | set(wo.EQ_STATUS_MAINT) | {'可用'}
    unknown = sorted({(r['status'] or '').strip() for r in rows} - enums)
    assert not unknown, '出现未识别的设备状态：%s（需同步更新 EQ_STATUS_* 常量）' % unknown
    unavailable = [r['name'] for r in rows
                   if (r['status'] or '').strip() == wo.EQ_STATUS_UNAVAILABLE]
    assert unavailable, '当前库内无「不可用」设备，该分支无法被端到端覆盖'
    print('不可用设备：', unavailable)


# ===== M6：manual_locked 迁移列 =====
def test_manual_locked_column_exists():
    cols = query_db(
        "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
        "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME='work_orders' "
        "AND COLUMN_NAME='manual_locked'")
    assert cols, 'work_orders.manual_locked 列缺失（启动迁移未生效）'


def test_unlock_route_rejects_unknown_id():
    from backend import create_app
    client = create_app().test_client()
    resp = client.put('/api/work-orders/__not_exist__/unlock-schedule', json={})
    assert resp.status_code == 404
    assert resp.get_json().get('success') is False


# ===== 既有缺陷修复：未完工为 0 时回退 plan_qty =====
def test_auto_end_falls_back_to_plan_qty(monkeypatch):
    """只改计划开工、未传计划完工时，完工必须按工期重算（否则出现完工早于开工）。"""
    monkeypatch.setattr(wo, '_wo_lead_days_eq', lambda q, m, e: 1)
    data = {'material_code': 'C001', 'planned_start': '2026-11-01 08:00:00',
            'plan_qty': 100, 'workcenter_name': '反应釜3号'}
    old = {'material_code': 'C001', 'planned_start': '2026-10-17 00:00:00',
           'planned_end': '2026-10-18 00:00:00',
           'confirm_qty': 0.0, 'completed_qty': 0.0, 'plan_qty': 100}
    out = wo._wo_auto_end(dict(data), old)
    assert out['planned_end'] == '2026-11-02 08:00:00'


def test_auto_end_respects_explicit_end():
    """调用方显式传了计划完工（甘特图拖动）→ 不得覆盖。"""
    data = {'material_code': 'C001', 'planned_start': '2026-11-01 08:00:00',
            'planned_end': '2026-11-05 16:30:00'}
    out = wo._wo_auto_end(dict(data), None)
    assert out['planned_end'] == '2026-11-05 16:30:00'


if __name__ == '__main__':
    pytest.main([__file__, '-q'])
