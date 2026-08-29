# 闲鱼选品数据采集 MVP 设计

版本：v1.0  
日期：2026-08-22  
状态：设计确认，尚未实现

## 1. 采集目标

### 1.1 当前目标

数据采集 MVP 的唯一核心目标是：

> **每天稳定采集一批可追踪、可复查的闲鱼候选商品数据。**

系统通过已有的 selection browser bridge 复用 xianyu-manager 持有的唯一 Persistent Context：先执行关键词搜索，再对少量候选商品补充详情数据，最后把搜索批次和商品快照保存到选品系统自己的数据库中。

第一阶段关注数据链路稳定性、数据真实性和失败可诊断性，不追求采集规模。

### 1.2 采集结果

每天应至少形成：

- 每个启用关键词的一次搜索运行记录。
- 搜索第一页的标准化商品列表。
- 去重后的商品身份记录。
- 进入详情补充队列的少量商品。
- 详情页返回的想要数、浏览量和收藏数。
- 每个商品本次观测的快照。
- 所有跳过、失败、登录校验和平台验证状态。

### 1.3 MVP 边界

- 不开发评分代码。
- 不接入 AI。
- 不开发前端页面。
- 不自动决定商品是否值得做。
- 不采集评论、卖家主页或更多详情数据。
- 不修改 xianyu-manager 的自动回复、自动发货和消息监听逻辑。
- 不创建第二套浏览器、BrowserContext 或闲鱼登录会话。
- 不绕过登录、验证码或平台验证。

## 2. 关键词管理方案

### 2.1 关键词来源

MVP 使用人工维护的关键词清单，不自动扩词。初始方向可包括：

- Skill
- AI 工具
- Python 自动化
- ComfyUI
- Agent
- AI 工作流
- 效率工具
- 数字商品

初期建议只启用 3～5 个最重要的关键词。先验证连续采集稳定性，再逐步增加。

### 2.2 关键词数据结构

如果项目已有关键词任务表，应优先复用。最低需要记录：

| 字段 | 类型 | 用途 |
|---|---|---|
| `keyword_id` | INTEGER/TEXT | 关键词唯一标识 |
| `keyword` | TEXT | 实际搜索文字 |
| `enabled` | BOOLEAN | 是否参与每日任务 |
| `priority` | INTEGER | 执行顺序，数值越小越优先 |
| `detail_limit` | INTEGER | 本关键词每天最多补充详情数 |
| `last_run_at` | DATETIME NULL | 最近一次执行时间 |
| `next_run_at` | DATETIME NULL | 下次计划时间 |
| `last_status` | TEXT NULL | 最近一次状态 |
| `consecutive_failures` | INTEGER | 连续失败次数 |
| `created_at` | DATETIME | 创建时间 |
| `updated_at` | DATETIME | 更新时间 |

关键词不建议直接删除。停止使用时设置 `enabled=false`，保留历史运行和快照的关联关系。

### 2.3 关键词规则

- 关键词去除首尾空格。
- 使用规范化值避免 `Skill`、`skill`、` skill ` 被视为三个任务。
- 原始展示文字可以保留，但建立唯一规范键，例如小写后的 `keyword_normalized`。
- 同一关键词同一计划周期最多创建一个有效运行。
- MVP 不自动生成同义词，不进行组合词爆炸。

## 3. 搜索任务流程

### 3.1 前置检查

每次采集开始前检查：

1. xianyu-manager 本机健康接口可访问。
2. BrowserSessionManager 已持有可用 Persistent Context。
3. `status=bound`。
4. `message_ready=true`。
5. `delivery_status=listening`。
6. 内部 selection bridge 密钥可用。
7. 当前没有正在运行的选品搜索任务。

任何前置检查失败，本轮任务记录为 `blocked` 或 `failed`，不得把结果保存成零商品，也不得自动启动第二个 Chrome。

### 3.2 单关键词搜索

