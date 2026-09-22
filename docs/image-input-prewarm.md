# 3.2.25：参考图传输与页面预热并行

后续 3.2.28 已将第一张图的并行扩展为整组有序上传，见 [整组参考图预热](./batch-input-prewarm.md)。本文保留 3.2.25 当时的实现记录。

## 本次日志定位

用户确认三条 2026-09-22 23:41–23:43 的请求运行于 3.2.24。
原始日志含用户内容，不提交到仓库。观测结果：

| 请求 | 慢点 | 出口等待 |
| --- | --- | --- |
| 57769c88d5704ace | 首次取号 8.992s，其中凭据维护 8.928s；三个 auth_invalid 后第四次成功。成功尝试上传 2.047s、预热 3.076s | 各次 5–30ms |
| bd8dcd4795b24760 | 取号 7–56ms；两次 auth_invalid 后第三次成功。上传 3.427s、预热 4.882s、结果地址解析 4.761s | 各次 14–29ms |
| 984e06d7083a473d | 取号 11.130s，其中凭据维护 10.990s，候选取号合计 136ms；没有输入图 | 3ms |

不是出口等待 9 秒，也不是未更新。3.2.24 的 ready-AT 优先策略有回退路径，
不能消除所有刷新等待。这三条旧日志不足以区分刷新排队、OAuth 网络或保存。

## 实际流程变更

旧流程：

```
登记文件 → PUT 图片字节 → 确认上传 → 首页预热 → 获取令牌 → 准备会话 → 生图
```

新流程（仅带输入图的图片请求，且存在空闲并行槽位）：

```
登记文件 ┬→ 独立 Session PUT 图片字节 ─┐
         └→ 原会话首页预热 ──────────┴→ 确认上传 → 获取令牌 → 准备会话 → 生图
```

* 首页预热仍在原账号的原 HTTP 会话中执行，cookie、指纹与 PoW 参数保留。
* 仅签名资产地址的 PUT 移到独立 Session，沿用出口、TLS 设置和 UA；不拷贝
  Authorization、账号 cookie 或会话 ID。输入图字节、顺序、尺寸均不改变。
* 文件登记出现 401/429 时没有启动预热，保留快速换号。若 PUT 与预热均失败，
  优先保留上传异常；不启动会话、生图或上传确认。预热失败也会等待已经开始的
  PUT 按原超时预算收尾，以免释放账号/出口租约后留下一条未受控传输。
  因此不承诺签名 PUT 失败时比串行更快。
* 一次请求只并行第一张图的 PUT；不把多张参考图全部同时发出，不增加上游
  文件登记压力。后续参考图仍按原顺序上传。
* 不预热整池账号，不缓存或跨请求复用请求令牌；纯文生图、Codex 路径和普通
  聊天附件上传仍走原流程。此次不实现提前准备账号的热会话池。

## 并发与回退

可选环境变量 `CHATGPT2API_IMAGE_PREWARM_CONCURRENCY`：默认每实例 64，允许
0–1024，0 关闭。它是并行优化的额外工作线程预算，**不是生图并发上限**。
容量占满时立即走原串行流程，不等待槽位、不往线程池排长队。8 个实例默认
最多 512 个额外传输工作线程（按需创建），实际应结合带宽、连接数和 CPU 调整。

Compose 只添加此变量映射；已有导号、账号并发、超分、数据库与代理配置不变。
不改用户 `.env`。需关闭时在 `.env` 设置为 0 并重建应用容器。

## 新耗时字段

* `input_prepare_ms`：上传与预热实际墙钟时间，阶段总计使用该值，不再累加
  与其重叠的 `upload_ms` / `bootstrap_ms`。
* `upload_decode_ms` / `upload_register_ms` / `upload_put_ms` /
  `upload_confirm_ms`：读取解码、申请地址、字节传输、确认上传。
* `prewarm_overlap_ms`：真实重叠时长，不伪装成更短的上传耗时；不等于实测
  端到端收益，因为独立连接及线程调度也有成本。
* 凭据维护增加 `account_token_lookup_ms`、`account_token_slot_ms`、
  `account_token_http_ms`、`account_token_save_ms`、`account_token_singleflight_ms`，
  分别定位凭据读取/锁等待、刷新并发槽、OAuth 请求、结果保存、等待另一刷新。
  分项包含在 `account_token_maintenance_ms` 内，不重复累计。

成功和失败尝试均保留计时，监控事件、日志 API、步骤明细同步支持。旧日志
没有的细项不能反推；不会改写历史记录，也不承诺凭据刷新网络时间自动缩短。

## 复验与上线

```powershell
$env:PYTHONPATH="$PWD\src_extract;$PWD\GPT-Register-Tool-main"
python -X utf8 -m pytest src_extract/tests/test_image_input_prewarm.py src_extract/tests/test_account_maintenance_metrics.py -q
python -X utf8 -m pytest src_extract/tests -q --tb=short
node src_extract/tests/test_web_runtime.cjs
node src_extract/tests/test_account_import_web.cjs
node src_extract/tests/test_dashboard_web.cjs
```

测试使用真实线程、合成 HTTP 会话和临时数据库，覆盖并行握手、代理/UA一致、
不共享认证信息、池占满/关闭回退、401/429、上传/预热失败、请求 deadline、
多图顺序、成功图片字节落盘交付、失败尝试经过日志 API 后计时不丢失。
未调用真实上游，不能以测试节省时间代替生产收益。

本地回归结果（2026-09-23）：Python 全量 288 passed；上面三组 Node 测试通过；
`git diff --check` 通过。包含凭据诊断合并时清零旧请求细项的回归验证。

部署须核对镜像 SHA 及应用版本 3.2.25；`.env` 若固定旧镜像标签需更新。
低峰重建后，用相同参考图、相同参数对比并行开/关的多条新请求，观察成功率、
`input_prepare_ms` 和 `prewarm_overlap_ms`，并用凭据细项确认剩余 9–11 秒。
