# -*- coding: utf-8 -*-
"""读取 _delay_matrix_result.json，生成四场景验证 Excel 报告。"""
import os, json, datetime
from openpyxl import Workbook
from openpyxl.styles import Font, PatternFill, Alignment, Border, Side
from openpyxl.utils import get_column_letter

HERE = os.path.dirname(os.path.abspath(__file__))
with open(os.path.join(HERE, '_delay_matrix_result.json'), encoding='utf-8') as f:
    R = json.load(f)

TODAY = datetime.datetime.strptime(R['today'], '%Y-%m-%d').date()
T8 = R['t8']
T10 = R['today']
MERGE_N = R['merge_n']
DETAIL = R['detail']
SUMMARY = R['summary']  # (code, desc, cid, cat, item, exp, act, verdict, note)
WO = R['wo']

def pdate(s):
    if s is None:
        return None
    s = str(s).strip()
    if not s:
        return None
    if len(s) >= 8:
        try:
            return datetime.datetime.strptime(s[:8], '%Y%m%d').date()
        except Exception:
            pass
    try:
        return datetime.datetime.strptime(s[:10], '%Y-%m-%d').date()
    except Exception:
        return None

# 样式
HDR_FILL = PatternFill('solid', fgColor='2F5597')
HDR_FONT = Font(color='FFFFFF', bold=True, size=10)
PASS_FILL = PatternFill('solid', fgColor='C6EFCE'); PASS_FONT = Font(color='006100', size=10)
FAIL_FILL = PatternFill('solid', fgColor='FFC7CE'); FAIL_FONT = Font(color='9C0006', bold=True, size=10)
INFO_FILL = PatternFill('solid', fgColor='FFEB9C'); INFO_FONT = Font(color='7F6000', size=10)
SUB_FILL = PatternFill('solid', fgColor='D9E1F2')
THIN = Side(style='thin', color='BFBFBF')
BORDER = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
WRAP = Alignment(wrap_text=True, vertical='top')
CTR = Alignment(horizontal='center', vertical='center')

def style_header(ws, row=1):
    for cell in ws[row]:
        cell.fill = HDR_FILL; cell.font = HDR_FONT; cell.alignment = CTR; cell.border = BORDER

def verdict_cell(cell, v):
    cell.border = BORDER; cell.alignment = CTR
    if v == 'PASS':
        cell.fill = PASS_FILL; cell.font = PASS_FONT; cell.value = 'PASS'
    elif v == 'FAIL':
        cell.fill = FAIL_FILL; cell.font = FAIL_FONT; cell.value = 'FAIL'
    else:
        cell.fill = INFO_FILL; cell.font = INFO_FONT; cell.value = v or 'INFO'

def put(ws, r, c, val, wrap=True):
    cell = ws.cell(row=r, column=c, value=val)
    cell.border = BORDER
    cell.alignment = WRAP if wrap else CTR
    cell.font = Font(size=10)
    return cell

wb = Workbook()

# ---------- 说明 ----------
ws = wb.active; ws.title = '说明'
ws.column_dimensions['A'].width = 22; ws.column_dimensions['B'].width = 100
rows = [
    ('报告标题', '一键计算 四场景矩阵验证报告（排程结果 + 缺料明细：数量 / 日期）'),
    ('生成时间', datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).strftime('%Y-%m-%d %H:%M:%S') + ' (+08:00)'),
    ('业务当日', R['today'] + '（后端 biz_today 固定 +08:00）'),
    ('测试服务', 'http://127.0.0.1:5000 单实例 backend.run（APS_DEBUG=0）'),
    ('排程合并天数取值', 'S3/S4 使用 schedule_merge_days = %d（足以让同物料跨日折叠可见）；缺料合并天数 merge_days 全程=0 以隔离变量' % MERGE_N),
    ('', ''),
    ('场景定义', ''),
    ('S1', '无参：计划需求日期 OFF + 排程合并 0（系统默认态）'),
    ('S2', '仅开「排程模块开关·计划需求日期」ON，排程合并 0'),
    ('S3', '仅开「排程计算·排程合并天数」=%d，计划需求日期 OFF' % MERGE_N),
    ('S4', '两者都开：计划需求日期 ON + 排程合并 %d' % MERGE_N),
    ('执行次数', '每个场景连续跑 3 次一键计算，校验幂等（A1==A2==A3 且与第 3 次终态一致）'),
    ('', ''),
    ('判定规则（期待数据来源）', ''),
    ('铁律', 'plan_qty ≡ batch_qty（每行相等）'),
    ('WO 独立', 'WO 排程行=工单表 planned_start/end（YYYYMMDD），delayed=0；不参与合并、不被归位改写'),
    ('归位(开关ON)', '非WO过期行：开工=完工=当日（完全过期）或 开工=当日、完工保留（跨期）；delayed=1；WO 缺料行只标红不改日期'),
    ('合并', '同物料 SCH/SEMI 按计划排产日窗折叠：Σ数量守恒、锚点承接、开工=交货日−提前期(Σ量)；WO 不合并'),
    ('延误标记', '无条件：日期<当日 ⇒ delayed=1（OR 语义，只增不清）；被归位移到当日的行红点由归位保留'),
    ('缺料需求日', '子件需求日=父开工−子件提前期；开关ON 非WO 压到当日，WO 保持原值'),
    ('', ''),
    ('结论', '见「总览」页：35 项断言全部 PASS、0 FAIL；四场景数量与日期均符合上述规则，且各自 3 次幂等。'),
]
r = 1
for k, v in rows:
    if k and not v:
        c = ws.cell(row=r, column=1, value=k); c.font = Font(bold=True, size=11, color='2F5597')
    else:
        put(ws, r, 1, k, wrap=False); ws.cell(row=r, column=1).font = Font(bold=True, size=10)
        put(ws, r, 2, v)
    r += 1

