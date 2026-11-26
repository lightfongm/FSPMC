# -*- coding: utf-8 -*-
"""【L1 序列编码 + 统一解码器 / L2 模拟退火】回归基线。

不依赖真实库数据：换型矩阵与工期一律用 monkeypatch 注入，测试只验证内核语义。

核心不变式（L1 存在的意义）：
  · 时间**只由解码器产出一次** → 评估成本用的时间 == 落库的时间；
  · 解码结果构造性满足「序列内不重叠 + 序列相关换型间隔 + 避开静态占用」；
  · 因此 `_wo_seq_conflicts()` 对解码结果应返回空集（落库后无需二次顺排）。
"""
import random
from datetime import datetime, timedelta

import pytest

from backend.routes import work_orders as wo
from backend.services.scheduling_core import (
    SchedContext, anneal, _apply, _re_decode, _propose,
)

TODAY = datetime(2026, 10, 7, 0, 0, 0)

# 换型矩阵：M1→M2 = 4h，M1→M3 = 8h；同物料 0（由 _wo_changeover_between 保证）
CO_MATRIX = {('M1', 'M2'): 4.0, ('M1', 'M3'): 8.0, ('M2', 'M3'): 6.0}
DEFAULT_CO = 2.0


@pytest.fixture(autouse=True)
def _no_db(monkeypatch):
    """切断排产内核对数据库的依赖（换型开关/矩阵/物料换型时间全部注入）。"""
    monkeypatch.setattr(wo, '_co_matrix_enabled', lambda: True)
    monkeypatch.setattr(wo, '_CO_MATRIX', dict(CO_MATRIX))
    monkeypatch.setattr(wo, '_wo_changeover_hours', lambda m: DEFAULT_CO)


def _job(mat, cands, due=None, cust=1, lead=2.0, mat_avail=None, shelf_floor=None):
    return {'mat': mat, 'cands': list(cands), 'due': due, 'cust': cust,
            'shelf_floor': shelf_floor, 'mat_avail': mat_avail,
            'lead': lead, 'lead_by_eq': {}}


def _ctx(jobs, static_occ=None, maint_occ=None, static_load=None, weights=None):
    return SchedContext(
        today0=TODAY, jobs=jobs, static_occ=static_occ or {}, maint_occ=maint_occ or {},
        static_load=static_load or {},
        earliest_start=wo._wo_earliest_start,
        changeover_between=wo._wo_changeover_between,
        lead_on=lambda j, eq: (j.get('lead_by_eq') or {}).get(eq, j['lead']),
        weights=weights or {'tardy': 0.4, 'changeover': 0.3, 'balance': 0.2, 'stability': 0.1},
    )


# --------------------------------------------------------------------------
# L1：解码器语义
# --------------------------------------------------------------------------

def test_decode_applies_sequence_dependent_changeover():
    """序列内相邻两单：异物料留出矩阵换型，同物料连产为 0。"""
    jobs = {'A': _job('M1', ['E1']), 'B': _job('M2', ['E1']), 'C': _job('M2', ['E1'])}
    ctx = _ctx(jobs)
    pl = ctx.decode({'E1': ['A', 'B', 'C']})
    # A: 从 today0 起，工期 2 天
    assert pl['A']['es'] == TODAY
    assert pl['A']['pe'] == TODAY + timedelta(days=2)
    # A→B 异物料：4 小时换型
    assert pl['B']['es'] == pl['A']['pe'] + timedelta(hours=4)
    # B→C 同物料：0 换型，直接接上
    assert pl['C']['es'] == pl['B']['pe']


def test_decode_span_is_exactly_lead_days():
    """【L1 关键】pe − es 恒等于工期，换型**不**塞进 span（与级联顺排同口径）。

    改动前贪心阶段把换型同时加在「队尾 base」和「span」上，等于重复预留一次。
    """
    jobs = {'A': _job('M1', ['E1'], lead=3.0), 'B': _job('M2', ['E1'], lead=1.5)}
    pl = _ctx(jobs).decode({'E1': ['A', 'B']})
    assert pl['A']['pe'] - pl['A']['es'] == timedelta(days=3.0)
    assert pl['B']['pe'] - pl['B']['es'] == timedelta(days=1.5)


