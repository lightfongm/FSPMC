# -*- coding: utf-8 -*-
"""关开关回归：开关 OFF 时代码路径必须完全 inert（不归位、不强制合并、不写 delayed），
开关 ON 时恢复与基线一致。"""
import sys, json, urllib.request, uuid
sys.path.insert(0, '.')
import backend.routes.schedule as S

CALC_URL = 'http://127.0.0.1:5000/api/peacock/calculate'
CFG = 'module_plan_req_date'

def set_switch(on):
    S.execute_db("UPDATE system_config SET config_value=%s WHERE config_key=%s",
                 ('1' if on else '0', CFG))
    v = S.get_config(CFG, '0')
    print('  [config] %s = %s' % (CFG, v))
    return v == ('1' if on else '0')

def calc():
    data = json.dumps({'schedule_merge_days': 0, 'merge_days': 0,
                       'enabled_stock_types': ['unrestricted']}).encode()
    req = urllib.request.Request(CALC_URL, data=data,
                                 headers={'Content-Type': 'application/json'}, method='POST')
    return json.loads(urllib.request.urlopen(req, timeout=180).read())

def snap():
    sch = S.query_db("SELECT COUNT(*) c FROM peacock_schedule WHERE is_deleted=0", one=True)['c']
    sh = S.query_db("SELECT COUNT(*) c FROM peacock_material_shortage", one=True)['c']
    sd = S.query_db("SELECT COUNT(*) c FROM peacock_schedule WHERE is_deleted=0 AND `delayed`=1", one=True)['c']
    shd = S.query_db("SELECT COUNT(*) c FROM peacock_material_shortage WHERE `delayed`=1", one=True)['c']
    today8 = S._biz_today().strftime('%Y%m%d')
    overdue_sch = S.query_db(
        "SELECT COUNT(*) c FROM peacock_schedule WHERE is_deleted=0 AND batch_date_start IS NOT NULL AND batch_date_start < %s",
        (today8,), one=True)['c']
    return (sch, sh, sd, shd, overdue_sch)

def find_zz(vbeln):
    rows = S.query_db(
        "SELECT id, order_no, batch_date_start, batch_date_end, plan_qty, `delayed` "
        "FROM peacock_schedule WHERE order_no=%s AND is_deleted=0", (vbeln,))
    return rows

results = []
def check(name, cond, detail=''):
    results.append((name, cond, detail))
    print(('  [PASS] ' if cond else '  [FAIL] ') + name + ('  ' + detail if detail else ''))

print('=== 一、全流水线快照比对（ON -> OFF -> ON） ===')
# 基线 A（开关 ON）
assert set_switch(True)
calc()
A = snap()
print('  基线 A (ON)  =', A)

# OFF
assert set_switch(False)
calc()
B = snap()
print('  快照 B (OFF) =', B)
check('OFF: 排程 delayed 计数 = 0', B[2] == 0, 'delayed_sch=%d' % B[2])
check('OFF: 缺料 delayed 计数 = 0', B[3] == 0, 'delayed_sh=%d' % B[3])
check('OFF: 排程行数稳定', B[0] == A[0], '%d vs %d' % (B[0], A[0]))
check('OFF: 缺料行数稳定', B[1] == A[1], '%d vs %d' % (B[1], A[1]))

# ON 还原
assert set_switch(True)
calc()
C = snap()
print('  快照 C (ON)  =', C)
check('ON: 还原与基线 A 完全一致', C == A, '%s vs %s' % (C, A))

print('\n=== 二、受控注入：过去日销售单，确定性证明 OFF 不归位 / ON 归位 ===')
VB = 'ZZTESTSO'
MAT = None
# 取一个现有有 BOM 的物料（优先 C002）
try:
    MAT = 'C002'
    ex = S.query_db("SELECT id FROM peacock_schedule WHERE material_code=%s AND is_deleted=0 LIMIT 1", (MAT,), one=True)
    if not ex:
        MAT = S.query_db("SELECT material_code FROM peacock_schedule WHERE is_deleted=0 LIMIT 1", one=True)['material_code']
