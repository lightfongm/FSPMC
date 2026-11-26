import urllib.request, json
BASE="http://127.0.0.1:5000"
def req(m,p,b=None):
    d=json.dumps(b).encode() if b is not None else None
    r=urllib.request.Request(BASE+p,data=d,method=m,headers={"Content-Type":"application/json"})
    try:
        with urllib.request.urlopen(r,timeout=10) as x: return x.status,json.loads(x.read().decode())
    except urllib.error.HTTPError as e: return e.code,json.loads(e.read().decode())

print("== 默认状态（省略 status）==")
st,j=req("POST","/api/purchase-orders",{
 "purchase_order_no":"ZZCLOSE","order_date":"2026-07-20",
 "supplier_code":"G001","supplier_name":"一号供应商",
 "material_code":"MCLOSE","material_name":"闭测料",
 "order_qty":100,"arrival_qty":50,"plan_arrival_date":"2026-07-22"})
print("  POST 省略status ->",st, "落库状态:", j['data'].get('status') if st==200 else j.get('message'))
ids=[]
if st==200:
    pid=j['po_no']; ids.append(pid)
    # 第二行同组
    st2,j2=req("POST","/api/purchase-orders",{
     "purchase_order_no":"ZZCLOSE","order_date":"2026-07-20",
     "supplier_code":"G001","supplier_name":"一号供应商",
     "material_code":"MCLOSE","material_name":"闭测料",
     "order_qty":100,"arrival_qty":50,"plan_arrival_date":"2026-07-23"})
    if st2==200: ids.append(j2['po_no'])
    # 两行各填 50 → 合计100 = 订单量 → 应自动关闭(auto)
    for pid in ids:
        req("PUT","/api/purchase-orders/"+pid,{"actual_arrival_qty":50,"actual_arrival_date":"2026-07-21"})
    print("  填齐后两行状态:")
    for pid in ids:
        _,d=req("GET","/api/purchase-orders/"+pid); print("   ",pid,"->",d['data']['status'],d['data']['close_source'])
    # 把其中一行清回0 → 未到齐 → auto 行应重开为 在途
    req("PUT","/api/purchase-orders/"+ids[0],{"actual_arrival_qty":0})
    print("  清回0后状态:")
    for pid in ids:
        _,d=req("GET","/api/purchase-orders/"+pid); print("   ",pid,"->",d['data']['status'],d['data']['close_source'])
    for pid in ids:
        req("DELETE","/api/purchase-orders/"+pid)
    print("  已清理")
