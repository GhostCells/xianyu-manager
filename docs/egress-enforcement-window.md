# 首账号记录与出口保护：准备交付，未应用在线规则

## 后续维护补充（优先于下文历史记录）

用户已明确确认内部账号2并完成prepare配置绑定，Windows字段来源核对已收口；不再要求确认编号或运行版本。真实平台身份未验证。
维护窗口已批准，控制台VNC已由用户验证。允许独立IPv4 forward范围表，保持非业务转发禁止，不修改Tailscale自有规则或既有默认策略。
`host-forward.nft`在-175优先级只准精确双向业务接口和地址继续接受-150业务lease门禁；非业务IPv4丢弃，不触及其他hook/IPv6。
`rollback-window.py`读取root受控独占对象清单，先停业务/更新器并撤lease，确认namespace无人，再恢复转发与本轮实际受影响的可写sysctl，验证成功后才删除专属对象。参数恢复失败保留保护。原服务配置先验证哈希和prepare/login/account约束，不覆盖业务库。
回退清单的`restore_sysctls`必须在开启转发前记录潜在受影响可写项，并在变更后收窄到实际差异；不能缺失后再补。未布置实际timer时不得执行在线部署。
新增匿名网络空间真实包预检覆盖空lease、精确路径、非业务阻断及到期阻断，并加入后置合成ACCEPT链。它不替代实际业务UID、浏览器、宿主网络或真实回退验收。

## 已核实的账号事实（2026-09-06）

用户确认同账号迁移，回传 Windows GET /api/delivery：account_id=2、status=verification_required、running=false。
准确状态：**Windows 仍是尚未正式切换的旧端和最终数据来源，但当前该自动化监听服务未运行，处于需要验证状态。** 不推断封号，不尝试验证或重启。

当前 `app.py:delivery_status` 返回 `DeliveryService.snapshot()` 的 account_id；`delivery.py:snapshot` 取内存 `_account_id`。start/start_auto_reply 会赋值；_run 遇 SessionVerificationRequired 会设置状态并退出任务，不必清空此 ID。因此它可能是最近任务所属账号，不等于页面选择、当前 active 或平台身份验证。端点另有部分统计来自 active account，不能混作身份依据。
迁移基线 `acf09a9` 和当前代码均有此语义，已补合成回归；**Windows 实际加载版本仍未知**，不能将本地源码当成旧端进程取证。

只读比较母版与准备副本：accounts.id=2 全部原字段相同，inactive、未归档；另一个历史 id=1 保留。ID=2 的关联计数均一致：account_products 42、account_listings 43、orders 12、chat_sessions 52、chat_messages 514、outbound_events 53。没有读取 Cookie/Profile/DPAPI，未输出昵称、平台标识或正文。

决定：候选 ID=2，**本轮仍不绑定配置**。最小缺失证据是 Windows 当前服务实际加载版本中 delivery_status/snapshot/_account_id 赋值的来源片段，或可对应到这些代码的运行版本/commit；无需新生产快照、无需唤醒任务。补足后只更改准备 EnvironmentFile 的 ACCOUNT_ID 并重启准备 API，前后只读核对业务表；不能写 active/bound，不授权登录。Linux 真实登录时仍由用户本人确认同一平台账号。

## 新增边界（不是第二套应用门禁）

- 继续使用 RuntimePolicy；仅加固状态来源和有效期。root 所有、父目录不可由业务写入、拒绝 symlink，boot ID + 单调时钟 + 墙钟共同限制有效期20秒。Mac 合成测试用显式假生产者，不提供生产环境绕过开关。
- `egress_control.py` 是 stdlib-only root 更新器；安装为 `/usr/local/lib/xianyu-egress/egress_control.py` 后独立运行，绝不能让 root 定时执行 ubuntu 可写 Git checkout。
- root 审批记录 `/etc/xianyu-egress/approval.json` 需要实际故障报告摘要、规则摘要、boot ID、最长24小时有效期、人工复核出口地址和新 approval_id；示例全部关闭，不自动签发验收。
- 更新器核对 Tailscale 指定 R2S、在线状态、业务转发路由、独立 runtime UID/cgroup 中所有进程的 namespace、规则摘要及无 flow offload。公网观察强制 tailscale0，忽略代理变量，不代表逐调用栈实测。
- 每轮成功更新30秒 nft lease，然后原子发布20秒状态；异常清 lease、持久化阻断原因、退出更新器。`Restart=no`，不能网络恢复后自行恢复；需新人工批准和服务重启。写盘或撤销命令失败时，不续约；状态和内核 lease 各自到期。已有 RuntimePolicy 熔断锁存继续适用。
- 停止/杀死更新器后的内核阻断有最多30秒 lease 窗口，应用状态最多20秒；这是有界检测，不是零延迟保证。tailscale0 也不是 R2S 身份本身，管理员切换 Exit Node 依赖观察阻断窗口；不声称防 root 篡改或逐包 peer 身份锁定。

