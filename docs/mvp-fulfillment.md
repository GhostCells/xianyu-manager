# 首批新订单发货

仅在真实回复验收通过后开启。`XIANYU_MANAGER_MVP_FULFILLMENT=true`要求normal、resident、账号2、非空完整item白名单及合法UTC cutoff；启动器/readiness使用`--mvp`。
`XIANYU_MANAGER_FULFILLMENT_ITEMS`为逗号分隔完整ID。白名单在付款处理、最终交付发送前再次验证。
`XIANYU_MANAGER_ORDER_CUTOFF_AT`必须在用户准备新订单测试时读取准确当前UTC写入root配置及审计，不在此文档预设。

MVP启用后订单恢复恒false；拒绝人工系统补偿入口、历史confirm_pending、自动免拼和selection。
平台当前付款时间仍从原接口`commonData.paySuccessTime`获取并严格晚于cutoff；原A0核验、claim、幂等、限额不变。
显式runtime账号用于发送资格，不改变数据库is_active，也不选择历史账号1。
root出口授权使用`resident_mvp`及`[login,reply,delivery]`，仍保留短内核租约、状态过期和故障锁存。
正常新订单的平台确认遵循账号现有配置；失败不重发，转人工核对。

部署此代码不等于启用发货。可保持原reply-only配置、cutoff空部署；准备就绪后才写cutoff、启用本模式及账号发货开关。
