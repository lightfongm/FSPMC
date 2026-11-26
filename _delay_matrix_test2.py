#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
一键计算 四场景矩阵验证 v2（独立预言机 + 测试/期待/实际 三方严格比对）

背景（2026-10-15 用户判定旧报告马虎，全部重做）：
  旧报告缺陷：① S2 排程 aufnr 0009 测试 59 vs 实际 159 仍判 PASS；
             ② 缺料页测试列全空仍判 PASS；③ 测试日期与实际日期不一致仍判 PASS。
  根因：verdict 函数从不比对测试 vs 实际；缺料基线键 (component, source, source_refs) 因
        source_refs 全空而大规模碰撞。

本版设计：
  1) 独立预言机 oracle：先用「全 OFF 计算」建立规范基线，并要求 oracle 逐行复现基线
     （自检不过 → 中止，不出报告）。
  2) 每场景：测试数据 = 计算前快照（须==基线）；期待数据 = 规则推导（oracle）；
     实际数据 = 计算后快照。连跑 3 次验幂等。
  3) 判定硬标准：
       PASS ⟺ 实际==期待（逐字段） 且 数量守恒（测试成员Σ==实际） 且
              日期规则一致（测试成员日期→期待日期 的每一步都由已实现规则解释）
     报告同时如实展示「测试数量 vs 实际数量是否一样」「测试日期 vs 实际日期是否一样」，
     每一项「不一样」都必须有归因；不可归因条目必须为 0。
  4) 金丝雀自检：把实际快照篡改一行后送进比对器，必须报 FAIL（证明比对器非空转）。