## 待批准的执行环境

`deploy/egress/` 为未安装的配置模板与部署/撤销脚本。

1. 新专用用户 xianyu-runtime，与 ubuntu SSH/Git 和 root tailscaled 分开；xianyu-proxy 仅做固定目标管理转发。不是封禁整个 ubuntu。
2. 一个 xianyu-business network namespace，经 xmg-host/xmg-net veth；拟用10.203.0.0/30，部署前必须检查全部路由表的网段重叠。当前 `ip_forward=0`，脚本明确拒绝；**本轮不启用转发**。后续窗口需单独审核开启转发的主机影响和完整 sysctl 回退，不自动偷偷设置。
3. nft 只匹配该 veth。出站只允许 tailscale0 上的 TCP80/443 和指定 DNS TCP/UDP53；内网/Tailnet/host-local代理禁止。对目的10.203.0.0/30增加优先级1000的main表回程规则，避免Tailscale表52默认路由捕获veth回包；若该优先级占用则拒绝部署，不替换已有规则。首期 IPv6明确丢弃且仅在新 namespace关闭，不宣称IPv6可用；未增加第二出口。
4. 每个方向（包括 established/related）都要求 lease；没有“已连接一律允许”的旁路。禁止 flowtable/flow offload，避免绕开 forward hook。实际规则组合及 conntrack/SNAT 回程必须在窗口验收。
5. 应用及 Chrome 子进程同 namespace/UID/cgroup；无 capabilities、禁止创建/加入额外 namespace。独立网络空间隔离 host loopback 和 abstract Unix sockets；隐藏 host /run、/tmp、/var/tmp、/home，仅绑定必要 X99 Unix socket、root只读状态目录和私有 API socket。不得继承 host 代理环境。
6. DNS 初拟1.1.1.1，需与规则一起批准或调整；`resolv.conf` 只读绑定。浏览器 DoH/TCP443若存在也必须经同一出口，本方案不把HTTPS内容过滤冒充DNS域名限制。
7. 主服务仍拥有唯一 BrowserSessionManager，prepare/login-disabled 启动器不放宽。新增仅允许固定 `/run/xianyu-runtime/api.sock` 的可选 UDS；不设置时仍为当前loopback8765。单 worker，无 scheduler。
8. 管理 socket-proxyd 只将宿主127.0.0.1:8765转到固定UDS；代理为另一UID并使用PrivateNetwork，不能充当业务通用出口。UDS父目录0750、xianyu-api组仅包含两个服务用户；ubuntu经SSH端口转发访问TCP，不获业务目录写权限。

## 后续安装步骤（本轮均未执行）

先开第二条独立 `ssh xianyu-cloud` 会话；确认腾讯云控制台串行/VNC救援渠道能用。该备用渠道尚未实际验证，不应假设公网SSH开放。

1. 记录基线：全部ip rule/route、sysctl转发相关设置、现有nft/iptables规则与哈希、SSH可达、准备health和业务表摘要。所有备份放权限700的独立维护目录，不提交Git。
2. 审核10.203.0.0/30无重叠、DNS选择、现有FORWARD链和开启IPv4转发的影响。当前为0，未经明确批准不可改为1；改变转发可能影响内核相关默认值，须保存并逐项核对，不只记一个布尔值。
3. 从批准commit制作不可变代码/venv与root-owned监测器；审阅后用install写入 `/opt/xianyu-manager`、`/usr/local/lib/xianyu-egress`，root拥有且业务不可写。整个 /etc/xianyu-egress 由root控制；approval.json 0600，状态目录0755/文件0644。不复用ubuntu可写源码运行root服务。
4. 建专用用户/组，创建新的独立准备数据和只读资产工作副本、授权同一X99会话；不移动或改母版、不开整个/home/ubuntu权限。配置模板中的路径必须存在并通过访问检查；不加入真实Key/Profile。不是最终生产快照。
5. 将reviewed的网络脚本和guard.nft安装到 `/etc/xianyu-egress`（root所有、不可被业务写），只在窗口执行：`sudo /etc/xianyu-egress/apply-network.sh --approved-window <窗口编号>`。先安装空lease再接入veth；失败保持关闭，不自动删除保护规则。
6. 暂停旧“准备API”（不是Windows），部署独立准备服务和固定目标管理代理。保持账号未绑定或已核实ID、prepare=true、LOGIN_AUTHORIZED=false；先证明管理UDS、X显示和文件权限可用。当前准备API可按下面回退恢复。
7. 使用合成临时fixture、本地/自控无业务端点完成下表；临时开放lease的测试命令仅在窗口给出、不得签发生产批准。真实闲鱼/LLM/网盘仍不访问。记录真实规则摘要及报告哈希后，人工制作带当前boot ID的新审批文件，才可启动root更新器。它没有自动审批命令。
8. 先观察准备模式持续阻断业务，再关闭测试进程。整机重启另行批准；namespace未自动持久重建，重启默认失去授权并保持业务禁止，不能声称无人值守恢复完成。

