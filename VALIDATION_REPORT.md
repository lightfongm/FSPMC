# PMC 一键计算 端到端验证报告

**验证日期**：2026-08-04
**验证人**：PMC 排程系统（自动化验证脚本 + 运行态 HTTP 实测）
**验证对象**：`/api/peacock/calculate`（一键计算）全套输出
**结论**：✅ **38 / 38 项全部 PASS**；过程中发现并修复 1 个严重 BUG（缺料明细组件需求双计）。

---

## 一、验证范围与方法

### 验证链路（与一键计算一致）
`一键计算` → `_cleanup_before_calculate()`（清草稿/作废）→ `calculate_readiness()`（齐备套 + 写 `peacock_schedule`，含半成品独立排程行）→ `calculate_priority()`（评分）→ `_compute_plan_qty_and_rebuild_shortages()`（净需求 `plan_qty` + `_rebuild_all_shortages` 缺料明细）。

### 验证方法
1. **隔离测试数据**：植入 `VAL-*` 前缀的独立数据集（物料 / BOM / 库存 / 销售订单 / 工单 WIP / 采购在途），与既有业务数据零冲突。
2. **真实路由调用**：通过 Flask 测试客户端 `POST /api/peacock/calculate` 真实跑完整链路（非 mock）。
3. **运行态 HTTP 实测**：另起已修复后端于 **5001 端口**，用真实 HTTP 请求复跑计算并核对 `peacock/schedules`、`peacock/shortages/all` 返回，确认修复在"运行中的服务"同样正确。
4. **逐项手算对照**：对 `plan_qty`、倒排日期、齐备状态、缺料可用/需求/建议采购/在途、组件 WIP 抵扣、批量规则逐项用手工公式核对。

---

## 二、发现并修复的 BUG（严重）

### BUG：缺料明细（存储层）组件需求双计 + 库存池重复消耗
- **位置**：`backend/routes/schedule.py` → `_rebuild_all_shortages()`
- **为什么会双计**：该函数把"毛需求"收集进同一个 `req_lines` 列表，但有**两条独立路径**都会调用 `_explode()` 把父件按 BOM 拆成子件需求并追加进来：
  1. **排程来源展开**（源码 405–416 行）：遍历 `peacock_schedule` 每一行（含"销售订单驱动的顶层成品" + "半成品独立排程行"）。这些行的 `plan_qty` 在 `calculate_readiness` 阶段**已经扣减过对应的 WIP**，BOM 展层已完整覆盖其全部组件需求。
  2. **独立工单展开**（源码 419–438 行）：又用 `SELECT ... FROM work_orders WHERE status IN ('批准','执行')` 把所有"批准/执行"工单**再展开一遍**。
  - 当某个物料**既**被销售订单排程驱动（有 `peacock_schedule` 行）、**又**有"批准/执行"工单时，它的组件需求被这两条路径**各展开一次 → 双计**。两条路径共享同一个 `req_lines` / 库存池 / 组件 WIP 池，于是同一组件需求被重复累加、库存池被重复消耗，存储层 `peacock_material_shortage` 甚至出现 (排程行, 组件) 的**重复行**。

#### 具体例子（测试数据 VALSF01）
排程阶段 `calculate_readiness` 算出的 `peacock_schedule`：
- `VALFG01`（成品）：销售 100 − WIP 工单 VALWO-FG(20) = **plan_qty 80**
- `VALSF01`（半成品）：被 VALFG01 的 BOM 驱动，WIP 工单 VALWO-SF(20) 不锁（半成品交货日空）→ **plan_qty 100**

→ `schedule_set = {VALFG01, VALSF01}`

BOM：`VALFG01 → VALSF01×1, VALRM01×2, VALPK01×4`；`VALSF01 → VALRM02×3, VALRM03×1`
工单 WIP（批准/执行）：`VALWO-FG(VALFG01,20)`、`VALWO-SF(VALSF01,20)`、`VALWO-RM2(VALRM02,10)`

**▶ 修复前（双计）**
- 路径① 排程展开（用 plan_qty）：
  - VALFG01(80) → VALSF01 ×80、VALRM01 ×160、VALPK01 ×320
  - VALSF01(100) → VALRM02 ×300、VALRM03 ×100
