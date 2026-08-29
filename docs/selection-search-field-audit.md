# 选品搜索字段审计与数据模型建议

> 审计范围：`selection-browser-bridge` 当前实现及首次 `keyword=skill`、第一页实测结果。  
> 审计日期：2026-08-22。  
> 本文只描述当前事实和后续数据设计建议，不代表已经修改数据库或选品业务。

## 1. 当前 selection search 返回结构

### 1.1 API

- 请求：`POST /api/internal/selection/search`
- 请求体：`{"keyword":"skill","page":1,"limit":30}`
- 认证：请求头 `X-Internal-Token`
- 当前仅支持第一页，最多返回 30 条。

成功响应结构：

```json
{
  "ok": true,
  "keyword": "skill",
  "page": 1,
  "source": {
    "request_url": "https://.../h5/mtop.taobao.idlemtopsearch.pc.search/1.0/...",
    "http_status": 200,
    "content_type": "application/json;charset=UTF-8",
    "ret": ["SUCCESS::调用成功"],
    "list_path": "data.resultList"
  },
  "total": 30,
  "items": []
}
```

说明：`total` 是本次成功标准化的条数，不是闲鱼平台的搜索结果总量。商品列表位于 API 响应的 `items`；原始闲鱼 JSON 的商品卡片位于 `data.resultList`。

### 1.2 当前标准化字段及原始来源

| 标准字段 | 原始字段/生成方式 |
|---|---|
| `item_id` | `data.resultList[].data.item.main.exContent.itemId` |
| `title` | `...exContent.title` |
| `price_text` | 拼接 `...exContent.price[].text`，移除“当前价” |
| `price` | 从 `price_text` 解析数值，兼容人民币符号、逗号和“万” |
| `want_count` | `...main.clickParam.args.wantNum` |
| `want_count_text` | `wantNum` 的文本形式 |
| `publish_time` | `...main.clickParam.args.publishTime` 转为本地 ISO 时间 |
| `url` | `...exContent.targetUrl`；必要时从 `item_id` 生成标准链接 |
| `image_url` | `...exContent.picUrl` |
| `region` | `...exContent.area` |
| `seller_nickname` | `...exContent.userNickName` |
| `raw_tags` | `clickParam.args.tag` 中的包邮标记，加 `fishTags.r1.tagList[].data.content` |

首次实测成功捕获闲鱼搜索接口 HTTP 200 JSON，在 `data.resultList` 得到 30 条并完成标准化。该次 30 条记录的 `want_count` 均显示为 0，这只能证明字段存在，不能证明它准确反映真实想要数，更不能当作销量。

## 2. 字段稳定性分析

| 字段 | 稳定存在 | 可能为空/异常 | 是否需二次解析 | 数据库建议 |
|---|---|---|---|---|
| `item_id` | 普通商品卡片通常稳定 | 广告、推荐模块或异常卡片可能缺失 | 无；入库前校验非空 | 必须，作为平台自然键 |
| `title` | 普通卡片通常存在 | 可能是很长的展示文案，未必等于简洁商品标题 | 后续可派生短标题，原文必须保留 | 必须，建议命名 `title_raw` |
| `price_text` | 多数商品存在 | 面议、区间价、活动价可能无法数值化 | 保留原文 | 建议保留，允许空 |
| `price` | 可解析数字时存在 | 非标准价格会是 `null`；浮点不适合长期存钱 | 入库转为分并记录解析状态 | 建议用 `price_cents INTEGER NULL` |
| `want_count` | 当前响应中字段存在 | 可能缺失、为字符串或恒为 0；真实性待验证 | 类型转换并区分“缺失”和 0 | 快照字段，允许空，不能当销量 |
| `publish_time` | 部分商品存在 | 可能缺失、格式变化或时间戳无效 | 需要解析并保留原值 | 快照/商品字段，允许空 |
| `url` | 通常可得到或由 ID 重建 | 深链可能带场景参数或失效 | 统一生成 canonical URL | 建议存标准链接，必须 |
| `image_url` | 多数卡片存在 | 可能为空、带临时参数或后续失效 | 暂不下载；保留原 URL | 可选字段 |
| `seller_nickname` | 多数普通卡片存在 | 可能为空且昵称可修改 | 不能替代 seller ID | 可选展示字段 |
| `region` | 常见但非必有 | 粒度不一，可能只是省市展示文字 | 可做轻量清洗 | 可选筛选字段 |
| `raw_tags` | 仅有标签时出现 | 空数组很正常；内容可能随 UI 变化 | 去重；需要查询时再拆表 | 建议 JSON/TEXT 保存 |

