import pymysql, math
from datetime import datetime, timedelta
conn = pymysql.connect(host='127.0.0.1', port=3306, user='root', password='', database='schedule_system', charset='utf8mb4')
cur = conn.cursor(pymysql.cursors.DictCursor)

def d(s): 
    s=str(s); s=s[:8] if len(s)>=8 else s
    return datetime.strptime(s,'%Y%m%d')

cur.execute("SELECT material_code, production_time, production_cycle FROM products WHERE material_code IN ('C003','B002','M001','M003')")
pm={}
for r in cur.fetchall():
    pt=float(r['production_time'] or 0); pc=float(r['production_cycle'] or 0)
    pm[r['material_code']]=(pt,pc)
    print("PROD", r['material_code'], "time",pt,"cycle",pc)

cur.execute("SELECT parent_matnr, comp_matnr, comp_menge, yield_qty FROM bom WHERE parent_matnr IN ('C003','B002')")
bm={}
for r in cur.fetchall():
    bm.setdefault(r['parent_matnr'],[]).append(r)
    print("BOM", r['parent_matnr'],"->",r['comp_matnr'],"menge",r['comp_menge'],"yield",r['yield_qty'])

def lead(m,q):
    pt,pc=pm.get(m,(0,0))
    if pt>0 and q>0: return math.ceil(q/pt)
    return pc

# C003 top row
c3_end=d('20260821'); c3_qty=1100
c3_cyc=lead('C003',c3_qty)
c3_req=c3_end - timedelta(days=c3_cyc)   # C003 children demand date
print("\nC003 end",c3_end.date(),"cyc",c3_cyc,"children req",c3_req.date())

# B002 qty from C003
b2_ratio=[b for b in bm.get('C003',[]) if b['comp_matnr']=='B002'][0]
q2=(c3_qty/float(b2_ratio['yield_qty'] or 1))*float(b2_ratio['comp_menge'] or 0)
print("B002 qty from C003 =",q2)
b2_cyc=lead('B002',q2)
b2_req=c3_req - timedelta(days=b2_cyc)
print("B002 as produced -> its OWN batch_date_end should be ~",b2_req.date())
print("Existing B002 SEMI row 901fefc2 batch_date_end = 20260817")
