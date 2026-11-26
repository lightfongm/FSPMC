# -*- coding: utf-8 -*-
"""排程计算 — 齐备套 / 优先级 / 结果查询"""

import json
import logging
import math
import threading
import uuid
from datetime import datetime, date, timedelta
from flask import Blueprint, request, jsonify

from ..database import (query_db, execute_db, execute_many_db, transaction,
                        in_transaction, execute_ddl)
from ..errors import err_payload
from ..utils import gen_id, serialize_row, serialize_rows, biz_today, biz_now
from ..helpers import get_inventory_map, get_unlimited_codes, get_config, set_config, build_inventory_pool

# 一键计算单飞锁（P1-3）：peacock_calculate 跨多表 DELETE+INSERT，Flask 默认多线程，
# 重复点击 / 定时与手动并发触发会交错执行，破坏 peacock_schedule 与
# peacock_material_shortage 的一致性。用非阻塞锁直接拒绝并发请求（不排队）。
# 【P3 说明 · 2026-10-14】本锁是**进程内**锁。当前部署形态为单进程
# （`backend/run.py` → `app.run()`，Werkzeug 单进程多线程），故进程内锁即可覆盖全部请求。
# ⚠ 若将来改成多 worker（gunicorn -w N / waitress 多进程 / 多容器），本锁失效，
# 需换成跨进程互斥（数据库 advisory lock 或 Redis 分布式锁），否则并发一键计算
# 会交错 DELETE+INSERT 破坏 peacock_schedule / peacock_material_shortage 一致性。
_calc_lock = threading.Lock()

# 【P1-14】环状 BOM 告警去重集合（进程级）：同一条环只报一次，避免递归展开时刷屏。
_BOM_CYCLE_REPORTED = set()


def _as_int(v, default=0, lo=None, hi=None):
    """把任意入参安全转成 int（【P3 归一 · 2026-10-14】）。

    合并天数类参数（`schedule_merge_days` / `merge_days`）原本直接拿 `data.get(...)` 的
    原值参与 `> 0` 比较与 `set_config`：
      · 前端传字符串（"7"）→ `str > int` 在 Py3 直接 TypeError → 一键计算 500；
      · 传非数字（"abc"/None）→ `int()` 抛 ValueError 未被捕获 → 500 HTML 错误页。
    现统一走本函数：非法值静默回退 default，并按 lo/hi 夹紧。
    """
    try:
        if v is None or v == '':
            return default
        n = int(float(v))
    except (TypeError, ValueError):
        return default
    if lo is not None and n < lo:
        n = lo
    if hi is not None and n > hi:
        n = hi
    return n


def _recalc_available(component_code, stock_types, inv_map):
    """
    根据库存类型选择，动态计算某个物料的可用库存。
    stock_types: ['unrestricted', 'quality', ...]
    inv_map: get_inventory_map() 的返回结果
    """
    if not stock_types:
        stock_types = ['unrestricted']
    inv = inv_map.get(component_code, {})
    total = 0
    if 'unrestricted' in stock_types:
        total += inv.get('sap_labst', 0)
    if 'quality' in stock_types:
        total += inv.get('sap_insme', 0)
    if 'blocked' in stock_types:
        total += inv.get('sap_speme', 0)
    if 'transit' in stock_types:
        total += inv.get('sap_umlme', 0)
    return total

# MRP 数量浮点容差：吸收 req/avail 浮点运算误差，
# 避免"库存与需求本应相等"时算出 1e-13 量级的假缺料（远小于任何实际计量精度）。
MRP_QTY_EPS = 1e-6

# [预留钩子] 排程模块开关「计划需求日期」：启用后需求日期须 >= 当日。
# 开关状态存于 system_config.module_plan_req_date（'1'启用 / '0'禁用），默认禁用。
# 暂不接入任何计算分支；启用逻辑待在生成排程/缺料前，对需求日期 < 当日 的行做调整或拦截。
PLAN_REQ_DATE_MODULE = 'plan_req_date'


def _biz_today():
    """业务「当日」：固定 +08:00（中国时区），不依赖服务器 OS 时区。

    用于「计划需求日期」归位等需要稳定业务日期的逻辑（问题6 修复）。
    公式为 max(0, 需求 − 可用库存) 的「当日」基准必须唯一且稳定：若直接用
    Python `date.today()`，当后端进程 OS 时区非 +08:00（如 UTC）时「当日」会
    比业务日期差一天，导致归位误判。这里用显式偏移，绕开 OS 时区。

    【P1-5 修复·2026-10-14】实现已抽至共享模块 backend/utils.py（biz_today），
    工单侧（work_orders.py）改用同一函数，杜绝排程侧 +08:00 / 工单侧 OS 时区分裂。
    本函数保留为模块内别名，5 个调用点（:699/:852/:1141/:2338/:3327）无需改动。
    """
    return biz_today()


def _migrate():
    """存量表加列（幂等，模块导入即启动时执行）。

    1) `delayed`（延误标记）：peacock_schedule / peacock_material_shortage；
    2) `parent_qty`（缺料明细"产品需求"）：原先在 `_get_shortage_rows` 内执行 ALTER，
       等于**查询接口触发 DDL** —— 既在只读请求上产生 DDL 锁，也会在事务内隐式提交，
       已上提到启动迁移。

    失败处理：记录 ERROR 日志但**不阻断后端启动**（仅对应功能不可用）。
    这里刻意不向上抛出——启动时抛异常会导致整个后端起不来，代价大于缺列；
    与原先 `except: pass` 的区别是：现在会显式报错，不再"静默"。
    """
    try:
        for table in ('peacock_schedule', 'peacock_material_shortage'):
            cols = query_db(
                "SELECT COLUMN_NAME FROM information_schema.COLUMNS "
                "WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = %s", (table,)
            )
            if not cols:
                continue  # 表尚未创建（全新库），跳过
            if 'delayed' not in {r['COLUMN_NAME'] for r in cols}:
                # 注意：DELAYED 是 MySQL 保留字（INSERT DELAYED 语法），列名必须用反引号包裹
                execute_ddl("ALTER TABLE %s ADD COLUMN `delayed` TINYINT(1) DEFAULT 0 "
                            "COMMENT '延误标记：1=原计划日期早于计算当日，已被强制调整为当日'" % table)
        # parent_qty：由查询路径 _get_shortage_rows 上提到启动迁移
        if query_db("SHOW TABLES LIKE 'peacock_material_shortage'"):
            sh_cols = {r['Field'] for r in query_db("SHOW COLUMNS FROM peacock_material_shortage")}
            if 'parent_qty' not in sh_cols:
                execute_ddl("ALTER TABLE peacock_material_shortage ADD COLUMN parent_qty DECIMAL(18,3) NULL")
            # 【P0/P1 · 2026-10-20】缺料明细新增列（缺口数量/建议采购量/最迟下单日/在途匹配/合并成员）。
            # 与 parent_qty 同理放在启动迁移：只读接口（缺料明细弹窗/导出）直接 SELECT 这些列，
            # 若等 _ensure_calc_columns（仅计算路径触发）才建列，重启后首次打开弹窗会因缺列报错。
            for _c, _ddl in (
                ('shortage_qty',
                 "ADD COLUMN shortage_qty DECIMAL(18,3) NULL COMMENT '缺口数量=max(0,子件需求-可用)'"),
                ('suggested_purchase_qty',
                 "ADD COLUMN suggested_purchase_qty DECIMAL(18,3) NULL COMMENT '建议采购量(含最小采购量/包装量取整)'"),
                ('latest_order_date',
                 "ADD COLUMN latest_order_date VARCHAR(10) NULL COMMENT '最迟下单日=子件需求日期-采购提前期'"),
                ('intransit_qty',
                 "ADD COLUMN intransit_qty DECIMAL(18,3) NULL COMMENT '该子件在途计划到货总量(注释用,不计入可用)'"),
                ('intransit_arrival_date',
                 "ADD COLUMN intransit_arrival_date VARCHAR(10) NULL COMMENT '在途最早计划到货日'"),
                ('intransit_match',
                 "ADD COLUMN intransit_match VARCHAR(20) NULL COMMENT '在途匹配:无缺口/可赶上/晚于需求/无在途'"),
                ('member_count',
                 "ADD COLUMN member_count INT NULL COMMENT '合并成员数(1=未合并)'"),
                ('merge_members',
                 "ADD COLUMN merge_members TEXT NULL COMMENT '合并成员明细JSON(来源/单据号/父件/数量/日期)'"),
            ):
                if _c not in sh_cols:
                    execute_ddl("ALTER TABLE peacock_material_shortage " + _ddl)
        # 【P1 · 2026-10-20】排程表合并成员列（排程合并行「展开成员」按钮数据源）
        if query_db("SHOW TABLES LIKE 'peacock_schedule'"):
            sc_cols = {r['Field'] for r in query_db("SHOW COLUMNS FROM peacock_schedule")}
            for _c, _ddl in (
                ('member_count',
                 "ADD COLUMN member_count INT NULL COMMENT '合并成员数(1=未合并)'"),
                ('merge_members',
                 "ADD COLUMN merge_members TEXT NULL COMMENT '合并成员明细JSON(来源/单据号/数量/日期)'"),
            ):
                if _c not in sc_cols:
                    execute_ddl("ALTER TABLE peacock_schedule " + _ddl)
    except Exception as e:
        logging.getLogger(__name__).error('[启动迁移] 加列失败，相关功能可能不可用：%s', e)


_migrate()


def _net_need(req, avail):
    """标准 MRP 净需求：max(0, 毛需求 − 可用库存)。

    存储层(_rebuild_all_shortages) 与展示层(_apply_inventory_and_transit) **必须共用此函数**，
    以落实"存储层 = 展示层"一致性原则。

    历史实现为四分支判断，其中 `avail == req` 分支误写为 `buy = req`，
    导致"库存刚好等于需求"时被判为全额缺料(MISSING)并生成虚假采购建议，已修正。
    """
    need = float(req or 0) - float(avail or 0)
    return 0.0 if need <= MRP_QTY_EPS else need

bp = Blueprint('schedule', __name__)


def _parse_date(value):
    """容错解析日期：支持 YYYYMMDD / YYYY-MM-DD / 带时间格式，返回 date 或 None。"""
    if not value:
        return None
    s = str(value).strip()
    candidates = []
    if ' ' in s or 'T' in s:
        candidates.append(s[:19])
    candidates.append(s[:10])
    for c in candidates:
        for fmt in ('%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d', '%Y%m%d'):
            try:
                return datetime.strptime(c, fmt).date()
            except ValueError:
                continue
    return None


def _load_product_lot_rules():
    """
    加载物料的采购批量规则：{material_code: (min_purchase_qty, batch_size)}。
    用于\"建议采购数量\"的最小采购量 / 包装量（批量）约束。
    """
    rows = query_db("SELECT material_code, min_purchase_qty, batch_size FROM products")
    rules = {}
    for r in rows:
        mat = (r.get('material_code') or '').strip()
        if not mat:
            continue
        rules[mat] = (float(r.get('min_purchase_qty') or 0), float(r.get('batch_size') or 0))
    return rules


def _apply_lot_sizing(raw_buy, min_purchase_qty, batch_size):
    """
    对\"建议采购数量\"施加最小采购量(min_purchase_qty)与包装量(batch_size)约束。
    标准 MRP 批量规则（顺序：先保最小，再取整到包装倍数）：
      1) 净需求 <= 0 → 0（无缺口不采购，最小采购量不强制）；
      2) 若净需求 < 最小采购量(且最小采购量>0) → 提升到最小采购量；
      3) 再向上取整到\"包装量\"的整数倍（包装量>0 时）。
    说明：先保最小、再取整到包装倍数，保证结果既 >= 最小采购量 又是包装量的整数倍。
    """
    if raw_buy <= 0:
        return 0.0
    buy = raw_buy
    if min_purchase_qty and min_purchase_qty > 0 and buy < min_purchase_qty:
        buy = min_purchase_qty
    if batch_size and batch_size > 0:
        buy = math.ceil((buy - 1e-9) / batch_size) * batch_size
    return float(buy)


def _load_transit_pools():
    """
    采购入库中状态='在途' 的计划到货数量，按物料+计划到货日期 汇总为池。
    返回 {material_code: [[plan_arrival_date(date|None), remaining_qty], ...]}，桶内按到货日期升序。
    说明：状态='在途' 表示实际到货数量未填，计划到货数量(arrival_qty) 即"在途待到"数量。
    """
    from collections import defaultdict
    rows = query_db(
        "SELECT material_code, plan_arrival_date, arrival_qty "
        "FROM purchase_orders WHERE status='在途' AND arrival_qty IS NOT NULL AND arrival_qty > 0"
    )
    pools = defaultdict(list)
    for r in rows:
        mat = (r.get('material_code') or '').strip()
        if not mat:
            continue
        d = _parse_date(r.get('plan_arrival_date'))
        qty = float(r.get('arrival_qty') or 0)
        if qty <= 0:
            continue
        pools[mat].append([d, qty])
    for mat in pools:
        pools[mat].sort(key=lambda x: (x[0] or date.max))
    return pools


def _consume_sort_key(req_date, source, aufnr, matnr, comp_code, req_qty, row_id):
    """【P1-13 修复 · 2026-10-14】「逐行耗用」的**全序排序键**，存储层与展示层**共用**。

    背景：库存不足时，同一子件在同一需求日若有多条需求行，"谁先耗用"直接决定
    `available_qty / shortage_type / 建议采购` 的取值。原实现两层口径不同：
      · 存储层 `_rebuild_all_shortages`：次键 = `source`（SCH 恒先于 WO，确定语义）；
      · 展示层 `_apply_inventory_and_transit`：次键 = `ms.id`，而 `id` 由 `gen_id()`
        （uuid4）生成，**每次 DELETE+INSERT 全量轮换** —— 顺序随机。
    后果：① 同子件、同需求日、不同来源的两行，两层可能给出相反的 OK/PARTIAL 结论，
    正是本文件"消除存储层/展示层口径分裂"所针对的问题复现；
    ② **非幂等** —— 同一份数据连跑两次，uuid 全换 → 并列行耗用顺序变化 →
    可用量/建议采购在两次运行间不同（上一轮"×3 连跑逐值一致"只在无并列行时成立）。

    统一为逐字相同的全序键：需求日 → 来源 → 工单/订单号 → 父物料 → 子件 → 需求量 → 行主键。
    日期统一转成 `YYYYMMDD` 字符串比较（两层原始类型不同：存储层多为 date、
    展示层为 'YYYY-MM-DD' 字符串，直接混比会 TypeError），无日期者排最后。
    """
    if isinstance(req_date, (datetime, date)):
        dkey = req_date.strftime('%Y%m%d')
    elif req_date:
        dkey = str(req_date)
    else:
        dkey = '99999999'  # 无需求日 → 排最后
    return (dkey,
            str(source or ''),
            str(aufnr or ''),
            str(matnr or ''),
            str(comp_code or ''),
            float(req_qty or 0),
            str(row_id or ''))


def _apply_inventory_and_transit(rows, inv_map, stock_types, include_transit):
    """
    统一的库存耗用 + 在途分配（递归/逐单耗用）。直接就地修改 rows。

    规则（对应计算规则第 10、11 节）：
      1) 按子件编码分组，组内按"子件需求日期"升序处理（保证在途桶按到货日期依次可用 + 逐单耗用顺序确定）。
      2) 在途桶：采购入库 status='在途' 且 plan_arrival_date <= 当前行子件需求日期 的 arrival_qty 进入可用池。
      3) transit_qty（展示）= 本行"新可用"的在途量（每个在途桶仅在其首次可按时到达的行展示一次），
         【不】按子件需求截断、也【不】显示运行余额（避免跨行重复累计，例如 300 整笔只显示一次）。
      4) 可用库存 = 实物库存 + 在途池（include_transit=False 时仅实物）；在途池仍是运行余额，逐行递减。
      5) 递归耗用：先耗用实物、再耗用在途；逐行递减，多余在途顺延至后续行（即"递归计算"）。
      6) 计划采购数量 = max(0, 子件需求 - 可用库存)。
      7) 建议采购数量：当净采购(计划采购) <> 0 时，在净采购基础上按该子件物料在"物料管理"中的
         "最小采购量"与"包装量"施加标准 MRP 批量规则（先保最小、再取整到包装倍数）。
         建议采购的富余量(建议采购 - 净采购)回补到该物料的"建议采购池"，参与后续行（同物料、
         更晚子件需求日期）的可用库存计算（即建议采购的数量会补充后续可用库存）。
    """
    from collections import defaultdict
    if not stock_types:
        stock_types = ['unrestricted']
    transit_pools = _load_transit_pools()
    lot_rules = _load_product_lot_rules()
    by_comp = defaultdict(list)
    for st in rows:
        by_comp[st.get('component_code', '')].append(st)

    for comp, comp_rows in by_comp.items():
        # 净算基线排除 'transit'：在途统一由下方 transit_pool（按到货日期）计入，
        # 避免与「可用库存（多选）」里的在途选项重复累加导致双计。
        _net_stock_types = [t for t in (stock_types or []) if t != 'transit']
        stock = _recalc_available(comp, _net_stock_types, inv_map)
        # 建议采购池：前面行"建议采购"的富余量(建议采购-净采购)回补此处，参与后续行可用库存
        suggested_pool = 0.0
        # 组内全序排序：【P1-13】与存储层 `_rebuild_all_shortages` 的 `comp_lines_sorted`
        # **共用同一个排序键**（原次键为 ms.id=随机 uuid，导致两层结论可能相反且非幂等）。
        comp_rows_sorted = sorted(
            comp_rows,
            key=lambda x: _consume_sort_key(
                x.get('component_demand_date'), x.get('source'), x.get('aufnr'),
                x.get('material_code'), x.get('component_code'),
                x.get('required_qty'), x.get('id'))
        )
        buckets = transit_pools.get(comp)
        bi = 0
        transit_pool = 0.0
        for st in comp_rows_sorted:
            d = st.get('component_demand_date') or ''
            dd = _parse_date(d)
            # 把"到货日期 <= 本行子件需求日期"的在途桶加入可用池（按到货日期升序）；
            # 同时记录"本行新可用"的在途量 —— 每个在途桶仅在其首次可按时到达的行展示一次，
            # 后续行展示 0，避免跨行重复累计（如 CD001 的 300 整笔只显示一次，列合计=实际在途 300）。
            newly_available = 0.0
            if buckets:
                while bi < len(buckets):
                    bd, bq = buckets[bi]
                    if bq <= 0:
                        bi += 1
                        continue
                    if bd is not None and dd is not None and bd <= dd:
                        transit_pool += bq
                        newly_available += bq
                        bi += 1
                    else:
                        break
            req = float(st.get('required_qty') or 0)
            # 展示在途 = 本行"新可用"的在途量（每个在途桶仅展示一次），无论是否勾选都展示
            st['transit_qty'] = newly_available
            # 可用库存 = 实物库存 + 在途池（勾选时） + 建议采购池（前面行的富余，供后续行耗用）
            if include_transit:
                avail = stock + transit_pool + suggested_pool
            else:
                avail = stock + suggested_pool
            # 递归耗用：先耗用实物、再耗用在途、再耗用"建议采购池"（逐行递减）
            consume = req
            s_used = min(consume, stock)
            stock -= s_used
            consume -= s_used
            t_used = min(consume, transit_pool)
            transit_pool -= t_used
            consume -= t_used
            sp_used = min(consume, suggested_pool)
            suggested_pool -= sp_used
            consume -= sp_used
            st['available_qty'] = avail
            st['stock_remaining'] = avail - req
            # 计划采购数量（净需求，即"净采购"；不施加批量约束）
            # 标准 MRP：buy = max(0, 需求 − 可用库存)。
            # 历史实现在 avail == req 时误写为 buy = req（库存刚好够却判全额缺料），
            # 已统一改用 _net_need（与存储层共用，保证两层一致）。
            buy = _net_need(req, avail)
            st['planned_purchase'] = buy
            # 建议采购数量：当净采购 <> 0 时，按物料"最小采购量/包装量"批量约束；
            # 富余量(delta = 建议采购 - 净采购)回补"建议采购池"，参与后续行的可用库存计算。
            if buy > 0:
                min_q, batch_sz = lot_rules.get(comp, (0.0, 0.0))
                suggested = _apply_lot_sizing(buy, min_q, batch_sz)
                suggested_pool += (suggested - buy)
            else:
                suggested = 0.0
            st['suggested_purchase'] = suggested
            # 状态判定
            if buy == 0:
                st['shortage_type'] = 'OK'
                st['stock_status'] = '正常'
            elif buy < req:
                st['shortage_type'] = 'PARTIAL'
                st['stock_status'] = '部分'
            else:
                st['shortage_type'] = 'MISSING'
                st['stock_status'] = '缺料'


def _prepare_shortage_context(bom_map):
    """构建物料生产/采购周期映射与日产量映射。

    【P3 清理 · 2026-10-14】原实现还额外构建 `comp_wip_pool`（按组件物料归集在制工单量），
    但 `_rebuild_all_shortages` **从未使用**它（组件 WIP 不参与原料缺料抵扣，见计算规则），
    每次调用却都要付一次 `work_orders` 聚合查询。现删除该死计算。
    """
    mat_cycle_map = {}
    mat_daily_map = {}
    cyc_rows = query_db("SELECT material_code, production_cycle, production_time FROM products")
    for cr in cyc_rows:
        mat_cycle_map[cr['material_code']] = float(cr.get('production_cycle') or 0)
        mat_daily_map[cr['material_code']] = float(cr.get('production_time') or 0)
    return mat_cycle_map, mat_daily_map


def _merge_req_lines(req_lines, merge_days, sched_ref=None, wo_ref=None, same_day=False):
    """按 采购件(comp_code) + 需求日期窗 合并子件需求（缺料合并天数）。

    - merge_days<=0：完全不合并，原样返回。
    - 同 采购件(comp_code) 内按需求日升序，以首行为锚点；后续行与锚点相差 > merge_days 即另起一段
      （新段锚点=该段首行，即「锚点式 7 天窗口」）。每段的：
        * 需求数量(gross)    = 段内之和；
        * 子件需求日期        = 段内最早(首行)日期；
        * 来源(source)       = 段内去重类型拼接（如 'WO,SCH'，供内部识别/前端合并判定）；
        * 来源单据(source_refs) = 段内各行的实际单据号（销售订单号-行号 / 工单号）拼接，
          供前端"缺料明细"展示（避免只显示 'WO,SCH' 类型码）；
        * 父件(parent_code)  = 段内全部父件（顿号分隔），跨父件合并时一并列出，保留追溯；
          其余字段(物料编码/aufnr/schedule_id/work_order_id)取锚点(首行)值。
    跨父件也合并（如 M001 的 B002 半成品需求与 C003 工单需求可并为一行，父件列显示
    "B002、C003"），不再将父件字段置空。需求日期为 NULL 的行不参与合并（各自独立成段）。
    合并后整段只参与一次 MRP 扣减。
    """
    if merge_days <= 0 and not same_day:
        return req_lines
    # 精确同日合并（same_day）：阈值取 0，仅 diff==0 合并；否则按 merge_days 窗口合并。
    _eff_days = merge_days if merge_days > 0 else 0
    from collections import defaultdict

    def _doc_ref(line):
        """合并行展示用实际单据号：SCH→销售订单号-行号；WO→工单号；SEMI→aufnr。

        兜底（修复「来源只统计第一行」）：SCH/SEMI 行若对应排程行 order_no 为空
        （半成品/独立排产行），sched_ref 取到空串会静默丢弃该需求来源；
        此时回退到物料编码(matnr)或排程序号(aufnr)，确保 7 天窗口内的
        每一条需求来源都被列出（如半成品 B002 驱动的原料需求显示为 'B002'）。"""
        src = line.get('source')
        if src == 'WO':
            return (wo_ref or {}).get(line.get('work_order_id') or '', '')
        if src == 'SCH':
            ref = (sched_ref or {}).get(line.get('schedule_id') or '', '')
            if not ref:
                ref = line.get('matnr') or line.get('aufnr') or ''
            return ref
        if src == 'SEMI':
            # 合并行 SEMI 也显示"顶层驱动订单号"(drive_aufnr)，与排程结果、非合并行口径一致。
            return line.get('drive_aufnr') or line.get('aufnr') or line.get('matnr') or ''
        return src or ''

    # 缺料合并天数：同 采购件(comp_code) + 7天需求日窗 合并（跨父件也合并，
    # 父件字段列出本段全部父件，保留追溯；不再置空，避免无法追溯）。
    by_comp = defaultdict(list)
    for line in req_lines:
        by_comp[line['comp_code']].append(line)

    def _dkey(l):
        d = l.get('req_date')
        return d if d is not None else date.max

    merged = []
    for comp_code, lines in by_comp.items():
        dated = [l for l in lines if l.get('req_date') is not None]
        undated = [l for l in lines if l.get('req_date') is None]
        dated.sort(key=_dkey)
        segments = []
        cur = None
        anchor = None
        for l in dated:
            d = l['req_date']
            if cur is None:
                cur = [l]; anchor = d
            else:
                diff = (d - anchor).days if isinstance(d, date) and isinstance(anchor, date) else 0
                if diff > _eff_days:
                    segments.append(cur)
                    cur = [l]; anchor = d
                else:
                    cur.append(l)
        if cur is not None:
            segments.append(cur)
        for l in undated:
            segments.append([l])

        for seg in segments:
            if len(seg) == 1:
                # 单来源段也要补 source_refs（否则来源列显示为空），并保留原始行其余字段
                lone = dict(seg[0])
                lone['source_refs'] = _doc_ref(seg[0])
                lone['delayed'] = seg[0].get('delayed', 0)
                # 【P1 · 2026-10-20】成员清单：单成员段记 1，前端不展示展开按钮
                lone['member_count'] = 1
                lone['merge_members'] = ''
                merged.append(lone)
                continue
            gross = sum(float(x.get('gross') or 0) for x in seg)
            if gross <= 0:
                continue
            sources = []
            doc_refs = []
            parents = []
            for x in seg:
                s = x.get('source')
                if s and s not in sources:
                    sources.append(s)
                dr = _doc_ref(x)
                if dr and dr not in doc_refs:
                    doc_refs.append(dr)
                pc = x.get('parent_code') or ''
                if pc and pc not in parents:
                    parents.append(pc)
            source_str = ','.join(sources)
            source_refs_str = '、'.join(doc_refs)
            first = seg[0]
            # 跨父件合并：父件列列出本段全部父件（顿号分隔）；其余字段取锚点行，保留追溯。
            parent_code_str = ('、'.join(parents)
                              if len(parents) > 1
                              else (parents[0] if parents else (first.get('parent_code') or '')))
            # 【P2-1】"产品需求"(parent_qty) 合并语义 = **段内求和**（与 gross 求和同口径）：
            # 合并行代表本段全部父件的合计产出需求，取锚点值会丢掉其余父件的量，
            # 与"需求数量已求和"自相矛盾（界面上会出现 需求大而产品需求小的怪象）。
            parent_qty_sum = sum(float(x.get('parent_qty') or 0) for x in seg)
            # 【P1 · 2026-10-20】合并成员明细：落库 JSON，供前端「展开N成员」按钮逐条展示
            # （来源/单据号/父件/子件需求/需求日期/延误）。此前只能靠 source_refs 文字反查，
            # 同一单据号对应多条排程行时存在歧义（旧报告 59 vs 159 误判的根源之一）。
            _members = []
            for x in seg:
                _rd = x.get('req_date')
                _members.append({
                    'source': x.get('source') or '',
                    'ref': _doc_ref(x),
                    'aufnr': x.get('aufnr') or '',
                    'parent_code': x.get('matnr') or '',
                    'gross': float(x.get('gross') or 0),
                    'req_date': _rd.strftime('%Y-%m-%d') if hasattr(_rd, 'strftime') else (_rd or ''),
                    'delayed': int(x.get('delayed', 0) or 0),
                })
            mline = {
                'source': source_str,
                'source_refs': source_refs_str,
                'comp_code': comp_code,
                'comp_name': first.get('comp_name', ''),
                'gross': gross,
                'parent_qty': parent_qty_sum,
                'req_date': first.get('req_date'),  # dated 已升序 → 首行为最早
                'bom_level': first.get('bom_level', 0),
                'is_parent_material': first.get('is_parent_material', 0),
                'schedule_id': first.get('schedule_id'),
                'work_order_id': first.get('work_order_id'),
                'aufnr': first.get('aufnr'),
                'matnr': first.get('matnr'),
                'parent_code': parent_code_str,
                # 延误保留铁律：段内任一原始行 delayed=1 → 合并行 delayed=1（合并不丢失延误信号）
                'delayed': 1 if any((x.get('delayed') or 0) == 1 for x in seg) else 0,
                'member_count': len(seg),
                'merge_members': json.dumps(_members, ensure_ascii=False),
            }
            merged.append(mline)
    return merged


