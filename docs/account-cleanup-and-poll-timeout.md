# 3.2.22：手动删除异常账号与两份超时日志排查

## 删除异常账号预览为空

账号列表的状态由 `account_row()` 投影：原始状态是“正常”或“限流”，但 AT 已过期/远程确认无效且 RT 缺失/已确认无效时，列表仍会显示“异常”。此前“删除异常账号”仅提交 `auto_remove_invalid_accounts=true`，服务端只选择原始 `status == 异常`，导致截图中 AT 失效、RT 缺失的账号无法进入预览。

修复在手动操作中同时提交已有的 `remove_unusable_credentials=true`，预览与确认执行使用同一组选项。继续排除禁用账号；原始状态非异常且仍有可用 RT 的账号不因 AT 过期而被删除。额度仍大于零或额度未知，并不妨碍已失去可用凭证的账号进入手动异常清理预览。只有超时、网络错误或没额度的账号不会因此被视为凭证失效。

完整页面回归还发现，批处理菜单原来没有接收 `loadData` 与 `setError`：删除成功后刷新报错，再由缺失的错误处理回调产生第二个错误。现已接好这两个回调，并让清理失败正常提示。原删除确认框、分页预览、取消行为保留；执行时重新读取状态，预览后恢复可用 RT 的账号会被跳过。

改动只涉及账号管理页面的手动操作。没有改自动移除策略、后台维护、导号入库与 RT 兑换、账号选择、生图重试、超分、超时参数、数据库格式、Compose 或 Nginx。没有操作用户真实账号或执行实际删除。

## 超时日志实际说明了什么

两份文件都是请求日志 JSON。`extracted_at.txt` 在本次附件里不是 AT 导入数据，因此其中的报错与导入格式无关。

| 日志 | 已观察到的过程 | 结果 |
| --- | --- | --- |
| `6.17.txt` | 图生图，10 张参考图；前三次提交会话收到 HTTP 413，共约 54.6 秒；第 4 次上传收到 429，约 0.57 秒；第 5 次准备和流约 19 秒，随后轮询 120 秒 | 总共约 194.5 秒。最后快照只有用户输入，保存下来的轮询记录均无输出图片、无匹配任务；最终 `image_poll_timeout` |
| `extracted_at.txt` | 文生图，第 1 次约 136.2 秒，其中轮询 120 秒；换号后第 2 次约 103.8 秒，剩余轮询预算约 91.3 秒 | 总共 240 秒，已换号一次。两次轮询合计约 211.3 秒，取得账号合计约 19.3 秒，最终耗尽请求总预算 |

`extracted_at.txt` 的会话只看到用户消息、空 assistant 文本与 reasoning_recap；保留下来的轮询记录没有图片输出或匹配任务。第一次第 18 次任务查询有一次 `upstream_connection_timeout`，后续查询恢复，不能把整个 240 秒归为这一次网络异常。

这些日志没有进入下载、超分或存储，所以本次超时与先前的图片存储锁不是同一故障。`finished_successfully` 是消息状态，不等于图片完成。`image_generating` 是收到上游流后的阶段名称，也不证明上游已成功建立生图任务。

413 是上游 `/backend-api/f/conversation` 返回的拒绝，通常对应请求内容/大小限制，但返回体为空，没有原始请求字节数或限制说明，不能据此断言“10 张一定超限”或提示词就是原因。也不是本地 Nginx 上传限制：前几次图片上传已经完成，报错发生在后续上游会话提交阶段。第五个账号没有报 413，却也未产出图片；现有证据不足以证明只换号或缩减某一个字段即可修复。

超时只能确认“在当前预算内，程序没有查到可交付图片”，不能证明图片已经生成，也不能证明后台稍后绝不会完成。没有把超时账号自动标坏或删除，没有直接延长 120/240 秒预算，没有盲目增加跨账号重试。

## 后续需要的证据

短流结束原因没有保存到用户提供的日志 JSON。现有容器日志的 `image_stream_resolve_start` 包含 `sse_last_payload_preview`、`sse_event_count`、`tool_invoked`、`has_image_arguments`、`should_poll_for_image`，可用于继续区分上游未触发生图、流已终止、消息协议变化等原因。读取对应时间段与 call_id 即可，不需要全量日志、账号 AT/RT 或密钥。

在这些证据到达前，不能宣称超时的上游根因已修复。本版修复的是已复现的手动删除故障。

若该时间段的容器日志尚未轮转，可在服务器项目目录读取这两个请求的对应事件：

```bash
docker compose --env-file .env logs --since '2026-09-22T16:28:00+08:00' --until '2026-09-22T17:14:00+08:00' app0 app1 app2 app3 app4 app5 app6 app7 2>&1 | grep 'image_stream_resolve_start' | grep -E '9e2a5b6a0ff341d1|507adb0369654dba'
```

提供上述诊断字段即可；不要附带 AT、RT、Cookie 或密钥。没有输出也不能证明事件没发生，需要先确认日志是否已轮转或容器已重建。

## 验证与更新

```powershell
$env:PYTHONPATH="$PWD\src_extract;$PWD\GPT-Register-Tool-main"
python -X utf8 -m pytest src_extract/tests -q --tb=short
node src_extract/tests/test_web_runtime.cjs
node src_extract/tests/test_account_import_web.cjs
node src_extract/tests/test_dashboard_web.cjs
```

回归使用隔离数据库与合成凭据：实际 Vue 页面发出清理选项，经过真实 FastAPI 预览和服务端条件选择，最终删除仅作用于测试内存。覆盖原始正常但 AT 失效/无 RT、有额度仍异常、原始异常、RT 可恢复、禁用、单纯没额度、单纯超时、取消、执行前恢复、删除后列表刷新和失败提示。

最终全套 Python 回归 **239 项通过**，包含本次新增的 5 项完整页面交互测试；三个 Node 测试脚本均通过。只有既有 Starlette TestClient 弃用提示。本轮没有使用真实账号发起生图，也没有验证线上上游恢复出图。

本地证据（被 Git 忽略）：`.runtime/cleanup-abnormal-baseline.txt`、`.runtime/cleanup-abnormal-fixed.png`、`.runtime/cleanup-3.2.22-tests.txt`、`.runtime/20260922-image-diagnostics.json`。最后一份只保留诊断字段，没有用户提示词、邮箱或凭证。原始附件没有复制进仓库。

构建成功后把服务器 `.env` 中 `CHATGPT2API_IMAGE_TAG` 改为本次提交对应的 `sha-<前7位>`，再拉取并重建应用。应确认 `/version` 为 `3.2.22` 并刷新网页。`git pull` 不会修改锁定的镜像标签；本次也没有改变更新方式，应用重建仍需避开在途生图请求。

更新后进入“账号管理 → 批量处理 → 删除异常账号”，核对 AT 失效且无可用 RT 的账号是否出现在预览中，以及其状态和额度是否正确。可先点取消验证不会删除；只有确认预览范围后再执行删除，完成后列表应立即刷新。测试中的服务器失败会显示“账号清理失败”，不会出现缺失回调导致的前端异常。