### 关键判断

- `item_id` 是当前最可靠的跨次观测关联键；无 ID 的卡片应拒绝入正式商品表或进入异常区。
- `title` 应保存搜索卡片原文，不能假定它总是“纯标题”。
- 金额不要长期保存为浮点数，应保存整数分和原始价格文字。
- `want_count` 缺失时不能填 0。首次实测全部为 0，必须经过重复观测或人工抽样核验后，才能用于热度评分。
- `seller_nickname` 不是卖家唯一标识；当前标准结果没有可靠 seller ID。
- `raw_tags` 是展示标签，不应作为稳定业务枚举。
- `source.request_url` 含时间戳、签名等易变参数，不建议完整长期入库；保存 API 名称、版本、状态码即可。

## 3. 对选品目标的支持程度

### 热度判断

当前可使用：`want_count`、`publish_time`、搜索排名、重复出现次数、价格。  
限制：现在没有真实销量、浏览量和评论数；`want_count` 的可靠性仍需验证。因此目前只能形成“搜索热度线索”，不能形成成交结论。

### 增长速度判断

单次搜索无法判断增长。需要定时重复搜索并保存每次快照：

```text
想要增速 = (本次想要数 - 上次想要数) / 间隔小时
```

只有两次数据都非空、字段语义确认可靠时才计算。还应保留搜索排名变化、价格变化和是否持续出现。派生值可动态计算，不应覆盖原始快照。

### 商品筛选

当前可按关键词、价格、发布时间、地区、标签、卖家昵称、搜索排名过滤。建议同时增加数据质量条件，例如排除 `item_id` 为空或价格解析失败的记录。

### 后续 AI 辅助分析

可提供给 AI 的证据包括：标题原文、价格、想要数、商品年龄、想要增速、搜索排名及变化、标签、地区和卖家展示名。AI 输出必须保留依据与缺失字段，不能让模型把“想要数”解释成“销量”。评论文本、成交数据和卖家信誉目前尚不存在。

## 4. 推荐数据模型

为了支持增长趋势，不能只建一张 `selection_items`。最小建议是“商品主表 + 搜索任务表 + 观测快照表”。

### 4.1 `selection_items`：商品身份及最新展示信息

| 字段 | 类型 | 用途 | 必须 |
|---|---|---|---|
| `item_id` | TEXT PRIMARY KEY | 闲鱼商品唯一标识 | 是 |
| `canonical_url` | TEXT | 可复用的标准商品链接 | 是 |
| `title_raw` | TEXT | 搜索卡片原始标题/展示文案 | 是 |
| `image_url` | TEXT NULL | 当前主图地址 | 否 |
| `seller_id` | TEXT NULL | 未来详情采集后的稳定卖家标识 | 否 |
| `seller_nickname` | TEXT NULL | 当前卖家展示名 | 否 |
| `region` | TEXT NULL | 展示地区 | 否 |
| `tags_json` | TEXT NULL | 当前标签数组 | 否 |
| `publish_time` | DATETIME NULL | 商品发布时间 | 否 |
| `first_seen_at` | DATETIME | 系统首次发现时间 | 是 |
| `last_seen_at` | DATETIME | 最近一次发现时间 | 是 |
| `created_at` | DATETIME | 本地记录创建时间 | 是 |
| `updated_at` | DATETIME | 本地记录更新时间 | 是 |

### 4.2 `selection_search_runs`：每次搜索运行

