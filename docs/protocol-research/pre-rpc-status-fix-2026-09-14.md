# 3.5 pre：RPC 错误解析与分类修复（2026-09-14）

## 结论与统计口径

本轮接续 11:41:33 部署的任务计数修复，分析到 15:23:13 的独立公共任务：**45 个，成功 23、失败 22，失败率 48.9%**。时间均为 Asia/Taipei。

使用 `async_generation_outcomes` 的公共任务 ID 去重，不把内部任务、提交日志、轮询日志或排队重试分别算成任务。此前采样的 43 个任务为 21 成功、22 失败；随后两个用户任务完成，统计窗口相应扩大。

| 失败类别 | 数量 | 证据与结论 |
| --- | ---: | --- |
| 提交 RPC 错误封装未解析 | 19 | `MZZa6b` HTTP 200，227–229 字节，`payload_not_string`；账号 32 为 10 次，账号 29 为 9 次 |
| 已提交媒体终态失败 | 3 | `wire_status=4`；账号 52 两次、账号 20 一次；原适配器未提取内层真实 Status |

19 次错误的结构一致：`list(7)[str,str,null,null,null,list(3)[int,null,list(1)],str]`。旧日志只保留类型，未保留数值和公开错误枚举，**不能据此断言它们全部是 429、额度不足、风控或内容审核**。

错误涉及 6 秒、10 秒等模型，不是 10 秒模型独有；这些账号之后仍能成功，不能直接判定账号或登录态永久失效。上一轮“协议异常不累计账号禁用次数”的保护仍有效。

## 协议证据

与 9 月 11 日已保存的 Google Flow 前端代码对照：

- `output/playwright/flow-native-auth-20260911/frontend.js`：`Dq/wrb.fr` 是带字符串 sentinel 的 proto；`getError()` 读取字段 5，对应响应行 `row[5]`。没有字符串 payload 时，前端抛出这个 Status，而不是将其认作正常数据。
- `output/playwright/g9-9urm-flow-native-20260911/frontend-wO1vlb.js`：`cJ/uQa` 解析 `google.rpc.Status.details` 中的 `google.rpc.ErrorInfo` 和 `PublicAitkError`；Any 支持数组和 base64 protobuf 两种表示。
- 同一源码的媒体状态 `vL.getError()` 读取字段 2，即 `state[1]`。外层状态 4 与内层 gRPC 错误码是两个不同的字段。

本轮通过 Playwright 只读附加服务器已有浏览器，监听响应；没有启动本机 BAT、创建新登录浏览器或重新提交历史任务。曾观察到只读 `as29s` 返回 gRPC 5（NOT_FOUND）；这是查询历史媒体的结果，**不是历史 19 次生成失败原因的证明**。

## 已落地并上线的修复

1. 新增 `flow_rpc_errors.py`，解析真实 gRPC code 和源码确认的公开错误枚举。解析器有大小边界、类型检查、截断检查和重复字段检查；不保留原始 message、Any 元数据、Cookie、提示词或签名 URL。
2. 只有单一、无冲突、明确拒绝的 RPC 才产生 `AngularRpcRejected`；多行冲突、响应损坏、未知语义仍按提交不确定处理。
3. 明确限流/风控归入既有排队退避和绑定账号代理的风险处理；内容问题归为友好 400；用户额度与单模型限制单独分类，不因单模型额度限制把整个代理冷却或把所有模型余额置零。
4. native CDP 保留拒绝、认证失效、协议不确定的不同异常类型。RPC 认证失效进入会话刷新路径；明确拒绝不在 FlowClient 内层隐式重试，由外层队列处理适用的 429/503。
5. 媒体终态保留真实 gRPC code、公开原因和 `code_source=google_rpc_status`；只有缺少真实 Status 才使用本地兼容码。外层 `wire_status` 不再与错误码混淆。
6. 结构化诊断新增 `grpc_code`、`public_error`、`reason_conflict`，保留原有公共任务关联和终态计数保护。

