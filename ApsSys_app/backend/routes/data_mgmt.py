# -*- coding: utf-8 -*-
"""数据管理 — 本地 MySQL 备份 / 快照 / 日志 / 恢复

备份说明：
- 每次「立即备份」会在服务器本地目录 ApsSys_app/backups/ 生成两份文件：
    * backup_YYYYMMDD_HHMMSS.sql  —— 标准 MySQL 转储（DROP/CREATE/INSERT），
      可用 `mysql -u<user> -p <db> < backup_xxx.sql` 直接还原，是真正的数据库级备份。
    * backup_YYYYMMDD_HHMMSS.json —— 结构化快照，供系统内「一键恢复」使用。
- 同时在 data_snapshots 表记录备份元信息（路径 / 表数量 / 文件大小）。
"""

import os
import re
import json
import logging
import threading
import time
from datetime import datetime, date

from flask import Blueprint, request, jsonify, send_file

from ..database import query_db, get_db
from ..config import DB_CONFIG, BASE_DIR
from ..utils import biz_now
from ..errors import err_payload

bp = Blueprint('data_mgmt', __name__)

# 本地备份目录：ApsSys_app/backups/
BACKUP_DIR = os.path.join(BASE_DIR, 'backups')
os.makedirs(BACKUP_DIR, exist_ok=True)

_NAME_RE = re.compile(r'^backup_\d{8}_\d{6}$')

# 【P0-3 方案 C】恢复时**不回滚**的系统表：它们记录"备份/恢复/定时"自身状态，
# 若被备份覆盖，恢复后回滚点记录会被一并冲掉 → 无法再撤销。
_RESTORE_SKIP_TABLES = {'data_snapshots', 'auto_backup_config', 'data_log'}


# ---------------------------------------------------------------------------
# 工具：把 Python 值转成 MySQL 字面值
# ---------------------------------------------------------------------------
def _mysql_quote(v):
    if v is None:
        return 'NULL'
    if isinstance(v, bool):
        return '1' if v else '0'
    if isinstance(v, (int, float)):
        return str(v)
    if isinstance(v, (datetime, date)):
        return "'" + str(v) + "'"
    if isinstance(v, bytes):
        try:
            s = v.decode('utf-8')
        except Exception:
            s = v.decode('latin-1', 'replace')
        return "'" + s.replace('\\', '\\\\').replace("'", "\\'") + "'"
    s = str(v).replace('\\', '\\\\').replace("'", "\\'").replace('\n', '\\n').replace('\r', '\\r')
    return "'" + s + "'"


# ---------------------------------------------------------------------------
# 构建备份内容（同一连接内完成 SQL 转储 + 结构化快照）
# ---------------------------------------------------------------------------
def _build_backup(conn):
    cur = conn.cursor()
    cur.execute("SHOW TABLES")
    tables = [list(r.values())[0] for r in cur.fetchall()]

    lines = []
    lines.append("-- PMC MySQL backup")
    lines.append("-- Generated: " + biz_now().strftime('%Y-%m-%d %H:%M:%S'))
    lines.append("-- Host: %s  Database: %s" % (DB_CONFIG.get('host'), DB_CONFIG.get('database')))
    lines.append("SET NAMES utf8mb4;")
    lines.append("SET FOREIGN_KEY_CHECKS=0;")
    lines.append("")

    dcur = conn.cursor()
    snapshot = {}
    for t in tables:
        cur.execute("SHOW CREATE TABLE `%s`" % t)
        create_stmt = list(cur.fetchall()[0].values())[1]
        lines.append("-- ===== table: %s =====" % t)
        lines.append("DROP TABLE IF EXISTS `%s`;" % t)
        lines.append(str(create_stmt).rstrip(';') + ";")
        dcur.execute("SELECT * FROM `%s`" % t)
        rows = dcur.fetchall()
        snapshot[t] = [dict(r) for r in rows]
        if rows:
            cols = list(rows[0].keys())
            col_list = ", ".join("`%s`" % c for c in cols)
            for r in rows:
                vals = ", ".join(_mysql_quote(r[c]) for c in cols)
                lines.append("INSERT INTO `%s` (%s) VALUES (%s);" % (t, col_list, vals))
        lines.append("")
    lines.append("SET FOREIGN_KEY_CHECKS=1;")

    cur.close()
    dcur.close()
    return "\n".join(lines), snapshot, tables