def test_decode_respects_static_occupancy():
    """解码不得与静态占用（锁定单/未参与本轮的创建单）重叠，且尾部含序列相关换型。"""
    occ = {'E1': [(TODAY, TODAY + timedelta(days=5), 'M1')]}
    jobs = {'A': _job('M2', ['E1'])}
    pl = _ctx(jobs, static_occ=occ).decode({'E1': ['A']})
    # 静态单 M1 → 本单 M2：换型 4h，故最早开工 = 静态完工 + 4h
    assert pl['A']['es'] == TODAY + timedelta(days=5, hours=4)


def test_decode_respects_release_floor():
    """开工下限 = max(当日, 货架期 floor, 物料可用日)。"""
    jobs = {'A': _job('M1', ['E1'], mat_avail=TODAY + timedelta(days=3),
                      shelf_floor=TODAY + timedelta(days=1))}
    pl = _ctx(jobs).decode({'E1': ['A']})
    assert pl['A']['es'] == TODAY + timedelta(days=3)


def test_decoded_result_passes_conflict_selfcheck():
    """【L1 核心】解码结果自检为空 → 落库后无需二次级联顺排（评估=落库）。"""
    jobs = {'A': _job('M1', ['E1']), 'B': _job('M2', ['E1']),
            'C': _job('M1', ['E1']), 'D': _job('M2', ['E2'])}
    ctx = _ctx(jobs)
    seqs = {'E1': ['A', 'B', 'C'], 'E2': ['D']}
    pl = ctx.decode(seqs)
    assigned = [{'oid': k, 'eq': v['eq'], 'es': v['es'], 'pe': v['pe'], 'mat': jobs[k]['mat']}
                for k, v in pl.items()]
    assert wo._wo_seq_conflicts(assigned, {}, {}) == set()


def test_conflict_selfcheck_detects_real_overlap():
    """人为把后一单提前到前一单完工之前 → 必须检出冲突（否则兜底永远不会触发）。"""
    jobs = {'A': _job('M1', ['E1']), 'B': _job('M2', ['E1'])}
    ctx = _ctx(jobs)
    pl = ctx.decode({'E1': ['A', 'B']})
    assigned = [{'oid': 'A', 'eq': 'E1', 'es': pl['A']['es'], 'pe': pl['A']['pe'], 'mat': 'M1'},
                {'oid': 'B', 'eq': 'E1', 'es': pl['A']['es'] + timedelta(hours=1),
                 'pe': pl['A']['es'] + timedelta(days=2), 'mat': 'M2'}]
    assert 'E1' in wo._wo_seq_conflicts(assigned, {}, {})


def test_conflict_selfcheck_detects_maintenance_band():
    """压到保养带上的排产必须被检出。"""
    jobs = {'A': _job('M1', ['E1'])}
    ctx = _ctx(jobs, maint_occ={'E1': [(TODAY, TODAY + timedelta(days=10))]})
    # 解码器会避开保养带 → 本身无冲突
    pl = ctx.decode({'E1': ['A']})
    assigned = [{'oid': 'A', 'eq': 'E1', 'es': pl['A']['es'], 'pe': pl['A']['pe'], 'mat': 'M1'}]
    assert wo._wo_seq_conflicts(assigned, {}, ctx.maint_occ) == set()
    # 人为放到带内 → 检出
    bad = [{'oid': 'A', 'eq': 'E1', 'es': TODAY, 'pe': TODAY + timedelta(days=2), 'mat': 'M1'}]
    assert 'E1' in wo._wo_seq_conflicts(bad, {}, ctx.maint_occ)


# --------------------------------------------------------------------------
# L1：增量解码（退火性能依赖它）
# --------------------------------------------------------------------------

