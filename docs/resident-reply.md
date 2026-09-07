# 常驻回复（MVP阶段A）

`XIANYU_MANAGER_RESIDENT_REPLY=true`仅与normal、reply-only、显式账号和登录授权同时使用。
主服务等待root出口状态就绪后恢复唯一Profile owner；登录未被检测到时保持页面等待人工验证，不循环确认、不伪造bound。
只有已有回复开关开启且正常登录确认成功才恢复消息监听。默认临时模式行为不变。

root审批的`lifecycle=resident_reply`、`account_id=2`、`operations=[login,reply]`、`expires_at=null`
表示用户明确授权的持续回复，不是取消内核短许可：root状态仍20秒有效、内核许可仍30秒有效，
出口/UID/路由/规则检查与故障锁存完全保留。整机boot变化仍需要重新确认，不宣称整机自动业务恢复已验收。
普通主服务重启可恢复owner；noVNC是独立服务，关闭图形入口不关闭owner。

不再布置普通验收结束时关闭Chrome的timer。部署失败的回退保障仍可保留，稳定后取消。
撤销时先停业务主服务，再撤销root审批及短许可。不要先撤保护后留业务运行。
本阶段cutoff空、发货与订单恢复false、scheduler关闭；不提供发货开启授权。
