# 结果查询限流恢复（3.2.46）

## 线上证据

2026-10-07 北京时间 14:46:59–15:46:48 的只读统计包含 746 条生图请求：564 成功、129 条最终 429、18 条最终 502，另有 35 条其他失败或文字回复。所有应用实例为 3.2.45 / `sha-6fd5fa6`。

128 条 429 均来自已提交生图后的 `GET /backend-api/conversation/{id}`，错误体为 `Too many requests`，全部带有 2–55 秒的 `Retry-After`。另 1 条 429 为上传限制。查询接口的限流不能证明图片额度耗尽；当时快照仍有 2,256 个凭据就绪账号和 8,743 已知文生图额度，实际派发还受模型、分片、账号槽位等条件限制。

旧轮询代码只接受全局 `failure.retryable`。通用 `upstream_rate_limited` 的全局策略是直接返回 429，因此未执行这里已有的退避等待，导致这 128 个请求直接结束。全局禁止对通用 429 换号仍有必要：原会话已经提交，重新生图可能重复工作，也不能保证解除查询限流。

18 条最终 502 中，12 条为 `image_poll_timeout`，5 条为 `image_tool_error`，1 条为 `no_image_generated`。工具错误样本明确提到 Instant 用量限制和 Mini 不支持图片；不能将它等同于统计卡片中的图片剩余额度。轮询超时样本仍未取得图片文件，任务列表为空；assistant 的生成参数完成并不能证明图片生成完成。本版没有证据支持提前中止这些活跃生成，也不宣称消除全部 502。

## 修改范围

- 仅在结果轮询的只读会话查询中，为通用 HTTP 429 增加原会话恢复。按 `Retry-After` 等待后查询同一会话，账号租约保留至本次尝试结束。
- 无等待头时沿用有上限的指数退避；等待头为零时至少等待 1 秒并加入抖动，防止紧密重试。
- 所有等待、网络查询使用既有轮询预算和请求截止时间。等待要求超过剩余预算时不提前重查；持续限流最终保留 429、原始原因和 `Retry-After`。读取恢复后，旧 429 不覆盖后续真实超时。
- `poll_trace` 增加 HTTP 状态、上游要求等待秒数和实际退避毫秒数；等待仍计入原 `poll_wait_ms`。成功时继续走现有图片下载、保存、返回流程。
- 鉴权失败、明确额度耗尽、内容拒绝和发起生成时的通用 429 保留原分类与重试规则。不修改账号额度、后台维护、导入、超分、并发参数、数据库结构或用户 `.env`。

## 离线复验

```powershell
$env:PYTHONPATH="$PWD\src_extract;$PWD\GPT-Register-Tool-main"
python -X utf8 -m pytest -q src_extract/tests/test_image_poll_rate_limit.py src_extract/tests/test_image_recovery.py src_extract/tests/test_image_account_retries.py src_extract/tests/test_image_http_errors.py src_extract/tests/test_upload_cooldown.py
```

新增 14 项测试。先在旧代码复现立即失败，再验证：2/13/24/55 秒等待、无头/零值、有界持续限流、恢复后超时分类、鉴权/拒绝/真实额度边界。完整流程覆盖 SSE 解码→429→同会话恢复→PNG 保存和返回，确认只提交一次生成、只使用一个账号，成功或持续失败后均释放槽位。测试使用合成输入和模拟网络，不调用真实生图。

## 更新后验证

服务器由用户更新；镜像和更新步骤见 [发布记录](../VERSIONING.md)。确认 `/version` 为 3.2.46，再观察 `image_poll_retry` 的 `reason=upstream_poll_rate_limit`、等待分项和最终成功率。恢复需额外等待上游限流解除，不能保证每条请求更快，也不能保证持续限流或上游未产出图片时成功。历史失败任务不会自动补跑。
