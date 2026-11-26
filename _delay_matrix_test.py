# -*- coding: utf-8 -*-
"""一键计算 四场景矩阵验证（×3 幂等）+ Excel 报告。
场景：S1 无参 / S2 仅计划需求日期 / S3 仅排程合并天数 / S4 两者都开。
只读业务数据做断言；改 system_config 两个键并在结束后复位。
"""
import os, json, time, urllib.request, datetime
from collections import defaultdict
import pymysql

BASE = 'http://127.0.0.1:5000'
DBPWD = os.environ.get('DB_PWD', '')
MERGE_N = 30  # 排程合并天数（取足够大以确保同物料跨日折叠可见）

TZ8 = datetime.timezone(datetime.timedelta(hours=8))
TODAY = datetime.datetime.now(TZ8).date()
T8 = TODAY.strftime('%Y%m%d')
T10 = TODAY.strftime('%Y-%m-%d')

# ---------------- HTTP ----------------
def post(path, payload=None, timeout=300):
    body = json.dumps(payload or {}).encode()
    req = urllib.request.Request(BASE + path, data=body, headers={'Content-Type': 'application/json'})
    try:
        r = urllib.request.urlopen(req, timeout=timeout)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read())
        except Exception:
            return e.code, {}

def db():
    return pymysql.connect(host='127.0.0.1', port=3306, user='root', password=DBPWD,
                           database='schedule_system', charset='utf8mb4')

def set_cfg(k, v):
    c = db(); cur = c.cursor()
    cur.execute('REPLACE INTO system_config(config_key,config_value) VALUES(%s,%s)', (k, v))
    c.commit(); c.close()

def get_cfg(k):
    c = db(); cur = c.cursor()
    cur.execute('SELECT config_value FROM system_config WHERE config_key=%s', (k,))
    row = cur.fetchone(); c.close()
    return (row[0] if row else None)

# ---------------- 日期解析 ----------------
def pdate(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    if len(s) >= 8:  # 紧凑 8 位 YYYYMMDD
        try:
            return datetime.datetime.strptime(s[:8], '%Y%m%d').date()
        except Exception:
            pass
    try:  # ISO YYYY-MM-DD（用原始串前 10 位，勿用已截断串）
        return datetime.datetime.strptime(s[:10], '%Y-%m-%d').date()
    except Exception:
        return None

# ---------------- 快照 ----------------
def snap_schedule():
    c = db(); cur = c.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT aufnr, source, material_code, material_name, plan_qty, batch_qty, "
                "batch_date_start, batch_date_end, `delayed`, source_refs, work_order_id, is_deleted "
                "FROM peacock_schedule")
    rows = [dict(r) for r in cur.fetchall()]; c.close()
    live = [r for r in rows if int(r.get('is_deleted') or 0) == 0]
    return live, rows

def snap_shortage():
    c = db(); cur = c.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT component_code, component_name, parent_code, source, source_refs, "
                "required_qty, need_qty, component_demand_date, `delayed`, parent_qty "
                "FROM peacock_material_shortage")
    rows = [dict(r) for r in cur.fetchall()]; c.close()
    return rows

def snap_wo():
    c = db(); cur = c.cursor(pymysql.cursors.DictCursor)
    cur.execute("SELECT order_no, material_code, planned_start, planned_end, confirm_qty, status, `delayed` "
                "FROM work_orders WHERE status IN ('批准','执行') ORDER BY order_no")
    rows = [dict(r) for r in cur.fetchall()]; c.close()
    return rows

def norm_sched(rows):
    out = []
    for r in rows:
        out.append((str(r['material_code']), str(r['source'] or ''), str(r['aufnr']),
                    str(r['batch_date_start'] or ''), str(r['batch_date_end'] or ''),
                    round(float(r['batch_qty'] or 0), 4), round(float(r['plan_qty'] or 0), 4),
                    int(r['delayed'] or 0), str(r['source_refs'] or '')))
    return sorted(out)

def norm_short(rows):
    out = []
    for r in rows:
        out.append((str(r['component_code']), str(r['source'] or ''), str(r['source_refs'] or ''),
                    str(r['component_demand_date'] or ''), round(float(r['required_qty'] or 0), 4),
                    round(float(r['need_qty'] or 0), 4), int(r['delayed'] or 0),
                    str(r['parent_code'] or '')))
    return sorted(out)

# ---------------- 场景执行 ----------------
def run_calc(plan_req, smd):
    body = {'limit': 100, 'enabled_stock_types': ['unrestricted'],
            'schedule_merge_days': smd, 'merge_days': 0}
    return post('/api/peacock/calculate', body)

SCEN = [
    ('S1', '无参（计划需求日期 OFF + 排程合并 0）', 0, 0),
    ('S2', '仅计划需求日期 ON', 1, 0),
    ('S3', '仅排程合并天数 ON(%d)' % MERGE_N, 0, MERGE_N),
    ('S4', '计划需求日期 ON + 排程合并天数 %d' % MERGE_N, 1, MERGE_N),
]