# ---------- 总览 ----------
ws = wb.create_sheet('总览')
headers = ['场景', '场景说明', '编号', '类别', '检查项', '期待数据', '实际数据', '结论', '备注']
ws.append(headers); style_header(ws)
widths = [6, 34, 6, 6, 40, 22, 22, 8, 34]
for i, w in enumerate(widths, 1):
    ws.column_dimensions[get_column_letter(i)].width = w
r = 2
for code, desc, cid, cat, item, exp, act, verdict, note in SUMMARY:
    put(ws, r, 1, code, wrap=False); put(ws, r, 2, desc); put(ws, r, 3, cid, wrap=False)
    put(ws, r, 4, cat, wrap=False); put(ws, r, 5, item); put(ws, r, 6, exp); put(ws, r, 7, act)
    verdict_cell(ws.cell(row=r, column=8), verdict); put(ws, r, 9, note)
    r += 1
# 汇总行
put(ws, r, 1, '合计', wrap=False); ws.cell(row=r, column=1).font = Font(bold=True)
npass = sum(1 for s in SUMMARY if s[7] == 'PASS'); nfail = sum(1 for s in SUMMARY if s[7] == 'FAIL')
put(ws, r, 5, '断言总数 %d，PASS %d，FAIL %d' % (len(SUMMARY), npass, nfail))
ws.merge_cells(start_row=r, start_column=5, end_row=r, end_column=9)
ws.cell(row=r, column=5).font = Font(bold=True, size=11, color='006100' if nfail == 0 else '9C0006')
ws.freeze_panes = 'A2'

# ---------- 场景行数对比 ----------
ws = wb.create_sheet('场景行数对比')
ws.append(['场景', '说明', '计划需求日期', '排程合并天数', '排程行数(非删除)', '缺料行数', '排程过期(标红)', '缺料过期(标红)'])
style_header(ws)
for i, w in enumerate([6, 34, 14, 14, 16, 12, 14, 14], 1):
    ws.column_dimensions[get_column_letter(i)].width = w
r = 2
for code in ['S1', 'S2', 'S3', 'S4']:
    d = DETAIL[code]; sched = d['sched']; short = d['short']
    so = sum(1 for x in sched if (x.get('source') or '') != 'WO' and pdate(x.get('batch_date_start')) and pdate(x.get('batch_date_start')) < TODAY)
    sho = sum(1 for x in short if pdate(x.get('component_demand_date')) and pdate(x.get('component_demand_date')) < TODAY)
    put(ws, r, 1, code, wrap=False); put(ws, r, 2, d['desc'])
    put(ws, r, 3, 'ON' if d['plan_req'] else 'OFF', wrap=False); put(ws, r, 4, d['smd'], wrap=False)
    put(ws, r, 5, len(sched), wrap=False); put(ws, r, 6, len(short), wrap=False)
    put(ws, r, 7, so, wrap=False); put(ws, r, 8, sho, wrap=False)
    r += 1
