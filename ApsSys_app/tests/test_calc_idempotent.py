# -*- coding: utf-8 -*-
"""一键计算幂等回归：连续多次一键计算，缺料明细必须逐行一致（含行数）。

守护 2026-10-08 修复的「缺料明细 18↔17」非幂等缺陷。

根因复盘：
  · 缺料重建读取 peacock_schedule 无 ORDER BY → 同物料多排程行遍历顺序随机
    → 动态 SEMI 行「首父胜出」的 qty 随序摆动，且合并阶段 anchor 选取随序摆动；
  · 消费循环排序键仅为 (需求日, 序列号)，而 req_lines 无 id 字段
    → 同键两行靠「插入序」兜底 → 跨行「建议采购池」回补量在 0 边界翻转 → 缺料行数 ±1。

修复手段（schedule.py）：
  · schedule_rows / wo_rows / 合并阶段读取均加 ORDER BY（物料→交货日→开工日→序列号）；
  · 消费循环改全序键（需求日→来源→序列号→父物料→子件→需求量→行主键）。

本用例对任意数据集都保证：两次一键计算产出的缺料明细逐行相等。
"""

import pytest

from backend import create_app
from backend.routes import schedule as sch


@pytest.fixture
def client():
    return create_app().test_client()


# 比较键：仅取业务字段。schedule_id/work_order_id 是每次重建重新生成的 uuid（随机），
# 不能作为幂等判据；aufnr/source/component_code/数量/状态/需求日才是真实缺料内容。
_FP_COLS = ('aufnr', 'source', 'component_code', 'required_qty', 'available_qty',
            'current_stock', 'shortage_type', 'component_demand_date')


def _shortage_fingerprint():
    rows = sch.query_db(
        "SELECT schedule_id, work_order_id, aufnr, source, component_code, "
        "required_qty, available_qty, current_stock, shortage_type, component_demand_date "
        "FROM peacock_material_shortage"
    ) or []
    fp = []
    for r in rows:
        row = []
        for k in _FP_COLS:
            v = r.get(k)
            row.append(round(v, 6) if isinstance(v, float) else str(v))
        fp.append(tuple(row))
    # 全序化：ignore 随机主键，按业务列排序后比较
    fp.sort()
    return fp


def _run_calc(client):
    resp = client.post('/api/peacock/calculate', json={
        'enabled_stock_types': ['unrestricted'],
        'schedule_merge_days': 0,
        'merge_days': 0,
    })
    assert resp.status_code == 200, resp.get_data(as_text=True)[:800]
    return resp.get_json()


def test_peacock_calculate_idempotent(client):
    """连续 3 次一键计算，任意两次缺料明细须逐行一致（行数 + 业务字段）。"""
    fps = []
    for _ in range(3):
        _run_calc(client)
        fps.append(_shortage_fingerprint())

    assert fps[0], "缺料明细为空，计算可能未产出缺料数据"
    assert fps[0] == fps[1], \
        "第1次与第2次缺料明细不一致（非幂等）：行数 %d vs %d" % (len(fps[0]), len(fps[1]))
    assert fps[1] == fps[2], \
        "第2次与第3次缺料明细不一致（非幂等）：行数 %d vs %d" % (len(fps[1]), len(fps[2]))
