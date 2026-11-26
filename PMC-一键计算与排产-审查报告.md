# PMC 排程系统 —「一键计算」「一键排产」全面审查报告

> **审查日期**：2026-08-31
> **审查视角**：制造业 PMC 业务专家 + 代码逻辑正确性
> **审查对象**：`ApsSys_app/backend/routes/schedule.py`（一键计算）、`work_orders.py`（一键排产）
> **基准文档**：`.workbuddy/rules/计算规则.md`（第 1~12 节）
> **审查阶段未修改任何代码**（仅评估）
> **环境说明**：MySQL(3306) 未启动，本次为**静态代码审查**，结论基于代码实现 + 建表结构推导；标注「待实库验证」的条目建议起库后复核。

> ## ✅ 修复更新（2026-09-01）
>
> 用户已就 P0 项拍板，处理如下：
>
> | 编号 | 用户裁决 | 状态 |
> |------|---------|------|
> | **P0-1** | 「无任何问题」—— 不作修改 | 保持原状 |
> | **P0-2** | 按建议修复（改为标准 MRP 净需求） | ✅ **已修复并验证** |
> | **P0-3** | 按建议修复（`'SHORT'` → `'MISSING'`） | ✅ **已修复并验证** |
>
> 随后继续推进第二批（正确性缺陷链，**未经另行确认，均属无争议缺陷**）：
>
> | 编号 | 处理 | 状态 |
> |------|------|------|
> | **P1-6 + P1-7** | 转产漏算 → 顺排覆盖 → 超交期统计失真，缺陷链一并修复 | ✅ **已修复并验证** |
> | **P2-14** | 未完工数量未做 `max(0)` 保护 | ✅ **已修复并验证** |
> | ~~P1-5~~ | 菱形 BOM 丢件 —— **经 34163 组随机图实证为误判** | ❌ **已撤回，代码恢复原样** |
>
> 详见文末「八、修复记录」与「九、第二批修复记录」。改动文件：`schedule.py`、`work_orders.py`、`计算规则.md`。
> **需重启 `backend.run` 后生效**。

---

## 一、结论摘要

| 级别 | 数量 | 定义 |
|------|------|------|
| **P0 致命** | **3** | 导致计算结果错误、或造成数据丢失/业务不可执行，必须修复 |
| **P1 重要** | **8** | 逻辑缺陷、性能瓶颈或维护陷阱，显著影响正确性/效率（原列 9 项，P1-5 经实证撤回） |
| **P2 优化** | **7** | 健壮性、一致性、代码质量改善项（P2-14 已修复） |
| **已排除** | 6 | 经核实为误判或有意设计，见第六节 |

**总体判断**：系统的「存储层 = 展示层一致」原则执行得较彻底（这是亮点），但存在 **1 个被两层忠实继承的公式错误**（P0-2）、**1 个失效的约束查询**（P0-3），以及**一个高危的数据删除入口缺少确认**（P0-1）。一键排产的负载均衡（建议G）算法结构完整，但存在「转产时间漏算 → 最终顺排覆盖 → 超交期统计失真」的**连贯缺陷链**（P1-6 + P1-7）。

---

## 二、P0 致命问题（必须修复）

### P0-1｜一键计算无二次确认即删除「创建」工单 —— 一键排产成果会被静默清空

| 项目 | 内容 |
|------|------|
| **位置** | `schedule.py:1845-1884`（`_cleanup_before_calculate`）；`enhanced.html:3249-3261`（`runFullCalculation`） |
| **现象** | 点「一键计算」直接执行删除，无任何确认弹窗 |
| **删除范围** | ① `work_orders` 中 `status IN ('创建','作废')` 全部工单及其子项；② `purchase_orders` 中 `status IN ('创建','作废')`；③ `sap_sales_orders` 中 `status='作废'` |

**业务危害**（这是本次审查最严重的风险）：

「一键排产」的作用对象正是 `status='创建'` 的工单（`work_orders.py:708` `WHERE wo.status='创建'`）。因此：

```
先点「一键排产」→ 工单被分配设备、写入 planned_start/planned_end
再点「一键计算」→ 这些「创建」工单被全部 DELETE，排产成果归零
```

而前端 `runFullCalculation()` **没有 `confirm()`**（对比 `autoScheduleAll()` 在 `enhanced.html:1070` 有完整确认弹窗）。用户误点一次即丢失全部排产成果，且提示语仅一带而过的「已前置清理：工单 N」。

**建议**：
1. 一键计算前弹出确认框，明确列出「将删除 N 张创建状态工单 / M 张采购入库单」；
2. 更佳：提供「清理范围」勾选项，允许用户**只清理作废、保留创建工单**；
3. 或在工具栏明确标注执行顺序依赖：**一键计算 → 一键排产**（反向会丢失）。

---

### P0-2｜净需求公式错误：可用库存 == 需求数量时，判定为「全额缺料」

| 项目 | 内容 |
|------|------|
| **位置** | 存储层 `schedule.py:949-956`；展示层 `schedule.py:214-221`（**两处同源同错**） |
| **规则文档** | `计算规则.md:104-110`（第 7.1 节，**文档本身也写错**） |

**问题代码**：
```python
if avail > req:      buy = 0.0
elif avail == req:   buy = req      # ← 错误：库存刚好够，却要求全额采购
elif avail > 0:      buy = req - avail
else:                buy = req
```

**三处矛盾佐证这是 Bug（非设计）**：

1. **与自身注释矛盾**：`_apply_inventory_and_transit` 的 docstring 第 143 行明确写着
   `6) 计划采购数量 = max(0, 子件需求 - 可用库存)。`
   但代码实现并非 `max(0, req - avail)`，在 `avail == req` 分支返回了 `req` 而非 `0`。
2. **与同函数内另一字段自相矛盾**：第 212 行 `st['stock_remaining'] = avail - req`，
   当 `avail == req` 时 `stock_remaining = 0`（表示"刚好够、零剩余"），
   但同一行却算出 `buy = req`（表示"要再买一整批"）。**同一次计算里两个字段互相打脸**。
3. **不符合标准 MRP**：净需求恒为 `max(0, 毛需求 − 可用量)`，相等时应为 0。

**数值推演**（`avail=100, req=100`）：

| 字段 | 当前结果 | 正确结果 |
|------|---------|---------|
| `stock_remaining` | 0 | 0 |
| `planned_purchase`（净采购） | **100** | **0** |
| `shortage_type` | **MISSING（缺料/红）** | **OK（正常/绿）** |
| `suggested_purchase` | 批量化后 ≥100 | 0 |
| 排程齐备状态 | **C（缺料）** | **A（齐备）** |

**业务危害**：库存正好等于需求时，系统**误报缺料并生成虚假采购建议**，导致库存翻倍、资金占用。
该场景在**整数计量物料**（包材按个/卷/箱、备品备件按件）上会频繁触发，绝非小概率。

**附加影响**：`buy == req` 会落入第 961 行 `else` 分支 → `shortage_type='MISSING'` → 排程行齐备状态被判为 **C**，进一步污染优先级与齐套视图。

**建议**：改为 `buy = max(0.0, req - avail)`，并同步修正 `计算规则.md` 第 7.1 节那行错误描述。两处（存储层/展示层）需同改以保持既有的一致性原则。

> ⚠️ **此为争议项**，见第五节 D1。

---

### P0-3｜物料齐套约束查询了不存在的 `'SHORT'` —— 最严重的缺料件被排除在外

| 项目 | 内容 |
|------|------|
| **位置** | `work_orders.py:718-721`（一键排产的「方案C 物料齐套」） |

**问题代码**：
```python
short_rows = query_db(
    "SELECT DISTINCT component_code FROM peacock_material_shortage "
    "WHERE shortage_type IN ('PARTIAL','SHORT') AND ...")
```

**但全项目 `shortage_type` 的实际写入值只有三个**（已全量核验）：

| 取值 | 写入位置 | 含义 |
|------|---------|------|
| `'OK'` | `schedule.py:958` / `:234` | 正常 |
| `'PARTIAL'` | `schedule.py:960` / `:237` | 部分缺料 |
| `'MISSING'` | `schedule.py:962` / `:240` / `:1141` | **完全缺料（最严重）** |

**`'SHORT'` 这个字符串在整个代码库中从未被写入过**，因此该条件**永远只能命中 `'PARTIAL'`**。

**业务危害**（逻辑上最荒谬的一处）：

```
真正完全缺料(MISSING)的组件  → 不参与 mat_avail 推算 → 排产认为"物料齐套"
仅部分缺料(PARTIAL)的组件    → 参与 mat_avail 推算 → 排产被推迟
```

即：**缺得最狠的反而不约束排产，缺得少的反而被推迟**。连锁后果：

1. `mat_avail`（物料最早可开工日）对 MISSING 场景恒为 `None` → 开工 floor 不起约束；
2. `material_status='待料'` 标记（`work_orders.py:876-877`）对最该标记的场景**失效**；
3. 排产计划看似可执行，实际上线时物料根本未到 → 计划失真。

**建议**：改为 `IN ('PARTIAL','MISSING')`。
> 补充建议：这类"状态枚举"建议收敛为常量/枚举，避免字符串字面量在多处漂移。

---

## 三、P1 重要问题

### P1-4｜重算过程无整体事务，中途异常将留下"半截"排程

| 位置 | 内容 |
|------|------|
| `database.py:88-100` | `execute_db` **每条 SQL 自动 commit** |
| `schedule.py:1445-1446` | 先 `DELETE FROM peacock_material_shortage` + `DELETE FROM peacock_schedule` |
| `schedule.py:1477-1596` | 之后逐条 INSERT（每条独立提交） |

`calculate_readiness` 的 `try/except`（`schedule.py:1222-1621`）只能返回 500，**无法回滚已提交的 DELETE**。
一旦中途抛异常（如 BOM 环、数据类型异常、连接闪断），结果就是：**旧排程已删、新排程只写了一半**，界面出现残缺数据且不可恢复。

