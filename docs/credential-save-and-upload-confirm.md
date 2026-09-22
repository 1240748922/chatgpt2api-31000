# 3.2.26：凭据冲突按账号重读，上传确认与预热并行

## 来自 3.2.25 的新证据

2026-09-23 三条新记录的并行重叠只有 334 / 527 / 383ms。原始用户日志不提交到仓库。

* `c705cd818a9e46b6`：取号 11.237s，候选获取 0.141s；凭据维护 11.092s，
  其中刷新槽等待 0.014s、OAuth 请求 1.490s、结果保存 9.491s；预热 10.300s。
* `e3e0f359f8514b79`：图片字节传输 0.527s，但确认上传 4.559s，预热 2.911s。
* `56bffd9e5dff4218`：第一账号上传登记 429 后约 0.723s 换号，第二次成功。

这证明 3.2.25 的并行正在执行，但只覆盖 PUT 的范围太窄。保存阶段还包含锁、
数据库和账号日志，不能把 9.491s 全部当成某条 SQL 或某一种锁。

## 修复一：避免普通凭据冲突重读整个账号池

已复现的旧路径：OAuth 刷新完成 → 另一实例更新同账号额度 → 逐行 CAS 冲突 →
加载并规范化整个账号池 → 合并 → 重试保存。全池读取还占用本实例写入串行区间，
会拖延后续维护写入；不需要正在导号也能触发。

现在凭据保存的普通同账号冲突只查询本次涉及的旧/新 token 对应行，并在这个
小范围内合并额度与上传限制，随后重新 CAS。数据库使用现有 access_token 索引。

保障不变：

* AT/RT 仍同步、事务性落库后才能供生图使用；没有后台丢弃保存，也不绕过 CAS。
* 远端删除、不同凭据代次、新 token 冲突仍不能覆盖。旧 token 已消失时保留
  全量身份追踪回退，识别另一实例轮换后的账号并迁移别名和占用槽位。
* 子集快照的版本仅供本次重试，不能更新全池缓存版本或吞掉其他实例的导入。
* 没有子集读取能力的存储适配器保持旧路径；不改表结构、事务锁和数据库配置。

新增 `account_token_write_wait_ms`、`account_token_commit_ms`、
`account_token_conflict_ms`、`account_token_log_ms`，分别显示本地写锁等待、
数据库提交（含连接/事务锁）、冲突重读与规范化、同步账号日志写入。
它们包含在 `account_token_save_ms` 内，不能再次相加到请求总耗时。

## 修复二：确认上传也参与并行

3.2.25：

```
登记 → (PUT 图片 || 页面预热) → 确认上传 → 获取令牌 → 生图
```

3.2.26：

```
登记 → ((PUT 图片 → 确认上传) || 页面预热) → 获取令牌 → 生图
```

* 只处理第一张参考图，后续仍按顺序；不改变输入字节、尺寸或生成次数。
* 页面预热仍使用原会话；传输与确认使用工作线程独占会话，同一 Session 不会
  被两个线程同时操作。代理、指纹、UA 和确认用的账号身份保持一致。
* 签名资产 PUT 不带账号 cookie、Authorization 或 OAI 会话头；PUT 完成后
  才安装确认请求所需的上游头和独立 cookie 快照。
* 两边结束后按 domain/path/name 合并确认响应的 cookie 变化，保留预热期间
  主会话已经改动的同名 cookie，不粗暴覆盖整个 cookie jar。
* 登记 401/429 时没有启动工作线程；失败仍收尾再释放账号/出口；两边都失败
  优先保留上传错误。确认、预热均成功后才获取令牌和提交一次生图。
* 仍沿用同一请求 deadline、原步骤超时和不排队的线程预算；池满或配置为 0
  时立即退回原串行路径，不改账号重试次数或扩大生图并发限制。

`prewarm_overlap_ms` 现在涵盖 PUT 和确认与预热的交叠；上传细项、输入准备总计
及失败计时仍独立记录，时间线不重复相加。

## 复验

```powershell
$env:PYTHONPATH="$PWD\src_extract;$PWD\GPT-Register-Tool-main"
python -X utf8 -m pytest src_extract/tests/test_account_write_contention.py src_extract/tests/test_database_snapshot_concurrency.py src_extract/tests/test_account_maintenance_metrics.py src_extract/tests/test_image_input_prewarm.py -q
python -X utf8 -m pytest src_extract/tests -q --tb=short
node src_extract/tests/test_web_runtime.cjs
node src_extract/tests/test_account_import_web.cjs
node src_extract/tests/test_dashboard_web.cjs
```

覆盖真实临时 SQLite 事务、1/11,000 个无关合成账号下的定向重读、同账号额度
合并、远端删除/轮换/目标冲突、快照语句一致性、写锁/数据库/日志独立计时。
上传覆盖真实线程和本地 loopback HTTP + curl_cffi 会话、确认与预热并行、凭据
隔离、cookie 合并、401/429、超时收尾、池满回退和最终图片字节交付。
测试中的账号、图片和 HTTP 数据均为构造，不访问生产账号、数据库或上游。

2026-09-23 本地结果：全量 Python 300 passed；三组 Node 测试通过；
`git diff --check` 通过。Python 有一条既有的 TestClient/httpx 弃用警告。

## 边界与部署验证

本次修复的是可复现的全池重读放大，不宣称旧日志中全部 9.491s 都来自它。
数据库连接/事务锁、日志写入或页面响应仍可能慢；新版细项用于确认剩余占比。
没有削减预热超时、跨请求缓存 requirements 或取消凭据一致性保障。
本地验证不能代替生产端到端 A/B；尤其不能把理论重叠时间当成承诺提速。

部署核对应用版本 3.2.26，并将 `.env` 的 `CHATGPT2API_IMAGE_TAG` 改为本次
发布的 SHA 标签。无需数据库迁移，原导号、超分、代理和其他设置不变。
低峰重建应用实例后，用同参数多条请求对比输入准备总计及保存细项；重建会
中断旧实例在途请求。若需关闭并行，原环境变量
`CHATGPT2API_IMAGE_PREWARM_CONCURRENCY=0` 仍可回退。