## 故障验收矩阵（全部尚未执行）

| 场景 | 影响范围 / 做法 | 预期与停止条件 |
|---|---|---|
| 正常IPv4、HTTP/Context request/模拟MTOP/WebSocket/Chrome子进程 | 独立合成runtime、自控端点；不使用真实平台或Profile | 同一批准出口，管理SSH持续可达；任何真实业务请求立即停止 |
| 禁止直接公网、LAN、host代理、IPv6及错误DNS | 仅业务namespace中发合成探测 | 全部禁止；宿主SSH/Git不受限，泄漏即停止验收并撤lease |
| 新连接和已建长连接失去lease | 仅清本表lease或停止root更新器 | 不仅新连接，既有流量也应阻断；不得因ct established继续通行 |
| 更新失败、root状态不可写、过期、boot不符 | 独立测试状态目录/测试服务，不能破坏实际/root状态权限 | 拒绝、更新器停止、无自动续约；20/30秒界限按实测记录 |
| 出口选择不符/公网变化 | 合成观察输入；不得切换整机Exit Node | 同一approval_id不能自行恢复；需人工新复核，不能接受任意新公网IP |
| 业务路由故障 | 仅新namespace临时替换为unreachable default，之后恢复其既定路由 | 管理通道不变；状态失败，恢复路由不应自动恢复授权 |
| tailscaled/R2S整体中断 | 更大影响，**不属于此最小窗口** | 另行批准并先验证控制台救援；不能把模拟结果当作真实中断已通过 |

所有故障前停止合成发送循环之外的任务；真实sending/ACK未明记录不做重置，网络恢复不代表订单可重发。

## 精确回退与停止条件

- SSH任一路不稳定、准备模式/登录开关不符、规则触及非业务接口、误读母版、出现真实业务网络请求：停止，不继续“修到绿”。
- 首先 `sudo systemctl stop xianyu-egress.service xianyu-isolated-prepare.service xianyu-api.socket xianyu-api.service`；然后 `sudo nft flush set inet xianyu_guard lease`。只撤通行，不先撤保护。
- 确认新namespace无进程后，运行 `sudo /etc/xianyu-egress/rollback-network.sh --approved-window <窗口编号>`。脚本只删除xmg-host、xianyu-business、上述精确回程rule和两个自有表；不清空全局规则、不操作tailscaled或Exit Node。
- 若安装中途未生成某对象，逐项只读检查后跳过不存在项，不能改成批量删除或忽略所有错误。若业务未停下，不删除guard。
- 按本窗口记录逐项恢复获批改变的转发sysctl并验证管理路径；保留配置、批准记录和新工作副本，不删除数据。
- 确认8765释放且原EnvironmentFile仍prepare/login-disabled后，`sudo systemctl start xianyu-preparation-display.service xianyu-preparation.service` 恢复原准备后台。不恢复Windows业务，不启新业务。

## 证据边界与参考

本轮只有合成测试和新临时namespace中的 `nft --check`；没有应用在线规则、没有故障注入、没有安装/启动新监测器或独立runtime。nft静态检查通过不证明Tailscale真实回程、namespace服务路径或Chrome运行已验收。

本轮全套合成测试297通过、2跳过（Windows DPAPI、真实商品库）。云端systemd静态校验未报未知配置项，但整体未通过：模板依赖的 `/opt/xianyu-manager/.venv/bin/python` 尚未部署；另有既存腾讯云tat_agent的legacy PIDFile提示，不修改它。不能把模板解析检查写成独立服务已可启动。当前已运行的准备API未替换。

- [nftables官方手册：check、hook、set timeout](https://netfilter.org/projects/nftables/manpage.html)
- [systemd官方源码文档：NetworkNamespacePath及文件系统限制](https://github.com/systemd/systemd/blob/v255/man/systemd.exec.xml)

最后切换仍需获批停旧端和核对在途，取得最新Windows一致快照后新端人工登录同账号，登录不恢复业务，发送单独放行。回滚需对账云端新增/未明发送，不能直接重启迁移前Windows旧库。