# 排程合并 / 预填共用的提前期映射（从 products 加载，按需刷新）
_SCH_LEAD_DAILY = {}
_SCH_LEAD_CYCLE = {}


def _load_lead_maps():
    """从 products 加载 日产量(production_time)/采购周期(production_cycle) 映射，供 `_lead_days` 使用。"""
    global _SCH_LEAD_DAILY, _SCH_LEAD_CYCLE
    _SCH_LEAD_DAILY = {}
    _SCH_LEAD_CYCLE = {}
    for _pr in query_db("SELECT material_code, production_time, production_cycle FROM products"):
        _SCH_LEAD_DAILY[_pr['material_code']] = float(_pr.get('production_time') or 0)
        _SCH_LEAD_CYCLE[_pr['material_code']] = float(_pr.get('production_cycle') or 0)


def _lead_days(matnr, qty):
    """提前期(天)：日产量>0 时 ceil(数量/日产量)，否则回退采购周期；同为0 则 0。"""
    import math
    _d = _SCH_LEAD_DAILY.get(matnr, 0) or 0
    if _d > 0 and qty and qty > 0:
        return math.ceil(qty / _d)
    return _SCH_LEAD_CYCLE.get(matnr, 0) or 0


def _parse_date8(s):
    """把 YYYYMMDD / YYYY-MM-DD 等解析为 date；非法返回 None。"""
    from datetime import datetime
    if not s:
        return None
    s = str(s).strip()
    if len(s) >= 8:
        s = s[:8]
    try:
        return datetime.strptime(s, '%Y%m%d').date()
    except Exception:
        return None


def _prefill_schedule_starts():
    """排程合并 / 归位前，为「计划排产日期(batch_date_start)」为空的销售(SCH)/半成品(SEMI)行
    按 交货日(batch_date_end) − 提前期 预填 start（与步骤3 重算口径一致：
    日产量=production_time，缺失/0 回退 production_cycle）。

    ⚠ 必须在 `_enforce_schedule_dates()`（归位）**之前**执行：归位的 WHERE 条件为
    `batch_date_start IS NOT NULL AND batch_date_start < 今日`，若半成品行 start 仍为空会被
    跳过，随后再被本预填写入过去日 → 半成品永远不被归位（2026-10-13 重构回归修复）。
    该预填幂等：start 已有值的行直接跳过，可重复调用。
    """
    from datetime import timedelta
    _load_lead_maps()
    for _r in query_db(
        "SELECT id, material_code, plan_qty, batch_qty, batch_date_start, batch_date_end "
        "FROM peacock_schedule WHERE is_deleted=0 AND source IN ('SCH','SEMI') AND batch_qty>0"
    ):
        if _r.get('batch_date_start'):
            continue
        _end = _parse_date8(_r.get('batch_date_end'))
        _pq = float(_r.get('plan_qty') or 0)
        if _end and _pq > 0:
            _ld = _lead_days(_r['material_code'], _pq)
            _st = (_end - timedelta(days=_ld)) if _ld else _end
            execute_db("UPDATE peacock_schedule SET batch_date_start=%s WHERE id=%s",
                       (_st.strftime('%Y%m%d'), _r['id']))


def _merge_schedule_rows(schedule_merge_days, force_same_day=False):
    """按 物料(material_code) + 计划排产日期窗 合并排程行（排程合并天数）。

    镜像 _merge_req_lines 的分段重锚算法：
    - schedule_merge_days<=0：完全不合并，直接返回。
    - 销售订单(SCH)/半成品(SEMI)来源行参与合并；工单(WO)保持独立、不参与。
    - 同 material_code 内（SCH/SEMI 行）按 计划排产日期 batch_date_start(YYYYMMDD) 升序，以首行为锚点；后续行与锚点相差 > 天数
      即另起一段（新段锚点=该段首行）；batch_date_start 在合并前已由本函数预填（= 交货日 − 提前期）。每段的：
        * 保留锚点行（最早 batch_date_start），其余行 is_deleted=1 软删（防下游缺料双计）；
        * batch_qty = Σ段内（折叠后总量不变，锚点承接全部）；
        * plan_qty  = Σ段内（plan 可手动覆盖，分别求和保留累加）；
        * source    = 段内去重来源拼接（合并后可能为 'SCH'/'SEMI'/'SCH,SEMI'，供前端"合并"识别）；
        * source_refs = 段内实际单据号拼接（SCH→销售订单号-行号 / SEMI→drive_aufnr），供前端展示；
        * work_order_id = 段内若有 WO 行则逗号拼接 carry（排程合并不含 WO，故实际恒为空，保留兼容）。
    batch_date_start 为 NULL/空 的行独立成段，不参与合并。合并后由下游步骤3/4 级联重算缺料/齐备。
    """
    if schedule_merge_days <= 0 and not force_same_day:
        return
    # 精确同日合并（force_same_day，开关 ON）：阈值取 0，仅 diff==0 合并；
    # 否则按 schedule_merge_days 窗口合并（窗口>=1 时已包含同日，概念1 被概念2 覆盖）。
    _eff_days = schedule_merge_days if schedule_merge_days > 0 else 0
    from collections import defaultdict
    from datetime import date, datetime, timedelta
    import math

    # 注：source_refs 列的补建已上提到 _ensure_calc_columns()（事务外统一执行）。
    # 此处不再做 DDL —— DDL 会隐式提交事务，破坏一键计算的原子性。

    def _parse(s):
        if not s:
            return None
        s = str(s).strip()
        if len(s) >= 8:
            s = s[:8]
        try:
            return datetime.strptime(s, '%Y%m%d').date()
        except Exception:
            return None

    def _merge_key(row):
        """合并用锚定日期 = 计划排产日期(batch_date_start)。

        逻辑3：合并基准统一为计划排产日期。本函数在合并前已按 batch_date_end − 提前期
        预填 start（由 _prefill_schedule_starts() 完成，归位前调用），故此处取到的是计划排产日期；
        极个别 start 仍缺失的行回退交货日(batch_date_end)，不至于无法分段。
        """
        return _parse(row.get('batch_date_start')) or _parse(row.get('batch_date_end'))

    def _doc_ref(row):
        """合并行展示用实际单据号：SCH→销售订单号-行号；WO→工单号；SEMI→drive_aufnr。"""
        src = (row.get('source') or '').strip()
        if src == 'WO':
            return (row.get('work_order_id') or '').strip()
        if src == 'SCH':
            ono = (row.get('order_no') or '').strip()
            pos = (row.get('sales_posnr') or '').strip()
            return f"{ono}-{pos}" if (ono and pos) else ono
        if src == 'SEMI':
            return (row.get('drive_aufnr') or '').strip()
        return src or ''

    # ---- 逻辑3：合并基准 = 计划排产日期(batch_date_start) ----
    # batch_date_start 原本要到步骤3(_compute) 才回填，合并时多为空；为使合并真正基于
    # "计划排产日期"而非"交货日"，此处先按 batch_date_end − 提前期 预填 start 并落库
    # （与步骤3 重算口径一致：日产量=production_time，缺失/0 回退 production_cycle）。
    # 注：start 预填现已提取为 `_prefill_schedule_starts()`，归位前也会调用，
    # 此处再调用一次保持幂等（start 已有值则跳过）。
    _prefill_schedule_starts()

    # 幂等修复（2026-10-08）：排程合并的输入顺序必须确定，否则同物料同日期两行的
    # anchor(段首)选取随物理行序摆动 → 合并后 batch_date_start 不同 → 下游需求日不同 →
    # 缺料明细行数在 0 边界 ±1（即"18↔17"非幂等）。统一按 物料→交货日→开工日→序列号 稳定排序。
    rows = query_db(
        "SELECT * FROM peacock_schedule WHERE is_deleted=0 "
        "ORDER BY material_code, COALESCE(batch_date_end,'99999999'), "
        "COALESCE(batch_date_start,'99999999'), aufnr"
    )
    by_mat = defaultdict(list)
    for r in rows:
        # 排程合并作用于销售订单(SCH)/半成品(SEMI)来源行；工单(WO)保持独立，不参与合并。
        # batch_qty<=0 的行（如"已覆盖"行）无生产需求，既不该被折叠、也不该打断同物料分段，故排除。
        if (r.get('source') or '').strip() in ('SCH', 'SEMI') and float(r.get('batch_qty') or 0) > 0:
            by_mat[r['material_code']].append(r)

    for mat_code, lines in by_mat.items():
        dated = [l for l in lines if _merge_key(l)]
        undated = [l for l in lines if not _merge_key(l)]
        dated.sort(key=lambda l: _merge_key(l))

        segments = []
        cur = None
        anchor = None
        for l in dated:
            d = _merge_key(l)
            if cur is None:
                cur = [l]; anchor = d
            else:
                diff = (d - anchor).days
                if diff > _eff_days:   # 边界与缺料合并一致：严格大于（相差恰好 N 天即另起一段）
                    segments.append(cur)
                    cur = [l]; anchor = d
                else:
                    cur.append(l)
        if cur is not None:
            segments.append(cur)
        for l in undated:
            segments.append([l])

        for seg in segments:
            if len(seg) <= 1:
                continue
            anchor_row = seg[0]  # 最早 batch_date_start 行
            sum_batch = sum(float(x.get('batch_qty') or 0) for x in seg)
            sum_plan = sum(float(x.get('plan_qty') or 0) for x in seg)
            sources = []
            doc_refs = []
            wo_ids_all = []  # 段内所有 WO 的 work_order_id（去重，按出现序）
            for x in seg:
                s = (x.get('source') or '').strip()
                if s and s not in sources:
                    sources.append(s)
                dr = _doc_ref(x)
                if dr and dr not in doc_refs:
                    doc_refs.append(dr)
                if s == 'WO' and x.get('work_order_id'):
                    _wid = str(x.get('work_order_id')).strip()
                    if _wid and _wid not in wo_ids_all:
                        wo_ids_all.append(_wid)
            anchor_id = anchor_row['id']
            del_ids = [x['id'] for x in seg if x['id'] != anchor_id]
            if del_ids:
                placeholders = ','.join(['%s'] * len(del_ids))
                execute_db(
                    "UPDATE peacock_schedule SET is_deleted=1 WHERE id IN (%s)" % placeholders,
                    tuple(del_ids)
                )
            # 延误保留铁律：段内任一原始行 delayed=1 → 锚点 delayed=1（合并不丢失延误信号）
            max_delayed = 1 if any((x.get('delayed') or 0) == 1 for x in seg) else 0
            new_source = ','.join(sources)
            new_refs = '、'.join(doc_refs)
            # WO carry：段内所有 WO 的 work_order_id 逗号拼接挂锚点（保证每个 WO 缺料行都能
            # 经 wo_sched_map 挂回锚点，不破坏"独立工单进缺料"铁律）；无 WO 则清空
            carry_wo = ','.join(wo_ids_all)
            # 【P1 · 2026-10-20】合并成员明细：落库 JSON，供前端「展开N成员」按钮逐条展示
            # （来源/单据号/序列号/排产数量/计划排产日期/交货日期/延误）。
            _members = []
            for x in seg:
                _members.append({
                    'source': (x.get('source') or '').strip(),
                    'ref': _doc_ref(x),
                    'aufnr': x.get('aufnr') or '',
                    'material_code': x.get('material_code') or '',
                    'batch_qty': float(x.get('batch_qty') or 0),
                    'plan_qty': float(x.get('plan_qty') or 0),
                    'batch_date_start': x.get('batch_date_start') or '',
                    'batch_date_end': x.get('batch_date_end') or '',
                    'delayed': int(x.get('delayed') or 0),
                })
            # 合并后重算「计划排产日期」：合并后的排产数量变大 → 提前期变长 → 计划排产日应更早。
            # 公式：计划排产日期 = 交货日期(batch_date_end) − 提前期(合并后总排产数量 sum_batch)
            # 交货日期作为基准保持不变；提前期口径与本函数预填块一致（ceil(数量/日产量)，缺失回退周期）。
            _anchor_end = _parse(anchor_row.get('batch_date_end'))
            _new_start = anchor_row.get('batch_date_start')
            if _anchor_end:
                _ld = _lead_days(mat_code, sum_batch)
                _st = (_anchor_end - timedelta(days=_ld)) if _ld else _anchor_end
                _new_start = _st.strftime('%Y%m%d')
                # 归位保护（2026-10-13 重构回归修复）：本段含 delayed=1（被「计划需求日期」归位）的行时，
                # 合并重算出的 start 不得回退到今日之前，否则会抵消归位（归位后 start 必须 >= 今日）。
                # 原流程归位在步骤5(合并之后)故恒为最后写入方；重构后归位前置于合并，须在此处兜底钳制。
                if max_delayed == 1:
                    _t8 = _biz_today()
                    if _parse_date8(_new_start) < _t8:
                        _new_start = _t8.strftime('%Y%m%d')
            execute_db(
                """UPDATE peacock_schedule
                   SET batch_qty=%s, plan_qty=%s, source=%s, source_refs=%s,
                       work_order_id=%s, batch_date_start=%s, `delayed`=%s,
                       member_count=%s, merge_members=%s
                   WHERE id=%s""",
                (sum_batch, sum_plan, new_source, new_refs, carry_wo, _new_start, max_delayed,
                 len(seg), json.dumps(_members, ensure_ascii=False), anchor_id)
            )


