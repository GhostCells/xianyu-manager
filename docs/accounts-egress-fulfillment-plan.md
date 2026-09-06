# 多账号、出口与交付一致性：决策及分批验收

本设计基于 `daf7feb` 及本轮定向源码检查。本轮只实现离线诊断/台账导出，
不启用多账号、不调整出口、不操作真实分享，不更新腾讯云。
已知业务基线继续保留：49 个数据库商品、47 个磁盘目录、35 passed、12 failed。

## 1. 联合架构决策

共享管理层保存商品、知识、交付版本及分享登记；`account_products` 和
`account_listings` 仍承载账号自己的上架映射。管理页面选中账号仅是查询上下文，
后台允许启动的账号集合必须另行管理。后台命令必须携带不可缺省的 `account_id`，
runtime 从创建到关闭归属于该账号，禁止从可变的全局 active account 获取目标。

| 方案 | 适用性 | 成本及风险 |
|---|---|---|
| A：同进程多个账号 runtime | 可复用管理层，资源开销较小 | 必须重构当前单实例字段；单个代理配置无法约束所有 Python/WebSocket 流量；错误可跨账号影响 |
| B：共享管理层 + 每账号独立进程/容器 | 推荐演进方向；方便独立网络命名空间、生命周期、审计与停止 | 需要明确进程通信、数据写入归属和 SQLite 并发控制；增加运维成本 |

先上线一个账号，保留 B 的接口边界。不要增加 Uvicorn workers 代替账号隔离。
不默认引入 Redis、消息队列或 PostgreSQL。初期共享单机 SQLite 时保持短事务、
明确写者和订单 claim 所有权；并发瓶颈经测试后再决定是否拆出唯一写入服务。
4GB 主机不预估真实 Chrome 并发量。先验收两个合成 runtime 的隔离，再测真实单账号
内存、峰值、CPU、重连与带宽；第二账号需要独立容量和授权验收。

## 2. 需要调整的具体边界

- `app.py:refresh_products/lifespan`、`database.py:enforce_single_account_mode`：
  正常模式目前会激活一个账号、归档其余账号。未来由明确的管理动作决定账号集合，
  不再在启动扫描中重选。当前行为本轮不变，SAFE_MODE 继续跳过恢复。
- `database.py:get_active_account` 的隐式调用者包括商品、分享更新、listing、订单、
  自动回复和安全状态读取。后台接口改为必须显式 account_id；UI 可保留便捷选择。
- `session.py:BrowserSessionManager`：已有按账号 Profile 路径与 ProfileOwnerLock；
  实例内只有一份 `_context/_page/_account_id/_monitor_task`，不能直接复用给并发账号。
  每 runtime 一份 manager，Profile、owner 和 shutdown 只能影响自己的账号。
- `delivery.py:DeliveryService`：`_task`、`_runtime_websocket`、cookie/request context、
  pending ACK、回复任务和 stop event 属于实例。每账号独立实例/进程；不把后台身份
  绑定到管理页面。启动、恢复订单、发送、确认和停止都需核对 runtime account_id。
- `account_products` 主键 `(account_id, product_dir_name)`；`account_listings` 主键
  `(account_id, item_id)`，复用并保留。当前 `configure_listing_delivery` 使用
  `__listing__{item_id}` 作为共享 products 引用，未来必须先核实平台 item_id 的全局
  语义，防止不同账号的映射更新意外覆盖同一共享登记。
- `orders.xianyu_order_id` 当前全局 UNIQUE，`claim_order_delivery` 使用 paid、
  pending/failed、`message_sent_at IS NULL`、重试上限和原子 UPDATE 防重发。
  不直接改为 `(account_id, order_id)`：若平台订单号全局唯一，改键可能允许重复发送。
  先核实平台语义；保留全局唯一，并增加账号归属冲突拒绝，比盲目放宽键更稳妥。