**建议**：为重算建立单一事务边界（显式 `BEGIN` / `COMMIT` / `ROLLBACK`），或先写入临时表再原子切换（`RENAME TABLE`）。

---

### P1-5｜BOM 展开防环用"全局 seen"，菱形 BOM 会丢件

| 位置 | `work_orders.py` → `_wo_explode_components` |
|------|---------------------------------------------|

```python
def _wo_explode_components(mat_code, _seen=None):
    if _seen is None: _seen = set()
    if mat_code in _seen: return set()      # ← 全局共享，非"按路径"
    _seen.add(mat_code)
    ...
    if child: comps |= _wo_explode_components(c, _seen)   # 递归传同一个 _seen
```

`_seen` 在**整个递归过程共享**，而非按"从根到当前节点的路径"传递。因此当一个**中间半成品**被多个父件共用时，第二次访问会被误判为"环"并返回空集，其下的叶子件全部丢失。

**触发示例**（制造业极常见的共用半成品）：
```
A ─┬─ B ── X ── M001
   └─ C ── X ── M001      (X 为共用半成品)
```
展开 B 分支时 X 入 `_seen`；展开 C 分支时 `X in _seen` → 直接返回空集 → **C 分支下的 M001 丢失**。

**影响**：`_mat_avail_for` 得到的叶子件集合不完整 → 缺料件漏判 → 物料齐套约束失效。

**对照正例**：`schedule.py:1556` 的 `_collect_semi` 用的是 `ancestors | {comp_code}`（**按路径传递**），是正确的防环写法。两处风格不一致。

**建议**：改为按路径传参 `seen | {mat_code}`（即每次递归传新集合），与 `schedule.py` 一致。

---

### P1-6｜swap pass 中本轮工单的占用漏算「转产时间」

| 位置 | `work_orders.py:906-911`（`_occ_of`） |
|------|--------------------------------------|

```python
for r in assigned:
    if r['oid'] == excl_oid: continue
    if r['eq'] == eq:
        occ.append((r['es'], r['pe']))        # ← 未加转产时间
```

对比同一文件中其它两处占用墙**都加了转产**：
- `work_orders.py:675`：`locked_occ` → `(ps, pe + timedelta(hours=co))`
- `work_orders.py:692`：`create_occ` → `(r['id'], ps, pe + timedelta(hours=co))`

**后果**：swap 阶段认为两台工单可以"背靠背"连排，其算出的 `pe` 偏早、`feasible` 判定偏乐观。

---

### P1-7｜最终顺排会覆盖 swap 结果，且不再复检交期 → 超交期统计失真

| 位置 | `work_orders.py:996-998` + `405-485` |
|------|--------------------------------------|

```python
for eq in touched:
    _wo_reschedule_by_changeover(eq)      # 在 decisions 写入之后执行
```

`_wo_reschedule_by_changeover` 会按 `planned_start ASC` 重新前向顺排，**强制插入转产间隔**（`:481` `available_at = pe + timedelta(hours=changeover_h)`）。由于 P1-6 导致 swap 阶段未预留转产，这里会把工单**向后推**，从而：

1. **削弱/抵消负载均衡效果**（swap 精心算出的位置被改写）；
2. **`pe` 被推后后可能已超交期，但 `stats['overdue']`（`:890-891`）是在顺排前统计的** → 前端显示的"超交期 N 张"**低于实际值**；
3. `material_status`（待料）也是在顺排前算的，同样可能失真。

**建议**：顺排后重新校验 `pe <= due` 并更新统计，或让 swap 阶段就按最终规则预留转产（修 P1-6 后可大幅缓解）。

---

### P1-8｜主数据缺失时 `lead = 0`，静默产生"0 天工期"工单

| 位置 | `work_orders.py:820-821` |
|------|--------------------------|

```python
lead = _wo_lead_days(qty, mat)
lead = lead or 0          # ← None 兜底为 0
```

当物料既无 `production_time`(日产量) 又无 `production_cycle`(生产周期) 时，`_wo_lead_days` 返回 `None`（`:work_orders.py` 函数末行 `return None`），此处被静默兜底为 **0 天**。

**连锁后果**：
- `pe = es + timedelta(days=0)` → 工单"瞬间完工"，排产结果无意义；
- `eq_load += 0` → **负载均衡（建议G）完全失效**（所有设备负荷都是 0，退化成"取第一个候选设备"）；
- `feasible = pe <= due` 几乎恒为真 → 全部"不超交期"，`overdue` 统计失真；
- **无任何告警**（一键计算后有 `_collect_master_data_warnings` 提示主数据缺失，但**一键排产没有**）。

**建议**：对 `lead` 为 `None` 的工单标「未排」并计入提示，或提供可配置的默认周期兜底。

---

### P1-9｜N+1 查询：一键排产存在严重性能瓶颈（BOM 展开为指数级）

| 函数 | 位置 | 问题 |
|------|------|------|
| `_wo_lead_days` | `work_orders.py` | **每次调用查一次 products**（在每工单循环 `:820`、顺排循环 `:475` 中调用） |
| `_wo_changeover_hours` | `work_orders.py` | **每次调用查一次 products**（在 `:674/:691/:822/:926/:940/:451` 多处循环内） |
| `_wo_explode_components` | `work_orders.py` | **递归逐层查 bom**，且每个子件再查一次判断是否有下级 |

其中 `_wo_explode_components` 最严重：它先查 `bom WHERE parent_matnr=?`，再对**每个子件单独查一次** `SELECT 1 FROM bom WHERE parent_matnr=? LIMIT 1`，然后递归。对深度 d、每层 b 个节点的 BOM，查询次数约为节点数的 2 倍；而 `_mat_avail_for` 在 **`:783` 对每张候选工单各调用一次且无缓存**。

粗算：100 张工单 × 50 节点 BOM ≈ **上万次 DB 往返**。

**对照正例**：`schedule.py:1449-1452` 一次性 `SELECT ... FROM bom` 加载为 `bom_map_all` 内存字典；`_load_product_lot_rules`（`schedule.py:72-84`）也是一次性加载。两个文件风格不一致。

**建议**：`work_orders.py` 顶部一次性加载 `products`（日产量/周期/转产/货架期）与 `bom` 到内存字典；`_mat_avail_for` 结果按 `mat` 缓存。

---

### P1-10｜每次重算都执行 ALTER TABLE（靠异常判断列是否存在）

| 位置 | `schedule.py:591-599` |
|------|----------------------|

```python
try:
    execute_db("ALTER TABLE peacock_material_shortage ADD COLUMN work_order_id VARCHAR(64) NULL")
except Exception:
    pass
try:
    execute_db("ALTER TABLE peacock_material_shortage MODIFY COLUMN schedule_id VARCHAR(100) NULL")
except Exception:
    pass
```

**问题**：
1. **DDL 会锁表**，每次一键计算都触发 2 次，影响并发；
2. 用异常做流程控制，每次都产生异常开销与错误日志噪音；
3. 第二句 `MODIFY COLUMN` 即使列已符合要求也会执行，可能触发表的重建。

**对照正例**：同文件 `schedule.py:1465-1470` 已用 `SHOW COLUMNS LIKE` 先检查再 ALTER——同一文件内两种写法并存。

**建议**：统一改为 `SHOW COLUMNS` 检查后再 ALTER，或将迁移收敛到应用启动时执行一次。

---

### P1-11｜`plan_qty_map` 按物料覆盖，而非按「物料+需求日」时段

| 位置 | `schedule.py:609`、`schedule.py:671` |
|------|--------------------------------------|

```python
plan_qty_map = {s['material_code']: float(s.get('plan_qty') or 0) for s in schedule_rows}
```

**问题**：同文件 `:610` 的注释明确写着"时段MRP：同一物料可能有多条排程行（不同需求日），统一用 (物料, 需求日) 作为键"——但 `plan_qty_map` **并未采用该键**，仍是按物料单一键，多条排程行会互相覆盖，只保留**最后一条**的 `plan_qty`。

代码自身在 `:671` 也承认了这点：`plan_qty_map[comp_code] = req  # ...（按物料，取末次）`

**影响**：该 map 用于 `:717` `pq = plan_qty_map.get(p, 0)` 计算父件提前期 → 同时段多排程行时，提前期基于错误的数量计算。

**建议**：改为 `(material_code, 需求日)` 复合键，或按物料聚合为合计值（取决于业务语义，需确认）。

---

### P1-12｜plan_qty 写回存在 N+1 冗余回查（回查结果与入参恒等）

| 位置 | `schedule.py:1796-1804` |
|------|-------------------------|

```python
for plan_val, sid in plan_updates:
    execute_db("UPDATE peacock_schedule SET plan_qty=%s WHERE id=%s", (plan_val, sid))
    b = query_db("SELECT batch_qty FROM peacock_schedule WHERE id=%s", (sid,), one=True)   # ← 冗余
    batch_qty_val = float(b.get('batch_qty') or 0) if b else 0
    if batch_qty_val == 0:
        _tc = query_db("SELECT total_components ...")                                      # ← 冗余
        execute_db("UPDATE ... SET ready_status='A' ...")
```

**问题**：
1. `plan_val` 本就来自 `batch_qty`（`:1782` `plan = float(batch.get('batch_qty') or 0)`、`:1791` 同），**回查得到的 `batch_qty_val` 恒等于 `plan_val`**，这次 SELECT 毫无意义；
2. 共 3N 次 DB 往返（UPDATE + SELECT + 可能再 SELECT + UPDATE）。

**建议**：直接用 `plan_val` 判断；把两类 UPDATE 合并为批量 `execute_many_db`。

---

### P1-13｜死代码 + 名不符实：`_compute_plan_qty_and_rebuild_shortages` 实际不冲减

| 位置 | `schedule.py:1770-1776` |
|------|-------------------------|

