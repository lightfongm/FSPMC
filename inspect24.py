import pymysql
conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='', database='schedule_system', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

cur.execute("""
SELECT ms.id, ms.schedule_id, ms.work_order_id, ms.material_code, ms.component_code,
       ms.component_name, ms.parent_code, ms.required_qty, ms.source, ms.source_refs,
       ms.component_demand_date,
       s.plan_qty, s.batch_qty, s.batch_date_end, s.aufnr, s.source AS sch_source,
       s.order_no, s.sales_posnr, s.drive_aufnr, s.parent_code AS sch_parent, s.source_refs AS sch_source_refs
FROM peacock_material_shortage ms
LEFT JOIN peacock_schedule s ON ms.schedule_id = s.id
ORDER BY ms.component_demand_date, ms.component_code
""")
rows = cur.fetchall()
print("TOTAL ROWS:", len(rows))
print("="*140)
for i, r in enumerate(rows, 1):
    pq = r['plan_qty']
    pqf = float(pq) if pq is not None else None
    flag = ""
    if 22 <= i <= 26:
        flag = " <-- IN 22-26"
    if pqf == 1100:
        flag += " <-- PQTY=1100"
    print(f"#{i} id={r['id']} mat={r['material_code']} comp={r['component_code']} req={r['required_qty']} "
          f"src={r['source']} sref={r['source_refs']} pqty={pq} bqty={r['batch_qty']} "
          f"cdd={r['component_demand_date']} schsrc={r['sch_source']} ord={r['order_no']}-{r['sales_posnr']} drive={r['drive_aufnr']}{flag}")
