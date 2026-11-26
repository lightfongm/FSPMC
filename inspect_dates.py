import pymysql
conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='', database='schedule_system', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

print("=== B002 schedule rows incl deleted (start/end) ===")
cur.execute("""SELECT id, material_code, source, aufnr, order_no, sales_posnr, drive_aufnr,
                      parent_code, batch_qty, plan_qty, batch_date_start, batch_date_end, source_refs, is_deleted
               FROM peacock_schedule WHERE material_code='B002' ORDER BY batch_date_end""")
for x in cur.fetchall():
    print(x)

print()
print("=== C003 top row (drive for this) ===")
cur.execute("""SELECT id, material_code, source, aufnr, order_no, sales_posnr, drive_aufnr, batch_qty, plan_qty, batch_date_start, batch_date_end, is_deleted
               FROM peacock_schedule WHERE material_code='C003' ORDER BY batch_date_end""")
for x in cur.fetchall():
    print(x)