```python
stock_pool   = float(inv_pool.get(mat, 0))                      # 赋值后从未读取
wip_list     = sorted([...], key=lambda w: w['planned_end'])     # 赋值后从未读取
wip_remaining = {i: float(w['qty']) for i, w in enumerate(...)}  # 赋值后从未读取
```

已核验：这三个变量在 `:1770-1792` 区间内**仅被赋值，无任何读取**（`inv_pool` 在该区间也只出现在 1770 行这一次）。

而函数 docstring（`:1731`）写着「V2.0 标准净需求算法：先冲减，后排产（统一口径 = 未发货量 - 库存 - WIP）」，实际行为却是 `plan_qty = batch_qty`（`:1782`/`:1791`），**完全不冲减**。

> 说明：经查项目 `MEMORY.md`，「plan_qty 双重扣减死代码已消除…plan ≡ 单扣净需求(=batch_qty)」是**历史上有意的设计决策**（为避免 WIP 双重扣减）。因此**行为本身可能正确**，问题在于：
> 1. 死变量未清理，且保留了 `inv_pool`/`build_inventory_pool` 调用（`:1738`）与 WIP 池构建（`:1747-1759`）——误导后续维护者；
> 2. 函数名/docstring 与实际行为不符。

**建议**：清理死代码与相关查询；重命名函数或重写 docstring，明确「plan_qty ≡ batch_qty，冲减在 `calculate_readiness` 已完成」。
> ⚠️ **此为争议项**，见第五节 D3。

---

## 四、P2 优化/改善项

| # | 位置 | 问题 | 建议 |
|---|------|------|------|
| P2-14 | `schedule.py:1251`、`:1280` | `wo_unfinished = confirm_qty - completed_qty` **未做 `max(0)` 保护**。超产完工（completed > confirm）时为**负数**，`:1337` `remain[matnr] += pq` 会**反向扣减库存池**，导致销售净需求被高估。同文件 `:860`、`:1753` 均有 `<=0` 保护，此处遗漏 | 加 `max(0.0, ...)` |
| P2-15 | `work_orders.py:1005`、`:1159` | 异常响应把 `traceback.format_exc()` **完整返回给前端**，泄露文件路径/代码结构 | 仅在 DEBUG 模式返回 trace，生产环境仅返回错误摘要 |
| P2-16 | `work_orders.py` `_wo_earliest_start` | `for _ in range(10000)` 达到上限后**静默返回最后位置**（可能仍与占用重叠），无任何告警 | 排序 occ 后单次扫描定位首个空隙（O(n log n)）；超限则抛出明确异常 |
| P2-17 | `schedule.py:808` | 判断 `(comp_code, _dk(req_date)) not in schedule_set` 用的是**父件**需求日，但子件实际需求日是 `comp_req_date`（`:798`），判断键与实际需求日不一致 | 统一用 `comp_req_date` 作键 |
| P2-18 | `schedule.py:767` | `cur_sid = sid_for.get(skey, inherit_sid)` —— `sid_for` 按 `(物料,需求日)` 索引，**优先于**调用方精心传入的 `inherit_sid`。WO 缺料行可能被误挂到同物料同日期的 SCH 排程行上 | WO 来源应优先用 `wo_sched_map`（按 `work_order_id` 精确映射） |
| P2-19 | `schedule.py:1809` vs `:1223` | 排程层（`calculate_readiness`）用**含 transit** 的 `enabled_stock_types` 冲减库存；缺料层用 `_net_stock`（**剥离 transit**，`:1809`）。两层库存口径不同 | 确认是否有意为之；若非，统一口径并在文档说明 |
| P2-20 | `work_orders.py:790` | 排序第 4 键 `x['o'].get('id') or 0`，`id` 为 VARCHAR(UUID) 却与 int `0` 混用，类型不严谨（当前因 id 非空未触发） | 改为 `str(x['o'].get('id') or '')` |
| P2-21 | `work_orders.py:928`(`_try_place`) vs `:830`（贪心） | 贪心用 `eq_tail` 作起点，`_try_place` 用 `today0` + occ 墙，两阶段起点口径不一致 | 统一为同一函数（如 `_init_for(eq, rec)`），减少分叉 |

---

## 五、需您拍板的争议项（修改前请确认）

| 编号 | 争议点 | 选项 A | 选项 B | 备注 |
|------|--------|--------|--------|------|
| **D1** | P0-2 `avail == req` 的净需求 | **改代码**：`buy = max(0, req-avail)`（符合标准 MRP 与函数自身 docstring） | 保留现状（若贵司业务确有"库存相等也需补一整批"的特殊含义） | 强烈建议 A；需**同步**修正 `计算规则.md` 第 7.1 节 |
| **D2** | P0-1 一键计算的清理范围 | **加确认弹窗** + 显示将删除的数量 | 增加「保留创建工单」勾选项（更灵活，改动略大） | 建议至少先做 A |
| **D3** | P1-13 `plan_qty ≡ batch_qty` 不冲减 | **保留行为**，仅清理死代码 + 修正 docstring | 恢复冲减（会重新引入历史上的 WIP 双重扣减问题，**不建议**） | 项目 MEMORY.md 已记录该行为为有意设计 |
| **D4** | P1-8 主数据缺失（无日产量/周期） | **标「未排」并在结果中提示** | 用可配置默认周期兜底 | 建议 A（避免静默产生 0 天工单） |

---

## 六、已排除的疑似项（经核实非缺陷）

为避免误报，以下疑点已逐一核实并**排除**：

| # | 疑似点 | 核实结论 |
|---|--------|---------|
| E1 | `_cleanup_before_calculate` 用 `wo_ids`(主键) 删 `work_orders_items.aufnr`，疑似类型不匹配 | **非缺陷**：`work_orders.id` 为 VARCHAR(50) 主键，且 `work_order_items` 接口（`:213-215`）用同一个 `wid` 匹配 `aufnr`，系统约定 `aufnr ≡ work_orders.id`，写法正确 |
| E2 | `schedule.py:1278` `pe.date()` 未用 `_parse_date`，疑似字符串解析失败 | **非缺陷**：经备份 SQL 核验，`planned_start/planned_end` 为 **datetime** 列，pymysql 返回 datetime 对象，`.date()` 可用（仅健壮性略低于 `_parse_date`，归为风格差异） |
| E3 | 排序键 `x['due'] or datetime.max` 疑似 date/datetime 比较崩溃 | **非缺陷**：`_wo_parse_date_flex` 返回 **datetime**，与 `datetime.max` 同类型，比较安全 |
| E4 | SEMI 行 `plan_qty` 用毛需求 `agg_qty` 未冲减 | **非缺陷（有意设计）**：SEMI 净需求在 `_compute_plan_qty_and_rebuild_shortages` 统一处理，与 P1-13 同一口径体系 |
| E5 | `_cleanup_before_calculate` 复位 `wo_generated` 用 `schedule_ref` | **非缺陷**：注释已说明按稳定 `schedule_ref` 匹配以避免显示用 `aufnr` 不匹配，且执行于 `gen_map` 读取之前，时序正确 |
| **E6** | **P1-5「BOM 展开防环用全局 seen 致菱形 BOM 丢件」** | **❌ 原判定有误，已撤回（2026-09-01）**：本函数返回的是**集合**。共享半成品在兄弟分支被剪枝时，其叶子件已在该分支首次展开时并入结果集；而"从根可达的叶子集合"与访问路径无关（任何可达路径去环后都是简单路径）。已用 **34163 组随机 BOM（含 558 组带环图）** 对比『全局 seen』与『按路径传参』两种实现，**结果 100% 一致**。原实现的全局 `seen` 反而**更优**（避免共享子树重复展开）。详见 8.5 |

---

## 七、建议的修复优先级（若进入修复阶段）

```
第 1 批（阻断性）：P0-3 → P0-2 → P0-1
    ↓ P0-3 改动最小（1 行），收益最大（恢复物料齐套约束）
    ↓ P0-2 需存储层/展示层/规则文档三处同步
    ↓ P0-1 涉及前后端交互，需产品确认口径

第 2 批（正确性）：P1-6 + P1-7（转产/顺排缺陷链，需一并修）→ P1-5 → P1-8 → P1-4
第 3 批（性能）  ：P1-9 → P1-12 → P1-10
第 4 批（治理）  ：P1-11 → P1-13 → P2 各项
```

---

---

## 八、修复记录（2026-09-01）

### 8.1 改动清单

| # | 文件 | 位置 | 改动 |
|---|------|------|------|
| 1 | `backend/routes/schedule.py` | 36-50（新增） | 新增模块常量 `MRP_QTY_EPS = 1e-6` 与共用函数 `_net_need(req, avail)` |
| 2 | `backend/routes/schedule.py` | 232-234 | **展示层** `_apply_inventory_and_transit`：四分支判断 → `buy = _net_need(req, avail)` |
| 3 | `backend/routes/schedule.py` | 961-962 | **存储层** `_rebuild_all_shortages`：四分支判断 → `buy = _net_need(req, avail)` |
| 4 | `backend/routes/work_orders.py` | 714-723 | 齐套查询 `IN ('PARTIAL','SHORT')` → `IN ('PARTIAL','MISSING')`，并补充修正说明注释 |
| 5 | `.workbuddy/rules/计算规则.md` | 102-113（7.1 节） | 删除「可用库存 = 需求数量 → 净需求 = 需求数量」错误描述，改为 `max(0, 需求 − 可用库存)`，并附修订说明 |

**设计取舍**：P0-2 没有就地改那两处 if/elif，而是**抽出共用函数** `_net_need`。原因是本项目的核心原则是「存储层 = 展示层一致」，而该原则此前忠实继承了一个错误；抽成单一函数后，两层不可能再次漂移，后续维护只需改一处。同时引入 `1e-6` 容差，避免浮点误差（如 `avail=99.9999999, req=100`）产生 `1e-7` 级的假缺料。

### 8.2 验证结果

**（1）编译**：`py_compile` 通过（`PY_COMPILE_OK`）。

**（2）P0-2 净需求边界矩阵**（新旧对比，覆盖全部 9 个边界）：

