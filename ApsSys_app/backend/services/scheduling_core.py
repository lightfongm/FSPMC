# -*- coding: utf-8 -*-
"""【L1 + L2】排产内核：序列编码 + 统一解码器 + 模拟退火局部搜索。

──────────────────────────────────────────────────────────────────────
为什么要重写（改动前的范式缺陷）
──────────────────────────────────────────────────────────────────────
改动前「一键排产」的优化对象**是时间戳本身**：
  · 贪心阶段：往 es/pe 上追加；
  · swap 优化：`_try_place()` 从 today0 起在设备空隙里找位置，直接改写 es/pe；
  · 落库后：`_wo_reschedule_by_changeover()` 又按 planned_start 排序做一次紧凑顺排。

三步各算一次时间、口径互不相同 → **优化阶段评的是一个不会落库的时间方案**。
swap 把单 A 插进设备 L 的早期空隙后，落库顺排会按新 planned_start 重排序，
把 A 拉成队首并推后其后的所有单，成本函数的四项（拖期/换型/负荷/偏离）
全部按「插空隙版」评估，落库却是「紧凑版」。这是本模块存在的根本原因。

──────────────────────────────────────────────────────────────────────
L1：序列编码 + 统一解码器
──────────────────────────────────────────────────────────────────────
  · **决策变量 = 每台设备的作业序列** `seqs: {设备名: [工单id, ...]}`；
  · **时间只有一个来源** `decode()`：给定序列，前向推进算出 es/pe；
  · 任何评估（成本、KPI、待料判定）一律基于解码结果 → 评估 = 落库，口径统一；
  · 解码器**构造性**保证不变式：
      es[i] ≥ pe[i-1] + changeover(前一单物料, 本单物料)       （序列内）
      [es, pe] 不与静态占用（锁定单/未参与本轮创建单/保养维修带）重叠
    → 解码结果可直接落库，**正常情况下无需再跑二次级联顺排**。

  🔴 与 `_wo_reschedule_by_changeover`（级联顺排）口径对齐：
    换型**只叠加一次**，且是序列相关的 changeover(prev, cur)。
    改动前贪心阶段实际叠加了两次（队尾 base 加 prev 自身换型 + span 加 cur 自身换型），
    属过度预留；级联顺排用的是一次。本内核与级联顺排同口径。
    （当前库所有 changeover_time 为 NULL → 换型恒 0，两种口径**逐值一致**，无回归。）

──────────────────────────────────────────────────────────────────────
L2：序列空间上的模拟退火
──────────────────────────────────────────────────────────────────────
改动前只有一个邻域（把单挪到另一台候选设备）+ 最陡下降（无改进即停），
既没有「改变设备内先后顺序」的算子（而那正是同物料聚批、减少换型的唯一手段），
也没有逃离局部最优的机制。本模块提供三种邻域 + Metropolis 接受：
  · relocate   跨设备迁移并插到指定位置（含设备内换位置）
  · swap       两台设备（或同设备）上的两张单互换位置
  · shift      同设备内移动位置（轻量版，驱动换型聚批）
硬约束：只能落在工单的候选设备内；原本不超期的单不得被迁成超期。
"""
import math
import random
import time
from datetime import timedelta

# 成本比较容差：小于该值视为无改进，避免浮点抖动导致无限迭代
COST_EPS = 1e-9

# 【P2-14】墙钟看门狗倍率：迭代预算(max_iters)是**确定的**终止主体，
# 墙钟仅用于兜底防死循环，故放宽到预算的 4 倍——正常负载下永远不会触发，
# 一旦触发即说明存在异常（会记入 info.timed_out 供观测）。
_WATCHDOG_MULT = 4.0