def capture_scenario(code, plan_req, smd):
    set_cfg('module_plan_req_date', '1' if plan_req else '0')
    runs = []
    for i in range(3):
        st, d = run_calc(plan_req, smd)
        sched_live, sched_all = snap_schedule()
        short = snap_shortage()
        runs.append({'status': st, 'resp': d, 'sched': sched_live,
                     'sched_norm': norm_sched(sched_live), 'short': short,
                     'short_norm': norm_short(short)})
    return runs

# ---------------- 不变量 ----------------
def checks_for(code, desc, plan_req, smd, runs, baseline_sched, baseline_short, wo):
    """返回 check dict 列表。"""
    res = []
    final = runs[-1]
    sched = final['sched']; short = final['short']

    # G1 接口成功
    ok = (final['status'] == 200 and isinstance(final['resp'], dict) and final['resp'].get('success'))
    res.append(('G1', '接口', '一键计算 success=True 且 200', 'success=True', 'status=%s success=%s' % (final['status'], (final['resp'] or {}).get('success')), 'PASS' if ok else 'FAIL', 'delay=%s' % (final['resp'] or {}).get('delay')))

    # V6 幂等（排程 + 缺料，3 次完全一致）
    idem_s = all(r['sched_norm'] == runs[0]['sched_norm'] for r in runs)
    idem_sh = all(r['short_norm'] == runs[0]['short_norm'] for r in runs)
    res.append(('V6a', '幂等', '排程结果 3 次完全一致', 'A1==A2==A3', '一致' if idem_s else '不一致', 'PASS' if idem_s else 'FAIL', '行数=%d' % len(sched)))
    res.append(('V6b', '幂等', '缺料明细 3 次完全一致', 'A1==A2==A3', '一致' if idem_sh else '不一致', 'PASS' if idem_sh else 'FAIL', '行数=%d' % len(short)))

    # V1 plan_qty≡batch_qty
    bad = [r for r in sched if round(float(r['plan_qty'] or 0), 6) != round(float(r['batch_qty'] or 0), 6)]
    res.append(('V1', '排程', '铁律 plan_qty≡batch_qty', '全部相等', '违例 %d 行' % len(bad), 'PASS' if not bad else 'FAIL', ', '.join('%s:%s' % (r['aufnr'], r['material_code']) for r in bad[:6])))

    # V2 WO 排程行 == work_orders（日期/数量），delayed=0
    wo_map = {w['order_no']: w for w in wo}
    wo_rows = [r for r in sched if (r['source'] or '') == 'WO']
    wo_bad = []
    for r in wo_rows:
        w = wo_map.get(r['aufnr'])
        if not w:
            wo_bad.append('%s 无对应工单' % r['aufnr']); continue
        exp_s = (str(w['planned_start'])[:10]).replace('-', '')
        exp_e = (str(w['planned_end'])[:10]).replace('-', '')
        got_s = str(r['batch_date_start'] or ''); got_e = str(r['batch_date_end'] or '')
        if exp_s != got_s or exp_e != got_e:
            wo_bad.append('%s 日期 %s/%s≠%s/%s' % (r['aufnr'], got_s, got_e, exp_s, exp_e))
        if int(r['delayed'] or 0) != 0:
            wo_bad.append('%s delayed=%s≠0' % (r['aufnr'], r['delayed']))
    res.append(('V2', '排程', 'WO 排程行=工单表(日期不变)+delayed=0', '逐单一致', '违例 %d' % len(wo_bad), 'PASS' if not wo_bad else 'FAIL', '；'.join(wo_bad[:6]) or 'WO 行 %d 张全部一致' % len(wo_rows)))

    non_wo = [r for r in sched if (r['source'] or '') != 'WO']

    # V4 ON：无非WO行 start<今日
    if plan_req:
        past = [r for r in non_wo if (pdate(r['batch_date_start']) and len(str(r['batch_date_start']).strip()) == 8 and pdate(r['batch_date_start']) < TODAY)]
        res.append(('V4', '排程', '开关ON：非WO过期行已被压到>=当日', 'start<今日 计=0', '仍过期 %d 行' % len(past), 'PASS' if not past else 'FAIL', ', '.join('%s/%s' % (r['aufnr'], r['material_code']) for r in past[:6])))
    else:
        # V3 OFF：非WO行 start<今日 ⇒ delayed=1（标记完备）
        overdue = [r for r in non_wo if (pdate(r['batch_date_start']) and len(str(r['batch_date_start']).strip()) == 8 and pdate(r['batch_date_start']) < TODAY)]
        unmarked = [r for r in overdue if int(r['delayed'] or 0) != 1]
        res.append(('V3', '排程', '开关OFF：非WO过期行均标 delayed=1', '过期⇒delayed=1', '过期%d/未标%d' % (len(overdue), len(unmarked)), 'PASS' if not unmarked else 'FAIL', '过期行 %d' % len(overdue)))

    # V5 合并守恒（S3/S4）：按物料 batch_qty 合计 == 基线
    if smd and smd > 0:
        base_by = defaultdict(float)
        for r in baseline_sched:
            if (r['source'] or '') in ('SCH', 'SEMI'):
                base_by[r['material_code']] += float(r['batch_qty'] or 0)
        act_by = defaultdict(float)
        for r in sched:
            if (r['source'] or '') in ('SCH', 'SEMI'):
                act_by[r['material_code']] += float(r['batch_qty'] or 0)
        diffs = [(m, round(base_by[m], 4), round(act_by.get(m, 0), 4)) for m in base_by if abs(base_by[m] - act_by.get(m, 0)) > 1e-4]
        extra = [m for m in act_by if m not in base_by]
        res.append(('V5', '排程', '合并守恒：按物料Σbatch_qty==基线', '各物料合计相等', '差异物料 %d' % len(diffs), 'PASS' if not diffs and not extra else 'FAIL', '；'.join('%s 基线%.1f实际%.1f' % d for d in diffs[:6]) or '各物料合计一致'))

    # ---- 缺料 ----
    # W1 delayed 完备：req_date<今日 ⇒ delayed=1
    def short_overdue(r):
        d = pdate(r['component_demand_date'])
        return d is not None and d < TODAY
    ov = [r for r in short if short_overdue(r)]
    un = [r for r in ov if int(r['delayed'] or 0) != 1]
    res.append(('W1', '缺料', '需求日<今日 ⇒ delayed=1', '过期⇒delayed=1', '过期%d/未标%d' % (len(ov), len(un)), 'PASS' if not un else 'FAIL', '过期行 %d' % len(ov)))

    if plan_req:
        # W3 ON：非WO缺料行 req_date 不得<当日；WO 缺料行保持基线日期
        nonwo_past = [r for r in short if (r['source'] or '') != 'WO' and short_overdue(r)]
        res.append(('W3', '缺料', '开关ON：非WO缺料需求日已压到当日', '非WO过期=0', '仍过期 %d' % len(nonwo_past), 'PASS' if not nonwo_past else 'FAIL', ', '.join(r['component_code'] for r in nonwo_past[:8])))
        # WO 缺料行保持基线（同 component+source+source_refs 的 req_date 与基线一致）
        bmap = {}
        for r in baseline_short:
            if (r['source'] or '') == 'WO':
                bmap[(r['component_code'], r['source_refs'])] = r['component_demand_date']
        wo_short = [r for r in short if (r['source'] or '') == 'WO']
        wo_bad2 = []
        for r in wo_short:
            key = (r['component_code'], r['source_refs'])
            if key in bmap and str(bmap[key]) != str(r['component_demand_date']):
                wo_bad2.append('%s %s→%s' % (r['component_code'], bmap[key], r['component_demand_date']))
        res.append(('W3b', '缺料', '开关ON：WO缺料需求日不被改写', 'WO日期=基线', '违例 %d' % len(wo_bad2), 'PASS' if not wo_bad2 else 'FAIL', 'WO缺料行 %d' % len(wo_short)))

    if not plan_req and not (smd and smd > 0):
        # W2 OFF 且无合并：缺料集合==基线
        same = (final['short_norm'] == norm_short(baseline_short))
        res.append(('W2', '缺料', '全OFF：缺料明细与基线一致(日期/数量不变)', '集合==基线', '一致' if same else '不一致', 'PASS' if same else 'FAIL', '行数 实际%d 基线%d' % (len(short), len(baseline_short))))

    return res

