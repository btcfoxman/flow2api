# 3.5 pre：模型能力目录与入口拒绝诊断修复

## 本轮任务分析

本轮继续监测线上任务，未更改账号、代理、内容安全策略或风险冷却参数。时间均为 2026-09-14，Asia/Taipei。

上一镜像运行窗口 15:23:13–16:46:37 的独立视频任务：**18 个，成功 13、最终失败 5**；另有 1 个图片任务成功，以及 6 次 Responses 入口 501（未接收为任务）。统计按公共任务 ID 去重，不能把重试次数当成任务失败数。

- 4 个视频终态失败：`google.rpc.Status.code=3`；1 个为 `PUBLIC_ERROR_SEXUAL`，3 个为 `PUBLIC_ERROR_UNSAFE_GENERATION`。这些是上游内容策略拒绝，当前已正确结束任务、返回友好 400，不增加账号禁用次数，不自动修改输入或重试以绕过判定。
- 1 个上传阶段失败：`maseQ` 返回 code 3，没有公开细分原因。可确认参考图上传请求被拒绝，但不能根据该码断言文件损坏、尺寸问题或内容问题。该任务未生成上游视频，未重复提交。
- 期间另有 6 次异常活动拒绝：账号 29 四次、账号 32 两次，公开原因为 `PUBLIC_ERROR_UNUSUAL_ACTIVITY`。队列退避仍有效；其中部分任务重试后生成成功，另一个通过提交后收到独立的内容策略失败。这两层原因不能混为一谈。
- 旧的六次 `/v1/responses` 501 没有记录模型名，无法补造历史型号；本轮修复以后可以定位新的拒绝。

## 已落地代码

### 模型发现与生成检查使用一致的协议能力

- 提取 `uses_flow_only_protocol`，与生成时 `model_transport_error` 共用“启用账号全部为 Flow、执行模式为 native_cdp”的判断。
- `/v1/models`、`/v1/models/aliases`、`/v1beta/models`、`/models` 及 Gemini 单模型查询同步过滤当前协议无法执行的型号。
- Flow-only 模式不再列出尚未适配的图片 2K/4K 放大、Imagen 等路径；基础图片别名仍可用，但说明不再宣称支持 2K/4K 放大。
- 保留已支持的 360p、720p 视频和原始画幅图片。没有把高分辨率请求静默改成低分辨率，也没有宣称新增了上游放大协议支持。
- 混合 Labs/Flow、仅 Labs、非 native_cdp 模式保持既有目录兼容；无启用账号时保持原有目录语义。余额、会话暂时失效和冷却不会被误判为协议不支持。
- 每次列表请求只读取一次账号协议快照，不按每个模型重复访问数据库；启用账号协议变化后，无需重启即可重新计算目录。

### 入口拒绝增加安全诊断

统一记录 `generation_capability_rejected`，包含规范化后的已知模型、固定阶段、原因和 HTTP 状态；不记录请求正文、提示词、Cookie 或连接 Token。

覆盖 `image_response_entry`、`video_queue_entry`、`video_queue_dispatch`、`generation_entry`。原本 Responses 提前返回 501 的路径现在也能定位到具体模型。未知模型字符串不会原样写入此诊断。

业务文件为 `src/services/model_capabilities.py`、`src/api/routes.py`、`src/services/generation_handler.py`；新增回归文件 `tests/test_model_catalog_capabilities.py`。其余既有工作区改动保留。

## 验证与部署

- 本地全量：**522 passed、216 subtests passed**；18 项既有 SQLite datetime adapter 弃用警告；`git diff --check` 通过。
- 新增 8 项测试覆盖各目录一致性、别名、单模型查询、混合/空账号兼容、协议动态变化、余额和会话状态不影响协议判断、Responses/视频入口不入队、安全诊断。
- Linux 新镜像通过相关目录、协议和任务计数回归。
- **16:46:37** 部署 `ghcr.io/btcfoxman/flow2api:capability-catalog-20260914`；先等待正在执行的用户任务结束，部署前队列、运行任务、人工登录锁均为 0。
- 线上 3 个变更业务文件 SHA-256 与本地一致；SQLite `quick_check=ok`。
- 按部署规范保留 `.env`、Compose、配置和数据库备份，未修改源/目标分离方式，未重启 `flow2api-tupdater-pre` 或 VNC。
- 回滚镜像：`ghcr.io/btcfoxman/flow2api:rpc-status-20260914`。
- 备份：`/home/btcfoxman/docker/flow2api/capability-catalog-20260914/backup`。
- 本轮未执行 Git commit/push。

## 公共 API 实测

16:47:04 实际调用线上公共接口：

| 接口 | 实测结果 |
| --- | --- |
| `/v1/models` | 26 个型号，与已支持协议集合一致 |
| `/v1/models/aliases` | 2 个基础图片别名，说明不宣称放大支持 |
| `/v1beta/models`、`/models` | 各 28 项，与具体型号及别名并集一致 |
| Gemini 单模型查询 | 不支持的 2K 型号为 404；支持的基础型号为 200 |
| `/v1/responses` 不支持的 2K 请求 | 501 / `model_not_supported`，没有任务 ID |

该拒绝测试前后公共任务台账、生成结果台账和任务行数量增量均为 0；结构化日志实际记录 `model=gemini-3.1-flash-image-landscape-2k`、`stage=image_response_entry`。这条人为构造的诊断 501 与用户历史的 6 次 501 分开统计。

最小付费测试因新用户任务已进入队列，在生成 API 调用前被空闲检查拦截。测试 journal 未创建，因此本轮没有额外付费测试任务或测试积分消耗；改为跟踪上线后的真实用户任务。

截至 16:52，新版上线后的三个真实用户任务均完成，公共 API 状态与唯一终态台账一致：

| 公共任务 ID（均带 `flow2api-submit-` 前缀） | 模型 | 执行账号 | 完成时间 |
| --- | --- | --- | --- |
| `d391eca36cac49c3bb4fac87c37a7ce2` | abra_r2v_4s | 19 | 16:51:38 |
| `7c8e2126035646cebb5f0cf53f15e6c4` | abra_r2v_10s | 52 | 16:51:45 |
| `da1e60bc760649689f297ff2c3ef4f13` | abra_r2v_10s | 7 | 16:51:04 |

三者均返回 `completed / 100`、存在结果链接、无错误；关联内部尝试 outcome 均为 0，没有重复计数。没有下载用户视频。验收后队列和执行中任务指标均为 0；累计视频成功 1064 → 1067、累计成功 1131 → 1134、累计失败保持 1941。账号 7 的参与是当时系统可用账号状态，不是本轮手动启用或修改其会话。

部署与 API 验收记录位于工作区 `output/deployments/capability-catalog-20260914/`；`before-tasks.json`、`before-runtime.json` 保存部署前证据，`api-verify.json` 保存公共接口验证，`user-task-verify.json` 保存后续真实任务的公共 API 与终态台账对照。

## 仍需区分的限制

- 本轮修复的是错误的模型能力展示及缺失诊断，不保证上游风控或内容策略拒绝消失。收到真实策略拒绝时仍按失败结束，不通过重复提交“碰运气”。
- 启动时很多旧会话停在 `/about`；额度刷新和任务预检共用浏览器容量，仍导致首批任务等待。本轮未更改该调度机制；锁内健康状态复核、重复预检合并和健康账号探测顺序是后续独立优化点。
- 账号启用、会话有效、余额满足所选模型、风险冷却结束是不同条件；不能用启用账号数替代可执行并发数。
- 进程快照仍有 1 个 zombie，未出现增长；本轮未进行浏览器进程清理或启动本机 BAT。
