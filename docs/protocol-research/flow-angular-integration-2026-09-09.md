# Flow 新站协议迁移：三项目修复与验收记录

日期：2026-09-09（Asia/Taipei）。本记录区分“代码修复已回归”“源浏览器实测成功”和“跨 Profile/线上环境未验收”。不得据此宣称 3.5 pre 的 429 已全部解决。

## 分析结论

1. 网页主域已从 Labs 迁移至 `flow.google.com`。旧 Labs NextAuth session 仍用于兼容 REST，但不是新站 Google/Flow 登录凭据。
2. 两个同步端原先最终只推送 Labs session；已有插件代理补丁并未补齐新站 Cookie。同步器的纯协议刷新同样不能证明目标浏览器已登录 Flow。
3. 同出口代理是必要的链路一致性要求，但不足以证明跨 Profile 会话可迁移。Cookie 配置已保存、页面已登录、上游接受请求是三个不同检查点。
4. 新 RPC 依赖当前页面 bootstrap（`SNlM0e` / `cfb2h` / `FdrFJe`），不能静态同步 `at`、`f.sid` 或复用 reCAPTCHA。页面 `at` 与 OAuth access_token 不同。
5. 原协议建议中的“失败自动回退 REST”存在重复生成/扣费风险，已更正：提交超时、响应无法识别等未知结果，不自动重提或切换传输。
6. 已完成状态不等于可交付下载地址。直接拼接 `flow-content.google/video/{id}` 实测 403，必须保留上游返回的签名 URL。

原始分析基线见 [手工流程记录](flow-angular-capture-2026-09-08.md) 和 [脱敏结构](flow-angular-rpc-2026-09-08.json)。其中“当前代码差距”描述修复前基线，以本记录为修复后状态。

## 已落地的变更

| 项目 | 本轮修复 |
| --- | --- |
| Flow2API-Token-Updater 1.2.0 | 新域权限；当前 Cookie store 内收集带作用域的 Google/Flow Cookie；Labs 分片合并；同步互斥、页面/请求超时；校验服务端接收确认；保留 Profile 本地代理字段 |
| flow2api | 数据库自动迁移 `google_cookies`；兼容旧请求不覆盖 Cookie；普通 Token 输出不包含新增凭据；native CDP 导入、分片种植、独立快照指纹；新域项目页和登录前置检查；已认证 Profile 例行/风险重启保留本地数据；Angular adapter、轮询和签名下载地址；新 resumable upload |
| flow2api_tupdater | 默认浏览器刷新并补齐 Flow 登录；重新读取刷新后的 Cookie；显式区分源代理/目标代理；目标接收确认；协议登录导入完整 Google seed 而非仅 Labs session；操作锁覆盖浏览器补齐；列表不返回 Google Cookie |

### 同步请求契约

`POST /api/plugin/update-token`，现有 Bearer connection token 不变：

```json
{
  "session_token": "<Labs session，仍必需>",
  "captcha_proxy_url": "socks5://<目标可访问的同出口地址>:20001",
  "google_cookies": [
    {"name":"SID","value":"<redacted>","domain":".google.com","path":"/","secure":true},
    {"name":"__Secure-OSID","value":"<redacted>","domain":"flow.google.com","path":"/","secure":true}
  ]
}
```

上例只示意字段，不能作为真实最小登录 Cookie 集。实际传输源 Profile 的完整受支持 Cookie 数组，保留 expiry/httpOnly/sameSite。只接受 Google/Flow 白名单域名；拒绝无 domain 的 Cookie header、非法字段和无法保留分区语义的 Cookie。

响应新增/沿用：`cookies_updated`、`cookies_configured`、`flow_cookies_configured`、`cookie_count`、`proxy_updated`、`proxy_configured`。

- 确认字段只表示已保存合法配置，不代表上游登录或账号身份校验通过。
- 旧客户端省略 `google_cookies`，保留目标现有 Cookie；只更新 Labs session 时不会把旧 Google 快照再次覆盖浏览器当前 Cookie。
- 新版插件要求完整接收确认；旧服务端静默忽略字段将明确报错。
- 同步器 `proxy_url` 是源浏览器代理；新增 `captcha_proxy_url` 是目标地址，留空保留目标账号已有配置。目标账号必须已有代理或显式设置，不能默认把另一台机器的 localhost 复制过去。
- 常规列表隐藏原始新增 Cookie；数据库、备份、显式 Cookie 导出仍属于敏感凭据存储，应限制访问。

### 传输路由与对外兼容

