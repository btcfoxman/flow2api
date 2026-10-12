# 异步图片 Responses

Flow 图片模型可通过 `POST /v1/responses` 提交，返回 `resp_...` ID；用
`GET /v1/responses/{id}` 查询，完成后从 `output[0].url` 读取图片链接。

提交请求中的参考图必须是 HTTP(S) URL，输出格式必须是 URL。接口拒绝
Base64 图片输入和 `b64_json` 输出，避免图片数据经 API、数据库和调用链
多次复制。提交阶段只验证参数并持久化 URL；后台 worker 才下载参考图和
调用上游。任务默认最多 50 个排队或处理中，默认只有 1 个 worker；可用
`FLOW_IMAGE_RESPONSE_QUEUE_CAPACITY`（1–1000）和
`FLOW_IMAGE_RESPONSE_WORKERS`（1–4）调整。队列满返回 HTTP 429。

状态保存在 `image_response_tasks`。完成或失败后会清除存储的提示词和
参考图 URL，只保留结果与任务状态；30 天后清理终态记录。服务重启时，
排队任务会继续执行，已经开始的任务会标记为结果不明的失败状态，避免
未知上游结果被重复提交。部署前检查会阻止重启正在执行的图片任务。
