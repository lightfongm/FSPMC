# -*- coding: utf-8 -*-
"""数据库连接与查询工具（含连接池 + 禁用SSL，避免每次查询都重建连接）"""

import logging
import queue
import threading
from contextlib import contextmanager

import pymysql
from pymysql.cursors import DictCursor

from .config import DB_CONFIG

# 锁等待超时（秒）。一键计算被包进单个事务后，最长持锁约 2 分钟（用户确认上界）；
# 而 MySQL 默认 innodb_lock_wait_timeout=50s —— 计算期间其它会话（如刷新排程列表）
# 等待这些行锁会在 50s 就报 1205(Lock wait timeout)，属误报。故把每个连接
# 的会话级锁等待放宽到 180s，覆盖 2 分钟计算 + 余量。
LOCK_WAIT_TIMEOUT = 180

# 事务上下文：begin 后，所有 query_db/execute_db/execute_many_db 共用同一连接，
# 直到 commit/rollback。用于在「一键计算/排产」中保证「删旧→建新」的原子性（修复 P1-4）。
_local = threading.local()

# ── 连接池 ───────────────────────────────────────────────────────────────
# 原实现每次 query_db/execute_db 都新建连接并关闭，无连接池。
# pymysql 2.x 在无显式 SSL 配置时会进入 PREFERRED 模式，每次 connect() 都创建
# SSL 上下文并枚举系统证书库（Windows 上约 0.2s/次），导致重算场景 76 次 DB 调用
# 累计消耗 16s+。连接池化 + 禁用 SSL 后，单次计算只建 1 个连接，耗时降至毫秒级。

_POOL_SIZE = 8
_pool = queue.Queue(maxsize=_POOL_SIZE)
# 注：`queue.Queue` 本身线程安全，无需额外锁（原 `_pool_lock` 定义后从未使用，已删除）。


def _new_conn():
    """新建一个物理连接；本地 MySQL 无需 SSL，显式禁用以跳过证书枚举。"""
    cfg = dict(DB_CONFIG)
    cfg['ssl_disabled'] = True
    conn = pymysql.connect(cursorclass=DictCursor, **cfg)
    try:
        # 会话级锁等待放宽（见 LOCK_WAIT_TIMEOUT 说明）
        with conn.cursor() as cur:
            cur.execute("SET SESSION innodb_lock_wait_timeout=%s", (LOCK_WAIT_TIMEOUT,))
            # 业务时区固定 +08:00（中国），使 SQL 端 CURDATE()/NOW() 与业务日期一致，
            # 避免服务器 OS 时区非 +08:00 时「当日」差一天（问题6 修复）。
            # 用固定偏移 '+08:00'（非命名时区），无需 MySQL 加载时区表，任何环境都安全。
            cur.execute("SET SESSION time_zone='+08:00'")
        conn.commit()
    except Exception:
        pass
    return conn


def _really_close(conn):
    """真正关闭物理连接（**绕过**被重绑定的 `conn.close`）。

    🔴【P1-2 修复 · 2026-10-14】`get_db()` 把 `conn.close` 重绑成
    `lambda: _release(conn)`，因此在本模块内部**任何**"想真正关闭连接"的地方
    都不能写 `conn.close()` —— 那会调回 `_release()`：
      · 在 `_release()` 内 → 池满时 `close() → _release() → 池仍满 → close() …`
        **无限自引用递归**，连接既没入池也没关闭（原实现 `_orig_close` 存了却从未使用）；
      · 在 `_acquire()` 内 → 把 ping 失败的**坏连接**重新"归还"进池，下次取出即报错。
    两者都会使连接泄漏并在 MySQL 侧累积至 `Too many connections`
    （池容量仅 8，而前端一次加载并发 10+ 个 API 调用，极易触顶）。
    """
    orig = getattr(conn, '_orig_close', None)
    try:
        if orig is not None:
            orig()
        else:
            # 尚未被重绑定（理论上不会发生）：退回类方法，避免递归
            conn.__class__.close(conn)
    except Exception:
        pass


