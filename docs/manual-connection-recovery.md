# 手动检查并恢复连接

后台账号连接 → 连接助手 → 检查并恢复业务连接。Mac助手需运行，图形隧道不是此按钮的前提。

固定通过现有 xianyu-cloud SSH 身份运行 root 安装副本 `/usr/local/lib/xianyu-egress/recover_egress.py`；不添加网页通用命令或可配置目标，不改变 sudoers。POST 保留 loopback Host、同源 Origin 和随机令牌验证。

只恢复当前有效账号2常驻授权下，observe/curl 明确临时错误造成的 inactive/failed 更新器；当前审核锁、授权/boot变化、出口/规则/进程身份变化均拒绝。旧授权对应的历史锁保留，不解除当前锁。

以 root 文件锁串行，60秒冷却；重新观测原出口、路由、规则及进程身份，再复核授权和故障没有改变，然后只 reset-failed/start xianyu-egress.service。原更新器自行重验并生成短租约；本工具不写授权、锁或可信状态、不重启 Chrome/主服务、不修改业务开关、cutoff或订单。

最多等待40秒观察至少两次新鲜成功状态且稳定10秒；整体 root 操作150秒上限，Mac SSH180秒上限。出口成功只表示出口恢复，业务由原 guard 重新连接；仍需检查后台 listening。平台验证需本人处理，工具不点击快速进入/重连、不刷新闲鱼页面。

安装：测试后将脚本复制到 root-owned `/usr/local/lib/xianyu-egress/recover_egress.py`，权限0644；既有 egress_control.py 保持不变。仅更新前端和Mac助手，无需重启主服务。

回滚：恢复备份静态文件与Mac助手，移除本轮新增root脚本（先确认无执行中任务）。不回滚生产数据库，不撤销原授权，不修改出口保护服务。