def _rebuild_all_shortages(inv_pool, bom_map, mat_cycle_map, mat_daily_map, stock_types=None, merge_days=0, generate_semi=True, compute_shortage=True):
    """基于当前 peacock_schedule.plan_qty 与工单，重建缺料明细与齐备状态（含组件WIP抵扣）。
    合并逻辑：merge_days>0 时按 comp_code+需求日期窗聚合子件需求（见 _merge_req_lines）。"""
    from collections import defaultdict

    def _lead(matnr, qty):
        """生产提前期天数：优先 ceil(需求数量/日产量)，日产量缺失/0 时回退固定生产周期。"""
        daily = mat_daily_map.get(matnr, 0) or 0
        if daily > 0 and qty and qty > 0:
            return math.ceil(qty / daily)
        return mat_cycle_map.get(matnr, 0) or 0

    if compute_shortage:
        execute_db("DELETE FROM peacock_material_shortage")

    # 注：缺料明细关键列（work_order_id / parent_qty / schedule_id 可空化）的补建
    # 已上提到 _ensure_calc_columns()（事务外统一执行）。此处不再做 DDL ——
    # DDL 会隐式提交事务，使"删旧→建新"的原子性失效。

    # 收集所有毛需求行（排程来源 + 工单来源），统一按需求日升序后顺序扣减
    req_lines = []

    # 物料类型映射（用于 component_type 标注；半成品/产成品等有 BOM 即视为父件）
    _prod_type_rows = query_db("SELECT material_code, type FROM products")
    _type_map = {r['material_code']: (r.get('type') or '').strip() for r in _prod_type_rows}

    # 幂等修复（2026-10-08）：缺料重建的输入顺序必须确定。
    # 无序 → 同一物料多排程行的遍历顺序随机 → 动态 SEMI 行"首父胜出"的 qty 随序摆动，
    # 且消费循环(下方 comp_lines_sorted)的跨行"建议采购池"回补在 0 边界翻转 → 缺料行数 ±1。
    # 统一按 物料→交货日→开工日→序列号 稳定排序，与合并阶段口径一致。
    schedule_rows = query_db(
        "SELECT * FROM peacock_schedule WHERE is_deleted=0 "
        "ORDER BY material_code, COALESCE(batch_date_end,'99999999'), "
        "COALESCE(batch_date_start,'99999999'), aufnr"
    )
    # 日期键辅助：物料的需求日（batch_date_end 优先，否则 batch_date_start）→ 'YYYYMMDD'
    # 【P2-3 加固·2026-10-14】原实现 `s[:8] if len(s) >= 8 else s` 对 '2026-07-18' 式
    # （ISO 带横线）字符串会截出脏键 '2026-07-'：schedule_set/sid_for/seq_for/plan_qty_map
    # 的键全部失配 → 动态 SEMI"首父胜出"与跨行回补在边界翻转（即 memory 记载的
    # ":818/:841 优先级方向"类回归的根因形态）。库内虽暂无实例（排程列恒写 %Y%m%d），
    # 但 override 接口直接落库用户提交的日期串，属可复现的潜伏入口。
    # 现：紧凑 8 位数字走原快速路径（行为逐字节不变）；其余格式先过 _parse_date 归一，
    # 解析失败才回退旧截断（保证不因加固把原有键改丢）。
    def _dk(v):
        if v is None:
            return None
        if isinstance(v, (datetime, date)):
            return v.strftime('%Y%m%d')
        s = str(v).strip()
        if len(s) == 8 and s.isdigit():
            return s  # 紧凑 YYYYMMDD：与旧实现 s[:8] 完全一致
        d = _parse_date(s)
        if d is not None:
            return d.strftime('%Y%m%d')
        return s[:8] if len(s) >= 8 else s
    # 时段MRP（P1-11 修复）：同一物料可能有多条排程行（不同需求日），每条行是独立的
    # (物料, 需求日) 需求节点；plan_qty_map 必须用复合键，否则多行被单一物料键覆盖，
    # 导致 _resolve 反推父件提前期时取错生产数量（末次覆盖）。
    plan_qty_map = {(s['material_code'], _dk(s.get('batch_date_end') or s.get('batch_date_start'))):
                    float(s.get('plan_qty') or 0) for s in schedule_rows}
    schedule_set = set()
    sid_for = {}
    seq_for = {}
    mat_codes = set()
    mat_in_schedule = set()  # 已在排程结果中的物料(任意需求日)，用于展开时判断是否"已归排程结果"
    for s in schedule_rows:
        # 与 plan_qty_map(:630)、顶层展开(:876) 统一口径：batch_date_end 优先，缺失时回落 batch_date_start。
        # 不加兜底时该行键会退化成 (物料, None)，而 _explode 内部一律按 (物料, 具体需求日) 查询，
        # 两侧永远对不上 → schedule_set / sid_for / seq_for 三个索引会整体漏掉这一行。
        # 注意日期轴：这三个索引的日期是「必须到位日/交货日」，而 _explode 的 req_date 是「开工日」，
        # 二者靠 _ensure_semi_schedule 桥接（半成品排程行的 batch_date_end 被写成父件开工日）。
        k = (s['material_code'], _dk(s.get('batch_date_end') or s.get('batch_date_start')))
        schedule_set.add(k)
        sid_for[k] = s['id']
        seq_for[k] = s['aufnr']
        mat_codes.add(s['material_code'])
        mat_in_schedule.add(s['material_code'])

    # 物料名称 / 无限供应 / 简易库存映射（供动态补入排程行时计算齐备状态）
    name_map = {r['material_code']: (r.get('name') or '') for r in _prod_type_rows}
    unlimited_codes = get_unlimited_codes()
    inv_map = {m: {'total': inv_pool.get(m, 0), 'available': inv_pool.get(m, 0)} for m in inv_pool}
    seq_state = {'counter': len(schedule_rows)}
    # 生产件（成品/半成品）：需求归入排程结果，不进缺料明细
    produced_types = {'产成品', '半成品'}

    def _ensure_semi_schedule(comp_code, req, req_date, parent_code=None, drive_aufnr=None):
        """把未纳入排程的生产件（成品/半成品）动态补入排程结果，
        避免其需求被直接当作缺料写入缺料明细。按 (物料,需求日) 键避免重复。
        若 generate_semi=False（排程合并后阶段）：不新增 SEMI 行直接返回——
        SEMI 行已在合并前阶段生成，此处跳过以避免合并后数量变化导致日期错位、重复建行。"""
        if not generate_semi:
            return
        skey = (comp_code, _dk(req_date))
        if skey in schedule_set:
            return
        seq_state['counter'] += 1
        seq = str(seq_state['counter']).zfill(4)
        sched_id = gen_id()
        semi_order = {'plnum': comp_code, 'order_no': '', 'matnr': comp_code,
                      'maktx': name_map.get(comp_code, comp_code), 'gsmng': req, 'quantity': req}
        r_semi = _calc_order_readiness(semi_order, inv_map, unlimited_codes, False, None)
        has_child = 1 if comp_code in bom_map else 0
        execute_db(
            """INSERT INTO peacock_schedule
               (id, aufnr, order_no, material_code, material_name, material_type, parent_code, drive_aufnr, batch_qty, plan_qty,
                batch_date_start, batch_date_end, source, ready_status, status_description,
                ready_components, partial_components, missing_components, total_components,
                has_child_bom, schedule_sequence, calculated_at, calculated_by)
               VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)""",
            (sched_id, seq, '', comp_code, r_semi['maktx'],
             _type_map.get(comp_code, '半成品'), parent_code or '', drive_aufnr or '', req, req,
             '', _dk(req_date) or '', 'SEMI',
             r_semi['ready_status'], r_semi['status_description'], r_semi['ready_components'],
             r_semi['partial_components'], r_semi['missing_components'], r_semi['total_components'],
             has_child, seq_state['counter'], 'system')
        )
        schedule_set.add(skey)
        plan_qty_map[(comp_code, _dk(req_date))] = req  # 供后续锚点回fill计算开工日（复合键，按行自身需求日）
        sid_for[skey] = sched_id
        seq_for[skey] = seq
        mat_in_schedule.add(comp_code)  # 动态新增的半成品亦视为"已在排程结果"，避免后续父件下钻重复挂接

    # ---- 半成品排程行日期锚点回填（递归模式）----
    # 规则：
    #   1) 成品/半成品的"交货日"(batch_date_end)= 必须到位日 = min(父件开工日)；
    #      父件开工日 = 父件交货日 − 父件提前期 ceil(数量/日产量)。
    #   2) "计划排产日期"(batch_date_start) = 交货日 − 自身提前期 ceil(数量/日产量)；
    #      日产量缺失/0 时回退固定生产周期(production_cycle)。
    #   3) 递归求解：从已有锚点（销售订单交货日）出发，沿 BOM 父→子方向传播日期；
    #      带 visiting 防环（即使 BOM 存在意外环也不会死循环）。
    anchor_map = {}   # (material_code, 'YYYYMMDD') -> date(必须到位/交货日)；时段MRP 复合键（P1-11 修复）
    anchor_min = {}   # material_code -> date；该物料全部需求节点的最早日，供无自身日期的行兜底

    def _backfill_semi_dates():
        """对全部排程行（含计算中途动态新增）回填 交货日期/计划排产日期。可重复调用。
        时段MRP（P1-11 修复）：同一物料可能有多条排程行（不同需求日），每条行是独立的
        (物料, 需求日) 需求节点，必须用复合键处理，禁止用物料单一键折叠多行。

        ⚠「计划需求日期」开关 ON 时，回填日期**不得早于业务当日**（2026-10-13 回归修复）：
        一键计算时序为「规则1 归位 → 排程合并 → 缺料重建(本函数)」，本函数在归位**之后**执行，
        若按 `开工 = 交货日 − 提前期` 无条件回填，会把已归位到当日的开工日重新推回过去，
        使归位被静默回退 → 出现「delayed=1 但计划排产日期仍是过去日」的口径分裂，
        且排程合并（基于归位日期收口的同日簇）与最终落库日期不一致。
        故开关 ON 时对回填结果施加「当日」下限（等价于规则1），delayed 由归位阶段写入、此处不清。
        """
        _prd_on = False
        _biz_td = None
        try:
            _prd_on = (get_config('module_%s' % PLAN_REQ_DATE_MODULE, '0') == '1')
        except Exception:
            _prd_on = False
        # 【P0-2 · 2026-10-20】顺排模式（schedule_overdue_mode=forward_capacity）：
        # 开关 ON 时本回填的"当日下限"改为"首个可用产能日"，过期行不再全部堆到同一天开工。
        # 本回填在 _generate_semi_phase 阶段即先于归位执行，是顺排的真正落地点；
        # _enforce_schedule_dates 内的 _forward_overdue_to_capacity 仅兜底（两处共用
        # _next_capacity_day，口径逐字一致）。默认 clamp_today 时行为与历史完全相同。
        _fwd_on = False
        _fwd_daily = None
        _fwd_load = None
        if _prd_on:
            _biz_td = _biz_today()
            try:
                _fwd_on = (get_config('schedule_overdue_mode', 'clamp_today') or '') \
                    .strip() == 'forward_capacity'
            except Exception:
                _fwd_on = False
            if _fwd_on:
                _fwd_daily = _load_daily_capacity()
                _fwd_load = _build_capacity_load(_biz_td)

        cur_rows = query_db("SELECT * FROM peacock_schedule WHERE is_deleted=0")
        cur_set = set(r['material_code'] for r in cur_rows)
        # 仅保留"排程行 → 排程行"的父子关系（采购件子料非排程行，不参与日期传播）
        child_parents = defaultdict(set)  # child_code -> {parent_code, ...}
        for pcode in cur_set:
            if pcode in bom_map:
                for b in bom_map[pcode]:
                    ccode = b['comp_matnr']
                    if ccode in cur_set:
                        child_parents[ccode].add(pcode)

        # 种子：每条排程行自身的 batch_date_end/batch_date_start 即其需求日（必须到位日）。
        # anchor_map 改为 (物料, 需求日) 复合键，每个需求节点独立；
        # anchor_min 保留物料级最早日，供"无自身日期的行"回退（与修复前行为一致）。
        occ_by_mat = defaultdict(list)  # material_code -> [date, ...] 该物料出现过的需求日
        for r in cur_rows:
            d = _parse_date(r.get('batch_date_end') or r.get('batch_date_start'))
            if not d:
                continue
            k = (r['material_code'], d.strftime('%Y%m%d'))
            if k not in anchor_map or d < anchor_map[k]:
                anchor_map[k] = d
            occ_by_mat[r['material_code']].append(d)
            if r['material_code'] not in anchor_min or d < anchor_min[r['material_code']]:
                anchor_min[r['material_code']] = d

        # 递归求解（复合键）：节点 (matnr, date) 的"必须到位日" =
        #   min( 自身需求日, 各父件需求日 − 父件提前期 )。
        # 父件提前期用该父件"对应需求日"的生产数量计算（plan_qty_map 复合键，取正确数量）。
        visiting = set()  # 防环：(matnr, date)

        def _resolve(matnr, date):
            key = (matnr, date.strftime('%Y%m%d'))
            if key in anchor_map:
                return anchor_map[key]
            vkey = (matnr, date)
            if vkey in visiting:
                return None  # 环路保护：跳过，等外部锚点兜底
            visiting.add(vkey)
            cands = []
            for p in child_parents.get(matnr, ()):
                for pdate in occ_by_mat.get(p, ()):
                    p_anchor = _resolve(p, pdate)
                    if not p_anchor:
                        continue
                    pq = plan_qty_map.get((p, pdate.strftime('%Y%m%d')), 0)
                    pl = _lead(p, pq)
                    cands.append((p_anchor - timedelta(days=pl)) if pl else p_anchor)
            visiting.discard(vkey)
            if cands:
                val = min(cands)
                anchor_map[key] = val
                if matnr not in anchor_min or val < anchor_min[matnr]:
                    anchor_min[matnr] = val
                return val
            return None

        # 对每个有日期的需求节点求解（memo 化；无日期行后续用 anchor_min 兜底）
        for r in cur_rows:
            d = _parse_date(r.get('batch_date_end') or r.get('batch_date_start'))
            if d:
                _resolve(r['material_code'], d)

        # 无自身日期的行：若物料本身也无种子（纯组件、仅被父件消耗），则从其父件需求日反推。
        _SENT = date(1900, 1, 1)
        for r in cur_rows:
            m = r['material_code']
            if not (r.get('batch_date_end') or r.get('batch_date_start')) and m not in anchor_min:
                _resolve(m, _SENT)

        # 回填 DB batch_date_end（排程结果界面显示 + _get_shortage_rows 读取）
        # 有自身日期的行保留自身日期；无日期行用物料级最早日兜底。
        for r in cur_rows:
            m = r['material_code']
            if not (r.get('batch_date_end') or r.get('batch_date_start')) and m in anchor_min:
                anchor_str = anchor_min[m].strftime('%Y%m%d')
                if _prd_on and anchor_min[m] < _biz_td:
                    anchor_str = _biz_td.strftime('%Y%m%d')  # 开关 ON：交货日不得早于业务当日
                execute_db("UPDATE peacock_schedule SET batch_date_end=%s WHERE id=%s", (anchor_str, r['id']))
                r['batch_date_end'] = anchor_str  # 同步内存

        # 回填 batch_date_start（开工日 = 交货日 − 生产提前期 ceil(数量/日产量)）
        # 工单来源(WO) 行跳过：其计划排产日期固定取"工单.计划开工"，不按提前期倒推
        # 【P0-2】顺排模式逐行分配产能日，顺序即分配结果 → 必须全序（与
        # _forward_overdue_to_capacity 的 ORDER BY material_code, batch_date_start, aufnr 同键）；
        # 压当日模式下每行结果互不依赖（都去当日），排序不改变任何历史行为。
        for r in sorted(cur_rows, key=lambda x: (
                (x.get('material_code') or '').strip(),
                (x.get('batch_date_start') or ''),
                (x.get('aufnr') or ''))):
            if r.get('source') == 'WO':
                continue
            m = r['material_code']
            end_date = _parse_date(r.get('batch_date_end') or r.get('batch_date_start'))
            pq = float(r.get('plan_qty') or 0)
            if end_date and pq > 0:
                lead = _lead(m, pq)
                start_date = (end_date - timedelta(days=lead)) if lead else end_date
                if _prd_on and start_date < _biz_td:
                    # 开关 ON：开工日不得早于业务当日（防「规则1 归位」被本回填静默回退）。
                    # 【P0-2】顺排模式：改为顺延到当日之后首个可用产能日（按物料日产量），
                    # 解决"过期单一复产全部堆到同一天开工"的当日产能过载；无日产量的物料
                    # 退化为压当日（_next_capacity_day 内处理，不虚构产能数据）。
                    # 同时补齐规则1 的另两个动作，否则本回填在归位之前执行（_generate_semi_phase
                    # 阶段亦会调用本函数）会「抢跑」：start 已是当日 → 后续 _enforce_schedule_dates
                    # 的 WHERE(start<当日) 匹配不到该行 → 延误标记丢失、且 start(当日) > end(原交货日)。
                    if _fwd_on:
                        # 先剔除本行当前已占用的产能：本回填单次一键计算内会被调用多次
                        # （_generate_semi_phase 与 _compute_plan_qty 各两次），不剔除的话
                        # 第一次把自己排到 day1 后，第二次 snapshot 里 day1 已含本行 → 
                        # 「自己把自己挤走」顺漂一天，四次回填累计漂 3~4 天（产能凭空浪费）。
                        _cur = _parse_date(r.get('batch_date_start'))
                        if _cur is not None:
                            _own = _fwd_load[m].get(_cur, 0.0)
                            if _own > 0:
                                _fwd_load[m][_cur] = max(0.0, _own - pq)
                        start_date = _next_capacity_day(m, pq, _biz_td, _fwd_daily, _fwd_load)
                        _fwd_load[m][start_date] = _fwd_load[m].get(start_date, 0.0) + pq
                    else:
                        start_date = _biz_td
                    if end_date < start_date:
                        # ① 完工日(必须到位日)早于开工日 → 一并压到当日，保「开工 <= 完工」自洽
                        end_date = start_date
                        execute_db("UPDATE peacock_schedule SET batch_date_end=%s WHERE id=%s",
                                   (end_date.strftime('%Y%m%d'), r['id']))
                        r['batch_date_end'] = end_date.strftime('%Y%m%d')  # 同步内存
                    # ② 补标 delayed=1（规则1：计划日期早于当日、已被强制调整为当日）
                    execute_db("UPDATE peacock_schedule SET `delayed`=1 WHERE id=%s", (r['id'],))
                    r['delayed'] = 1
                start_str = start_date.strftime('%Y%m%d')
                execute_db("UPDATE peacock_schedule SET batch_date_start=%s WHERE id=%s", (start_str, r['id']))
                r['batch_date_start'] = start_str  # 同步内存

    # 首次回填：供 _explode 计算子件需求日期使用 anchor_map
    _backfill_semi_dates()

    def _explode(matnr, qty, level, parent_code, inherit_sid, inherit_seq, req_date, src='SCH', wo_id=None, drive_aufnr=None, ancestors=None):
        """递归展开 BOM（多级）：
        - 排程/工单来源行先展开其直接子件（一层）；
        - 生产件（成品/半成品）：需求归入"排程结果"，【不】进入"缺料明细"；
          若其尚未纳入排程则动态补入，并继续下钻取其采购件（原辅料/包材）子料；
          若已在排程结果中，则由顶层排程行统一展开（此处不重复下钻，防双计）；
        - 采购件（原辅料/包材/原材料/未分类）：进入"缺料明细"，有 BOM 且非排程行则继续下钻。

        【P1-14 修复 · 2026-10-14】新增 `ancestors` 防环（与 `_collect_semi:1851` 同构）：
        原实现无任何环检测。环状 BOM（X→Y→X）且提前期为 0 时 `child_date == req_date`，
        而 `schedule_set` 用完工日、`_explode` 用开工日 → 两者恒不等 → **无限递归**
        → RecursionError → 一键计算整体 500 回滚。菱形 BOM 还会指数级展开。
        主数据误录一条环/自环 BOM 即让一键计算完全不可用，且报错不直观。
        """
        if qty <= 0 or matnr not in bom_map:
            return
        ancestors = ancestors or frozenset()
        if matnr in ancestors:
            # 环状 BOM：停止下钻（同 _collect_semi 的处理），首次出现记一条告警
            key = (matnr, tuple(sorted(ancestors)))
            if key not in _BOM_CYCLE_REPORTED:
                _BOM_CYCLE_REPORTED.add(key)
                logging.getLogger(__name__).warning(
                    '检测到环状 BOM，已停止下钻（物料 %s，祖先链 %s）——请检查 bom 表主数据',
                    matnr, sorted(ancestors))
            return
        ancestors = ancestors | {matnr}
        skey = (matnr, _dk(req_date))
        # 【P2-18】inherit_sid / inherit_seq 是顶层入口传入的**精确**归属：
        #   - SCH/SEMI 行 → 排程行自身 id / aufnr（:883 传入）
        #   - WO 行       → wo_sched_map 按 work_order_id 精确映射（:920-921 传入）
        # 其优先级必须**高于** sid_for / seq_for——后者是按 (物料,日期) 的**猜测**，
        # 可能命中同物料的另一张单据（如另一 SCH 行），把缺料行挂错归属，导致
        # 「排程结果齐备」与「缺料明细」判定分裂。故改为：精确值优先，猜测值仅兜底。
        # 说明：sid_for 键取到位/完工日，而本函数 req_date 是开工日（完工日−提前期），
        # 提前期>0 时两者恒不等 → 顶层实际恒走精确值（实库 14 个入口命中 0 次，纯防御性）。
        cur_sid = inherit_sid or sid_for.get(skey)
        cur_seq = inherit_seq or seq_for.get(skey)
        for b in bom_map[matnr]:
            comp_code = b['comp_matnr']
            yq = float(b.get('yield_qty') or 1)
            cm = float(b.get('comp_menge') or 0)
            req = (qty / yq) * cm if yq > 0 else 0
            if req <= 0:
                continue
            comp_type = _type_map.get(comp_code, '')
            # 生产件（成品/半成品）：归排程结果，不进缺料明细
            if comp_type in produced_types:
                if comp_code in mat_in_schedule:
                    # 该生产件已作为排程行存在（任意需求日），其 BOM 由顶层排程行统一展开，
                    # 此处不重复下钻——否则会把子件需求错误地挂到「父件 schedule_id」上：
                    # 典型：C003 展开 B002 时，把 B002 的子件 M001/M003 以 material_code=B002、
                    # schedule_id=C003 写入缺料明细，造成重复且数量错误的 B002 生产需求行。
                    continue
                # 兜底：该生产件尚未纳入排程，动态补入并下钻取采购件子料
                _ensure_semi_schedule(comp_code, req, req_date, matnr, drive_aufnr)
                cyc = _lead(comp_code, req)
                child_date = (req_date - timedelta(days=cyc)) if (req_date and cyc) else req_date
                # 【勿改成 cur_sid 优先】此处**必须**保持 sid_for 优先：上方 _ensure_semi_schedule
                # 刚为 (comp_code, req_date) 动态建了 SEMI 行，并在 :691-692 写入
                # sid_for[(comp_code, _dk(req_date))] = 新行id —— 与本查询键完全相同（同一 req_date），
                # 必然命中，且命中值才是正确的归属（子件的采购件缺料应挂到该 SEMI 行，而非父件）。
                # 若改成 cur_sid or sid_for.get(...)，会让子件缺料错误地挂回父件排程行（真实回归）。
                # 与 :818 的差异：:818 顶层键用开工日、sid_for 存完工日（恒 MISS，故精确值优先安全），
                # 而此处键与 _ensure_semi_schedule 写入的键同源同值（必中，故须猜测值优先）。
                _explode(comp_code, req, level + 1, matnr,
                         sid_for.get((comp_code, _dk(req_date)), cur_sid),
                         seq_for.get((comp_code, _dk(req_date)), cur_seq),
                         child_date, src, wo_id, drive_aufnr, ancestors)
            # 采购件（原辅料/包材/原材料/未分类）：进缺料明细
            # 子件需求日期 = 父件开工日 − 子件自身提前期（与展示层 component_demand_date 完全一致：
            # 展示层 demand_date=父交货−父提前期(=本行 req_date)，component_demand_date=demand_date−子件提前期），
            # 否则在途时间窗(plan_arrival_date<=子件需求日期)判定会与展示层错位（问题2根因）。
            comp_cycle = _lead(comp_code, req)
            comp_req_date = (req_date - timedelta(days=comp_cycle)) if (req_date and comp_cycle) else req_date
            req_lines.append({
                'source': src, 'schedule_id': cur_sid, 'work_order_id': wo_id, 'aufnr': cur_seq,
                'matnr': matnr, 'comp_code': comp_code, 'comp_name': b.get('comp_maktx') or '',
                'gross': req, 'req_date': comp_req_date,
                'bom_level': level, 'parent_code': matnr,
                'parent_qty': qty,  # 直接父件(本层 matnr)的生产量；"产品需求"列取值，与 BOM 层级无关
                'is_parent_material': 1 if comp_code in bom_map else 0,
            })
            # 子件自身有 BOM 且非独立排程行 → 继续下钻
            if (comp_code, _dk(req_date)) not in schedule_set and comp_code in bom_map:
                cyc = _lead(comp_code, req)
                child_date = (req_date - timedelta(days=cyc)) if (req_date and cyc) else req_date
                _explode(comp_code, req, level + 1, matnr, cur_sid, cur_seq, child_date, src, wo_id, drive_aufnr, ancestors)

    # 排程来源（销售订单/半成品驱动的排产）——递归展开多级 BOM
    # 注意：source='WO' 的工单排程行已在下方「独立工单」循环中单独展开，
    # 若此处不跳过，同一 WO 会被当成 SCH 再展一次，导致同一子件需求日期出现 source='SCH'+'WO' 两条重复。
    for s in schedule_rows:
        if s.get('source') == 'WO':
            continue
        matnr = s.get('material_code', '')
        sid = s['id']
        seq = s['aufnr']
        # 展开数量用 batch_qty（本排程行实际生产量），而非 plan_qty：
        # plan_qty 是「净生产量」(已扣成品库存/在制)，为 0 不代表不需要原材料；
        # 排程行代表「要生产 batch_qty 件」，其 BOM 子件需求必须进缺料明细（否则工单不进缺料）。
        qty = float(s.get('batch_qty') or 0)
        if qty <= 0:
            continue
        cycle = _lead(matnr, qty)
        # 时段MRP：同一物料不同需求日已拆成独立排程行，必须用本行自身的需求日期（不能用折叠的 anchor_min）
        pdd = _parse_date(s.get('batch_date_end') or s.get('batch_date_start')) or anchor_min.get(matnr)
        req_date = (pdd - timedelta(days=cycle)) if (pdd and cycle) else pdd
        _explode(matnr, qty, 0, '', sid, seq, req_date, 'SCH', None, seq)

    # 工单来源（独立/备货工单）
    # 【业务规则·独立工单】批准/执行状态的 WO 工单视为「独立生产任务」，其 BOM 组件需求
    # 必须单独进缺料明细，不能因"同物料已存在排程行(SCH/SEMI/WO)"而跳过——
    # 旧逻辑 `if matnr in mat_codes: continue` 会导致 WO 原料永久缺失（approved/executing WO 的
    # matnr 必在 mat_codes，故 WO 展开被完全跳过）。现按业务规则展开。
    # 不产生重复统计说明：SCH 的 batch_qty 已在 calculate_readiness 中扣减过 WO 在制(WIP)
    # （wo_supply 释放逻辑），故"单独展开 WO"与"SCH 展开"合计原料 =「未发-库存」的正确总量，
    # 二者分别列示、互不重叠。
    # 关联：把 WO 缺料行挂到对应 peacock_schedule 行（schedule_id），使"排程结果齐备"
    # 与"缺料明细"采用同一套短缺判定（问题1/2 统一原则），杜绝"WO 排程齐备/其缺料明细缺料"分裂。
    wo_sched_map = {}
    for s in schedule_rows:
        # 排程合并后锚点行可能 carry 多个 WO 的 work_order_id（逗号分隔），逐一拆分映射，
        # 保证每个独立工单的缺料行都能挂回合并锚点（不破坏"独立工单进缺料"铁律）
        if s.get('work_order_id'):
            for _wid in str(s['work_order_id']).split(','):
                _wid = _wid.strip()
                if _wid:
                    wo_sched_map[_wid] = s['id']
    # 幂等修复（2026-10-08）：工单来源行的遍历顺序亦须确定（与 schedule_rows 同因），否则
    # 动态 SEMI 行"首父胜出"的 qty 及消费循环的回补边界会随序摆动 → 缺料行数非幂等。
    wo_rows = query_db("""
        SELECT id, order_no, material_code, COALESCE(confirm_qty,0) as confirm_qty,
               COALESCE(completed_qty,0) as completed_qty, planned_start, planned_end
        FROM work_orders WHERE status IN ('批准','执行') ORDER BY id
    """)
    for wo in wo_rows:
        matnr = wo.get('material_code', '')
        effective_qty = float(wo.get('confirm_qty') or 0) - float(wo.get('completed_qty') or 0)
        if effective_qty <= 0 or matnr not in bom_map:
            continue
        wo_no = wo.get('order_no', '') or wo['id']
        # 🔴 【P1-1·有意为之·2026-10-14 用户拍板方案B】此处的 cycle 扣减是**故意保留**的：
        #   req_date = 计划开工日 − 父件自身生产周期，再由 _explode(:1025) 减子件周期
        #   ⇒ WO 组件需求日 = 计划开工 − 父周期 − 子周期（比 SCH 的"交货−父−子"口径
        #     在 WO 路径上多保守一个父周期）。文档 §4「工单来源」已按此实际公式修订，
        #     **不要再当缺陷"修"掉**；如业务要改保守度，应调 products 提前期参数。
        # （历史：2026-10-13 审查曾提"改代码删 cycle 对齐 SCH"，用户拍板保留现状。）
        cycle = _lead(matnr, effective_qty)
        # 组件需求日按工单「计划开工」倒推（用户决策 2026-10-02）：组件须在父件开工前到位；
        # 优先取 planned_start，缺则回退 planned_end 兜底，避免 None 导致炸不出需求。
        pdd = _parse_date(wo.get('planned_start') or wo.get('planned_end'))
        req_date = (pdd - timedelta(days=cycle)) if (pdd and cycle) else pdd
        # 挂到对应排程行（若有），保持"排程结果齐备=缺料明细"口径一致；无则留 NULL（旧行为兜底）
        cur_sid = wo_sched_map.get(wo['id'])
        _explode(matnr, effective_qty, 0, '', cur_sid, wo_no, req_date, 'WO', wo['id'], wo_no)

    # 二次回填：覆盖计算中途由 _ensure_semi_schedule 动态新增的半成品排程行（其日期在首次回填时尚不存在）
    _backfill_semi_dates()

    # 仅生成 SEMI 阶段（排程合并前，compute_shortage=False）：SEMI 行已生成 + 日期已回填，
    # 到此返回，不计算缺料明细/齐备状态；缺料明细将在合并后阶段基于"合并后排程结果"统一计算。
    if not compute_shortage:
        return

    # 按需求日升序（NULL 排最后）；同日按 排程优先、再按序列号，保证确定性
    req_lines.sort(key=lambda x: (x['req_date'] or date.max, 0 if x['source'] == 'SCH' else 1, str(x['aufnr'])))

    # 单据号映射：合并行"来源"展示实际单据号（销售订单号-行号 / 工单号 / SEMI驱动订单号），而非类型码 'WO,SCH'
    _sched_ref = {}
    for _r in query_db("SELECT id, source, order_no, sales_posnr, drive_aufnr, material_code, aufnr FROM peacock_schedule WHERE is_deleted=0"):
        _ono = (_r.get('order_no') or '').strip()
        _pos = (_r.get('sales_posnr') or '').strip()
        if _ono:
            _sched_ref[_r['id']] = (f"{_ono}-{_pos}" if (_ono and _pos) else _ono)
        elif (_r.get('source') or '').strip() == 'SEMI':
            # SEMI 行无销售订单号：用"顶层驱动订单号"展示，才能知道该半成品为谁准备
            _sched_ref[_r['id']] = (_r.get('drive_aufnr') or '').strip() \
                or (_r.get('material_code') or '').strip()
    _wo_ref = {}
    for _r in query_db("SELECT id, order_no FROM work_orders"):
        _wo_ref[_r['id']] = (_r.get('order_no') or '').strip()

    # ── 规则2（缺料归位，仅开关 ON）：把过期子件需求日期压到当日，并标 delayed=1 ──
    # 在内存中完成，位于缺料合并之前（先归位 → 再合并），归位产生的"同日簇"被下方合并收口；
    # 同时让内联可用量算法（下方 1047 起）直接基于"已归位日期"推算，数量口径与归位同源、无需事后重算。
    # 延误保留铁律：无论怎么合并，段内任一原始行 delayed=1 → 合并行 delayed=1（见 _merge_req_lines 取 OR）。
    # 开关关闭（默认）时整段跳过，行为与历史完全一致。
    # 🔧【2026-10-15】_biz_td 提到外层：下方「无条件延误标记循环」开关 OFF 时也要用今日。
    _biz_td = _biz_today()
    _plan_req_on = (get_config('module_%s' % PLAN_REQ_DATE_MODULE, '0') == '1')
    if _plan_req_on:
        for _rl in req_lines:
            if (_rl.get('source') or '') == 'WO':
                # WO 独立任务：日期不被「计划需求日期」改写（与 _enforce_schedule_dates 的 _WO_EXCL
                # 一致）；其延误标记交给下方「无条件标记循环」按子件需求日判定。
                continue
            _rd = _rl.get('req_date')
            _rdd = _parse_date(_rd) if _rd is not None else None
            if _rdd is not None and _rdd < _biz_td:
                _rl['req_date'] = _biz_td        # 压当日
                _rl['delayed'] = 1
            else:
                _rl['delayed'] = _rl.get('delayed', 0)

    # ── 延误标记（无条件，2026-10-15 设计）──────────────────────────────
    # 把「子件需求日期 < 今日」的行标 delayed=1（只置 1，不清 0，OR 语义）。
    # 与「日期改写」解耦：无论「计划需求日期」开关是否开启，过期缺料行都必须可见（红点）。
    # 位置必须在「缺料合并」之前：_merge_req_lines 按段内 OR 传递 delayed，先标后合，合并行才带得上红点。
    # 本循环位于 _rebuild_all_shortages 内部 → 一键计算 / 重新计算缺料 / 手动覆盖 / 齐备套计算
    # 四个入口自动全覆盖，无需额外调用点。
    # 被规则2 压到当日的行 req_date=当日，本循环匹配不到，其红点由规则2 保留（OR 语义不冲突）。
    for _rl in req_lines:
        if _rl.get('delayed', 0):
            continue                              # 已标则跳过，避免重复解析
        _rdd = _parse_date(_rl.get('req_date')) if _rl.get('req_date') is not None else None
        if _rdd is not None and _rdd < _biz_td:
            _rl['delayed'] = 1

    # 缺料合并（概念1 + 概念2）：
    #  概念1（开关 ON）：精确同日合并（diff==0），收口归位产生的"同日簇"（与排程侧一致）；
    #  概念2（缺料合并天数>0）：按需求日期窗合并。
    # merge_days>0 时概念2 已包含概念1（窗>=1 天必含同日）；仅 merge_days<=0 且开关 ON 时概念1 单独生效。
    #
    # 🔴【P2-2 修复 · 2026-10-14】原为两个 `if` **串行**：归位 ON **且** merge_days>0 时
    # 同一批行被合并两次。第二次进来时 `source` 已是第一次拼出的类型码（如 'SCH,WO'），
    # 而 `_doc_ref()` 靠 `source == 'WO'/'SCH'/'SEMI'` 分支取单据号 → 全部落空、
    # 退化返回类型码本身 → 缺料明细「来源」列显示 'SCH,WO' 而不是实际单据号，追溯链断裂。
    # 现改为互斥：merge_days>0 走窗口合并（已含同日），否则开关 ON 时才走同日合并。
    if merge_days > 0:
        req_lines = _merge_req_lines(req_lines, merge_days, _sched_ref, _wo_ref)
    elif _plan_req_on:
        req_lines = _merge_req_lines(req_lines, 0, _sched_ref, _wo_ref, same_day=True)

    # 统一顺序扣减：与展示层 _apply_inventory_and_transit 完全一致的逻辑
    # （逐组件、按子件需求日期升序、采购在途按"到货日期<=子件需求日期"时间窗计入、先耗实物再耗在途、
    #  建议采购富余回补后续行、不扣组件WIP）。目的：消除"存储层(排程结果齐备)"与"展示层(缺料明细)"
    # 两套口径分裂（问题1、2）。
    _transit_on = bool(stock_types) and 'transit' in stock_types
    _transit_pools_all = _load_transit_pools() if _transit_on else {}
    # 【P0-3 · 2026-10-20】在途匹配注释池：与计算用的 _transit_pools_all **相互独立**。
    # 无论用户是否把「在途」勾进可用库存，都加载一份用于「在途到货日 vs 需求日」匹配标注
    # （可见性功能：避免在途明明能赶上却重复下单）。刻意不复用 _transit_pools_all：
    # 勾选在途时该池会被逐行耗用递减，直接拿来标注会得到"越往后在途越少"的失真值。
    _intransit_pools = _load_transit_pools()
    lot_rules = _load_product_lot_rules()
    _by_comp = defaultdict(list)
    for line in req_lines:
        _by_comp[line['comp_code']].append(line)

    for comp_code, comp_lines in _by_comp.items():
        # 实物库存（不含在途，已在调用方剥离 transit）
        stock = float(inv_pool.get(comp_code, 0))
        full_stock = stock
        suggested_pool = 0.0
        buckets = _transit_pools_all.get(comp_code)
        bi = 0
        transit_pool = 0.0
        # 幂等修复（2026-10-08）：消费循环的逐行顺序必须是【全序】，否则同 (需求日, 序列号) 的两条
        # 缺料行靠"插入序"兜底，导致跨行"建议采购池"回补量在 0 边界翻转 → 缺料行数 ±1（18↔17）。
        # 现以 需求日→来源→序列号→父物料→子件→需求量→行主键 作为全序键（schedule_id 为 uuid，唯一，
        # 保证任意两条不同行都能被确定地区分，彻底消除对插入序的依赖）。
        # 【P1-13】改用与展示层共用的 `_consume_sort_key()`（键序逐字一致）
        comp_lines_sorted = sorted(
            comp_lines,
            key=lambda x: _consume_sort_key(
                x.get('req_date'), x.get('source'), x.get('aufnr'),
                x.get('matnr'), x.get('comp_code'),
                x.get('gross'), x.get('schedule_id'))
        )
        for line in comp_lines_sorted:
            d = line.get('req_date')
            dd_line = _parse_date(d)
            newly_available = 0.0
            if buckets:
                while bi < len(buckets):
                    bd, bq = buckets[bi]
                    if bq <= 0:
                        bi += 1
                        continue
                    if bd is not None and dd_line is not None and bd <= dd_line:
                        transit_pool += bq
                        newly_available += bq
                        bi += 1
                    else:
                        break
            req = float(line['gross'])  # 与展示层一致：用 gross，不扣组件WIP
            avail = (stock + transit_pool + suggested_pool) if _transit_on else (stock + suggested_pool)
            # 递归耗用：先耗实物、再耗在途、再耗建议采购池
            consume = req
            s_used = min(consume, stock); stock -= s_used; consume -= s_used
            t_used = min(consume, transit_pool); transit_pool -= t_used; consume -= t_used
            sp_used = min(consume, suggested_pool); suggested_pool -= sp_used; consume -= sp_used
            # 状态判定（与展示层 _apply_inventory_and_transit 完全一致）
            buy = _net_need(req, avail)
            if buy == 0:
                shortage_type = 'OK'
            elif buy < req:
                shortage_type = 'PARTIAL'
            else:
                shortage_type = 'MISSING'
            stock_status = {'OK': '正常', 'PARTIAL': '部分', 'MISSING': '缺料'}[shortage_type]
            comp_name = line['comp_name']
            comp_type = _type_map.get(comp_code, '')
            # 【P0-1 · 2026-10-20】建议采购量提前到 INSERT 之前计算（原实现放在 INSERT 之后，
            # 导致建议采购量只进"建议采购池"却从不落库，缺料明细表/导出拿不到该值）。
            # 计算口径与原实现逐字一致：_apply_lot_sizing(buy, 最小采购量, 包装量)。
            min_q, batch_sz = lot_rules.get(comp_code, (0.0, 0.0))
            suggested = _apply_lot_sizing(buy, min_q, batch_sz)
            # 最迟下单日 = 子件需求日期 − 采购提前期（提前期口径与需求日推导一致：
            # ceil(数量/日产量)，日产量缺失/0 回退 products.production_cycle）。
            # 仅对有缺口行计算：无缺口行无可采购事项，展示过去的下单日只会造成误读
            # （实测 OK 行曾显示 2026-05-28 这类需求日−标准周期的过去日期）。
            _rd = line.get('req_date')
            _rd = _rd if isinstance(_rd, date) else _parse_date(_rd)
            _cdaily = float(mat_daily_map.get(comp_code, 0) or 0)
            if buy > 0:
                if _cdaily > 0:
                    _clead = int(math.ceil(buy / _cdaily))
                else:
                    _clead = int(mat_cycle_map.get(comp_code, 0) or 0)
                if _rd is not None and _clead > 0:
                    _latest_str = (_rd - timedelta(days=_clead)).strftime('%Y-%m-%d')
                else:
                    _latest_str = _rd.strftime('%Y-%m-%d') if _rd is not None else None
            else:
                _latest_str = None
            # 【P0-3 · 2026-10-20】在途匹配标注（纯注释，不参与数量计算）：
            #   按到货日期升序累计在途量，取「累计 ≥ 本行缺口」的最早到货日与需求日比较。
            _it_buckets = _intransit_pools.get(comp_code) or []
            _it_qty = sum(float(bq or 0) for _bd, bq in _it_buckets)
            _it_dates = [bd for bd, bq in _it_buckets if bd is not None]
            _it_date_str = min(_it_dates).strftime('%Y-%m-%d') if _it_dates else None
            _cum = 0.0
            _cover_date = None
            for bd, bq in sorted(_it_buckets, key=lambda x: (x[0] or date.max)):
                if bq is None or bq <= 0:
                    continue
                _cum += float(bq)
                if _cum >= buy - MRP_QTY_EPS and bd is not None:
                    _cover_date = bd
                    break
            if buy <= 0:
                _it_match = '无缺口'
            elif _cover_date is not None and _rd is not None and _cover_date <= _rd:
                _it_match = '可赶上'
            elif _it_qty > 0:
                _it_match = '晚于需求'
            else:
                _it_match = '无在途'
            execute_db(
                # 【P2-1 修复 · 2026-10-14】补写 `parent_qty`：该列在 `_explode` 里已产出
                # （= 直接父件本层生产量）、`_migrate()` 也已建列，但 INSERT **从未写入**
                # → 库内恒 NULL → 缺料明细「产品需求」列只能回退显示 plan_qty，
                # 多级 BOM 下显示的是顶层计划量而非本层父件量（`:3165-3168` 的修复目标落空）。
                # 【P0/P1 · 2026-10-20】补写 shortage_qty / suggested_purchase_qty /
                # latest_order_date / intransit_* / member_count / merge_members（见上方注释）。
                "INSERT INTO peacock_material_shortage "
                "(id, schedule_id, work_order_id, aufnr, source, source_refs, material_code, component_code, component_name, "
                " component_type, required_qty, available_qty, current_stock, shortage_type, stock_status, "
                " `delayed`, "
                " is_parent_material, parent_code, parent_qty, bom_level, component_demand_date, created_at, updated_at, "
                " shortage_qty, suggested_purchase_qty, latest_order_date, "
                " intransit_qty, intransit_arrival_date, intransit_match, member_count, merge_members) "
                "VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),NOW(),"
                "%s,%s,%s,%s,%s,%s,%s,%s)",
                (gen_id(), line['schedule_id'], line['work_order_id'], line['aufnr'], line['source'],
                 line.get('source_refs') or '', line['matnr'], comp_code, comp_name, comp_type, req, avail, full_stock,
                 shortage_type, stock_status,
                 int(line.get('delayed', 0) or 0),
                 1 if line.get('is_parent_material') else 0, line.get('parent_code', ''),
                 float(line.get('parent_qty') or 0), line.get('bom_level', 0),
                 line.get('req_date'),
                 buy, suggested, _latest_str,
                 _it_qty, _it_date_str, _it_match,
                 int(line.get('member_count') or 1), line.get('merge_members') or '')
            )
            # 建议采购富余回补后续行（与展示层一致）；suggested 已提前计算，此处只回补富余量
            if buy > 0:
                suggested_pool += (suggested - buy)
        # 实物剩余持久化（在途不计入净库存，与展示层一致）
        inv_pool[comp_code] = stock

    # 持久化 MRP 净剩余库存（供发货计划 ATP 复用，保证与缺料表口径一致）
    # 注：mrp_net_inventory 建表已上提到 _ensure_calc_columns()（事务外统一执行）。
    for mat, avail in inv_pool.items():
        execute_db("REPLACE INTO mrp_net_inventory (material_code, available, updated_at) VALUES (%s,%s,NOW())",
                   (mat, avail))

    if merge_days > 0:
        # 合并模式：缺料明细已按 comp_code+需求日期窗聚合，schedule_id 关联断裂，
        # 排程行 A/B/C 齐备状态失去参考意义（与前端"缺料合并提醒"口径一致）→ 统一置"缺料合并模式，不统计"。
        execute_db(
            "UPDATE peacock_schedule SET ready_status='缺料合并模式', status_description='不统计', "
            "ready_components=0, partial_components=0, missing_components=0 WHERE is_deleted=0"
        )
    else:
        # 基于缺料明细重算排程齐备状态：直接采用缺料明细已计算的 shortage_type
        # （与展示层 _apply_inventory_and_transit 同源），保证"排程结果齐备"与"缺料明细"完全一致。
        shortage_rows = query_db("""
            SELECT ms.schedule_id, ms.shortage_type as comp_status
            FROM peacock_material_shortage ms
            JOIN peacock_schedule s ON ms.schedule_id = s.id AND s.is_deleted=0
            WHERE ms.required_qty > 0
        """)
        sch_status = defaultdict(lambda: {'total': 0, 'ready': 0, 'partial': 0, 'missing': 0})
        for sr in shortage_rows:
            sid = sr['schedule_id']
            sch_status[sid]['total'] += 1
            st = sr['comp_status']
            if st == 'OK':
                sch_status[sid]['ready'] += 1
            elif st == 'PARTIAL':
                sch_status[sid]['partial'] += 1
            else:
                sch_status[sid]['missing'] += 1

        for sid, counts in sch_status.items():
            if counts['missing'] == 0 and counts['partial'] == 0:
                rs, sd = 'A', '齐备'
            elif counts['missing'] == 0:
                rs, sd = 'B', '部分齐备'
            else:
                rs, sd = 'C', '缺料'
            execute_db(
                "UPDATE peacock_schedule SET ready_status=%s, status_description=%s, ready_components=%s, partial_components=%s, missing_components=%s WHERE id=%s",
                (rs, sd, counts['ready'], counts['partial'], counts['missing'], sid)
            )
        # 排产量=0 且无缺料行的行：强制齐备（有缺料行的行保留上面基于缺料明细的重算结果，避免与缺料明细矛盾）
        execute_db("""
            UPDATE peacock_schedule s
            SET s.ready_status='A', s.status_description='齐备',
                s.ready_components = s.total_components, s.partial_components=0, s.missing_components=0
            WHERE s.is_deleted=0 AND COALESCE(s.plan_qty,0)=0
              AND NOT EXISTS (SELECT 1 FROM peacock_material_shortage ms WHERE ms.schedule_id = s.id)
        """)