def _acquire():
    """从池中获取一个保活的连接；池空则新建。"""
    try:
        conn = _pool.get_nowait()
    except queue.Empty:
        return _new_conn()
    try:
        conn.ping(reconnect=True)
        return conn
    except Exception:
        # 【P1-2】坏连接必须真正丢弃，不能用 conn.close()（那是"归还池"）
        _really_close(conn)
        return _new_conn()


def _release(conn):
    """把连接归还池（而非真正关闭）；池满则**真正**关闭。"""
    try:
        if _pool.full():
            _really_close(conn)
        else:
            _pool.put_nowait(conn)
    except Exception:
        _really_close(conn)


def get_db():
    """获取数据库连接：若已开启事务（_local.conn），直接返回事务连接；
    否则从池取一个，并把 conn.close() 重绑为归还池，调用方无需改动。"""
    txn = getattr(_local, 'conn', None)
    if txn is not None:
        return txn
    conn = _acquire()
    orig_close = getattr(conn, '_orig_close', None)
    if orig_close is None:
        orig_close = conn.close
        conn._orig_close = orig_close
    conn.close = lambda: _release(conn)
    return conn


def query_db(sql, args=None, one=False):
    """查询数据库，返回字典列表。事务内复用事务连接且不自行提交。"""
    txn = getattr(_local, 'conn', None)
    owned = txn is None
    conn = txn if not owned else get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
            rows = cur.fetchall()
        if owned:
            conn.commit()
        return rows[0] if one and rows else rows
    except Exception as e:
        if owned:
            conn.rollback()
        raise e
    finally:
        if owned:
            conn.close()


def execute_db(sql, args=None):
    """执行写操作，返回受影响行数。事务内复用事务连接且不自行提交。"""
    txn = getattr(_local, 'conn', None)
    owned = txn is None
    conn = txn if not owned else get_db()
    try:
        with conn.cursor() as cur:
            cur.execute(sql, args)
        if owned:
            conn.commit()
        return cur.rowcount
    except Exception as e:
        if owned:
            conn.rollback()
        raise e
    finally:
        if owned:
            conn.close()


def execute_many_db(sql, args_list):
    """批量执行。事务内复用事务连接且不自行提交。"""
    txn = getattr(_local, 'conn', None)
    owned = txn is None
    conn = txn if not owned else get_db()
    try:
        with conn.cursor() as cur:
            cur.executemany(sql, args_list)
        if owned:
            conn.commit()
        return cur.rowcount
    except Exception as e:
        if owned:
            conn.rollback()
        raise e
    finally:
        if owned:
            conn.close()


def begin_transaction():
    """开启连接级事务。后续 query_db/execute_db/execute_many_db 共用该连接。

    **支持可重入（嵌套）**：已开启时只把深度 +1，不再申请新连接。
    原实现遇到嵌套直接抛 RuntimeError，导致 peacock_calculate 无法整体包裹事务
    —— 因为它内部会调用 calculate_readiness()，而后者自己开了事务。

    嵌套语义（关键，改动此处务必保持）：
      - 内层 commit 只递减深度，**不真正提交**；
      - 内层 rollback 递减深度并置 rollback_only 标记，该标记**必须传播到最外层**，
        由最外层执行真正的 rollback。否则会出现"内层已失败、外层仍提交"的脏数据。
    """
    if getattr(_local, 'conn', None) is not None:
        _local.depth = getattr(_local, 'depth', 1) + 1
        return
    _local.conn = _acquire()
    _local.depth = 1
    _local.rollback_only = False


def commit_transaction():
    """提交（仅最外层真正提交）；若任一层请求过回滚，则整体回滚而非提交。"""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        return
    depth = getattr(_local, 'depth', 1) - 1
    if depth > 0:
        _local.depth = depth
        return
    ok = not getattr(_local, 'rollback_only', False)
    if not ok:
        logging.getLogger(__name__).warning('事务内层已请求回滚 → 最外层执行整体回滚（不提交）')
    _finish_transaction(conn, commit=ok)