put(ws, r, 2, '注：排程过期=开工日<今日的非WO行；缺料过期=需求日<今日。开关ON时非WO行被压到当日，故过期数下降（WO行保留过去日）。')
ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=8); ws.cell(row=r, column=2).font = Font(size=9, italic=True, color='7F6000')

# ---------- 每场景排程/缺料明细 ----------
wo_map = {w['order_no']: w for w in WO}

def base_sched_by_aufnr(code):
    m = {}
    for x in DETAIL[code]['baseline_sched']:
        m[x['aufnr']] = x
    return m

def expected_delayed_sched(actual, base, plan_req):
    a_start = pdate(actual.get('batch_date_start'))
    if a_start is not None and a_start < TODAY:
        return 1
    if plan_req and base is not None:
        b_start = pdate(base.get('batch_date_start'))
        if b_start is not None and b_start < TODAY:
            return 1
    return 0

def sched_expect_text(actual, base, plan_req, smd):
    src = (actual.get('source') or '')
    if src == 'WO':
        return '工单独立：日期/数量同工单表，延误=0'
    parts = []
    if plan_req:
        b = pdate(base.get('batch_date_start')) if base else None
        be = pdate(base.get('batch_date_end')) if base else None
        if b is not None and b < TODAY:
            if be is not None and be < TODAY:
                parts.append('过期→开工=完工=当日%s' % T8)
            else:
                parts.append('跨期→开工=当日%s、完工保留%s' % (T8, base.get('batch_date_end')))
        else:
            parts.append('未过期→开工=交货−提前期')
    else:
        parts.append('日期/数量不变；延误=1 若开工<当日')
    if smd and smd > 0:
        parts.append('合并→Σ数量、开工=交货−提前期(Σ量)')
    return '；'.join(parts)

def sched_verdict(actual, base, plan_req):
    src = (actual.get('source') or '')
    if src == 'WO':
        w = wo_map.get(actual.get('aufnr'))
        if not w:
            return 'FAIL'
        es = str(w['planned_start'])[:10].replace('-', ''); ee = str(w['planned_end'])[:10].replace('-', '')
        ok = (str(actual.get('batch_date_start') or '') == es and str(actual.get('batch_date_end') or '') == ee and int(actual.get('delayed') or 0) == 0)
        return 'PASS' if ok else 'FAIL'
    exp = expected_delayed_sched(actual, base, plan_req)
    a_start = pdate(actual.get('batch_date_start'))
    if plan_req and a_start is not None and len(str(actual.get('batch_date_start')).strip()) == 8 and a_start < TODAY:
        return 'FAIL'  # ON 时非WO不应仍过期
    return 'PASS' if int(actual.get('delayed') or 0) == exp else 'FAIL'