def _generate_semi_phase(stock_types=None):
    """排程合并前阶段：仅生成半成品(SEMI)排程行并回填日期，不计算缺料明细。

    目的：使"排程合并"能按相同物料折叠 SCH+SEMI（用户指定顺序：排程结果→排程合并→缺料明细）。
    若跳过此阶段，SEMI 行要到"缺料明细"阶段才由 BOM 展开生成，彼时已在排程合并之后，
    SEMI 将永远无法被合并。

    内部调用 _rebuild_all_shortages(generate_semi=True, compute_shortage=False)：
    - generate_semi=True → 动态生成 SEMI 行；
    - compute_shortage=False → 生成完 SEMI + 回填日期后即返回，不删/不写 peacock_material_shortage。
    """
    if stock_types is None:
        stock_types = ['unrestricted']
    bom_rows = query_db("SELECT parent_matnr, comp_matnr, comp_maktx, comp_menge, yield_qty FROM bom")
    bom_map = {}
    for b in bom_rows:
        bom_map.setdefault(b['parent_matnr'], []).append(b)
    net_stock = [t for t in stock_types if t != 'transit']
    inv_pool = build_inventory_pool(net_stock)
    mat_cycle_map, mat_daily_map = _prepare_shortage_context(bom_map)
    _rebuild_all_shortages(inv_pool, bom_map, mat_cycle_map, mat_daily_map,
                           stock_types, merge_days=0, generate_semi=True, compute_shortage=False)


def _calc_order_readiness(order, inv_map, unlimited_codes, calc_actual_required, enabled_stock_types=None):
    """
    计算单个订单的齐备套状态。
    需求数量 = (order_qty / yield_qty) * comp_menge
    当前库存 = sap_labst + sap_insme + sap_speme + sap_umlme
    可用库存 = 按 enabled_stock_types 动态求和
    """
    plnum = order.get('plnum') or order.get('order_no') or order.get('vbeln') or ''
    matnr = order.get('matnr') or order.get('material_code') or ''
    maktx = order.get('maktx') or order.get('material_desc') or order.get('name') or ''
    order_qty = float(order.get('gsmng') or order.get('plan_qty') or order.get('kwmeng') or order.get('quantity') or 0)

    if enabled_stock_types is None:
        enabled_stock_types = ['unrestricted']

    # 获取子件：优先 BOM（含 yield_qty），其次 sap_planned_order_items
    items = []
    yield_qty = 1.0
    if matnr:
        bom_rows = query_db(
            "SELECT comp_matnr as matnr, comp_maktx as maktx, comp_menge, "
            "comp_meins as meins, postp, lgort, yield_qty "
            "FROM bom WHERE parent_matnr=%s",
            (matnr,)
        )
        if bom_rows:
            items = bom_rows
            yield_qty = float(bom_rows[0].get('yield_qty') or 1)
            if yield_qty <= 0:
                yield_qty = 1

    if not items:
        items = query_db("SELECT * FROM sap_planned_order_items WHERE plnum=%s", (plnum,))

    # 如果子件为空，把自身当子件
    if not items:
        items = [{'matnr': matnr, 'maktx': maktx, 'comp_menge': order_qty,
                  'meins': order.get('unit') or order.get('meins') or ''}]

    ready_count = 0
    partial_count = 0
    missing_count = 0
    component_details = []

    for item in items:
        comp_matnr = item.get('matnr')
        comp_menge = float(item.get('comp_menge') or item.get('bdmng') or 0)

        # 需求数量 = (order_qty / yield_qty) * comp_menge
        required_qty = (order_qty / yield_qty) * comp_menge if yield_qty > 0 else comp_menge
        if required_qty == 0:
            required_qty = order_qty

        # 当前库存 = sap_labst + sap_insme + sap_speme + sap_umlme (全部)
        inv = inv_map.get(comp_matnr, {})
        current_stock = inv.get('total', 0)
        # 可用库存 = 按库存类型过滤求和
        available_stock = inv.get('available', current_stock)

        if comp_matnr in unlimited_codes:
            status = 'READY'
            current_stock = max(required_qty, current_stock)
            available_stock = max(required_qty, available_stock)
            ready_count += 1
        else:
            if available_stock >= required_qty:
                status = 'READY'
                ready_count += 1
            elif available_stock > 0:
                status = 'PARTIAL'
                partial_count += 1
            else:
                status = 'MISSING'
                missing_count += 1

        remaining = current_stock - required_qty
        actual_required = max(0, required_qty - available_stock) if calc_actual_required else required_qty

        component_details.append({
            'component_code': comp_matnr,
            'component_name': item.get('maktx') or item.get('comp_maktx') or '',
            'required_qty': required_qty,
            'available_qty': available_stock,
            'current_stock': current_stock,
            'sap_labst': inv.get('sap_labst', 0),
            'sap_insme': inv.get('sap_insme', 0),
            'sap_speme': inv.get('sap_speme', 0),
            'sap_umlme': inv.get('sap_umlme', 0),
            'remaining': remaining,
            'actual_required_qty': actual_required,
            'status': status,
            'unit': item.get('meins') or item.get('comp_meins') or '',
        })

    if missing_count == 0 and partial_count == 0:
        ready_status = 'A'
        status_desc = '齐备'
    elif missing_count == 0:
        ready_status = 'B'
        status_desc = '部分齐备'
    else:
        ready_status = 'C'
        status_desc = '缺料'

    return {
        'plnum': plnum,
        'matnr': matnr,
        'maktx': maktx,
        'gsmng': order_qty,
        'ready_status': ready_status,
        'status_description': status_desc,
        'ready_components': ready_count,
        'partial_components': partial_count,
        'missing_components': missing_count,
        'total_components': len(items),
        'components': component_details,
    }


def _normalize_sales_posnr():
    """销售明细行号(posnr)为空的，按 vbeln 递增补顺序号。

    这样排程结果的「计划订单号」才能区分为 XS001-1 / XS001-2，
    否则空行号会被拼成裸的 'XS001'，无法判断来自第几行。幂等：只在行号为空时回填。
    """
    rows = query_db("SELECT id, vbeln, posnr FROM sap_sales_order_items ORDER BY vbeln, id")
    by_vbeln = {}
    for r in rows:
        by_vbeln.setdefault(r.get('vbeln') or '', []).append(r)
    for vbeln, items in by_vbeln.items():
        max_n = 0
        for it in items:
            p = (it.get('posnr') or '').strip()
            if p.isdigit():
                max_n = max(max_n, int(p))
        next_n = max_n + 1
        for it in items:
            if not (it.get('posnr') or '').strip():
                execute_db("UPDATE sap_sales_order_items SET posnr=%s WHERE id=%s", (str(next_n), it['id']))
                next_n += 1


def _ensure_calc_columns():
    """幂等确保一键计算所需的列/表存在（DDL）。

    ⚠️ **必须在事务之外调用**：MySQL 的 DDL 会隐式提交当前事务且不可回滚。
    一键计算的 DML 已整体包进事务，任何混在其中的 DDL 都会在事务中途把前面的 DML
    悄悄提交掉，使"删旧→建新"的原子性失效。故原先散落在 `_merge_schedule_rows`、
    `_rebuild_all_shortages`、`_get_shortage_rows` 里的 DDL 全部上提到本函数，
    由 `peacock_calculate` 在**开启事务之前**统一执行一次。

    若在事务内被调用（如 peacock_calculate 内部再调 calculate_readiness），
    直接跳过 —— 此刻前置步骤已完成，且 DDL 在事务内执行有害。
    """
    if in_transaction():
        return

    # 1) peacock_schedule：work_order_id / source_refs
    sch_cols = {r['Field'] for r in query_db("SHOW COLUMNS FROM peacock_schedule")}
    if 'work_order_id' not in sch_cols:
        execute_ddl("ALTER TABLE peacock_schedule ADD COLUMN work_order_id VARCHAR(255) NULL")
    if 'source_refs' not in sch_cols:
        execute_ddl("ALTER TABLE peacock_schedule ADD COLUMN source_refs TEXT NULL")

    # 2) peacock_material_shortage：work_order_id / parent_qty；schedule_id 可空化
    sh_cols = {r['Field'] for r in query_db("SHOW COLUMNS FROM peacock_material_shortage")}
    if 'work_order_id' not in sh_cols:
        execute_ddl("ALTER TABLE peacock_material_shortage ADD COLUMN work_order_id VARCHAR(64) NULL")
    if 'parent_qty' not in sh_cols:
        execute_ddl("ALTER TABLE peacock_material_shortage ADD COLUMN parent_qty DECIMAL(18,3) NULL")
    sid = query_db("SHOW COLUMNS FROM peacock_material_shortage LIKE 'schedule_id'")
    if sid and (sid[0].get('Null') or 'YES').upper() == 'NO':
        execute_ddl("ALTER TABLE peacock_material_shortage MODIFY COLUMN schedule_id VARCHAR(100) NULL")

    # 3) MRP 净库存表（供发货计划 ATP 复用，与缺料表口径一致）
    execute_ddl("CREATE TABLE IF NOT EXISTS mrp_net_inventory "
                "(material_code VARCHAR(64) PRIMARY KEY, available DECIMAL(18,3) DEFAULT 0, "
                "updated_at DATETIME)")