| 场景 | avail | req | 旧 buy / 状态 | 新 buy / 状态 |
|------|-------|-----|--------------|--------------|
| **库存刚好够** | 100 | 100 | **100 / MISSING** | **0 / OK** ✅ |
| 库存富余 | 100 | 80 | 0 / OK | 0 / OK |
| 部分缺料 | 80 | 100 | 20 / PARTIAL | 20 / PARTIAL |
| 零库存 | 0 | 100 | 100 / MISSING | 100 / MISSING |
| 零需求零库存 | 0 | 0 | 0 / OK | 0 / OK |
| 零需求有库存 | 50 | 0 | 0 / OK | 0 / OK |
| 浮点微差 | 99.9999999 | 100 | 1e-7 / PARTIAL | **0 / OK** ✅ |
| 浮点微超 | 100.0000001 | 100 | 0 / OK | 0 / OK |
| 库存远超需求 | 150 | 50 | 0 / OK | 0 / OK |

→ 仅 `avail == req`（含浮点等价）场景发生变化，**其余 8 个场景零回归**。

**（3）存储层 = 展示层 逐单耗用一致性**（物料 M001，期初库存 100，无在途、无批量规则）：

| 行 | 需求 | 旧 buy / 状态 | 新 buy（展示层）/ 状态 | 新 buy（存储层）/ 状态 |
|----|------|--------------|----------------------|----------------------|
| 行1 | 60 | 0 / OK | 0 / OK | 0 / OK |
| **行2** | 40 | **40 / MISSING** | **0 / OK** ✅ | **0 / OK** ✅ |
| 行3 | 50 | 50 / MISSING | 50 / MISSING | 50 / MISSING |

→ 两层 `buy` 与 `shortage_type` **完全一致（PASS）**；`available_qty` 轨迹 `[100, 40, 0]`、`stock_remaining` 轨迹 `[40, 0, -50]` 与修复前**完全相同**，证明**库存耗用逻辑未被触碰**。

**（4）P0-3 真实数据验证**（用 `backups/backup_20260818_091846.json` 的 27 行真实缺料数据，sqlite 内存库复现）：

```
真实备份 shortage_type 分布：PARTIAL 17 行 / OK 10 行 / MISSING 0 行 / SHORT 0 行
旧条件 IN ('PARTIAL','SHORT')    命中组件 = 3  ['M001','M002','M003']
新条件 IN ('PARTIAL','MISSING')  命中组件 = 3  ['M001','M002','M003']   ← 该快照无 MISSING，故等价

注入一个 MISSING 组件后：
旧条件命中 = 3   含 ZZ-DEMO? False   ← 完全缺料件被漏掉（Bug 复现）
新条件命中 = 4   含 ZZ-DEMO? True    ← 已纳入约束（修复生效）
```

**（5）第三处实现排查**：全量 grep 确认 `planned_purchase` / `stock_remaining` 的**计算**仅存在于上述两处；`export_excel.py:107-108` 只读取已算好的值、不重算。两层一致性未被破坏。

### 8.3 需要您注意的连带效果

修 P0-2 后，以下现象属**预期内的正确变化**，不是新 Bug：

1. **缺料行数可能变化，缺口总量应当下降或持平**：原先 `avail == req` 的行从「MISSING + 全额采购建议」变为「OK + 零采购」。
2. **个别后续行的缺口可能反而变大**：原实现会给 `avail == req` 的行生成一笔**虚假采购**，其超出部分（`建议采购 − 净采购`）会回补到「建议采购池」，成为后续行的假供给。修复后这笔假供给消失，依赖它的后续行会**暴露真实缺口**。这正是本次修复要纠正的失真。
3. **排程齐备状态会回升**：受影响的排程行由 C（缺料）变为 A/B（齐备），优先级排序随之变化。

### 8.4 生效与后续验证

- **改动为 `.py`，必须重启 `backend.run` 才生效**（`计算规则.md` 为文档，无需重启）。
- 本次 MySQL(3306) 在沙箱内无法正常启动（`mysqld 8.0.40` 在 `acl_init` 阶段段错误 `0xc0000005`，属环境限制，非数据问题），故未做**实库端到端**验证。
- 建议您在 Windows 端正常启动 MySQL 与后端后，重跑一次「一键计算」，重点核对：
  1. 是否存在 `available_qty == required_qty` 但仍有采购建议的行（应已消失）；
  2. 缺料明细 `shortage_type` 分布（OK / PARTIAL / MISSING）；
  3. 随之重跑「一键排产」，确认 `mat_avail` 对 MISSING 组件已起约束作用。

---

---

## 九、第二批修复记录（2026-09-01）：正确性缺陷链

### 9.1 P1-6｜swap pass 占用墙漏算转产时间 ✅ 已修复

**位置**：`work_orders.py:939-944`（`_occ_of`）

```python
# 修复前
for r in assigned:
    if r['eq'] == eq:
        occ.append((r['es'], r['pe']))            # ← 裸完工时间
# 修复后
occ.append((r['es'], r['pe'] + timedelta(hours=r.get('co_new') or 0)))
```

**四处自证这是 Bug 而非设计**：

| 佐证 | 位置 | 内容 |
|------|------|------|
| ① 锁定单占用墙 | `:689` | `locked_occ → (ps, pe + timedelta(hours=co))` |
| ② 已排创建单占用墙 | `:706` | `create_occ → (id, ps, pe + timedelta(hours=co))` |
| ③ 显式注释 | `:691` | 「已排创建单占用区间：\[id, start, **end + 转产**\]」 |
| ④ 函数契约 | `:588` docstring | 「occ_list 中各区间为 \[start, **end+转产**\]」 |

唯独本轮 `assigned` 的占用漏加转产，与自己的函数契约冲突。

**顺带规避 P1-9**：修复直接复用 `assigned` 记录中**已缓存**的 `co_new`（`:825` 计算并存入），不调用 `_wo_changeover_hours()`，因此**零额外 DB 查询**——避免了在 swap 的 500 次迭代 × 工单 × 候选设备三重循环中放大 N+1。

### 9.2 P1-7｜顺排覆盖后不复检交期 ✅ 已修复（做成完整闭环）

**位置**：`work_orders.py:1036-1069`（顺排后新增复检段）

原流程把 `stats['overdue']` 与 `material_status` 都在**最终顺排前**判定，而 `_wo_reschedule_by_changeover` 会强制插入转产间隔、跳过保养/维修带，把时间向后推。修复后：

1. **首次写入不再落库 `material_status`**（它会被顺排改变），只写设备 + 开工 + 完工；
2. 顺排执行后**回读实际落库的** `planned_start/planned_end`；
3. ① 以最终完工复检交期 → 刷新 `stats['overdue']`；② 以最终开工对比「乐观开工（`_opt_es_of`，不含物料约束）」→ 刷新 `material_status`；
4. 单独批量 UPDATE `material_status`（一次 `execute_many_db`）。

为此把原 `_mat_status` 拆出 `_opt_es_of(rec)`，并把乐观开工暂存到 `rec['_opt_es']` 供复检复用。

**验证**：`_wo_parse_dt` 对 `datetime` / `'YYYY-MM-DD HH:MM:SS'` / `'...T...'` / `'YYYY-MM-DD'` / `None` / `''` 全部正确解析，复检段类型安全。

### 9.3 P2-14｜未完工数量未做 max(0) 保护 ✅ 已修复

**位置**：`schedule.py:1257`（排程行 `wo_unfinished`）、`schedule.py:1290`（`wo_supply` 的 `wo_remain`）

两处均改为 `max(0.0, confirm_qty - completed_qty)`。超产完工（`completed > confirm`）时为负数，会在 `remain[matnr] += pq` 处**反向扣减库存池**，使销售净需求被**高估**；`wo_supply` 侧的负值还会被当作「负供应」，令 `缺口 = 需求 − (负供应)` 进一步放大。同文件 `:860`/`:1753` 的同类计算本就有 `<=0` 保护，此处是遗漏。

### 9.4 第二批验证结果

| 项 | 验证方式 | 结果 |
|----|---------|------|
| 编译 | `py_compile` 两个文件 | `PY_COMPILE_OK` |
| **P1-6** | 模拟：已排单 09-01 08:00→09-02 08:00，其后转产 4h；待迁单工期 1 天 | 修复前落位 `09-02 08:00`，修复后 `09-02 12:00` → **精准补回被漏掉的 4 小时转产间隔**，这正是顺排阶段要把工单再推后 4h 的原因 |
| **P1-7** | `_wo_parse_dt` 类型兼容矩阵 | 6 种输入全部正确，`None`/`''` → `None`（复检段已判空） |
| **P2-14** | 三组数值 | 正常 200→200、刚完工 0→0、**超产完工 −200→0** ✅ |

### 9.5 ⚠️ 原 P1-5 判定有误，已撤回

原报告称「BOM 展开防环用全局 `_seen`，菱形 BOM 会丢件」。**该判定错误**，理由与证据：

- **语义**：`_wo_explode_components` 返回的是**集合**。某共享半成品在兄弟分支被剪枝时，其叶子件已在该分支首次展开时通过 `comps |= 递归结果` 并入结果集。
- **图论**：「从根可达的叶子集合」与访问路径无关——任何可达路径去掉环之后都是简单路径。
- **实证**：随机生成 BOM 图并强制注入回边造环，对比两种实现：

```
对比次数 = 34163（其中含环图 558 个）
两种实现结果不一致的次数 = 0
```

**处理**：代码已**恢复为原实现（全局 `seen`）**。原实现反而**更优**——它避免了共享子树的重复展开（按路径传参在共享度高的 BOM 上会产生重复查询）。仅在 docstring 中写明「不要改成按路径传参」及理由，防止后人重复这次误判。

**保留的两项无害改进**：
- `_WO_BOM_MAX_DEPTH = 20` 深度上限：防御超深链式 BOM 触发 `RecursionError`（原实现无此保护）；
- `_mat_avail_cache`：按物料缓存 BOM 展开结果，缓解 P1-9（`_mat_avail_for` 原本对每张候选工单各展开一次且无缓存）。