现有公开模型名、请求参数、同步/异步任务格式不变。Abra 参考图与视频编辑的 360p/720p 共用各自模型族的新协议编码，不把 720p 映射为 360p。未启用新协议的请求，以及尚未适配的模型族，保留既有 REST 路由。

在 `[flow]` 中按模型显式灰度启用新视频 RPC：

```toml
# 两个开关默认都为 []；实际执行端登录态和只读请求验收通过后再灰度启用
# 按族启用：参考图覆盖 4/6/8/10 秒的 360p 与 720p；编辑覆盖两种分辨率
angular_video_families = ["abra_r2v", "abra_edit"]
# 或只按精确模型启用。两种配置取并集；留空 families 才是仅按精确模型启用
angular_video_models = ["abra_r2v_4s_360p"]
native_video_upload = true
```

精确模型配置保留原有含义：只配置 `abra_r2v_4s_360p` 不会顺带启用 720p。`abra_r2v_4s_720p` 等公开别名与无后缀 720p 上游键等价；编辑同理。未知时长/分辨率和 T2V、I2V、首尾帧不会因前缀相似而被模型族开关自动接管。

`veo_3_1_r2v_fast_portrait` 保留已抓包结构，仅能按精确模型启用。只有 `abra_r2v_4s_360p` 完成本轮新增扣费生成实测；720p 的编码已通过当前官网脚本与离线回归核对，尚未新增扣费实测。种子等未验证字段仍不能自行类推，新适配器不是全部模型迁移完毕。`ogiZ0b` 图片协议已记录但未启用；native 图片 REST 提交已改为使用同一浏览器。

新视频上传仅在 `native_cdp`、开关开启且账号具有 Flow Cookie 配置时使用；其他情况保留旧上传。完成新上传后不再追加旧 offset/workflow 初始化操作。

Angular 任务将 `transport` / `tokenId` / `projectId` 持久化在 operation 中，后续轮询不因 feature flag 改变而改用其他账号或 REST。未知状态不计成功。成功 URL 优先读取 `media[7][rendition][8]`，缺失时补查 `as29s`，不伪造无签名 URL。

### 360p / 720p 共用编码修正（2026-09-09）

原适配器错误地将“只抓到 360p 示例”等同于“只支持 360p”，并把 `[4]` 和比例 `1` 写死。现改为按已支持的模型族解析、规范化上游键，再用同一个请求构造器处理分辨率和比例。`MODEL_CONFIG` 中已有的时长、计费和公开别名不变。

本轮读取当前网站公开 JavaScript，不携带 Cookie 下载脚本，不提交生成。可复核来源：