# ---------------- 主流程 ----------------
def main():
    set_cfg('module_plan_req_date', '0')
    set_cfg('schedule_merge_days', 0)
    # 基线（全 OFF）
    st, d = run_calc(0, 0)
    baseline_sched, _ = snap_schedule()
    baseline_short = snap_shortage()
    wo = snap_wo()
    summary = []
    detail = {}
    for code, desc, plan_req, smd in SCEN:
        runs = capture_scenario(code, plan_req, smd)
        chks = checks_for(code, desc, plan_req, smd, runs, baseline_sched, baseline_short, wo)
        for ch in chks:
            summary.append((code, desc) + ch)
        detail[code] = {'desc': desc, 'plan_req': plan_req, 'smd': smd,
                        'sched': runs[-1]['sched'], 'short': runs[-1]['short'],
                        'baseline_sched': baseline_sched, 'baseline_short': baseline_short}
    # 复位
    set_cfg('module_plan_req_date', '0')
    set_cfg('schedule_merge_days', 0)
    st, d = run_calc(0, 0)  # 归位到干净 OFF 态

    out = {'today': T10, 't8': T8, 'merge_n': MERGE_N, 'summary': summary, 'detail': detail,
           'wo': wo}
    with open(os.path.join(os.path.dirname(__file__), '_delay_matrix_result.json'), 'w', encoding='utf-8') as f:
        json.dump(out, f, ensure_ascii=False, default=str)
    # 控制台摘要
    print('TODAY', T10, 'MERGE_N', MERGE_N)
    fails = 0
    for code, desc, cid, cat, item, exp, act, verdict, note in summary:
        if verdict == 'FAIL':
            fails += 1
        print('[%s] %-4s %-5s %-38s 期待=%-18s 实际=%-18s %s | %s' % (verdict, code, cid, item[:38], exp[:18], act[:18], '', note[:40]))
    print('TOTAL checks=%d FAIL=%d' % (len(summary), fails))

if __name__ == '__main__':
    main()
