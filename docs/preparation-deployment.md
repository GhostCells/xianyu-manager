# 单账号准备环境（不是生产环境）

本次已确认迁移 Windows 当前运营账号。数据库 ID 必须从旧端实际 runtime 核对，不能从演练库 inactive 状态或昵称猜测。缺少 ID 时先部署未绑定 prepare 后台。

2026-09-06 最新状态：Windows 仍是尚未正式切换的旧端和最终数据来源，但本次查询的自动化监听服务未运行，处于需要验证状态（account_id=2、verification_required、running=false）。不能推断封号，不能省略切换窗口停发和在途核对。候选内部 ID=2 的定向证据与尚未应用的出口部署配置见 `egress-enforcement-window.md`。本轮保持账号未绑定，不声称已验证平台身份。

## 固定进程策略与 API

`XIANYU_MANAGER_SAFE_MODE=true` 优先，原白名单和业务拒绝保持。
`XIANYU_MANAGER_PREPARE_MODE=true` 显式启用准备策略，未设置保持旧 Windows 正常模式兼容。
`XIANYU_MANAGER_ACCOUNT_ID` 可在未绑定准备环境省略，明确设置后必须是存在、未归档的账号；不重编号、不自动激活或归档其他记录。
`XIANYU_MANAGER_LOGIN_AUTHORIZED` 默认 false。仅未来本人批准登录并核实出口限制后才可配置 true，变更需重启。网页不能变更模式或账号授权。
本轮专用 `run_preparation_service.py` 启动器额外要求 LOGIN_AUTHORIZED=false；通用代码中的登录能力仅做 mock 验证。未来真实登录窗口还需单独审批启动器调整，不能只改环境变量绕过本轮边界。

prepare 的启动只迁移兼容 schema 和扫描本地资料，跳过默认账号策略、远程 listing 同步和所有业务恢复；即使导入数据库开关开启也不能执行。