class SchedContext:
    """排产内核的只读上下文（依赖注入，内核不反向依赖路由层）。

    jobs        oid -> {mat, cands, due, cust, shelf_floor, mat_avail, lead, lead_by_eq}
    static_occ  设备 -> [(开工, 完工, 物料)] 锁定单 + 未参与本轮的创建单
    maint_occ   设备 -> [(开工, 完工)]       保养/维修带
    static_load 设备 -> 静态负荷(天)
    earliest_start      (base, lead_days, co_hours, occ_list) -> datetime
    changeover_between  (prev_mat, cur_mat) -> hours
    lead_on             (job, eq) -> 工期(天)
    """

    def __init__(self, today0, jobs, static_occ, maint_occ, static_load,
                 earliest_start, changeover_between, lead_on, weights,
                 base_es=None):
        self.today0 = today0
        self.jobs = jobs
        self.static_occ = static_occ or {}
        self.maint_occ = maint_occ or {}
        self.static_load = static_load or {}
        self.earliest_start = earliest_start
        self.changeover_between = changeover_between
        self.lead_on = lead_on
        self.weights = weights or {}
        self.base_es = base_es or {}

    # ---------------- 解码 ----------------

    def release_of(self, job):
        """该单的『最早可开工』下限 = max(当日零点, 货架期floor, 物料可用日)。"""
        r = self.today0
        for k in ('shelf_floor', 'mat_avail'):
            v = job.get(k)
            if v is not None and v > r:
                r = v
        return r

    def occ_of(self, eq, mat, placed):
        """设备 eq 上、针对『待排物料 mat』的占用墙。

        【L1 关键】尾部换型按 changeover(占用单物料, mat) **序列相关**计算：
          · 同物料 → 0，故把同物料单排在一起能直接省下一次换型（聚批收益）；
          · 异物料 → 换型矩阵值，无矩阵回退本单自身换型时间。
        区间语义 [开工, 完工 + 换型]，与 `_wo_earliest_start` 契约一致。
        """
        co = self.changeover_between
        occ = []
        for (ls, le, lm) in self.static_occ.get(eq, []):
            occ.append((ls, le + timedelta(hours=co(lm, mat))))
        for (ls, le, lm) in placed:
            occ.append((ls, le + timedelta(hours=co(lm, mat))))
        occ.extend(self.maint_occ.get(eq, []))
        return occ

    def decode_eq(self, eq, seq):
        """解码单台设备：按序列顺序前向推进。

        span 只含工期、**不含换型**（换型已由 occ 尾部表达）——与级联顺排
        `_wo_reschedule_by_changeover` 的『一次序列相关换型』同口径，
        避免改动前贪心阶段『队尾 prev 自身换型 + span cur 自身换型』的重复预留。
        """
        placed = []     # [(es, pe, mat)] 已解码的前序作业
        out = {}
        for oid in seq:
            job = self.jobs.get(oid)
            if job is None:
                continue
            mat = job['mat']
            lead = self.lead_on(job, eq) or 0.0
            es = self.earliest_start(self.release_of(job), lead, 0.0,
                                     self.occ_of(eq, mat, placed))
            pe = es + timedelta(days=lead)
            placed.append((es, pe, mat))
            out[oid] = {'eq': eq, 'es': es, 'pe': pe, 'lead': lead}
        return out

    def decode(self, seqs, only_eqs=None):
        """解码全部（或指定）设备 → {oid: {eq, es, pe, lead}}。"""
        res = {}
        for eq, seq in seqs.items():
            if only_eqs is not None and eq not in only_eqs:
                continue
            res.update(self.decode_eq(eq, seq))
        return res

    # ---------------- 成本 ----------------

    def cost_parts(self, pl):
        """【M4】多目标加权成本分项（越小越好）。

        权重序（用户拍板 2026-10-07）：拖期最小化 > 换型最少 > 负荷最均衡 > 计划最稳定。
          ① 客户加权拖期(天) = Σ max(0, 完工−交期) × 客户等级(≤0 按 1)
          ② 换型总时长(天)   = Σ changeover(前一单物料, 本单物料) / 24（同物料=0）
          ③ 负荷极差(天)     = max(设备总负荷) − min(...)
          ④ 计划偏离(天)     = Σ |当前开工 − 贪心首轮开工|
        换型链按**解码后的实际时间顺序**串接（不是序列下标顺序）——
        插空导致时间顺序与序列顺序不一致时，以真实时间为准。
        """
        W = self.weights
        by_eq = {}
        tardy = 0.0
        drift = 0.0
        for oid, p in pl.items():
            by_eq.setdefault(p['eq'], []).append((p['es'], p['pe'], oid))
            job = self.jobs[oid]
            due = job.get('due')
            if due is not None and p['pe'] > due:
                d = (p['pe'] - due).total_seconds() / 86400.0
                try:
                    w = float(job.get('cust') or 0)
                except (ValueError, TypeError):
                    w = 0.0
                tardy += d * (w if w > 0 else 1.0)
            b = self.base_es.get(oid)
            if b is not None:
                drift += abs((p['es'] - b).total_seconds()) / 86400.0

        co_h = 0.0
        for _eq, lst in by_eq.items():
            lst.sort(key=lambda x: (x[0], x[2]))
            prev_mat = None
            for (_es, _pe, oid) in lst:
                mat = self.jobs[oid]['mat']
                if prev_mat is not None:
                    co_h += self.changeover_between(prev_mat, mat)
                prev_mat = mat

        loads = dict(self.static_load)
        for _eq, lst in by_eq.items():
            for (es, pe, _oid) in lst:
                loads[_eq] = loads.get(_eq, 0.0) + (pe - es).total_seconds() / 86400.0
        vals = sorted(loads.values(), reverse=True)
        spread = (vals[0] - vals[-1]) if len(vals) > 1 else 0.0

        parts = {'tardy': tardy, 'changeover': co_h / 24.0,
                 'balance': spread, 'stability': drift}
        total = (W.get('tardy', 0.0) * parts['tardy']
                 + W.get('changeover', 0.0) * parts['changeover']
                 + W.get('balance', 0.0) * parts['balance']
                 + W.get('stability', 0.0) * parts['stability'])
        parts['total'] = total
        return parts

    def cost(self, pl):
        return self.cost_parts(pl)['total']