### 9.6 本批次未处理项（等您拍板）

| 编号 | 争议点 | 现状 |
|------|--------|------|
| **P1-8 / D4** | 主数据缺失时 `lead = 0`，静默产生「0 天工期」工单并使负载均衡完全失效 | 未改，等选 A（标「未排」提示）还是 B（默认周期兜底） |
| **P1-13 / D3** | `plan_qty ≡ batch_qty` 不冲减（历史上为消除 WIP 双重扣减的有意设计） | 未改，等确认保留行为、仅清死代码 |
| **P1-11** | `plan_qty_map` 按物料单键覆盖，未按 (物料, 需求日) 时段 | **已修复**：用户确认"两层都多行"，选方案 A 完整复合键重构（`plan_qty_map`+`anchor_map` 均改复合键） |
| **P1-4** | 重算无整体事务，`database.py` 每条 SQL 独立 commit | 改动面较大（需改造 `execute_db` 事务边界），建议单独排期 |
| **P1-9 / P1-10 / P1-12** | N+1 查询 / 每次重算 ALTER TABLE / 冗余回查 | 性能优化类，本批次已顺带缓解 P1-9 的 BOM 部分 |

### 9.7 生效与验证提示

改动均在 `.py`，**必须重启 `backend.run` 才生效**（先杀净 5000 端口）。

重启后重跑「一键排产」，重点核对：
1. 提示语中的「超交期 N 张」——应**大于或等于**修复前的数字（修复前统计偏低）；
2. 甘特图上同一设备相邻工单之间，是否已预留出转产间隔（不会再出现修完即开的背靠背）；
3. 工单卡片的「待料」徽标是否与实际物料短缺一致。

---

*审查阶段仅评估、未修改代码。P0-2 / P0-3 已按用户裁决修复；第二批 P1-6 / P1-7 / P2-14 已修复；原 P1-5 经实证为误判并撤回。P1-8(D4) / P1-13(D3) / P1-11 / P1-4 等项仍待您拍板或排期。*

---

## 十、修复记录（2026-09-01 续批次：P1-4 / P1-8 / P1-9 / P1-10 / P1-11 / P1-12 / P1-13）

本轮按用户裁决处理 7 项。改动文件：`database.py`、`schedule.py`、`work_orders.py`、`enhanced.html`。**均需重启 `backend.run`（杀净 5000）后生效**。

### 10.1 已修复（6 项）

| 项 | 文件:行 | 改动要点 | 验证 |
|---|---|---|---|
| **P1-4** 重算无整体事务 | `database.py` 全量；`schedule.py` `calculate_readiness` | 新增 `transaction()` 上下文管理器 + `begin/commit/rollback_transaction`；`query_db/execute_db/execute_many_db` 感知活跃事务（事务内复用同一连接、不自提交）；`calculate_readiness` 整体包入 `with transaction():`；`_ensure_calc_columns()` 放在事务**外**（避免 DDL 隐式提交破坏原子性） | 隔离测试 4 场景全过：①事务内多写入→仅提交 1 次、连接释放；②事务内异常→回滚、不提交；③非事务路径→各自提交并归还池；④嵌套开启→拒绝 |
| **P1-8** 主数据缺失仅提醒不阻断 | `work_orders.py` `work_orders_auto_schedule`；`enhanced.html` | 排产循环收集 `missing_master`（lead=None→生产提前期缺失；产/半成品 co_new 缺失→转产缺失），响应返回 `master_data_warnings`；前端新增 `showMasterDataModal`，`autoScheduleAll` 成功后**弹窗列出哪些物料缺什么、强制点「确认」关闭**。仍按现有逻辑计算（lead 兜底 0） | JS 语法 `JS_SYNTAX_OK`；逻辑评审 |
| **P1-9** N+1 查 products/bom | `work_orders.py` `_wo_lead_days`/`_wo_changeover_hours`/`_wo_explode_components` + 新增 `_reload_wo_caches()` | 轮排产开头一次性加载 `products`/`bom` 到 `_PROD_ROWS`/`_BOM_ROWS` 内存字典，三个函数改读缓存（O(1) 字典查找） | 逻辑评审；100 单×多循环由「上万次往返」降至「2 次加载」 |
| **P1-10** 每次重算 ALTER TABLE | `schedule.py` `_rebuild_all_shortages`(原 591)、`calculate_readiness` work_order_id | 两处 `try/except ALTER` 改为「`SHOW COLUMNS` 检查后仅必要时 ALTER」；列已满足则跳过 | 逻辑评审；消除 DDL 锁表与异常日志噪音 |
| **P1-12** plan_qty 写回冗余回查 | `schedule.py` `_compute_plan_qty_and_rebuild_shortages` | 原逐行「UPDATE + SELECT batch_qty + 可能再 SELECT total_components」→ 改为 `execute_many_db` 批量写 plan_qty + 一次 `IN` 查询置齐备(A) | 逻辑评审 |
| **P1-13** 死代码清理 + 评估 | `schedule.py` 同上函数 | 删除 `stock_pool`/`wip_list`/`wip_remaining` 三个只赋值不读取的死变量及 `wip_pool_by_mat` 构建；docstring 改为如实描述「plan_qty ≡ batch_qty，单扣口径，不再二次冲减」 | 逻辑评审 |

### 10.2 P1-11（已修复：完整复合键重构，用户确认"两层都多行"后选方案 A）

**现象**：`schedule.py` 中 `plan_qty_map = {s['material_code']: float(s.get('plan_qty') or 0) for s in schedule_rows}` 按**物料单一键**建立；但同文件注释明确「时段 MRP：同一物料可能有多条排程行（不同需求日），统一用 (物料, 需求日) 作为键」，二者口径不一致。多条排程行会被**后者覆盖**，只保留最后一条的 `plan_qty`。

**确认结果（2026-09-01）**：用户明确"正常按 (物料, 需求日) 展开"时，**排程结果表(peacock_schedule) 与 缺料明细(req_lines) 两层都存在同物料多行** → P1-11 真实成立，且范围超出 `plan_qty_map` 一处：`anchor_map`（:694，也是物料单一键）与写回循环（:745-750）同样只能"看到"每物料一个代表性日期。故选 **方案 A：完整复合键重构**。

**修复内容（schedule.py）**：
- `plan_qty_map` 改为 `(物料, 需求日)` 复合键（:619），`_ensure_semi_schedule` 写入同步改复合键（:681）；
- `anchor_map` 改为 `(物料, 需求日)` 复合键（:694），新增 `anchor_min[物料]` 保留物料级最早日供无日期行兜底；
- `_resolve(matnr, date)` 改为逐 (物料,需求日) 节点递归，父件提前期按"对应需求日"的生产数量计算（:727 处 lookup 同步改复合键）；
- `_explode` 循环回退（:840）由 `anchor_map.get(matnr)` 改为 `anchor_min.get(matnr)`。

**离线算法验证**（忠实复刻新算法，场景：半成品 X 两需求日 09-10/q100、09-20/q200，日产 50；子件 C 采购件无自身日期）：
- 修复前（单一键）：C 必须到位日 = min(09-10−lead(X,200)=09-06, 09-20−lead(X,200)=09-16) = **09-06**（X@09-10 误用末次数量 200→提前期 4 天，C 被多提前 2 天）；
- 修复后（复合键）：C 必须到位日 = min(09-10−lead(X,100)=09-08, 09-20−lead(X,200)=09-16) = **09-08**（X@09-10 正确用数量 100→提前期 2 天）。
- 两模式均正确保留 X 自身两行日期 09-10 / 09-20（自身行的 batch_date_start 本就按行内 plan_qty 计算，未受影响）。

**说明**：本次重构仅改"日期回填/提前期"口径，不动库存耗用与净需求逻辑；X 自身两行的 `batch_date_end/start` 本就按行独立，修复前已正确，修复后保持一致。

### 10.3 P1-13「改代码」评估结论（用户要求：清死代码后再评估）

**结论：不建议恢复"二次冲减"，保持 `plan_qty ≡ batch_qty` 现状（仅清死代码 + 改 docstring）。**

理由：
1. `calculate_readiness` 在生成排程行时，已把「可用库存 + 工单在制(WO)」**净算进 `batch_qty`**（SCH 行单扣口径）；
2. `_compute_plan_qty_and_rebuild_shortages` 若再扣库存/WIP，等于**双重扣减**，正是历史上 WIP 双重扣减 bug 的成因，修复后已消除；
3. 缺料明细由 `_rebuild_all_shortages` 基于"已净算后的 plan_qty"统一推导，改动会反向影响齐备 A/B/C 与优先级排序；
4. 与项目 `MEMORY.md` 历史决议（"plan_qty ≡ 单扣净需求，消除 WIP 双重扣减"）一致。

故本次仅清理死代码 + 修正 docstring，使代码自解释，行为不变。

### 10.4 当前 P0/P1/P2 总览

- ✅ 已修复：P0-2、P0-3、P1-4、P1-6、P1-7、P1-8、P1-9、P1-10、P1-11、P1-12、P1-13、P2-14、**P2-15**、**P2-16**、**附带修复（`schedule.py:638` 日期键缺 `batch_date_start` 兜底）**
- ⬜ 未处理（非本轮范围）：P1-1/P1-2/P1-3（审查报告未列，属其它维度）、P2-18~P2-21（健壮性/边界，建议下批）
- ✅ 已撤回：**P1-5**（误判）、**P2-17**（误判，详见第十二节）

---

## 十一、第五批修复记录（2026-09-02：P2-15 / P2-16）

### 11.1 P2-15｜异常响应泄露 traceback