# ---------------------------------------------------------------------------
# 备份接口
# ---------------------------------------------------------------------------
def _do_backup(operator='system'):
    """执行一次完整备份（手动与定时共用）。返回 (success:bool, name_or_error:str)。"""
    ts = biz_now().strftime('%Y%m%d_%H%M%S')
    name = "backup_%s" % ts
    conn = None
    try:
        conn = get_db()
        sql_text, snapshot, tables = _build_backup(conn)
    except Exception as e:
        return False, '生成备份失败: %s' % e
    finally:
        # 【P2-27 修复】原实现仅在成功路径 close()，异常时连接不归还池 → 泄漏累积。
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass

    sql_path = os.path.join(BACKUP_DIR, name + '.sql')
    with open(sql_path, 'w', encoding='utf-8') as f:
        f.write(sql_text)

    json_path = os.path.join(BACKUP_DIR, name + '.json')
    try:
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(snapshot, f, ensure_ascii=False, default=str)
        json_size = os.path.getsize(json_path)
    except Exception:
        json_size = 0

    meta = {
        'sql_path': sql_path,
        'json_path': json_path,
        'table_count': len(tables),
        'sql_size': os.path.getsize(sql_path),
        'json_size': json_size,
        'mode': 'mysql_sql_file',
    }
    execute_db_safe(
        "INSERT INTO data_snapshots (snapshot_name, snapshot_data, created_by) VALUES (%s, %s, %s)",
        (name, json.dumps(meta, ensure_ascii=False), 'system')
    )
    write_operation_log('备份', '全库', len(tables), operator, '生成本地文件：%s' % name)
    return True, name


@bp.route('/api/data/backup', methods=['POST'])
def data_backup():
    operator = (request.get_json(silent=True) or {}).get('operator') or 'system'
    ok, name = _do_backup(operator)
    if not ok:
        return jsonify({'success': False, 'error': name}), 500
    return jsonify({
        'success': True,
        'message': '数据备份成功（本地文件）：%s' % name,
        'snapshot_name': name,
    })


def execute_db_safe(sql, args=None):
    """写入备份元信息；data_snapshots 不存在时跳过，不影响文件备份。

    【P2-27 修复】原实现 `except Exception: pass` 完全静默：备份元信息写失败时
    用户仍看到 `success:True`，误以为备份已登记。现改为记日志（不阻断主流程）。
    """
    from ..database import execute_db
    try:
        execute_db(sql, args)
    except Exception as e:
        logging.warning('[data_mgmt] 备份元信息写入失败（文件备份仍成功）: %s', e)


def write_operation_log(operation, table_name, record_count=0, operator='system', note=''):
    """写入操作日志到 data_log 表（备份/删除/恢复均记录）。

    【P2-27 修复】同上：失败必须留痕，不能静默吞掉。
    """
    from ..database import execute_db
    try:
        execute_db(
            "INSERT INTO data_log (operation, table_name, record_count, operator, note, created_at) "
            "VALUES (%s, %s, %s, %s, %s, %s)",
            (operation, table_name, record_count, operator, note, biz_now())
        )
    except Exception as e:
        logging.warning('[data_mgmt] 操作日志写入失败（%s/%s）: %s', operation, table_name, e)


# ---------------------------------------------------------------------------
# 快照列表（含备份文件信息）
# ---------------------------------------------------------------------------
@bp.route('/api/data/snapshots')
def data_snapshots():
    rows = query_db(
        "SELECT id, snapshot_name, created_by, created_at, snapshot_data "
        "FROM data_snapshots ORDER BY created_at DESC"
    )
    out = []
    for r in rows:
        meta = {}
        try:
            meta = json.loads(r.get('snapshot_data') or '{}')
        except Exception:
            pass
        out.append({
            'id': r['id'],
            'snapshot_name': r['snapshot_name'],
            'created_by': r['created_by'],
            'created_at': r['created_at'],
            'table_count': meta.get('table_count'),
            'sql_size': meta.get('sql_size'),
            'has_file': bool(meta.get('sql_path') and os.path.exists(meta.get('sql_path') or '')),
        })
    return jsonify({'success': True, 'data': out})