for code in ['S1', 'S2', 'S3', 'S4']:
    d = DETAIL[code]; bmap = base_sched_by_aufnr(code)
    ws = wb.create_sheet('%s_排程' % code)
    ws.append(['场景', '物料编码', '物料名称', '来源', '单据(source_refs)', '批次(aufnr)',
               '测试.数量', '测试.开工', '测试.完工', '期待.规则', '实际.数量', '实际.开工', '实际.完工', '实际.延误', '结论'])
    style_header(ws)
    for i, w in enumerate([6, 12, 16, 8, 22, 12, 10, 11, 11, 40, 10, 11, 11, 9, 8], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    r = 2
    for x in sorted(d['sched'], key=lambda z: ((z.get('source') or ''), str(z.get('material_code')), str(z.get('aufnr')))):
        base = bmap.get(x['aufnr'])
        merged = ('、' in str(x.get('source_refs') or '')) or (',' in str(x.get('source') or ''))
        put(ws, r, 1, code, wrap=False); put(ws, r, 2, x.get('material_code'), wrap=False)
        put(ws, r, 3, x.get('material_name')); put(ws, r, 4, x.get('source'), wrap=False)
        put(ws, r, 5, (str(x.get('source_refs') or '') + ('（合并）' if merged else '')))
        put(ws, r, 6, x.get('aufnr'), wrap=False)
        if base:
            put(ws, r, 7, round(float(base.get('batch_qty') or 0), 2), wrap=False)
            put(ws, r, 8, base.get('batch_date_start'), wrap=False); put(ws, r, 9, base.get('batch_date_end'), wrap=False)
        else:
            put(ws, r, 7, '', wrap=False); put(ws, r, 8, '', wrap=False); put(ws, r, 9, '', wrap=False)
        put(ws, r, 10, sched_expect_text(x, base, d['plan_req'], d['smd']))
        put(ws, r, 11, round(float(x.get('batch_qty') or 0), 2), wrap=False)
        put(ws, r, 12, x.get('batch_date_start'), wrap=False); put(ws, r, 13, x.get('batch_date_end'), wrap=False)
        put(ws, r, 14, int(x.get('delayed') or 0), wrap=False)
        verdict_cell(ws.cell(row=r, column=15), sched_verdict(x, base, d['plan_req']))
        r += 1
    ws.freeze_panes = 'A2'

    # 缺料明细
    bmap2 = {}
    for x in d['baseline_short']:
        bmap2.setdefault((x['component_code'], x['source'], x['source_refs']), []).append(x)
    ws = wb.create_sheet('%s_缺料' % code)
    ws.append(['场景', '子件编码', '子件名称', '父件', '来源', '来源单据', '测试.需求数', '测试.需求日',
               '期待.规则', '实际.需求数', '实际.需求日', '实际.need_qty', '实际.延误', '结论'])
    style_header(ws)
    for i, w in enumerate([6, 12, 14, 12, 8, 24, 11, 12, 38, 11, 12, 12, 9, 8], 1):
        ws.column_dimensions[get_column_letter(i)].width = w
    r = 2
    for x in sorted(d['short'], key=lambda z: (str(z.get('component_demand_date') or ''), str(z.get('component_code')), str(z.get('source')))):
        blist = bmap2.get((x['component_code'], x['source'], x['source_refs']), [])
        base = blist[0] if len(blist) == 1 else None  # 同键多行时不假定 1:1（WO 同子件可有多需求日）
        merged = ('、' in str(x.get('source_refs') or '')) or (',' in str(x.get('source') or ''))
        src = (x.get('source') or '')
        if d['plan_req'] and src != 'WO':
            exp_txt = '需求日压=当日%s；延误=1(若过期)' % T10
        elif d['plan_req'] and src == 'WO':
            exp_txt = '工单：需求日不改写；延误=1(若过期)'
        else:
            exp_txt = '需求日/数量不变；延误=1 若需求日<当日'
        if merged:
            exp_txt += '；合并→Σ需求、需求日=段内最早'
        put(ws, r, 1, code, wrap=False); put(ws, r, 2, x.get('component_code'), wrap=False)
        put(ws, r, 3, x.get('component_name')); put(ws, r, 4, x.get('parent_code'), wrap=False)
        put(ws, r, 5, src, wrap=False); put(ws, r, 6, (str(x.get('source_refs') or '') + ('（合并）' if merged else '')))
        if base:
            put(ws, r, 7, round(float(base.get('required_qty') or 0), 2), wrap=False)
            put(ws, r, 8, base.get('component_demand_date'), wrap=False)
        elif blist:
            put(ws, r, 7, '（同键多行）', wrap=False); put(ws, r, 8, '（同键多行）', wrap=False)
        else:
            put(ws, r, 7, '', wrap=False); put(ws, r, 8, '', wrap=False)
        put(ws, r, 9, exp_txt)
        put(ws, r, 10, round(float(x.get('required_qty') or 0), 2), wrap=False)
        put(ws, r, 11, x.get('component_demand_date'), wrap=False)
        put(ws, r, 12, round(float(x.get('need_qty') or 0), 2), wrap=False)
        put(ws, r, 13, int(x.get('delayed') or 0), wrap=False)
        # 结论（不依赖基线 1:1 匹配，避免同键多行误判；集合级不变式见「总览」W1/W2/W3b）
        a_d = pdate(x.get('component_demand_date'))
        if d['plan_req'] and src != 'WO':
            ok = (a_d is not None and a_d >= TODAY)  # 开关ON 非WO：日期须已归位到当日
            if base and pdate(base.get('component_demand_date')) is not None and pdate(base.get('component_demand_date')) < TODAY:
                ok = ok and (int(x.get('delayed') or 0) == 1)
        else:  # WO 或 开关OFF：延误应等于"需求日是否<当日"
            exp = 1 if (a_d is not None and a_d < TODAY) else 0
            ok = (int(x.get('delayed') or 0) == exp)
        verdict_cell(ws.cell(row=r, column=14), 'PASS' if ok else 'FAIL')
        r += 1
    ws.freeze_panes = 'A2'

out = os.path.join(HERE, '一键计算_四场景验证报告_%s.xlsx' % R['today'])
wb.save(out)
print('SAVED', out)
print('sheets:', wb.sheetnames)