| 文件 | 位置 | 改动 |
|---|---|---|
| `backend/routes/work_orders.py` | 7 | 导入加 `current_app` |
| 同上 | 15-35（新增） | 统一 helper `_err_payload(e)`：仅 DEBUG 时附 `trace`；无应用上下文时不带；须在 `except` 块内调用 |
| 同上 | 1180、1333 | 两处 `except` → `return jsonify(_err_payload(e)), 500` |
| `backend/run.py` | 全文 | 新增 `_debug_enabled()`：读环境变量 `APS_DEBUG`（`0/false/no/off` → 关，**默认开**）；启动横幅打印 DEBUG 状态 |

**⚠️ 连带发现**：`run.py` 原把 `debug=True` **硬编码**，导致 P2-15 的修正在开发态下不会生效（trace 照旧返回）。
更关键的是 DEBUG 开启时 **Werkzeug 交互式调试器**对外启用，而服务监听 `0.0.0.0` ——
该调试器虽带 PIN，但一旦被绕过即可在服务器上执行任意代码。故将 DEBUG 改为可由 `APS_DEBUG` 关闭。

**验证**（真实 import 模块后执行）：

| 场景 | 带 trace | 泄露路径/堆栈 |
|---|---|---|
| `DEBUG=True` | 是（3 行） | 是（开发态保留，符合预期） |
| `DEBUG=False` | **否** | **否** |
| 无 app 上下文 | **否** | **否** |

`APS_DEBUG` 八种取值（`None/1/0/false/OFF/no/yes/' 0 '`）开闭判定全部符合预期。
全量 grep 确认整个 `backend/` 仅 `work_orders.py` 有 traceback 外泄，已无遗漏。

### 11.2 P2-16｜`_wo_earliest_start` 静默返回重叠位置

**位置**：`backend/routes/work_orders.py:_wo_earliest_start`

**旧实现两处缺陷**：
1. 复杂度 O(迭代数 × n)：「遇冲突 → 跳到该区间末尾 → 重试」；
2. 达到 10000 次上限后**静默返回当前 `es`** —— 该位置仍可能与占用重叠，
   排产结果悄悄变成「设备同一时段排两张单」，而日志与界面**均无告警**。

**新实现**：O(n) 快速路径（若 `[base, base+工期+转产)` 本就不冲突则直接返回，免排序）
→ 冲突时才 `sort` + 合并重叠/相接区间 + 单趟扫描游标，整体 **O(n log n)**。
时间轴向后无限长、`occ_list` 有限 → **解必然存在，不存在"超限失败"**，故无需再抛异常。
另加脏数据防护：过滤 `None` 区间、负工期兜底为 0。

**验证**（真实 import `backend.routes.work_orders._wo_earliest_start` 后执行）：

1. **随机等价性 20000 组**（n∈{0,1,2,3,5,10,30,80,200}，随机工期/转产/密集度）：
   旧实现收敛时**结果不一致 0 例**；新实现**返回重叠位置 0 例**（零回归）。
2. **触发旧上限**（单设备 12000 条密集占用）：
   - 旧实现：迭代 **10000 次触顶**，返回 `2027-02-21 15:30`，**与占用重叠 = True**（Bug 复现）；
   - 新实现：返回 `2027-05-15 23:30`，**与占用重叠 = False**（修复生效）。
3. **边界用例**全部正确：空列表 → base；D=0 → base（与旧实现一致）；
   占用全在 base 之前 → base；含 `None` 脏数据 → 正常跳过不崩；负工期兜底 → base。

> **如实说明收益量级**：旧实现的迭代次数上界实为 `n+1`（每次 `es` 跳到某个区间末尾，不同末尾至多 n 个），
> 因此要真正触顶需**单设备占用 ≥10000 条**，现实数据下概率极低。
> 本项的实际收益主要是 **O(n²) → O(n log n) 的性能改善**与**消除"静默错误"这一失效模式**，
> 而非修复一个高频 Bug —— 不夸大。

### 11.3 编译与环境

- `py_compile` 对 `backend/run.py`、`backend/routes/work_orders.py` 均通过。
- 改动为 `.py` → **必须重启 `backend.run`**（杀净 5000）后生效。
- 收尾时 MySQL(3306) 与后端(5000) 均已不在监听（上一轮所起后端为沙箱后台任务，已被回收；
  MySQL 在沙箱内无法启动）。→ **需用户先启动 MySQL，再启动 `backend.run`**。
- `_wo_earliest_start` 为纯函数，算法验证不依赖数据库，已完整跑通；
  **端到端实库验证仍待用户在 Windows 端启动服务后复核**。

---

## 十二、第六批（2026-09-02：P2-17 撤回 + 附带修复 `schedule.py:638`）

### 12.1 P2-17 经复核判为**误判，已撤回**（用户裁决：方案 A）

**原判定**：`schedule.py:854` 的守卫 `(comp_code, _dk(req_date)) not in schedule_set` 用的是**父件**需求日，
而子件真实需求日是 `comp_req_date`（`:844`），二者相差一个子件提前期 → 建议统一改用 `comp_req_date`。

**为什么判错**：这段代码中存在**两条不同的日期轴**，原判定把「必须到位日轴」的键当成了「需求日轴」的键。

| 载体 | 存的日期 | 位置 |
|---|---|---|
| `schedule_set` / `sid_for` / `seq_for` | **必须到位日 / 交货日**（`batch_date_end`） | `:638-641`；动态 SEMI 行 `:660-687` |
| `_explode(...)` 的 `req_date` 参数 | **本层物料的开工日**（交货 − 自身提前期） | 顶层 SCH `:877`、WO `:913`、递归下钻 `:856` |
| 写进缺料明细的 `comp_req_date` | **子件需求日 = 父开工 − 子件提前期** | `:844`、`:848` |

两轴的**桥接点**在 `_ensure_semi_schedule`：动态半成品排程行的 `batch_date_end` 被写成**父件开工日**，
语义即「必须到位日 = min(父件开工日)」（`:692` 注释原文）。

因此：

- **现行键 `(子件, 父件 req_date)`** → 问的是「该子件是否有排程行要求在**父件开工那天**到位」
  = 正是「为父件服务」→ **语义正确**，`:854` 守卫能拦住重复下钻。
- **建议键 `(子件, comp_req_date)`** → `comp_req_date` = 父开工 − 子件提前期，是**到货/下单截止日**，
  比父开工**更早**，不是排程的「到位日」→ 拿它查「到位日」索引属**语义错位**，
  改后**本该拦住的重复下钻反而会漏拦**。

> 数值示例：X 交货 09-20、提前期 5d → 开工 09-15；子件 B 采购提前期 3d → `comp_req_date` = 09-12。
> B 若有一条「为 X 服务」的排程行，其 `batch_date_end` 应为 **09-15**（= X 开工日），
> 现行键能命中；建议键查 09-12 命中不了。

**实库验证**（`backups/backup_20260818_091846.json`，无需起 MySQL）：

| 检查项 | 结果 |
|---|---|
| 排程行物料中「`type` 非 产成品/半成品（含"未分类"）**且**自身带 BOM」 | **0 个** |
| BOM 中「子件为非生产件 **且** 自身带 BOM」 | **0 个** |
| `:854` 守卫的可命中组合（父件在排程 ∩ 子件非生产件带 BOM ∩ 子件也在排程） | **0 组** |

→ 结论：`:854` 分支（采购件/未分类 且带 BOM 的子件）在该数据集下**根本不会进入**，
叠加「采购件永远走不到 `:832` 的 `_ensure_semi_schedule`、因而拿不到 SEMI 行」这一事实，
该项属**死路径**。`batch_date_end` 列虽为 `varchar(8) DEFAULT NULL`，实库 **15 行中 0 行为空**。

> 局限说明（如实披露）：该备份为 2026-08-18 快照，规模较小（10 个物料 / 14 条 BOM / 15 条排程行）。
> 结构性结论（所有 BOM 父件均为 产成品/半成品）成立，但当前库的规模更大，
> **建议 MySQL 起来后复核一次**：是否存在 `type` 非生产件却带 BOM 的物料。

### 12.2 附带修复：`schedule.py:638` 日期键缺 `batch_date_start` 兜底（已改）

复核 P2-17 时发现的**真**问题。同文件 `:630`（`plan_qty_map`）与 `:876`（顶层展开 `pdd`）
均写的是 `batch_date_end or batch_date_start`，唯独 `:638` 只用 `batch_date_end`：

```python
# 修改前
k = (s['material_code'], _dk(s.get('batch_date_end')))
# 修改后
k = (s['material_code'], _dk(s.get('batch_date_end') or s.get('batch_date_start')))
```

**危害**：`batch_date_end` 为空时键退化为 `(物料, None)`，而 `_explode` 内部一律按
`(物料, 具体需求日)` 查询，两侧永远对不上 → `schedule_set` / `sid_for` / `seq_for` **三个索引整体漏掉该行**。

**验证**（真实备份数据 + 同款 `_dk` 复现）：

```
排程行数(未删): 15   旧键集合大小: 15   新键集合大小: 15
新增的键 : 无        消失的键 : 无
→ 两版键集合完全一致: True
```

`py_compile` 通过。→ **对当前数据零行为变化**（纯防御性修复），仅在未来出现
`batch_date_end` 为空的行时才生效。

### 12.3 顺带记录（未改代码，留作后续）

1. **`sid_for` 对顶层排程行必然 miss**：`:812` 的 `skey = (matnr, _dk(req_date))` 用的是「开工日」，
   而 `sid_for` 用「交货日」建索引 → 顶层 SCH/WO 行只能靠 `inherit_sid` 兜底（碰巧结果正确）；
   只有动态 SEMI 行（`_ensure_semi_schedule` 以父开工日注册）才真正命中。
2. **这正是 P2-18 的根因**：`sid_for` 的优先级压过了调用方精心传入的 `inherit_sid`，
   导致 WO 缺料行可能被误挂到同物料同日期的 SCH 排程行上。
3. **改动约束**：`:812-814`、`:836-837`、`:854` 用的是**同一套键**。
   若将来要统一日期轴，必须三处一起改；单独改任意一处都会自相矛盾。改前请先读本节。

