# -*- coding: utf-8 -*-
"""通用工具函数"""

import uuid
from datetime import datetime, date, timezone, timedelta

# 业务时区固定偏移：中国 +08:00。
# 「当日/当前时刻」一律以此为基准，不依赖服务器 OS 时区（P1-5，2026-10-14）。
# 历史教训：排程侧原用 _biz_today()（+08:00），工单侧用 date.today()/datetime.now()
# （OS 本地），OS 时区非 +08:00 时两侧「当日」差一天 → 归位/优先级/负荷窗口口径分裂。
_BIZ_TZ = timezone(timedelta(hours=8))


def biz_today():
    """业务「当日」（date）：固定 +08:00，不依赖服务器 OS 时区。"""
    return datetime.now(_BIZ_TZ).date()


def biz_now():
    """业务「当前时刻」：+08:00 墙钟的 naive datetime（与库内 naive datetime 可比）。"""
    return datetime.now(_BIZ_TZ).replace(tzinfo=None)


def biz_midnight():
    """业务「当日 00:00:00」的 naive datetime（+08:00 墙钟）。"""
    t = biz_today()
    return datetime(t.year, t.month, t.day)


def gen_id():
    """生成唯一ID"""
    return str(uuid.uuid4())


def serialize_row(row):
    """序列化数据库行，处理日期和Decimal"""
    if row is None:
        return None
    result = {}
    for k, v in row.items():
        if isinstance(v, (datetime, date)):
            result[k] = v.isoformat()
        elif isinstance(v, bytes):
            result[k] = v.decode('utf-8', errors='replace') if v else None
        elif hasattr(v, '__float__') and not isinstance(v, float):
            result[k] = float(v)
        else:
            result[k] = v
    return result


def serialize_rows(rows):
    """序列化多行"""
    return [serialize_row(r) for r in rows]


# ── 上传文件安全校验（P2-22，2026-10-14）─────────────────────────────────
# 背景：各导入接口原实现直接 `f.read()` 后交给 openpyxl 解析，**不校验文件类型、
# 不限制大小、不设解析上限**。一个恶意/损坏的 xlsx（本质是 zip，"zip bomb"
# 可用几 KB 压出几 GB）足以吃满内存与 CPU；本服务为单进程，一旦被拖住，
# 所有请求一起阻塞。故统一在此做三道闸：扩展名白名单 → 内容长度上限 → 只读解析。
UPLOAD_ALLOWED_EXT = ('.xlsx', '.xls')
UPLOAD_MAX_BYTES = 20 * 1024 * 1024   # 20 MB


def read_upload_excel(file_storage, max_bytes=UPLOAD_MAX_BYTES,
                      allowed_ext=UPLOAD_ALLOWED_EXT, read_only=True):
    """安全读取上传的 Excel，返回 (workbook, error_message)。

    - `workbook` 为 openpyxl 工作簿（read_only 模式，内存占用显著更低）；
    - 出错时 `workbook` 为 None，`error_message` 为可直接回前端的中文说明。

    三道闸：
      ① 扩展名白名单（.xlsx/.xls）—— 挡掉 .exe/.zip/.php 等；
      ② 内容长度上限（默认 20MB）—— 先 `seek/tell` 取长度，超限**直接拒绝**，
         不把内容读进内存（注意：不能靠 len(f.read()) 判断，那已把文件读完了）；
      ③ `read_only=True` 流式解析 —— 避免整表驻留内存。
    """
    import os
    import io
    import openpyxl

    if file_storage is None:
        return None, '未收到文件'
    filename = (getattr(file_storage, 'filename', '') or '').lower()
    _, ext = os.path.splitext(filename)
    if ext not in allowed_ext:
        return None, '文件格式不支持：仅允许 %s' % '、'.join(allowed_ext)

    # 长度校验：优先用 stream 的长度信息，避免为大文件分配内存
    try:
        stream = file_storage.stream
        pos = stream.tell()
        stream.seek(0, os.SEEK_END)
        size = stream.tell() - pos
        stream.seek(pos)
    except Exception:
        size = None
    if size is not None and size > max_bytes:
        return None, '文件过大：%.1f MB，上限 %d MB' % (size / 1048576.0, max_bytes // 1048576)

    try:
        data = file_storage.read()
    except Exception as e:
        return None, '文件读取失败：%s' % e
    if data is None:
        return None, '文件为空'
    if len(data) > max_bytes:   # 二次限长（stream 取长度失败时的兜底）
        return None, '文件过大：%.1f MB，上限 %d MB' % (len(data) / 1048576.0, max_bytes // 1048576)
    try:
        wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=read_only)
    except Exception as e:
        return None, '文件解析失败：%s' % e
    return wb, None
