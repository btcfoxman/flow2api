# 3.5 pre 新站登录与异步视频验收

## 环境与发布

2026-09-11，在 192.168.3.5 的生产 Flow2API（4020）验证，而非本地模拟或隔离测试 API。

- `93ca1fa`：显式 Flow Cookie 认证、目标身份验证、新站项目/图片/视频协议；GitHub Actions `34580623701` 发布成功。
- `39bd106`：未适配的模型/输出规格返回终止性 501，不再以 503 无限重试，不计入账号错误、不封禁账号。460 项回归通过。Actions `34581362663` 首次因在途任务被保护规则拒绝部署；待任务结束后只重跑失败部署作业，已成功。
- 发布前保留 SQLite 一致性备份及相关目标 Profile 备份（仅服务器上，包含凭据，不入库）。
- 新版本部署遵守既有在途任务保护；没有绕过保护、取消用户任务或删除排队记录。

## 账号与代理发现

g9 源浏览器 Profile `D:\tmp\g-99` 实际使用 SOCKS5 20019；目标账号 50 旧配置却为 20009。
通过两端 Cloudflare trace 核对：本机 20019 与服务器 20019 出口相同，20009 是另一个出口。
仅修正此账号的目标代理为 `socks5://127.0.0.1:20019`，未批量统一其他账号代理。

使用既有浏览器的当前 Google/Flow scoped Cookie，通过一次性内存通道送入生产 `/api/plugin/update-token`：

```json
{
  "auth_mode": "flow",
  "email": "<source verified email>",
  "google_cookies": "<scoped cookie jar; never logged>",
  "captcha_proxy_url": "socks5://127.0.0.1:20019"
}
```

接收端 HTTP 200，`flow_identity_verified`、`native_session_verified`、`account_active` 全部为 true，账号 50 从 Labs/禁用变为 Flow/活跃，余额 50。没有请求 Labs AT，没有伪造 AT 过期时间。

`39bd106` 容器启动后，通过正常管理 API `/api/tokens/50/refresh-at` 再次验证真实目标 Profile：HTTP 200、`success=true`、`auth_mode=flow`、`at_expires=null`。临时部署接单开关已还原为图片/视频均启用。

只核验另一旧账号 23 的既存 Cookie 和原代理：字段完整，但真实目标浏览器已无法登录 Flow，接收端明确拒绝，原账号未迁移、未启用。这证明“保存有 Cookie / 旧余额较多”不能等同于可用登录态。没有启动任何其他桌面脚本；独立登录账号 43 未覆盖。

## 生产异步链路

恢复账号后，原有 FIFO 队列开始推进；不是只运行诊断工具中的 FlowClient。

| 类型 | 对外任务 ID | 上游媒体 ID | 已核实结果 |
| --- | --- | --- | --- |
| 原用户任务，Abra R2V 10s 默认 720p | `flow2api-submit-8c69cdd8d7c640f89993a41eca672e0b` | `7d0fb8d9-aede-4625-b5a7-877b340be452` | completed / 100；余额 50 → 35 |
| 原用户任务，Abra R2V 10s 默认 720p | `flow2api-submit-958a41a71d964ac081dd03e3afa0d5c8` | `d4bdf516-bb8b-42c1-8ac5-93c0c6ebdac9` | completed / 100；余额 35 → 20 |
| 原用户任务，Abra R2V 10s 默认 720p | `flow2api-submit-f09173ce0ccd4ad49922e1ce36114b24` | `ea442426-2840-4df7-81d8-46e44012b935` | completed / 100；余额 20 → 5 |
| 自动验收，Abra R2V 4s 360p | `flow2api-submit-76db4beb64454bd8bc4a7130628f0bb0` | `20c3bc3d-8910-4a79-b5d8-37fb108dd342` | completed / 100；余额 5 → 1 |

自动验收只提交一次，先写服务器提交日志；网络异常时不自动再次付费提交。输入使用已有参考图，不另生成付费图片。保留原排队顺序，过期任务由原超时机制结束。

2026-09-11 17:05（UTC+8），通过同一生产 GET `/v1/videos/{id}` 确认 4 秒任务完成并取得视频 URL。随后下载、跨机器 SHA256 核对及完整解码通过：

- MP4，129701 字节；H.264，640×360，24 fps，96 帧。
- 视频流精确 4.000000 秒；含音轨容器总时长 4.010000 秒。
- SHA256：`d04170595589cd5a5e55c8f336e505cb8df86f4ab85d83aca0b772b122500289`。
- `ffmpeg -nostdin -v error -xerror -i canary-4s.mp4 -map 0:v:0 -f null -`：退出码 0，无解码错误。
- 自动验收消耗 4 积分；前面三条原用户队列任务合计消耗 45 积分，不计为自动测试消耗。
- 视频本地文件：工作区 `output/diagnostics/pre-flow-native-20260911/canary-4s.mp4`。仅研究验收文档入库，不提交媒体文件或凭据。

## 结论边界

4 秒生产异步任务和实际 MP4 校验已通过。不能把已成功的 R2V 推广为所有模型、所有账号都已恢复。T2V、未录制的模型和图片放大仍不能冒用旧 OAuth 请求；其余账号尚未完成新站身份验证，不能批量强制启用。新版同步插件需重新加载，旧 Labs 客户端不能覆盖已经迁移的 Flow 账号。

验收后唯一已验证活跃账号剩余 1 积分，不足以执行新的视频请求。队列继续等待有效且有额度的账号并按既有期限自动结束过期记录；这不应被误判为此次生成链路又全部报错。要恢复账号池容量，需要用新版同步端更新其余源 Profile 的有效登录态。

本地诊断脚本位于工作区 `output/diagnostics/pre-flow-native-20260911/`；原始 Cookie、连接 Token、API Key 和签名媒体 URL 不保存在此文档中。

浏览器 Secure Preferences 仅只读核对已加载插件路径；对应磁盘 manifest 为 1.2.4，不是新站专用同步构建 1.3.0。没有修改其缓存配置或加载目录。同步端回归复验：插件 17 项、tupdater 141 项通过；不等于它们已部署。