# ---------------------------------------------------------------------------
# 下载备份文件（.sql）
# ---------------------------------------------------------------------------
@bp.route('/api/data/backup/download/<name>')
def download_backup(name):
    if not _NAME_RE.match(name or ''):
        return jsonify({'success': False, 'error': '非法文件名'}), 400
    sql_path = os.path.join(BACKUP_DIR, name + '.sql')
    if not os.path.exists(sql_path):
        return jsonify({'success': False, 'error': '备份文件不存在'}), 404
    return send_file(sql_path, as_attachment=True, download_name=name + '.sql')


# ---------------------------------------------------------------------------
# 应用内一键恢复（读 .json 边车，按表 DELETE + INSERT）
# ---------------------------------------------------------------------------
@bp.route('/api/data/restore', methods=['POST'])
def restore_backup():
    """【P0-3 加固 · 2026-10-14】恢复 = 全库擦除重写，故加三道闸：

    1. **确认令牌**：请求体必须带 `confirm: true`（前端弹窗确认后才会带）。
       挡住脚本/误触直接 POST 本接口（原实现无任何鉴权，一次请求即毁库）。
    2. **恢复前自动先备份当前库**（方案 B）：保证任何一次恢复都可回滚。
       备份失败则**拒绝恢复**（fail-closed），避免"恢复后无法再撤销"。
    3. **系统表白名单排除**（方案 C）：`data_snapshots` / `auto_backup_config` /
       `data_log` 记录的是"备份与恢复"本身的状态，若一并回滚会把刚做的
       回滚点冲掉，导致恢复后不再可撤销 —— 故始终保留当前值。

    注：本项目当前**全站无认证机制**，本加固不能替代登录鉴权；若要对外开放，
    仍需整站接入认证（见复审报告 P0-2/P0-3 中期建议）。
    """
    data = request.get_json() or {}
    name = data.get('snapshot_name') or data.get('name')
    operator = data.get('operator') or 'system'
    if not name or not _NAME_RE.match(name):
        return jsonify({'success': False, 'error': '请提供合法的快照名称'}), 400

    # 闸 1：确认令牌
    if data.get('confirm') is not True:
        return jsonify({
            'success': False,
            'error': '恢复操作需显式确认：请求体缺少 confirm:true（该操作会覆盖全部业务表数据）'
        }), 400

    json_path = os.path.join(BACKUP_DIR, name + '.json')
    if not os.path.exists(json_path):
        return jsonify({'success': False, 'error': '备份的 JSON 文件缺失，无法应用内恢复'}), 404

    try:
        with open(json_path, 'r', encoding='utf-8') as f:
            snapshot = json.load(f)
    except Exception as e:
        return jsonify({'success': False, 'error': '读取备份失败: %s' % e}), 500

    # 闸 2：恢复前自动备份当前库（保证可回滚），失败即拒绝恢复
    pre_ok, pre_name = _do_backup(operator)
    if not pre_ok:
        return jsonify({
            'success': False,
            'error': '已取消恢复：无法为当前数据创建回滚点（%s）。请先排查备份目录权限。' % pre_name
        }), 500

    conn = get_db()
    cur = conn.cursor()
    restored = 0
    skipped = []
    try:
        cur.execute("SET FOREIGN_KEY_CHECKS=0")
        for t, rows in snapshot.items():
            if not isinstance(rows, list) or not rows:
                continue
            # 闸 3：系统表不回滚
            if t in _RESTORE_SKIP_TABLES:
                skipped.append(t)
                continue
            cur.execute("DELETE FROM `%s`" % t)
            cols = list(rows[0].keys())
            placeholders = ", ".join(["%s"] * len(cols))
            col_list = ", ".join("`%s`" % c for c in cols)
            args = [tuple(r.get(c) for c in cols) for r in rows]
            cur.executemany(
                "INSERT INTO `%s` (%s) VALUES (%s)" % (t, col_list, placeholders), args
            )
            restored += 1
        conn.commit()
    except Exception as e:
        conn.rollback()
        logging.error('[data_mgmt] 恢复失败（回滚点：%s）: %s', pre_name, e)
        return jsonify({
            'success': False,
            'error': '恢复失败: %s（数据未改动，如需回滚当前状态请使用备份 %s）' % (e, pre_name)
        }), 500
    finally:
        try:
            cur.execute("SET FOREIGN_KEY_CHECKS=1")
        except Exception:
            pass
        conn.close()

    write_operation_log('恢复', '全库', restored, operator,
                        '从备份恢复：%s（回滚点：%s；跳过系统表：%s）'
                        % (name, pre_name, ','.join(skipped) or '无'))
    return jsonify({
        'success': True,
        'message': '已从 %s 恢复完成，共恢复 %d 张表。当前数据的回滚点为 %s。' % (name, restored, pre_name),
        'restored': restored,
        'rollback_point': pre_name,
        'skipped_tables': skipped,
    })


