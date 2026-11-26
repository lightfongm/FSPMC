import pymysql
conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='', database='schedule_system', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

print("=== shortage row #24 (B002 M003) full FK ===")
cur.execute("""SELECT id, schedule_id, work_order_id, material_code, component_code, parent_code,
                      required_qty, source, source_refs, component_demand_date
               FROM peacock_material_shortage WHERE id='99a18fbb-3ac0-4a90-87ee-592f93422b46'""")
r = cur.fetchone()
print(r)
sid = r['schedule_id']

print()
print("=== schedule row it points to (schedule_id) ===")
cur.execute("""SELECT id, material_code, source, aufnr, order_no, sales_posnr, drive_aufnr,
                      parent_code, batch_qty, plan_qty, batch_date_end, source_refs, is_deleted
               FROM peacock_schedule WHERE id=%s""", (sid,))
print(cur.fetchone())

print()
print("=== ALL B002 schedule rows (any deleted) incl XS003-1 drive ===")
cur.execute("""SELECT id, material_code, source, aufnr, order_no, sales_posnr, drive_aufnr,
                      parent_code, batch_qty, plan_qty, batch_date_end, source_refs, is_deleted
               FROM peacock_schedule WHERE material_code='B002' OR drive_aufnr='XS003-1'
               ORDER BY batch_date_end""")
for x in cur.fetchall():
    print(x)

print()
print("=== top SCH schedule row(s) order_no=XS003-1 ===")
cur.execute("""SELECT id, material_code, source, aufnr, order_no, sales_posnr, drive_aufnr,
                      parent_code, batch_qty, plan_qty, batch_date_end, source_refs, is_deleted
               FROM peacock_schedule WHERE order_no='XS003-1' ORDER BY batch_date_end""")
for x in cur.fetchall():
    print(x)
