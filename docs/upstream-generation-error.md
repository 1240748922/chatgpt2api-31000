# 上游生成失败文字的处理（3.2.45）

生图会话已经结束但只返回“由于我这边发生了错误，我未能生成图片。”时，旧版把它归为 `upstream_text_reply`，映射为本地 HTTP 400 / `text_review` 后直接结束。这个分类既不能证明内容审核，也不能说明账号失效。

本版将以下两条实际观察到的完整 assistant/text 终止回复识别为 `upstream_image_generation_error`：

- 由于我这边发生了错误，我未能生成图片。
- 抱歉，我无法生成这张图片，因为图像生成过程中发生了错误。

用户输入、引用、未结束消息、普通聊天和其他文字不使用这个匹配。结构化审核、输入、鉴权等错误保留更明确的结论。

## 处理行为

1. 流结束、轮询或断流恢复发现这类文字时，利用有时限的任务查询补充证据。已有生成图片优先交付；任务详情给出的更明确错误优先处理。参考图附件不作为生成结果返回。
2. 未找到更明确结果时走现有普通错误重试，受重试开关、最大尝试次数、账号去重、请求总截止时间及剩余重试窗口约束。不会按额度/上传限制的独立规则持续扫描账号。重试需要额外生成时间，不能保证降低每条请求的耗时或保证成功。
3. 单凭这段文字不会标记账号异常、发起鉴权核验或推定额度已经消耗。它不证明上游实际余额未变，上游余额仍以同步结果为准。
4. 尝试日志显示“上游图片生成失败”，保留原文、错误码和切换记录。若重试耗尽仍是该错误，公开接口使用通用工具错误和本地 HTTP 502。HTTP 状态是本地映射，不能当作上游实际 HTTP 状态。

HTTP 400 / 文字回复会转为重试成功或 HTTP 502 / 技术失败，比较升级前后应看最终成功率及错误码，不能仅用 502 数量判断质量变差。历史日志不回写、不重新分类。

## 验证

离线使用合成消息、临时 SQLite 和生成的测试 PNG，不调用上游、不刷新真实凭据：

```powershell
$env:PYTHONPATH="$PWD\src_extract;$PWD\GPT-Register-Tool-main"
python -X utf8 -m pytest -q src_extract/tests/test_upstream_generation_error.py src_extract/tests/test_upstream_policy_notice.py src_extract/tests/test_image_account_retries.py src_extract/tests/test_image_recovery.py src_extract/tests/test_image_completion_flow.py src_extract/tests/test_image_terminal_poll.py
```

覆盖实际 SSE 解码→失败分类→有界重试→图片保存/返回、任务图片优先、任务明确拒绝优先、轮询/断流恢复、账号状态/额度不误改、普通聊天及日志 API 序列化。全量回归和镜像状态见 [发布记录](../VERSIONING.md)。

上线后以 `/version` 和镜像 revision 确认版本，再观察此错误码的尝试数、最终成功率和耗时。若仍然失败，保留同一次尝试的任务详情；不同账号、不同尝试的限制提示不能直接作为此文字的原因。

本版无数据库迁移，不调整导号、后台同步、超分、并发或用户 `.env` 配置。服务器由用户更新，升级步骤及回退版本见发布记录。