# ---------------------------------------------------------------------------
# 删除备份（删库记录 + 删本地文件）
# ---------------------------------------------------------------------------
@bp.route('/api/data/backup/delete/<name>', methods=['DELETE'])
def delete_backup(name):
    if not _NAME_RE.match(name or ''):
        return jsonify({'success': False, 'error': '非法文件名'}), 400
    operator = (request.get_json(silent=True) or {}).get('operator') or 'system'

    # 删本地文件
    removed = []
    for ext in ('.sql', '.json'):
        p = os.path.join(BACKUP_DIR, name + ext)
        if os.path.exists(p):
            try:
                os.remove(p)
                removed.append(ext)
            except Exception:
                pass

    # 删库记录
    try:
        execute_db_safe("DELETE FROM data_snapshots WHERE snapshot_name=%s", (name,))
    except Exception as e:
        return jsonify({'success': False, 'error': '删除记录失败: %s' % e}), 500

    write_operation_log('删除', 'data_snapshots', 1, operator, '删除备份：%s（本地文件：%s）' % (name, ','.join(removed) or '无'))
    return jsonify({
        'success': True,
        'message': '已删除备份 %s（本地文件：%s）' % (name, ','.join(removed) or '无'),
        'removed_files': removed,
    })


# ---------------------------------------------------------------------------
# 操作日志
# ---------------------------------------------------------------------------
@bp.route('/api/data/log')
def data_log():
    limit = int(request.args.get('limit', 50))
    rows = query_db("SELECT * FROM data_log ORDER BY created_at DESC LIMIT %s", (limit,))
    from ..utils import serialize_rows
    return jsonify({'success': True, 'data': serialize_rows(rows)})


# ---------------------------------------------------------------------------
# 自动定时备份
# ---------------------------------------------------------------------------
_AUTO_SCHEDULER_STARTED = False
_AUTO_SCHEDULER_LOCK = threading.Lock()
_WEEKDAYS_CN = ['周一', '周二', '周三', '周四', '周五', '周六', '周日']


def _ensure_auto_backup_table():
    from ..database import execute_db
    try:
        execute_db(
            """CREATE TABLE IF NOT EXISTS auto_backup_config (
                id INT AUTO_INCREMENT PRIMARY KEY,
                frequency VARCHAR(16) NOT NULL DEFAULT 'daily',
                hour INT NOT NULL DEFAULT 0,
                minute INT NOT NULL DEFAULT 0,
                weekday INT DEFAULT 0,
                monthday INT DEFAULT 1,
                enabled TINYINT NOT NULL DEFAULT 0,
                last_run_at DATETIME NULL,
                created_at DATETIME DEFAULT CURRENT_TIMESTAMP
            )"""
        )
    except Exception as e:
        import sys
        print('[auto_backup] 创建 auto_backup_config 表失败:', e, file=sys.stderr)


def _load_auto_backup_config():
    _ensure_auto_backup_table()
    rows = query_db("SELECT * FROM auto_backup_config ORDER BY id DESC LIMIT 1")
    return rows[0] if rows else None