```text
读取下一个启用关键词
→ 创建 selection_search_runs 记录，状态 running
→ 调用 POST /api/internal/selection/search
→ selection bridge 获取独占锁
→ 在现有 Context 创建 Selection Page
→ 搜索关键词第一页
→ 捕获搜索 JSON
→ 标准化商品
→ finally 关闭 Selection Page
→ 返回结果
→ 更新运行状态和结果数量
```

当前只采集第一页：

```json
{
  "keyword": "skill",
  "page": 1,
  "limit": 30
}
```

搜索成功必须同时满足：

- HTTP 调用成功。
- 上游接口状态成功。
- JSON 中存在 `data.resultList`。
- bridge 返回 `ok=true`。
- 结果结构可解析。

`items=[]` 只能表示“成功搜索但结果为空”，登录失败、验证码、超时和接口异常不能归类为零结果。

### 3.3 串行执行

MVP 所有关键词严格串行：

```text
关键词 A 搜索及详情补充完成
→ 等待安全间隔
→ 关键词 B
```

不进行多关键词并发，不让两个任务同时操作同一账号 Context。selection bridge 的 `_selection_lock` 继续作为浏览器层的最后一道保护。

## 4. 详情页补充流程

### 4.1 进入详情队列的条件

搜索结果先做低成本规则过滤，再决定是否访问详情页。MVP 可使用：

- `item_id`、标题和 URL 完整。
- 标题与目标方向存在基本关键词命中。
- 不是已人工永久排除的商品。
- 本轮尚未成功采集详情。
- 没有超过本关键词的 `detail_limit`。

此阶段只用于控制访问量，不计算选品评分。

建议每个关键词每天最多访问 10～20 个详情页。多个关键词命中同一商品时，本轮只补充一次详情。

### 4.2 Detail Page 生命周期

详情补充应沿用已验证的隔离方式：

```text
取得现有 Persistent Context
→ context.new_page() 创建独立 Detail Page
→ 在该 Page 上安装 response listener
→ 打开标准商品 URL
→ 捕获 mtop.taobao.idle.pc.detail/1.0
→ 解析指定字段
→ finally 只关闭本次 Detail Page
```

禁止：

- 导航或关闭主消息 Page。
- 关闭 Browser、Context 或 Playwright。
- 清理 Cookie、LocalStorage 或 Cache。
- 使用 Context 级全局 response listener 或 route。
- 自动处理登录、验证码和 Baxia。

### 4.3 详情字段

MVP 只解析已经确认的：

| 字段 | 原始路径 |
|---|---|
| `want_count` | `data.itemDO.wantCnt` |
| `browse_count` | `data.itemDO.browseCnt` |
| `collect_count` | `data.itemDO.collectCnt` |

不得使用以下字段替代：

- 搜索页 `wantNum`。
- `b2cItemDO.wantBuyCount` 或 `b2cItemDO.browseCnt`。
- 当前登录账号的 `buyerDO.favored/isCollected`。
- 卖家级 `hasSoldNumInteger` 或导航页 `soldCount`。

解析成功后保存字段原始数值；字段不存在时保存 `NULL`，不能填 0。

## 5. 商品去重策略

### 5.1 永久身份去重

使用 `item_id` 作为商品自然键：

```text
selection_items.item_id PRIMARY KEY
```

同一 `item_id` 再次出现时更新 `last_seen_at` 和当前展示信息，不创建第二条商品身份记录。

### 5.2 单次运行去重

同一关键词、同一运行中，保留每个 `item_id` 的第一次有效出现，并记录最靠前的搜索排名。

建议唯一约束：

```text
UNIQUE(run_id, item_id, keyword)
```

### 5.3 跨关键词去重

同一商品当天可能被多个关键词搜索到：