# --------------------------------------------------------------------------
# 邻域算子（全部作用在「序列」上，返回受影响的设备集合）
# --------------------------------------------------------------------------

def _locate(seqs, oid):
    for eq, seq in seqs.items():
        for i, x in enumerate(seq):
            if x == oid:
                return eq, i
    return None, None


def _rollback(cur, backup):
    """把 cur 按 backup 还原（【P3】relocate 可能新建设备键，需按 None 删除）。"""
    for eq, seq in backup.items():
        if seq is None:
            cur.pop(eq, None)
        else:
            cur[eq] = seq


def _apply(seqs, move):
    """应用一个邻域动作，返回 (changed_eqs, backup)；失败返回 (None, None)。

    backup 只备份受影响设备的序列（不是全量），回滚成本 O(该设备序列长度)。
    backup 中值为 None 表示该键是本次动作**新建**的，回滚时应删除（见 _rollback）。
    """
    kind = move[0]
    if kind == 'relocate':
        _, oid, dst, pos = move
        src, i = _locate(seqs, oid)
        if src is None:
            return None, None
        # 【P3 修复 · 2026-10-14】允许迁到「本轮无单」的空闲设备：
        # 原实现要求 `dst in seqs`，而 seqs 只含贪心落点 → 空闲设备永远进不来，
        # 负载均衡项（权重 0.2）形同虚设。现按需新建空序列，并在 backup 里以 None 标记。
        dst_new = dst not in seqs
        if dst_new:
            seqs[dst] = []
        changed = {src, dst}
        backup = {eq: list(seqs[eq]) for eq in changed}
        if dst_new:
            backup[dst] = None
        seqs[src].pop(i)
        d = seqs[dst]
        p = max(0, min(pos, len(d)))
        seqs[dst] = d[:p] + [oid] + d[p:]
        return changed, backup
    if kind == 'swap':
        _, o1, o2 = move
        e1, i1 = _locate(seqs, o1)
        e2, i2 = _locate(seqs, o2)
        if e1 is None or e2 is None:
            return None, None
        changed = {e1, e2}
        backup = {eq: list(seqs[eq]) for eq in changed}
        seqs[e1][i1] = o2
        seqs[e2][i2] = o1
        return changed, backup
    if kind == 'shift':
        _, eq, i, j = move
        seq = seqs.get(eq)
        if seq is None or not (0 <= i < len(seq)):
            return None, None
        backup = {eq: list(seq)}
        oid = seq.pop(i)
        seq.insert(max(0, min(j, len(seq))), oid)
        return {eq}, backup
    return None, None


def _propose(rng, seqs, ctx):
    """随机生成一个邻域动作描述（**不修改** seqs，apply 阶段才改，便于回滚）。

    返回 None 表示当前解无法生成该动作（如所有单都只有唯一候选设备）。
    """
    oids = [oid for seq in seqs.values() for oid in seq]
    if not oids:
        return None
    r = rng.random()
    if r < 0.45:                                   # relocate：跨设备迁移 + 指定插入位置
        oid = rng.choice(oids)
        # 【P3 修复 · 2026-10-14】不再过滤 `if e in seqs`：候选设备即合法落点，
        # 本轮贪心没派单的空闲设备同样应该被考虑（否则均衡项无解可走）。
        # 硬约束①（只落候选设备内）仍然成立，因为 cands 本身就来自工单候选设备。
        cands = list(ctx.jobs[oid].get('cands') or [])
        if not cands:
            return None
        dst = rng.choice(cands)
        return ('relocate', oid, dst, rng.randint(0, len(seqs.get(dst) or [])))
    if r < 0.80:                                   # swap：两张单位置互换（可跨设备）
        if len(oids) < 2:
            return None
        o1, o2 = rng.sample(oids, 2)
        e1, _ = _locate(seqs, o1)
        e2, _ = _locate(seqs, o2)
        # 硬约束①：换过去之后所在设备必须仍在其候选设备内
        if e2 not in (ctx.jobs[o1].get('cands') or []):
            return None
        if e1 not in (ctx.jobs[o2].get('cands') or []):
            return None
        return ('swap', o1, o2)
    # shift：同设备内换位置（**驱动同物料聚批、减少换型**的唯一算子）
    eqs = [eq for eq, seq in seqs.items() if len(seq) >= 2]
    if not eqs:
        return None
    eq = rng.choice(eqs)
    n = len(seqs[eq])
    i = rng.randrange(n)
    j = rng.randrange(n)
    if i == j:
        return None
    return ('shift', eq, i, j)


