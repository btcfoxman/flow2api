# 新站独立 Profile 与本地 FlowClient 实测（2026-09-11）

## 范围

源为用户已打开的 g9-9urm 浏览器（CDP 9226、代理 20019）。源、目标 Profile 始终分离：仅经本机一次性回环接口在内存中传递完整 scoped Cookie，目标使用新建临时数据库与 Native CDP Profile。没有修改源浏览器配置、启动其他桌面脚本、批量同步、调用生产同步接口或部署 3.5 pre。

实测脚本、只读检查及脱敏汇总位于工作区 `output/playwright/g9-9urm-flow-native-20260911/`。临时验证数据库、目标 Profile 在浏览器正常停止后自动清理；原有源 Profile 和监听日志保留。没有保存原始 Cookie、页面 XSRF、验证码或签名 URL 参数到研究文件。

## 已通过的真实验证

1. `TokenManager.sync_flow_session`：独立候选 Profile 身份核对通过，再写入临时账号；目标真实 Profile 身份/余额/项目核对通过；返回 `auth_mode=flow`、身份已验证、会话已验证、账号活跃。
2. `token.at` 为空。关闭目标浏览器后重新启动，`verify_native_session` 仍返回 true。不依赖 Labs `/auth/session`。
3. 本地 `FlowClient.generate_image` 单次 NARWHAL 文生图，square / 1024×1024，无放大；RPC `ogiZ0b` HTTP 200，浏览器 fetch 用时 13.56 秒（上游 di=13239ms）。
4. 媒体 ID `49d07833-0a4c-475c-ab8f-9d5eb34657fc`，工作流 `a06fe2e3-d6b3-4e0f-b714-417114e65892`；项目绑定、签名结果 URL 解析通过。
5. 通过相同代理下载结果：HTTP 200、image/jpeg、188491 字节。余额前后均为 50；不据此推断所有图片模型免费。
6. 在源浏览器只读查询 `Zzl0ze` 再确认项目新增了上述媒体，与本地返回 ID 一致。此操作不刷新页面或修改 UI 设置。

`Zzl0ze` 已由当前官方前端确认是 `/FlowService.GetProjectContents`。本次只读请求：

```json
["projects/PROJECT_ID", null, null, null, [1]]
```

响应字段 2 是工作流数组，字段 3 是媒体数组；只用于本次研究核对，尚未将其当作通用自动重试/补偿协议。

## 必须保留的失败记录

在上述成功之前进行过另一次独立、最多一次尝试的图片实测，返回 `AngularSubmissionUncertain`。第一次诊断未保留足够的上游响应分类，无法认定是 429、验证码拒绝还是响应/连接异常。

失败后先只读查询余额与项目内容：余额 50，项目仍只有人工操作产生的 2 个媒体，没有测试结果。随后增加脱敏响应观察，另行进行单次诊断才取得上述成功。成功后再查项目总计 3 个媒体，仅 1 个匹配测试提示词。两次诊断均未自动重放请求；第一次结果不能从统计中抹去，也不能凭第二次成功宣称故障已完全消失。

## 本轮补充修复

- Flow 账号验证码直接选择 Angular 新站页面，不依赖视频模型 opt-in，也不会选择其他验证码提供方式。
- 图片和视频提交显式携带目标账号 ID；缺失或串用浏览器上下文时在发送前停止，不回退到 OAuth HTTP。
- `flow:` 数据库身份标识不能构造成 Labs Cookie。
- 新站 HTTP 429 原异常文本为 `HTTP 429`，与既有流控分类使用的 `HTTP Error 429` 不匹配。已统一格式并补回归测试。
- 新站图片生成与视频生成均在实际绑定的账号代理出口记录风险/成功，不能只依赖视频专用 reservation 标记。
- 即使关闭 debug，`native_rpc_failed` 也记录 RPC ID、HTTP 状态、耗时和错误阶段（CDP 超时/中断、fetch 中断、bootstrap 缺失、响应无法识别、HTTP 拒绝）。不记录原始请求、响应、鉴权或代理密钥。

## 验收边界

最终回归：Flow2API 458 项、tupdater 141 项、插件 17 项通过；三库 `git diff --check` 通过。

本次证明的是单账号、本机分离 Profile、新站同步核心和本地 FlowClient 图片调用可行，不是 3.5 pre HTTP API 已恢复。生产部署、插件实际加载版本及真实目标同步回执、视频生成/轮询/下载、多账号稳定性仍需分别验证。未验证模型和图片放大不冒用旧 OAuth 接口。