| prepare API | 条件 |
|---|---|
| GET/HEAD /、/static/*、/api/health、/api/accounts、/api/products | 本机管理读取，不恢复 session |
| POST /api/session/start、confirm、sync、cancel | 必须显式绑定账号、进程授权真实登录、出口门禁通过；仍不恢复业务 |
| GET /api/preparation/orders | 显式账号，最小订单核对字段及并发指纹 |
| POST /api/preparation/order-review | 显式账号、固定本机请求头、操作者/原因/证据引用、最新订单指纹 |
| PATCH /api/products/{dir}、POST /api/products/{dir}/verify-share | 显式账号和固定本机请求头；既有 A0 检查继续执行 |
| 其他 API | 默认拒绝，包括账号切换、业务启动/探测/订单恢复/免拼/平台确认/采集 |

本机管理请求仍检查 Host、Origin/Referer 和跨站来源。状态与页面明确展示 mode、runtime_account_id、login_allowed、automation_allowed、egress。账户未绑定时只读。

## 人工核对状态

新增 append-only `order_manual_reviews`；orders 仅新增人工结论与修订号，原发送状态、claim 指纹、ACK 时间、消息 hash、发送次数和出站记录不重置。

- copied：仅记录复制，不改变已知发送结论。
- unknown：继续阻止自动发送；sending 的资料编辑锁保持。
- confirmed_sent：永久阻止本轮自动再次发送；可解除资料编辑锁。
- confirmed_not_sent：可解除资料编辑锁，但仍禁止自动重试。存在 ACK 或历史人工已发证据时拒绝此结论。
- platform_only：仅登记人工报告；不写 platform_confirmed_at，不冒充在线平台验证，不解除 sending 锁。

只在 prepare（业务已硬性禁止）接受核对，避免操作者与正在运行的发送任务竞态。使用整个订单快照指纹和短写事务拒绝过期/并发提交。证据填私有档案编号，不上传聊天或截图正文。
本轮**不提供自动重试授权入口**：确认未发送不清除幂等锁，不转换为 pending；如需重试，后续单独批准并实现受控重试事件，仍必须通过 A0，不能手写 SQL 复位。
准备数据上的审核若要迁移到最终快照，应按 account/order/product 指纹逐条比对并由操作者重新确认，不能覆盖最终数据库保留修改。

## 出口门禁与尚未应用的强制限制

managed 策略（prepare 或显式 ACCOUNT_ID）读取 `XIANYU_MANAGER_EGRESS_STATUS_PATH` 的 root 控制目录及普通文件，拒绝业务可写路径和符号链接。新契约同时核对 boot ID、单调时钟和墙钟（20 秒有效）；指定 R2S、客户端及受限路由、人工复核地址与当前观测一致、review_required=false、enforcement_verified=true 才放行。历史公网地址不是源码常量，direct/relay 不参与出口身份判断。当前准备环境未配置此文件，继续阻断登录。
缺失/过期/异常拒绝浏览器、HTTP/MTOP/WebSocket及任务入口；运行中的主服务每两秒检查，异常关闭自身 runtime/Context并锁存，不能因网络恢复自动重启。此检查仍有窗口，不等于内核限制，也不承诺已经发出的外部请求可撤销；取消中的 sending 保留待人工核对。
诊断脚本 `scripts/inspect_runtime_egress.py` 只观察，不签发批准：enforcement_verified 永远 false、review_required 永远 true。新 `egress_control.py` 是独立 root 状态生产者，部署后复用同一策略契约；本轮仅代码/合成验证，未安装或运行该生产者，未签发批准。不能手工改 checked_at 冒充新观测。

**下面是下一窗口的具体方案，不在本轮应用：**

1. 为业务及 Chrome 建独立 systemd slice/cgroup和专用 UID；现有 ubuntu SSH/Git、tailscaled 留在管理域。授予精确数据目录访问，不放宽 /home/ubuntu。
2. 使用单独 network namespace＋veth；主机对该 veth 的 IPv4/IPv6 forwarding 只允许 tailscale0 和必要返回流量，其他接口拒绝；namespace 不允许 CAP_NET_ADMIN，不能改路由或脱离限制。
3. namespace 只准访问指定 DNS（经 tailscale0），禁止主机代理与任意 LAN、禁止通过 host loopback 代理绕路；本地 API/Unix socket管理通道单独受控，不给业务开放通用代理。
4. 将主服务及全部 Chrome 子进程放入同 namespace；长连接同样受 forwarding 规则约束，不依赖新连接门禁。Xvfb走授权 Unix socket，管理 SSH 不迁入该网络域。
5. 明确正常回程、DNS、IPv6、代理绕路、已建 WebSocket 的测试矩阵。在批准窗口验收 R2S/路由/tailscaled 故障和恢复；失败保持业务停止。绝不仅用 network-online.target 当作业务就绪。
6. 规则以独立、可识别的 namespace/veth/规则集落地，预先保存其精确删除与恢复命令；撤销前先停止业务 cgroup，不删除现有 Tailscale或管理规则。保留独立 SSH 会话验证管理可达，再完成回退。

本轮只做应用门禁，不声明上述网络隔离已完成。真实登录也须在这一窗口之后单独授权。

## 服务与图形访问约束

- Uvicorn 单 worker，127.0.0.1:8765；prepare=true、LOGIN_AUTHORIZED=false，不提供真实 Key，不启 scheduler。
- Xvfb 使用 Xauthority、-nolisten tcp；主服务启动前检查显示实际可用。主服务拥有唯一业务 Context。
- 临时 x11vnc 只镜像相同 DISPLAY，loopback、VNC 认证；noVNC/websockify 用系统本地资源且只监听 loopback。SSH 隧道传输，绝不向公网开端口。
- 图形测试使用独立临时 Profile 与 about:blank/本地页面。测试后关闭该测试进程及图形服务，保留准备 API 和 Xvfb。
- 普通用户、数据700、环境600；systemd KillMode=control-group、合理 TimeoutStopSec、Restart=on-failure 和启动速率上限；不全局 pkill Chrome。
- 禁用 API access log，不记录请求正文；运维日志限制大小/份数，只保留脱敏启动/退出摘要。错误详情在私有界面处理，不发送到普通日志。
- 停机先停临时图形服务，再主服务，再显示；重启后 prepare 不变且不自动重开浏览器。整机重启、真实登录与业务验收留待单独批准。

最终切换：停旧端并核对在途任务→最新 Windows 一致快照→新正式数据目录兼容检查→本人登录同账号→单独放行业务→selection最后启用。演练/准备库不能直接提升生产。回滚须核对云端新增与不确定发送记录，不能直接重启迁移前 Windows 旧库。