def rollback_transaction():
    """回滚（仅最外层真正回滚）；内层调用只递减深度并置 rollback_only 标记。"""
    conn = getattr(_local, 'conn', None)
    if conn is None:
        return
    depth = getattr(_local, 'depth', 1) - 1
    if depth > 0:
        _local.depth = depth
        _local.rollback_only = True
        return
    _finish_transaction(conn, commit=False)


def _finish_transaction(conn, commit):
    """结束最外层事务：提交或回滚，释放连接并清空 thread-local 状态。"""
    try:
        if commit:
            conn.commit()
        else:
            conn.rollback()
    finally:
        try:
            conn.close()
        except Exception:
            pass
        _local.conn = None
        _local.depth = 0
        _local.rollback_only = False


def in_transaction():
    """当前线程是否已在事务中。

    用途：DDL 判断。MySQL 的 DDL 会**隐式提交**当前事务且不可回滚，
    所以任何 DDL 都必须先 `if in_transaction(): return` 跳过，改由事务外的
    统一前置步骤执行（见 schedule._ensure_calc_columns）。
    """
    return getattr(_local, 'conn', None) is not None


@contextmanager
def transaction():
    """事务上下文管理器：with transaction(): ... 正常退出自动提交，异常自动回滚。
    支持嵌套（见 begin_transaction 的嵌套语义说明）。"""
    begin_transaction()
    try:
        yield
        commit_transaction()
    except Exception:
        rollback_transaction()
        raise


def _mysql_errno(e):
    """取 pymysql 异常的 MySQL 错误码（取不到返回 None）。"""
    args = getattr(e, 'args', None)
    if args and isinstance(args[0], int):
        return args[0]
    return getattr(e, 'errno', None)


def safe_fields(data, table):
    """只保留「表中真实存在的列」，用于防御标识符被拼接进 SQL。

    若干导入接口用 `', '.join(data.keys())` 拼 INSERT 的列名 —— **列名无法参数化**，
    只能靠白名单。当前这些键由代码按固定表头构造、并不可被外部控制，
    但一旦导入改为接收 JSON 或 data 混入任意键，就会变成 SQL 注入。
    故统一按表的真实列过滤，行为与现状一致，同时堵住该隐患。

    :param data: 待插入的 dict（键为列名）
    :param table: 目标表名（代码内字面量，非用户输入）
    """
    try:
        cols = {r['Field'] for r in query_db("SHOW COLUMNS FROM %s" % table)}
    except Exception as e:
        logging.getLogger(__name__).warning(
            'safe_fields 无法读取 %s 的列信息，退回原键列表：%s', table, e)
        return list(data.keys())
    dropped = [k for k in data.keys() if k not in cols]
    if dropped:
        logging.getLogger(__name__).warning(
            '导入 %s 时忽略非表字段：%s', table, dropped)
    return [k for k in data.keys() if k in cols]


def execute_ddl(sql, ignore_errnos=(1060, 1061)):
    """执行 DDL（建表/加列）：幂等错误忽略，其它错误**记日志后抛出**。

    历史教训：原先迁移一律 `except Exception: pass` 静默吞掉所有异常，曾掩盖
    `delayed` 列因 MySQL 保留字未加反引号导致的 1064 语法错误，
    表现为"列没加上、但没有任何报错"，排查成本极高。

    :param ignore_errnos: 视为幂等成功的错误码
        1060 = Duplicate column name（列已存在）
        1061 = Duplicate key name（键已存在）
    """
    try:
        execute_db(sql)
        return True
    except Exception as e:
        if _mysql_errno(e) in ignore_errnos:
            return False
        logging.getLogger(__name__).error(
            'DDL 执行失败（errno=%s）：%s | SQL: %s', _mysql_errno(e), e, sql)
        raise
