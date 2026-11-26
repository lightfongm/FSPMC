def work_orders_device_load():
    """设备负荷汇总（只读，按窗口）：展示所有「非不可用」的生产设备/包装设备（基准列表），
    并附「未定生产设备」（存在未分配设备工单时）。各列按「起始日期 + 显示天数」窗口计算：
    工单数/创建/批准/执行、总时间(=days*24h)、等待时间(维修/保养占窗小时)、
    有效时间(=总时间-等待)、工单时间(窗内工单交集小时)、负荷占比(=工单时间/有效时间)。
    窗口参数： ?start=YYYY-MM-DD&days=N （缺省 start=今天, days=7）。无任何写操作。"""
    try:
        start_s = request.args.get('start')
        days_s = request.args.get('days')
        try:
            win_start = datetime.strptime(start_s, '%Y-%m-%d') if start_s else datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        except Exception:
            win_start = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        win_start = win_start.replace(hour=0, minute=0, second=0, microsecond=0)
        try:
            days = int(days_s) if days_s else 7
        except Exception:
            days = 7
        if days <= 0:
            days = 7
        win_end = win_start + timedelta(days=days)
        win_hours = float(days) * 24.0

        def _inter_hours(a0, a1, b0, b1):
            lo = max(a0, b0)
            hi = min(a1, b1)
            if hi <= lo:
                return 0.0
            return (hi - lo).total_seconds() / 3600.0

        rows = query_db(
            "SELECT workcenter_name, status, planned_start, planned_end "
            "FROM work_orders WHERE status IN ('创建','批准','执行') "
            "AND planned_start IS NOT NULL AND planned_end IS NOT NULL")
        equip_rows = query_db(
            "SELECT code, name, type, status FROM equipment "
            "WHERE type IN ('生产设备','包装设备') AND (status IS NULL OR status <> '不可用') "
            "AND name IS NOT NULL AND name <> ''")
        maint = query_db(
            "SELECT name, start_time, end_time FROM equipment "
            "WHERE status IN ('保养','维修') AND start_time IS NOT NULL AND end_time IS NOT NULL")

        maint_by_name = {}
        for m in maint:
            nm = (m.get('name') or '').strip()
            if not nm:
                continue
            ms = _wo_parse_dt(m['start_time'])
            me = _wo_parse_dt(m['end_time'])
            if not ms or not me:
                continue
            if me <= win_start or ms >= win_end:
                continue
            maint_by_name[nm] = maint_by_name.get(nm, 0.0) + _inter_hours(ms, me, win_start, win_end)

        by_dev = {}
        unassigned = {'order_count': 0, 'created': 0, 'approved': 0, 'executing': 0, 'order_hours': 0.0}
        has_unassigned = False

        def _dev(nm):
            return by_dev.setdefault(nm, {'workcenter_name': nm, 'order_count': 0,
                                          'created': 0, 'approved': 0, 'executing': 0,
                                          'order_hours': 0.0})

        for r in rows:
            eq = (r['workcenter_name'] or '').strip()
            ps = _wo_parse_dt(r['planned_start'])
            pe = _wo_parse_dt(r['planned_end'])
            if not ps or not pe:
                continue
            if pe <= win_start or ps >= win_end:
                continue
            st = r['status']
            if not eq:
                has_unassigned = True
                unassigned['order_count'] += 1
                if st == '创建':
                    unassigned['created'] += 1
                elif st == '批准':
                    unassigned['approved'] += 1
                elif st == '执行':
                    unassigned['executing'] += 1
                unassigned['order_hours'] += _inter_hours(ps, pe, win_start, win_end)
                continue
            d = _dev(eq)
            d['order_count'] += 1
            if st == '创建':
                d['created'] += 1
            elif st == '批准':
                d['approved'] += 1
            elif st == '执行':
                d['executing'] += 1
            d['order_hours'] += _inter_hours(ps, pe, win_start, win_end)

        result = []
        for e in equip_rows:
            nm = (e['name'] or '').strip()
            code = (e.get('code') or '').strip()
            etype = (e.get('type') or '').strip()
            wait = maint_by_name.get(nm, 0.0)
            d = by_dev.get(nm, {'order_count': 0, 'created': 0, 'approved': 0,
                                'executing': 0, 'order_hours': 0.0})
            effective = max(0.0, win_hours - wait)
            load_ratio = (d['order_hours'] / effective) if effective > 0 else 0.0
            result.append({
                'workcenter_name': nm,
                'equipment_code': code,
                'equipment_type': etype,
                'order_count': d['order_count'],
                'created': d['created'],
                'approved': d['approved'],
                'executing': d['executing'],
                'total_hours': round(win_hours, 2),
                'wait_hours': round(wait, 2),
                'effective_hours': round(effective, 2),
                'order_hours': round(d['order_hours'], 2),
                'load_ratio': round(load_ratio, 4),
            })

        if has_unassigned:
            effective = max(0.0, win_hours)
            load_ratio = (unassigned['order_hours'] / effective) if effective > 0 else 0.0
            result.append({
                'workcenter_name': '未定生产设备',
                'equipment_code': '',
                'equipment_type': '',
                'order_count': unassigned['order_count'],
                'created': unassigned['created'],
                'approved': unassigned['approved'],
                'executing': unassigned['executing'],
                'total_hours': round(win_hours, 2),
                'wait_hours': 0.0,
                'effective_hours': round(effective, 2),
                'order_hours': round(unassigned['order_hours'], 2),
                'load_ratio': round(load_ratio, 4),
            })

        def _sort_key(x):
            if x['workcenter_name'] == '未定生产设备':
                return (0, '')
            t = x.get('equipment_type', '')
            grp = 1 if t == '生产设备' else (2 if t == '包装设备' else 3)
            return (grp, x.get('equipment_code') or '')
        result.sort(key=_sort_key)

        return jsonify({'success': True, 'devices': result,
                        'window': {'start': win_start.strftime('%Y-%m-%d'),
                                   
