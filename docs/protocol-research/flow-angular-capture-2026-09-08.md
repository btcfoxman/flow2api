# Flow Angular 浏览器协议观察（2026-09-08）

## 结论

本次人工实操确认 Flow 网页端已发生一次协议层迁移：从 `https://labs.google/fx/...` 打开的旧入口最终落在 `https://flow.google.com/project/{project_id}`，实际生成、查询和项目加载均通过 `flow.google.com/_/AiSandboxAngularFrontend/data/batchexecute` 完成。本次会话没有观察到旧网页端的 `labs.google/fx/api` 或 `aisandbox-pa.googleapis.com/v1` 生成请求。

现有 `flow2api` 中已支持本次出现的三个视频模型键：

- `abra_r2v_4s_360p`
- `abra_edit_360p`
- `veo_3_1_r2v_fast_portrait`

因此模型目录不是本轮的主要缺口。主要缺口是域名和认证状态、Angular RPC 信封、视频上传协议、XSSI 响应解析，以及浏览器插件仍只允许 `labs.google`。

本次人工提交全部成功，没有出现 HTTP 400、429 或 5xx。它可以作为“同一浏览器 Profile + 同一代理出口”下的成功基线，但不能单独证明线上 429 已消失。

## 采集范围

- 浏览器启动脚本：`g1-1hua.bat`
- Chrome Profile：`D:/tmp/g-41`
- Profile 代理入口：`socks5://127.0.0.1:20001`
- Chrome：`152.0.7977.66`
- 采集时间：2026-09-08 22:50:12 至 23:18:00（Asia/Taipei）
- 最终页面：`https://flow.google.com/project/{project_id}`
- 原始脱敏记录：`output/playwright/g1-1hua-20260908-224529/`

原始目录受 `.gitignore` 保护。Cookie、反 CSRF 值、reCAPTCHA token、上传会话 URL、签名媒体 URL 等均只保留长度和哈希或固定占位符，禁止把浏览器原始凭据提交到仓库。

## 人工操作与结果

| 操作 | 上游模型/RPC | 输入 | 结果 | 约耗时 |
| --- | --- | --- | --- | ---: |
| 参考图生成 360p/4 秒视频 | `abra_r2v_4s_360p` / `MZZa6b` | 1 张参考图 | 成功 | 29.5 秒 |
| 上传本地视频 | `/upload/v1/flow/upload/video/{project_id}` | 720×1280、7.4 秒、531806 字节 | 成功 | 两阶段上传 |
| 视频编辑 360p | `abra_edit_360p` / `jIps6` | 1 个视频 + 1 张参考图 | 成功 | 64.2 秒 |
| 图片换场景 | `GEM_PIX_2` / `ogiZ0b` | 1 张输入图 | 两个同步响应均成功 | 20.1/35.0 秒 |
| 两张参考图生成视频 | `veo_3_1_r2v_fast_portrait` / `MZZa6b` | 2 张参考图 | 成功 | 39.5 秒 |

上述耗时按 Chrome Network Timing 的请求起点到终态轮询起点计算。视频提交响应本身约 2.8～3.5 秒，随后约每 5 秒轮询一次。

积分字段在三个视频提交/终态响应中依次观察到 `46 -> 36 -> 16`，与 4 点、10 点、20 点的本次消耗相符。该字段是响应位置字段，余额为零时是否仍省略需另行复验。

## 网络概览

- 请求 203 个，响应 205 个；监听接入时存在少量在途响应，所以二者不要求完全相等。
- HTTP 状态：`200` 162 个、`204` 39 个、`206` 4 个；所有已收到的 HTTP 响应均为 2xx。
- 48 个 `request_failed` 全部是 `net::ERR_ABORTED`：39 个 Google Analytics、7 个 reCAPTCHA 辅助请求、2 个页面切换/播放产生的媒体请求。
- `ERR_ABORTED` 是浏览器主动取消，不可计入上游生成失败或 429。
- `batchexecute` 共 76 次。

主要域名和用途：