- `chat_sessions` 主键 `(account_id, chat_id)`；`chat_messages` UNIQUE
  `(account_id, event_fingerprint)`；出站记录 UNIQUE `(account_id, kind, reference)`。
  人工接管按账号/聊天保存，安全限额和熔断按 account_id 保存，已有边界应继续复用。
  内存任务/ACK 键在每账号 runtime 内使用，跨 runtime 路由也必须携带账号身份。
- `app.py:internal_selection_search/internal_selection_detail` 当前取 active account；
  `selection_collection.py` 和 scheduler 的请求没有明确账号选择。未来请求显式携带
  account_id，经 runtime 路由和授权校验；观测记录补充采集账号来源，不能使用全局 token
  加 active account 隐式路由。本轮 scheduler 的 SAFE_MODE 拒绝保持不变。

## 3. account → runtime → egress

三个独立层次，不能把配置当成隔离已经完成：

1. 登记：账号引用 `egress_id`，runtime 启动参数绑定同一出口。出口登记包含授权来源、
   路由机制、人工确认时间和当前预期地址；不把某个公网 IPv4 当永久身份。
2. 网络限制：独立执行环境通过网络命名空间/容器与经验证的路由规则限制出站；
   浏览器、Context request/MTOP、Python HTTP 和 Python WebSocket 都必须纳入。
   仅设置 browser proxy 不足；HTTP CONNECT/SOCKS 对所有客户端的支持需逐一验证。
   禁止并发账号轮流切换腾讯云整机 Exit Node。
3. 运行检测：启动和运行中检测各调用栈出口、路由可达性及禁止回落行为。
   出口不匹配/未知时暂停该账号外部任务，不静默转走默认出口；其他账号不受牵连。

目前只有家中 R2S 一个已验证出口。历史观测 `39.191.10.92` 不是源码常量，
家庭地址变化后停止业务，由用户确认出口仍属授权网络，再更新观测/批准记录。
网络限制和断路演练留在 C 批次；本轮没有实现或宣称隔离完成。
第二网络是否存在、目标同时在线数、容量和带宽仍待确认，不采购代理，不承诺防关联/防封。

## 4. 分享“失效/待复核”的准确路径

| 操作及源码入口 | 实际行为 |
|---|---|
| `scanner.py:scan_product` | 只接受根目录恰好一个 ZIP；计算 ZIP 文件字节 SHA-256。零个或多个 ZIP 时扫描 hash 为空；validator 提示与 delivery-blocking 提示有区分 |
| `database.py:sync_products` | 同一 dir_name 的旧 ZIP hash 非空且与新 hash 不同，置 `share_needs_review=1`；保留 URL、提取码和 share_verified；不是在线网盘检测 |
| 只移动商品库父路径 | dir_name 和 ZIP 字节不变就不会触发 hash 待复核。质检文本中的绝对路径可能改变，但不参与该 hash 比较 |
| 重命名商品文件夹 | dir_name 是主键，新名称作为新商品插入，原记录不删除；新记录没有原分享登记，映射不会自动迁移。可能看起来“链接丢失” |
| 重新压缩相同文件 | ZIP 元数据也参与字节 hash，hash 可能变化；当前没有语义内容 hash，因此会要求复核 |
| `app.py:_load_product_knowledge` → `set_product_knowledge_folder` | 更新知识来源/正文/hash/时间；不清除分享核验、不设置 share_needs_review |
| `clear_product_knowledge_folder` → refresh | 清空知识字段后重扫；知识本身不影响分享，但同次扫描发现 ZIP 改变仍会触发复核 |
| `app.py:update_product` → `database.py:update_product` | 显式 share_verified=false 清除核验；true 清除 share_needs_review。URL/code 更新覆盖旧值，无分享版本历史 |
| `static/app.js:openEdit/saveEdit` | 复选框取 verified 且非 needs_review；保存时总是发送该复选框。已有待复核的商品打开后保存其他字段，也可能将 share_verified 写为 false |
| `configure_listing_delivery` | 校验百度 HTTPS `/s/` URL 语法；写入/覆盖 synthetic product，按输入设 verified 并清除 needs_review；默认 verified=true，仍不是在线核验 |
| `delivery.py:_process_paid_event/_recover_recent_paid_orders` | 未登记/未核验/待复核则停止；没有访问网盘判断分享是否失效 |