- 路径② 工单展开（用 effective_qty 20/20）：
  - VALWO-FG(20) → VALSF01 ×20、VALRM01 ×40、VALPK01 ×80
  - VALWO-SF(20) → VALRM02 ×60、VALRM03 ×20

合并后各组件 **gross 需求（错误）**：

| 组件 | 应得 gross | 修复前实际 | 偏差 |
|---|---|---|---|
| VALSF01 | 80 | 80 + 20 = **100** | +20（双计） |
| VALRM01 | 160 | 160 + 40 = **200** | +40 |
| VALPK01 | 320 | 320 + 80 = **400** | +80 |
| VALRM02 | 300 | 300 + 60 = **360** | +60 |
| VALRM03 | 100 | 100 + 20 = **120** | +20 |

后果：组件 `required_qty` 整体被放大 ≈1.25 倍；同一 (排程行, 组件) 在存储层出现**两行**（SCH 来源 + WO 来源），各自再扣一遍库存池 → 可用库存被重复消耗、净缺料虚高；组件级 WIP 也被重复抵扣两次。

**▶ 修复后（正确）**
第 304 行 `schedule_set = set(plan_qty_map.keys())`；第 428 行在工单循环内加 `if matnr in schedule_set: continue`：
- 路径② 中 `VALWO-FG(VALFG01)`、`VALWO-SF(VALSF01)` 因 `matnr ∈ schedule_set` **被跳过**；
- 只有"没有排程行、纯靠工单备货"的物料才会走工单路径（如 `VALWO-RM2(VALRM02)`，但 VALRM02 为原料无 BOM，展开为空）——这条路径对真正的独立备货工单**仍正确保留**。

最终 `req_lines` 仅来自路径①，组件 gross 需求恢复正确（80 / 160 / 320 / 300 / 100）。存储层实测：
- `VALSF01` 组件 `required_qty = 60 = 80 − 组件WIP 20`（来自 VALWO-SF 的 WIP 抵扣，仅扣一次）；
- `VALRM02` 组件 `required_qty = 290 = 300 − 组件WIP 10`（来自 VALWO-RM2 的 WIP 抵扣）；
- 不再出现重复行。修复前 `VALSF01` 会双计成 ~100 或产生两行。

> **一句话本质**：排程行已经"代表"了该物料的全部需求（且已扣 WIP），不该再让同名工单额外展开一次——所以"有排程行就跳过工单展开"，纯工单备货物料继续正常展开。

---

## 三、验证结果明细（38/38 PASS）

### 3.1 排程结果（peacock_schedule）
| 物料 | 校验项 | 实测值 | 期望值 | 结果 |
|---|---|---|---|---|
| VALFG01 | plan_qty | 80 | 80（100销售−20 WIP） | ✅ |
| VALSF01 | plan_qty | 100 | 100 | ✅ |
| VALFG01 | batch_date_start（开工日） | 20260906 | 20260906（交货−4天） | ✅ |
| VALFG01 | batch_date_end（交货日） | 20260910 | 20260910 | ✅ |
| VALSF01 | batch_date_end（锚点回填） | 20260906 | 20260906（=FG开工日） | ✅ |
| VALSF01 | batch_date_start（开工回写） | 20260827 | 20260827（20260906−10天） | ✅ |
| VALFG01 | ready_status（齐备状态） | C | C（有缺料） | ✅ |
| VALSF01 | ready_status（齐备状态） | B | B（仅部分） | ✅ |

> 提前期口径：`lead(mat,qty)=ceil(qty/daily)`（daily>0 时），否则取 `production_cycle`；每层只减自身提前期一次。半成品交货日空时由父件开工日反向锚点回填。

### 3.2 缺料明细 · 展示层（含在途，逐单耗用 + 批量规则）
| 组件 | 需求(gross) | 可用库存(含在途) | 计划采购(净) | 建议采购(批量) | 状态 | 结果 |
|---|---|---|---|---|---|---|
| VALSF01（半成品） | 80 | 0 | 80 | 80 | 缺料 | ✅ |
| VALRM01（原料C） | 160 | 50 | 110 | 110 | 部分 | ✅ |
| VALPK01（包装F） | 320 | 0 | 320 | 320 | 缺料 | ✅ |
| VALRM02（原料D） | 300 | 70 | 230 | 230 | 部分 | ✅ |
| VALRM03（原料E） | 100 | 5 | 95 | 95 | 部分 | ✅ |

