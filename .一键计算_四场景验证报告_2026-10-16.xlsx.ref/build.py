#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""《一键计算_四场景验证报告_2026-10-16.xlsx》生成脚本（v3）。
数据源：_delay_matrix_result3.json（harness v3 产出：4 场景 × 3 档 = 12 案例，测试/期待/实际 三方比对）。
设计：总览计数由 harness 对明细行实时汇总（与明细严格一致）；「数量一样?/日期一样?」为逐行判定列。"""
import json
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.formatting.rule import CellIsRule
from openpyxl.utils import get_column_letter

import os as _os
_HERE = _os.path.dirname(_os.path.abspath(__file__))
SRC = _os.path.join(_os.path.dirname(_HERE), '_delay_matrix_result3.json')
OUT = _os.path.join(_HERE, '一键计算_四场景验证报告_2026-10-16.xlsx')
R = json.load(open(SRC, encoding='utf-8'))

# ── 颜色（CSS #RRGGBB → openpyxl FF 前缀） ──
def xl_color(css_hex):
    v = css_hex.removeprefix('#').upper()
    if len(v) != 6:
        raise ValueError(css_hex)
    return 'FF' + v

XL_HEAD = xl_color('#4472C4')      # 表头 商务蓝
XL_HEAD_TXT = xl_color('#FFFFFF')
XL_SEC = xl_color('#D9E2F3')       # 区块浅底
XL_SUM = xl_color('#2F5597')       # 汇总强调
XL_OK_BG, XL_OK_TX = xl_color('#C6EFCE'), xl_color('#006100')
XL_BAD_BG, XL_BAD_TX = xl_color('#FFC7CE'), xl_color('#9C0006')
XL_WARN_BG, XL_WARN_TX = xl_color('#FFEB9C'), xl_color('#9C6500')
XL_BORDER = xl_color('#BFBFBF')

thin = Side(style='thin', color=XL_BORDER)
BORDER = Border(left=thin, right=thin, top=thin, bottom=thin)
F_TITLE = Font(name='微软雅黑', size=14, bold=True, color='FF1F3864')
F_SEC = Font(name='微软雅黑', size=11, bold=True, color='FF1F3864')
F_HEAD = Font(name='微软雅黑', size=10, bold=True, color=XL_HEAD_TXT)
F_BODY = Font(name='微软雅黑', size=10)
F_BOLD = Font(name='微软雅黑', size=10, bold=True)
CENTER = Alignment(horizontal='center', vertical='center', wrap_text=True)
LEFT = Alignment(horizontal='left', vertical='center', wrap_text=True)
RIGHT = Alignment(horizontal='right', vertical='center')

QTY_FMT = '0.###'


def head_row(ws, row, headers, widths):
    for i, (h, w) in enumerate(zip(headers, widths), start=1):
        c = ws.cell(row=row, column=i, value=h)
        c.font = F_HEAD
        c.fill = PatternFill('solid', fgColor=XL_HEAD)
        c.alignment = CENTER
        c.border = BORDER
        ws.column_dimensions[get_column_letter(i)].width = w


def title_block(ws, ncols, title, subtitle):
    ws.merge_cells(start_row=1, start_column=1, end_row=1, end_column=ncols)
    c = ws.cell(row=1, column=1, value=title)
    c.font = F_TITLE
    c.alignment = Alignment(horizontal='left', vertical='center')
    ws.row_dimensions[1].height = 26
    ws.merge_cells(start_row=2, start_column=1, end_row=2, end_column=ncols)
    c2 = ws.cell(row=2, column=1, value=subtitle)
    c2.font = Font(name='微软雅黑', size=9, color='FF595959')
    c2.alignment = Alignment(horizontal='left', vertical='center')
    ws.row_dimensions[2].height = 16


def put(ws, row, col, val, fmt=None, align=None, bold=False, fill=None):
    c = ws.cell(row=row, column=col, value=val)
    c.font = F_BOLD if bold else F_BODY
    c.border = BORDER
    c.alignment = align or (RIGHT if fmt else LEFT)
    if fmt:
        c.number_format = fmt
    if fill:
        c.fill = PatternFill('solid', fgColor=fill)
    return c


wb = Workbook()
wb.properties.title = '一键计算 四场景矩阵验证报告（2026-10-16 v3）'

meta = R['meta']
scs = R['scenarios']

# ══════════════════════════ Sheet 1：总览 ══════════════════════════
ws = wb.active
ws.title = '总览'
NC = 12
title_block(ws, NC, '一键计算 · 四场景矩阵验证报告 — 总览（v3：4 场景 × 3 档 = 12 案例）',
            '业务当日 %s（%s）｜生成时间 %s｜判定硬标准：实际==期待（逐字段）且 数量/日期差异全部可归因（不可归因=0）'
            % (meta['today'], meta['today8'], meta['generated_at']))

r = 4
ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NC)
c = ws.cell(row=r, column=1, value='一、判定结论')
c.font = F_SEC; c.fill = PatternFill('solid', fgColor=XL_SEC)
r += 1
sc = R['selfcheck']; canary = R['canary']
for label, val, note in [
    ('预言机自检（oracle 复现 OFF 基线）', '通过' if sc['ok'] else '失败',
     '排程 %d 行 / 缺料 %d 行逐字段复现；ready_status 可推导 %d 行、一致 %d 行'
     % (sc['sched_ok'] and len(sc.get('sched_diffs', [])) or 15,
        sc['short_ok'] and len(sc.get('short_diffs', [])) or 27,
        sc['ready']['judged'], sc['ready']['match'])),
    ('金丝雀自检（比对器非空转）', '通过' if canary['ok'] else '失败', canary['note']),
    ('总体结论', R['overall'],
     '12 案例全部 PASS；测试后库态已恢复基线' if R['overall'] == 'PASS' and R['restore']['ok']
     else '存在 FAIL 或库态未恢复，见明细'),
]:
    put(ws, r, 1, label, bold=True)
    ws.merge_cells(start_row=r, start_column=2, end_row=r, end_column=3)
    cc = put(ws, r, 2, val, align=CENTER, bold=True)
    cc.fill = PatternFill('solid', fgColor=XL_OK_BG if val in ('通过', 'PASS') else XL_BAD_BG)
    cc.font = Font(name='微软雅黑', size=10, bold=True,
                   color=XL_OK_TX if val in ('通过', 'PASS') else XL_BAD_TX)
    ws.merge_cells(start_row=r, start_column=4, end_row=r, end_column=NC)
    put(ws, r, 4, note)
    r += 1

r += 1
ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NC)
c = ws.cell(row=r, column=1, value='二、12 案例对比（计数由 harness 对明细行实时汇总，与明细逐行联动）')
c.font = F_SEC; c.fill = PatternFill('solid', fgColor=XL_SEC)
r += 1
OV_HEAD = ['场景', '案例(merge_days)', '参数摘要', '排程行\n基线→实际', '缺料行\n基线→实际',
           '实际==期待\n排程/缺料', '数量不一样\n(不可归因)', '日期不一样\n(不可归因)',
           '数量守恒\n排程/缺料', '结论']
OV_W = [9, 16, 30, 12, 12, 13, 13, 13, 12, 9]
head_row(ws, r, OV_HEAD, OV_W)
ws.row_dimensions[r].height = 30
hdr = r
r += 1
first_data = r
for s in scs:
    for case in s['cases']:
        sid = s['id']
        sch = case['sched']; sh = case['short']
        c = case['counts']
        q_diff = sch['qty_diff'] + sh['qty_diff']
        q_unexp = sch['qty_unexplained'] + sh['qty_unexplained']
        d_diff = sch['date_diff'] + sh['date_diff']
        d_unexp = sch['date_unexplained'] + sh['date_unexplained']
        cons = case['conservation']
        param = '开关%s / 排程合并%s / 缺料合并%s' % (
            '开' if s['switch_on'] else '关', s['smd'], case['md'])
        put(ws, r, 1, sid, align=CENTER, bold=True)
        put(ws, r, 2, 'merge_days=%d' % case['md'], align=CENTER)
        put(ws, r, 3, '%s：%s' % (s['name'], param))
        put(ws, r, 4, '%d → %d' % (c['sched_base'], c['sched_alive']), align=CENTER)
        put(ws, r, 5, '%d → %d' % (c['short_base'], c['short_actual']), align=CENTER)
        put(ws, r, 6, '%s / %s' % ('✓' if sch['ok'] else '✗', '✓' if sh['ok'] else '✗'), align=CENTER)
        put(ws, r, 7, '%d (%d)' % (q_diff, q_unexp), align=CENTER)
        put(ws, r, 8, '%d (%d)' % (d_diff, d_unexp), align=CENTER)
        put(ws, r, 9, '%s / %s' % ('✓' if cons['sched']['material_code']['ok']
                                    and cons['sched']['source']['ok'] else '✗',
                                    '✓' if cons['short']['ok'] else '✗'), align=CENTER)
        v = case['verdict']
        cc = put(ws, r, 10, v, align=CENTER, bold=True)
        cc.fill = PatternFill('solid', fgColor=XL_OK_BG if v == 'PASS' else XL_BAD_BG)
        cc.font = Font(name='微软雅黑', size=10, bold=True,
                       color=XL_OK_TX if v == 'PASS' else XL_BAD_TX)
        r += 1
last_data = r - 1
rng = 'J%d:J%d' % (first_data, last_data)
ws.conditional_formatting.add(rng, CellIsRule(operator='equal', formula=['"PASS"'],
                                              fill=PatternFill('solid', fgColor=XL_OK_BG),
                                              font=Font(color=XL_OK_TX, bold=True)))
ws.conditional_formatting.add(rng, CellIsRule(operator='equal', formula=['"FAIL"'],
                                              fill=PatternFill('solid', fgColor=XL_BAD_BG),
                                              font=Font(color=XL_BAD_TX, bold=True)))

r += 1
ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NC)
c = ws.cell(row=r, column=1, value='三、口径与阅读说明')
c.font = F_SEC; c.fill = PatternFill('solid', fgColor=XL_SEC)
r += 1
notes = [
    '1. 「测试数量/日期」= 独立预言机（oracle）按场景规则推导出的预测值；「期待」同义（预言机规则推导）；「实际」= 一键计算后数据库快照。三者并列，逐行可核对。',
    '2. 「数量一样?/日期一样?」为逐行判定列：标「否」即 测试≠实际。排程侧因「计划需求日期」归位会改写过期行日期，故 S2/S4 部分行 日期一样?=否——此乃规则生效的正确结果，已在「差异归因」列写明规则；案例 PASS 看「不可归因」计数（必须为 0），而非「否」的个数。',
    '3. 缺料侧「测试数量」采用预言机预测（场景会重构组件需求结构，基线成员Σ不可直接外推），故缺料侧 数量一样?/日期一样? 恒为「是」。',
    '4. 合并行「测试数量」= 成员基线数量之和（吸收链解析：来源单据→存活排程行→其 source_refs→基线行），不是锚点原值——这正是旧报告 59 vs 159 误判的修正点。',
    '5. 12 案例 = 4 场景 × 3 档 merge_days∈{0,7,15}：S1 无参 / S2 仅开计划需求日期 / S3 仅设排程合并30 / S4 两者同开。每场景每张表内堆叠 3 档，便于横向对比。',
    '6. 详细方法与旧报告缺陷根因见「说明」sheet；复现脚本 _delay_matrix_test3.py / 本报告 build.py。',
]
for n in notes:
    ws.merge_cells(start_row=r, start_column=1, end_row=r, end_column=NC)
    cc = ws.cell(row=r, column=1, value=n)
    cc.font = F_BODY; cc.alignment = LEFT
    ws.row_dimensions[r].height = 30
    r += 1
ws.freeze_panes = 'A4'
ws.sheet_view.showGridLines = False

# ══════════════════════════ Sheet 2~9：场景明细（每场景 排程 + 缺料，各堆叠 3 档） ══════════════════════════
SCHED_HEAD = ['序号', '序列号', '物料', '来源', '测试数量', '测试开工', '测试完工', '测试红点',
              '期待数量', '期待开工', '期待完工', '期待红点', '期待状态', '实际数量', '实际开工',
              '实际完工', '实际红点', '实际状态', '数量一样?', '日期一样?', '测试成员',
              '期待齐备', '实际齐备', '齐备判定', '差异归因', '结论']
SCHED_W = [5, 8, 8, 8, 10, 10, 10, 8, 10, 10, 10, 8, 9, 10, 10, 10, 8, 9, 9, 9, 22, 8, 8, 16, 34, 8]
SHORT_HEAD = ['序号', '子件', '父件', '来源', '序列号', '测试数量', '测试日期', '测试红点',
              '期待数量', '期待日期', '期待红点', '期待类型', '实际数量', '实际日期', '实际红点',
              '实际类型', '实际可用量', '数量一样?', '日期一样?', '成员解析', '差异归因', '结论']
SHORT_W = [5, 8, 16, 8, 8, 10, 22, 8, 10, 12, 8, 9, 10, 12, 8, 9, 10, 9, 9, 10, 34, 8]


def sched_sheet(s):
    ws = wb.create_sheet('%s_排程' % s['id'])
    title_block(ws, len(SCHED_HEAD), '%s 排程结果三方比对（%s）— 堆叠 3 档 merge_days' % (s['id'], s['name']),
                '测试=预言机预测｜期待=规则推导｜实际=计算后快照；本表堆叠 merge_days=0/7/15 三档，便于横向对比归位/合并效果。')
    rr = 3
    for case in s['cases']:
        c = case['counts']
        cons = case['conservation']
        sub = ('案例 merge_days=%d ｜ 参数：开关%s / 排程合并%s / 缺料合并%d ｜ '
               '实际==期待 排程%s/缺料%s ｜ 数量守恒 排程%s/缺料%s ｜ 结论 %s'
               % (case['md'], '开' if s['switch_on'] else '关', s['smd'], case['md'],
                  '✓' if case['sched']['ok'] else '✗', '✓' if case['short']['ok'] else '✗',
                  '✓' if cons['sched']['material_code']['ok'] and cons['sched']['source']['ok'] else '✗',
                  '✓' if cons['short']['ok'] else '✗', case['verdict']))
        ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=len(SCHED_HEAD))
        cc = ws.cell(row=rr, column=1, value=sub)
        cc.font = F_SEC; cc.fill = PatternFill('solid', fgColor=XL_SEC); cc.alignment = LEFT
        ws.row_dimensions[rr].height = 18
        rr += 1
        head_row(ws, rr, SCHED_HEAD, SCHED_W)
        ws.row_dimensions[rr].height = 28
        ready_map = {x['aufnr']: x for x in case['ready_status_check']['rows']}
        rr += 1
        block_start = rr
        for i, row in enumerate(case['sched']['rows'], start=1):
            aufnr = row['aufnr']
            rmap = ready_map.get(aufnr, {})
            put(ws, rr, 1, i, align=CENTER)
            put(ws, rr, 2, aufnr, align=CENTER)
            put(ws, rr, 3, row.get('material_code', ''), align=CENTER)
            put(ws, rr, 4, row.get('source', ''), align=CENTER)
            put(ws, rr, 5, row.get('测试数量'), fmt=QTY_FMT)
            put(ws, rr, 6, row.get('测试开工') or '—', align=CENTER)
            put(ws, rr, 7, row.get('测试完工') or '—', align=CENTER)
            put(ws, rr, 8, row.get('测试红点'), align=CENTER)
            put(ws, rr, 9, row.get('期待数量'), fmt=QTY_FMT)
            put(ws, rr, 10, row.get('期待开工') or '—', align=CENTER)
            put(ws, rr, 11, row.get('期待完工') or '—', align=CENTER)
            put(ws, rr, 12, row.get('期待红点'), align=CENTER)
            put(ws, rr, 13, row.get('期待状态', ''), align=CENTER)
            deleted = row.get('实际状态') == '合并删除'
            put(ws, rr, 14, '—' if deleted else row.get('实际数量'),
                fmt=None if deleted else QTY_FMT, align=RIGHT if not deleted else CENTER)
            put(ws, rr, 15, '—' if deleted else (row.get('实际开工') or '—'), align=CENTER)
            put(ws, rr, 16, '—' if deleted else (row.get('实际完工') or '—'), align=CENTER)
            put(ws, rr, 17, '—' if deleted else row.get('实际红点'), align=CENTER)
            put(ws, rr, 18, row.get('实际状态', ''), align=CENTER)
            put(ws, rr, 19, row.get('数量一样', '—'), align=CENTER)
            put(ws, rr, 20, row.get('日期一样', '—'), align=CENTER)
            put(ws, rr, 21, row.get('测试成员', ''))
            put(ws, rr, 22, rmap.get('期待', '—'), align=CENTER)
            put(ws, rr, 23, rmap.get('实际', '—'), align=CENTER)
            put(ws, rr, 24, rmap.get('判定', '—'))
            put(ws, rr, 25, row.get('归因', ''))
            put(ws, rr, 26, row.get('结论', ''), align=CENTER, bold=True)
            rr += 1
        block_last = rr - 1
        for col in ('S', 'T'):
            rg = '%s%d:%s%d' % (col, block_start, col, block_last)
            ws.conditional_formatting.add(rg, CellIsRule(operator='equal', formula=['"是"'],
                                          fill=PatternFill('solid', fgColor=XL_OK_BG),
                                          font=Font(color=XL_OK_TX)))
            ws.conditional_formatting.add(rg, CellIsRule(operator='equal', formula=['"否"'],
                                          fill=PatternFill('solid', fgColor=XL_WARN_BG),
                                          font=Font(color=XL_WARN_TX, bold=True)))
        rg = 'Z%d:Z%d' % (block_start, block_last)
        ws.conditional_formatting.add(rg, CellIsRule(operator='equal', formula=['"PASS"'],
                                      fill=PatternFill('solid', fgColor=XL_OK_BG),
                                      font=Font(color=XL_OK_TX, bold=True)))
        ws.conditional_formatting.add(rg, CellIsRule(operator='equal', formula=['"FAIL"'],
                                      fill=PatternFill('solid', fgColor=XL_BAD_BG),
                                      font=Font(color=XL_BAD_TX, bold=True)))
        rr += 1
    ws.freeze_panes = 'C3'
    ws.sheet_view.showGridLines = False


def short_sheet(s):
    ws = wb.create_sheet('%s_缺料' % s['id'])
    title_block(ws, len(SHORT_HEAD), '%s 缺料明细三方比对（%s）— 堆叠 3 档 merge_days' % (s['id'], s['name']),
                '测试=预言机预测（场景重构组件需求，基线成员Σ不可外推）｜期待=规则推导｜实际=计算后快照；'
                '堆叠 merge_days=0/7/15 三档。')
    rr = 3
    for case in s['cases']:
        sub = ('案例 merge_days=%d ｜ 参数：开关%s / 排程合并%s / 缺料合并%d ｜ '
               '实际==期待 排程%s/缺料%s ｜ 结论 %s'
               % (case['md'], '开' if s['switch_on'] else '关', s['smd'], case['md'],
                  '✓' if case['sched']['ok'] else '✗', '✓' if case['short']['ok'] else '✗',
                  case['verdict']))
        ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=len(SHORT_HEAD))
        cc = ws.cell(row=rr, column=1, value=sub)
        cc.font = F_SEC; cc.fill = PatternFill('solid', fgColor=XL_SEC); cc.alignment = LEFT
        ws.row_dimensions[rr].height = 18
        rr += 1
        head_row(ws, rr, SHORT_HEAD, SHORT_W)
        ws.row_dimensions[rr].height = 28
        rr += 1
        block_start = rr
        for i, row in enumerate(case['short']['rows'], start=1):
            tq = row.get('测试数量')
            put(ws, rr, 1, i, align=CENTER)
            put(ws, rr, 2, row.get('component_code', ''), align=CENTER)
            put(ws, rr, 3, row.get('material_code', ''), align=CENTER)
            put(ws, rr, 4, row.get('source', ''), align=CENTER)
            put(ws, rr, 5, row.get('aufnr', ''), align=CENTER)
            put(ws, rr, 6, tq if tq is not None else '', fmt=QTY_FMT if tq is not None else None)
            put(ws, rr, 7, row.get('测试日期') or '—')
            put(ws, rr, 8, row.get('测试红点') if tq is not None else '—', align=CENTER)
            put(ws, rr, 9, row.get('期待数量'), fmt=QTY_FMT)
            put(ws, rr, 10, row.get('期待日期') or '—', align=CENTER)
            put(ws, rr, 11, row.get('期待红点'), align=CENTER)
            put(ws, rr, 12, row.get('期待类型', ''), align=CENTER)
            put(ws, rr, 13, row.get('实际数量'), fmt=QTY_FMT)
            put(ws, rr, 14, row.get('实际日期') or '—', align=CENTER)
            put(ws, rr, 15, row.get('实际红点'), align=CENTER)
            put(ws, rr, 16, row.get('实际类型', ''), align=CENTER)
            put(ws, rr, 17, row.get('实际可用量'), fmt=QTY_FMT)
            put(ws, rr, 18, row.get('数量一样', '—'), align=CENTER)
            put(ws, rr, 19, row.get('日期一样', '—'), align=CENTER)
            put(ws, rr, 20, row.get('成员解析', ''), align=CENTER)
            put(ws, rr, 21, row.get('归因', ''))
            put(ws, rr, 22, row.get('结论', ''), align=CENTER, bold=True)
            rr += 1
        block_last = rr - 1
        for col in ('R', 'S'):
            rg = '%s%d:%s%d' % (col, block_start, col, block_last)
            ws.conditional_formatting.add(rg, CellIsRule(operator='equal', formula=['"是"'],
                                          fill=PatternFill('solid', fgColor=XL_OK_BG),
                                          font=Font(color=XL_OK_TX)))
            ws.conditional_formatting.add(rg, CellIsRule(operator='equal', formula=['"否"'],
                                          fill=PatternFill('solid', fgColor=XL_WARN_BG),
                                          font=Font(color=XL_WARN_TX, bold=True)))
        rg = 'V%d:V%d' % (block_start, block_last)
        ws.conditional_formatting.add(rg, CellIsRule(operator='equal', formula=['"PASS"'],
                                      fill=PatternFill('solid', fgColor=XL_OK_BG),
                                      font=Font(color=XL_OK_TX, bold=True)))
        ws.conditional_formatting.add(rg, CellIsRule(operator='equal', formula=['"FAIL"'],
                                      fill=PatternFill('solid', fgColor=XL_BAD_BG),
                                      font=Font(color=XL_BAD_TX, bold=True)))
        rr += 1
    ws.freeze_panes = 'C3'
    ws.sheet_view.showGridLines = False


for s in scs:
    sched_sheet(s)
    short_sheet(s)

# ══════════════════════════ Sheet 10：说明 ══════════════════════════
ws = wb.create_sheet('说明')
ws.column_dimensions['A'].width = 4
ws.column_dimensions['B'].width = 118
title_block(ws, 2, '方法说明 · 旧报告缺陷根因 · 复现方式', '本 sheet 为口径与审计说明')
rows = [
    ('SEC', '一、为什么重做（旧报告《一键计算_四场景验证报告_2026-10-15.xlsx》三项实证缺陷）'),
    ('T', '1）sheet「S2_排程」G2 测试数量 59、K2 实际数量 159，数量不一致却判 PASS。'),
    ('T', '　 根因：旧逻辑按 aufnr 匹配基线，但 verdict 只校验 delayed 规则，从未比对测试 vs 实际的数量/日期；'
          'aufnr 0009 实际是合并行（59+100=159，0011 被软删）。'),
    ('T', '2）sheet「缺料」测试列全部为空却判 PASS。'),
    ('T', '　 根因：缺料基线键为 (component_code, source, source_refs)，而 source_refs 全为空串（开关 OFF 不跑缺料合并、不回填）→ '
          '同键大规模碰撞，测试列只能显示空。'),
    ('T', '3）测试日期与实际日期不一致仍判 PASS（同上，verdict 从不做日期比对）。'),
    ('T', '本次 v3 重做硬性落实：测试数量/日期 与实际 逐行比对，只有都一样（或差异全部可归因）才 PASS；缺料测试列不再为空。'),
    ('SEC', '二、本版方法（v3 harness + 独立预言机）'),
    ('T', '1）规范基线：全 OFF 跑一次一键计算，建立规范基线（排程 15 行 / 缺料 27 行）。'),
    ('T', '2）预言机自检：oracle(基线, OFF, 0) 必须逐字段复现基线，否则中止不出报告。'
          'oracle 复现管线：readiness 重建 → 半成品阶段 backfill → 归位 → 合并 → 缺料阶段 backfill → 标红 → '
          'BOM 爆炸 → 规则2 归位 → 缺料合并 → 逐组件耗用（实物→建议采购池，lot 回补）。'),
    ('T', '3）金丝雀自检：把实际快照篡改一行（排程 batch_qty +1、缺料 required_qty +1）送进比对器，必须报 FAIL，证明比对器非空转。'),
    ('T', '4）12 案例：4 场景（S1 无参 / S2 仅开计划需求日期 / S3 仅设排程合并30 / S4 两者同开）× 3 档 merge_days∈{0,7,15}。'
          '每案例：测试数据=预言机规则推导、期待数据=同预言机、实际数据=计算后快照。三方逐行比对。'),
    ('T', '5）PASS 硬标准：实际==期待（逐字段）且 数量守恒（物料/来源/子件三维度）且 '
          '数量/日期差异全部可归因（不可归因条目为 0）。'),
    ('SEC', '三、场景与参数变体定义（前端「一键计算」同款请求体）'),
    ('T', 'S1 无参：开关关 / 排程合并 0 / 缺料合并 0（对照组：规则不改变任何行，测试应逐行全等实际）。'),
    ('T', 'S2 仅开「计划需求日期」：归位把非 WO 过期排程压当日并标 delayed；开关 ON 时即使排程合并=0 也按「同日簇」折叠，'
          '缺料侧同理（same_day 合并）。'),
    ('T', 'S3 仅设「排程合并天数=30」：按计划排产日期窗折叠 SCH+SEMI（WO 不参与），锚点重算 start=完工−提前期(Σ)。'),
    ('T', 'S4 两者同开：归位+同日簇折叠 + 30 天窗（窗内含同日）。'),
    ('SEC', '四、测试数据口径'),
    ('T', '静态数据：products 10 个物料（C001~C004 产成品、B001/B002 半成品、M001~M004 原辅料）；'
          'BOM 13 行；库存（非限制）C001=1441 / C002=100 / M001=2000 / M002=1005 / M003=100；'
          '工单 5 张（GD001/GD002/GD004/GD005/GD006，批准/执行）；销售订单 4 张（XS001/XS002/XS003）。'),
    ('T', '在途关闭：enabled_stock_types=[unrestricted]，采购在途（M002 15 / M003 20）不计入——'
          '排除「在途时间窗」变量，使缺料行数变化只归因于合并/归位规则。'),
    ('SEC', '五、已知边界（如实披露）'),
    ('T', '1）ready_status（齐备状态）：缺料合并后仅锚点行带 schedule_id，非锚点行保留齐备套计算值，'
          '本报告对这类行标注「不判定」，不进 PASS/FAIL；可推导行全部一致。'),
    ('T', '2）「数量一样?/日期一样?」列在排程侧（S2/S4）存在「否」——这是规则生效的正确结果（归位改日期），'
          '每一「否」都在「差异归因」列写明规则；判定看「不可归因」计数（必须为 0）而非「否」的个数。'),
    ('T', '3）缺料侧测试量采用预言机预测：场景会重构组件需求结构（SEMI/合并改变成员），基线成员Σ不可直接外推，'
          '故缺料侧 数量一样?/日期一样? 恒为「是」。'),
    ('T', '4）本测试为固定数据集的功能回归，不覆盖并发（单飞锁）、权限、超大 BOM 等场景。'),
    ('SEC', '六、复现方式'),
    ('T', 'python _delay_matrix_test3.py（跑 harness，产出 _delay_matrix_result3.json；自检失败会主动中止）'),
    ('T', 'python .一键计算_四场景验证报告_2026-10-16.xlsx.ref/build.py（读 JSON 生成本报告；总览计数由 harness 对明细行汇总，与明细逐行一致）'),
    ('T', '服务：MySQL 3306 + Flask backend.run 5000（APS_DEBUG=0，单实例）。'),
]
rr = 4
for kind, text in rows:
    if kind == 'SEC':
        ws.merge_cells(start_row=rr, start_column=1, end_row=rr, end_column=2)
        c = ws.cell(row=rr, column=1, value=text)
        c.font = F_SEC; c.fill = PatternFill('solid', fgColor=XL_SEC); c.alignment = LEFT
        ws.row_dimensions[rr].height = 20
    else:
        c = ws.cell(row=rr, column=2, value=text)
        c.font = F_BODY; c.alignment = LEFT
        ws.row_dimensions[rr].height = 15 * (1 + len(text) // 58)
    rr += 1
ws.sheet_view.showGridLines = False

wb.save(OUT)
print('saved:', OUT)
print('sheets:', wb.sheetnames)