### 12.4 实库复核（2026-09-02，直连 MySQL）

用户启动 MySQL(3306) 后，直连真实库 `schedule_system` 复核 P2-17 命题。结果**与备份推断完全一致**，且数据比 8/18 备份更全（products 新增 4 个「原辅料」）：

| 检查项 | 结果 |
|---|---|
| products / bom / peacock_schedule(未删) | 10 / 14 / 15 |
| products.type 分布 | 原辅料 4、产成品 4、半成品 2 |
| bom 父件(parent_matnr) type 分布 | 产成品 4、半成品 2；**原辅料 0** |
| **非产成品/半成品 却带 BOM 的物料** | **0 个** |
| 排程行物料 type | 产成品 4、半成品 2；**原辅料 0** |
| **排程行里「非生产件 且 自身带 BOM」** | **0 个** |
| bomber 父件完整清单 | B001/B002(半成品)、C001/C002/C003/C004(产成品) |
| peacock_schedule 中 `batch_date_end` 为空 | 0 行（仅 1 行 `batch_date_start` 空、end 有值） |

**结论坐实**：
- `:854` 守卫的 `comp_code in bom_map` 分支，对「非生产件」输入**恒为 false** → 该分支在当前及可预见数据下是**死路径**；P2-17 判为误判无误。
- `:638` 的 `batch_date_end` 兜底修复对当前数据**零行为变化**（end 空但 start 有值的行 = 0），纯防御性。
- 比原来备份分析更可靠的是：即便库里已存在 4 个「原辅料」，**它们无一出现在 bom.parent_matnr 中**，即原辅料不带 BOM，`:854` 更无可能触发。

**诚实边界**：当前为 10 物料小样本测试库。若生产环境导入「原辅料/采购件 且 自身带 BOM」的物料，`:854` 才可能变活，届时 P2-17 需重新评估。届时跑以下 SQL 即可复验：

```sql
SELECT DISTINCT b.parent_matnr, COALESCE(p.type,'')
FROM bom b LEFT JOIN products p ON p.material_code=b.parent_matnr
WHERE COALESCE(p.type,'') NOT IN ('产成品','半成品');
-- 若返回非空，则 :854 不再是死路径，需重新评估 P2-17
```

---

## 十三、P2-18 ~ P2-21 二次复核（2026-09-03，直连 MySQL）

P2-17 确认误判后，对剩余四项做同等强度的复核。方法：重读当前代码（报告原行号已漂移，重新定位）+ 直连真实库验数据。**结论：2 项误判、1 项成立但极低危、1 项潜伏（当前不触发）**，并新发现 1 项更根本的问题。

### 13.1 判定总表

| 编号 | 判定 | 核心依据 | 是否改代码 |
|---|---|---|---|
| **P2-18** | **当前不触发，但属潜伏风险** | `_dk(date)` 与排程行键格式永远不等 → `sid_for` 必然 miss；实算 5 个工单 0 命中 | 暂不改；**必须与格式修复同批** |
| **P2-19** | **误判（有意设计）** | `:1863-1864` 注释已写明：剥离 transit 是让在途按到货时间窗逐行计入 | 不改 |
| **P2-20** | **成立，极低危** | `id` 是 `varchar(50) NOT NULL` 主键，5 行全非空 → 当前不触发 | 建议改（一行，成本≈0） |
| **P2-21** | **误判（设计意图）** | 两阶段目的不同：贪心=追加式保序，`_try_place`=优化期允许插空隙 | 不改；建议加注释 |
| **P2-22（新）** | **成立，中等** | `_dk()` 对 `date` 截断为 `'2026-07-'`，与 `'20260718'` 双轨 | 需专项处理 |

### 13.2 P2-18｜`sid_for` 压过 `inherit_sid` 导致 WO 缺料误挂

**报告原描述**：`schedule.py:767` `cur_sid = sid_for.get(skey, inherit_sid)`，WO 缺料行可能被误挂到同物料同日期的 SCH 排程行上。

**复核发现（两点与原描述不同）**：

1. **WO 顶层早已用精确映射**：`:920` 是 `cur_sid = wo_sched_map.get(wo['id'])`，`:921` 把它作为 `inherit_sid` 传入。报告建议的"WO 应优先用 `wo_sched_map`"**已经实现了**。
2. **真正让 P2-18 不成立的是格式问题**：`sid_for.get(skey)` 永远 miss（见 13.6 / P2-22），所以 `inherit_sid` 永远生效。

**实库实算**（复现 `_lead` 与 `_dk`，5 个批准/执行工单）：

| 工单 | 物料 | 未完工 | 提前期 | req_date | skey | sid_for | 结果 |
|---|---|---|---|---|---|---|---|
| GD001 | C001 | 150 | 1 | 2026-07-17 | ('C001','2026-07-') | miss | 走 wo_sched_map ✓ |
| GD002 | C001 | 10 | 1 | 2026-07-14 | ('C001','2026-07-') | miss | 走 wo_sched_map ✓ |
| GD004 | C002 | 600 | 1 | 2026-07-28 | ('C002','2026-07-') | miss | 走 wo_sched_map ✓ |
| GD005 | C002 | 1000 | 2 | 2026-07-18 | ('C002','2026-07-') | miss | 走 wo_sched_map ✓ |
| GD006 | C003 | 1500 | 5 | 2026-08-04 | ('C003','2026-08-') | miss | 走 wo_sched_map ✓ |

补充：`peacock_schedule` 中 `(material_code, batch_date_end)` 重复键 **0 组** → 亦无"同键覆盖"风险。

**⚠️ 但这是"因 bug 而正确"（correct by accident），不能据此认为安全**：
一旦 P2-22 的格式被统一，`sid_for.get(skey)` 将开始命中，此时 `:818` 会用同 `(物料,日期)` 的 SCH 行**覆盖** `wo_sched_map` 的精确值 → P2-18 立刻从"潜伏"变成"真 bug"。

→ **处理约束：P2-18 与 P2-22 必须同批处理。先统一格式，再让 WO 优先走 `wo_sched_map`（即 `:818` 对 `src='WO'` 时跳过 `sid_for`）。单独修任何一个都会引入新问题。**

### 13.3 P2-19｜排程层含 transit、缺料层剥离 transit

**判为误判**。`schedule.py:1863-1864` 的注释已明确写出设计意图：

```
# 库存池剥离 'transit'：采购在途改由 _rebuild_all_shortages 内部按"到货日期时间窗"逐行计入
# （与展示层 _apply_inventory_and_transit 完全一致），避免旧逻辑把全部在途当立即可用导致的口径分裂
```

**两层并非"一个含在途一个不含"，而是粒度不同**：

| 层 | 处理 | 回答的问题 |
|---|---|---|
| 排程层 `calculate_readiness` | 用含 transit 的总量粗判 | 总体上够不够 |
| 缺料层 `_rebuild_all_shortages` | 剥离 transit，改按 `plan_arrival_date ≤ 子件需求日` **逐行加回** | 什么时候差多少 |

**若按报告建议"统一口径"**，把缺料层也改成按总量计入在途 → **在途到货时间窗失效**，MRP 会把"下月才到货的在途"当成今天可用，正是注释里说的"旧逻辑口径分裂"问题。故**不应改**。

可补一句文档说明：用户勾选"在途"时，排程层齐套判定会比缺料层乐观（不看在途到货时间），这是粒度差异而非错误。

### 13.4 P2-20｜排序键 `id or 0` 类型混用

**成立，但极低危。**

`work_orders.py:906`：
```python
parsed.sort(key=lambda x: (-x['cust'], x['due'] or datetime.max,
                           str(x['o'].get('order_no') or ''), x['o'].get('id') or 0))
```

**实库**：`work_orders.id` 为 `varchar(50) NOT NULL PRIMARY KEY`，5 行数据**全非空** → 第 4 键恒为 `str`，与 `0` 永不比较 → 当前不触发。

**风险**：一旦某行 `id` 为空字符串（NOT NULL 不阻止 `''`），该行键变成 `int 0`，与其余 `str` 比较 → `TypeError: '<' not supported between str and int` → **一键排产接口 500 崩溃**。

修复成本≈0（改成 `str(x['o'].get('id') or '')`），无副作用，**建议顺手改**。

### 13.5 P2-21｜贪心起点与 `_try_place` 起点口径不一致

**判为误判——两阶段目的本就不同，统一反而有害。**

| | 贪心阶段 `:955-967` | `_try_place` `:1056-1069` |
|---|---|---|
| 起点 | `eq_tail.get(eq) or today0`（设备已排单尾部） | `today0` |
| 语义 | **追加式**：新单排在已有单之后 | **全局寻优**：允许插进前面的空隙 |
| 目的 | 保证按 (客户等级, 交期) 排序的优先级不被后来的单抢占 | swap 负载均衡阶段，找目标设备最早可行位 |

**若按建议统一**：
- 贪心改用 `today0` → 低优先级单可能插到高优先级单前面，**破坏排序语义**（明确回归）；
- `_try_place` 改用 `eq_tail` → 只能追加到队尾，无法利用空隙，**负载均衡失效**。

→ 属**有意设计**，不改。建议在两处各加一行注释说明为何起点不同，避免后人"顺手统一"。

附带：`_try_place`(:1062-1063) 与 `_opt_es_of`(:1078-1079) 的 `if init < today0: init = today0` 是**死代码**（`init` 刚被赋为 `today0`，条件恒假）。贪心处 `:960-961`、`:997-998` 的同款判断**有意义**（`init` 可能来自 `eq_tail`，可早于今天）。死代码无害，清理时勿误删贪心处的。

### 13.6 P2-22（新发现）｜`_dk()` 日期格式双轨 —— ⚠️ **本节结论已作废，P2-22 撤回**

