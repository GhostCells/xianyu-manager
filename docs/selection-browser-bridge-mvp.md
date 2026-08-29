# xianyu-manager 选品浏览器桥 MVP 开发记录

更新：2026-08-22  
状态：manager 侧最小闭环已实现并完成一次真实搜索验证；尚未接入 ai-goofish-monitor 正式流程。

## 1. 当前新增模块

### `src/xianyu_manager/selection_bridge.py`

- 构造关键词搜索 URL。
- 仅监听 Selection Page 自己的 XHR/fetch response。
- 识别 `mtop.taobao.idlemtopsearch` 搜索响应。
- 检查可见的登录提示、Baxia和验证码信号，不处理或绕过验证。
- 从 `data.resultList` 提取并标准化商品。
- 定义选品桥错误码和失败阶段。

### `BrowserSessionManager.search_listings()`

- 新增独立 `_selection_lock`。
- 只取得 manager 已持有且存活的 Persistent Context。
- Context 不存在时返回 `BROWSER_NOT_RUNNING`，不会启动 Chrome。
- 使用 `context.new_page()` 创建本次任务专属 Page。
- 所有出口在 `finally` 中只关闭该 Selection Page。

### 内部 HTTP API

- `POST /api/internal/selection/search`
- 只允许 localhost 调用。
- 使用 `X-Internal-Token` 校验调用方。
- 本地随机密钥位于 `data/selection-bridge-token.txt`，不进入业务数据库。

## 2. API 定义

请求：

```http
POST http://127.0.0.1:8765/api/internal/selection/search
Content-Type: application/json
X-Internal-Token: <本机密钥>
```

```json
{"keyword":"skill"}
```

当前仅允许第一页；`keyword` 长度1～40，`limit` 默认30且最大30。

成功响应：

```json
{
  "ok": true,
  "keyword": "skill",
  "page": 1,
  "source": {
    "request_url": "搜索接口URL",
    "http_status": 200,
    "content_type": "application/json;charset=UTF-8",
    "ret": ["SUCCESS::调用成功"],
    "list_path": "data.resultList"
  },
  "total": 30,
  "items": []
}
```

主要错误：

| 错误码 | 含义 | HTTP |
|---|---|---:|
| `SELECTION_BUSY` | 已有搜索任务 | 409 |
| `LOGIN_REQUIRED` | 需要重新登录 | 409 |
| `VERIFICATION_REQUIRED` | 需要人工完成平台验证 | 409 |
| `BROWSER_NOT_RUNNING` | manager 未持有可用 Context | 502 |
| `NAVIGATION_TIMEOUT` | 导航失败或超时 | 504 |
| `SEARCH_RESPONSE_TIMEOUT` | 搜索响应超时 | 504 |
| `SEARCH_TIMEOUT` | 总任务超过45秒 | 504 |

## 3. 浏览器生命周期

```text
等待 selection_lock（最多2秒）
→ 检查现有 Context 的账号归属和存活状态
→ context.new_page()
→ 导航前安装 Page 级 response listener
→ 搜索页导航（最多20秒）
→ 搜索响应（最多30秒）
→ 标准化结果
→ finally 关闭本次 Selection Page
→ 释放 selection_lock
```

整个任务最多45秒。任何成功、异常、超时或验证状态都会进入 `finally`。

## 4. Selection Page 与主 Page 隔离

- Selection Page 仅保存在局部变量中，不赋值给 `_page`。
- 不读取、导航或关闭原消息 Page。
- 不使用 `context.pages[0]` 作为 Selection Page。
- 不关闭 Context、Browser 或 Playwright。
- 不清 Cookie、LocalStorage、SessionStorage或Cache。
- 不修改 storage state。
- 不使用 `context.route()` 或 Context 级 response listener。
- 不操作消息 WebSocket、自动回复和自动发货服务。

两类 Page 共享账号会话但页面导航隔离；账号层面的请求频率仍共享，因此后续必须保持低频和串行。

## 5. 当前搜索返回字段

| 字段 | 说明 |
|---|---|
| `item_id` | 商品ID |
| `title` | 商品标题/搜索展示文本 |
| `price` | 数字价格，无法识别时为null |
| `price_text` | 展示价格文本 |
| `want_count` | 搜索接口返回的想要数 |
| `want_count_text` | 想要数文本 |
| `publish_time` | 本地ISO时间，缺失时为空 |
| `url` | 标准化商品链接 |
| `image_url` | 搜索结果主图 |
| `region` | 展示地区 |
| `seller_nickname` | 卖家昵称 |
| `raw_tags` | 搜索结果标签 |

字段缺失时返回空值，不访问详情页补充，也不推测数据。

## 6. 已验证功能

2026-08-22 使用关键词 `skill` 完成一次真实测试：

- API返回HTTP 200、`application/json`。
- 命中 `mtop.taobao.idlemtopsearch.pc.search/1.0`，接口HTTP 200。
- `ret` 为 `SUCCESS::调用成功`。
- 商品列表路径为 `data.resultList`，成功标准化30条。
- Selection Page 创建和回收成功。
- 原主 Page 仍位于 `https://www.goofish.com/im`。
- 测试后 `message_ready=true`、`delivery_status=listening`、`status=bound`。
- 自动回复和发货监听未受影响。
- 未访问详情页、卖家主页，未调用AI，未运行monitor，未修改数据库。

## 7. 未验证功能

- 并发请求时的真实 `SELECTION_BUSY` 行为。
- 登录失效、可见 Baxia/验证码时的真实错误返回。
- Context 关闭、导航超时、异常JSON等失败路径。
- 中文混合、特殊字符和超长关键词。
- 第二页、多关键词、定时调度和失败重试。
- 商品详情、卖家主页、评论及真实销量字段。
- 搜索去重、历史快照和AI分析。
- ai-goofish-monitor 客户端接入及数据保存。
- 长期运行下对账号风控和消息监听的影响。

## 8. 下一阶段建议

下一阶段仍应保持最小范围：

1. 在 ai-goofish-monitor 增加一个只调用本机API的轻量客户端。
2. 把桥接响应适配成 monitor 现有搜索结果输入格式。
3. 先提供手动、单关键词、第一页入口。
4. 只验证结果写入 monitor 自己的现有存储流程。
5. 调用前后只读检查 manager 的 `message_ready/listening` 状态。
6. 保留桥接错误码，不能把登录验证误判为零结果。
7. 稳定运行若干次后再评估低频定时调度。

暂不建议多关键词并发、自动翻页、复杂队列、CDP、第二套Profile、共享数据库、详情抓取或把AI评分移入manager。

## 9. 当前结论

manager 侧桥接已证明可以复用现有 Persistent Context 完成第一页搜索，并保持消息页面和监听正常。下一步应继续维持“xianyu-manager拥有浏览器，ai-goofish-monitor拥有选品数据和分析”的边界。