- 每个关键词仍保存各自的搜索排名和命中关系。
- 商品主表只保存一条。
- 详情补充在当日任务范围内只执行一次。
- 后续关键词可以复用当天已经成功取得的详情值，但应保留各自搜索快照关系。

建议使用内存或任务级集合：

```text
detail_fetched_item_ids
```

如果任务中断，仍应通过数据库查询当日成功详情快照，避免重启后重复访问。

### 5.4 URL 与标题不能作为唯一键

- URL 可能携带场景参数，入库前统一为 canonical URL。
- 标题可能修改，也可能混合详情描述。
- 卖家可能重新发布类似商品。

因此 URL 仅作访问字段，标题仅作展示和筛选字段，均不替代 `item_id`。

## 6. 快照保存策略

### 6.1 先记录运行，再记录数据

采集开始时立即创建 `selection_search_runs`。这样即使进程中断，也能区分“没有执行”和“执行失败”。

建议状态：

```text
pending
running
success
partial
failed
blocked
```

### 6.2 搜索快照

搜索成功后先保存每个商品的搜索字段：

- `run_id`
- `item_id`
- `keyword`
- `observed_at`
- `search_rank`
- `title_raw`
- `price_cents`
- `price_text`
- `price_parse_status`

即使商品未进入详情队列，搜索快照也应保留。

### 6.3 详情快照

详情成功后，在对应快照补充或关联：

- `want_count`
- `browse_count`
- `collect_count`
- `detail_status=success`
- 详情响应时间

详情跳过或失败时：

```text
want_count=NULL
browse_count=NULL
collect_count=NULL
detail_status=skipped/failed/verification_required
detail_error_code=<明确代码>
```

### 6.4 原始响应原则

- MVP 不保存完整搜索或详情响应。
- 保存原始字段值、来源接口版本和解析状态。
- 诊断日志不得包含 Cookie、token、签名、手机号或完整敏感请求 URL。
- 如需定位接口变化，仅保存安全的结构摘要和错误码。

### 6.5 写入一致性

建议以单个搜索运行作为逻辑事务边界：

- 商品主表采用 upsert。
- 快照使用唯一约束避免重复写入。
- 详情失败不回滚已经成功保存的搜索快照。
- 部分详情失败时运行状态为 `partial`，不是 `failed`。

## 7. 失败重试策略

### 7.1 基本原则

- 重试必须有限、可诊断。
- 平台验证类错误绝不自动重试。
- 不在一次任务中连续刷新页面。
- 不因零结果自动重试。
- 不杀浏览器进程，不创建第二套会话。

### 7.2 错误分类

| 错误 | 当次处理 | 自动重试 |
|---|---|---:|
| `LOGIN_REQUIRED` | 停止当日后续闲鱼采集，提示人工登录 | 否 |
| `VERIFICATION_REQUIRED` | 停止当日后续采集，等待人工处理 | 否 |
| `SELECTION_BUSY` | 本次延后，不抢占现有任务 | 可在当天稍后重试 1 次 |
| `BROWSER_NOT_RUNNING` | 停止，提示先恢复 manager | 否 |
| 导航/响应超时 | 记录失败并关闭本次 Page | 可延后重试 1 次 |
| HTTP 5xx/临时网络错误 | 记录具体状态 | 可延后重试 1 次 |
| JSON 结构变化 | 保存安全结构摘要并停止 | 否 |
| 单个详情失败 | 保存 NULL 和错误状态，继续下一个 | 默认不立即重试 |
| 空结果且接口成功 | 保存成功零结果 | 否 |

### 7.3 重试间隔

MVP 不做秒级指数重试。建议：

- `SELECTION_BUSY`：延后 10～30 分钟重试一次。
- 临时网络或超时：延后 30～60 分钟重试一次。
- 单个详情失败：下一次日常任务再尝试。
- 连续 3 天失败的关键词自动暂停，并提示人工检查。

重试必须复用原计划运行的业务日期，并创建独立 attempt 记录或明确记录尝试次数，避免重复快照含义不清。