`DEADLINE_EXCEEDED`、`UNKNOWN`、`INTERNAL`、`UNAVAILABLE` 等不能证明非幂等操作未被执行，因此不自动重新生成。此处使用保守判定，避免重复生成和扣费；参见 [gRPC 官方状态码说明](https://grpc.github.io/grpc/core/md_doc_statuscodes.html)。

## 测试、部署与回滚资料

- 本地全量回归：**514 passed、216 subtests passed**；18 项既有 SQLite datetime adapter 弃用警告。`git diff --check` 通过。
- 新测试覆盖 JSON/二进制 Any、畸形与冲突数据、明确拒绝与提交不确定、认证分支、无内层重提、代理归属、额度/策略隔离、媒体真实错误保留。Linux 新镜像通过相关协议和数据库回归。
- **15:23:13** 部署 `ghcr.io/btcfoxman/flow2api:rpc-status-20260914`。部署前确认队列、运行任务、人工登录锁均为 0；之前两次遇到用户任务时未进行替换。
- 线上 10 个业务文件 SHA-256 与本地一致，SQLite `quick_check=ok`。
- 备份：`/home/btcfoxman/docker/flow2api/rpc-status-20260914/backup`；上一镜像 `ghcr.io/btcfoxman/flow2api:task-accounting-20260914` 保留。
- 按部署规范仅替换 Flow2API，保留 `.env`、数据库及 Profile；独立 `flow2api-tupdater-pre` / VNC 未重启，密码及代理配置未修改。
- 本轮未执行 Git commit/push；已有工作区改动保留。

## 真实生成验收

只额外提交了一次最小消耗测试，使用项目公开卡通图标作为参考图，没有重发该测试。

| 项目 | 结果 |
| --- | --- |
| 公共任务 ID | `flow2api-submit-ce9220e881d44b4089e3dded1aa14fd7` |
| 模型 / 账号 | `abra_r2v_4s_360p` / 38 |
| API 接收 | 15:23:37 |
| 上游提交成功 | 15:26:02 |
| 公共任务完成 | 约 15:26:27，`completed / 100` |
| 下载后文件验证 | MP4，640×360，4.011 秒，355,507 字节 |
| SHA-256 | `84ef20beb26ef28677d262accc871994561b676fd74c97357ed2cab8d5bc2b9f` |
| 积分差值 | 4 |
| 任务计数 | 1 条 completed；内部尝试 outcome 为 0 |

视频保留于服务器 `/app/data/acceptance/rpc-status-20260914/canary.mp4`。本地部署及验收记录在工作区 `output/deployments/rpc-status-20260914/`。

上线后的另一用户任务 `flow2api-submit-6adfed4445134cd68795765da230c2d0`（账号 20，`abra_r2v_10s`，7 张参考图）也已完成；未下载用户视频。15:29 快照中，新版本接收的两个任务均完成，队列及运行任务均为 0。累计视频成功 1051 → 1053，累计成功 1117 → 1119，累计失败保持 1936。

## 尚未解决或尚未证明的部分

- 两个成功样本不等于长期失败率归零；新版上线后的短观察窗尚未再次遇到 `MZZa6b` 错误帧。错误分支已做回归验证，真实上游拒绝的业务原因仍需后续新日志确认，不能伪造历史原因。
- 15:29 的 34 个账号中，24 个启用，但仅 7 个会话验证通过，17 个为 `refresh_required`；其余 10 个禁用。验证通过也不代表余额足够提交所有模型。不能把“启用数量”视为可执行并发。
- 多个旧会话打开后停留在 `/about`。启动时额度刷新与任务预检共享 3 个浏览器容量，本次测试前置等待约 110 秒；之后提交链路约 36 秒、上游生成约 24 秒。冷启动探测顺序/容量竞争仍是下一项调度优化，不通过跳过身份验证提速。
- 没有自动恢复旧禁用账号，也没有代替人工登录失效账号。仍保持源账号管理与目标执行分离。
- 进程快照存在 1 个 zombie，观察中未增长；本轮没有清理运行中浏览器或宣称全部进程生命周期问题已解决。

安全分析快照：`output/diagnostics/pre-task-errors-20260914/latest.json`、`runtime.json`；部署前快照：`output/playwright/pre-rpc-errors-20260914/before-deploy.json`。快照是采样时刻证据，不应与其他窗口的历史累计数混用。