没有发现“复制链接”会撤销网盘分享的代码。当前字段不表达 HTTP 超时、需要登录、
撤销、过期等外部检测结果，因此不能说系统已把三者检测并合并；它没有这种探测。
具体失败来源需用户提供时间、操作顺序、脱敏提示与截图，不能根据观察猜外部原因。

### 已识别的独立修复范围（本轮不改发货判定）

1. `update_product` 更换 URL/code 而没有提交 verified 时，会保留旧 verified；UI 改链接
   也不自动取消复选框。建议绑定核验到分享修订，链接/code 变化默认待核验，明确核验
   新修订后才能切换；回归覆盖 partial PATCH、UI 全量保存、纯知识修改不影响分享。
2. `get_product_by_listing_item_id` 使用 `id=...` 子串匹配，存在前缀碰撞；建议改为
   精确解析平台域名与唯一 item_id，并测试碰撞、重复映射和跨账号。当前纯 helper
   抽取保持原语义；离线工具检测碰撞并拒绝生成可直接发送文本。
3. 部分生产 readiness/发送路径只检查映射与分享，不统一检查 quality_status。
   本轮不放宽或修复；导出工具额外要求 quality_status=passed，并明确这是保守的
   离线文本资格，不声称与所有现有自动发货路径完全等价。后续统一规则需单独回归。
4. 当前覆盖式分享更新没有完整旧值历史；audit 记录字段名/操作不足以还原旧分享。
   不能伪造旧分享修订或核验时间。

## 5. 最小商品与交付关联方案

渐进新增稳定 product_id，保留 dir_name 作为兼容别名和现有外键；通过受控迁移建立
一对一映射，先双读校验再迁移引用，不直接替换主键。

- 商品：product_id、展示名、知识版本、兼容 dir_name。
- 交付版本：delivery_version_id → product_id，ZIP 字节 hash、可选内容清单 hash、
  登记来源与时间；不把运行机器绝对路径当版本标识。
- 分享修订：share_revision_id → delivery_version_id，URL/code、核验方法/状态/时间、
  修改者和前修订；保留旧记录，新修订核验后显式切换 current 引用。
- 账号上架映射：account_id + listing_id → product_id / delivery_version_id；复用已有
  account_products/account_listings，不按账号复制整套商品/知识/分享登记。
- 实际交付：订单关联实际 delivery_version_id、share_revision_id、文本 hash、渠道、
  发送状态和平台确认状态。旧订单的发送版本缺失就保持 unknown，不回填为当前分享。

明确的“商品登记/发布”动作或清单导入负责绑定知识、交付包、分享和账号上架。
清单先放业务数据目录；validator 未确认兼容前不往客户 ZIP/商品目录加新文件。
扫描通过不代表已上传网盘、不代表分享核验通过，也不自动上架。
长期拆分完整生产目录和运行所需知识/交付登记；本轮不搬文件、不改启动扫描。

## 6. 人工兜底和恢复

自动发送与人工复制应读取同一已登记分享修订。正常时复制已登记文本，无需默认
重新创建分享。故障时使用事先授权导出、存放 Mac 私有目录的带版本和时间快照。
本轮仅用合成数据库验证，没有导出或同步真实台账。

复制事件不是发送事件；人工发送需记录 account/listing/order、分享修订、时间、渠道、
操作者及证据。人工已发送也不等于平台已确认。恢复时先冻结自动补偿、导入去重的人工
记录、核对不确定订单；不确定即转人工，不能盲目重发，不能承诺离线 exactly-once。

真实快照含链接/code：只放私有运行目录，不进 Git/公共 Obsidian/客户 ZIP。
当前工具创建 700 目录、600 文件，这不等于加密。未来真实备份使用用户确认的加密
工具和独立密钥管理（如 age 接收者或系统加密卷），避免密码进入命令日志；加密及恢复
需另行测试。快照按生成时间标识最新/过期；链接更新后生成新快照，旧快照隔离为只读
审计资料，按用户批准的保留期处理，不擅自覆盖/删除。离线状态不保证链接始终有效。