### 3.3 在途列核对（含在途）
| 组件 | transit_qty | 说明 | 结果 |
|---|---|---|---|
| VALRM02 | 40 | 在途 PO 40，到货 2026-08-20 ≤ 组件需求日 | ✅ |

### 3.4 在途开关（不含在途）核对
| 组件 | 校验项 | 实测 | 期望 | 结果 |
|---|---|---|---|---|
| VALRM02 | 可用（仅库存，不含在途） | 30 | 30 | ✅ |
| VALRM02 | transit_qty 展示列（始终显示） | 40 | 40 | ✅ |

### 3.5 组件级 WIP 抵扣一致性（存储层 vs 展示层）
| 组件 | 存储层 required（已扣 WIP） | 展示层 required（gross） | 一致性 | 结果 |
|---|---|---|---|---|
| VALSF01 | 60 | 80 | 60 = 80 − 20 WIP | ✅ |
| VALRM02 | 290 | 300 | 290 = 300 − 10 WIP | ✅ |

> 存储层 `peacock_material_shortage` 的 `required_qty` 已扣除组件级 WIP；展示层（前端缺料弹窗）用 gross 重算、纯展示，二者口径不同属设计预期，数值闭环一致。

### 3.6 前置清理（一键计算附带）
清理 `创建`/`作废` 草稿与作废记录计数正确返回，确认性数据（启用/批准/执行/在途/关闭）不受影响。

### 3.7 优先级评分
公式评分在验证过程中复算一致（如 `VALSF01=60`、`VALFG01=7`），评分逻辑正确。

**汇总：验证项总数 = 38，通过 = 38，失败 = 0。**

---

## 四、测试数据清单（已按用户要求保留，不清理）

| 类别 | 编码 | 关键参数 |
|---|---|---|
| 成品 | VALFG01 | 产成品，日产量 20，销售 100 |
| 半成品 | VALSF01 | 半成品，日产量 10 |
| 原料 | VALRM01 | 周期 5 |
| 原料 | VALRM02 | 周期 3，库存 30，在途 PO 40 |
| 原料 | VALRM03 | 周期 2，最小采购 10 / 包装 5，库存 5，关闭 PO 100 |
| 包装 | VALPK01 | 周期 1，最小 50 / 包装 20 |
| BOM | VALFG01→VALSF01×1 / VALRM01×2 / VALPK01×4；VALSF01→VALRM02×3 / VALRM03×1 | |
| 销售 | VALSO01 | 交货 2026-09-10，数量 100（VALFG01） |
| 工单WIP | VALWO-FG（VALFG01，批准，20）/ VALWO-SF（VALSF01，执行，20）/ VALWO-RM2（VALRM02，批准，10） | |
| 采购 | VALPO-RM2（VALRM02，在途 40，到货 2026-08-20）/ VALPO-RM3（VALRM03，关闭 100） | |

> 用户可在系统中按 `VAL*` 前缀检索查看上述数据；脚本位于 `ApsSys_app/validate_calc.py`，可重复运行（每次自动 wipe+重植，结果稳定 38/38）。

---

## 五、部署与生效说明（重要）

- **修复已落盘**：改动写入 `backend/routes/schedule.py`（`schedule_set` 跳过逻辑 + 注释）。
- **当前 5000 端口实例（原生 Windows 进程 PID 2968）仍运行旧代码**：沙箱无法终止原生 Windows 进程，因此**用户需手动重启该后端**才能使修复在生产 URL（5000）生效：
  1. 在宿主机结束 PID 2968（任务管理器 / `Stop-Process -Id 2968 -Force`）；
  2. 重新启动后端（`backend/run.py`，监听 5000）。
- **已修复后端已在 5001 端口运行并实测通过**：可立即用于核对验证结果（返回 VALFG01/VALSF01 等正确数据）。
- **前端无关改动**：合并按钮、日期带出均为纯前端，浏览器 Ctrl+F5 强刷即可生效。

---
*报告生成：PMC 排程系统自动验证 · 38/38 PASS*