def test_incremental_decode_equals_full_decode():
    """只重算受影响设备 == 全量重算（否则退火的评估会与真实解码不一致）。"""
    jobs = {'A': _job('M1', ['E1', 'E2']), 'B': _job('M2', ['E1']),
            'C': _job('M3', ['E2']), 'D': _job('M1', ['E2'])}
    ctx = _ctx(jobs)
    seqs = {'E1': ['A', 'B'], 'E2': ['C', 'D']}
    full = ctx.decode(seqs)
    pl = ctx.decode(seqs)
    seqs2 = {'E1': list(seqs['E1']), 'E2': list(seqs['E2'])}
    changed, _ = _apply(seqs2, ('shift', 'E1', 0, 1))   # E1: [A,B] → [B,A]
    partial = _re_decode(ctx, pl, seqs2, changed)
    assert partial == ctx.decode(seqs2)
    # A 被换到 B 之后，须等 B 完工 + changeover(M2→M1)=4h
    assert partial['A']['es'] == TODAY + timedelta(days=2, hours=4)
    assert partial['A'] != full['A']     # 换序后 A 确实变了（证明这条用例有效）


# --------------------------------------------------------------------------
# L2：邻域算子
# --------------------------------------------------------------------------

def test_relocate_move_and_rollback():
    seqs = {'E1': ['A', 'B'], 'E2': ['C']}
    changed, backup = _apply(seqs, ('relocate', 'A', 'E2', 1))
    assert changed == {'E1', 'E2'}
    assert seqs == {'E1': ['B'], 'E2': ['C', 'A']}
    for eq, seq in backup.items():
        seqs[eq] = seq
    assert seqs == {'E1': ['A', 'B'], 'E2': ['C']}


def test_swap_and_shift_moves():
    seqs = {'E1': ['A', 'B'], 'E2': ['C', 'D']}
    _apply(seqs, ('swap', 'A', 'C'))
    assert seqs == {'E1': ['C', 'B'], 'E2': ['A', 'D']}
    _apply(seqs, ('shift', 'E1', 0, 1))
    assert seqs['E1'] == ['B', 'C']


def test_propose_never_leaves_candidate_equipment():
    """硬约束①：任何邻域动作都不得把单放到它的候选设备之外。"""
    jobs = {'A': _job('M1', ['E1']), 'B': _job('M2', ['E1', 'E2'])}
    ctx = _ctx(jobs)
    seqs = {'E1': ['A', 'B'], 'E2': []}
    rng = random.Random(7)
    seen = set()
    for _ in range(300):
        mv = _propose(rng, seqs, ctx)
        if mv is None:
            continue
        seen.add(mv[0])
        if mv[0] == 'relocate':
            assert mv[2] in jobs[mv[1]]['cands']
        elif mv[0] == 'swap':
            e1 = next(e for e, s in seqs.items() if mv[1] in s)
            e2 = next(e for e, s in seqs.items() if mv[2] in s)
            assert e2 in jobs[mv[1]]['cands'] and e1 in jobs[mv[2]]['cands']
        for eq, s in seqs.items():       # 应用后仍然合法
            _apply(seqs, mv)
            for oid in s:
                assert eq in jobs[oid]['cands']
    assert seen, '应至少生成过一种邻域动作'


# --------------------------------------------------------------------------
# L2：模拟退火
# --------------------------------------------------------------------------

def test_anneal_is_deterministic_for_same_seed():
    """固定 seed → 同输入同输出（否则无法回归、也无法向用户解释排产结果）。"""
    jobs = {'A': _job('M1', ['E1', 'E2'], due=TODAY + timedelta(days=30)),
            'B': _job('M2', ['E1', 'E2'], due=TODAY + timedelta(days=30)),
            'C': _job('M1', ['E1', 'E2'], due=TODAY + timedelta(days=30)),
            'D': _job('M2', ['E2'], due=TODAY + timedelta(days=30))}
    seqs0 = {'E1': ['A', 'B'], 'E2': ['C', 'D']}
    r1 = anneal(_ctx(jobs), dict(seqs0), budget_sec=5.0, seed=42, max_iters=500)
    r2 = anneal(_ctx(jobs), dict(seqs0), budget_sec=5.0, seed=42, max_iters=500)
    assert r1[0] == r2[0]
    assert r1[1] == r2[1]