@bp.route('/api/calculate/readiness', methods=['POST'])
def calculate_readiness(_skip_post_rebuild=False):
    """齐备套计算 — 支持多数据源

    【P0-1 修复 · 2026-10-14】本处理器会 `DELETE FROM peacock_material_shortage`，
    原实现**删完就返回、从不重建** → 用户单独点「📊 齐备套计算」后缺料明细永久变空
    （统计归 0、导出报"无数据"），必须再跑一次「一键计算」才恢复。
    现于末尾补做「排程合并 + 缺料重建」，使本按钮语义自洽。

    【P1-10 修复】原实现从不读取 `schedule_merge_days`，导致单独点本按钮时
    「排程合并天数」输入框静默失效（走一键计算才生效）。现读取并调用 `_merge_schedule_rows`。

    参数 `_skip_post_rebuild`：仅供 `peacock_calculate` 内部调用时传 True ——
    它随后会自己做合并与缺料重建，避免同一事务内重复计算（显式参数，不靠
    request.get_json() 同一 dict 隐式通信，见 P3 建议）。
    """
    data = request.get_json() or {}
    plan_order_nums = data.get('plan_order_nums', [])
    limit = data.get('limit', 100)
    enabled_warehouses = data.get('enabled_warehouses', [])
    calc_actual_required = data.get('calc_actual_required', False)
    enabled_stock_types = data.get('enabled_stock_types', ['unrestricted'])
    merge_days = _as_int(data.get('merge_days'), 0, 0, 365)

    _ensure_calc_columns()
    try:
        with transaction():
            inv_map = get_inventory_map(enabled_warehouses, enabled_stock_types)
            unlimited_codes = get_unlimited_codes()

            orders = []
            source_label = ''

            from datetime import datetime, date, timedelta
            # ============ 新逻辑：工单优先 + 销售订单净追加 ============
            # 数据源 1: 生产工单（批准/执行）—— 优先作为主生产计划纳入排程与缺料计算
            wo_rows = query_db(
                "SELECT * FROM work_orders WHERE status IN ('批准','执行') ORDER BY planned_end, order_no"
            )
            for wo in wo_rows:
                pe = wo.get('planned_end')
                pe_str = ''
                if pe:
                    try:
                        pe_str = pe.strftime('%Y%m%d')
                    except Exception:
                        pe_str = ''
                ps = wo.get('planned_start')
                ps_str = ''
                if ps:
                    try:
                        ps_str = ps.strftime('%Y%m%d')
                    except Exception:
                        ps_str = ''
                # 工单未完工数量 = 已确认数量 - 已完工数量（按"仍需生产"口径纳入排程）
                # 【2026-09-01 修复 P2-14】加 max(0) 保护：超产完工(completed > confirm)时为负数，
                # 会在 remain[matnr] += pq 处反向扣减库存池，导致销售净需求被高估。
                # 同文件 :860/:1753 的同类计算已有 <=0 保护，此处遗漏。
                wo_unfinished = max(0.0, float(wo.get('confirm_qty') or 0) - float(wo.get('completed_qty') or 0))
                wo_no = wo.get('order_no') or wo.get('sap_aufnr') or ''
                orders.append({
                    'plnum': wo_no,
                    'order_no': wo_no,
                    'matnr': wo.get('material_code') or wo.get('sap_matnr') or '',
                    'maktx': wo.get('material_desc') or wo.get('sap_maktx') or '',
                    'gsmng': wo_unfinished,
                    'meins': wo.get('unit') or wo.get('sap_meins') or '',
                    'quantity': wo_unfinished,
                    'src': 'WO',
                    'aufnr': wo_no,          # 计划订单号 = 工单.工单号
                    'work_order_id': wo.get('id'),  # 工单主键，用于把 WO 缺料行关联到对应排程行
                    'merge_start': ps_str,   # 计划排产日期 = 工单.计划开工
                    'merge_end': pe_str,
                })
            if orders:
                source_label = 'work_orders'

            # 数据源 2: 销售订单 —— 对比「工单计划完工(带时间窗) + 可用库存」后，仅追加缺口部分的生产排程
            # 工单产出池（按物料 + 计划完工日期），供销售订单逐行(带时间窗)释放
            wo_supply = {}
            for wo in wo_rows:
                m = wo.get('material_code') or wo.get('sap_matnr') or ''
                pe = wo.get('planned_end')
                if not m or not pe:
                    continue
                pe_d = pe.date() if hasattr(pe, 'date') else None
                # 工单可供应产出 = 未完工数量（与排程行口径一致）
                # 【2026-09-01 修复 P2-14】同上，max(0) 防止超产完工的负值被当作"负供应"，
                # 进而使销售缺口 = 需求 − (负供应) 被高估。
                wo_remain = max(0.0, float(wo.get('confirm_qty') or 0) - float(wo.get('completed_qty') or 0))
                wo_supply.setdefault(m, []).append((pe_d, wo_remain))
            for m in wo_supply:
                wo_supply[m].sort(key=lambda x: (x[0] is None, x[0] or date.max))
            wo_ptr = {m: 0 for m in wo_supply}

            # 销售明细行号为空的，按 vbeln 递增补顺序号，保证「计划订单号」能区分到第几行
            _normalize_sales_posnr()

            so_rows = query_db("""
                SELECT si.matnr, si.arktx as maktx, si.vrkme as meins,
                       COALESCE(NULLIF(si.vdatu,''), so.vdatu, si.vdatu) as vdatu,
                       si.kwmeng, si.lfimg, si.vbeln, si.posnr
                FROM sap_sales_order_items si
                LEFT JOIN sap_sales_orders so ON si.vbeln = so.vbeln
                WHERE si.matnr IS NOT NULL AND si.matnr <> ''
                ORDER BY si.matnr, vdatu
            """)
            # 按 (物料, 交货期) 分组（同日合并），按交货期升序逐行独立判断缺口
            so_groups = {}
            for si in so_rows:
                mat = si.get('matnr', '')
                vd = si.get('vdatu', '')
                key = (mat, vd)
                if key not in so_groups:
                    so_groups[key] = {'matnr': mat, 'vdatu': vd, 'maktx': si.get('maktx', ''),
                                      'meins': si.get('meins', ''), 'vbeln': si.get('vbeln', ''),
                                      'qty': 0.0, 'posnrs': []}
                # 毛需求 = 未发数量 = 订单数量(kwmeng) − 已发货(lfimg)；已发货为空按 0 计
                so_groups[key]['qty'] += max(0, float(si.get('kwmeng') or 0) - float(si.get('lfimg') or 0))
                if si.get('posnr'):
                    so_groups[key]['posnrs'].append(str(si.get('posnr')))

            # 可用库存剩余池（按物料），逐行消耗
            remain = {}
            def _stock(mat):
                return float(inv_map.get(mat, {}).get('available', 0))

            so_added = 0
            for (matnr, vdatu) in sorted(so_groups.keys(), key=lambda k: (k[1] or '99999999')):
                g = so_groups[(matnr, vdatu)]
                qty = g['qty']
                d_due = None
                if vdatu:
                    try:
                        d_due = datetime.strptime(str(vdatu), '%Y%m%d').date()
                    except Exception:
                        d_due = None
                if matnr not in remain:
                    remain[matnr] = _stock(matnr)
                # 释放「计划完工日 <= 交货期」的工单产出（带时间窗）
                if matnr in wo_supply and d_due is not None:
                    lst = wo_supply[matnr]
                    i = wo_ptr[matnr]
                    while i < len(lst):
                        pe_d, pq = lst[i]
                        if pe_d and pe_d <= d_due:
                            remain[matnr] += pq
                            i += 1
                        else:
                            break
                    wo_ptr[matnr] = i
                if qty <= 0:
                    continue
                if remain[matnr] >= qty:
                    # 已覆盖：销售需求被工单+库存全额满足，仍生成 SCH 行（batch_qty=0）
                    remain[matnr] -= qty
                    orders.append({
                        'plnum': g['vbeln'],
                        'order_no': g['vbeln'],
                        'matnr': matnr,
                        'maktx': g['maktx'],
                        'gsmng': 0,
                        'meins': g.get('meins', ''),
                        'quantity': 0,
                        'src': 'SCH',
                        'posnr': '+'.join(g.get('posnrs') or []),
                        'merge_end': vdatu or '',
                        'sales_qty': qty,
                        'covered': True,
                    })
                    so_added += 1
                else:
                    gap = qty - remain[matnr]
                    remain[matnr] = 0
                    orders.append({
                        'plnum': g['vbeln'],
                        'order_no': g['vbeln'],
                        'matnr': matnr,
                        'maktx': g['maktx'],
                        'gsmng': gap,
                        'meins': g.get('meins', ''),
                        'quantity': gap,
                        'src': 'SCH',
                        'posnr': '+'.join(g.get('posnrs') or []),
                        'merge_end': vdatu or '',
                        'sales_qty': qty,
                    })
                    so_added += 1
            if so_added:
                source_label = (source_label + '+sales') if source_label else 'sales'

            # 数据源 3: 兜底 —— 既无工单也无销售订单时，退回 BOM 父物料 / 所有物料
            if not orders:
                bom_parents = query_db("""
                    SELECT parent_matnr, parent_maktx,
                           COUNT(*) as comp_count
                    FROM bom
                    GROUP BY parent_matnr, parent_maktx
                    LIMIT %s
                """, (limit,))
                if bom_parents:
                    orders = []
                    for bp in bom_parents:
                        orders.append({
                            'plnum': bp.get('parent_matnr', ''),
                            'order_no': '',
                            'matnr': bp.get('parent_matnr', ''),
                            'maktx': bp.get('parent_maktx', ''),
                            'gsmng': 1,
                            'quantity': 1,
                        })
                    source_label = 'bom_parent'

            # 数据源 4: 所有物料
            if not orders:
                products = query_db(
                    "SELECT material_code, name, unit FROM products LIMIT %s",
                    (limit,)
                )
                if products:
                    orders = []
                    for pr in products:
                        orders.append({
                            'plnum': pr.get('material_code', ''),
                            'matnr': pr.get('material_code', ''),
                            'maktx': pr.get('name', ''),
                            'gsmng': 1,
                            'quantity': 1,
                            'meins': pr.get('unit', ''),
                        })
                    source_label = 'products'


            if not orders:
                return jsonify({
                    'success': True,
                    'message': '没有可计算的数据源（请先添加物料、BOM或订单）',
                    'total': 0,
                    'results': []
                })

            # 保存已有排程的 wo_generated 状态（仅复活"仍存在对应非草稿工单"的行）
            # 说明：草稿工单(创建/作废) 已被前置清理删除，其排程行不应再保持"已生成"；
            # 只有状态为 批准/执行 且 schedule_ref 匹配当前 aufnr 的工单，才应保留"已生成"标记。
            gen_map = {}
            old_gen = query_db("SELECT aufnr, wo_generated FROM peacock_schedule WHERE wo_generated=1")
            surviving_refs = {r['schedule_ref'] for r in query_db(
                "SELECT DISTINCT schedule_ref FROM work_orders "
                "WHERE schedule_ref IS NOT NULL AND schedule_ref <> '' AND status NOT IN ('创建','作废')")}
            for og in old_gen:
                if og['aufnr'] in surviving_refs:
                    gen_map[og['aufnr']] = 1

            # 清空旧排程结果，重新计算
            execute_db("DELETE FROM peacock_material_shortage")
            execute_db("DELETE FROM peacock_schedule")

            # 加载 BOM 与物料类型，用于把"半成品"子件也纳入排程结果（多级展开）
            all_bom_rows = query_db("SELECT parent_matnr, comp_matnr, comp_maktx, comp_menge, yield_qty FROM bom")
            bom_map_all = {}
            for b in all_bom_rows:
                bom_map_all.setdefault(b['parent_matnr'], []).append(b)
            prod_rows = query_db("SELECT material_code, name, type FROM products")
            type_map = {r['material_code']: (r.get('type') or '').strip() for r in prod_rows}
            name_map = {r['material_code']: (r.get('name') or '') for r in prod_rows}

            # 对每个订单计算齐备套
            results = []
            for order in orders:
                r = _calc_order_readiness(order, inv_map, unlimited_codes, calc_actual_required, enabled_stock_types)
                results.append(r)

            # work_order_id 列已统一在 _ensure_calc_columns()（事务之外）按需补建，此处不再重复。

            # 保存结果
            calc_id = gen_id()
            seq_counter = 0

            # 1) 写入顶层（销售订单/计划订单/工单/BOM父项）排程行
            for i, r in enumerate(results):
                seq_counter += 1
                seq = str(seq_counter).zfill(4)
                order = orders[i] if i < len(orders) else {}
                schedule_id = gen_id()
                top_matnr = r['matnr']
                top_type = type_map.get(top_matnr, '')
                has_child = 1 if top_matnr in bom_map_all else 0
                execute_db(
                    """INSERT INTO peacock_schedule
                       (id, aufnr, order_no, work_order_id, material_code, material_name, material_type, batch_qty, plan_qty,
                        batch_date_start, batch_date_end, source, sales_posnr, sales_qty,
                        ready_status, status_description, ready_components, partial_components,
                        missing_components, total_components, has_child_bom, schedule_sequence,
                        calculated_at, calculated_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)""",
                    (schedule_id, order.get('aufnr') or seq, order.get('order_no', ''), order.get('work_order_id'),
                     top_matnr, r['maktx'],
                     top_type, r['gsmng'], r['gsmng'],
                     order.get('merge_start', '') or '',
                     order.get('merge_end', '') or '',
                     order.get('src', ''), order.get('posnr', '') or '',
                     float(order.get('sales_qty') or 0) if order.get('sales_qty') else None,
                     r['ready_status'], r['status_description'], r['ready_components'],
                     r['partial_components'], r['missing_components'], r['total_components'],
                     has_child, seq_counter, 'system')
                )

            # 2) 把 BOM 中的"生产件"（成品/半成品）子件也作为独立排程行纳入（多级递归）；
            #    时段MRP：同一物料若被多个父件在不同需求日需求，按 (物料, 需求日) 分段聚合为多条排程行，
            #    各自独立排产/齐套，避免"不同需求日的需求被错误合并成一行"。
            #    组件需到位日 = 父件开工日 = 父件交货日(交货日期) − 父件生产提前期（自顶向下前向传播）。
            semi_agg = {}  # (comp_code, req_date_str) -> 数量
            semi_parent = {}  # (comp_code, req_date_str) -> 直接父件物料编码（用于排程结果展示"父件的计划订单号"）
            semi_drive = {}  # (comp_code, req_date_str) -> 驱动该行的顶层订单号（按实际来源，不取 SCH 优先）
            # 顶层订单物料集合：避免把"既是顶层订单、又是某 BOM 子件"的物料重复计入排程行
            top_matnrs = set()
            for order in orders:
                _m = order.get('matnr') or order.get('material_code') or ''
                if _m:
                    top_matnrs.add(_m)

            # 生产提前期映射（用于前向传播需求日期）
            _cyc_rows = query_db("SELECT material_code, production_cycle, production_time FROM products")
            _mat_cycle = {r['material_code']: float(r.get('production_cycle') or 0) for r in _cyc_rows}
            _mat_daily = {r['material_code']: float(r.get('production_time') or 0) for r in _cyc_rows}

            def _lead(matnr, qty):
                daily = _mat_daily.get(matnr, 0) or 0
                if daily > 0 and qty and qty > 0:
                    return math.ceil(qty / daily)
                return _mat_cycle.get(matnr, 0) or 0

            def _collect_semi(parent_matnr, parent_qty, parent_date, ancestors, top_aufnr=None):
                """自顶向下前向传播需求日期：组件需到位日 = 父件开工日 = 父件交货日 − 父件提前期。
                top_aufnr: 驱动该分支的顶层订单号（WO 工单号 或 SCH 销售订单-行号），一路向下传递，
                           用于 SEMI 行"计划订单号"按实际来源显示。"""
                if parent_matnr not in bom_map_all:
                    return
                p_lead = _lead(parent_matnr, parent_qty)
                parent_start = (parent_date - timedelta(days=p_lead)) if (parent_date and p_lead) else parent_date
                for b in bom_map_all[parent_matnr]:
                    comp_code = b['comp_matnr']
                    yq = float(b.get('yield_qty') or 1) or 1
                    cm = float(b.get('comp_menge') or 0)
                    req = (parent_qty / yq) * cm
                    if req <= 0:
                        continue
                    # 组件需到位日（= 父件开工日），再往下钻时作为该组件的"父件交货日"
                    comp_req_date = parent_start.strftime('%Y%m%d') if isinstance(parent_start, date) else None
                    # 成品/半成品 均视为需排程的生产件（半成品自行生产，不进缺料明细）
                    if type_map.get(comp_code) in ('产成品', '半成品') and comp_code not in top_matnrs:
                        key = (comp_code, comp_req_date)
                        semi_agg[key] = semi_agg.get(key, 0) + req
                        if key not in semi_parent:
                            semi_parent[key] = parent_matnr
                        if key not in semi_drive:
                            semi_drive[key] = top_aufnr
                        if comp_code not in ancestors:
                            _collect_semi(comp_code, req, parent_start, ancestors | {comp_code}, top_aufnr)

            for order in orders:
                _top_matnr = order.get('matnr') or order.get('material_code') or ''
                _top_qty = float(order.get('gsmng') or order.get('plan_qty') or 0)
                _top_date = None
                _d = order.get('merge_end') or order.get('merge_start') or ''
                if _d:
                    try:
                        _top_date = datetime.strptime(str(_d), '%Y%m%d').date()
                    except Exception:
                        _top_date = None
                # 驱动来源订单号：WO 用工单号；SCH 用"销售订单-行号"（与展示口径一致）
                _top_aufnr = order.get('aufnr') or order.get('plnum') or order.get('order_no') or ''
                if order.get('src') == 'SCH' and order.get('posnr'):
                    _top_aufnr = f"{order.get('order_no')}-{order.get('posnr')}"
                _collect_semi(_top_matnr, _top_qty, _top_date, {_top_matnr}, _top_aufnr)

            for (comp_code, req_date), agg_qty in semi_agg.items():
                seq_counter += 1
                seq = str(seq_counter).zfill(4)
                semi_order = {'plnum': comp_code, 'order_no': '', 'matnr': comp_code,
                              'maktx': name_map.get(comp_code, comp_code), 'gsmng': agg_qty, 'quantity': agg_qty}
                r_semi = _calc_order_readiness(semi_order, inv_map, unlimited_codes, calc_actual_required, enabled_stock_types)
                schedule_id = gen_id()
                has_child = 1 if comp_code in bom_map_all else 0
                execute_db(
                    """INSERT INTO peacock_schedule
                       (id, aufnr, order_no, material_code, material_name, material_type, parent_code, drive_aufnr, batch_qty, plan_qty,
                        batch_date_start, batch_date_end, source,
                        ready_status, status_description, ready_components, partial_components,
                        missing_components, total_components, has_child_bom, schedule_sequence,
                        calculated_at, calculated_by)
                       VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,NOW(),%s)""",
                    (schedule_id, seq, '', comp_code, r_semi['maktx'],
                     '半成品', semi_parent.get((comp_code, req_date), ''), semi_drive.get((comp_code, req_date), '') or '', agg_qty, agg_qty,
                     '', req_date or '', 'SEMI',
                     r_semi['ready_status'], r_semi['status_description'], r_semi['ready_components'],
                     r_semi['partial_components'], r_semi['missing_components'], r_semi['total_components'],
                     has_child, seq_counter, 'system')
                )

            # 恢复已生成工单的标记
            for aufnr in gen_map:
                execute_db("UPDATE peacock_schedule SET wo_generated=1 WHERE aufnr=%s", (aufnr,))

            # ── P0-1 / P1-10 修复：补做「排程合并 + 缺料重建」 ──────────────
            # 上面已 DELETE 掉两张表，若不重建则缺料明细永久为空（原实现的行为）。
            # 一键计算路径（_skip_post_rebuild=True）随后自行执行这两步，此处跳过。
            rebuilt = False
            if not _skip_post_rebuild:
                # 排程合并天数：<=0 时 _merge_schedule_rows 内部直接 return（不合并）
                smd = _as_int(data.get('schedule_merge_days'), 0, 0, 365)
                if smd > 0:
                    # 与一键计算同口径持久化，使"输入框显示值"与"库内数据口径"一致
                    set_config('schedule_merge_days', smd)
                _merge_schedule_rows(
                    smd,
                    force_same_day=(get_config(f'module_{PLAN_REQ_DATE_MODULE}', '0') == '1'))
                _compute_plan_qty_and_rebuild_shortages(enabled_stock_types, merge_days)
                # 【2026-10-15】延误标记（排程侧，无条件）：合并+缺料重建后标红，与一键计算同口径
                _mark_schedule_overdue()
                rebuilt = True

            # 保存日志
            execute_db(
                "INSERT INTO peacock_calculation_logs (calculation_id, log_type, timestamp, description, details) "
                "VALUES (%s, %s, %s, %s, %s)",
                (calc_id, 'readiness', biz_now().strftime('%Y-%m-%d %H:%M:%S'),
                 f'齐备套计算完成，数据源={source_label}，共{len(results)}个订单'
                 + ('，已重建排程合并与缺料明细' if rebuilt else ''),
                 json.dumps({'total': len(results), 'source': source_label, 'rebuilt': rebuilt},
                            ensure_ascii=False))
            )

            return jsonify({
                'success': True,
                'message': (f'计算完成（数据源:{source_label}），共处理{len(results)}个订单'
                            + ('，已重建排程合并与缺料明细' if rebuilt else '')),
                'total': len(results),
                'results': results,
                'calculation_id': calc_id,
                'source': source_label,
                'rebuilt': rebuilt,
            })

    except Exception as e:
        return jsonify(err_payload(e)), 500


def _int_arg(name, default, lo=None, hi=None, source=None):
    """安全读取整数型查询参数（【P2-21 修复 · 2026-10-14】）。

    原实现到处裸写 `int(request.args.get('page', 1))`：
      · `?page=abc` → ValueError 未捕获 → Flask 返回 500 **HTML** 错误页，
        前端 `res.json()` 解析失败 → 用户只看到空白/报错弹窗，无有效提示；
      · `?per_page=100000` 无上界 → 一次拉全表打爆内存/响应超时。
    本函数统一：解析失败或越界时**静默回落到默认值**（分页参数不值得中断请求），
    并可按 lo/hi 钳制；`source` 用于从 POST body 取值（默认取 query string）。
    """
    raw = (source if source is not None else request.args).get(name, None)
    if raw is None or raw == '':
        return default
    try:
        v = int(str(raw).strip())
    except (TypeError, ValueError):
        return default
    if lo is not None and v < lo:
        return lo
    if hi is not None and v > hi:
        return hi
    return v


@bp.route('/api/calculate/priority', methods=['POST'])
def calculate_priority():
    """优先级计算"""
    data = request.get_json() or {}
    calc_id = data.get('calculation_id')

    try:
        # 🔴【P2-3 修复 · 2026-10-14】原实现硬编码 `LIMIT 100`（配合
        # `ORDER BY calculated_at DESC`），排程行 > 100 时取到的是**最后插入的 100 行**
        # —— 即 SEMI 等后置行，而顶层 SCH/WO 行可能整体落选 → 这些行的
        # `overall_priority` 永远不被写入（NULL），界面排序与"建议下单"判定失真。
        # 现改为默认**全量**计算；仅当调用方显式传 `limit` 时才截断（保留可控性）。
        _limit = data.get('limit')
        _lim_sql = " LIMIT %s" if _limit else ""
        _lim_args = (int(_limit),) if _limit else ()
        if calc_id:
            schedules = query_db(
                "SELECT * FROM peacock_schedule WHERE calculated_at > DATE_SUB(NOW(), INTERVAL 1 HOUR) "
                "ORDER BY calculated_at DESC" + _lim_sql, _lim_args
            )
        else:
            schedules = query_db(
                "SELECT * FROM peacock_schedule WHERE is_deleted=0 ORDER BY calculated_at DESC" + _lim_sql,
                _lim_args
            )

        if not schedules:
            return jsonify({'success': False, 'message': '请先执行齐备套计算'})

        results = []
        for s in schedules:
            base_score = {'A': 100, 'B': 50, 'C': 0}.get(s.get('ready_status'), 0)
            qty = float(s.get('plan_qty') or 0)
            if qty >= 1000:
                qty_score = 10
            elif qty >= 500:
                qty_score = 8
            elif qty >= 100:
                qty_score = 5
            else:
                qty_score = 2

            # 优先级紧急度：使用交货日（排程来源用 batch_date_end，工单来源用 planned_end），替代恒为 NULL 的 finish_date
            finish_date = s.get('finish_date') or s.get('batch_date_end') or s.get('batch_date_start')
            urgency_score = 0
            if finish_date:
                fd = _parse_date(finish_date)
                if fd:
                    # 【P1-5 修复·2026-10-14】紧急度「当日」基准统一为 _biz_today()
                    # （固定 +08:00），与排程归位、工单侧同源；原 date.today() 依赖
                    # OS 时区，服务器非 +08:00 时 days_left 分档会整体偏移一档。
                    days_left = (fd - _biz_today()).days
                    if days_left < 0:
                        urgency_score = 30
                    elif days_left <= 3:
                        urgency_score = 25
                    elif days_left <= 7:
                        urgency_score = 20
                    elif days_left <= 14:
                        urgency_score = 15
                    elif days_left <= 30:
                        urgency_score = 10
                    else:
                        urgency_score = 5

            priority_score = base_score + urgency_score + qty_score
            suggest = 'YES' if s.get('ready_status') in ('A', 'B') else 'NO'

            results.append({
                'schedule_id': s['id'],
                'aufnr': s.get('aufnr'),
                'material_code': s.get('material_code'),
                'material_name': s.get('material_name'),
                'ready_status': s.get('ready_status'),
                'base_score': base_score,
                'urgency_score': urgency_score,
                'qty_score': qty_score,
                'priority_score': priority_score,
                'suggest_order': suggest,
            })

        results.sort(key=lambda x: x['priority_score'], reverse=True)

        for i, r in enumerate(results):
            execute_db(
                "UPDATE peacock_schedule SET overall_priority=%s, "
                "suggest_order=%s WHERE id=%s",
                (r['priority_score'], r['suggest_order'], r['schedule_id'])
            )
            r['rank'] = i + 1

        return jsonify({
            'success': True,
            'message': f'优先级计算完成，共{len(results)}个订单',
            'results': results,
        })

    except Exception as e:
        return jsonify(err_payload(e)), 500


@bp.route('/api/calculate/last-info')
def calculate_last_info():
    """获取上次计算信息"""
    try:
        row = query_db(
            "SELECT calculation_id, log_type, timestamp, description, details, created_at "
            "FROM peacock_calculation_logs ORDER BY created_at DESC LIMIT 1",
            one=True
        )
        if row:
            return jsonify({'success': True, 'data': serialize_row(row)})
        return jsonify({'success': False, 'message': '无计算记录'})
    except Exception as e:
        return jsonify(err_payload(e)), 500


def _compute_plan_qty_and_rebuild_shortages(stock_types=None, merge_days=0):
    """为 peacock_schedule 计算 plan_qty 并重建缺料明细。

    重要（2026-09-01 P1-13 复核）：本函数【不再二次冲减】。plan_qty 恒等于 batch_qty，
    即统一采用 calculate_readiness 已算好的"单扣净需求"口径（避免历史 WIP 双重扣减 bug）。
    - WO 行：plan_qty = 未完工数量（batch_qty），不做库存/WIP/已发货冲减；
    - SCH/SEMI 行：plan_qty = batch_qty（SCH 已在 calculate_readiness 合并"库存+工单在制"净算）。
    实际缺料明细由 _rebuild_all_shortages 基于该 plan_qty 统一推导。
    """
    from datetime import datetime, date

    if stock_types is None:
        stock_types = ['unrestricted']

    # 【P3 清理 · 2026-10-14】原此处有一次 `inv_pool = build_inventory_pool(stock_types)`，
    # 但在下方 `_net_stock` 版本（剔除 transit）覆盖之前**从未被读取** —— 纯死计算，
    # 每次一键计算白白多付一次全库存聚合查询。已删除。
    # BOM
    bom_rows = query_db("SELECT parent_matnr, comp_matnr, comp_maktx, comp_menge, yield_qty FROM bom")
    bom_map = {}
    for b in bom_rows:
        bom_map.setdefault(b['parent_matnr'], []).append(b)

    # 2.2 全局WIP在制池：本函数仅生成 plan_qty（≡ batch_qty，单扣口径，无双重扣减），
    # 实际缺料推算在 _rebuild_all_shortages 中基于"已净算后的 plan_qty"完成；
    # 此处不再构建 WIP 池（原死代码已在 2026-09-01 清理，见 P1-13）。

    # ---- 第一步：取排程，按交货日期排序 ----
    schedule_rows = query_db("SELECT * FROM peacock_schedule WHERE is_deleted=0 ORDER BY COALESCE(batch_date_end,'99999999'), aufnr")
    mat_batches = {}
    for s in schedule_rows:
        mat_batches.setdefault(s['material_code'], []).append(s)

    # ---- 第三步：逐行迭代计算（净需求 = 未发货量 - 库存 - WIP）----
    plan_updates = []
    for mat, batches in mat_batches.items():
        for batch in batches:
            src = batch.get('source')
            # 工单来源(WO)：计划排产 = 未完工数量，直接按工单仍需生产量，不做库存/WIP/已发货冲减
            if src == 'WO':
                plan_updates.append((float(batch.get('batch_qty') or 0), batch['id']))
                continue

            # SCH / SEMI 行：
            #  - SCH 的 batch_qty 在 calculate_readiness 中已合并「可用库存 + 工单在制(WO)」净算为净需求，
            #    此处【不再】二次扣减库存/WIP，直接采用该净需求，避免 WIP/库存重复扣减（双重扣减 bug 修复）。
            #  - SEMI 的 batch_qty 为 BOM 展开毛需求；缺料展开统一以 batch_qty 为基准（见 _rebuild_all_shortages），
            #    故 plan_qty 与 batch_qty 保持一致，二者默认相等，可由 PUT 接口手动覆盖。
            #  结论：plan_qty 恒等于 batch_qty（单扣口径），彻底消除历史"计算层二次重算导致的 WIP 双重扣减"。
            plan = float(batch.get('batch_qty') or 0)
            plan_updates.append((plan, batch['id']))

    if plan_updates:
        # 批量写回 plan_qty（净需求/计划排产）；plan_qty 恒等于 batch_qty（单扣口径），无需再回查。
        execute_many_db(
            "UPDATE peacock_schedule SET plan_qty=%s WHERE id=%s",
            [(float(pv), sid) for pv, sid in plan_updates])
        # 净需求=0 → 无需生产 → 直接置齐备(A)。原实现逐行回查 batch_qty/total_components（N+1 冗余），
        # 修复 P1-12：合并为一次 IN 查询统一置齐备。
        zero_ids = [sid for (pv, sid) in plan_updates if pv == 0]
        if zero_ids:
            ph = ','.join(['%s'] * len(zero_ids))
            tc_rows = query_db(
                "SELECT id, total_components FROM peacock_schedule WHERE id IN (%s)" % ph,
                tuple(zero_ids))
            tc_map = {r['id']: (r.get('total_components') or 0) for r in tc_rows}
            execute_many_db(
                "UPDATE peacock_schedule SET ready_status='A', status_description='齐备', "
                "ready_components=%s, partial_components=0, missing_components=0 WHERE id=%s",
                [(tc_map.get(sid, 0), sid) for sid in zero_ids])

    # ---- 重建缺料明细与齐备状态 ----
    # 库存池剥离 'transit'：采购在途改由 _rebuild_all_shortages 内部按"到货日期时间窗"逐行计入
    # （与展示层 _apply_inventory_and_transit 完全一致），避免旧逻辑把全部在途当立即可用导致的口径分裂（问题1、2）。
    _net_stock = [t for t in (stock_types or []) if t != 'transit']
    inv_pool = build_inventory_pool(_net_stock)
    mat_cycle_map, mat_daily_map = _prepare_shortage_context(bom_map)
    # generate_semi=False：SEMI 排程行已在"排程合并前"阶段(_generate_semi_phase)生成，
    # 此处仅基于"合并后排程结果"计算缺料明细 + 缺料合并，避免合并后数量变化导致日期错位、重复建行。
    _rebuild_all_shortages(inv_pool, bom_map, mat_cycle_map, mat_daily_map, stock_types, merge_days, generate_semi=False)




def _rebuild_shortages_only(stock_types, merge_days=0):
    """仅重建缺料明细和齐备状态，使用当前 plan_qty（不重新计算排产）

    【P1-9 修复】新增 `merge_days` 形参：原实现恒用 0，使「重新计算缺料明细」
    与「一键计算」的缺料合并口径不一致（后者按用户设定的合并天数）。
    """
    _net_stock = [t for t in (stock_types or []) if t != 'transit']
    inv_pool = build_inventory_pool(_net_stock)

    bom_rows = query_db("SELECT parent_matnr, comp_matnr, comp_maktx, comp_menge, yield_qty FROM bom")
    bom_map = {}
    for b in bom_rows:
        bom_map.setdefault(b['parent_matnr'], []).append(b)

    mat_cycle_map, mat_daily_map = _prepare_shortage_context(bom_map)
    _rebuild_all_shortages(inv_pool, bom_map, mat_cycle_map, mat_daily_map,
                           stock_types, merge_days)


@bp.route('/api/peacock/recalc-shortages', methods=['POST'])
def peacock_recalc_shortages():
    """仅重建缺料明细（不改变排产数量，基于当前 plan_qty 和工单）。

    缺料归位（规则2：子件需求日期 < 当日 → 归位当日 + delayed=1）已随「缺料重建」
    (_rebuild_all_shortages 内部、内存内 归位→合并→内联重算) 一并完成；
    此处再套用 _enforce_plan_req_date()（现仅规则1：排程计划排产日期 < 当日 → 归位当日
    + 完工日按 `当日+GREATEST(0,原完工−当日)` 联动 + delayed=1），否则「一键计算」做的
    排程归位会被本路径悄悄抹掉。
    """
    data = request.get_json() or {}
    stock_types = data.get('enabled_stock_types', ['unrestricted'])
    # 【P1-9 修复·2026-10-14】透传「缺料合并天数」：
    # 原实现既不从前端接收、也不回读配置，导致 `_rebuild_shortages_only` 用形参默认 0
    # → 用户一键计算按 merge_days=7 合并后，只要点一次「🔄 重新计算缺料明细」，
    #   缺料明细立刻退回未合并状态（行数与口径全变）且无任何提示。
    # 取值优先级：前端显式传值 > 已持久化配置(get_config) > 0，与 peacock_calculate
    # 持久化配置（set_config('merge_days', ...)）同源，保证两种入口口径一致。
    merge_days = data.get('merge_days')
    if merge_days is None:
        try:
            merge_days = int(get_config('merge_days', 0) or 0)
        except (TypeError, ValueError):
            merge_days = 0
    else:
        try:
            merge_days = int(merge_days or 0)
        except (TypeError, ValueError):
            merge_days = 0
    try:
        # 【P1-3 修复】"清空缺料 → 逐行重建 → 归位"整体包事务：
        # 原实现三步独立提交，重建中途失败会留下已清空且不可回滚的缺料表
        # （表现为"越刷越少"）。transaction() 支持可重入，与一键计算同源。
        with transaction():
            _rebuild_shortages_only(stock_types, merge_days=merge_days)
            delay_stat = _enforce_plan_req_date(stock_types)
            # 【2026-10-15】排程侧延误标记（无条件）：即使本接口只重建缺料，也保证排程红点正确
            # （独立运行 recalc 时不必依赖上一次 calculate 的标记残留）。只在位标 1（OR 语义），幂等。
            _mark_schedule_overdue()
            # 回执口径（2026-10-15 解耦后）：无条件统计界面实际展示的 delayed 行数，与开关无关。
            delay_stat['schedule'] = query_db(
                "SELECT COUNT(*) c FROM peacock_schedule WHERE is_deleted=0 AND `delayed`=1",
                one=True)['c']
            delay_stat['shortage'] = query_db(
                "SELECT COUNT(*) c FROM peacock_material_shortage WHERE `delayed`=1",
                one=True)['c']
        return jsonify({'success': True, 'message': '缺料明细已更新', 'delay': delay_stat})
    except Exception as e:
        return jsonify(err_payload(e)), 500


def _cleanup_before_calculate():
    """一键计算前置清理：删除草稿/作废记录，避免与重算结果混淆。

    1) 工单管理：删除状态为 创建/作废 的工单（含其子项），并复位对应排程的 wo_generated；
    2) 采购入库：删除状态为 创建/作废 的采购入库单；
    3) 销售订单：删除状态为 作废 的销售单（含其子项）。
    """
    counts = {'work_orders': 0, 'purchase_orders': 0, 'sales_orders': 0}

    # 1) 工单
    wo_rows = query_db("SELECT id, source, schedule_ref FROM work_orders WHERE status IN ('创建','作废')")
    wo_ids = [r['id'] for r in wo_rows]
    wo_refs = [r['schedule_ref'] for r in wo_rows if r.get('schedule_ref')]
    # 【P2-17 修复 · 2026-10-14】本函数整体仍留在「一键计算事务之外」（用户拍板方案 B：
    # 清理结果即便后续计算失败也保留），但**内部每组删除改为一个事务** ——
    # 原实现每组都是逐条独立提交：如"子项已删、主表删除失败"会留下孤儿 work_orders_items；
    # 销售单同理。事务化后每组删除要么全成要么全回滚，不再产生半截。
    if wo_ids:
        ph = ','.join(['%s'] * len(wo_ids))
        with transaction():
            execute_db(f"DELETE FROM work_orders_items WHERE aufnr IN ({ph})", wo_ids)
            execute_db(f"DELETE FROM work_orders WHERE id IN ({ph})", wo_ids)
            # 复位这些工单对应的排程行 wo_generated（按稳定的 schedule_ref 匹配；
            # 必须在 calculate_readiness 读取 gen_map 之前完成）
            for ref in wo_refs:
                execute_db("UPDATE peacock_schedule SET wo_generated=0 WHERE aufnr=%s", (ref,))
        counts['work_orders'] = len(wo_ids)

    # 2) 采购入库
    po_cnt = query_db("SELECT COUNT(*) AS cnt FROM purchase_orders WHERE status IN ('创建','作废')", one=True)
    po_deleted = int(po_cnt['cnt']) if po_cnt and po_cnt.get('cnt') else 0
    if po_deleted:
        with transaction():
            execute_db("DELETE FROM purchase_orders WHERE status IN ('创建','作废')")
    counts['purchase_orders'] = po_deleted

    # 3) 销售订单（作废）
    so_rows = query_db("SELECT vbeln FROM sap_sales_orders WHERE status='作废'")
    so_vbelns = [r['vbeln'] for r in so_rows]
    if so_vbelns:
        ph = ','.join(['%s'] * len(so_vbelns))
        with transaction():
            execute_db(f"DELETE FROM sap_sales_order_items WHERE vbeln IN ({ph})", so_vbelns)
            execute_db(f"DELETE FROM sap_sales_orders WHERE vbeln IN ({ph})", so_vbelns)
        counts['sales_orders'] = len(so_vbelns)

    return counts