## 8. 调度频率建议

### 8.1 MVP 初始频率

建议每天执行一次，安排在消息和订单较少的固定时段，例如：

```text
每天 09:30 开始
```

具体时间可以根据实际客服高峰调整。目标是稳定，而不是抢分钟级热点。

### 8.2 关键词间隔

建议：

- 所有关键词串行。
- 每个关键词搜索结束后等待 30～90 秒再进入下一个。
- 详情页之间设置 5～15 秒间隔，并加入少量随机浮动。
- 遇到登录或验证立即停止，不继续当日剩余任务。

间隔的目的不是规避平台机制，而是限制自身访问频率、减少对主业务和账号稳定性的影响。

### 8.3 历史增长采样

每天一次只能计算日级变化，适合 MVP。稳定运行后，如果确实需要更细增长曲线，再评估每天两次；在验证账号和消息监听稳定前，不建议提高频率。

## 9. 数据生命周期

### 9.1 长期保留

建议长期保留：

- `selection_items` 商品身份。
- `selection_search_runs` 运行记录。
- `selection_item_snapshots` 数值快照。
- 失败状态和安全诊断代码。

这些数据体积较小，是趋势分析和问题追踪的依据。

### 9.2 不长期保存

- 完整上游 JSON 响应。
- Cookie、token、签名和带敏感参数的完整请求 URL。
- 页面 HTML。
- 临时截图，除非人工明确要求用于故障诊断。

### 9.3 快照保留建议

MVP 可先全部保留。数据量增长后再采用：

- 最近 90 天保留每日完整快照。
- 90 天以前按周聚合，但不要在验证分析需求前提前删除原始快照。
- 运行错误日志保留 90～180 天。
- 商品下架后保留身份和历史快照，标记状态，不物理删除。

### 9.4 数据修正

- 原始快照原则上不可覆盖。
- 解析规则修正后，应创建新解析版本或重新生成派生数据。
- 后续评分变化不能修改历史采集事实。
- 删除关键词不应级联删除历史运行和商品快照。

## 10. MVP 阶段必须实现的最小功能

第一阶段只需要以下能力：

1. 人工维护关键词的启用、停用和优先级。
2. 每天一次的串行任务入口，也可以先采用手动触发验证。
3. 调用现有 selection browser bridge 搜索关键词第一页。
4. 创建并更新搜索运行记录。
5. 按 `item_id` 写入或更新商品身份。
6. 保存关键词、搜索排名、标题和价格搜索快照。
7. 对每个关键词限定数量的商品补充详情。
8. 使用独立 Detail Page 获取 `wantCnt`、`browseCnt`、`collectCnt`。
9. 同一商品在当日跨关键词详情去重。
10. 正确区分 `NULL`、真实 0、跳过和失败。
11. 记录登录、验证、超时、接口变化等明确错误状态。
12. 所有任务保持串行，并在任何出口只关闭自己的 Selection/Detail Page。
13. 采集前后只读确认 manager 的 `status=bound`、`message_ready=true`、`delivery_status=listening`。
14. 提供最基本的命令行或日志汇总：本日关键词数、搜索成功数、商品数、详情成功数、失败原因。

以下功能不属于采集 MVP：

- 评分计算和候选分层实现。
- AI 分析。
- 前端管理页面。
- 评论、卖家主页和单品销量采集。
- 多页搜索。
- 多账号支持。
- 并发采集。
- 自动制作或上架商品。

## 结论

数据采集 MVP 应先建立一个小规模、低频、串行、可恢复的每日数据链路。搜索页负责发现商品，详情页只补充已确认的想要数、浏览量和收藏数，数据库保存运行事实及时间快照。任何未采集或失败字段必须保存为 `NULL` 并附带状态，不得伪装成零。只有连续多日稳定获得可信快照后，才进入评分实现和增长分析阶段。