- [主模块 wO1vlb](https://www.gstatic.com/_/mss/boq-labs-ai-sandbox/_/js/k=boq-labs-ai-sandbox.AiSandboxAngularFrontend.zh_CN.YchTg_GTHpg.2018.O/ck=boq-labs-ai-sandbox.AiSandboxAngularFrontend.qbEsQyWqiYE.L.B1.O/d=1/exm=_b/excm=_b/ed=1/br=1/wt=2/ujg=1/rs=APwUF7xzU0peyvnEAlzKc01uf8TyO1H5_g/ee=Pjplud:PoEs9b;QGR0gd:Mlhmy;ScI3Yc:e7Hzgb;YIZmRd:A1yn5d;cEt90b:ws9Tlc;dowIGb:ebZ3mb/dti=1/m=wO1vlb)：3,429,153 字节，SHA-256 `ba5cc4c17eb558ee9e967803c1888e338ba357ac2ee8613ca4cd419696a780c1`。
- [项目模块 XRV0Af](https://www.gstatic.com/_/mss/boq-labs-ai-sandbox/_/js/k=boq-labs-ai-sandbox.AiSandboxAngularFrontend.zh_CN.YchTg_GTHpg.2018.O/ck=boq-labs-ai-sandbox.AiSandboxAngularFrontend.qbEsQyWqiYE.L.B1.O/d=1/exm=_b,wO1vlb/excm=_b/ed=1/br=1/wt=2/ujg=1/rs=APwUF7xzU0peyvnEAlzKc01uf8TyO1H5_g/ee=Pjplud:PoEs9b;QGR0gd:Mlhmy;ScI3Yc:e7Hzgb;YIZmRd:A1yn5d;cEt90b:ws9Tlc;dowIGb:ebZ3mb/dti=1/m=XRV0Af)：1,833,126 字节，SHA-256 `920f82496f5bb4e7250a61327b127ca265b38a1b0dc517151f51c6ce1edfc10c`。

| 核对点 | 当前官网实现 | 适配器行为 |
| --- | --- | --- |
| 分辨率枚举 | `BK` 映射 360p→4、720p→1；生成构造器仅在非默认分辨率时填 OutputSpec | 360p 填 `[4]`；720p 按官网省略默认字段，不附加 360p 尾部 |
| 参考图生成 | `MZZa6b`，`BatchAsyncGenerateVideoReferenceImages`，OutputSpec 为字段 12 | 同一个 RPC 处理所有已支持时长的 360p/720p，数组索引 11 |
| 视频编辑 | `jIps6`，`BatchAsyncGenerateVideoEditVideo`，OutputSpec 为字段 13 | 同一个 RPC 处理 360p/720p，数组索引 12；保留输入视频和帧范围 |
| 视频比例 | `gAb` 将横屏映射为 2、竖屏映射为 1 | 按请求传递；Abra 无参数时沿用公开模型横屏默认，已采集 Veo portrait 默认竖屏 |

模型键与 `outputSpec.resolution` 冲突、未知比例/输出字段在提交前报错，不静默降级，不自动补发 REST。新增回归覆盖 15 个公开模型键/别名、两种比例、精确模型开关兼容、模型族开关、同账号绑定及未知结果禁止重提；不额外声称种子或其他未适配模型已验证。

保持用户要求的分离部署。本轮不推进目标端重新登录或源浏览器远程执行方案，不修改两个同步端的 Cookie/代理契约；其余跨 Profile 会话限制仍未解除。

本轮回归：Flow2API 全量 `295 passed`（18.78 秒）；模型与路由专项 `73 passed`。未消耗积分，未修改线上配置，未提交/push。同步器和插件本轮未改动，沿用前轮验收结果。

## 验证结果

- Flow2API 全量测试：276 passed（含数据库迁移、Cookie 隐私/兼容、native 状态保留、位置数组、上传脚本、签名 URL、安全重试）。
- 同步器全量测试：33 passed。插件 Node 测试：3 passed。
- JavaScript 语法检查与三个仓库 `git diff --check` 通过；Git 的 CRLF 提示不属于测试失败。
- Windows 测试存在已有 pytest-asyncio 默认 loop scope 弃用提示，不影响本次通过结果。

### 实际网站验证（不是 3.5 pre 部署验证）

源浏览器：`g1-1hua.bat` 对应 CDP 9226，已打开的 Flow 项目。日志仅记录结构、状态和必要 ID，不保存 Cookie、页面令牌或签名参数值。

1. 原 Profile 项目 `ngNC2`：HTTP 200，页面动态 bootstrap 可用，RPC 解码成功。
2. 明确 opt-in 的一次 `abra_r2v_4s_360p` 参考图生成：HTTP 200，经历 ACTIVE 后 SUCCESSFUL；余额 16 → 12，消耗 4 积分。媒体 ID `47683e42-7309-4f19-92d7-fef0c52cd823`。失败的前置素材检查没有发出另一笔生成请求。
3. 签名地址读取：Range 0–31 返回 HTTP 206、`video/mp4`、32 字节，并验证 MP4 `ftyp` 文件头。未暴露签名 URL。
4. 跨 Profile 只读测试：完整 scoped Cookie 导入临时 headless native CDP Profile、同代理，首次项目查询 HTTP 401。后续检查 Cookie 名/domain/value 一致性未发现导入丢失，但目标页转到 `/about`；随后源旧页查询也返回 401，源 Profile 新页面缺少登录 bootstrap。
5. 已停止重复跨 Profile 测试，等待用户重新登录源 Flow。临时浏览器/探测标签页已关闭，未修改源页面导航，也未再提交扣费任务。

因此，第 1–3 项证明已登录源浏览器的新 RPC 提交/查询/下载链可用；第 4 项没有通过，不能把它包装为“跨服务器完整登录已验证”。现有时间顺序不能单独判定是服务端撤销、源会话自然失效、风险重认证或设备绑定所致。

Google 官方说明 Chrome 支持设备绑定会话，刷新可能依赖不可导出的设备密钥；Cookie 字节一致不等于设备/会话状态等价。首轮尚无本账号的直接证据；重新登录后的复验已确认当前源 Profile 存在相关绑定，详见下一节，但这仍不能单独定性历史 429。[Chrome 文档](https://developer.chrome.com/docs/web-platform/device-bound-session-credentials)、[Google 2026-04 说明](https://blog.google/security/protecting-cookies-with-device-bound-session-credentials/)。不得以关闭或绕过会话保护作为修复方案。

### 重新登录后复验（2026-09-09 02:20，Asia/Taipei）

用户重新登录后，原 Profile 停在 Flow 首页。本轮仅在原 Profile 做读查询，未复制 Cookie、未创建另一个 Chromium Profile、未发起生成、未消耗积分。

| 检查 | 结果 |
| --- | --- |
| 首页 `UpteDb` 项目列表 | HTTP 200、RPC 解码通过、页面 bootstrap 完整 |
| 同 Profile 临时标签页 `ngNC2` 原项目读取 | HTTP 200 |
| `as29s` 原成功媒体元数据 | 解码通过，包含签名视频地址 |
| 原媒体 Range 下载 | HTTP 206、video/mp4、32 字节、MP4 文件头通过 |
| 检查结束后的源页查询 | HTTP 200，会话仍有效 |

通过当前 Chrome 暴露的 `Network.enableDeviceBoundSessions` 订阅只读调试事件（不是开启/关闭安全功能），收到了初始绑定会话列表：

- `accounts.google.com/RotateBoundGaps`：对应 `__Host-GAPSTS`。
- `accounts.google.com/RotateBoundCookies`：对应 `__Secure-1PSIDTS`、`__Secure-3PSIDTS`、`__Secure-1PSIDRTS`、`__Secure-3PSIDRTS`。

同时匹配 Flow 项目列表 RPC 的 `Network.requestWillBeSentExtraInfo.deviceBoundSessionUsages`：一项 `NotInScope`，另一项 **`InScopeRefreshNotYetNeeded`**。这直接证明本次 Flow 请求处于一个设备绑定会话的作用范围内，当前暂不需要刷新；不是仅凭浏览器版本或数据库文件名推测。

上述事件中的 session ID、cachedChallenge、Cookie 值、请求头和签名参数均未输出/写入记录。探针只保留域名、刷新端点路径、Cookie 名称和使用状态枚举。接口定义见 [Chrome DevTools Protocol](https://chromedevtools.github.io/devtools-protocol/tot/Network/#method-enableDeviceBoundSessions)。

修复方向需要据此收紧：**当前 Profile 的 Cookie 同步不能承诺恢复另一 Profile 的完整设备绑定会话**。目标应该独立完成登录并保留自己的持久化认证状态，或通过受保护通道在源已认证 Profile 内执行 RPC；不能用复制 Storage/私钥或停用保护替代。已有 Cookie 同步字段仅解决凭据传输缺口，跨 Profile 登录验收仍未通过。之前 401 与历史 429 的完整因果关系仍需目标侧对照证据。

本轮代码只完善诊断工具：支持登录后的首页、可显式指定已有项目、缺少 Flow 标签页时不再抛 StopIteration；新增只读 DBSC 元数据及请求关联检查。新增隐私/目标选择回归测试。Flow2API 全量测试更新为 281 passed；同步器和插件本轮未改动，沿用此前 33/3 项通过结果。

## 升级与后续验收

1. 先备份并升级 Flow2API 服务端，启动时自动新增 Cookie 字段；新 RPC 暂保留 `angular_video_models = []`、`angular_video_families = []`。
2. 升级插件并重新授权；升级同步器，默认 `FLOW_PROTOCOL_REFRESH_ENABLED=false`。各 Profile 完成 Flow 登录并手动同步一次。
3. 核对源/目标实际公网出口、目标代理可达性、配置确认字段；在目标持久化 native Profile 做项目只读查询。401/跳转登录不得归类为流量 429。若目标不能继承会话，需要在目标完成受支持登录，或另行设计在源已认证 Profile 执行请求的方式，不能承诺仅加 Cookie 就可跨机长期使用。
4. 目标读请求验收通过后，小范围开启已确认模型；验证完整 API 输入素材→提交→轮询→下载，再逐步扩大。用相同时间段/模型/素材统计源同步方式、上游 HTTP 状态、账号/代理绑定、明确成功/失败终态。
5. 继续补采失败/取消枚举、T2V、首尾帧、720p 扣费生成实测、不同编辑帧率、批量输出。360p/720p 编码一致性已核对，但现有成功单样本不能替代 3.5 pre 线上失败率对照。

可复用工具：`tools/probe_flow_angular.py` 默认只读。`--canary-reference` 才会生成；`--verify-native-proxy` 会创建临时本地 Profile，必须在源已登录且明确同出口时使用；`--fresh-tab` 只打开并关闭自有探测标签页。已失效源会话会先行停止，不继续测试导入/生成。

本轮没有 push、部署或修改线上实例配置。