| 域名 | 用途 |
| --- | --- |
| `flow.google.com` | 页面、Angular RPC、视频上传 |
| `flow-content.google` | `/image/{media_id}`、`/video/{media_id}` 媒体读取 |
| `www.google.com` | reCAPTCHA Enterprise anchor/reload/clr |
| `accounts.google.com` | `RotateCookiesPage` 和 Cookie 轮换 |
| `www.gstatic.com` | Flow 静态资源和前端 bundle |

## Angular RPC 信封

观察到的请求模板：

```http
POST /_/AiSandboxAngularFrontend/data/batchexecute
  ?rpcids={rpc_id}
  &source-path=/project/{project_id}
  &bl={boq_build_id}
  &f.sid={page_session_id}
  &hl=zh-CN
  &_reqid={monotonic_request_id}
  &rt=c
Host: flow.google.com
Origin: https://flow.google.com
Referer: https://flow.google.com/
Content-Type: application/x-www-form-urlencoded;charset=UTF-8
X-Same-Domain: 1

f.req={serialized_positional_rpc}&at={page_anti_csrf_token}
```

本次页面生命周期内 `bl`、`f.sid`、`source-path` 和 `at` 各自保持一个值，`_reqid` 每次请求变化。`bl` 的实测值含前端发布日期，必须从页面 bootstrap 动态解析，不能写死。

这里的表单字段 `at` 是页面反 CSRF 值，不是当前 `flow2api` 数据库中的 OAuth access token `AT`。新 RPC 还携带 Google 一方 Cookie 和 Chrome client hints。最稳妥的调用方式是在对应账号的 native CDP 页面上下文中执行，避免把 Cookie、reCAPTCHA 和代理出口拆到不同会话。

响应采用 Google XSSI/长度分帧格式：

```text
)]}'

{byte_length}
[["wrb.fr", "{rpc_id}", "{nested_json_string}", ...]]
```

解析器需要先剥离 XSSI 前缀和长度帧，再解析外层 JSON，最后再次解析 `wrb.fr[2]` 内的 JSON 字符串。

## RPC 映射

| RPC ID | 次数 | 观察到的职责 |
| --- | ---: | --- |
| `MZZa6b` | 2 | 参考图视频提交；同时承载 Abra 与 Veo 模型 |
| `jIps6` | 1 | 视频编辑提交 |
| `ogiZ0b` | 2 | 图片编辑/生成，内部模型 `GEM_PIX_2` |
| `jwpduf` | 30 | 按一个或多个 media ID 轮询，约 5 秒一次 |
| `as29s` | 21 | 按 media ID 获取媒体元数据 |
| `UpteDb` | 3 | 项目列表，实测参数 `['projects/*', 21, ..., [1]]` |
| `ngNC2` | 1 | 加载 `tools/PINHOLE/projects/{project_id}` |
| `WuwhI` | 14 | 页面/上传/生成埋点，不属于生成主链 |

另有初始化响应 RPC `maseQ` 等因为监听接入时请求体已超过原采集上限，只捕获到响应 ID，暂不能形成可靠请求结构。

### `MZZa6b`：参考图视频提交

以下为位置结构，省略值均必须保留 `null` 占位：

```text
[
  [[
    [null, null, [[[PROMPT]]]],
    [[null, REFERENCE_MEDIA_ID], ...],
    MODEL_KEY,
    1,
    null,
    [null, null, null, null, CLIENT_UUID_1, CLIENT_UUID_2],
    ...,
    [4]
  ]],
  [null, 22, null, null, null, PROJECT_ID,
   null, null, null, null, [RECAPTCHA_TOKEN, 1]],
  [REQUEST_UUID, 2]
]
```

`abra_r2v_4s_360p` 请求末尾观察到 `[4]`；`veo_3_1_r2v_fast_portrait` 请求在 client UUID tuple 后结束，不能把 Abra 的尾部字段无条件套给 Veo。

### `jIps6`：视频编辑提交

```text
[
  [[
    [null, INPUT_VIDEO_MEDIA_ID, 0, 178],
    [null, null, [[[PROMPT]]]],
    "abra_edit_360p",
    1,
    [null, null, null, null, CLIENT_UUID_1, CLIENT_UUID_2],
    null, null, null,
    [[null, REFERENCE_IMAGE_MEDIA_ID]],
    null, null, null,
    [4]
  ]],
  PROJECT_CONTEXT,
  [REQUEST_UUID, 2]
]
```

