# 闲鱼候选商品人工审核反馈 MVP 设计

版本：v1.0  
日期：2026-08-22  
状态：仅设计，尚未开发

## 1. 目标与边界

当前选品系统已经具备：

```text
关键词采集 → 搜索结果入库 → 详情快照 → 数据观察日报
```

人工审核反馈 MVP 的目标是：

> 让人工针对候选商品记录判断结果，并保留判断过程，为后续验证选品规则是否有效提供事实依据。

本阶段只记录人工结论，不自动判断商品，不改变采集策略。

明确边界：

- 不接入 AI。
- 不实现自动评分。
- 不修改搜索、详情补采或调度流程。
- 不根据互动数自动改变候选状态。
- 不自动制作、上架或复制商品。
- 不把“值得测试”解释为一定能成交。
- 不把“已成交”解释为同行商品销量；它只表示自己的测试商品出现了经人工确认的成交。
- 原始搜索运行和商品快照不可被审核操作覆盖或删除。

## 2. 状态模型

### 2.1 推荐状态

数据库内部使用稳定的英文枚举，界面和命令行展示中文：

| 内部值 | 中文 | 含义 |
|---|---|---|
| `unreviewed` | 待审核 | 系统已发现，但人工尚未判断 |
| `worth_testing` | 值得测试 | 人工认为值得进一步制作或上架测试 |
| `not_worth_testing` | 不值得测试 | 人工认为当前不适合继续投入 |
| `tested` | 已测试 | 已经实际制作、上架或进行过明确的市场测试 |
| `converted` | 已成交 | 自己的测试商品已出现至少一笔人工确认的真实成交 |

用户要求的四个可操作状态为：

```text
值得测试 / 不值得测试 / 已测试 / 已成交
```

`unreviewed` 是系统默认状态，不需要人工主动设置，但允许人工将误操作恢复为待审核。

### 2.2 状态不是评分

候选状态是人工工作流状态，不是 0～100 分，也不是商品质量标签：

- `worth_testing` 表示值得投入一次验证，不代表必然能卖。
- `not_worth_testing` 表示在当前条件下暂不投入，不代表商品永久无价值。
- `tested` 表示发生了测试动作，不代表测试结果良好。
- `converted` 表示自己确认出现成交，不代表持续盈利或可规模化。

### 2.3 推荐状态流转

正常路径：

```text
unreviewed
├─→ worth_testing
│   └─→ tested
│       └─→ converted
└─→ not_worth_testing
```

允许人工纠错和重新判断：

- `not_worth_testing → worth_testing`
- `worth_testing → not_worth_testing`
- 任意状态 → `unreviewed`，但必须填写原因
- `converted → tested` 只用于撤销误标，必须填写原因

第一版不强制阻止跨级操作，例如可以直接将商品标记为 `tested`。原因是现有商品可能在功能上线前已经完成测试。所有跨级操作必须进入审核历史。

## 3. 数据模型建议

推荐采用：

```text
selection_items 保存当前状态
+
selection_item_reviews 保存不可变审核历史
```

只在审核记录表中保存历史、原因和备注；不要把这些信息塞入商品快照，也不要使用 `selection_scores`。

## 4. `selection_items` 建议新增字段

| 字段 | 类型 | 默认值 | 用途 |
|---|---|---|---|
| `candidate_status` | TEXT NOT NULL | `unreviewed` | 当前人工审核状态 |
| `candidate_status_updated_at` | TEXT NULL | `NULL` | 最近一次人工状态更新时间 |
| `last_review_id` | INTEGER NULL | `NULL` | 指向最近一次审核记录，便于查询 |

建议约束：

```sql
CHECK (
  candidate_status IN (
    'unreviewed',
    'worth_testing',
    'not_worth_testing',
    'tested',
    'converted'
  )
)
```

这三个字段只用于快速获取当前状态。完整变更历史以审核记录表为准。

不建议只在 `selection_items` 增加一个状态字段而不保存历史，因为无法回答：

- 谁在什么时候改变了判断。
- 为什么从值得测试改为不值得测试。
- 商品测试过几次。
- 是否发生过误标和撤销。
- 未来规则与人工判断是否一致。

## 5. 审核记录表

建议新增：`selection_item_reviews`