except Exception as e:
    print('  [warn] 取物料失败:', e)
print('  注入物料 =', MAT)

today8 = S._biz_today().strftime('%Y%m%d')
PAST = '20200101'

def inject_so():
    S.execute_db("DELETE FROM sap_sales_orders WHERE vbeln=%s", (VB,))
    S.execute_db("DELETE FROM sap_sales_order_items WHERE vbeln=%s", (VB,))
    soid = uuid.uuid4().hex
    S.execute_db(
        "INSERT INTO sap_sales_orders (id, vbeln, erdat, kunnr, name1, status, item_count, data_source) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s,%s)",
        (soid, VB, today8, '001', '01', '启用', 1, 'manual'))
    S.execute_db(
        "INSERT INTO sap_sales_order_items (id, vbeln, posnr, matnr, vdatu, kwmeng, lfimg) "
        "VALUES (%s,%s,%s,%s,%s,%s,%s)",
        (uuid.uuid4().hex, VB, '10', MAT, PAST, 100, 0))

def cleanup_so():
    S.execute_db("DELETE FROM sap_sales_order_items WHERE vbeln=%s", (VB,))
    S.execute_db("DELETE FROM sap_sales_orders WHERE vbeln=%s", (VB,))

try:
    # OFF 注入
    inject_so()
    assert set_switch(False)
    calc()
    rows_off = find_zz(VB)
    check('OFF: ZZTESTSO 生成 SCH 行', len(rows_off) >= 1, 'rows=%d' % len(rows_off))
    if rows_off:
        r = rows_off[0]
        check('OFF: 不归位（batch_date_start 仍为过去日）',
              str(r['batch_date_start']) == PAST, 'start=%s' % r['batch_date_start'])
        check('OFF: delayed = 0', int(r['delayed'] or 0) == 0, 'delayed=%s' % r['delayed'])
    # ON 注入
    inject_so()
    assert set_switch(True)
    calc()
    rows_on = find_zz(VB)
    check('ON: ZZTESTSO 生成 SCH 行', len(rows_on) >= 1, 'rows=%d' % len(rows_on))
    if rows_on:
        r = rows_on[0]
        check('ON: 归位到今日', str(r['batch_date_start']) == today8, 'start=%s' % r['batch_date_start'])
        check('ON: delayed = 1', int(r['delayed'] or 0) == 1, 'delayed=%s' % r['delayed'])
    # 缺料侧 规则2（仅当该物料有 BOM 子件）
    sh_rows = S.query_db(
        "SELECT id, material_code, component_demand_date, `delayed` FROM peacock_material_shortage "
        "WHERE source_refs LIKE %s OR order_no=%s", ('%' + VB + '%', VB))
    if sh_rows:
        all_today = all(str(x['component_demand_date']) == S._biz_today().strftime('%Y-%m-%d') for x in sh_rows)
        any_delay = any(int(x['delayed'] or 0) == 1 for x in sh_rows)
        check('ON: 缺料子件需求日期归位到今日', all_today, 'n=%d' % len(sh_rows))
        check('ON: 缺料 delayed = 1', any_delay, 'n=%d' % len(sh_rows))
    else:
        print('  [info] 该物料无 BOM 子件缺料行，跳过规则2 校验')
finally:
    cleanup_so()

# 最终还原到 ON 基线
assert set_switch(True)
calc()
final = snap()
check('最终: 还原到 ON 基线 A', final == A, '%s vs %s' % (final, A))

print('\n=== 汇总 ===')
nfail = sum(1 for _, c, _ in results if not c)
for name, c, d in results:
    print(('  PASS ' if c else '  FAIL ') + name + ('  ' + d if d else ''))
print('\n结果: %d 项, 失败 %d 项' % (len(results), nfail))
sys.exit(1 if nfail else 0)