## 7. 本轮工具契约

`python -m xianyu_manager.offline_catalog --database <existing-backup.db>` 仅打印不含
商品名、URL、code、买家或聊天的聚合诊断；不会创建输出文件。

加 `--output-dir <outside-repository/new-private-directory>` 显式导出 JSON 和 Markdown。
目录必须尚不存在、父目录存在，拒绝任何 Git 仓库及其符号链接内的目标，拒绝覆盖。
以 `mode=ro`、query_only 和读取事务打开现有库，不实例化 Database、不迁移 schema、
不导入 app、浏览器或发货 runtime。拒绝 WAL/SHM/journal sidecar，要求先获得一致性
独立备份；导出前后检查主文件 hash。对并发修改的活库不承诺完整一致性，应使用静止快照。

只查询必要字段，不读取订单、聊天、买家、Cookie、LLM Key 或知识正文。
兼容字段缺失时拒绝而非初始化。诊断输出仅为计数和固定错误码。

文本资格：登记链接已核验且非待复核、语法合理、quality passed，并至少有一个启用、
published、item_id 可解析且唯一、无已知 listing 映射冲突的账号映射。
不依赖账号 active 状态，故障人工处理不会因此偷偷激活账号；仅适用于明确列出的映射。
阻断商品只列问题，不附原始分享或可发送文本。无法知道远端分享是否过期/撤销，
`online_status=not_checked`；语法通过不代表在线有效。
若用户已从外部确认某分享过期/撤销，但库尚未更新，可重复传入
`--block-product-ref <现有dir_name>`：仅在本次导出增加 `OPERATOR_REPORTED_UNUSABLE`
阻断，未知引用报错，不写数据库、不声称工具进行了在线核验。

JSON/Markdown 共用同一快照。没有稳定 ID/版本/核验时间的字段明确 null 或未提供，
`record_ref=product:dir_name` 是现有引用，不冒充跨重命名稳定 ID。
导出资格不是订单发送许可，不调用 claim/send、不写任何发送状态。
中途磁盘失败可能留下权限受限的未完成目录；不自动覆盖或删除，应检查后选择新的目录重试。

## 8. 后续批次和最短上线路径

| 批次 | 实施及验收 | 首个账号上线要求 |
|---|---|---|
| A 商品/分享/人工补发一致性 | 分享修改必须重新核验；精确 listing 匹配；统一质检阻断；逐步增加版本、修订和人工事件。回归订单防重发与旧映射保留 | 先完成 A0：修复上述明确风险，审核当前交付登记及人工未决单；完整版本化可后置 |
| B 账号上下文 | 显式 account_id、后台集合与 UI 分离、每账号 manager/任务/限额/幂等归属；两个合成账号互不影响 | 单账号上线前确认唯一 runtime/owner 和账号归属；完整并发改造可后置 |
| C 执行环境/出口 | 账号 runtime 对出口声明、网络强制限制和逐调用栈观测分别验收；断路不回落，不切全局 Exit Node 服务并发 | 首账号完成现有唯一出口的异常停止与恢复验收；第二出口/多命名空间可后置 |
| D 首账号云端上线 | 人工建立新 Linux Profile、独立授权 secret、低风险真实 smoke、最新 Windows 一致快照、订单/人工记录核对、停止旧发送者后切换、systemd/重启与回滚验收 | 必须单独授权和执行；演练库不能升为生产库 |
| E 第二真实账号 | 目标数/第二网络授权/资源容量确定，B/C 双合成账号验收后再增加真实账号 | 可后置，不阻塞首账号与 Windows 退役 |

最短路径：A0 交付登记修复与未决单核对 → 单账号 owner/出口异常停止验收 → D。
不要求完整多账号、第二出口、所有商品目录完成或全部版本化后才退役 Windows。
需要用户补充：目标同时在线数、第二获授权网络是否存在，以及“链接失效”的准确提示、
发生时间和人工操作顺序（复制、编辑旧链接、重新分享、重压缩、改名、系统保存分别区分）。
