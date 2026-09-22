# 3.2.28：整组参考图上传与页面预热重叠

## 日志定位

2026-09-23 01:39–01:42 的三条请求（只保留耗时，不复制用户提示词、账号或凭据）：

| 请求 | 总耗时 | 准备 | 生成及结果 |
| --- | ---: | ---: | --- |
| `419332efe7a2487c` | 103.638s | 7.928s | 启动请求与 SSE 88.302s；最大 SSE 事件间隔 83.716s |
| `4cfa05c2750b4e26` | 53.923s | 10.917s | 轮询等待、查询、解析合计 39.391s |
| `190b5eb24e524e0c` | 73.907s | 成功尝试 17.732s | 前两次上传限流 11.768s；成功尝试启动请求与 SSE 39.668s |

取号每次仅 3–17ms，图片存储锁 1ms，没有复现上一版的账号写锁慢点。
第三条有五张输入图：上传累计 8.675s、预热 10.833s，但仅重叠 2.461s，
输入准备仍耗时 17.050s。不能把跨尝试的顶层 max 指标相加，也不能把轮询
休眠全部认定为图片生成完成后的浪费。当前日志不能区分上游排队、推理与传输。

## 原因与实际改动

之前仅第一张输入图的 PUT 和确认请求与首页预热重叠。第一张处理函数等待
预热结束后，调用者才能继续上传第二张，慢预热因此阻挡整组参考图。

现在流程如下：

```text
第一张读取/登记 ┬→ 独立 worker：PUT → 确认 → 第二张登记/PUT/确认 → … → 最后一张确认 ─┐
               └→ 原会话：首页预热（保留原重连规则）───────────────────────────────┴→ 请求令牌 → 准备会话 → 一次生图
```

- 所有图片仍按原顺序串行上传；每个请求只用一个优化 worker。不预热整个号池，
  不并发登记多张图，不缩图、不改变输入字节，不改模型、质量、尺寸或提示词。
- 第一张登记 401/429 时不启动预热。后续任何上传步骤失败即停止余下文件，
  原认证/上传限流分类与换号规则保留；不会提交部分参考图去生图。
- 上传与预热均成功后才提交生成。预热失败会通知 worker 不再开始后续步骤，
  已在执行的网络请求仍按原预算收尾。先等待 worker 关闭其 Session，再释放
  账号和出口；不在后台遗留正在上传的线程。
- 主 Session 只由调用线程使用。worker 使用独立的认证 Session 做登记/确认，
  沿用同一账号、UA、指纹和代理；后续签名资产 PUT 使用另一个无账号凭据的
  Session，并在每次 PUT 前清空资产 cookie。此连接在整组上传结束时关闭。
- 上传 cookie 按 domain/path/name 三方合并回主 Session，不覆盖预热期间主
  Session 已更新的 cookie。预热得到的 PoW 资源继续留在原会话。
- 不提前解码整个批次，逐张读取，避免额外持有所有解码字节。
- 原 `CHATGPT2API_IMAGE_PREWARM_CONCURRENCY` 预算不变；没有空闲 worker 时
  立即走串行路径，不排队、不重复登记第一张。每个步骤沿用同一请求 deadline。

## 诊断改动

- 增加 `bootstrap_first_ms`：首页首次请求耗时，包含在 `bootstrap_ms` 内。
- 增加 `bootstrap_retry_ms`：首次瞬时失败后的新连接重试耗时，只有进入重试
  才出现，包含在 `bootstrap_ms` 内。不把这两个子项再次加入阶段总计。
- 纯文生图和带参考图均记录；成功与失败日志、实时监控 API 和步骤明细保留。
- `prewarm_overlap_ms` 扩展为整个有序上传期间与预热的交叠。
- 预热单次 10 秒、两次共享最多 20 秒及总请求 deadline 的规则没有改动。
  不跳过预热，不复用其他账号 cookie，不盲目压短超时来掩盖连接问题。

## 验证与复现

测试使用临时 SQLite、合成图片/账号和 loopback HTTP，不调用真实上游。

1. 新增门控测试让预热等第五张确认。旧版因为只能传第一张而失败；修改后
   五张确认均能在预热结束前完成，生成只提交一次。
2. 相同五张图、每张 PUT 60ms、预热 300ms 的本地受控对照：第一张并行路径
   543ms，整组并行路径 303ms。此为合成测试，不代表生产固定节省比例。
3. 覆盖不同尺寸、PNG/JPEG、本地文件与 base64、字节/顺序/文件名、401/429、
   第三张登记限流、请求中途 deadline、预热失败取消、预算占满/关闭的回退。
4. 真实 curl_cffi + loopback HTTP 验证所有 PUT 均不携带账号 Authorization、
   cookie、Session ID；登记/确认身份保持一致。整组准备经过生图协议，最终
   合成图片字节成功保存并返回 URL/base64，而不只验证中间上传步骤。
5. 验证首次 10 秒超时、第二次 1 秒成功/失败的诊断经过真实日志 API 后不丢失，
   阶段总计仍是 11 秒而不是 22 秒。

```powershell
$env:PYTHONPATH="$PWD\src_extract;$PWD\GPT-Register-Tool-main"
python -X utf8 -m pytest src_extract/tests/test_image_input_prewarm.py src_extract/tests/test_image_completion_flow.py src_extract/tests/test_image_phase_diagnostics.py -q
python -X utf8 -m pytest src_extract/tests -q --tb=short
node src_extract/tests/test_web_runtime.cjs
node src_extract/tests/test_account_import_web.cjs
node src_extract/tests/test_dashboard_web.cjs
git diff --check
```

本地最终回归：326 项 Python 测试通过，三组 Node 测试通过，`git diff --check`
通过。仅有既有 TestClient/httpx 弃用警告。

## 部署与边界

应用版本 3.2.28，镜像使用发布提交对应的 `sha-xxxxxxx` 标签，不存在
`v3.2.28` 镜像标签。服务器 `.env` 若固定旧 SHA，必须更新镜像标签后重建
应用容器；单独 `git pull` 不会切换正在运行的镜像。低峰操作，注意在途请求。

本次没有数据库迁移，也没有修改 `.env`、Compose、导号、账号重试、代理、
超分或轮询设置。可退回 3.2.27 的 `sha-c567742`，无需数据回迁。

上线后应对比相同输入和参数下多条请求的 `input_prepare_ms`、
`prewarm_overlap_ms` 和成功率，并检查新增预热子项。单图本来已能重叠其上传；
纯文生图没有参考图可重叠，不会因这项批次优化直接缩短首次页面请求。
上游自身几十秒的生成/轮询等待不在此次修复范围，不能承诺一并消失。
