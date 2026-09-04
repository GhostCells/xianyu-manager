# 自动爆品候选闭环 MVP

## 数据职责

- `selection_search_runs`：每个关键词搜索的运行结果。
- `selection_search_item_observations`：每次搜索命中证据；同一商品可保留多个关键词和多个 run。
- `selection_items`：按平台 `item_id` 去重后的商品主记录。
- `selection_item_snapshots`：成功或失败的详情观测原始证据；缺失互动字段保持 `NULL`。
- `selection_item_trends`：相邻两次成功详情快照的派生差分和单位小时增速。
- `selection_candidate_assessments`：商品最新的可解释爆品评估。
- `selection_item_tracking` / `selection_item_tracking_events`：采集追踪状态及不可变事件。
- `candidate_status` / `selection_item_reviews`：既有人工审核结论及不可变历史。
- `pipeline_status`：独立的自动候选生命周期。

## 趋势规则

首次观测只记录 `first_observation`，不推断增长。相邻成功快照按详情观测时间计算间隔；间隔小于等于零时不计算速率。浏览、想要、收藏分别计算原始差值和每小时增长。任一字段缺失时，该字段差值和速率保持 `NULL`。数值下降保留负差值并记录异常代码，但不把负值用于增长评分。

候选评估另从全部成功快照动态派生多次观测指标，不新增历史表或冗余计数字段：快照数、最近 3 次想要增量/平均速率/连续增长、最近 7 天想要增量/平均速率、最新与前一段速率及速率变化。证据不足时返回 `null` 并列入 `insufficient_data`。

## 持续追踪与详情预算

当前搜索负责发现新商品；`active` tracking 商品达到冷却时间后，即使未出现在当前搜索结果中，也会进入详情候选池。`selection-schedule.json` 的 `tracking_budget_ratio` 配置 tracking 保留预算，默认为 `0.4`；新发现和 tracking 任一池未用完的预算可由另一池借用。tracking 池优先考虑 `tracking_priority`、最新想要增速、已有成功快照数和超过冷却的时长。本阶段不自动转为 `paused` 或 `retired`。

## 评分规则

`hot-candidate-mvp-v1` 只在至少有一个有效增长速率时生成总分：

- 趋势信号占 60%：浏览每小时增长、想要每小时增长、收藏每小时增长。
- 当前信号占 40%：当前浏览、想要、收藏、标题相关性和价格适配。
- 总分达到 60 分进入 `hot_candidate`。

评分输出同时保存分项、权重、阈值、原因代码、原始趋势和未知字段。当前详情响应没有可靠发布时间，因此 `published_at` 和 `item_age_hours` 明确输出为 `null`，不参与 MVP 分数。真实销量和评论数不在模型中。

## 候选生命周期

`new_discovery → observing → hot_candidate → reviewed → production` 是主路径；另有 `rejected` 和 `stopped`。人工终态不会被后续自动评分覆盖。既有 `candidate_status` 继续表示人工审核反馈，避免迁移时改变历史语义。

## Operation 出口

`GET /api/operation/selection-candidates?limit=50` 仅返回当前 `hot_candidate`，并按总分降序排列。同一商品不因多个关键词生成重复行，来源关键词以数组返回。

`GET /api/operation/selection-tracking/{item_id}` 返回商品、tracking 状态、成功快照原始证据和上述多次观测派生指标，用于 Debug / diagnostics。

## 生产边界

本实现可用临时 SQLite 和 mock/fake 完整测试。真实搜索仍只能复用 `xianyu-manager` 唯一 Persistent Context Owner 创建的 Selection Page；出现平台验证时立即停止，不重试、不绕过。Mac 不运行真实 scheduler 或真实闲鱼连接。
