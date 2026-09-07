# 订单接管边界与 reply-only 验收

本轮仅部署能力；账号2、prepare、登录禁止、业务禁止不变。用户报告当前无平台待发货订单，但近期人工交付未必全部进入候选库，不能恢复旧订单补发。本轮不记录正式接管时间。

## 配置与时间证据

- `XIANYU_MANAGER_ORDER_CUTOFF_AT`：留空，未来本人明确“现在接管新订单”时读取准确UTC并记录北京时间、操作者和审批证据，写入服务器root控制配置，重启加载。不是源码常量，不使用启动时间或最近N小时代替。
- 时间来源：卖家订单接口 `mtop.taobao.idle.trade.merchant.sold.get` 的 `commonData.paySuccessTime`，现有解析器输出 `paid_time`。这是当前代码字段语义依据，不宣称本轮做了真实订单时间验证。
- 仅接受10位Unix秒、13位Unix毫秒或带明确UTC offset的ISO时间。缺失、错误、无时区字符串均阻断。不推断中国时区，不使用 `createTime`、数据库时间或消息接收时间。
- 每次交付在claim之前重新读取并精确核对order/item/buyer和待发货状态；未找到唯一记录也阻断。有限订单页未包含目标不代表订单不存在，只代表无法安全发送。
- `paid_time <= cutoff`禁止，严格晚于才继续A0；发送锁内再检查同一付款时间。订单事实、人工结论、claim、ACK及平台确认不修改。

## 发送入口清单

| 入口 | 统一边界 |
|---|---|
| WebSocket新付款、拼团完成事件 | `_process_paid_event` → `_verified_payment_time` → A0 claim → `_guarded_send_text` |
| 启动/重连近期恢复、pending/failed补偿 | `_recover_recent_paid_orders` → 同一付款处理路径 |
| API人工系统补偿发货 | `reconcile_order` → 同一付款处理路径 |
| 服务重启 | 重新加载固定策略和cutoff；无内存旧时间默认值 |
| 最终交付文本出口 | `_guarded_send_text(kind=delivery)`要求付款时间证据；没有证据直接拒绝 |
| confirm_pending | `retry_platform_confirmation`仅确认平台，不重新发送；reply-only完全禁止 |
| 平台确认、免拼 | 最底层各自要求fulfillment能力；reply-only拒绝 |

`_send_text`只有统一出站检查函数调用。数据库claim API本身不执行网络发送。已有12条已完成订单保持不变；缺失历史订单即便恢复发现，也必须通过平台付款时间cutoff。

## reply-only

`XIANYU_MANAGER_REPLY_ONLY=true`要求显式ACCOUNT_ID。它是不可由UI开启发货的进程级限制，派生 `fulfillment_enabled=false` 和 `order_recovery_enabled=false`；数据库遗留delivery_enabled不能覆盖。正常聊天仍由原DeliveryService和唯一BrowserSessionManager拥有，非另建浏览器架构。

- WebSocket启动不创建恢复任务，订单/付款/免拼事件不分派；直接调用补偿、确认或发货入口也拒绝。
- 新聊天精确匹配账号与完整listing ID，要求非空安全knowledge；与分享人工核验解耦。生成后再次核对映射和knowledge，保留回复claim、限额、ACK与幂等。
- reply-only忽略本连接建立之前或没有可靠消息时间的聊天，不把历史聊天重放当新咨询。该聊天门槛不是订单cutoff。
- selection API（含internal bridge）、浏览器搜索/详情及scheduler入口拒绝。
- reply-only进程启动仅提供API，不自动恢复监听；必须在本轮许可、出口和API就绪后显式启动回复。重启不会隐式重新开启测试窗口。
- health中的reply/fulfillment/recovery字段是进程能力，实际回复还取决于数据库开关、连接、凭据与出口；prepare下均关闭。

## 下一轮：仅自动回复真实验收

1. 用户批准限定验收窗口，复核prepare关闭业务状态并保存配置与一致性备份；复用既有隔离、root可信更新器、单worker及Chrome沙箱。不要恢复旧人工登录/合成许可。
2. 安全配置LLM凭据（不进Git/日志/聊天），确认账号2原有登录态；不扫码，除非平台正常要求本人验证。
3. 同一个服务仅在该窗口改用现有启动器的`--reply-only`和readiness的`--reply-only`，设置REPLY_ONLY=true、PREPARE_MODE=false、ACCOUNT_ID=2。正式cutoff继续空。不要直接翻prepare而仍用默认准备启动器；默认入口仍拒绝LLM秘密和正常模式。
4. API先就绪，再核对业务UID/namespace并按既有审批机制开启限定出口窗口，之后显式开启账号2回复。fulfillment/recovery必须false，scheduler不启动。
5. 用户另一个账号向首批一个商品发一条普通咨询。检查唯一接收、正确product/knowledge、一次回复、内容合理；对比订单/出站记录，无delivery/免拼/平台确认/恢复副作用。
6. 失败或到期先关闭监听/owner，再撤许可、关闭临时入口，恢复原prepare启动配置。保留Profile和幂等，不删除数据。成功后仍需用户单独决定是否常驻。

后续自动发货测试仅在reply-only真实验收通过、三件核验仍有效且用户批准“现在开始接管”时设置准确cutoff。旧人工交付不补发。尚未设置cutoff时，完整发货启动与发送均fail-closed。