def _collect_master_data_warnings():
    """一键计算后，检查本次参与计算的物料主数据是否缺失关键字段：
       - 产成品/半成品（生产件）：日产量(production_time) 不能为空或 0
       - 原辅料/包材/原材料/包装等（采购件）：采购周期(production_cycle) 不能为空或 0
    返回缺失清单（列表），用于前端弹窗提示。范围 = 销售需求物料 + 其整棵 BOM 组件树。
    """
    # 1) 本次计算涉及的需求物料：销售订单行项目（排除已作废销售单）
    demanded = set()
    so_items = query_db("""
        SELECT DISTINCT si.matnr
        FROM sap_sales_order_items si
        LEFT JOIN sap_sales_orders so ON si.vbeln = so.vbeln
        WHERE COALESCE(so.status, '') != '作废'
    """)
    for r in so_items:
        m = (r.get('matnr') or '').strip()
        if m:
            demanded.add(m)

    # 2) BOM 展开，收集所有组件（多层级，防环）
    involved = set(demanded)
    stack = list(demanded)
    guard = set()
    while stack:
        cur = stack.pop()
        if cur in guard:
            continue
        guard.add(cur)
        children = query_db("SELECT comp_matnr FROM bom WHERE parent_matnr=%s", (cur,))
        for c in children:
            cm = (c.get('comp_matnr') or '').strip()
            if cm and cm not in involved:
                involved.add(cm)
                stack.append(cm)

    if not involved:
        return []

    # 3) 查询主数据并分类校验
    ph = ','.join(['%s'] * len(involved))
    prods = query_db(f"""
        SELECT material_code, name, type, production_time, production_cycle
        FROM products WHERE material_code IN ({ph})
    """, list(involved))

    warnings = []
    produced_types = {'产成品', '半成品'}
    for p in prods:
        code = p.get('material_code')
        ptype = (p.get('type') or '').strip()
        pt = p.get('production_time')
        pc = p.get('production_cycle')
        pt_val = float(pt) if pt is not None else 0.0
        pc_val = float(pc) if pc is not None else 0.0
        if ptype in produced_types:
            if pt_val <= 0:
                warnings.append({
                    'material_code': code,
                    'name': p.get('name') or '',
                    'type': ptype,
                    'missing_field': '日产量',
                    'current_value': '' if pt is None else str(pt),
                })
        else:
            # 采购件：原辅料/包材/原材料/包装/未分类 等均需采购周期
            if pc_val <= 0:
                warnings.append({
                    'material_code': code,
                    'name': p.get('name') or '',
                    'type': ptype or '未分类',
                    'missing_field': '采购周期',
                    'current_value': '' if pc is None else str(pc),
                })
    warnings.sort(key=lambda w: (w['missing_field'], w['material_code']))
    return warnings


def _enforce_schedule_dates():
    """「计划需求日期」开关：规则1 排程归位（仅排程结果）。

    把早于当日的「计划排产日期(batch_date_start)」按归位模式处理、完工日按实际公式联动、
    并标 delayed=1；工单(WO)不参与（独立生产任务铁律）。
    开关关闭时直接返回 enabled=False（不改动任何数据）。

    归位模式（system_config.schedule_overdue_mode，【P0-2 · 2026-10-20】新增二选一）：
      · clamp_today（默认）：压当日（历史行为）；
      · forward_capacity：顺排到当日之后首个可用产能日（见 _forward_overdue_to_capacity）。

    🔴【P1-1 修复 · 2026-10-14 用户拍板方案 A】此前本函数 WHERE **缺少 source 过滤**，
    实际会把 WO 行一并改写，与上面这句 docstring 自述的铁律相反。实测（真实库）：
    5 张 WO 行全部被压成"今日→今日"（GD006 原 5 天工期 → 0），与工单管理页的
    7~8 月真实计划日期直接矛盾，并虚增 5/12 的 `delayed` 红点。现两处 WHERE 均排除 WO。
    注：WO 行的 `delayed` 仍可能由其它路径（如展示层）产生，但本归位不再写它。
    调用点：① 一键计算（_generate_semi_phase 之后、_merge_schedule_rows 之前，
    使归位产生的同日簇被排程合并收口）；② 重新计算缺料 / 手动覆盖（仅规则1）。

    ⚠ 完工日公式（2026-10-13 实证修正）：MySQL 单表 UPDATE 赋值左到右可见，
    `SET start=当日, end=…DATEDIFF(end,start)…` 中 start 读到的是【新值=当日】，
    实际为 `end = 当日 + GREATEST(0, 原完工 − 当日)`：
      · 原完工 < 当日（完全过期行）→ end = 当日，开工=完工=当日，工期压 0（用户已拍板接受）；
      · 原完工 ≥ 当日（跨期行）     → end = 原完工，完工日/交货承诺不动，工期压缩为非 0。
    「当日 + 原工期」的顺延表述从未生效，勿再据其推断行为。
    """
    stat = {'enabled': False, 'today': None, 'schedule': 0, 'shortage': 0, 'work_orders': 0}
    try:
        if get_config('module_%s' % PLAN_REQ_DATE_MODULE, '0') != '1':
            return stat
    except Exception:
        return stat
    today = _biz_today()  # 问题6 修复：业务当日固定 +08:00，不依赖 OS 时区
    tstr8 = today.strftime('%Y%m%d')
    stat['enabled'] = True
    stat['today'] = today.strftime('%Y-%m-%d')
    # 【P0-2 · 2026-10-20】过期归位模式二选一（system_config.schedule_overdue_mode）：
    #   clamp_today（默认，历史行为）：压当日，完工日按 GREATEST(0,原完工−当日) 联动；
    #   forward_capacity：顺排到「当日之后首个可用产能日」——按物料日产量做产能可行性顺延，
    #     解决"过期单一复产全部堆到同一天开工"的当日产能过载（实测 6/15 行同压 20261019）。
    # 模式只改"过期行落到哪一天"，不改数量、不改 delayed 标记，默认值与历史逐字一致。
    _mode = (get_config('schedule_overdue_mode', 'clamp_today') or 'clamp_today').strip()
    stat['mode'] = _mode
    if _mode == 'forward_capacity':
        return _forward_overdue_to_capacity(today, stat)
    # 规则1：排程结果 —— 计划排产日期(batch_date_start)
    # 完工日联动公式见上方 docstring「⚠ 完工日公式」：实际为
    #   当日 + GREATEST(0, 原完工 − 当日)   ← DATEDIFF 读到新 start(当日)，非原开工
    # 完全过期行（原完工<当日）→ 开工=完工=当日、工期压 0（2026-10-13 用户拍板）；
    # 跨期行（原完工≥当日）→ 完工原样保留、工期压缩为「完工−当日」。原完工为空则保持空。
    # 🔴 【P1-1 修复 · 2026-10-14 用户拍板方案 A】**排除 WO 行**（`AND COALESCE(source,'') <> 'WO'`）。
    # WO 排程行是「工单」的镜像，其日期来自 `work_orders.planned_start/end`，工单是独立生产任务，
    # 不应被"计划需求日期"归位改写。改写会同时造成：
    #   ① 排程结果页与工单管理页**同一工单显示两个不同日期**（用户日常直接可见）；
    #   ② 工期被压成 0（GD006 原 5 天 → 0）；
    #   ③ `delayed` 红点虚高（实测 12 个里 5 个是 WO 行）。
    # 与同文件既有铁律统一：`:937` `if r.get('source')=='WO': continue`、
    # `_merge_schedule_rows:611`「工单(WO)保持独立，不参与合并」、本文档 docstring。
    # 数量口径不受影响：缺料的 WO 分支读的是 `work_orders.planned_start`，非排程行。
    _WO_EXCL = " AND COALESCE(source,'') <> 'WO'"
    # 【P3 修复 · 2026-10-14】日期只认紧凑 8 位 `YYYYMMDD`。
    # 手动覆盖接口允许前端直写日期，若写成了 `2026-07-18` 这类带分隔符的格式：
    #   STR_TO_DATE(...,'%Y%m%d') → NULL → DATEDIFF(NULL,…) → NULL → GREATEST(0,NULL) → NULL
    #   → DATE_ADD(…, INTERVAL NULL DAY) → NULL → **完工日被清成 NULL（交货日丢失）**。
    # 故：① WHERE 只匹配可解析的 8 位开工日（解析不了的整行不动，避免脏写）；
    #     ② 完工日非 8 位时原样保留，不做重算。
    _YMD8 = " REGEXP '^[0-9]{8}$'"
    stat['schedule'] = query_db(
        "SELECT COUNT(*) c FROM peacock_schedule "
        "WHERE is_deleted=0 AND batch_date_start IS NOT NULL AND batch_date_start < %s"
        " AND batch_date_start" + _YMD8 + _WO_EXCL,
        (tstr8,), one=True)['c']
    if stat['schedule']:
        execute_db(
            "UPDATE peacock_schedule "
            "SET batch_date_start=%s, "
            "    batch_date_end = CASE "
            "        WHEN batch_date_end IS NULL THEN NULL "
            "        WHEN batch_date_end" + _YMD8 + " THEN "
            "            DATE_FORMAT(DATE_ADD(STR_TO_DATE(%s,'%%Y%%m%%d'), "
            "                INTERVAL GREATEST(0, DATEDIFF(STR_TO_DATE(batch_date_end,'%%Y%%m%%d'), "
            "                    STR_TO_DATE(batch_date_start,'%%Y%%m%%d'))) DAY), '%%Y%%m%%d') "
            "        ELSE batch_date_end "
            "    END, "
            "    `delayed`=1 "
            "WHERE is_deleted=0 AND batch_date_start IS NOT NULL AND batch_date_start < %s"
            " AND batch_date_start" + _YMD8 + _WO_EXCL,
            (tstr8, tstr8, tstr8))
    # 注：本 stat['schedule'] 为「本函数归位处理的行数」。最终落库后界面红点数由
    # peacock_calculate 在「排程合并 + 缺料重建」之后重新统计覆盖（见该处注释）。
    return stat


def _load_daily_capacity():
    """物料日产量映射 {material_code: 件/天}（products.production_time，缺失/0 视为无产能依据）。"""
    daily = {}
    for r in query_db("SELECT material_code, production_time FROM products"):
        daily[(r.get('material_code') or '').strip()] = float(r.get('production_time') or 0)
    return daily


def _build_capacity_load(today):
    """当日起各物料每日已排产量 {mat: {date: qty}}（只算开工日可解析、>= 当日、非 WO 行）。

    供顺排模式计算"首个可用产能日"：同物料当日已排量 + 本行量 <= 日产量 才可落入该日。
    """
    from collections import defaultdict
    load = defaultdict(lambda: defaultdict(float))
    for r in query_db("SELECT material_code, plan_qty, batch_date_start FROM peacock_schedule "
                      "WHERE is_deleted=0 AND COALESCE(source,'')<>'WO'"):
        d = _parse_date8(r.get('batch_date_start'))
        if d is None or d < today:
            continue
        load[(r.get('material_code') or '').strip()][d] += float(r.get('plan_qty') or 0)
    return load


def _next_capacity_day(mat, qty, today, daily, load):
    """顺排模式：该物料当前行最早的"可用产能日"（>= today）。

    - 日产量缺失/0 → 无产能依据，退化为压当日（不虚构产能）；
    - 单行量 >= 日产量 → 任何单日都装不下，独占"最早的空日"（负载为 0 的第一天），
      避免 qty>cap 时逐日扫描永不收敛（死循环）；
    - 否则找「当日已排量 + 本行量 <= 日产量」的最早一天。
    """
    cap = (daily or {}).get(mat, 0) or 0
    if cap <= 0:
        return today
    day = today
    mat_load = (load or {}).get(mat) or {}
    if qty >= cap:
        while mat_load.get(day, 0.0) > 0:
            day += timedelta(days=1)
    else:
        while mat_load.get(day, 0.0) + qty > cap + MRP_QTY_EPS:
            day += timedelta(days=1)
    return day


def _forward_overdue_to_capacity(today, stat):
    """归位模式 forward_capacity：把过期排程行顺排到当日之后首个可用产能日。

    产能口径：物料日产量 products.production_time（件/天）。同物料「当日已排量 + 本行量」
    超过日产量即顺延到下一日（逐行按原开工日升序处理，先占先得，结果确定可复现）。
    - 日产量缺失/0 的物料没有产能依据 → 退化为压当日（与 clamp_today 一致），
      并在 stat['no_capacity_materials'] 如实列出，**不虚构产能数据**；
    - 单行量 >= 日产量 → 独占最早空日（见 _next_capacity_day）；
    - 完工日 = 新开工 + 原工期(max(0, 原完工−原开工))；原完工非 8 位时原样保留；
    - 只处理非 WO、开工日为紧凑 8 位且 < 当日的行；delayed 一律置 1（与压当日模式同语义）；
    - 数量（batch_qty/plan_qty）不受影响。

    注：一键计算主路径中 _backfill_semi_dates 已先行按同一口径顺排（开关 ON 时），
    本函数作为 recalc-shortages 等路径的兜底；两处共用 _next_capacity_day，口径一致。
    """
    from collections import defaultdict
    tstr8 = today.strftime('%Y%m%d')

    def _p8(s):
        s = (str(s).strip() if s is not None else '')
        if len(s) >= 8:
            s = s[:8]
        try:
            return datetime.strptime(s, '%Y%m%d').date()
        except Exception:
            return None

    daily = _load_daily_capacity()
    load = _build_capacity_load(today)
    rows = query_db(
        "SELECT id, material_code, plan_qty, batch_qty, batch_date_start, batch_date_end FROM peacock_schedule "
        "WHERE is_deleted=0 AND batch_date_start IS NOT NULL AND batch_date_start < %s "
        "AND batch_date_start REGEXP '^[0-9]{8}$' AND COALESCE(source,'') <> 'WO' "
        "ORDER BY material_code, batch_date_start, aufnr", (tstr8,))
    updates = []
    no_cap = set()
    for r in rows:
        mat = (r.get('material_code') or '').strip()
        qty = float(r.get('plan_qty') or 0) or float(r.get('batch_qty') or 0)
        os_, oe = _p8(r.get('batch_date_start')), _p8(r.get('batch_date_end'))
        dur = max(0, (oe - os_).days) if (os_ and oe) else 0
        if (daily.get(mat, 0) or 0) <= 0:
            no_cap.add(mat)
        nd = _next_capacity_day(mat, qty, today, daily, load)
        load[mat][nd] = load[mat].get(nd, 0.0) + qty
        if os_ and oe:
            ne_str = (nd + timedelta(days=dur)).strftime('%Y%m%d')
        else:
            ne_str = r.get('batch_date_end')  # 原完工非 8 位：原样保留
        updates.append((nd.strftime('%Y%m%d'), ne_str, r['id']))
    if updates:
        execute_many_db(
            "UPDATE peacock_schedule SET batch_date_start=%s, batch_date_end=%s, `delayed`=1 WHERE id=%s",
            updates)
    stat['schedule'] = len(updates)
    stat['forwarded'] = len(updates)
    stat['no_capacity_materials'] = sorted(no_cap)
    return stat


def _mark_schedule_overdue():
    """延误标记（排程侧，无条件，2026-10-15 设计）：计划排产日期 < 当日 → delayed=1（只置1，不清0）。
    与「日期改写」解耦：无论「计划需求日期」开关是否开启，过期排程行都必须可见（红点）。
    被归位改到当日的行 start=当日，本 WHERE 匹配不到，其红点由 _enforce_schedule_dates 保留（OR 语义）。
    WO 行不参与（与 _enforce_schedule_dates 的 _WO_EXCL 一致：排程行是工单镜像，不在此标红）。
    必须在「缺料重建」之后调用：_backfill_semi_dates() 会回写 batch_date_start，标早了日期还会变。"""
    today8 = _biz_today().strftime('%Y%m%d')
    execute_db(
        "UPDATE peacock_schedule SET `delayed`=1 "
        "WHERE is_deleted=0 AND batch_date_start REGEXP '^[0-9]{8}$' "
        "AND batch_date_start < %s AND COALESCE(source,'') <> 'WO'",
        (today8,))


def _enforce_plan_req_date(stock_types=None):
    """兼容包装：「计划需求日期」开关的规则2（缺料归位）已随「缺料重建」一并移至
    _rebuild_all_shortages 内部（内存内 归位→合并→内联重算），故本函数现仅负责规则1
    （排程归位），等价于 _enforce_schedule_dates()。保留此名以兼容既有调用与注释引用。"""
    return _enforce_schedule_dates()


class _CalcAbort(Exception):
    """计算中途失败并需回滚：携带要返回给前端的响应体与状态码。

    为什么必须抛异常而不是直接 return：这些失败发生在 `with transaction():` 内部，
    直接 return 会让上下文管理器**正常提交**事务，等于把半截数据写进库。
    抛异常才能触发 rollback；再由外层捕获并按原样返回响应体。
    """

    def __init__(self, payload, status=500):
        super().__init__((payload or {}).get('error') or (payload or {}).get('message') or '计算失败')
        self.payload = payload or {'success': False, 'error': '计算失败'}
        self.status = status


@bp.route('/api/peacock/calculate', methods=['POST'])
def peacock_calculate():
    """一键排程计算（齐备套+优先级）。

    并发与原子性（P1-1 / P1-3）：
      - **单飞锁**：同一时刻只允许一个计算在执行，并发请求直接 409 拒绝（不排队，
        避免请求堆积）。
      - **事务**：步骤1~5 的 DML 整体包进一个事务，中途失败整体回滚，
        杜绝"旧行已删、新行只插一半"的脏状态。
      - **前置清理(cleanup) 刻意留在事务之外**（维持既有行为）：即便后续计算失败，
        已清理的草稿/作废记录也不回滚 —— 前端失败提示需明示这一点。
      - **DDL 必须在事务之外**：见 `_ensure_calc_columns` 的说明。
    """
    if not _calc_lock.acquire(blocking=False):
        return jsonify({'success': False, 'error': '计算进行中，请稍后再试'}), 409

    try:
        # ── 事务之外 ────────────────────────────────────────────────
        # 前置清理：删除草稿/作废的工单、采购入库单、作废销售单。
        # 维持现状（不纳入事务）：清理结果即使后续失败也保留。
        cleanup = _cleanup_before_calculate()

        data = request.get_json() or {}

        # 持久化"合并天数"：保存本次计算使用的配置，使重启/重新进入系统后
        # 输入框显示值与库内已计算的数据口径一致，避免误解。
        # 【P3 归一】合并天数先经 _as_int 归一（字符串/None/非法值都安全回退 0），
        # 归一后的值同时写回 data，使后续步骤与持久化配置用的是同一个值。
        data['schedule_merge_days'] = _as_int(data.get('schedule_merge_days'), 0, 0, 365)
        data['merge_days'] = _as_int(data.get('merge_days'), 0, 0, 365)
        set_config('schedule_merge_days', data['schedule_merge_days'])
        set_config('merge_days', data['merge_days'])

        # DDL 必须在事务开启之前完成（DDL 会隐式提交事务）
        _ensure_calc_columns()

        # ── 事务之内：以下步骤任一失败则整体回滚 ────────────────────
        with transaction():
            # 「计划需求日期」开关：作用于排程结果 / 缺料明细（见 _enforce_plan_req_date），
            # **不作用于工单** —— 历史工单永不被一键计算改写。
            data['plan_req_date_enabled'] = get_config(f'module_{PLAN_REQ_DATE_MODULE}', '0') == '1'

            # 【P0-1】显式传 _skip_post_rebuild=True：一键计算随后会自行做排程合并与
            # 缺料重建（下方 :merge/:rebuild），避免 readiness 内部重复算一遍。
            resp1 = calculate_readiness(_skip_post_rebuild=True)
            # calculate_readiness 作为 Flask 处理器调用时返回 (response, status_code)
            if isinstance(resp1, tuple):
                resp1 = resp1[0]
            if hasattr(resp1, 'status_code') and resp1.status_code != 200:
                raise _CalcAbort(resp1.get_json() if hasattr(resp1, 'get_json') else {}, resp1.status_code)
            result1 = resp1.get_json() if hasattr(resp1, 'get_json') else resp1
            if isinstance(result1, dict) and not result1.get('success'):
                raise _CalcAbort(result1)

            data['calculation_id'] = result1.get('calculation_id')

            # 🔴【P2-4 修复 · 2026-10-14】优先级计算原在**排程合并之前**执行，
            # 但优先级依赖 `plan_qty`（数量分档）与 `ready_status`（A/B/C 基础分），
            # 而这两者都会被后续步骤改写：合并会把锚点数量改成段内求和、并新增 SEMI 行，
            # 缺料重建会刷新 ready_status。结果：落库的优先级是**过期值**，
            # 且后置新增的 SEMI 行优先级为 NULL。现把 calculate_priority() 移到
            # 「排程合并 + 缺料重建」之后（见下方 `# 步骤5` 之后）。

            # 步骤2（排程合并前）：先生成半成品(SEMI)排程行并回填日期，
            # 使"排程合并"能按相同物料折叠 SCH+SEMI（用户指定顺序：排程结果→排程合并→缺料明细）。
            # 仅生成 SEMI(generate_semi=True)、不计算缺料(compute_shortage=False)——缺料在合并后阶段统一算。
            _generate_semi_phase(data.get('enabled_stock_types', ['unrestricted']))

            # 规则1（排程归位）：开关 ON 时把过期「计划排产日期」压当日 + 标 delayed，
            # 置于排程合并之前，使归位产生的同日簇被 _merge_schedule_rows 收口（概念1）。
            # 归位前必须先预填「计划排产日期」：半成品(SEMI)行 start 默认空，归位 WHERE
            # (batch_date_start IS NOT NULL) 会跳过空 start 行，导致半成品永不被归位
            # （2026-10-13 重构回归修复：预填提取为 _prefill_schedule_starts，置于归位之前）。
            _prefill_schedule_starts()
            schedule_delay_stat = {'enabled': False, 'today': None, 'schedule': 0, 'shortage': 0, 'work_orders': 0}
            if data['plan_req_date_enabled']:
                schedule_delay_stat = _enforce_schedule_dates()

            # 步骤2：排程合并（按"排程合并天数"）：同物料 + 计划排产日期窗 折叠排程行（SCH+SEMI 参与，WO 独立）。
            # 概念1（开关 ON）：force_same_day 使过期归位产生的同日簇一并折叠；
            # 跳过条件：schedule_merge_days<=0 且未开「计划需求日期」开关时 _merge_schedule_rows 直接 return（完全不合并）。
            _merge_schedule_rows(data.get('schedule_merge_days', 0), force_same_day=data['plan_req_date_enabled'])

            # 步骤3+4：缺料明细（基于"合并后排程结果"）+ 缺料合并（按"缺料合并天数"）。
            # 跳过条件：merge_days<=0 时 _merge_req_lines 直接原样返回（完全不合并）。
            # 内部 _rebuild_all_shortages 传 generate_semi=False，不再重复插入 SEMI（避免合并后数量变化导致
            # 日期错位、重复建行），只基于合并后排程结果算缺料。
            _compute_plan_qty_and_rebuild_shortages(
                data.get('enabled_stock_types', ['unrestricted']), data.get('merge_days', 0))

            # 【2026-10-15】延误标记（排程侧，无条件）：合并+缺料重建后，过期排程行标红（OR 语义）。
            # 必须在「缺料重建」之后：_backfill_semi_dates() 会回写 batch_date_start，标早了日期还会变。
            _mark_schedule_overdue()

            # 步骤5：缺料归位（规则2）已随「缺料重建」(_rebuild_all_shortages 内部)一并完成
            # —— 内存内 归位→合并→内联重算，数量口径与归位同源；排程归位（规则1）已在上方合并前执行。
            # 故此处不再二次归位，仅把排程侧归位统计并入响应（开关关闭时 schedule_delay_stat 保持默认零值）。
            delay_stat = schedule_delay_stat
            # 【2026-10-15】延误标记已解耦为「无条件」：无论「计划需求日期」开关是否开启，
            # 回执都统计界面实际红点数（与 ORM 标记同口径，开关 OFF 也如实播报）。
            delay_stat['schedule'] = query_db(
                "SELECT COUNT(*) c FROM peacock_schedule WHERE is_deleted=0 AND `delayed`=1",
                one=True)['c']
            delay_stat['shortage'] = query_db(
                "SELECT COUNT(*) c FROM peacock_material_shortage WHERE `delayed`=1",
                one=True)['c']
            # 【P0-2】顺排模式下，「无日产量物料退化为压当日」清单的如实上报：
            # 主路径中 _backfill_semi_dates 已先行按同口径顺排（_generate_semi_phase 阶段），
            # _forward_overdue_to_capacity 届时常处理 0 行 → 其 no_capacity_materials 恒空，
            # 前端退化提示会丢。改按落库结果统计：delayed 行中日产量缺失/为 0 的物料
            # （顺排模式下 delayed 行即被顺移的行；WO 不标 delayed，天然排除）。
            # forwarded 同口径补齐（主路径由 backfill 落地，enforce 兜底只计残余行）。
            if delay_stat.get('mode') == 'forward_capacity':
                _daily = _load_daily_capacity()
                _nc = []
                for _r in query_db("SELECT DISTINCT material_code FROM peacock_schedule "
                                   "WHERE is_deleted=0 AND `delayed`=1"):
                    _mc = (_r['material_code'] or '').strip()
                    if _mc and (_daily.get(_mc, 0) or 0) <= 0:
                        _nc.append(_mc)
                delay_stat['no_capacity_materials'] = sorted(_nc)
                delay_stat['forwarded'] = int(delay_stat['schedule'] or 0)

            # 【P2-4】优先级在「排程合并 + 缺料重建」**之后**计算：
            # 此时 plan_qty 已是合并后的最终值、ready_status 已由缺料重建刷新、
            # SEMI 行也已全部生成 → 优先级不再过期、不会漏行。
            resp2 = calculate_priority()
            if isinstance(resp2, tuple):
                resp2 = resp2[0]
            if hasattr(resp2, 'status_code') and resp2.status_code != 200:
                raise _CalcAbort(resp2.get_json() if hasattr(resp2, 'get_json') else {}, resp2.status_code)
            result2 = resp2.get_json() if hasattr(resp2, 'get_json') else resp2
            # 【P3】校验优先级接口实际成功（该接口"无排程行"时返回 200 + success:False）
            if isinstance(result2, dict) and not result2.get('success'):
                logging.getLogger(__name__).warning(
                    '优先级计算未成功（排程行为空？）：%s', result2.get('message'))

        # ── 事务之外 ────────────────────────────────────────────────
        # 主数据完整性校验（日产量/采购周期缺失）：只读 products 表，与计算结果无关，
        # 放在事务外以缩短持锁时间。
        material_warnings = _collect_master_data_warnings()

        return jsonify({
            'success': True,
            'message': '一键排程计算完成（已前置清理：工单 %d / 采购入库 %d / 销售单 %d）'
                       % (cleanup['work_orders'], cleanup['purchase_orders'], cleanup['sales_orders']),
            'cleanup': cleanup,
            'material_warnings': material_warnings,
            'readiness': result1,
            'priority': result2,
            'delay': delay_stat,
        })
    except _CalcAbort as ab:
        # 事务内失败 → 已回滚，按原响应体返回
        return jsonify(ab.payload), ab.status
    except Exception as e:
        # 其余异常：事务已回滚，统一异常响应（仅 DEBUG 附 trace）
        logging.getLogger(__name__).error('一键计算失败（已回滚）：%s', e)
        return jsonify(err_payload(e)), 500
    finally:
        _calc_lock.release()