输入视频为 7.4 秒，位置值为 `178`，与按 24 fps 取整的帧数吻合；这只是单样本推断，集成前还需用其他帧率/时长复验。

### `ogiZ0b`：图片编辑

```text
[
  null,
  [[
    null,
    null,
    [[INPUT_IMAGE_MEDIA_ID, null, null, null, 1]],
    SEED,
    3,
    "GEM_PIX_2",
    null,
    PROJECT_CONTEXT,
    [[[PROMPT]]],
    null, null, null,
    CLIENT_UUID_1,
    CLIENT_UUID_2
  ]],
  1,
  PROJECT_CONTEXT,
  [[REQUEST_UUID]]
]
```

### 查询

```text
jwpduf: [null, null, [[MEDIA_ID], [MEDIA_ID], ...]]
as29s:  [MEDIA_ID]
ngNC2:  ["tools/PINHOLE/projects/{project_id}"]
```

从同一媒体的提交、轮询和页面结果推断，媒体元数据中的位置状态数组发生 ` [6] -> [2] -> [3] `：分别对应已接受/排队、处理中、成功终态。用户上传媒体观察为 `[1]`。这是抓包推断，并非官方枚举，失败/取消状态仍需补样本。

## 新视频上传协议

现网页端使用 Google resumable upload，两次均为 `POST`：

1. 启动会话：

```http
POST /upload/v1/flow/upload/video/{project_id}
Content-Length: 0
Slug: {percent_encoded_filename}
X-Goog-Upload-Command: start
X-Goog-Upload-Header-Content-Length: {bytes}
X-Goog-Upload-Protocol: resumable
```

响应头返回 `x-goog-upload-url` / `x-goog-upload-control-url`、`x-goog-upload-status: active` 和 `x-goog-upload-chunk-granularity: 1048576`。

2. 上传并结束：

```http
POST {x-goog-upload-url}
X-Goog-Upload-Command: upload, finalize
X-Goog-Upload-Offset: 0
Content-Length: {bytes}

{binary video body}
```

最终 JSON 使用顶层 `mediaId`，并返回 `media`、`workflow`、视频尺寸和时长。现有 `flow_client.py` 使用 `/upload-video?action=start|upload`、自定义 `x-upload-*`、PUT 分块并读取 `mediaServerId`，与本次网页协议不兼容。

## Cookie、Storage 与风险边界

`flow.google.com` 的 RPC 实测携带：

- host cookie：`OSID`、`__Secure-OSID`；
- `.google.com` 登录 cookie：`SID`、`SSID`、`HSID`、`APISID`、`SAPISID` 和多组 `__Secure-*`；
- 高频轮换 cookie：`SIDCC`、`__Secure-1PSIDCC`、`__Secure-3PSIDCC`；
- 页面反 CSRF `at` 与 reCAPTCHA token。

在 28 分钟内，三组 `*SIDCC` 各观察到 69 个版本，`*SIDTS/*SIDRTS` 各观察到 4 个版本。`accounts.google.com/RotateCookiesPage` 的下一次轮换时间也更新了 4 次。因此不能把首次采集的一份 Cookie 字符串长期静态复用。

旧 `labs.google` 的 `__Secure-next-auth.session-token`、CSRF 和 callback Cookie 仍留在 Profile 中，但它们没有作为 Cookie 发往 `flow.google.com`。这说明：

- 旧 session token 仍可能继续服务现有 REST token exchange；
- 仅同步这个 session token，不足以在另一台机器上完整复现新 Angular 网页会话；
- 若采用新 RPC，应该同步安全封装的域名化 Cookie jar，或直接让 RPC 在已登录且绑定代理的 native CDP Profile 中执行。

与风控相关的状态包括 `flow.google.com` 的 `_grecaptcha`，以及 reCAPTCHA frame 中的 `rc::a`、`rc::f`、`rc::c`。本次每次生成前都观察到新的 reCAPTCHA reload；token 必须一次一取，并和项目页 Cookie、页面 `at`、`f.sid`、User-Agent、代理出口处于同一会话边界。

主要 UI localStorage：