| 字段 | 类型 | 必须 | 用途 |
|---|---|---:|---|
| `review_id` | INTEGER PRIMARY KEY AUTOINCREMENT | 是 | 审核记录编号 |
| `item_id` | TEXT REFERENCES selection_items(item_id) | 是 | 被审核商品 |
| `previous_status` | TEXT | 是 | 变更前状态 |
| `new_status` | TEXT | 是 | 变更后状态 |
| `reason_code` | TEXT NULL | 否 | 结构化原因 |
| `note` | TEXT NULL | 否 | 人工补充说明 |
| `source` | TEXT NOT NULL DEFAULT `manual_cli` | 是 | 操作来源 |
| `reviewed_by` | TEXT NOT NULL DEFAULT `owner` | 是 | 操作者，单账号 MVP 固定 owner |
| `reviewed_at` | TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP | 是 | 审核时间 |
| `related_snapshot_id` | INTEGER NULL | 否 | 审核时参考的快照 |
| `related_search_run_id` | TEXT NULL | 否 | 审核时参考的搜索批次 |
| `metadata_json` | TEXT NOT NULL DEFAULT `{}` | 是 | 少量扩展信息，不放敏感数据 |

索引建议：

```sql
CREATE INDEX idx_selection_reviews_item_time
ON selection_item_reviews(item_id, reviewed_at DESC);

CREATE INDEX idx_selection_reviews_status_time
ON selection_item_reviews(new_status, reviewed_at DESC);
```

### 5.1 不可变原则

审核记录一旦创建，不直接 UPDATE 或 DELETE。

如果操作错误：

```text
新增一条纠正记录
→ 更新 selection_items 当前状态
→ 保留原错误记录
```

这样才能完整还原人工决策路径。

### 5.2 事务一致性

一次人工状态变更必须在同一个数据库事务中完成：

```text
读取当前状态
→ 插入 selection_item_reviews
→ 更新 selection_items.candidate_status
→ 更新 last_review_id 和时间
→ 提交事务
```

任何一步失败则整体回滚，避免“主表状态已变但没有审核历史”。

## 6. 原因代码建议

MVP 推荐原因代码：

### 6.1 值得测试

| reason_code | 中文说明 |
|---|---|
| `clear_demand` | 用户需求清楚 |
| `good_interaction` | 互动表现值得观察 |
| `good_price_band` | 价格带适合测试 |
| `deliverable` | 当前能力能够稳定交付 |
| `low_support_cost` | 售后成本预计可控 |
| `portfolio_fit` | 与现有商品组合匹配 |

### 6.2 不值得测试

| reason_code | 中文说明 |
|---|---|
| `weak_demand` | 需求表现较弱 |
| `irrelevant` | 与经营方向无关 |
| `copyright_risk` | 版权或授权风险 |
| `platform_risk` | 平台发布风险 |
| `delivery_difficulty` | 难以稳定交付 |
| `high_support_cost` | 售后成本过高 |
| `price_too_low` | 价格竞争过低 |
| `technical_gap` | 当前技术能力不足 |
| `duplicate_product` | 与现有商品重复 |

### 6.3 已测试和已成交

| reason_code | 中文说明 |
|---|---|
| `listing_published` | 已上架测试 |
| `manual_trial` | 已人工完成产品测试 |
| `first_real_order` | 已确认第一笔真实订单 |
| `repeat_order` | 已出现重复成交 |
| `correction` | 纠正之前的状态操作 |

原因代码用于统计，不代替备注。暂时不允许 AI 自动生成或选择原因。

## 7. 成交状态边界

`converted` 必须由人工确认，第一版不从订单表自动推断。

推荐确认条件：

1. 候选商品已经转化为自己的商品。
2. 自己的商品出现真实买家支付或完成交易。
3. 排除人为补单、测试订单、退款订单和内部交易。
4. 审核备注可填写自己的商品名称或本地商品 ID，但不保存买家隐私。

未来如需与自有商品关联，可以新增独立映射表，不建议直接把同行 `item_id` 当作自己的商品 ID。

## 8. 命令行操作方式

建议新增独立脚本：

```text
scripts/review_selection_item.py
```

该脚本只操作人工审核字段和审核记录表，不调用浏览器、不访问网络、不触发采集。

### 8.1 查看待审核商品

```powershell
python scripts/review_selection_item.py list --status unreviewed --limit 20
```

建议输出：

- `item_id`
- 标题摘要
- 商品链接
- 最近价格
- 最近 `want_count`
- 最近 `browse_count`
- 最近 `collect_count`
- 最近采集时间
- 当前状态

