# PMC 优先级打分公式（参考文档）

> 来源：`backend/routes/schedule.py` → `calculate_priority()`（第 916–1000 行）
> 该计算已包含在「一键计算」(`peacock_calculate`) 流程内，跑一键计算后自动得出。

---

## 一、总公式

```
优先级总分 priority_score = 齐备基础分(base_score) + 紧急度分(urgency_score) + 数量分(qty_score)
```

- 算完后按 `priority_score` **从大到小排序**（分越高越优先）。
- 结果写回 `peacock_schedule.overall_priority` 与 `suggest_order`。

---

## 二、分项公式

### 1. 齐备基础分 `base_score` —— 取自 `ready_status`

| ready_status | 含义 | 基础分 |
|---|---|---|
| `A` | 齐备 | **100** |
| `B` | 部分齐备 | **50** |
| `C` | 缺料 | **0** |
| 其它 / 空 | — | **0** |

### 2. 数量分 `qty_score` —— 取自净排产数量 `plan_qty`

| plan_qty 区间 | 数量分 |
|---|---|
| ≥ 1000 | **10** |
| ≥ 500 | **8** |
| ≥ 100 | **5** |
| < 100 | **2** |

### 3. 紧急度分 `urgency_score` —— 取自距交货日天数

**交货日取值优先级**：`finish_date` → 否则 `batch_date_end` → 否则 `batch_date_start`

```
days_left = 交货日 − 今天
```

| days_left（距今天数） | 紧急度分 |
|---|---|
| < 0（已逾期） | **30** |
| ≤ 3 | **25** |
| ≤ 7 | **20** |
| ≤ 14 | **15** |
| ≤ 30 | **10** |
| > 30 | **5** |
| 无交货日 | **0** |

---

## 三、建议下单标记 `suggest_order`

与分数无关，只看齐备状态：

```
suggest_order = 'YES'   当 ready_status ∈ {A, B}   （齐备 / 部分齐备 → 建议排产）
suggest_order = 'NO'    当 ready_status = C         （缺料 → 暂不排）
```

---

## 四、计算示例（验证数据反推，确凿证据）

### VALSF01（半成品）
| 项 | 值 | 得分 |
|---|---|---|
| ready_status = `B` | 部分齐备 | 基础分 **50** |
| plan_qty = 100 | ≥ 100 | 数量分 **5** |
| batch_date_end = 2026-09-06，today 2026-08-04 → days_left ≈ 33 | > 30 | 紧急度 **5** |
| **总分** | | **60** ✅ |

### VALFG01（成品）
| 项 | 值 | 得分 |
|---|---|---|
| ready_status = `C` | 缺料 | 基础分 **0** |
| plan_qty = 80 | < 100 | 数量分 **2** |
| batch_date_end = 2026-09-10 → days_left ≈ 37 | > 30 | 紧急度 **5** |
| **总分** | | **7** ✅ |

---

## 五、设计要点

1. **齐备状态主导**：`A` 比 `C` 多 100 分，基本锁定排序——齐备的永远排前面。
2. **逾期最优先**：紧急度封顶 30（逾期），远高于正常交货窗口。
3. **数量仅微调**：最大 10 分，远小于齐备的 100 分——数量不影响大局，只做同档内区分。
4. **一键计算已包含**：`peacock_calculate()` 内部调用 `calculate_priority()`，跑一键计算后优先级自动算好。

---

*文档由 PMC 验证流程生成，公式与 `schedule.py` 源码逐行核对一致。*