@bp.route('/api/peacock/schedules')
def peacock_schedules():
    """获取排程结果列表（含销售订单汇总 + 动态可用库存）"""
    # 【P2-21】参数安全化：`?page=abc` 不再 500；per_page 上界 1000 防一次拉爆。
    page = _int_arg('page', 1, lo=1)
    per_page = _int_arg('per_page', 20, lo=1, hi=1000)
    status = request.args.get('status')
    stock_types = request.args.getlist('stock_types') or ['unrestricted']
    offset = (page - 1) * per_page

    conditions = ["s.is_deleted=0"]
    args = []
    if status:
        conditions.append("s.ready_status=%s")
        args.append(status)

    where = " AND ".join(conditions)

    # 构建动态库存求和表达式
    # 在途(transit)不在此 SQL 汇总：统一改用 get_transit_by_material()（采购入库'在途'唯一真相源），
    # 并在下方按"计划到货日期 <= 本行交货日"时间窗计入（与缺料明细口径完全一致），避免：
    #   ① 取冗余列 sap_umlme（可能漂移）；② 把全部在途当成立刻可用（无时间窗）。
    stock_map = {
        'unrestricted': 'SUM(COALESCE(sap_labst,0))',
        'quality': 'SUM(COALESCE(sap_insme,0))',
        'blocked': 'SUM(COALESCE(sap_speme,0))',
    }
    stock_expr = ' + '.join(stock_map[t] for t in stock_types if t in stock_map)
    if not stock_expr:
        stock_expr = '0'
    include_transit = 'transit' in stock_types

    total = query_db(f"SELECT COUNT(*) as cnt FROM peacock_schedule s WHERE {where}", args, one=True)['cnt']

    rows = query_db(f"""
        SELECT s.*,
               COALESCE(so.so_qty, 0) as so_qty,
               COALESCE(so.so_delivered, 0) as so_delivered,
               COALESCE(s.batch_qty, 0) as so_undelivered,
               COALESCE(inv.avail_stock, 0) as inv_stock,
               COALESCE(s.batch_qty, 0) as batch_so_qty,
               0 as wip_qty,
               COALESCE(p.production_cycle, 0) as production_cycle,
               COALESCE(p.production_time, 0) as production_time
        FROM peacock_schedule s
        LEFT JOIN (
            SELECT matnr, vbeln, posnr,
                   SUM(kwmeng) as so_qty,
                   SUM(COALESCE(lfimg,0)) as so_delivered
            FROM sap_sales_order_items
            GROUP BY matnr, vbeln, posnr
        ) so ON s.material_code = so.matnr AND so.vbeln = s.order_no AND so.posnr = s.sales_posnr
        LEFT JOIN (
            SELECT material_code, ({stock_expr}) as avail_stock
            FROM inventory
            GROUP BY material_code
        ) inv ON s.material_code = inv.material_code
        LEFT JOIN products p ON s.material_code = p.material_code
        WHERE {where}
        -- 前台排程结果最终显示顺序：按「计划排产日期」(batch_date_start) 升序；
        -- 计划排产日期缺失/空字符串时回落计划完工/交货日(batch_date_end)，再按物料编码稳定排序。
        ORDER BY COALESCE(NULLIF(TRIM(s.batch_date_start), ''), s.batch_date_end, '99999999'), s.material_code
    """, args)

    # 保留原始 aufnr（DB 序号/订单号），供前端"生成订单"作为工单稳定链接键(schedule_ref)使用。
    # 下方展示逻辑会把 aufnr 改写成顶层计划订单号(如 XS001-2)，若直接用其作链接键，
    # 工单删除时按 aufnr 复位会匹配不上，导致 wo_generated 不清零、按钮恒灰。
    for row in rows:
        row['raw_aufnr'] = row.get('aufnr')

    # ===== SCH 行 销售订单/未发货 口径修正 =====
    # 销售订单列须显示「订单数量(kwmeng, 即 so_qty)」；未发货 = 订单数量 - 已发货 = 未发数量(=毛需求)。
    # 早期版本误用 sales_qty(毛需求) 作 batch_so_qty，导致：
    #   ① 销售订单列错显 1350（应为 1500）；② 未发货被二次扣减已发货→1200（应为 1350）；
    #   ③ 净需求迭代少冲减库存(undelivered 少算 150)。改用 so_qty(订单数量) 作 gross，三处同时正确。
    for row in rows:
        if row.get('source') == 'SCH':
            gross = float(row.get('so_qty') or 0)           # 订单数量
            if gross <= 0:
                gross = float(row.get('sales_qty') or 0)   # 兜底：无订单数量时用毛需求
            row['batch_so_qty'] = gross
            row['so_undelivered'] = max(0.0, gross - float(row.get('so_delivered') or 0))

    # ===== 合并行（source_refs 含多张单）销售订单/已发货/未发货 跨单求和 =====
    # 排程合并后锚点行仅保留首张单的 order_no/sales_posnr，原单张单 JOIN 只会取到首张单的销售数据，
    # 导致"未发货/已发货/销售订单"漏算其余被合并的销售单（如 C001 漏算 XS002-02）。
    # 此处按 source_refs 解析所有 'vbeln-posnr' 单据，以 (matnr, vbeln, posnr) 匹配 sap 销售明细，
    # 对全部 SCH 销售单求和；SEMI 驱动的 token(drive_aufnr) 因 matnr 不匹配被自动排除。
    _sap_agg = query_db(
        "SELECT matnr, vbeln, posnr, SUM(kwmeng) as so_qty, SUM(COALESCE(lfimg,0)) as so_delivered "
        "FROM sap_sales_order_items GROUP BY matnr, vbeln, posnr"
    )
    _sap_map = {}
    for _r in _sap_agg:
        try:
            _pi = int(_r.get('posnr') or 0)
        except Exception:
            _pi = _r.get('posnr')
        _sap_map[(_r['matnr'], _r['vbeln'], _pi)] = _r

    for row in rows:
        _refs = (row.get('source_refs') or '').strip()
        if '、' not in _refs:
            continue
        _mat = row.get('material_code')
        _qty = 0.0
        _del = 0.0
        _hit = False
        for _tok in _refs.split('、'):
            _tok = _tok.strip()
            if not _tok or '-' not in _tok:
                continue
            _v, _p = _tok.rsplit('-', 1)
            try:
                _pi = int(_p)
            except Exception:
                _pi = _p
            _sd = _sap_map.get((_mat, _v, _pi))
            if _sd:
                _hit = True
                _qty += float(_sd.get('so_qty') or 0)
                _del += float(_sd.get('so_delivered') or 0)
        if _hit:
            row['so_qty'] = _qty
            row['so_delivered'] = _del
            row['batch_so_qty'] = _qty
            row['so_undelivered'] = max(0.0, _qty - _del)

    # V2.0 标准净需求：逐行迭代，先冲减后排产
    if rows:
        from datetime import datetime, date  # datetime 仍用于下方 planned_end 解析；date 历史遗留
        # 【P1-5 清理·2026-10-14】原 `today = date.today()` 为死赋值：WIP 全局逾期过滤
        # 早已移除（任务#29），此变量无任何下游使用。顺带消除一处 OS 时区依赖。

        wo_rows = query_db(
            "SELECT id, order_no, material_code, planned_end, COALESCE(confirm_qty,0)-COALESCE(completed_qty,0) as remaining_qty FROM work_orders WHERE status IN ('批准','执行')"
        )
        wip_pool_by_mat = {}
        for wo in wo_rows:
            remaining = float(wo.get('remaining_qty') or 0)
            if remaining <= 0:
                continue
            pe = wo.get('planned_end')
            pe_dt = None
            if pe:
                try:
                    if isinstance(pe, datetime):
                        pe_dt = pe.date()
                    else:
                        pe_dt = datetime.strptime(str(pe)[:10], '%Y-%m-%d').date()
                except (ValueError, TypeError) as e:
                    # 原为 `except: pass` 全吞异常。此处解析失败会把该工单静默踢出
                    # WIP 供给池（planned_end 为空 ⇒ 不参与 wip_list 排序），
                    # 可能导致缺料误报，故显式记录。
                    logging.getLogger(__name__).warning(
                        '工单 %s 的 planned_end 解析失败(%s)，该工单不计入 WIP 在制供给',
                        wo.get('order_no'), e)
            wip_pool_by_mat.setdefault(wo['material_code'] or '', []).append({
                'id': wo['id'], 'order_no': wo.get('order_no') or '',
                'qty': float(wo.get('remaining_qty') or 0), 'planned_end': pe_dt
            })

        # 物料级「在制供应」参考值：该物料全部在制工单（批准/执行）未完工量之和
        # （如 C001 = GD001 150 + GD002 10 = 160），作为 WIP在制 列的参考展示，
        # 与库存是否覆盖本行无关；计划排产仍使用实际时间相位耗用(consumed)。
        wip_supply_by_mat = {
            mat: sum(float(w.get('qty') or 0) for w in lst)
            for mat, lst in wip_pool_by_mat.items()
        }

        mat_batches = {}
        for row in rows:
            mat_batches.setdefault(row.get('material_code') or '', []).append(row)

        # 在途池（唯一真相源 get_transit_by_material）：采购入库'在途'按物料+到货日期分桶，
        # 下方按"到货日期 <= 本行交货日"时间窗计入（与缺料明细口径完全一致）。
        _transit_pools = _load_transit_pools()

        for mat, batches in mat_batches.items():
            # 【P2-5 修复】补 `id` 兜底成全序：原键 (batch_date_end, aufnr) 在同一物料
            # 同交货日的多行间仍可并列，而本循环的耗用顺序（库存/WIP/在途池）直接取决于它
            # → 分配不稳定。补 uuid 唯一的 id 后顺序确定，可复核、可复现。
            batches.sort(key=lambda r: (r.get('batch_date_end') or r.get('aufnr') or '',
                                        str(r.get('id') or '')))
            stock_pool = float(batches[0].get('inv_stock') or 0) if batches else 0.0
            # WIP 在制池：按完工日升序，建立"剩余可耗用量"池（时间从上到下顺序消耗，非固定值）
            wip_list = sorted(
                [w for w in wip_pool_by_mat.get(mat, []) if w.get('planned_end')],
                key=lambda w: w['planned_end']
            )
            wip_remaining = {w['id']: float(w['qty']) for w in wip_list}

            # 在途分桶（按到货日期升序）+ 时间窗指针（每个桶仅加一次）
            _buckets = _transit_pools.get(mat)
            _bi = 0
            _transit_pool = 0.0

            for batch in batches:
                batch_date = batch.get('batch_date_end') or batch.get('batch_date_start') or ''
                bd = None
                if batch_date:
                    try:
                        bd = datetime.strptime(str(batch_date)[:8], '%Y%m%d').date()
                    except (ValueError, TypeError) as e:
                        # 原为 `except: pass`。排程日期列为 varchar(8) 'YYYYMMDD'，
                        # 解析失败时 bd 保持 None ⇒ 该行不参与在途时间窗判定。
                        logging.getLogger(__name__).warning(
                            '排程行日期解析失败(%s)：%r', e, batch_date)

                # 把"到货日期 <= 本行交货日"的在途桶加入可用池（按到货日期升序，每个桶仅加一次）；
                # 与缺料明细 _apply_inventory_and_transit 同源：先耗实物、再耗在途。
                # 【P1-2 修复·2026-10-14】入池条件由乐观口径（_bd/bd 为 None 也放行）改为保守口径：
                #   双日期均非 None 且 _bd <= bd 才入池，与存储层 _rebuild_all_shortages(:1203)
                #   逐字一致。原乐观口径下：① 本行交货日解析失败(bd=None)会把**全部**在途桶
                #   （含晚于本行的未来到货）一次性吞入，且指针 _bi 走到底 → 后续行在途全丢；
                #   ② 无计划到货日期的在途桶(_bd=None)也会被无条件放行入池。
                #   改为保守口径后，日期缺失行不参与在途窗判定（与存储层行为一致，可复核、可对账）。
                if include_transit and _buckets:
                    while _bi < len(_buckets):
                        _bd, _bq = _buckets[_bi]
                        if _bq <= 0:
                            _bi += 1
                            continue
                        if _bd is not None and bd is not None and _bd <= bd:
                            _transit_pool += _bq
                            _bi += 1
                        else:
                            break

                # 工单来源(WO)：工单自身即为"在制"，WIP=本工单未完工数量；不参与需求冲减
                if batch.get('source') == 'WO':
                    batch['wip_qty'] = float(batch.get('batch_qty') or 0)
                    batch['inv_stock'] = None
                    batch['so_delivered'] = None
                    batch['so_undelivered'] = None
                    batch['computed_plan_qty'] = float(batch.get('batch_qty') or 0)
                    continue

                # SCH / SEMI：先消耗「可用库存(实物+时间窗在途)」，再消耗「WIP在制」（时间相位，非固定值）
                avail_stock = max(0.0, stock_pool) + (_transit_pool if include_transit else 0.0)
                undelivered = max(0.0, float(batch.get('batch_so_qty') or 0) - float(batch.get('so_delivered') or 0))
                # ① 先消耗可用库存（库存池为物料级共享，按时间从上到下顺序消耗）
                stock_used = min(undelivered, avail_stock)
                remaining = undelivered - stock_used
                # ② 再消耗 WIP 在制（仅取 planned_end <= 本行交货日的剩余量，按完工日升序顺序耗用）
                consumed = 0.0
                if remaining > 0 and bd:
                    for w in wip_list:
                        if consumed >= remaining:
                            break
                        pe = w.get('planned_end')
                        rid = w['id']
                        if pe and pe <= bd and wip_remaining[rid] > 0:
                            take = min(wip_remaining[rid], remaining - consumed)
                            if take > 0:
                                wip_remaining[rid] -= take
                                consumed += take
                batch['inv_stock'] = avail_stock
                batch['wip_qty'] = consumed
                # WIP在制(参考)：该物料全部在制工单未完工量之和，作为「在制供应」参考展示
                batch['wip_supply'] = wip_supply_by_mat.get(mat, 0.0)
                batch['computed_plan_qty'] = max(0.0, undelivered - stock_used - consumed)
                # 库存池：先扣实物、再扣时间窗在途（与缺料明细"先耗实物再耗在途"一致；WIP 已单独记账）
                _phys_used = min(stock_used, max(0.0, stock_pool))
                stock_pool = max(0.0, stock_pool - _phys_used)
                _transit_pool = max(0.0, _transit_pool - (stock_used - _phys_used))

            # GET 只读：不再写回 plan_qty / ready_status（事实源为一键计算与 PUT 接口）

        # ===== 销售订单来源(SCH)行展示修正（V2.0 之后）=====
        # batch_so_qty / so_undelivered 已在 V2.0 之前修正
        # WIP在制(wip_qty) 已由上面的时间相位消耗逻辑写入：按(物料,交货日)从上到下顺序耗用，非固定值
        for batch in rows:
            if batch.get('source') == 'SCH':
                posnr = batch.get('sales_posnr') or ''
                if batch.get('order_no'):
                    batch['aufnr'] = (f"{batch['order_no']}-{posnr}" if posnr else batch['order_no'])
                # 已覆盖行（batch_qty=0 且 gross>0）：标记状态
                gross = float(batch.get('batch_so_qty') or 0)
                if float(batch.get('batch_qty') or 0) == 0 and gross > 0:
                    batch['ready_status'] = '已覆盖'
                    batch['status_description'] = '库存+工单已全额覆盖'

        # ===== 半成品来源(SEMI)行展示修正 =====
        # SEMI 为动态补入的生产计划，无外部订单号（order_no 为空），DB aufnr 存的是排程序列号。
        # 计划订单号改为显示其父件（直接父件）的计划订单号：
        #   父件为 SCH/WO -> 直接取父件的 aufnr（如 XS003-1 / GD006）；
        #   父件为另一 SEMI -> 继续向上追溯直到顶层 SCH/WO（如 B002 经 B001 追溯到 XS003-2）。
        _order_aufnr = {}   # material_code -> 顶层订单的 aufnr（优先取 SCH，其次 WO）
        _semi_parent = {}    # material_code -> 直接父件物料（仅 SEMI 行）
        for batch in rows:
            m = batch.get('material_code') or ''
            bsrc = batch.get('source')
            if bsrc == 'SEMI':
                _semi_parent[m] = batch.get('parent_code') or ''
            elif bsrc in ('SCH', 'WO'):
                if m not in _order_aufnr or bsrc == 'SCH':
                    _order_aufnr[m] = batch.get('aufnr')

        def _resolve_top_order(mat):
            """从某物料出发，沿父件链向上，返回顶层 SCH/WO 订单的计划订单号。"""
            seen = set()
            cur = mat
            while cur and cur not in seen:
                seen.add(cur)
                if cur in _order_aufnr:
                    return _order_aufnr[cur]
                cur = _semi_parent.get(cur)
            return None

        for batch in rows:
            if batch.get('source') == 'SEMI':
                # 半成品行显示"顶层驱动订单号"，才能知道该半成品(B002/B003)是为哪个产品准备的。
                # 优先用生成时记录的 drive_aufnr；没有则沿父件链向上解析（SCH 优先）。
                drive = batch.get('drive_aufnr') or ''
                if drive:
                    batch['aufnr'] = drive
                else:
                    parent = batch.get('parent_code') or ''
                    top = _resolve_top_order(parent) if parent else None
                    batch['aufnr'] = top if top else (batch.get('material_code') or batch.get('aufnr') or '')

    # ===== 排程结果排序：计划排产日期(batch_date_start) → 物料编码 =====
    # 严格按「计划排产日期」升序，同日再按「物料编码」稳定排序（用户明确要求，2026-08-20）。
    # 计划排产日期缺失/空时回落计划完工/交货日(batch_date_end)。
    # 注意：此最终排序会覆盖上方 SQL 的 ORDER BY 与同物料内重排，是前台展示顺序的唯一决定处。
    # 【P2-5 修复 · 2026-10-14】原排序键为 (日期, 物料编码)，**非全序**：
    # 同一物料同一天有多行时（不同 SCH 单/SEMI/WO），相对顺序取决于上游 SQL 与 dict 顺序，
    # 分页切片时可能出现重复行或漏行；WIP/库存的逐行分配也随之不稳定。
    # 现补 `aufnr` 与 `id`（uuid 唯一）兜底 → 全序，翻页稳定；并列行的相对顺序仍确定性。
    rows.sort(key=lambda r: (
        str(r.get('batch_date_start') or r.get('batch_date_end') or '99999999'),
        r.get('material_code') or '',
        str(r.get('aufnr') or ''),
        str(r.get('id') or '')
    ))

    # 分页（全局排序后切片）
    page_rows = rows[offset:offset + per_page]

    return jsonify({
        'success': True,
        'total': total,
        'page': page,
        'per_page': per_page,
        'data': serialize_rows(page_rows)
    })


@bp.route('/api/peacock/schedule/<schedule_id>/details')
def peacock_schedule_details(schedule_id):
    """排程详情（支持动态库存类型）"""
    stock_types = request.args.getlist('stock_types') or ['unrestricted']
    # 【P2-28 修复】按主键读也要带 is_deleted=0：合并折叠的行是**软删**（is_deleted=1），
    # 原实现不加条件仍能读出 → 详情弹窗会显示已合并掉的行（"幽灵数据"）。
    schedule = query_db("SELECT * FROM peacock_schedule WHERE id=%s AND is_deleted=0",
                        (schedule_id,), one=True)
    if not schedule:
        return jsonify({'success': False, 'message': '未找到排程记录'}), 404

    plan_qty_db = schedule.get('plan_qty')
    plan_qty = float(plan_qty_db) if plan_qty_db is not None else None
    matnr = schedule.get('material_code') or ''
    shortages = query_db("SELECT * FROM peacock_material_shortage WHERE schedule_id=%s", (schedule_id,))

    # 查询 BOM 获取 yield_qty 和各子件的 comp_menge
    bom_map = {}
    if matnr:
        bom_rows = query_db(
            "SELECT comp_matnr, comp_menge, yield_qty FROM bom WHERE parent_matnr=%s",
            (matnr,)
        )
        if bom_rows:
            for b in bom_rows:
                bom_map[b['comp_matnr']] = b

    # 根据当前库存类型重新计算可用库存和需求数量
    inv_map = get_inventory_map(stock_types=stock_types)

    # 如果 plan_qty 是 NULL（未设置），用 batch_qty 作为默认排产数量
    if plan_qty is None:
        batch_qty = float(schedule.get('batch_qty') or 0)
        if batch_qty > 0:
            plan_qty = batch_qty
        else:
            so_data = query_db(
                "SELECT SUM(kwmeng) as so_qty, SUM(COALESCE(lfimg,0)) as so_delivered "
                "FROM sap_sales_order_items WHERE matnr=%s",
                (matnr,), one=True
            )
            so_qty = float(so_data['so_qty']) if so_data and so_data['so_qty'] else 0
            so_delivered = float(so_data['so_delivered']) if so_data and so_data['so_delivered'] else 0
            inv_stock = inv_map.get(matnr, {}).get('available', 0)
            plan_qty = max(0, so_qty - so_delivered - inv_stock)
    # 递归库存消耗：与缺料明细一致，所有行按日期排序逐行消耗
    all_shortages = query_db("""
        SELECT ms.*,
               COALESCE(s.batch_date_end, DATE_FORMAT(wo.planned_start,'%Y%m%d')) as sort_date
        FROM peacock_material_shortage ms
        LEFT JOIN peacock_schedule s ON ms.schedule_id = s.id
        LEFT JOIN work_orders wo ON ms.schedule_id = wo.id AND s.id IS NULL
        WHERE ms.required_qty > 0
        ORDER BY sort_date, ms.aufnr, ms.component_code
    """)
    include_transit = 'transit' in stock_types
    result_shortages = []
    for st in all_shortages:
        # 仅处理本排程记录关联的缺料行
        if st['schedule_id'] != schedule_id:
            continue
        code = st['component_code']
        inv = inv_map.get(code, {})
        st['sap_labst'] = inv.get('sap_labst', 0)
        st['sap_insme'] = inv.get('sap_insme', 0)
        st['sap_speme'] = inv.get('sap_speme', 0)
        st['sap_umlme'] = inv.get('sap_umlme', 0)

        # 重算需求数量 = (plan_qty / yield_qty) * comp_menge
        effective_qty = plan_qty if plan_qty is not None else 0
        if code in bom_map:
            if effective_qty > 0:
                bom = bom_map[code]
                yield_qty = float(bom.get('yield_qty') or 1)
                comp_menge = float(bom.get('comp_menge') or 0)
                if yield_qty > 0:
                    st['required_qty'] = (effective_qty / yield_qty) * comp_menge
            else:
                st['required_qty'] = 0

        result_shortages.append(st)

    # 🔴【P2-8 修复 · 2026-10-14】原实现只对**本排程行**的缺料行做耗用，
    # 于是每一行的可用量基线都是"全量库存" —— 而缺料明细列表是把**全部**行按日期
    # 依次耗用后的结果。两者基线不同 → 弹窗的可用量永远比列表**乐观**（缺料更少），
    # 上方注释所称"与缺料明细完全一致"并不成立。
    # 现改为：先对**全量**缺料行跑一次耗用（与列表同源同序），再按 schedule_id 过滤展示。
    _all_rows = [dict(r) for r in all_shortages]
    _apply_inventory_and_transit(_all_rows, inv_map, stock_types, include_transit)
    _by_id = {r.get('id'): r for r in _all_rows}
    result_shortages = [_by_id[r.get('id')] for r in result_shortages if r.get('id') in _by_id]

    # 按子件编码排序
    result_shortages.sort(key=lambda x: x.get('component_code', ''))

    return jsonify({
        'success': True,
        'schedule': serialize_row(schedule),
        'shortages': serialize_rows(result_shortages)
    })


