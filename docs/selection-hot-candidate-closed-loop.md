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

## 生产边界

本实现可用临时 SQLite 和 mock/fake 完整测试。真实搜索仍只能复用 `xianyu-manager` 唯一 Persistent Context Owner 创建的 Selection Page；出现平台验证时立即停止，不重试、不绕过。Mac 不运行真实 scheduler 或真实闲鱼连接。
