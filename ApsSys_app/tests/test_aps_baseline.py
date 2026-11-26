# -*- coding: utf-8 -*-
"""APS 回归基线（P1-4）。

用意：给 `schedule.py` / `database.py` 的后续改动提供一张最小安全网。
覆盖的是**最容易被改坏、又最难从界面发现**的几条不变式：

1. 净需求公式 `_net_need`（存储层与展示层共用，改错会全盘错算）；
2. 事务可重入语义（原实现遇嵌套直接抛 RuntimeError）；
3. 内层 rollback 必须传播到最外层（否则"内层失败、外层仍提交"留下脏数据）；
4. DDL 幂等 + 迁移列确实存在（历史 bug：迁移静默失败导致列缺失）；
5. 延误标记不变式：`delayed=1` 的行，其日期必须等于当日。

只读校验为主，不写入业务表、不删除业务数据。
"""

import os
import sys
from datetime import date

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from backend.database import (query_db, _local, begin_transaction, commit_transaction,
                              rollback_transaction, in_transaction, safe_fields)
from backend.routes.schedule import _net_need, _ensure_calc_columns


def _db_ok():
    try:
        query_db("SELECT 1")
        return True
    except Exception:
        return False


DB_OK = _db_ok()
needs_db = pytest.mark.skipif(not DB_OK, reason='MySQL 不可用，跳过依赖数据库的用例')


# ── 1. 净需求公式 ────────────────────────────────────────────────
def test_net_need_basic():
    assert _net_need(100, 30) == 70        # 需求 > 库存 → 净需求 70
    assert _net_need(30, 30) == 0          # 刚好齐套 → 0（历史 bug：曾误判为全额缺料）
    assert _net_need(20, 50) == 0          # 库存充足 → 0，绝不为负
    assert _net_need(0, 0) == 0


def test_net_need_tolerance():
    """浮点容差内视为齐套，避免 1e-9 级误差被判成缺料。"""
    assert _net_need(100.0, 100.0 - 1e-9) == 0
    assert _net_need(1.0, 0.0) == 1.0


# ── 2/3. 事务可重入与回滚传播 ─────────────────────────────────────
@needs_db
def test_transaction_is_reentrant():
    """嵌套开启事务不得抛异常（原实现会抛 RuntimeError）。"""
    begin_transaction()
    try:
        assert in_transaction() is True
        begin_transaction()               # 内层：不应抛异常
        assert getattr(_local, 'depth', 1) == 2
        commit_transaction()              # 内层提交：仅递减深度
    finally:
        commit_transaction()              # 最外层：真正提交
    assert in_transaction() is False


@needs_db
def test_inner_rollback_propagates_to_outer():
    """内层回滚后，最外层的 commit 必须转为回滚 —— 否则会提交半截数据。"""
    begin_transaction()
    try:
        begin_transaction()
        rollback_transaction()             # 内层请求回滚
        assert getattr(_local, 'rollback_only', False) is True, \
            '内层 rollback 必须置 rollback_only，否则外层会误提交'
        commit_transaction()               # 外层"提交"，实际必须走回滚
    finally:
        # 收尾：确保状态被清空（若上面已结束事务，这里应为空操作）
        if in_transaction():
            rollback_transaction()
    assert in_transaction() is False
    assert getattr(_local, 'rollback_only', False) is False, '事务结束后必须重置 rollback_only'


@needs_db
def test_ddl_skipped_inside_transaction():
    """事务内禁止执行 DDL（会隐式提交）；_ensure_calc_columns 必须直接返回。"""
    begin_transaction()
    try:
        # 不应抛异常，也不应执行任何 DDL
        _ensure_calc_columns()
        assert in_transaction() is True, 'DDL 若在事务内执行会隐式提交，事务应已被破坏'
    finally:
        rollback_transaction()


# ── 4. DDL 幂等与迁移列存在性 ─────────────────────────────────────
@needs_db
def test_ensure_calc_columns_idempotent():
    """连续两次调用不得报错，且所需列/表均存在。"""
    _ensure_calc_columns()
    _ensure_calc_columns()

    sch = {r['Field'] for r in query_db("SHOW COLUMNS FROM peacock_schedule")}
    assert 'work_order_id' in sch
    assert 'source_refs' in sch

    sh = {r['Field'] for r in query_db("SHOW COLUMNS FROM peacock_material_shortage")}
    assert 'work_order_id' in sh
    assert 'parent_qty' in sh

    assert query_db("SHOW TABLES LIKE 'mrp_net_inventory'"), 'mrp_net_inventory 表应存在'


@needs_db
def test_migrated_columns_exist():
    """启动迁移补的列必须真实存在（历史 bug：静默失败导致列没加上也不报错）。"""
    for tbl in ('peacock_schedule', 'peacock_material_shortage'):
        cols = {r['Field'] for r in query_db("SHOW COLUMNS FROM %s" % tbl)}
        assert 'delayed' in cols, '%s 缺少 delayed 列' % tbl
    wo = {r['Field'] for r in query_db("SHOW COLUMNS FROM work_orders")}
    assert 'delayed' in wo, 'work_orders 缺少 delayed 列'
    assert 'material_status' in wo, 'work_orders 缺少 material_status 列'


# ── 5. 延误标记不变式 ────────────────────────────────────────────
@needs_db
def test_delayed_rows_must_be_today():
    """delayed=1 的行，其计划/需求日期必须等于当日。

    排程表是 varchar(8) 'YYYYMMDD'，缺料表是 'YYYY-MM-DD' —— 两处格式不同，
    必须分别比对（混用会触发 1406 Data too long）。
    """
    today8 = date.today().strftime('%Y%m%d')
    today10 = date.today().strftime('%Y-%m-%d')

    n1 = query_db("SELECT COUNT(*) AS c FROM peacock_schedule "
                  "WHERE `delayed`=1 AND batch_date_start <> %s", (today8,), one=True)
    n2 = query_db("SELECT COUNT(*) AS c FROM peacock_material_shortage "
                  "WHERE `delayed`=1 AND component_demand_date <> %s", (today10,), one=True)
    assert (n1 or {}).get('c', 0) == 0, '存在 delayed=1 但排产日期不等于当日的行'
    assert (n2 or {}).get('c', 0) == 0, '存在 delayed=1 但子件需求日期不等于当日的行'


# ── 6. 标识符白名单（防 SQL 注入加固）─────────────────────────────
def test_safe_fields_filters_unknown_keys():
    """非表字段必须被过滤掉，不能被拼进 INSERT 的列名。"""
    if not DB_OK:
        pytest.skip('MySQL 不可用')
    kept = safe_fields({'material_code': 'X', 'name': 'Y',
                        'evil`; DROP TABLE products; --': 1}, 'products')
    assert 'material_code' in kept
    assert 'name' in kept
    assert not any('DROP' in k for k in kept), '恶意键必须被过滤'
