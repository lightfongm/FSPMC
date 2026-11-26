import pymysql
conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='', database='schedule_system', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

print("=== schedule rows for material B002 (XS003-1 related) ===")
cur.execute("""SELECT id, material_code, source, aufnr, order_no, sales_posnr, drive_aufnr,
                      parent_code, batch_qty, plan_qty, batch_date_start, batch_date_end, source_refs, is_deleted
               FROM peacock_schedule WHERE material_code='B002' ORDER BY batch_date_end""")
for r in cur.fetchall():
    print(r)

print()
print("=== ALL schedule rows order_no=XS003-1 (merged) ===")
cur.execute("""SELECT id, material_code, source, aufnr, order_no, sales_posnr, drive_aufnr,
                      parent_code, batch_qty, plan_qty, batch_date_end, source_refs
               FROM peacock_schedule WHERE order_no='XS003-1' OR source_refs LIKE '%XS003-1%' ORDER BY material_code, batch_date_end""")
for r in cur.fetchall():
    print(r)

print()
print("=== sales order XS003-1 line items ===")
cur.execute("""SELECT vbeln, posnr, matnr, kwmeng, lfimg, edatu FROM sap_sales_order_items WHERE vbeln='XS003-1' ORDER BY posnr""")
for r in cur.fetchall():
    print(r)