| 字段 | 类型 | 用途 | 必须 |
|---|---|---|---|
| `run_id` | TEXT PRIMARY KEY | 一次搜索的唯一编号 | 是 |
| `keyword` | TEXT | 搜索词 | 是 |
| `page` | INTEGER | 页码，当前固定 1 | 是 |
| `started_at` | DATETIME | 搜索开始时间 | 是 |
| `finished_at` | DATETIME NULL | 搜索结束时间 | 否 |
| `status` | TEXT | success/failed | 是 |
| `source_api` | TEXT NULL | 接口名称及版本 | 否 |
| `http_status` | INTEGER NULL | 上游状态码 | 否 |
| `result_count` | INTEGER | 本轮解析数量 | 是 |
| `error_code` | TEXT NULL | 失败诊断代码 | 否 |

### 4.3 `selection_item_snapshots`：商品随时间的观测值

| 字段 | 类型 | 用途 | 必须 |
|---|---|---|---|
| `snapshot_id` | INTEGER PRIMARY KEY | 快照编号 | 是 |
| `run_id` | TEXT | 对应搜索运行 | 是 |
| `item_id` | TEXT | 对应商品 | 是 |
| `observed_at` | DATETIME | 观测时间 | 是 |
| `rank_position` | INTEGER | 本关键词本页排名 | 是 |
| `price_cents` | INTEGER NULL | 可比较价格，单位分 | 否 |
| `price_text` | TEXT NULL | 原始价格展示 | 否 |
| `price_parse_status` | TEXT | parsed/missing/unparseable | 是 |
| `want_count` | INTEGER NULL | 当前想要数代理值 | 否 |
| `want_count_raw` | TEXT NULL | 原始值，便于追查格式变化 | 否 |
| `publish_time_raw` | TEXT NULL | 原始发布时间值 | 否 |
| `title_raw` | TEXT | 当次标题，保留改名历史 | 是 |
| `image_url` | TEXT NULL | 当次图片 | 否 |
| `seller_nickname` | TEXT NULL | 当次卖家展示名 | 否 |
| `region` | TEXT NULL | 当次地区 | 否 |
| `tags_json` | TEXT NULL | 当次标签 | 否 |

建议建立唯一约束 `UNIQUE(run_id, item_id)`。`age_hours`、`want_growth_24h`、`want_velocity`、`price_change`、`demand_score` 和 `ai_score` 应先作为查询或分析层派生结果，不要在第一阶段成为原始事实字段。

## 5. 当前缺少的关键数据

以下内容不在当前标准化搜索结果中，不能声称已经取得：

1. 真实销量、已售数量或成交次数。
2. 评论数、评论正文、评论时间和买家反馈倾向。
3. 浏览量、曝光量、点击率。
4. `wantNum` 到底代表想要、收藏还是某种展示指标的稳定语义验证。
5. 稳定的卖家 ID、卖家信用、在售数量、历史销量等卖家画像。
6. 平台类目、商品状态、库存、服务/交付类型。
7. 商品正文与“纯标题”的明确分离。
8. 商品更新时间、下架时间和重新上架关系。
9. 广告/付费推广/推荐位标识。
10. 首次被系统发现之前的价格和热度历史。

这些字段未来可能需要商品详情、卖家主页或其他接口，但不属于当前搜索桥 MVP。补采前应先验证平台稳定性和风险，再决定是否值得增加访问次数。

## 6. 推荐的下一步（仅建议，尚未实施）

下一阶段最有价值的不是立刻接 AI，而是先把现有第一页搜索结果按“运行 + 快照”保存，并连续观察同一批商品。优先完成：

1. 对 `item_id`、价格和想要数建立严格的空值/解析状态规则。
2. 入库时附加 `run_id`、`keyword`、`rank_position`、`observed_at`。
3. 用少量固定关键词低频重复采集，验证 `want_count` 是否真实变化。
4. 人工抽查若干商品页面，确认搜索卡片数值与页面展示是否一致。
5. 数据可信后再计算增速；最后才考虑详情补采、评论分析和 AI 评分。

结论：当前搜索桥足以建立“候选商品发现与搜索快照”的基础层，但不足以单独判断销量或成交潜力。数据库设计必须保存时间序列和缺失状态，避免把一次搜索中的 0 或空值误当成真实业务事实。