### 8.2 标记值得测试

```powershell
python scripts/review_selection_item.py set 1070581150308 worth_testing `
  --reason clear_demand `
  --note "财务岗位需求明确，可先做低价版本测试"
```

### 8.3 标记不值得测试

```powershell
python scripts/review_selection_item.py set 1070581150308 not_worth_testing `
  --reason high_support_cost `
  --note "安装环境差异大，预计售后较重"
```

### 8.4 标记已测试

```powershell
python scripts/review_selection_item.py set 1070581150308 tested `
  --reason listing_published `
  --note "已制作并上架测试"
```

### 8.5 标记已成交

```powershell
python scripts/review_selection_item.py set 1070581150308 converted `
  --reason first_real_order `
  --note "已确认真实买家成交，排除补单"
```

### 8.6 查看商品审核历史

```powershell
python scripts/review_selection_item.py history 1070581150308
```

### 8.7 恢复待审核

```powershell
python scripts/review_selection_item.py set 1070581150308 unreviewed `
  --reason correction `
  --note "上次状态选择错误，恢复待审核"
```

恢复和撤销必须填写 `--note`。

## 9. 命令行校验规则

执行状态变更前必须验证：

1. `item_id` 在 `selection_items` 中存在。
2. 新状态属于允许枚举。
3. `reason_code` 与目标状态兼容。
4. `note` 不超过建议长度，例如 500 字。
5. 新状态与当前状态相同时默认拒绝，避免重复历史记录。
6. 恢复 `unreviewed` 或从 `converted` 回退必须填写备注。
7. 操作前显示商品标题和当前状态。
8. 支持 `--yes` 非交互确认，但默认要求人工确认。

任何校验失败不得修改数据库。

## 10. 日报中的反馈展示建议

不改变现有采集统计口径，只在后续日报迭代中增加独立人工反馈区：

```text
待审核：12
值得测试：3
不值得测试：7
已测试：2
已成交：1
```

建议增加：

- 当日新增审核数。
- 各状态累计商品数。
- 值得测试 → 已测试的数量。
- 已测试 → 已成交的数量。
- 不值得测试原因分布。

这些数字属于人工工作流反馈，不进入采集成功率，也不修改历史互动指标。

在尚未积累足够样本前，不展示“转化率”结论。若未来计算，也必须说明分母、时间窗口，并排除补单、退款和测试订单。

## 11. 与现有模块的隔离

人工审核模块只依赖现有数据库：

```text
selection_items
selection_item_snapshots（只读，用于展示最近数据）
selection_item_reviews（新增、写入）
```

禁止人工审核命令：

- 启动或关闭 Chrome。
- 创建 Selection Page 或 Detail Page。
- 修改关键词或采集调度配置。
- 重跑搜索或详情补采。
- 写入 `selection_scores`。
- 修改自动回复、自动发货或商品数据库。
- 删除商品身份或历史快照。

## 12. MVP 最小实现范围

后续开发时只实现：

1. 为 `selection_items` 增加当前状态字段。
2. 创建 `selection_item_reviews` 审核历史表。
3. 数据库层的事务式状态变更方法。
4. `list`、`set`、`history` 三个命令行操作。
5. 状态、原因和备注校验。
6. 针对状态变更和历史保留的单元测试。

暂不实现：

- Web 前端审核页面。
- AI 自动审核。
- 自动评分。
- 从订单自动标记成交。
- 批量修改候选状态。
- 自动停用或删除候选商品。
- 根据人工状态改变采集频率。

## 13. 后续可积累的验证数据

人工审核记录稳定后，可以回答：

- 哪些关键词更容易产生“值得测试”的商品。
- 哪些人工否决原因最常见。
- 值得测试的商品中有多少真正进入测试。
- 已测试商品中有多少出现真实成交。
- 未来评分结果与人工判断是否一致。

这些分析必须基于人工记录事实，不应反向修改原始快照或审核历史。

## 14. 结论

人工审核反馈 MVP 应采用“商品当前状态 + 不可变审核历史”的双层结构。当前状态用于快速筛选，审核记录用于解释每次人工判断和纠错。

第一版只提供命令行操作，让人工能够把候选依次标记为值得测试、不值得测试、已测试或已成交。该模块不参与采集、不调用 AI、不产生评分，也不自动改变任何经营动作。