def test_anneal_never_returns_worse_than_start():
    """返回的是搜索过程中见过的最优解，不得比初始解更差。"""
    jobs = {'A': _job('M1', ['E1', 'E2'], due=TODAY + timedelta(days=30)),
            'B': _job('M2', ['E1', 'E2'], due=TODAY + timedelta(days=30)),
            'C': _job('M1', ['E1', 'E2'], due=TODAY + timedelta(days=30))}
    ctx = _ctx(jobs)
    seqs0 = {'E1': ['A', 'B', 'C'], 'E2': []}
    start = ctx.cost(ctx.decode(seqs0))
    best, cost, info = anneal(ctx, dict(seqs0), budget_sec=5.0, seed=1, max_iters=800)
    assert cost <= start + 1e-9
    assert info['cost_from'] == pytest.approx(round(start, 6), abs=1e-6)
    assert info['iters'] > 0


def test_anneal_does_not_turn_on_time_order_into_overdue():
    """硬约束②：原本不超期的单，不得被迁成超期（交期优先于均衡/换型）。"""
    tight = TODAY + timedelta(days=2)          # 工期正好 2 天，只有排在当天才不超期
    jobs = {'A': _job('M1', ['E1', 'E2'], due=tight),
            'B': _job('M2', ['E1', 'E2'], due=TODAY + timedelta(days=60)),
            'C': _job('M2', ['E1', 'E2'], due=TODAY + timedelta(days=60))}
    ctx = _ctx(jobs)
    seqs0 = {'E1': ['A'], 'E2': ['B', 'C']}
    assert all(p['pe'] <= jobs[o]['due'] for o, p in ctx.decode(seqs0).items())
    best, _cost, _info = anneal(ctx, dict(seqs0), budget_sec=5.0, seed=3, max_iters=600)
    pl = ctx.decode(best)
    assert pl['A']['pe'] <= jobs['A']['due'], 'A 被迁成超期了'


def test_batching_reduces_changeover_cost():
    """换型项按**解码后的真实时间顺序**串接：同物料聚批 → 换型成本更低。

    这是 L2 的 shift 算子存在的意义（改动前只有"换设备"算子，无法改变设备内顺序）。
    """
    jobs = {'A': _job('M1', ['E1']), 'B': _job('M2', ['E1']),
            'C': _job('M1', ['E1']), 'D': _job('M2', ['E1'])}
    ctx = _ctx(jobs, weights={'tardy': 0.0, 'changeover': 1.0, 'balance': 0.0, 'stability': 0.0})
    interleaved = ctx.cost(ctx.decode({'E1': ['A', 'B', 'C', 'D']}))   # M1→M2→M1→M2
    batched = ctx.cost(ctx.decode({'E1': ['A', 'C', 'B', 'D']}))       # M1,M1,M2,M2
    assert batched < interleaved
    assert ctx.cost_parts(ctx.decode({'E1': ['A', 'C', 'B', 'D']}))['changeover'] == pytest.approx(
        CO_MATRIX[('M1', 'M2')] / 24.0, abs=1e-9)


def test_anneal_respects_time_budget():
    """时间预算为 0 时不做任何迭代（S1 降级通道：超时即接受初始解）。"""
    jobs = {'A': _job('M1', ['E1', 'E2']), 'B': _job('M2', ['E1', 'E2'])}
    seqs0 = {'E1': ['A'], 'E2': ['B']}
    best, cost, info = anneal(_ctx(jobs), dict(seqs0), budget_sec=0, max_iters=500)
    assert info['iters'] == 0
    assert best == seqs0


def test_anneal_info_is_json_serializable():
    """响应体要能直接 jsonify（前端会展示搜索过程）。"""
    import json
    jobs = {'A': _job('M1', ['E1', 'E2'], due=TODAY + timedelta(days=30)),
            'B': _job('M2', ['E1', 'E2'], due=TODAY + timedelta(days=30))}
    _best, _c, info = anneal(_ctx(jobs), {'E1': ['A'], 'E2': ['B']},
                             budget_sec=1.0, seed=9, max_iters=200)
    json.dumps(info, ensure_ascii=False)