def _re_decode(ctx, pl, seqs, changed):
    """增量解码：保留未受影响设备的放置，只重算 changed 中的设备。

    注意必须**先剔除**旧 pl 里属于 changed 设备的项，否则已迁走的单会残留。
    """
    pl2 = {oid: p for oid, p in pl.items() if p['eq'] not in changed}
    for eq in changed:
        pl2.update(ctx.decode_eq(eq, seqs[eq]))
    return pl2


def _breaks_due(ctx, pl_old, pl_new, changed):
    """硬约束②：原本不超期的单，不得被迁成超期（交期优先于均衡/换型）。

    只检查落在 changed 设备上的单；旧解中该单不存在（新增）时跳过。
    """
    for oid, p in pl_new.items():
        if p['eq'] not in changed:
            continue
        old = pl_old.get(oid)
        if old is None:
            continue
        due = ctx.jobs[oid].get('due')
        if due is None:
            continue
        if old['pe'] <= due and p['pe'] > due:
            return True
    return False


def anneal(ctx, seqs, budget_sec=3.0, seed=20261007, max_iters=4000,
           t0_ratio=0.10, tmin_ratio=0.01):
    """【L2】序列空间上的模拟退火（Metropolis 接受 + 几何降温）。

    返回 (best_seqs, best_cost, info)。
      · 固定 seed → 同输入同输出（回归可复现）；
      · 受 `budget_sec` 时间预算约束（与 S1 一致，超时即接受当前最优）；
      · 温度按迭代进度下降（不依赖墙钟，保证可复现）；
      · 硬约束：① 只能落在候选设备内（算子生成时保证）
                ② 原本不超期的单不得被迁成超期（交期优先于均衡）。
      · 返回的是**搜索过程中见过的最优解**，不是最后一个解。

    info = {iters, accepted, improved, cost_from, cost_to, elapsed_sec, timed_out, seed}
    """
    rng = random.Random(seed)
    cur = {eq: list(seq) for eq, seq in seqs.items()}
    pl = ctx.decode(cur)
    cur_cost = ctx.cost(pl)
    best = {eq: list(seq) for eq, seq in cur.items()}
    best_cost = cur_cost
    cost_from = cur_cost
    iters = accepted = improved = 0
    t_start = time.monotonic()
    timed_out = False

    if budget_sec > 0 and max_iters > 0:
        T0 = max(0.5, abs(cur_cost) * t0_ratio)
        Tmin = max(1e-3, T0 * tmin_ratio)
        alpha = (Tmin / T0) ** (1.0 / max(1, max_iters))
        T = T0
        for _ in range(max_iters):
            # 【P2-14 修复 · 2026-10-14】原实现以**墙钟**为主终止条件（每 32 轮查一次），
            # 这直接破坏了"固定 seed 可复现"的承诺：迭代次数由当时机器负载决定
            # （同一份数据、同一 seed，忙时跑 300 轮、闲时跑 480 轮）→
            # 两次排产结果不同是常态，无法做回归基线，也让 seed 形同虚设。
            # 现改为：**max_iters 是确定的迭代预算**（可复现的主体），
            # 墙钟只作为看门狗（兜底防死循环），并把是否触发记入 info.timed_out 便于观测。
            if (_ & 31) == 0 and (time.monotonic() - t_start) > budget_sec * _WATCHDOG_MULT:
                timed_out = True
                break
            iters += 1
            move = _propose(rng, cur, ctx)
            if move is None:
                continue
            changed, backup = _apply(cur, move)
            if not changed:
                continue
            pl2 = _re_decode(ctx, pl, cur, changed)
            if _breaks_due(ctx, pl, pl2, changed):
                _rollback(cur, backup)
                continue
            c = ctx.cost(pl2)
            d = c - cur_cost
            if d <= 0 or (T > 0 and rng.random() < math.exp(-d / T)):
                pl, cur_cost = pl2, c
                accepted += 1
                if c < best_cost - COST_EPS:
                    best_cost = c
                    best = {eq: list(seq) for eq, seq in cur.items()}
                    improved += 1
            else:
                _rollback(cur, backup)  # 回滚（backup 是旧列表对象，直接还原引用）
            T = max(Tmin, T * alpha)

    info = {'iters': iters, 'accepted': accepted, 'improved': improved,
            'cost_from': round(cost_from, 6), 'cost_to': round(best_cost, 6),
            'elapsed_sec': round(time.monotonic() - t_start, 3),
            'timed_out': timed_out, 'seed': seed}
    return best, best_cost, info