"""
import json
import math
import re
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime, date, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP

import pymysql

API = 'http://127.0.0.1:5000'
DB = dict(host='127.0.0.1', port=3306, user='root', passwd='', db='schedule_system', charset='utf8mb4')
TODAY = datetime.now(timezone(timedelta(hours=8))).date()   # 与 backend.utils.biz_today 同源
T8 = TODAY.strftime('%Y%m%d')
T10 = TODAY.strftime('%Y-%m-%d')
EPS = 1e-6
MERGE_N = 30
RUNS = 3

SCHED_COLS = ['id', 'aufnr', 'order_no', 'material_code', 'source', 'sales_posnr', 'parent_code',
              'drive_aufnr', 'work_order_id', 'source_refs', 'batch_qty', 'plan_qty',
              'batch_date_start', 'batch_date_end', 'delayed', 'is_deleted',
              'ready_status', 'status_description']
SHORT_COLS = ['id', 'aufnr', 'material_code', 'component_code', 'source', 'source_refs',
              'required_qty', 'available_qty', 'current_stock', 'shortage_type', 'delayed',
              'component_demand_date', 'parent_code', 'parent_qty', 'bom_level',
              'is_parent_material']

SCENARIOS = [
    ('S1', '无参（计划需求日期关 / 排程合并0 / 缺料合并0）',
     {'limit': 100, 'enabled_stock_types': ['unrestricted'], 'schedule_merge_days': 0, 'merge_days': 0}, False),
    ('S2', '仅开「计划需求日期」开关',
     {'limit': 100, 'enabled_stock_types': ['unrestricted'], 'schedule_merge_days': 0, 'merge_days': 0}, True),
    ('S3', '仅设「排程合并天数=30」',
     {'limit': 100, 'enabled_stock_types': ['unrestricted'], 'schedule_merge_days': MERGE_N, 'merge_days': 0}, False),
    ('S4', '「计划需求日期」开 + 排程合并30',
     {'limit': 100, 'enabled_stock_types': ['unrestricted'], 'schedule_merge_days': MERGE_N, 'merge_days': 0}, True),
]


# ────────────────────────── 基础工具 ──────────────────────────
def db_conn():
    return pymysql.connect(**DB, cursorclass=pymysql.cursors.DictCursor)


def q(sql, args=None):
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args or ())
            return cur.fetchall()
    finally:
        conn.close()


def set_cfg(key, value):
    conn = db_conn()
    try:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO system_config (config_key, config_value) VALUES (%s,%s) "
                "ON DUPLICATE KEY UPDATE config_value=%s", (key, str(value), str(value)))
        conn.commit()
    finally:
        conn.close()


def calc(body):
    req = urllib.request.Request(
        API + '/api/peacock/calculate',
        data=json.dumps(body).encode('utf-8'),
        headers={'Content-Type': 'application/json'}, method='POST')
    with urllib.request.urlopen(req, timeout=600) as resp:
        return json.loads(resp.read().decode('utf-8'))


def pdate(v):
    """与 backend _parse_date 同口径：支持 YYYYMMDD / YYYY-MM-DD / 带时间。"""
    if not v:
        return None
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    s = str(v).strip()
    cands = []
    if ' ' in s or 'T' in s:
        cands.append(s[:19])
    cands.append(s[:10])
    for c in cands:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%Y%m%d'):
            try:
                return datetime.strptime(c, fmt).date()
            except ValueError:
                continue
    return None


def r3(x):
    """MySQL DECIMAL(18,3) 入库口径：四舍五入(半远离零)到 3 位。"""
    if x is None:
        return None
    return float(Decimal(repr(float(x))).quantize(Decimal('0.001'), rounding=ROUND_HALF_UP))


def d8(d):
    return d.strftime('%Y%m%d') if d else ''


def d10(d):
    return d.strftime('%Y-%m-%d') if d else ''


def is8(s):
    return bool(s) and bool(re.fullmatch(r'[0-9]{8}', s))


# ────────────────────────── 静态上下文 ──────────────────────────
def load_static():
    st = {}
    prods = q("SELECT material_code, name, type, production_time, production_cycle, "
              "min_purchase_qty, batch_size FROM products")
    st['type'] = {p['material_code']: (p['type'] or '').strip() for p in prods}
    st['daily'] = {p['material_code']: float(p['production_time'] or 0) for p in prods}
    st['cycle'] = {p['material_code']: float(p['production_cycle'] or 0) for p in prods}
    st['lot'] = {p['material_code']: (float(p['min_purchase_qty'] or 0), float(p['batch_size'] or 0))
                 for p in prods}
    st['name'] = {p['material_code']: p['name'] for p in prods}
    bom = q("SELECT parent_matnr, comp_matnr, comp_maktx, comp_menge, yield_qty FROM bom")
    st['bom'] = defaultdict(list)
    for b in bom:
        st['bom'][b['parent_matnr']].append(b)
    inv = q("SELECT material_code, SUM(COALESCE(sap_labst,0)) AS a FROM inventory GROUP BY material_code")
    st['inv'] = {r['material_code']: float(r['a'] or 0) for r in inv}
    st['wos'] = q("SELECT id, order_no, material_code, COALESCE(confirm_qty,0) AS cq, "
                  "COALESCE(completed_qty,0) AS cpq, planned_start, planned_end "
                  "FROM work_orders WHERE status IN ('批准','执行') ORDER BY id")
    st['wo_ref'] = {r['id']: (r['order_no'] or '').strip()
                    for r in q("SELECT id, order_no FROM work_orders")}
    return st


def lead(st, mat, qty):
    d = st['daily'].get(mat, 0) or 0
    if d > 0 and qty and qty > 0:
        return int(math.ceil(qty / d))
    return int(st['cycle'].get(mat, 0) or 0)


def lot_size(buy, min_q, batch_sz):
    if buy <= 0:
        return 0.0
    b = buy
    if min_q and min_q > 0 and b < min_q:
        b = min_q
    if batch_sz and batch_sz > 0:
        b = math.ceil((b - 1e-9) / batch_sz) * batch_sz
    return float(b)


# ────────────────────────── 快照 ──────────────────────────
def _norm_sched(r):
    out = {}
    for k in SCHED_COLS:
        v = r.get(k)
        if k in ('batch_qty', 'plan_qty'):
            out[k] = float(v or 0)
        elif k in ('delayed', 'is_deleted'):
            out[k] = int(v or 0)
        elif k in ('batch_date_start', 'batch_date_end'):
            out[k] = ('' if v is None else str(v)).strip()
        else:
            out[k] = ('' if v is None else str(v))
    return out


def _norm_short(r):
    out = {}
    for k in SHORT_COLS:
        v = r.get(k)
        if k in ('required_qty', 'available_qty', 'current_stock', 'parent_qty'):
            out[k] = r3(v or 0)
        elif k in ('delayed', 'is_parent_material', 'bom_level'):
            out[k] = int(v or 0)
        elif k == 'component_demand_date':
            out[k] = d10(pdate(v)) if v else ''
        else:
            out[k] = ('' if v is None else str(v))
    return out


def snap_sched():
    rows = q("SELECT %s FROM peacock_schedule ORDER BY material_code, aufnr"
             % ','.join('`%s`' % c for c in SCHED_COLS))
    return [_norm_sched(r) for r in rows]


def snap_short():
    rows = q("SELECT %s FROM peacock_material_shortage "
             "ORDER BY component_code, aufnr, material_code, component_demand_date"
             % ','.join('`%s`' % c for c in SHORT_COLS))
    return [_norm_short(r) for r in rows]


# ────────────────────────── 预言机：排程侧 ──────────────────────────
def doc_ref_sched(r):
    src = (r.get('source') or '').strip()
    if src == 'WO':
        return (r.get('work_order_id') or '').strip()
    if src == 'SCH':
        ono = (r.get('order_no') or '').strip()
        pos = (r.get('sales_posnr') or '').strip()
        return '%s-%s' % (ono, pos) if (ono and pos) else ono
    if src == 'SEMI':
        return (r.get('drive_aufnr') or '').strip()
    return src or ''


def oracle_schedule(base_rows, st, switch_on, smd, warn):
    """复现 peacock_calculate 排程侧全时序：
       readiness 重建(delayed=0/source_refs=''/SCH·SEMI start 空) →
       半成品阶段 backfill#1 → 预填(no-op) → 归位(开关) → 合并 →
       缺料阶段 backfill#3 → 无条件标红。
    返回 (rows, members)：rows=预期行(含 is_deleted)；members={锚点aufnr: [成员基线aufnr]}"""
    # ── readiness 重建后的"新鲜"状态（每次一键计算都全表 DELETE+INSERT）:
    #    delayed 默认 0；source_refs 不写(NULL)；SCH/SEMI 的 batch_date_start 为空；
    #    WO 行 start = 工单计划开工。其余列与基线一致（静态数据不变 → 重建确定）。
    rows = []
    for b in base_rows:
        if b['is_deleted'] != 0:
            continue
        r = dict(b)
        r['delayed'] = 0
        r['source_refs'] = ''
        r['is_deleted'] = 0
        if r['source'] in ('SCH', 'SEMI'):
            r['batch_date_start'] = ''
        rows.append(r)
    members = {r['aufnr']: [r['aufnr']] for r in rows}

    def backfill():
        """_backfill_semi_dates：非 WO 行 start = end − lead(plan_qty)；
        开关 ON 且 start<当日 → 钳当日 + 完工<开工压当日 + 补标 delayed=1（三动作）。"""
        for r in rows:
            if r['is_deleted'] != 0 or r['source'] == 'WO':
                continue
            end = pdate(r['batch_date_end'] or r['batch_date_start'])
            pq = r['plan_qty']
            if end and pq > 0:
                ld = lead(st, r['material_code'], pq)
                sd = (end - timedelta(days=ld)) if ld else end
                if switch_on and sd < TODAY:
                    sd = TODAY
                    if end < sd:
                        end = sd
                        r['batch_date_end'] = d8(end)
                    r['delayed'] = 1
                r['batch_date_start'] = d8(sd)

    # 半成品阶段（_generate_semi_phase → _rebuild_all_shortages 内两次 backfill，幂等）
    backfill()
    # _prefill_schedule_starts：backfill#1 之后 SCH/SEMI 有量行的 start 已填；
    # 无量行(0006, plan_qty=0) 两侧均跳过 → 对本数据恒为 no-op（保持与源码一致的口径）。

    # 规则1 归位（开关 ON，排除 WO，只认 8 位）——backfill#1 已钳过，通常 no-op，保留保真
    if switch_on:
        for r in rows:
            if r['source'] == 'WO':
                continue
            s = r['batch_date_start']
            if is8(s) and s < T8:
                r['batch_date_start'] = T8
                e = r['batch_date_end']
                if is8(e):
                    delta = (pdate(e) - TODAY).days     # MySQL: DATEDIFF(旧end, 新start=当日)
                    r['batch_date_end'] = d8(TODAY + timedelta(days=max(0, delta)))
                r['delayed'] = 1

    # 2) _merge_schedule_rows
    if smd > 0 or switch_on:
        eff = smd if smd > 0 else 0
        cand = [r for r in rows if r['source'] in ('SCH', 'SEMI') and r['batch_qty'] > 0]
        cand.sort(key=lambda r: (r['material_code'], r['batch_date_end'] or '99999999',
                                 r['batch_date_start'] or '99999999', r['aufnr']))
        by_mat = defaultdict(list)
        for r in cand:
            by_mat[r['material_code']].append(r)

        def mk(r):
            return pdate(r['batch_date_start']) or pdate(r['batch_date_end'])

        for mat, lines in by_mat.items():
            dated = [l for l in lines if mk(l)]
            undated = [l for l in lines if not mk(l)]
            dated.sort(key=mk)                      # 稳定排序
            segs, cur, anchor = [], None, None
            for l in dated:
                d = mk(l)
                if cur is None:
                    cur, anchor = [l], d
                elif (d - anchor).days > eff:
                    segs.append(cur)
                    cur, anchor = [l], d
                else:
                    cur.append(l)
            if cur:
                segs.append(cur)
            for l in undated:
                segs.append([l])
            for seg in segs:
                if len(seg) <= 1:
                    continue
                a = seg[0]
                sum_batch = sum(x['batch_qty'] for x in seg)
                sum_plan = sum(x['plan_qty'] for x in seg)
                sources, refs = [], []
                for x in seg:
                    s = (x['source'] or '').strip()
                    if s and s not in sources:
                        sources.append(s)
                    dr = doc_ref_sched(x)
                    if dr and dr not in refs:
                        refs.append(dr)
                max_delayed = 1 if any(x['delayed'] == 1 for x in seg) else 0
                aend = pdate(a['batch_date_end'])
                ns = a['batch_date_start']
                if aend:
                    ld = lead(st, mat, sum_batch)
                    ns = d8(aend - timedelta(days=ld) if ld else aend)
                    if max_delayed == 1 and ns < T8:
                        ns = T8
                a['batch_qty'] = sum_batch
                a['plan_qty'] = sum_plan
                a['source'] = ','.join(sources)
                a['source_refs'] = '、'.join(refs)
                a['work_order_id'] = ''
                a['batch_date_start'] = ns
                a['delayed'] = max_delayed
                for x in seg:
                    if x['id'] != a['id']:
                        x['is_deleted'] = 1
                members[a['aufnr']] = [x['aufnr'] for x in seg]

    # 3) 缺料阶段 backfill#3（合并后，仅存活行；plan_qty 已是合并后 Σ）
    backfill()

    # 4) _mark_schedule_overdue（无条件，OR，仅 is_deleted=0）
    for r in rows:
        if r['is_deleted'] != 0 or r['source'] == 'WO':
            continue
        s = r['batch_date_start']
        if is8(s) and s < T8:
            r['delayed'] = 1
    return rows, members


# ────────────────────────── 预言机：缺料侧 ──────────────────────────
def oracle_shortage(sched_rows, st, switch_on, md, warn):
    surviving = [r for r in sched_rows if r['is_deleted'] == 0]
    mat_in_schedule = set(r['material_code'] for r in surviving)
    schedule_set = set((r['material_code'], d8(pdate(r['batch_date_end'] or r['batch_date_start'])))
                       for r in surviving)
    # _sched_ref / _wo_ref
    sched_ref = {}
    for r in surviving:
        ono = (r.get('order_no') or '').strip()
        pos = (r.get('sales_posnr') or '').strip()
        if ono:
            sched_ref[r['id']] = '%s-%s' % (ono, pos) if pos else ono
        elif (r.get('source') or '').strip() == 'SEMI':
            sched_ref[r['id']] = (r.get('drive_aufnr') or '').strip() or (r.get('material_code') or '').strip()
    wo_ref = st['wo_ref']
    wo_sched_map = {}
    for r in surviving:
        if r.get('work_order_id'):
            for wid in str(r['work_order_id']).split(','):
                wid = wid.strip()
                if wid:
                    wo_sched_map[wid] = r['id']

    req_lines = []

    def explode(matnr, qty, level, parent_code, sid, seq, req_date, src, wo_id, drive, ancestors=None):
        if qty <= 0 or matnr not in st['bom']:
            return
        ancestors = ancestors or frozenset()
        if matnr in ancestors:
            warn.append('BOM 环：%s（祖先 %s）' % (matnr, sorted(ancestors)))
            return
        ancestors = ancestors | {matnr}
        for b in st['bom'][matnr]:
            comp = b['comp_matnr']
            yq = float(b.get('yield_qty') or 1) or 1
            cm = float(b.get('comp_menge') or 0)
            req = (qty / yq) * cm if yq > 0 else 0
            if req <= 0:
                continue
            ctype = st['type'].get(comp, '')
            if ctype in ('产成品', '半成品'):
                if comp in mat_in_schedule:
                    continue
                warn.append('未建模路径：动态 SEMI %s（父 %s）' % (comp, matnr))
                return
            cc = lead(st, comp, req)
            crd = (req_date - timedelta(days=cc)) if (req_date and cc) else req_date
            req_lines.append({
                'source': src, 'schedule_id': sid, 'work_order_id': wo_id, 'aufnr': seq,
                'matnr': matnr, 'comp_code': comp, 'comp_name': b.get('comp_maktx') or '',
                'gross': req, 'req_date': crd, 'bom_level': level, 'parent_code': matnr,
                'parent_qty': qty, 'is_parent_material': 1 if comp in st['bom'] else 0,
            })
            if (comp, d8(req_date)) not in schedule_set and comp in st['bom']:
                cyc = lead(st, comp, req)
                cd = (req_date - timedelta(days=cyc)) if (req_date and cyc) else req_date
                explode(comp, req, level + 1, matnr, sid, seq, cd, src, wo_id, drive, ancestors)

    # 排程来源（非 WO），按 SQL 稳定序
    order = sorted(surviving, key=lambda r: (r['material_code'], r['batch_date_end'] or '99999999',
                                             r['batch_date_start'] or '99999999', r['aufnr']))
    for s in order:
        if s['source'] == 'WO':
            continue
        qty = s['batch_qty']
        if qty <= 0:
            continue
        cyc = lead(st, s['material_code'], qty)
        pdd = pdate(s['batch_date_end'] or s['batch_date_start'])
        rd = (pdd - timedelta(days=cyc)) if (pdd and cyc) else pdd
        explode(s['material_code'], qty, 0, '', s['id'], s['aufnr'], rd, 'SCH', None, s['aufnr'])

    # 工单来源（ORDER BY id）
    for wo in st['wos']:
        matnr = wo['material_code']
        eff = float(wo['cq'] or 0) - float(wo['cpq'] or 0)
        if eff <= 0 or matnr not in st['bom']:
            continue
        wo_no = wo['order_no'] or wo['id']
        cyc = lead(st, matnr, eff)
        pdd = pdate(wo['planned_start'] or wo['planned_end'])
        rd = (pdd - timedelta(days=cyc)) if (pdd and cyc) else pdd
        explode(matnr, eff, 0, '', wo_sched_map.get(wo['id']), wo_no, rd, 'WO', wo['id'], wo_no)

    # :1190 需求行预排序（规则2/合并之前）：需求日 → 来源(SCH<WO) → 序列号。
    # 缺料合并的 stable sort 在同日簇上保留本顺序 → 锚点/来源单据拼接顺序由它决定。
    req_lines.sort(key=lambda x: (x['req_date'] or date.max,
                                  0 if x['source'] == 'SCH' else 1, str(x['aufnr'] or '')))

    # 规则2 归位（开关 ON，排除 WO）
    if switch_on:
        for rl in req_lines:
            if (rl.get('source') or '') == 'WO':
                continue
            rd = rl.get('req_date')
            if rd is not None and rd < TODAY:
                rl['req_date'] = TODAY
                rl['delayed'] = 1
    # 无条件延误标记（OR）
    for rl in req_lines:
        if rl.get('delayed', 0):
            continue
        rd = rl.get('req_date')
        if rd is not None and rd < TODAY:
            rl['delayed'] = 1

    # 缺料合并
    if md > 0 or switch_on:
        eff = md if md > 0 else 0

        def _doc_ref(line):
            src = line.get('source')
            if src == 'WO':
                return wo_ref.get(line.get('work_order_id') or '', '')
            if src == 'SCH':
                ref = sched_ref.get(line.get('schedule_id') or '', '')
                if not ref:
                    ref = line.get('matnr') or line.get('aufnr') or ''
                return ref
            if src == 'SEMI':
                return line.get('drive_aufnr') or line.get('aufnr') or line.get('matnr') or ''
            return src or ''

        by_comp = defaultdict(list)
        for line in req_lines:
            by_comp[line['comp_code']].append(line)
        merged = []
        for comp, lines in by_comp.items():
            dated = [l for l in lines if l.get('req_date') is not None]
            undated = [l for l in lines if l.get('req_date') is None]
            dated.sort(key=lambda l: l['req_date'])
            segs, cur, anchor = [], None, None
            for l in dated:
                d = l['req_date']
                if cur is None:
                    cur, anchor = [l], d
                elif (d - anchor).days > eff:
                    segs.append(cur)
                    cur, anchor = [l], d
                else:
                    cur.append(l)
            if cur:
                segs.append(cur)
            for l in undated:
                segs.append([l])
            for seg in segs:
                if len(seg) == 1:
                    lone = dict(seg[0])
                    lone['source_refs'] = _doc_ref(seg[0])
                    lone['delayed'] = seg[0].get('delayed', 0)
                    merged.append(lone)
                    continue
                gross = sum(float(x.get('gross') or 0) for x in seg)
                if gross <= 0:
                    continue
                sources, refs, parents = [], [], []
                for x in seg:
                    s = x.get('source')
                    if s and s not in sources:
                        sources.append(s)
                    dr = _doc_ref(x)
                    if dr and dr not in refs:
                        refs.append(dr)
                    pc = x.get('parent_code') or ''
                    if pc and pc not in parents:
                        parents.append(pc)
                first = seg[0]
                merged.append({
                    'source': ','.join(sources), 'source_refs': '、'.join(refs),
                    'comp_code': comp, 'comp_name': first.get('comp_name', ''),
                    'gross': gross, 'parent_qty': sum(float(x.get('parent_qty') or 0) for x in seg),
                    'req_date': first.get('req_date'), 'bom_level': first.get('bom_level', 0),
                    'is_parent_material': first.get('is_parent_material', 0),
                    'schedule_id': first.get('schedule_id'), 'work_order_id': first.get('work_order_id'),
                    'aufnr': first.get('aufnr'), 'matnr': first.get('matnr'),
                    'parent_code': ('、'.join(parents) if len(parents) > 1
                                    else (parents[0] if parents else (first.get('parent_code') or ''))),
                    'delayed': 1 if any((x.get('delayed') or 0) == 1 for x in seg) else 0,
                })
        req_lines = merged

    # 消耗（实物 → 建议采购池；在途测试关闭）
    by_comp = defaultdict(list)
    for line in req_lines:
        by_comp[line['comp_code']].append(line)

    def ckey(x):
        rd = x.get('req_date')
        dk = rd.strftime('%Y%m%d') if isinstance(rd, (datetime, date)) else (str(rd) if rd else '99999999')
        return (dk, str(x.get('source') or ''), str(x.get('aufnr') or ''), str(x.get('matnr') or ''),
                str(x.get('comp_code') or ''), float(x.get('gross') or 0))

    out = []
    for comp, lines in by_comp.items():
        lines.sort(key=ckey)
        keys = [ckey(x) for x in lines]
        if len(set(keys)) != len(keys):
            warn.append('耗用排序键并列（%s）：顺序不可判定' % comp)
        stock = float(st['inv'].get(comp, 0.0))
        full_stock = stock
        suggested = 0.0
        for line in lines:
            req = float(line['gross'])
            avail = stock + suggested
            need = req - avail
            buy = 0.0 if need <= EPS else need
            if buy == 0:
                stype = 'OK'
            elif buy < req:
                stype = 'PARTIAL'
            else:
                stype = 'MISSING'
            out.append({
                'aufnr': line.get('aufnr') or '', 'material_code': line.get('matnr') or '',
                'component_code': comp, 'source': line.get('source') or '',
                'source_refs': line.get('source_refs') or '',
                'required_qty': r3(req), 'available_qty': r3(avail), 'current_stock': r3(full_stock),
                'shortage_type': stype, 'delayed': int(line.get('delayed', 0) or 0),
                'component_demand_date': d10(line.get('req_date')),
                'parent_code': line.get('parent_code') or '', 'parent_qty': r3(line.get('parent_qty') or 0),
                'bom_level': int(line.get('bom_level', 0) or 0),
                'is_parent_material': int(line.get('is_parent_material', 0) or 0),
            })
            c = req
            u = min(c, stock); stock -= u; c -= u
            u = min(c, suggested); suggested -= u; c -= u
            if buy > 0:
                mn, bs = st['lot'].get(comp, (0.0, 0.0))
                suggested += lot_size(buy, mn, bs) - buy
    return out


# ────────────────────────── ready_status 推导（与 _rebuild_all_shortages 同口径） ──────────────────────────
def derive_ready_status(short_rows, sched_rows, md):
    """返回 {aufnr: (ready_status, status_description, judged)}。
    judged=False = 无法从缺料行推导（缺料合并后只有锚点行带 schedule_id，
    非锚点行保留齐备套计算值）——此类行只做信息披露，不进严格判定。"""
    res = {}
    if md > 0:
        for r in sched_rows:
            if r['is_deleted'] == 0:
                res[r['aufnr']] = ('缺料合并模式', '不统计', True)
        return res
    by_aufnr = defaultdict(lambda: {'total': 0, 'ready': 0, 'partial': 0, 'missing': 0})
    for s in short_rows:
        by_aufnr[s['aufnr']]['total'] += 1
        if s['shortage_type'] == 'OK':
            by_aufnr[s['aufnr']]['ready'] += 1
        elif s['shortage_type'] == 'PARTIAL':
            by_aufnr[s['aufnr']]['partial'] += 1
        else:
            by_aufnr[s['aufnr']]['missing'] += 1
    for r in sched_rows:
        if r['is_deleted'] != 0:
            continue
        c = by_aufnr.get(r['aufnr'])
        if c is None:
            if r['plan_qty'] == 0:
                res[r['aufnr']] = ('A', '齐备', True)
            else:
                res[r['aufnr']] = ('', '', False)   # 保留齐备套计算值，不判定
        elif c['missing'] == 0 and c['partial'] == 0:
            res[r['aufnr']] = ('A', '齐备', True)
        elif c['missing'] == 0:
            res[r['aufnr']] = ('B', '部分齐备', True)
        else:
            res[r['aufnr']] = ('C', '缺料', True)
    return res


# ────────────────────────── 比对器 ──────────────────────────
SCHED_VAL = ['material_code', 'source', 'order_no', 'sales_posnr', 'drive_aufnr', 'parent_code',
             'batch_qty', 'plan_qty', 'batch_date_start', 'batch_date_end', 'delayed',
             'is_deleted', 'source_refs', 'work_order_id']
SHORT_VAL = ['material_code', 'component_code', 'source', 'source_refs', 'required_qty',
             'available_qty', 'current_stock', 'shortage_type', 'delayed',
             'component_demand_date', 'parent_code', 'parent_qty', 'bom_level', 'is_parent_material']


def _biz(rows):
    """去掉每次重建都会轮换的 uuid 主键，只留业务字段（用于跨次幂等比对）。"""
    return [{k: v for k, v in r.items() if k != 'id'} for r in rows]


def cmp_by_key(expected, actual, key, val_fields):
    """按键 1:1 比对。返回 (ok, diffs)"""
    emap = {r[key]: r for r in expected}
    amap = {r[key]: r for r in actual}
    diffs = []
    for k in sorted(set(emap) | set(amap)):
        e, a = emap.get(k), amap.get(k)
        if e is None:
            diffs.append('%s 实际多出（期待无此行）' % k)
            continue
        if a is None:
            diffs.append('%s 期待有、实际缺失' % k)
            continue
        for f in val_fields:
            ev, av = e.get(f), a.get(f)
            if isinstance(ev, float) or isinstance(av, float):
                if abs(float(ev or 0) - float(av or 0)) > 0.0011:
                    diffs.append('%s.%s 期待=%s 实际=%s' % (k, f, ev, av))
            elif ev != av:
                diffs.append('%s.%s 期待=%r 实际=%r' % (k, f, ev, av))
    return (len(diffs) == 0), diffs


def cmp_multiset(expected, actual, val_fields):
    """缺料行无稳定业务键 → 全字段多元组比对。返回 (ok, diffs, pairs)"""
    def tup(r):
        return tuple(r.get(f) for f in val_fields)
    from collections import Counter
    ec, ac = Counter(tup(r) for r in expected), Counter(tup(r) for r in actual)
    diffs = []
    for t in sorted(set(ec) | set(ac), key=lambda x: tuple(str(i) for i in x)):
        if ec.get(t, 0) != ac.get(t, 0):
            diffs.append('期待×%d 实际×%d → %s' % (ec.get(t, 0), ac.get(t, 0), t))
    pairs = []
    if not diffs:
        pool = defaultdict(int)
        for r in expected:
            pool[tup(r)] += 1
        for r in actual:
            t = tup(r)
            pairs.append((r, dict(zip(val_fields, t))))
    return (len(diffs) == 0), diffs, pairs


# ────────────────────────── 成员解析（吸收链） ──────────────────────────
def sched_member_aufnrs(actual_row, base_by_aufnr, actual_surv):
    """实际排程行 → 其吸收的基线 aufnr 集合。
    合并行按 source_refs(文档号) 展开（限同物料）；未合并行 = 自身 aufnr。"""
    if actual_row.get('source_refs'):
        docs = set(x for x in actual_row['source_refs'].split('、') if x)
        return set(b['aufnr'] for b in base_by_aufnr.values()
                   if b['material_code'] == actual_row['material_code']
                   and (doc_ref_sched(b) in docs or b['material_code'] in docs))
    return {actual_row['aufnr']}


def short_test_members(actual_short, actual_surv, base_sched_by_aufnr, base_short):
    """实际缺料行 → (测试成员Σ, 测试日期列表, 测试红点, 解析是否成功)。
    吸收链：来源单据 → 实际存活排程行 → 其 source_refs 展开 → 基线排程行 → 基线缺料行。
    按来源类型消歧（WO 单据号可能与 SEMI 驱动单号同名，如 GD006）。"""
    refs = [x for x in (actual_short.get('source_refs') or '').split('、') if x]
    src_kinds = set((actual_short.get('source') or '').split(','))
    member_aufnrs = set()
    resolved_all = True
    if not refs:
        # 无缺料合并：驱动排程行 = 同 aufnr（按来源类型过滤防撞号）
        drv = [r for r in actual_surv
               if r['aufnr'] == actual_short['aufnr']
               and (('WO' if r['source'] == 'WO' else 'SCH') in src_kinds)]
        if drv:
            for d in drv:
                member_aufnrs |= sched_member_aufnrs(d, base_sched_by_aufnr, actual_surv)
        else:
            resolved_all = False
    else:
        for ref in refs:
            hits = []
            for r in actual_surv:
                if r['source'] == 'WO':
                    if 'WO' in src_kinds and (r.get('order_no') or '') == ref:
                        hits.append(r)
                else:
                    if 'SCH' in src_kinds and (doc_ref_sched(r) == ref
                                               or r['material_code'] == ref):
                        hits.append(r)
            if not hits:
                resolved_all = False
                continue
            for h in hits:
                member_aufnrs |= sched_member_aufnrs(h, base_sched_by_aufnr, actual_surv)
    parents = set(x for x in (actual_short.get('parent_code') or '').split('、') if x)
    members = [b for b in base_short
               if b['component_code'] == actual_short['component_code']
               and b['aufnr'] in member_aufnrs
               and (not parents or b['material_code'] in parents)]
    if not members:
        return None, [], 0, False
    return (r3(sum(b['required_qty'] for b in members)),
            sorted(set(b['component_demand_date'] for b in members)),
            1 if any(b['delayed'] == 1 for b in members) else 0,
            resolved_all)


# ────────────────────────── ready_status 核查（信息披露，不进严格判定） ──────────────────────────
def ready_check(exp_short, exp_sched, actual_sched, md):
    rs = derive_ready_status(exp_short, exp_sched, md)
    amap = {r['aufnr']: r for r in actual_sched}
    out = []
    for r in exp_sched:
        if r['is_deleted'] != 0:
            continue
        st_, desc_, judged = rs.get(r['aufnr'], ('', '', False))
        a = amap.get(r['aufnr'])
        av = (a or {}).get('ready_status', '')
        out.append({'aufnr': r['aufnr'], 'material_code': r['material_code'],
                    '期待': st_ or '—', '实际': av or '—',
                    '判定': ('一致' if (judged and av == st_) else
                             ('不一致' if judged else '不判定（保留齐备套值）'))})
    judged_n = sum(1 for x in out if x['判定'] in ('一致', '不一致'))
    match_n = sum(1 for x in out if x['判定'] == '一致')
    return {'rows': out, 'judged': judged_n, 'match': match_n,
            'note': ('缺料合并后仅锚点行带 schedule_id，非锚点行保留齐备套计算值，不判定'
                     if any(x['判定'].startswith('不判定') for x in out) else '全部可推导')}


# ────────────────────────── 主流程 ──────────────────────────
def main():
    report = {'meta': {'today': T10, 'today8': T8, 'merge_n': MERGE_N, 'runs': RUNS,
                       'api': API, 'generated_at': datetime.now().strftime('%Y-%m-%d %H:%M:%S')}}
    st = load_static()

    # ── 阶段 0：全 OFF 建立规范基线 ──
    print('[0] 复位配置并建立规范基线（全 OFF）…')
    set_cfg('module_plan_req_date', 0)
    set_cfg('schedule_merge_days', 0)
    set_cfg('merge_days', 0)
    r = calc({'limit': 100, 'enabled_stock_types': ['unrestricted'],
              'schedule_merge_days': 0, 'merge_days': 0})
    assert r.get('success'), '基线计算失败: %s' % r
    base_sched = snap_sched()
    base_short = snap_short()
    print('    基线：排程 %d 行（存活 %d）/ 缺料 %d 行'
          % (len(base_sched), sum(1 for x in base_sched if x['is_deleted'] == 0), len(base_short)))

    # ── 阶段 1：预言机自检 ──
    print('[1] 预言机自检（oracle(基线, OFF, 0) 必须逐行复现基线）…')
    warn = []
    exp_sched, _ = oracle_schedule(base_sched, st, False, 0, warn)
    exp_short = oracle_shortage(exp_sched, st, False, 0, warn)
    ok_s, diffs_s = cmp_by_key(exp_sched, base_sched, 'aufnr', SCHED_VAL)
    ok_h, diffs_h, _ = cmp_multiset(exp_short, base_short, SHORT_VAL)
    rc = ready_check(exp_short, exp_sched, base_sched, 0)
    selfcheck = {'ok': bool(ok_s and ok_h and not warn),
                 'sched_ok': ok_s, 'short_ok': ok_h, 'warnings': warn,
                 'ready': rc,
                 'sched_diffs': diffs_s[:50], 'short_diffs': diffs_h[:50]}
    report['selfcheck'] = selfcheck
    if not selfcheck['ok']:
        print('    ✗ 自检失败：排程差异 %d 条 / 缺料差异 %d 条 / 警告 %d 条'
              % (len(diffs_s), len(diffs_h), len(warn)))
        for d in (diffs_s + diffs_h)[:30]:
            print('      -', d)
        for w in warn[:10]:
            print('      !', w)
        report['aborted'] = True
        with open('_delay_matrix_result.json', 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        print('已中止：未出具报告。')
        return 1
    print('    ✓ 自检通过（排程 %d 行 / 缺料 %d 行逐字段复现；ready_status 可推导 %d 行、一致 %d 行）'
          % (len(exp_sched), len(exp_short), rc['judged'], rc['match']))

    # ── 金丝雀：篡改一行必须报 FAIL ──
    canary_actual = [dict(r) for r in base_sched]
    for r in canary_actual:
        if r['aufnr'] == '0009':
            r['batch_qty'] = r['batch_qty'] + 1
            break
    ok_c, _ = cmp_by_key(base_sched, canary_actual, 'aufnr', SCHED_VAL)
    canary_ok = (ok_c is False)
    short_canary = [dict(r) for r in base_short]
    short_canary[0]['required_qty'] = r3(short_canary[0]['required_qty'] + 1)
    ok_c2, _, _ = cmp_multiset(base_short, short_canary, SHORT_VAL)
    canary_ok = canary_ok and (ok_c2 is False)
    report['canary'] = {'ok': canary_ok,
                        'note': '把实际快照各篡改 1 行（排程 batch_qty +1、缺料 required_qty +1），比对器均报 FAIL'}
    print('[1b] 金丝雀自检：%s' % ('✓ 通过（比对器能发现差异）' if canary_ok else '✗ 失败'))
    if not canary_ok:
        report['aborted'] = True
        with open('_delay_matrix_result.json', 'w', encoding='utf-8') as f:
            json.dump(report, f, ensure_ascii=False, indent=1)
        return 1

    base_sched_by_aufnr = {r['aufnr']: r for r in base_sched}
    report['scenarios'] = []
    prev_a3 = None

    # ── 阶段 2：四场景 ──
    for sid, sname, body, switch_on in SCENARIOS:
        print('[2] 场景 %s：%s' % (sid, sname))
        set_cfg('module_plan_req_date', 1 if switch_on else 0)
        # 预快照：验证场景间无外部干扰。
        # S1：计算前排程须==基线（排程表每次全表重建，OFF 态即基线）；
        # S2~S4：计算前排程 == 上一场景末次快照（合并/软删态会保留到下次计算才重建）。
        pre_sched, pre_short = snap_sched(), snap_short()
        if prev_a3 is None:
            pre_ok = (_biz(pre_sched) == _biz(base_sched))
            pre_note = '计算前排程==OFF 基线（ readiness 重建确定性）'
        else:
            pre_ok = (_biz(pre_sched) == _biz(prev_a3['sched'])
                      and _biz(pre_short) == _biz(prev_a3['short']))
            pre_note = '计算前状态==上一场景末次快照（场景间无外部干扰）'
        if not pre_ok:
            print('    ✗ %s 不成立（排程 %d vs %d 行）'
                  % (pre_note, len(pre_sched), len(base_sched) if prev_a3 is None
                     else len(prev_a3['sched'])))
        runs = []
        for i in range(RUNS):
            rr = calc(body)
            assert rr.get('success'), '%s 第%d次计算失败: %s' % (sid, i + 1, rr)
            runs.append({'sched': snap_sched(), 'short': snap_short(),
                         'delay': rr.get('delay')})
        a1, a2, a3 = runs[0], runs[1], runs[2]
        idem = (_biz(a1['sched']) == _biz(a2['sched']) == _biz(a3['sched'])
                and _biz(a1['short']) == _biz(a2['short']) == _biz(a3['short']))
        print('    幂等（×%d 逐字段一致）：%s' % (RUNS, '✓' if idem else '✗'))

        warn2 = []
        exp_sched, members = oracle_schedule(base_sched, st, switch_on,
                                             int(body['schedule_merge_days']), warn2)
        exp_short = oracle_shortage(exp_sched, st, switch_on, int(body['merge_days']), warn2)
        rc2 = ready_check(exp_short, exp_sched, a1['sched'], int(body['merge_days']))

        ok_s, diffs_s = cmp_by_key(exp_sched, a1['sched'], 'aufnr', SCHED_VAL)
        ok_h, diffs_h, pairs = cmp_multiset(exp_short, a1['short'], SHORT_VAL)
        print('    实际==期待：排程 %s（差异 %d）/ 缺料 %s（差异 %d）'
              % ('✓' if ok_s else '✗', len(diffs_s), '✓' if ok_h else '✗', len(diffs_h)))
        for d in (diffs_s + diffs_h)[:20]:
            print('      -', d)

        # ── 行级报告：排程 ──
        actual_by_aufnr = {r['aufnr']: r for r in a1['sched']}
        actual_surv = [r for r in a1['sched'] if r['is_deleted'] == 0]
        sched_rows = []
        n_qty_diff = n_date_diff = n_qty_unexplained = n_date_unexplained = 0
        for b in base_sched:
            aufnr = b['aufnr']
            e = next((x for x in exp_sched if x['aufnr'] == aufnr), None)
            a = actual_by_aufnr.get(aufnr)
            mem = members.get(aufnr, [aufnr])
            mem_rows = [base_sched_by_aufnr[m] for m in mem]
            test_qty = r3(sum(x['batch_qty'] for x in mem_rows))
            test_dates = sorted(set((x['batch_date_start'], x['batch_date_end']) for x in mem_rows))
            test_delayed = 1 if any(x['delayed'] == 1 for x in mem_rows) else 0
            exp_del = (e or {}).get('delayed', 0)
            exp_deleted = (e or {}).get('is_deleted', 0)
            act_deleted = (a or {}).get('is_deleted', 0)
            if a is None or e is None:
                verdict = 'FAIL'
                reason = '期待或实际缺失'
            elif act_deleted == 1:
                verdict = 'PASS'
                reason = '已按规则合并入锚点 %s（成员Σ=%s）' % (
                    next((x['aufnr'] for x in exp_sched
                          if x['is_deleted'] == 0 and aufnr in members.get(x['aufnr'], [])), '?'), test_qty)
            else:
                qty_same = abs(test_qty - a['batch_qty']) <= 0.0011
                tdate = (b['batch_date_start'], b['batch_date_end'])
                adate = (a['batch_date_start'], a['batch_date_end'])
                date_same = (tdate == adate)
                if not qty_same:
                    n_qty_diff += 1
                if not date_same:
                    n_date_diff += 1
                # 归因：期待值必须来自成员规则推导且实际==期待
                explained = (abs(test_qty - (e['batch_qty'])) <= 0.0011
                             and (e['batch_date_start'], e['batch_date_end']) == adate)
                if not qty_same and not explained:
                    n_qty_unexplained += 1
                if not date_same and not explained:
                    n_date_unexplained += 1
                if explained:
                    verdict = 'PASS'
                    why = []
                    if not qty_same:
                        why.append('数量差=合并（成员Σ→锚点）')
                    if not date_same:
                        why.append('日期差=归位/合并锚点规则')
                    reason = '；'.join(why) or '测试=实际=期待'
                else:
                    verdict = 'FAIL'
                    reason = '差异不可归因：测试%s 期待%s 实际%s' % (test_qty, e['batch_qty'], a['batch_qty'])
            sched_rows.append({
                'aufnr': aufnr, 'material_code': b['material_code'], 'source': b['source'],
                '测试数量': test_qty, '测试开工': b['batch_date_start'], '测试完工': b['batch_date_end'],
                '测试红点': test_delayed,
                '期待数量': r3((e or {}).get('batch_qty', 0)) if e else None,
                '期待开工': (e or {}).get('batch_date_start', ''),
                '期待完工': (e or {}).get('batch_date_end', ''),
                '期待红点': exp_del, '期待状态': '合并删除' if exp_deleted else '存活',
                '实际数量': r3(a['batch_qty']) if a else None,
                '实际开工': (a or {}).get('batch_date_start', ''),
                '实际完工': (a or {}).get('batch_date_end', ''),
                '实际红点': (a or {}).get('delayed', 0),
                '实际状态': '合并删除' if act_deleted else ('存活' if a else '缺失'),
                '数量一样': ('—' if (a is None or act_deleted == 1)
                            else ('是' if abs(test_qty - a['batch_qty']) <= 0.0011 else '否')),
                '日期一样': ('—' if (a is None or act_deleted == 1)
                            else ('是' if (b['batch_date_start'], b['batch_date_end']) ==
                                  (a['batch_date_start'], a['batch_date_end']) else '否')),
                '测试成员': '、'.join(mem), '归因': reason, '结论': verdict,
            })
        # 实际多出的行（期待没有的 aufnr）
        exp_aufnrs = set(x['aufnr'] for x in exp_sched)
        for a in a1['sched']:
            if a['aufnr'] not in exp_aufnrs:
                sched_rows.append({'aufnr': a['aufnr'], 'material_code': a['material_code'],
                                   'source': a['source'], '结论': 'FAIL',
                                   '归因': '实际多出（期待无此 aufnr）'})

        # ── 行级报告：缺料 ──
        short_rows = []
        sq_diff = sd_diff = sq_unexp = sd_unexp = 0
        for a, e in pairs:
            tq, tdates, tdel, resolved = short_test_members(a, actual_surv, base_sched_by_aufnr, base_short)
            adate = a['component_demand_date']
            qty_same = (tq is not None and abs(tq - a['required_qty']) <= 0.0011)
            date_same = (tdates == [adate])
            if not qty_same:
                sq_diff += 1
            if not date_same:
                sd_diff += 1
            exp_qty = e['required_qty']
            explained = (abs(exp_qty - a['required_qty']) <= 0.0011
                         and e['component_demand_date'] == adate
                         and tq is not None and abs(tq - exp_qty) <= 0.0011)
            if not qty_same and not explained:
                sq_unexp += 1
            if not date_same and not explained:
                sd_unexp += 1
            why = []
            if not qty_same and explained:
                why.append('数量差=缺料合并（成员Σ→锚点）')
            if not date_same and explained:
                why.append('日期差=规则2归位/合并锚点')
            short_rows.append({
                'component_code': a['component_code'], 'material_code': a['material_code'],
                'source': a['source'], 'aufnr': a['aufnr'],
                '测试数量': tq, '测试日期': '、'.join(tdates) if tdates else '—', '测试红点': tdel,
                '期待数量': exp_qty, '期待日期': e['component_demand_date'],
                '期待红点': e['delayed'], '期待类型': e['shortage_type'],
                '实际数量': a['required_qty'], '实际日期': adate, '实际红点': a['delayed'],
                '实际类型': a['shortage_type'], '实际可用量': a['available_qty'],
                '数量一样': '是' if qty_same else '否', '日期一样': '是' if date_same else '否',
                '成员解析': '成功' if resolved else '部分失败',
                '归因': '；'.join(why) or '测试=实际=期待',
                '结论': 'PASS' if explained else 'FAIL',
            })
        # 期待/实际多元组配对：剩余未配对的期待行与实际行都算 FAIL（防漏报）
        from collections import Counter

        def _t(r):
            return tuple(r.get(f) for f in SHORT_VAL)
        exp_counter = Counter(_t(r) for r in exp_short)
        for a in a1['short']:
            t = _t(a)
            if exp_counter.get(t, 0) > 0:
                exp_counter[t] -= 1
            else:
                short_rows.append({'component_code': a['component_code'], '结论': 'FAIL',
                                   '归因': '实际多出（期待无对应行）', '实际数量': a['required_qty'],
                                   '实际日期': a['component_demand_date']})
        for t, c in exp_counter.items():
            if c > 0:
                d = dict(zip(SHORT_VAL, t))
                short_rows.append({'component_code': d.get('component_code'), '结论': 'FAIL',
                                   '归因': '期待有、实际缺失 ×%d' % c,
                                   '期待数量': d.get('required_qty'),
                                   '期待日期': d.get('component_demand_date')})

        # ── 守恒 ──
        cons_sched = {}
        for dim in ('material_code', 'source'):
            tb = defaultdict(float)
            ta = defaultdict(float)
            for r in base_sched:
                if r['is_deleted'] == 0:
                    tb[(r[dim],)] += r['batch_qty']
            for r in a1['sched']:
                if r['is_deleted'] == 0:
                    ta[(r[dim],)] += r['batch_qty']
            bad = [k for k in set(tb) | set(ta) if abs(tb.get(k, 0) - ta.get(k, 0)) > 0.0011]
            cons_sched[dim] = {'ok': not bad, 'bad': bad}
        tb = defaultdict(float)
        ta = defaultdict(float)
        for r in base_short:
            tb[r['component_code']] += r['required_qty']
        for r in a1['short']:
            ta[r['component_code']] += r['required_qty']
        bad = [k for k in set(tb) | set(ta) if abs(tb.get(k, 0) - ta.get(k, 0)) > 0.0011]
        cons_short = {'ok': not bad, 'bad': bad,
                      'detail': {k: [r3(tb.get(k, 0)), r3(ta.get(k, 0))]
                                 for k in sorted(set(tb) | set(ta))}}

        sched_fail = sum(1 for r in sched_rows if r['结论'] != 'PASS')
        short_fail = sum(1 for r in short_rows if r['结论'] != 'PASS')
        verdict = ('PASS' if (pre_ok and idem and ok_s and ok_h and not warn2
                              and sched_fail == 0 and short_fail == 0
                              and cons_sched['material_code']['ok'] and cons_sched['source']['ok']
                              and cons_short['ok']
                              and n_qty_unexplained == 0 and n_date_unexplained == 0
                              and sq_unexp == 0 and sd_unexp == 0) else 'FAIL')
        print('    结论：%s（排程 FAIL %d / 缺料 FAIL %d；数量不一样 %d（不可归因 %d）/ 日期不一样 %d（不可归因 %d））'
              % (verdict, sched_fail, short_fail, n_qty_diff, n_qty_unexplained,
                 n_date_diff, n_date_unexplained))
        report['scenarios'].append({
            'id': sid, 'name': sname, 'params': body,
            'switch_on': switch_on, 'pre_equals_baseline': pre_ok, 'idempotent': idem,
            'oracle_warnings': warn2,
            'sched': {'ok': ok_s, 'diffs': diffs_s[:50], 'rows': sched_rows,
                      'qty_diff': n_qty_diff, 'date_diff': n_date_diff,
                      'qty_unexplained': n_qty_unexplained, 'date_unexplained': n_date_unexplained},
            'short': {'ok': ok_h, 'diffs': diffs_h[:50], 'rows': short_rows,
                      'qty_diff': sq_diff, 'date_diff': sd_diff,
                      'qty_unexplained': sq_unexp, 'date_unexplained': sd_unexp},
            'conservation': {'sched': cons_sched, 'short': cons_short},
            'ready_status_check': rc2,
            'delay_stat': a1['delay'],
            'pre_note': pre_note,
            'counts': {'sched_actual': len(a1['sched']), 'sched_alive': len(actual_surv),
                       'short_actual': len(a1['short']),
                       'sched_base': sum(1 for x in base_sched if x['is_deleted'] == 0),
                       'short_base': len(base_short)},
            'verdict': verdict,
        })
        prev_a3 = a3

    # ── 阶段 3：复位 ──
    print('[3] 复位配置（全 OFF）并复算基线…')
    set_cfg('module_plan_req_date', 0)
    set_cfg('schedule_merge_days', 0)
    set_cfg('merge_days', 0)
    r = calc({'limit': 100, 'enabled_stock_types': ['unrestricted'],
              'schedule_merge_days': 0, 'merge_days': 0})
    restore_ok = (r.get('success') and _biz(snap_sched()) == _biz(base_sched)
                  and _biz(snap_short()) == _biz(base_short))
    report['restore'] = {'ok': restore_ok}
    print('    库态恢复基线：%s' % ('✓' if restore_ok else '✗'))

    overall = ('PASS' if all(s['verdict'] == 'PASS' for s in report['scenarios'])
               and report['selfcheck']['ok'] and report['canary']['ok'] and restore_ok else 'FAIL')
    report['overall'] = overall
    with open('_delay_matrix_result.json', 'w', encoding='utf-8') as f:
        json.dump(report, f, ensure_ascii=False, indent=1)
    print('\n总体结论：%s → _delay_matrix_result.json' % overall)
    return 0 if overall == 'PASS' else 2


if __name__ == '__main__':
    sys.exit(main())
