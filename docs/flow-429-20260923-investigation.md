# 3.5 pre Flow R2V 429 排查记录（2026-09-23）

## 结论与边界

**生成任务提交及视频成片均已恢复。** 3.5 pre 于 2026-09-23 05:57:08 UTC 启动修复版后，两笔真实的 Abra R2V 服务请求分别由账号 2 和 1 提交，上游接受，`generate_video` 记录 HTTP 200、`video_submitted`；两笔异步结果分别于 06:00:35、06:01:42 UTC 更新为 HTTP 200、`completed`、进度 100。修复版启动后的同类提交暂未见 429。修复前上游实际返回 HTTP 200，但 `MZZa6b` RPC 内部为 gRPC 7、`PUBLIC_ERROR_UNUSUAL_ACTIVITY`，服务将其映射为 429。当天 00:00 UTC 到修复前，3.5 pre 的视频请求日志有 148 条 429，涉及 29 个账号；没有 200。前一天相同公开 Abra R2V 型号的服务请求有 203 条 200。

根因定位在验证码调用：官网当前脚本会把页面公开的 `grecaptcha.enterprise.execute` 包装成强制使用 `extension_hijack_detected` action 的函数，同时把原始函数保存在页面自身的闭包中，供官网生成流程调用。旧服务调用公开函数，传入 `VIDEO_GENERATION` 也会被改写为错误 action。修复版通过 CDP 读取官网保存的原始函数，并在同一页面调用它；闭包形态不符时直接报错，停止生成 RPC。官网判断异常的全部内部规则不可见，但修复前后的真实服务结果支持这一原因。

## 受控对照

| 环境 / 操作 | 项目与请求 | 结果 |
| --- | --- | --- |
| 本机 Chrome 153，账号 52 的同一代理出口，手动网页提交 | 4 秒 360p R2V | `MZZa6b` 接受 |
| 3.5 旧服务资料目录，Chrome 154 副本，手动网页提交 | 4 秒 360p R2V | gRPC 7，`UNUSUAL_ACTIVITY` |
| 3.5 全新 Chrome 154 资料目录，网页登录、浏览器级登录关闭，手动网页提交 | 4 秒 360p R2V | 接受 |
| 3.5 新资料目录副本，手动网页提交 | 8 秒 360p，原测试项目，1 张参考图 | 接受 |
| 同一副本，手动网页提交 | 8 秒 360p，服务实际项目，1 张参考图 | 接受 |
| 同一副本，手动网页提交 | 8 秒 720p，服务实际项目，1 张参考图 | 接受；上游模型键 `abra_r2v_8s`，请求项长度 6 |
| 3.5 服务自动提交，账号 52 已换为新资料目录 | 8 秒 720p、4 秒 720p R2V | 05:02、05:06、05:14 UTC 三次均为 gRPC 7 / 429 |

网页 720p 成功请求与服务 `build_video_rpc` 的模型键、参考图数量及请求项长度相符。服务失败请求没有完整脱敏的网络请求抓包，不能据此断言验证码或请求头完全相同。实验未保存提示词、素材、Cookie、验证码或访问令牌。

只读补查：成功资料目录的官网项目页后台请求，其 `_reqid` 依次为 49200、149200、249200 等，间隔 100000；服务的 `_reqid` 增量与此相同，因此没有据此改动请求编号逻辑。这项检查没有提交生成任务。

当前官网项目页加载的 reCAPTCHA `render` 网站密钥与服务固定值一致。源码检查确认 Flow 账号获取验证码与发送生成 RPC 均使用同一 Angular 项目页；因此没有证据支持“旧网站密钥”或“验证码来自 Labs 页面”这两种假设。

进一步只读验证：官网静态脚本中生成服务调用 `Mm("VIDEO_GENERATION")`，该方法使用保存的原始函数；项目页公开的 `execute` 函数实际包含 `extension_hijack_detected`。在 3.5 的已登录 Chrome 页面中，CDP 通过该原始函数取得了非空 `VIDEO_GENERATION` 验证码，未发送生成 RPC，也未记录验证码内容。

## 已实施

- `generation_handler.py` 将明确的 `UNUSUAL_ACTIVITY` 判决传给任务队列；`routes.py` 将此类任务的队列提交轮数限制为 3，避免单任务反复撞击上游。
- `browser_captcha_native_cdp.py` 移除了 JS `navigator.webdriver` getter 覆盖、两个多余的 Chrome 禁用参数，以及脚本派发的焦点、鼠标移动和滚动动作。[`dispatchEvent()` 产生的事件标记为非可信](https://developer.mozilla.org/en-US/docs/Web/API/Event/isTrusted)。此清理单独实施后服务仍返回 429，因此不能把恢复归因于这些清理。
- `browser_captcha_native_cdp.py` 新增官网验证码原始函数调用路径，并对包装函数及闭包结构做校验；结构变化时不回退到已知错误 action 的公开函数。已通过单元测试和 3.5 Chrome 的不扣分验证码实测。
- 3.5 pre 使用本地镜像 `ghcr.io/btcfoxman/flow2api:pre-recaptcha-site-binding-20260923` 和官方 Chrome 154。镜像仅在该主机，未推送仓库。健康检查 HTTP 200；源码 SHA256 前 20 位为 `5214f425439a57503356`。原 `.env` 备份在 `/home/btcfoxman/docker/flow2api/recaptcha-site-binding-20260923/.env.before`。
- 曾尝试按新登录资料目录时间清除旧 429 冷却；服务继续出现 429 后已回退，继续保留原有风险冷却。
- 账号 52 的成功网页登录资料目录已复制到 `/app/tmp/native_cdp_profiles/token-52`；旧目录备份在同一父目录的 `token-52.backup-20260923T045945Z`。账号 52 当前 `is_active=0`、本地积分记为 0。用户确认网页积分已耗尽，不再发起付费测试。

本地相关测试：针对浏览器、Angular、队列与账号切换的 `67 passed`；`git diff --check` 通过。修复版启动后两笔服务提交分别于 05:58:41 与 05:59:04 UTC 记录 HTTP 200 / `video_submitted`。

## 后续观察

继续观察后续服务任务是否持续返回 200。官网脚本更新后，CDP 闭包结构可能改变；此时服务会报告 `captcha_site_binding_unavailable`，需要重新核对官网验证码调用。账号 52 因积分耗尽仍保持停用，不参与提交。