def _check_and_run_auto_backup():
    cfg = _load_auto_backup_config()
    if not cfg or not cfg.get('enabled'):
        return
    now = biz_now()
    try:
        hh = int(cfg.get('hour') or 0)
        mm = int(cfg.get('minute') or 0)
    except Exception:
        return
    if now.hour != hh or now.minute != mm:
        return
    freq = cfg.get('frequency')
    if freq == 'weekly':
        if int(cfg.get('weekday') or 0) != now.weekday():
            return
    elif freq == 'monthly':
        if int(cfg.get('monthday') or 1) != now.day:
            return
    elif freq != 'daily':
        return
    # 防同分钟重复执行
    last = cfg.get('last_run_at')
    if isinstance(last, datetime) and last.date() == now.date() and last.hour == hh and last.minute == mm:
        return
    ok, name = _do_backup('system')
    if ok:
        execute_db_safe("UPDATE auto_backup_config SET last_run_at=%s WHERE id=%s", (now, cfg['id']))


def auto_backup_scheduler():
    while True:
        try:
            _check_and_run_auto_backup()
        except Exception:
            pass
        time.sleep(30)


def start_scheduler():
    """在 create_app() 中调用，确保只启动一个后台调度线程。"""
    global _AUTO_SCHEDULER_STARTED
    with _AUTO_SCHEDULER_LOCK:
        if _AUTO_SCHEDULER_STARTED:
            return
        _AUTO_SCHEDULER_STARTED = True
    _ensure_auto_backup_table()
    t = threading.Thread(target=auto_backup_scheduler, daemon=True)
    t.start()


@bp.route('/api/data/auto-backup', methods=['GET'])
def get_auto_backup():
    cfg = _load_auto_backup_config()
    return jsonify({'success': True, 'data': cfg})


@bp.route('/api/data/auto-backup', methods=['POST'])
def set_auto_backup():
    data = request.get_json(silent=True) or {}
    frequency = data.get('frequency') or 'daily'
    if frequency not in ('daily', 'weekly', 'monthly'):
        return jsonify({'success': False, 'error': 'frequency 必须是 daily/weekly/monthly'}), 400
    try:
        hour = int(data.get('hour', 0))
        minute = int(data.get('minute', 0))
        weekday = int(data.get('weekday', 0))
        monthday = int(data.get('monthday', 1))
        enabled = 1 if data.get('enabled') else 0
    except Exception as e:
        return jsonify({'success': False, 'error': '参数类型错误: %s' % e}), 400
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return jsonify({'success': False, 'error': '时间超出范围(时0-23, 分0-59)'}), 400
    _ensure_auto_backup_table()
    # 【P2-27 修复 · 2026-10-14】原用 `execute_db_safe`（静默吞异常）：写入失败时
    # 仍返回 `success:True` + "已保存" → 用户以为开了定时备份，实际配置从未落库，
    # 真到需要恢复时才发现一个备份都没有。这是**最危险的静默失败**。
    # 定时备份配置是"必须确认写成功"的操作，改用普通 execute_db，失败即如实报错。
    from ..database import execute_db
    existing = query_db("SELECT id FROM auto_backup_config ORDER BY id DESC LIMIT 1")
    try:
        if existing:
            execute_db(
                "UPDATE auto_backup_config SET frequency=%s,hour=%s,minute=%s,weekday=%s,monthday=%s,enabled=%s WHERE id=%s",
                (frequency, hour, minute, weekday, monthday, enabled, existing[0]['id'])
            )
            cid = existing[0]['id']
        else:
            execute_db(
                "INSERT INTO auto_backup_config (frequency,hour,minute,weekday,monthday,enabled) VALUES (%s,%s,%s,%s,%s,%s)",
                (frequency, hour, minute, weekday, monthday, enabled)
            )
            nw = query_db("SELECT id FROM auto_backup_config ORDER BY id DESC LIMIT 1")
            cid = nw[0]['id'] if nw else None
    except Exception as e:
        logging.getLogger(__name__).error('[data_mgmt] 自动备份配置保存失败: %s', e)
        return jsonify(err_payload(e, '自动备份计划保存失败：')), 500
    saved = query_db("SELECT * FROM auto_backup_config WHERE id=%s", (cid,))
    return jsonify({'success': True, 'message': '自动备份计划已保存', 'data': saved[0] if saved else None})