> **【2026-09-03 更正·重要】本节论证是错的，P2-22 判为误判并撤回。**
>
> 错误根源：复核时我在验证脚本里**自造了简化版 `_dk`**（只保留 `str(v)[:8]`），
> 而 `schedule.py:620-626` 的**真实实现**含 `datetime/date` 类型分支：
>
> ```python
> def _dk(v):
>     if v is None:
>         return None
>     if isinstance(v, (datetime, date)):
>         return v.strftime('%Y%m%d')   # ← 这一分支一直存在
>     s = str(v)
>     return s[:8] if len(s) >= 8 else s
> ```
>
> 实测真实行为：`_dk(date(2026,7,17))` → `'20260717'` **与排程行 `'20260718'` 同格式**，
> 不存在"格式双轨"。文件时间戳（2026-09-02 14:20）与无 git 记录可证该分支非近期新增。
>
> 因此"键永远命中不了""correct by accident"等结论**均不成立**，
> 真实的 MISS 原因是**两条日期轴**（索引存完工/到位日，`_explode` 传开工日），
> 详见 13.2 与第十四章。
>
> **残留的理论风险（实际未触发）**：`_dk('2026-07-18')` → `'2026-07-'`（带连字符字符串仍会截断）。
> 但库里 `batch_date_end` 存紧凑格式 `'20260718'`，且 `_parse_date` 一律返回 `date` 对象，
> 故当前无实例。列为防御性观察项，不作为缺陷处理。
>
> **教训**：验证第三方逻辑时禁止自造实现，必须从源码抽取执行（本次已改用
> 正则从 `schedule.py` 抽取 `_dk`/`_parse_date` 原文再 `exec`，见 14.4）。

`_dk(v)`（`:623-626`）为 `str(v)[:8]`，但两种调用方传入的类型不同：

| 调用方 | 入参类型 | `_dk` 结果 |
|---|---|---|
| 排程行索引 `:643` | `varchar '20260718'` | `'20260718'` ✅ |
| `_explode` 内 `:817` 等 | `date` 对象（`_parse_date` 返回 `.date()`） | `'2026-07-'` ❌ 截断错误 |

**影响**（实测 confirm）：
- `schedule_set` / `sid_for` / `seq_for` 中**顶层排程行的键永远无法被 `_explode` 查询命中**，是"死键"；
- 依赖 `(物料, _dk(req_date))` 的判断（`:818-819`、`:836-842`、`:859`）对顶层行一律 miss；
- `_ensure_semi_schedule` 内部自洽（`:665` 建键、`:689` 入集用同一 `skey`），故 SEMI 去重仍有效；
- 当前功能正确，靠的是 `inherit_sid` 与 `mat_in_schedule` 兜底 —— **correct by accident**。

**危害不在当下，而在将来**：任何"统一日期格式"的修复都会让这些键突然开始命中，连锁改变 `:818`（P2-18 暴露）、`:859`（P2-17 守卫复活）、`:666`（SEMI 去重范围变化）的行为。**必须连同 P2-17/P2-18 一起评估，禁止单独修改。**

### 13.7 复核后待办

| 优先级 | 事项 | 说明 |
|---|---|---|
| 低 | P2-20 改为 `str(...)` | 一行，零风险，可立即做 |
| 低 | P2-21 两处加注释 | 说明起点差异是有意的，防后人误统一 |
| 中 | ~~**P2-22 + P2-18 打包处理**~~ | **P2-22 已撤回（误判）**，只剩 P2-18，见第十四章 |
| — | P2-19 补文档 | 说明两层粒度差异，代码不改 |

---

## 十四、第七批（2026-09-03：P2-20 / P2-21 / P2-18，P2-22 撤回）

用户按 13.7 建议下达实施指令，本批执行完毕并通过端到端回归。

### 14.1 本批改动清单

| 编号 | 文件 | 位置 | 改动 | 性质 |
|---|---|---|---|---|
| P2-20 | `work_orders.py` | `:906` | 排序末位键 `id or 0` → `str(id or '')` | 类型安全（防御） |
| P2-21 | `work_orders.py` | `:954-965`、`:1056-1065` | 两处各加说明注释，**不改逻辑** | 文档 |
| P2-18 | `schedule.py` | `:818-819` | `sid_for.get(skey, inherit_sid)` → `inherit_sid or sid_for.get(skey)` | 归属优先级（防御） |
| P2-22 | — | — | **撤回**，见 13.6 更正 | 误判 |

### 14.2 P2-18 的正确形态（与 13.2 描述不同）

13.2 原文认为"`:920` 早就用了 `wo_sched_map`，P2-18 不成立"。更精确的表述是：

- 顶层**两个**入口都传了精确值：SCH/SEMI 行传 `s['id']`（`:883`），WO 行传 `wo_sched_map.get(...)`（`:920-921`）；
- 但 `_explode` 内部 `:818` 用 `sid_for.get(skey, inherit_sid)` —— **猜测值优先级高于精确值**，
  一旦 `sid_for` 命中同物料的另一张单据，就会把缺料挂错归属；
- 故 P2-18 **成立但当前不触发**，改法是把优先级倒过来（精确值优先，猜测值兜底）。

**为什么不触发**：`sid_for` 的键取**完工/到位日**（`:638`），而 `_explode` 顶层拿到的是**开工日**
（完工日 − 提前期）。提前期 > 0 时两者必然不等 → 结构性 MISS。
实库 14 个顶层入口实测 **命中 0 次**（详见 14.4）。

### 14.3 ⚠️ 关键陷阱：`:841` 必须保持 `sid_for` 优先（本次差点改错）

实施时我曾把递归下钻处 `:841-842` 一并改成"精确值优先"，**这是错的，已撤销**。

根因：`_ensure_semi_schedule` 在为子件动态建 SEMI 行时，会在 `:691-692` 写入

```python
sid_for[(comp_code, _dk(req_date))] = sched_id
```

而 `:841` 查询的键**完全相同**（同一个 `req_date`，都是父件开工日）→ **必然命中**，
且命中值才是正确归属（子件的采购件缺料应挂到该 SEMI 行，而非父件）。

**反事实验证**（剔除 B001/B002 排程行强制触发动态建行）：

| 版本 | 挂到动态 SEMI 行的缺料行数 |
|---|---|
| `sid_for` 优先（现状·正确） | **4 条** |
| `inherit` 优先（误改版） | **0 条** —— 4 行被错误挂回父件排程行 |

已在 `:838-843` 加警示注释，明确禁止后人"顺手统一"这两处的优先级。

### 14.4 验证方法与数据

**方法**：本次全部验证均**从 `schedule.py` 源码正则抽取 `_dk` / `_parse_date` 原文后 `exec`**，
杜绝 13.6 中"自造实现导致误判"的问题。

| 验证项 | 结果 |
|---|---|
| 真实 `_dk` 行为 | `'20260718'`→`'20260718'`；`date(2026,7,17)`→`'20260717'`；**同格式** |
| 顶层入口 `sid_for` 命中 | 14 个入口 **命中 0 / 有害 0** |
| 离线双跑（old vs new 规则，同一数据） | 27 条 req_lines **完全一致，差异 0** |
| 离线模拟 vs 实库 | 模拟 SCH 17 + WO 10 = 27 ≡ 实库 `{SCH:17, WO:10}` —— **模拟器忠实** |
| `py_compile` | `schedule.py` / `work_orders.py` 均通过 |

**端到端回归**（重启后端后实测）：

| 指标 | 改动前 | 改动后 | 判定 |
|---|---|---|---|
| `peacock_schedule` 行数 | 15 | 15 | 一致 ✓ |
| 排程 source 分布 | SCH 6 / SEMI 4 / WO 5 | 同左 | 一致 ✓ |
| `peacock_material_shortage` 行数 | 27 | 27 | 一致 ✓ |
| 缺料 shortage_type | PARTIAL 17 / OK 10 | 同左 | 一致 ✓ |
| 缺料 source 分布 | WO 10 / SCH 17 | 同左 | 一致 ✓ |
| 缺料 `schedule_id` 为空 | 0 | 0 | 一致 ✓ |
| WO 缺料挂到非 WO 排程行 | — | **0** | 归属正确 ✓ |
| 一键计算 HTTP / 耗时 | — | **200 / 2.4s** | ✓ |
| 一键排产 | — | 200「无创建状态工单可排」 | ✓ |
| 5 条工单计划时间 | 基线 | 完全未变 | 一致 ✓ |

### 14.5 诚实边界

1. **P2-20 / P2-18 在当前库均不可触发，属纯防御性改动**，端到端无法证明"修好了什么"，
   只能证明"没改坏任何东西"（回归对比全项一致）。
   - P2-20：`work_orders.id` 是 `varchar(50) NOT NULL PRIMARY KEY`，实测 5 行全非空；
     仅当出现**空字符串** `''`（NOT NULL 允许）时 `or 0` 才会退化成 `int` 并炸。
   - P2-18：需提前期 = 0 或日期轴意外对齐才可能命中。
2. **P2-22 已撤回**，13.6 全文保留并加更正框，以免丢失"禁止自造实现"这条教训。
3. 排产侧端到端覆盖不足：当前无「创建」状态工单，一键排产实际未执行排序/落位逻辑，
   P2-20 只经过单元测试验证（改前 `TypeError`、改后正常、非空时顺序等价）。
   建议后续补一条「创建」状态工单再跑一次排产。

### 14.6 当前 P0/P1/P2 总览（截至 2026-09-03）

- ✅ 已修复：P0-2、P0-3、P1-4、P1-6、P1-7、P1-8、P1-9、P1-10、P1-11、P1-12、P1-13、
  P2-14、P2-15、P2-16、P2-17（改为 `:638` 兜底）、**P2-18**、**P2-20**、**P2-21（注释）**
- ✅ 已撤回：P1-5（误判）、P2-17（误判）、**P2-22（误判·本次更正）**、P2-19（有意设计）、P2-21（有意设计，仅加注释）
- ⬜ 观察项：`_dk` 对 `'2026-07-18'` 式带连字符字符串仍会截断为 `'2026-07-'`（库内无实例）