- `flow-prompt-box-settings`：包含 `aspectRatio`、`Ki`、`rt`、`mode`、`Jp`、`gB`、`vy`；
- `flow-video-upload-consent-dismissed`：本次从 `false` 变为 `true`；
- `flow-tile-grid-size`、`flow-add-menu-sort-order`、侧栏/主题设置。

其中 `Ki`/`rt` 是 UI 选择别名，提交体仍使用 `GEM_PIX_2`、`abra_*`、`veo_*` 精确上游键；未证实的短字段不能直接当作公开 API 参数。

## 与 2026-08-31 记录的版本差分

| 项目 | 2026-08-31 | 2026-09-08 |
| --- | --- | --- |
| 网页主域 | `labs.google/fx/...` | `flow.google.com/project/...` |
| 生成传输 | REST JSON 到 `aisandbox-pa.googleapis.com/v1` | Angular `batchexecute` 位置数组 |
| 视频提交 | `video:batchAsyncGenerateVideo*` | `MZZa6b` / `jIps6` |
| 查询 | `video:batchCheckAsyncVideoGenerationStatus` | `jwpduf` / `as29s` |
| 网页认证 | Labs NextAuth session + OAuth AT | Google Cookie + 页面 `at` + reCAPTCHA |
| 视频上传 | Labs `/upload-video?action=...` | `/upload/v1/flow/upload/video/{project_id}` + `x-goog-upload-*` |
| 媒体读取 | Labs/API 返回媒体地址 | `flow-content.google/image|video/{media_id}` |
| 模型键 | Abra 360p/720p 等 | 本次抽样模型键保持兼容 |

结论是“模型能力连续、网页传输重构”。现有 REST 链仍可作为兼容后端，Angular 链应作为独立 adapter 灰度引入，不能在没有回退能力时整体替换。

## 当前代码差距

1. `config/setting_example.toml` 和 `src/services/flow_client.py` 默认仍使用 Labs/REST。
2. `src/services/browser_captcha_native_cdp.py` 的项目页常量仍是 `https://labs.google/fx/zh/tools/flow`；当前依赖重定向进入新站，增加一次跨域跳转和 Cookie 轮换风险。
3. `extension/manifest.json`、`extension/background.js` 只授权/打开 `labs.google`。页面重定向到 `flow.google.com` 后，Manifest V3 注入权限不完整。
4. 独立 `Flow2API-Token-Updater` 插件同样只授权 Labs，并且虽然枚举 `.google.com` Cookie，最终请求体只上传 `session_token` 和 `captcha_proxy_url`；它不能完整承载新 RPC 所需的 Cookie/page bootstrap 状态。
5. `src/services/flow_client.py` 的视频上传 URL、方法、header 和 `mediaServerId` 响应字段均与新网页协议不同。
6. 当前响应解析器主要面向 REST JSON，尚无 XSSI 长度帧、`wrb.fr` 二次 JSON 和位置状态解析。

## 建议集成顺序

1. 先做低风险兼容：native CDP 直接打开 `https://flow.google.com/project/{project_id}`；两个插件增加 `https://flow.google.com/*` 权限并保留 Labs 权限作为回退。
2. 增加独立 `flow_angular` adapter，继续保留现有 REST adapter。adapter 从真实页面动态读取 `bl`、`f.sid`、`at`，并在同一 Profile/代理内发起请求。
3. 优先实现 XSSI 解帧、`jwpduf` 查询和新 resumable upload，再实现 `MZZa6b`、`jIps6`、`ogiZ0b` 的模型分支位置编码。
4. 调度层按账号固定 Profile + 固定代理出口；reCAPTCHA token 不跨账号、不跨出口、不跨提交重用。
5. 使用 feature flag 和单账号 canary，先以 360p/4 秒最低消耗任务验证。不能在提交超时/响应未知时自动回退 REST，否则可能重复生成扣费；仅在明确尚未发出请求的路由阶段选择兼容传输。记录 RPC ID、HTTP 状态、位置状态和代理绑定哈希，不记录凭据。
6. 继续补采失败/取消、T2V、首帧/首尾帧、720p、批量输出和余额为零样本，建立完整位置枚举后再扩大流量。

机器可读的脱敏结构见 `flow-angular-rpc-2026-09-08.json`。