def _get_shortage_rows(stock_types, include_transit):
    """计算缺料明细行（与 /api/peacock/shortages/all 完全一致的数据），供接口与导出复用。"""
    # 注：parent_qty 列补建已上提到启动迁移 _migrate()。原先在此处（查询路径）执行 ALTER，
    # 等于只读接口触发 DDL：既产生 DDL 锁，又会在事务内隐式提交。此处不再做 DDL。
    rows = query_db(
        "SELECT "
        "s.material_name as sch_material_name, p.name as prod_name, "
        "s.batch_date_end as sch_date_end, wo.planned_start as wo_start, "
        "s.plan_qty, wo.confirm_qty, wo.completed_qty, "
        "ms.id, ms.schedule_id, ms.work_order_id, ms.material_code, ms.component_code, ms.component_name, "
        "ms.component_type, ms.parent_code, ms.bom_level, ms.is_parent_material, "
        "ms.required_qty, ms.available_qty, ms.current_stock, ms.quality_stock, ms.frozen_stock, "
        "ms.shortage_type, ms.stock_status, ms.created_at, ms.updated_at, ms.source, ms.source_refs, ms.component_demand_date, "
        "ms.`delayed`, "
        "ms.parent_qty, "
        # 【P0/P1 · 2026-10-20】缺口数量/建议采购量/最迟下单日/在途匹配/合并成员（_rebuild_all_shortages 落库；
        # 旧数据为 NULL，下方展示层按同一口径兜底重算，保证弹窗与导出不因缺列缺值而空白）
        "ms.shortage_qty, ms.suggested_purchase_qty, ms.latest_order_date, "
        "ms.intransit_qty, ms.intransit_arrival_date, ms.intransit_match, "
        "ms.member_count, ms.merge_members, "
        "ms.aufnr, s.aufnr as sch_aufnr, s.source as sch_source, s.order_no as sch_sales_orders, "
        "s.sales_posnr as sch_posnr, s.drive_aufnr as sch_drive, s.parent_code as sch_parent, "
        "s.source_refs as sch_source_refs, "
        "wo.order_no as wo_order_no "
        "FROM peacock_material_shortage ms "
        "LEFT JOIN peacock_schedule s ON ms.schedule_id = s.id "
        "LEFT JOIN work_orders wo ON ms.work_order_id = wo.id "
        "LEFT JOIN products p ON ms.material_code = p.material_code "
        # 【P2-5·2026-10-14】补 ms.id 兜底成全序：原 ORDER BY 三键理论上可并列
        # （同 aufnr+component_code 多行），分页翻页时并列行序不稳定会重复/丢行；
        # ms.id 为 uuid 唯一 → 全序，非分页调用行为不变（仅并列时序确定化）。
        "ORDER BY COALESCE(s.batch_date_end, DATE_FORMAT(wo.planned_start,'%Y%m%d')), ms.aufnr, ms.component_code, ms.id"
    )

    # Python 层合并 material_name
    for r in rows:
        r['material_name'] = r.get('sch_material_name') or r.get('prod_name') or ''

    # ===== 缺料明细「来源」= 排程结果.计划订单号（与排程结果页签口径完全一致）=====
    # 背景：peacock_schedule.aufnr 对 SCH/SEMI 行存的是排程序列号(seq，如 0001)，
    # 与排程结果展示用的计划订单号(order_no-posnr / 顶层订单号)不一致。
    # 这里按排程结果展示逻辑重算每一行的 来源，保证两个页签的"来源/计划订单号"完全一致。
    _sched_all = query_db(
        "SELECT material_code, source, parent_code, aufnr, order_no, sales_posnr, drive_aufnr "
        "FROM peacock_schedule WHERE is_deleted=0"
    )
    _order_aufnr = {}   # material_code -> 顶层 SCH/WO 计划订单号（SCH 优先）
    _semi_parent = {}   # material_code -> 直接父件物料（仅 SEMI 行）
    for _b in _sched_all:
        _m = _b.get('material_code') or ''
        _bs = _b.get('source')
        if _bs == 'SEMI':
            _semi_parent[_m] = _b.get('parent_code') or ''
        elif _bs in ('SCH', 'WO'):
            if _bs == 'SCH' and _b.get('order_no'):
                _a = (f"{_b['order_no']}-{_b['sales_posnr']}"
                      if _b.get('sales_posnr') else _b['order_no'])
            else:
                _a = _b.get('aufnr') or ''
            if _m not in _order_aufnr or _bs == 'SCH':
                _order_aufnr[_m] = _a

    def _resolve_top_order(mat):
        """从某物料出发，沿父件链向上，返回顶层 SCH/WO 订单的计划订单号。"""
        _seen = set(); _cur = mat
        while _cur and _cur not in _seen:
            _seen.add(_cur)
            if _cur in _order_aufnr:
                return _order_aufnr[_cur]
            _cur = _semi_parent.get(_cur)
        return None

    for st in rows:
        # 合并行（缺料合并天数>0 产生）：无论来源类型是否单一，只要合并出多个单据，
        # 来源字段就直接罗列本段所有单据号(source_refs)，多个单据以顿号分隔。
        # 非合并行 source_refs 为空，回退到下方按来源类型的展示逻辑。
        # 合并行(source_refs 非空)：罗列本段所有单据号；非合并行(缺料合并天数=0)source_refs 为空，
        # 必须回退到下方按来源类型的展示逻辑（直接取排程结果计划订单号）。
        # 注意：此处不可用 `or '0'`——空串会被当成 '0'（真值）而错误跳过 per-type 逻辑，导致来源显示异常。
        _refs = (st.get('source_refs') or '').strip()
        if _refs:
            st['source_display'] = _refs
            continue
        # 缺料未合并，但对应的「排程结果」行本身是合并行(排程合并天数>0)：直接取排程结果展示的
        # 全部计划订单号(sch_source_refs)，保证缺料明细「来源」与排程结果页签口径完全一致。
        _sch_refs = (st.get('sch_source_refs') or '').strip()
        if _sch_refs:
            st['source_display'] = _sch_refs
            continue
        _ss = (st.get('sch_source') or st.get('source') or '').strip()
        if _ss == 'WO':
            st['source_display'] = (st.get('wo_order_no') or st.get('sch_aufnr') or '').strip()
        elif _ss == 'SCH':
            _ono = (st.get('sch_sales_orders') or '').strip()
            _pos = (st.get('sch_posnr') or '').strip()
            st['source_display'] = (f"{_ono}-{_pos}" if (_ono and _pos) else _ono) \
                or (st.get('sch_aufnr') or '').strip()
        elif _ss == 'SEMI':
            # 半成品来源的「来源」= 该半成品在排程结果中的"计划订单号"(drive_aufnr，顶层驱动订单号)，
            # 与排程结果页签口径一致；这样才知道该半成品是为哪个产品准备的。
            _drive = (st.get('sch_drive') or '').strip()
            if _drive:
                st['source_display'] = _drive
            else:
                _top = _resolve_top_order(st.get('sch_parent') or '') if st.get('sch_parent') else None
                st['source_display'] = _top or (st.get('sch_aufnr') or st.get('material_code') or '').strip()
        else:
            st['source_display'] = (st.get('source') or '').strip()

    # 根据当前库存类型重新计算可用库存和需求数量
    inv_map = get_inventory_map(stock_types=stock_types)

    # 获取所有排程的 plan_qty 和 BOM 数据（批量加速）
    schedule_ids = list(set(st.get('schedule_id') for st in rows))
    plan_map = {}
    if schedule_ids:
        ph = ','.join(['%s'] * len(schedule_ids))
        plans = query_db(f"SELECT id, material_code, plan_qty, batch_qty, batch_date_end FROM peacock_schedule WHERE id IN ({ph})", schedule_ids)
        for p in plans:
            plan_map[p['id']] = p
    # 获取所有 BOM 数据
    all_matnrs = list(set(p.get('material_code', '') for p in plan_map.values() if p.get('material_code')))
    bom_data = {}
    if all_matnrs:
        ph2 = ','.join(['%s'] * len(all_matnrs))
        bom_rows = query_db(
            f"SELECT parent_matnr, comp_matnr, comp_menge, yield_qty FROM bom WHERE parent_matnr IN ({ph2})",
            all_matnrs
        )
        for b in bom_rows:
            key = (b['parent_matnr'], b['comp_matnr'])
            bom_data[key] = b

    # 第一遍：计算所有记录的初始可用库存和需求数量
    for st in rows:
        st['available_qty'] = _recalc_available(st['component_code'], stock_types, inv_map)
        inv = inv_map.get(st['component_code'], {})
        st['sap_labst'] = inv.get('sap_labst', 0)
        st['sap_insme'] = inv.get('sap_insme', 0)
        st['sap_speme'] = inv.get('sap_speme', 0)
        st['sap_umlme'] = inv.get('sap_umlme', 0)
        # 当前库存 = 按已选可用库存类型的汇总
        st['current_stock'] = inv.get('available', 0)

        # 重算需求数量：统一以「净需求 batch_qty」为唯一来源（与缺料行创建 _explode 用的 batch_qty
        # 完全一致），不再依赖 plan_qty（plan_qty 当前==batch_qty，但属脆弱耦合，一旦两者不一致
        # 缺料"需求数量"即失真，且原 plan_qty is None 的兜底会算出 batch_qty-库存 的错误净值）。
        sch = plan_map.get(st.get('schedule_id', ''), {})
        batch_qty = float(sch.get('batch_qty') or 0)
        matnr = sch.get('material_code') or ''

        # 合并行（缺料合并天数>0 产生，source_refs 非空）：需求数量已在写入缺料表时
        # 按多单据聚合（required_qty = 段内 gross 之和，如 GD002(10)+GD001(150)=160），
        # 此处严禁再用锚点行的 schedule_id/batch_qty 反算（那只算到锚点单一单据量，会丢掉合并单据）。
        # 直接保留 SELECT 取到的库表合并值，跳过此处重算。
        if (st.get('source_refs') or '').strip():
            pass
        elif matnr:
            bom_key = (matnr, st['component_code'])
            bom = bom_data.get(bom_key)
            if bom:
                if batch_qty > 0:
                    yield_qty = float(bom.get('yield_qty') or 1)
                    comp_menge = float(bom.get('comp_menge') or 0)
                    if yield_qty > 0:
                        st['required_qty'] = (batch_qty / yield_qty) * comp_menge
                else:
                    st['required_qty'] = 0

    # 获取所有物料的生产/采购周期 + 日产量（父件 + 子件统一加载）
    all_matnrs = list(set(st.get('material_code', '') for st in rows if st.get('material_code')))
    all_comp_codes = list(set(st.get('component_code', '') for st in rows if st.get('component_code')))
    all_prod_codes = list(set(all_matnrs + all_comp_codes))
    cycle_map = {}
    daily_map = {}
    if all_prod_codes:
        ph3 = ','.join(['%s'] * len(all_prod_codes))
        cycle_rows = query_db(
            f"SELECT material_code, production_cycle, production_time FROM products WHERE material_code IN ({ph3})",
            all_prod_codes
        )
        for cr in cycle_rows:
            cycle_map[cr['material_code']] = float(cr.get('production_cycle') or 0)
            daily_map[cr['material_code']] = float(cr.get('production_time') or 0)

    def _lead(matnr, qty):
        """生产提前期天数：优先 ceil(需求数量/日产量)，日产量缺失/0 时回退固定生产周期。"""
        daily = daily_map.get(matnr, 0) or 0
        if daily > 0 and qty and qty > 0:
            return math.ceil(qty / daily)
        return cycle_map.get(matnr, 0) or 0

    # 计算产品需求日期
    # 排程来源：计划排产日期 = 交货日期 - 物料周期
    # 工单来源：计划开工日期 - 物料周期
    wo_schedule_ids = [st.get('work_order_id','') for st in rows if st.get('work_order_id')]
    wo_dates = {}
    if wo_schedule_ids:
        ph4 = ','.join(['%s'] * len(wo_schedule_ids))
        wo_rows = query_db(f"SELECT id, planned_start FROM work_orders WHERE id IN ({ph4})", wo_schedule_ids)
        for w in wo_rows:
            ps = w.get('planned_start')
            if ps:
                from datetime import datetime as dt2
                wo_dates[w['id']] = ps.isoformat()[:10] if isinstance(ps, dt2) else str(ps)[:10]

    for st in rows:
        is_work_order = not st.get('sch_date_end')
        # 先算需求数量（提前期按 数量÷日产量）
        if is_work_order:
            _dq = float(st.get('confirm_qty') or 0) - float(st.get('completed_qty') or 0)
        else:
            _dq = float(st.get('plan_qty') or 0)
        mat_cycle = _lead(st.get('material_code', ''), _dq)
        from datetime import datetime, timedelta
        if is_work_order:
            # 工单：计划开工日期 - 物料周期
            wo_date = wo_dates.get(st.get('work_order_id',''), '')
            if wo_date and mat_cycle:
                try:
                    pd = datetime.strptime(wo_date[:10], '%Y-%m-%d')
                    st['demand_date'] = (pd - timedelta(days=int(mat_cycle))).strftime('%Y-%m-%d')
                except (ValueError, TypeError) as e:
                    # 【P2-19 修复】原为裸 `except:` —— 会连键盘中断、内存错误一起吞掉，
                    # 且**静默**产出错误的 demand_date（本分支会直接退回 wo_date，
                    # 即"没减提前期"），后续在途时间窗据此判定 → 缺料误判且无日志。
                    logging.getLogger(__name__).warning('工单需求日期推算失败(%s)：%r', e, wo_date)
                    st['demand_date'] = wo_date
            else:
                st['demand_date'] = wo_date
        else:
            # 排程：计划排产日期 = 交货日期 - 物料周期
            batch_date = st.get('sch_date_end', '') or ''
            if batch_date and mat_cycle:
                try:
                    bd = datetime.strptime(str(batch_date)[:8], '%Y%m%d')
                    st['demand_date'] = (bd - timedelta(days=int(mat_cycle))).strftime('%Y-%m-%d')
                except (ValueError, TypeError) as e:
                    # 【P2-19 修复】同上：裸 except → 收窄 + 留痕
                    logging.getLogger(__name__).warning('排程需求日期推算失败(%s)：%r', e, batch_date)
                    st['demand_date'] = batch_date
            else:
                st['demand_date'] = batch_date

    # 子件需求日期：优先使用缺料表已落库的"锚定最早日期"（合并行直接落库；非合并行等价），
    # 仅当缺料表未记录(component_demand_date 为空)时，按"产品需求日期-子件提前期"回退计算（兼容历史数据）。
    for st in rows:
        _stored_cdd = (st.get('component_demand_date') or '').strip()
        if _stored_cdd:
            st['component_demand_date'] = _stored_cdd
        else:
            dem = st.get('demand_date', '')
            cycle = _lead(st.get('component_code', ''), float(st.get('required_qty') or 0))
            if dem and cycle:
                try:
                    from datetime import datetime, timedelta
                    dd = datetime.strptime(str(dem)[:10], '%Y-%m-%d')
                    st['component_demand_date'] = (dd - timedelta(days=int(cycle))).strftime('%Y-%m-%d')
                except (ValueError, TypeError) as e:
                    # 【P2-19 修复】同上：裸 except → 收窄 + 留痕
                    logging.getLogger(__name__).warning('子件需求日期推算失败(%s)：%r', e, dem)
                    st['component_demand_date'] = dem
            else:
                st['component_demand_date'] = dem

    # 产品需求数量
    # 优先使用「直接父件生产量」parent_qty（多级 BOM 展开时记录于缺料行）：
    # 对"成品→半成品→原料"的深层级，原本 schedule_id 会指向顶层销售单，导致"产品需求"
    # 误显为顶层订单量（如 C003 顶单 1100 而非其下 B002 半成品的 22）。parent_qty
    # 在 BOM 递归展开时按"当前父件"记录，与层级无关，显示正确。无该值(旧数据/工单)回落 plan_qty。
    for st in rows:
        is_wo = not st.get('sch_date_end')
        if is_wo:
            confirm = float(st.get('confirm_qty') or 0)
            completed = float(st.get('completed_qty') or 0)
            # 【P2-7 修复 · 2026-10-14】加 max(0) 保护：超产完工(completed > confirm)时
            # 差值为负 →「产品需求」显示负数（业务上不存在负需求），
            # 且该值还会被 `_lead()` 用于工期换算，负数会让提前期回退到固定周期、
            # 与"按数量÷日产量"的口径不一致 → 日期计算随之错位。
            st['demand_qty'] = max(0.0, confirm - completed)
        else:
            _pq = float(st.get('parent_qty') or 0)
            st['demand_qty'] = _pq if _pq > 0 else float(st.get('plan_qty') or 0)

    # 排序：子件需求日期 → 子件编码（展示顺序）
    # 【P2-5】补 `id` 兜底成全序（同上，防并列行顺序不定）
    rows.sort(key=lambda x: (x.get('component_demand_date', '') or '',
                             x.get('component_code', '') or '',
                             str(x.get('id') or '')))

    # 统一的库存耗用 + 在途分配（递归/逐单耗用）：
    # 在途按到货日期进入可用池，展示在途=当前可用在途池（不按子件需求截断），并递归耗用。
    _apply_inventory_and_transit(rows, inv_map, stock_types, include_transit)

    # 【P0-1 · 2026-10-20】缺口数量 / 建议采购量 / 最迟下单日 展示值：
    # 优先用缺料表落库值（合并行的数量口径以落库为准，展示层不得反算），NULL 时按同一口径兜底：
    #   缺口数量 = max(0, 需求 − 可用)（≡ 净采购 planned_purchase，_net_need 共用）；
    #   最迟下单日 = 子件需求日期 − 采购提前期（ceil(数量/日产量)，缺失回退 production_cycle），
    #     无缺口行留空（无可采购事项，避免把「需求日−标准周期」的过去日期当行动提示）。
    from datetime import datetime as _dt, timedelta as _td
    for st in rows:
        _sq = st.get('shortage_qty')
        st['shortage_qty'] = float(_sq) if _sq is not None else float(st.get('planned_purchase') or 0)
        _sp = st.get('suggested_purchase_qty')
        st['suggested_purchase_qty'] = float(_sp) if _sp is not None else float(st.get('suggested_purchase') or 0)
        if not st.get('latest_order_date'):
            _buy = float(st.get('planned_purchase') or 0)
            if _buy <= 0:
                # 无缺口行不展示最迟下单日（与落库口径一致，避免过去日期误读）
                st['latest_order_date'] = ''
            else:
                _cdd = st.get('component_demand_date')
                _ld = _lead(st.get('component_code', ''), _buy)
                if _cdd and _ld:
                    try:
                        st['latest_order_date'] = (_dt.strptime(str(_cdd)[:10], '%Y-%m-%d')
                                                   - _td(days=int(_ld))).strftime('%Y-%m-%d')
                    except (ValueError, TypeError):
                        st['latest_order_date'] = _cdd
                else:
                    st['latest_order_date'] = _cdd or ''
        # 在途匹配：旧数据无落库值时给空串，前端按「-」展示（不阻塞其余列）
        if st.get('intransit_match') is None:
            st['intransit_match'] = ''

    # 标记缺料行是否已生成采购入库单（用于前端"生成入库"按钮置灰 / 被删后恢复可点）
    # 匹配键：子件编码 + 子件需求日期 + note='缺料明细生成'（由缺料明细生成的采购入库单）。
    # 每次打开弹窗都实时重算：PO 存在→置灰；PO 被删除→下一轮查询即恢复可点。
    gen_keys = set()
    gen_rows = query_db(
        "SELECT material_code, plan_arrival_date FROM purchase_orders WHERE note=%s",
        ('缺料明细生成',)
    )
    for gr in gen_rows:
        mc = (gr.get('material_code') or '').strip()
        pad = gr.get('plan_arrival_date')
        if pad is not None:
            pad = pad.isoformat()[:10] if hasattr(pad, 'isoformat') else str(pad)[:10]
        gen_keys.add((mc, pad))
    for st in rows:
        cc = (st.get('component_code') or '').strip()
        cdd = st.get('component_demand_date')
        if cdd is not None:
            cdd = str(cdd)[:10]
        st['po_generated'] = (cc, cdd) in gen_keys

    return rows


@bp.route('/api/peacock/shortages/all')
def peacock_shortages_all():
    """获取所有缺料明细（支持动态库存类型）

    【P2-5·2026-10-14】可选分页：请求带 page / per_page 时按页切片返回
    （响应体附 total / page / per_page）；两个参数都不传则**全量返回**
    （旧契约不变——前端"导出"与"一键生成采购入库单"依赖全量数据）。
    per_page 上限钳制 1000，防超大页拖库；排序已补 ms.id 兜底成全序，翻页稳定。
    """
    try:
        stock_types = request.args.getlist('stock_types') or ['unrestricted']
        # 「是否包含在途」由「可用库存（多选）」中的在途选项统一控制：
        # stock_types 含 'transit' 即视作包含在途（不再有独立的 include_transit 开关）。
        include_transit = 'transit' in stock_types
        rows = _get_shortage_rows(stock_types, include_transit)
        total = len(rows)
        page_s = request.args.get('page')
        per_page_s = request.args.get('per_page')
        if page_s is None and per_page_s is None:
            # 不传分页参数：全量返回（保前端既有契约）
            return jsonify({'success': True, 'data': serialize_rows(rows),
                            'total': total, 'page': 1, 'per_page': total})
        try:
            page = int(page_s) if page_s else 1
        except (TypeError, ValueError):
            page = 1
        try:
            per_page = int(per_page_s) if per_page_s else 100
        except (TypeError, ValueError):
            per_page = 100
        if page < 1:
            page = 1
        if per_page < 1:
            per_page = 100
        per_page = min(per_page, 1000)
        offset = (page - 1) * per_page
        page_rows = rows[offset:offset + per_page]
        return jsonify({'success': True, 'data': serialize_rows(page_rows),
                        'total': total, 'page': page, 'per_page': per_page})
    except Exception as e:
        return jsonify(err_payload(e)), 500


@bp.route('/api/peacock/schedule/<sid>/plan_qty', methods=['PUT'])
def peacock_update_plan_qty(sid):
    """更新生产订单数量，并同步刷新缺料明细与齐备状态

    【P1-3 修复·2026-10-14】plan_qty 与 batch_qty **同值写入**：
    缺料爆炸基准是 batch_qty（_rebuild_all_shortages:1052"展开数量用 batch_qty，
    而非 plan_qty"），旧实现只写 plan_qty → 用户改数量对缺料明细毫无影响
    （接口 docstring 的"同步刷新缺料明细"承诺落空）。
    本改动同时恢复系统级不变量 plan_qty ≡ batch_qty（见 :2030/:2074/:3084 注释：
    "一旦两者不一致，缺料需求数量即失真"）。改数量 → 爆炸量变 → 缺料与齐备状态随之刷新。
    """
    data = request.get_json() or {}
    qty = data.get('plan_qty')
    if qty is None:
        return jsonify({'success': False, 'message': '缺少 plan_qty'}), 400
    # 【P1-3 修复】入参转换必须在事务**之内**：原实现 `float(qty)` 在 try 之外，
    # 传 `"abc"` 时抛 ValueError → 未捕获 → 500 HTML；且数量 UPDATE 与后续缺料重建
    # 分属两次独立提交，重建失败会留下"数量已改、缺料未更新"的半截状态。
    try:
        with transaction():
            qty_f = float(qty)
            # 【P2-28】软删行不可改（原实现会改到已合并折叠的行）
            execute_db("UPDATE peacock_schedule SET plan_qty=%s, batch_qty=%s WHERE id=%s AND is_deleted=0",
                       (qty_f, qty_f, sid))
            stock_types = data.get('stock_types') or ['unrestricted']
            _rebuild_shortages_only(stock_types)
    except ValueError:
        return jsonify({'success': False, 'message': 'plan_qty 必须是数字'}), 400
    except Exception as e:
        return jsonify(err_payload(e)), 500
    return jsonify({'success': True, 'message': '更新成功'})


@bp.route('/api/peacock/schedule/<sid>/override', methods=['PUT'])
def peacock_override_schedule(sid):
    """（生产排单页签专用）手动覆盖排产：更新计划排产数量与计划排产日期，并刷新缺料明细。
    不影响其它页签逻辑。"""
    data = request.get_json() or {}
    sets, values = [], []
    if 'plan_qty' in data and data['plan_qty'] is not None:
        # 【P1-3 修复·2026-10-14】与 /plan_qty 接口同口径：plan_qty 必须与 batch_qty
        # 同值写入，否则破坏系统级不变量 plan_qty ≡ batch_qty（缺料爆炸基准是 batch_qty），
        # 手动改数量后缺料明细不跟着变（接口"刷新缺料明细"承诺落空）。
        sets.append('plan_qty=%s'); values.append(float(data['plan_qty']))
        sets.append('batch_qty=%s'); values.append(float(data['plan_qty']))
    if 'batch_date_start' in data:
        sets.append('batch_date_start=%s'); values.append(data['batch_date_start'] or None)
    if 'batch_date_end' in data:
        sets.append('batch_date_end=%s'); values.append(data['batch_date_end'] or None)
    if not sets:
        return jsonify({'success': False, 'message': '无可更新字段'}), 400
    values.append(sid)
    # 【P1-3 修复】本接口是"改数量/改日期 → 重建缺料 → 归位 → 回写手动日期 → 对齐 delayed"
    # 的多步写序列：原实现每步独立提交，任一步失败都会留下半截状态
    # （典型：缺料表已 DELETE、重建中途报错 → 缺料永久为空且不可回滚）。
    # 现整体包进一个事务，任一异常整体回滚。
    try:
        with transaction():
            # 【P2-28】软删行不可改
            execute_db(f"UPDATE peacock_schedule SET {', '.join(sets)} WHERE id=%s AND is_deleted=0",
                       values)
            _rebuild_shortages_only(data.get('stock_types') or ['unrestricted'])
            # 修复④：手动覆盖后，缺料侧套用「计划需求日期」归位（与 recalc-shortages 一致），
            # 使手动排产出的缺料明细也遵循该开关约束（归位当日 + 标 delayed）。
            try:
                _enforce_plan_req_date(data.get('stock_types') or ['unrestricted'])
            except Exception as e:
                # 归位失败不阻断主流程（与既有行为一致），但必须留痕
                logging.getLogger(__name__).error('override 归位失败（已忽略）: %s', e)
            # 手动覆盖的排产日期需回写：重建缺料内部的日期回填会覆盖 batch_date_start/end，
            # 这里以用户手动值为准，确保「可手动覆盖排产」持久生效。
            reap = []
            rvals = []
            if 'batch_date_start' in data:
                reap.append('batch_date_start=%s'); rvals.append(data['batch_date_start'] or None)
            if 'batch_date_end' in data:
                reap.append('batch_date_end=%s'); rvals.append(data['batch_date_end'] or None)
            if reap:
                rvals.append(sid)
                execute_db(f"UPDATE peacock_schedule SET {', '.join(reap)} WHERE id=%s AND is_deleted=0",
                           rvals)
            # 修复⑦：排程 delayed 与手动日期对齐（拖到未来→清0，拖到过去→标1）
            if 'batch_date_start' in data and data['batch_date_start']:
                # 问题6 修复：业务当日固定 +08:00，不再依赖 MySQL 会话时区（CURDATE()），
                # 否则 OS/MySQL 时区非 +08:00 时与「计划需求日期」归位口径不一致、会差一天。
                today8 = _biz_today().strftime('%Y%m%d')
                delayed = 1 if str(data['batch_date_start']) < today8 else 0
                execute_db("UPDATE peacock_schedule SET `delayed`=%s WHERE id=%s", (delayed, sid))
    except Exception as e:
        return jsonify(err_payload(e)), 500
    return jsonify({'success': True, 'message': '覆盖成功'})



@bp.route('/api/peacock/schedule/<sid>/mark-generated', methods=['PUT'])
def peacock_mark_generated(sid):
    """标记排程已生成工单"""
    # 【P2-28】软删行不可标记
    execute_db("UPDATE peacock_schedule SET wo_generated=1 WHERE id=%s AND is_deleted=0", (sid,))
    return jsonify({'success': True, 'message': '已标记'})


@bp.route('/api/peacock/logs')
def peacock_logs():
    """计算日志"""
    # 【P2-21】同上：非法 limit 回落默认，上界 500。
    limit = _int_arg('limit', 50, lo=1, hi=500)
    rows = query_db(
        "SELECT * FROM peacock_calculation_logs ORDER BY created_at DESC LIMIT %s",
        (limit,)
    )
    return jsonify({'success': True, 'data': serialize_rows(rows)})


@bp.route('/api/peacock/statistics')
def peacock_statistics():
    """排程统计"""
    try:
        total = query_db("SELECT COUNT(*) as cnt FROM peacock_schedule WHERE is_deleted=0", one=True)['cnt']
        ready_a = query_db("SELECT COUNT(*) as cnt FROM peacock_schedule WHERE is_deleted=0 AND ready_status='A'", one=True)['cnt']
        ready_b = query_db("SELECT COUNT(*) as cnt FROM peacock_schedule WHERE is_deleted=0 AND ready_status='B'", one=True)['cnt']
        ready_c = query_db("SELECT COUNT(*) as cnt FROM peacock_schedule WHERE is_deleted=0 AND ready_status='C'", one=True)['cnt']
        # 缺料明细行数：与「缺料明细」弹窗口径完全一致 —— 实时按当前库存类型/在途开关重算缺料，
        # 再过滤净需求 required_qty>0（避免统计数的是上次计算快照、而弹窗显示实时重算结果导致的 20≠16 不一致）
        stock_types = request.args.getlist('stock_types') or ['unrestricted']
        # 是否包含在途由「可用库存（多选）」的在途选项控制
        include_transit = 'transit' in stock_types
        rows = _get_shortage_rows(stock_types, include_transit)
        # 缺料明细行数：与弹窗口径一致 —— 仅统计"建议采购"不为0的行（建议采购=0 即库存充足、无需采购）
        shortages = sum(1 for r in rows if float(r.get('suggested_purchase') or 0) > 0)

        return jsonify({
            'success': True,
            'total': total,
            'ready_a': ready_a,
            'ready_b': ready_b,
            'ready_c': ready_c,
            'shortage_count': shortages,
        })
    except Exception as e:
        return jsonify(err_payload(e)), 500
