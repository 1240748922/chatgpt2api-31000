# 稳定版本、升级和回退

## 2026-09-24 3.2.37：后台维护实况与界面整理

- 新增后台 AT 续期/额度同步的实际处理中数量、当前批次和累计成功/失败/跳过次数；与“每批最多”明确区分。进程重启归零，同一账号可能重复计数。
- 可用性摘要改为平铺指标，详情拆为同步进度、凭据与额度、处理规则三个页签；统一字体/图标、固定关闭区域，适配手机和深色。
- 打开详情时约 3 秒查询管理员只读内存接口，不新增全池扫描；原摘要频率、导号加速、生图/超分并发、性能调度阈值、凭据租约和自动清理规则不变。
- 本地验证：517 项 Python 测试通过；无本地 PostgreSQL 的 6 项留给 CI 实跑。4 组 Node、Compose 和差异校验通过；额外复验 4 种屏宽/主题组合。未使用真实账号请求上游，未更新用户服务器。
- 自动续期和删除行为、计数口径及复验步骤见 [维护进度说明](./docs/account-maintenance-progress.md)。本版不会让失效 RT 自动恢复，也不会新增自动删除规则。
- 发布 SHA 与镜像锁定在构建验证成功后补齐；当前 Compose 镜像暂保持 3.2.36，避免引用未发布镜像。

## 2026-09-24 网关配置补丁：dynamic-backends-v1

- 配置提交：`1288478c155314689534011ef0be7eda16e7f914`；容器回归环境兼容修正：`ade81b5c7b30901772ce41b41688937699f6b678`。
- 修复管理/图任务/图片后备及 importer 的静态后端解析。两组固定角色 upstream 使用 Docker DNS 动态解析；生图池、路径、并发和 POST 重试策略不变。
- [真实 Nginx/Docker DNS 回归](https://github.com/1240748922/chatgpt2api-31000/actions/runs/36020215556)的 `gateway-regression` 与 PostgreSQL 回归已通过：对照组复现健康 `/version` + 概览 404，新配置在 app0/importer 旧 IP 被其他角色占用后自动恢复，导入路径/请求内容及平滑重载中的合成 SSE 保持正确。
- 现网已回传：直连 app0 401、网关 404，Nginx 平滑重载后网关恢复为预期的未认证 401；浏览器应使用已有登录态重载概览。
- **这是宿主机挂载配置更新，不需要更换应用镜像；应用仍为 3.2.36 / `sha-3c1535b`。** 当前已恢复的服务器可先继续运行，永久配置安装见 [网关更新与复验](./docs/gateway-routing.md)。单文件挂载可能仍引用旧 inode，须用容器内 `nginx -T` 核对标记，不能认为 `git pull` + reload 一定读到了新文件；如需重建，仅在排空后重建 gateway，不重启生图容器。
- 本机无 Linux Docker 引擎，已做 Python 语法和 Compose 校验；真实 Nginx/IP 复用与 PostgreSQL 测试在 GitHub CI 执行。DNS 刷新仍存在缓存窗口，不承诺后端重建零中断。

## 2026-09-24 当前发布：3.2.36

- 应用提交：`3c1535b06a57fcb54162954b8b510f680da1922f`。
- 应用镜像：`ghcr.io/1240748922/chatgpt2api-31000:sha-3c1535b`，同时发布到 `latest`。
- [GitHub Actions：PostgreSQL 回归及镜像发布](https://github.com/1240748922/chatgpt2api-31000/actions/runs/36016654221)已成功，已核实 `Build and push` 步骤成功。
- 补齐 OAuth `refresh_token_reused` 的终止性分类，按凭据代际标记 RT 无效，避免再次进入后台续期候选。AT 仍有效的账号继续保留可用，不清额度；不同 RT 覆盖更新时清除旧错误与退避，重复同 RT 不清失效标记。详见 [原因、边界与离线复验](./docs/refresh-token-reused.md)。
- 验证：503 项本地 Python 测试（新增 19 项）、6 项 GitHub PostgreSQL 17 回归、4 组 Node 检查和 Compose 配置校验通过。本机没有 PostgreSQL 的 6 项跳过已在 CI 实跑。未直接更新服务器、未使用真实账号做上游请求。
- 不改生图/超分并发、导号加速、预热、代理或维护阈值，无数据库迁移。**补丁不能恢复已使用的旧 RT**；需取得最新 AT/RT 或重新登录。历史旧错误缺少凭据代际依据，不启动全池判废迁移；下一次允许的续期明确拒绝后才标记，共享不确定请求仍不重放。

**如果服务器 `.env` 固定了 `CHATGPT2API_IMAGE_TAG`，改成 `sha-3c1535b`，或移除该项使用 Compose 默认值；`git pull` 不会修改 `.env`。**

低峰暂停新请求和导入，等在途任务结束并备份数据库/配置后执行：

```bash
git pull --ff-only &&
docker compose --env-file .env config -q &&
docker compose --env-file .env pull app0 app1 app2 app3 app4 app5 app6 app7 importer &&
docker compose --env-file .env stop -t 600 gateway &&
docker compose --env-file .env stop -t 600 app0 app1 app2 app3 app4 app5 app6 app7 importer &&
docker compose --env-file .env up -d --no-deps --force-recreate app0 app1 app2 app3 app4 app5 app6 app7 importer gateway
docker compose --env-file .env ps
curl -fsS http://127.0.0.1:31000/version
```

此流程有统一切换窗口，不是无中断升级，不删除卷、不重建 PostgreSQL。
恢复后 `/version` 应为 **3.2.36**。逐实例核验：

```bash
for s in app0 app1 app2 app3 app4 app5 app6 app7 importer; do
  docker inspect "$(docker compose --env-file .env ps -q "$s")" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
done
```

所有实例应输出 `3c1535b06a57fcb54162954b8b510f680da1922f`。观察续期错误、“可尝试恢复/需补凭据”及各分片就绪数；5 条 RT 复用样本不能证明所有待恢复账号都失效，历史额度也不等同于就绪额度。不要清共享租约或盲目重导整个池。

回退镜像为 `sha-53947c9`（3.2.35），同样排空后统一切换。部署锁定提交使用 `[skip ci]`，没有另外的应用镜像标签。

---

## 2026-09-24 上一版：3.2.35

- 应用提交：`53947c94cf12ceaa4b6338b49df6e28841ddf6ba`。
- 应用镜像：`ghcr.io/1240748922/chatgpt2api-31000:sha-53947c9`，同时发布到 `latest`。
- [GitHub Actions：PostgreSQL 回归及镜像发布](https://github.com/1240748922/chatgpt2api-31000/actions/runs/35988415876)已成功，已核实 `Build and push` 步骤成功。
- 生图结果由逐账号提交改为最多 32 个账号的小批提交；普通账号状态写入冲突只重读相关账号，不再在持写锁期间反复加载整个池。远端删除/轮换仍走原身份协调，额度/计数合并与共享租约保护保留。详见 [写锁修复与复验](./docs/account-writer-contention.md)。
- 包含 3.2.34：首次取号“就绪但全忙”默认有界等待最多 30 秒，释放槽位立即唤醒，受请求截止约束；真实空池/过期/额度耗尽不放行。取号日志保留繁忙、候选和分片诊断。3.2.34 应用提交 `cccfd32a1f8a1df5da85b7de34dd3d46089cd98a` 已构建发布，本次直接更新到包含它的 3.2.35。
- 验证：484 项本地 Python 测试、6 项 GitHub PostgreSQL 17 回归、4 组 Node 检查和 Compose 配置校验通过。本机无 PostgreSQL 的 6 项跳过已在 CI 实跑。未直接更新服务器，未使用真实账号生图压测。
- 不修改导号、预热、代理、生图/超分并发、真实 `.env` 或数据库结构；不放宽后台性能暂停阈值。更新后仍需观察写锁 P95 和后台维护是否恢复，无法续期的过期 AT 不会自动变成可用账号。

**若服务器 `.env` 固定了 `CHATGPT2API_IMAGE_TAG`，先改成 `sha-53947c9`，或移除该项使用 Compose 默认值。`git pull` 不会修改 `.env`。**

在低峰暂停新请求和导入，等在途任务结束并备份数据库/配置后执行：

```bash
git pull --ff-only &&
docker compose --env-file .env config -q &&
docker compose --env-file .env pull app0 app1 app2 app3 app4 app5 app6 app7 importer &&
docker compose --env-file .env stop -t 600 gateway &&
docker compose --env-file .env stop -t 600 app0 app1 app2 app3 app4 app5 app6 app7 importer &&
docker compose --env-file .env up -d --no-deps --force-recreate app0 app1 app2 app3 app4 app5 app6 app7 importer gateway
docker compose --env-file .env ps
curl -fsS http://127.0.0.1:31000/version
```

此流程有统一切换窗口，不是无中断升级，不删除卷、不重建 PostgreSQL。恢复后 `/version` 应为 **3.2.35**。逐实例核验：

```bash
for s in app0 app1 app2 app3 app4 app5 app6 app7 importer; do
  docker inspect "$(docker compose --env-file .env ps -q "$s")" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
done
```

所有实例应输出 `53947c94cf12ceaa4b6338b49df6e28841ddf6ba`。观察“账号可用性 → 详情”：写锁 P95 是否下降、维护每批是否从 0 恢复、就绪池是否补充；旧指标窗口和恢复观察不会瞬间清零。若仍暂停，保留新的原因、采样时间和一次取号失败 JSON，不要清空共享租约或重新导入全池。

回退镜像为 `sha-6f5cb89`（3.2.33），同样排空后统一切换。部署锁定提交使用 `[skip ci]`，没有另外的应用镜像标签。

---

## 2026-09-24 上一版：3.2.33

- 应用提交：`6f5cb89bee1ff0be5365510bffe6e01684d8cbb2`。
- 应用镜像：`ghcr.io/1240748922/chatgpt2api-31000:sha-6f5cb89`，同时发布到 `latest`。
- [GitHub Actions：PostgreSQL 回归及镜像发布](https://github.com/1240748922/chatgpt2api-31000/actions/runs/35959838872)已成功，已核实 `Build and push` 步骤成功。
- 概览中心、账号管理的可用性卡片增加文生图/图生图“就绪额度”；只汇总满足准入条件账号的已知额度，未知额度、无限额套餐单列，图生图排除上传冷却。两种额度有重叠，不能相加。
- 仅增加只读缓存统计与显示；账号派发、导号加速、后台维护、预热、生图/超分并发和代理配置不变，无新配置项或数据库迁移。详细口径见 [就绪额度说明](./docs/ready-account-quota.md)。
- 验证：466 项本地 Python 测试通过（含实际静态模块 Chromium 回归），本地跳过的 4 项 PostgreSQL 测试已在 GitHub PostgreSQL 17 中通过；4 组 Node 检查、Compose 配置校验通过。未直接更新服务器，未用真实账号做生图压测。

**如服务器 `.env` 固定了 `CHATGPT2API_IMAGE_TAG`，改为 `sha-6f5cb89`，或移除该项跟随 Compose 默认值。`git pull` 不会修改 `.env`。**

在低峰暂停新请求和导入、等待在途任务结束并备份数据库/配置后执行：

```bash
git pull --ff-only &&
docker compose --env-file .env config -q &&
docker compose --env-file .env pull app0 app1 app2 app3 app4 app5 app6 app7 importer &&
docker compose --env-file .env stop -t 600 gateway &&
docker compose --env-file .env stop -t 600 app0 app1 app2 app3 app4 app5 app6 app7 importer &&
docker compose --env-file .env up -d --no-deps --force-recreate app0 app1 app2 app3 app4 app5 app6 app7 importer gateway
docker compose --env-file .env ps
curl -fsS http://127.0.0.1:31000/version
```

此流程有统一切换窗口，不是无中断滚动升级；不删除卷、不重建 PostgreSQL。尤其从 3.2.31 或更早版本升级时，不要让未实现共享刷新保护的旧进程混跑。恢复后 `/version` 应为 **3.2.33**，浏览器 Ctrl+F5 刷新，在“账号可用性”的前两张卡片下方查看“就绪额度”。

```bash
for s in app0 app1 app2 app3 app4 app5 app6 app7 importer; do
  docker inspect "$(docker compose --env-file .env ps -q "$s")" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
done
```

所有实例输出应为 `6f5cb89bee1ff0be5365510bffe6e01684d8cbb2`。回退镜像为 `sha-074f2c8`（3.2.32），同样在排空后统一重建 app/importer。本次部署文件提交只锁定已经发布的应用镜像，使用 `[skip ci]`，没有另一个应用镜像标签。

---

## 2026-09-24 上一版：3.2.32

- 应用提交：`074f2c8cefdd0dda3bdbdd1072b699ba5f95f120`。
- 应用镜像：`ghcr.io/1240748922/chatgpt2api-31000:sha-074f2c8`，也发布到 `latest`。
- [GitHub Actions：PostgreSQL 回归及构建发布](https://github.com/1240748922/chatgpt2api-31000/actions/runs/35896696203)均成功，`Build and push` 步骤已核实。
- 整合严格凭据准入、跨实例生图/刷新互斥、请求前页面库存及精简可用性面板；原导号批量加速、导入窗口、生图/超分并发及代理配置保留。
- 验证：460 项本地 Python 测试、4 项 GitHub PostgreSQL 17 测试、4 组 Node 检查通过；本地因没有 Docker 引擎而跳过的 4 项 PostgreSQL 测试已在 CI 实跑。
- 详细参数、互斥边界及回退：[3.2.32 说明](./docs/ready-pool-prewarm.md)。镜像构建成功不代表服务器已经更新，未通过本机 Docker 或服务器真实账号做生图压测。

**先把服务器 `.env` 中的 `CHATGPT2API_IMAGE_TAG` 改为 `sha-074f2c8`，或移除该项使用 Compose 默认值。`git pull` 不会修改 `.env`。**

首次从 3.2.31 升级不要混跑：暂停外部新请求和新导入，等待在途任务结束，备份数据库/配置，然后执行：

```bash
git pull --ff-only &&
docker compose --env-file .env config -q &&
docker compose --env-file .env pull app0 app1 app2 app3 app4 app5 app6 app7 importer &&
docker compose --env-file .env stop -t 600 gateway &&
docker compose --env-file .env stop -t 600 app0 app1 app2 app3 app4 app5 app6 app7 importer &&
docker compose --env-file .env up -d --no-deps --force-recreate app0 app1 app2 app3 app4 app5 app6 app7 importer gateway
docker compose --env-file .env ps
curl -fsS http://127.0.0.1:31000/version
```

这会有一次统一切换窗口，不是无中断滚动升级；不删除数据卷、不重建 PostgreSQL。网关等待全部应用就绪，恢复后 `/version` 应为 **3.2.32**，浏览器按 Ctrl+F5 刷新。逐实例核验：

```bash
for s in app0 app1 app2 app3 app4 app5 app6 app7 importer; do
  docker inspect "$(docker compose --env-file .env ps -q "$s")" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
done
```

所有输出应为 `074f2c8cefdd0dda3bdbdd1072b699ba5f95f120`。查看账号可用性详情应显示严格准入；已知就绪数量会低于总账号数。预热命中通过请求事件 `page_prewarm_hit=1` 判断，未命中是正常冷回退，不应排队等库存。

上一版回退镜像为 `sha-471fc63`，回退也需要先排空并统一重建 app/importer。新表保留即可，不要手工清除 RT 发送记录。单独关闭预热可配置 `CHATGPT2API_PAGE_PREWARM_TARGET=0`。

后续仅锁定已发布镜像的部署提交使用 `[skip ci]`，不代表又有一个新的应用镜像。

---

## 2026-09-24 上一版：3.2.31（阶段一）

- 应用提交：`471fc6345f93acaf26d78795d769da6edfd27f07`。
- 应用镜像：`ghcr.io/1240748922/chatgpt2api-31000:sha-471fc63`，同时发布到 `latest`。
- [GitHub Actions 构建与发布](https://github.com/1240748922/chatgpt2api-31000/actions/runs/35890716722)已成功；Compose 默认标签已指向本次应用提交。
- 包含账号可用性统计和性能驱动的周期维护；**不包含**跨实例刷新租约、严格未知 AT 准入和请求前预热库存。说明及策略回退见 [阶段一文档](./docs/account-readiness-maintenance.md)。
- 426 项 Python 回归、4 组 Node 检查通过，无数据库迁移；导号加速、生图/超分并发及代理配置保留。

先确认服务器 `.env`：如果存在 `CHATGPT2API_IMAGE_TAG`，改为 `sha-471fc63`，或删除该配置以跟随 Compose 默认值。已有 `latest` 也会拉到本次发布，但使用明确 SHA 更便于回退和核验。`git pull` 不会覆盖服务器 `.env`。

低峰暂停新请求、等在途请求结束后执行（不要删除数据卷）：

```bash
git pull --ff-only &&
docker compose --env-file .env config -q &&
docker compose --env-file .env pull app0 app1 app2 app3 app4 app5 app6 app7 importer &&
docker compose --env-file .env up -d --no-deps --force-recreate app0 app1 app2 app3 app4 app5 app6 app7 importer gateway
docker compose --env-file .env ps
curl -fsS http://127.0.0.1:31000/version
```

本命令更新应用、导入进程和网关，不重建 PostgreSQL。网关需等待应用就绪，启动初期可稍后重试 `/version`；返回的 `version` 应为 `3.2.31`。镜像拉取如返回 401/403，使用服务器已有 GHCR 登录配置，并检查它具有该镜像的读取权限，不要把 token 贴到日志或聊天中。

逐容器核验应用提交：

```bash
for s in app0 app1 app2 app3 app4 app5 app6 app7 importer; do
  docker inspect "$(docker compose --env-file .env ps -q "$s")" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
done
```

每个输出应为 `471fc6345f93acaf26d78795d769da6edfd27f07`。上一应用版本为 `3.2.30` / `sha-9eb6e5b`，需要完整回退时将 `.env` 镜像标签改为它再拉取、重建；仅回退同步策略也可使用文档中的 `idle` 开关。

镜像对应应用提交；其后的部署文件提交只锁定上述已构建的标签，使用 `[skip ci]` 避免为部署文档重复构建。该部署提交没有自己的新镜像标签。

**下面保留的是历史发布记录，其中“当前”等称呼只对应当时版本，不代表本次默认镜像。**

## 新维护方案之前的稳定版本

| 项目 | 固定版本 |
| --- | --- |
| 稳定版 Git 标签（含版本锁定配置和本文） | `stable-before-maintenance-sharp-20260914` |
| 应用代码提交 | `f6a3f022954abadb3405fa0b4354584da70f246f` |
| 应用镜像 | `ghcr.io/1240748922/chatgpt2api-31000:sha-f6a3f02` |

这里保留的是引入新维护调度方案之前的应用，包含 ReMail 验证码代理隔离修复。
稳定标签从 `16e7193` 建立，只补充版本选择配置和文档，其应用代码与 `16e7193` 一致。
该应用提交的 [GitHub Actions 构建](https://github.com/1240748922/chatgpt2api-31000/actions/runs/34742644962) 已成功。

| 版本用途 | 应用镜像 | 说明 |
| --- | --- | --- |
| 旧稳定版（按需回退） | `sha-f6a3f02` | 新维护方案之前、已补齐 Sharp 运行依赖的版本 |
| 当前修复版（main 默认） | `sha-8d9d51e` | token 定向账号 CAS 写入、取号冲突继续选择候选；保留快照读取与图片存储修复 |
| 上一版账号快照读取修复 | `sha-4a8522e` | 账号快照单条 SQL 一致读取，修复持续写入导致的假空池 503；保留图片存储与计时优化 |
| 上一版图片存储与计时修复 | `sha-d8b7cdc` | 新图片索引独立持久化、逐图互斥、完整后处理计时；不因本地保存失败重复生图 |
| 上一版日志与删除预览修复 | `sha-8c1da8f` | 恢复日志详情接口，账号删除状态/额度分页预览，超分排队/处理耗时 |
| 上一版换号与账号快照修复 | `sha-ca94823` | 上传/额度限制不消耗普通重试次数；修复跨实例导入被单账号写入隐藏、空池缓存刷新滞后和上传冷却丢失 |
| 上一版阶段诊断修复 | `sha-6064cdb` | 超时阶段与耗时归属修复、换号错误隔离、轮询状态诊断 |
| 更早的上传重试版 | `sha-087cf9c` | 上传受限自动换号、90 秒生图流与 120 秒轮询窗口、注册代理检查 |
| 上一版维护与超分版 | `sha-281017a` | 本次修复之前的维护与 CPU 超分方案 |
| 上一版维护修复镜像 | `sha-26d613e` | 按生图负载延后维护、低库存放行补号和 Sharp 依赖修复 |

先前创建的 `stable-20260913` 标签误把新方案当成稳定版，已弃用；不要用它回退旧方案。为避免已拉取标签的电脑和服务器产生歧义，不重写该标签，改用上面的新稳定标签。
`main` 保留新方案代码，Compose 默认运行 `sha-8d9d51e`。直接用源码启动或自行构建 `main` 也会运行新方案；需要旧方案源码时使用 `stable-before-maintenance-sharp-20260914`。
如果之前已经在服务器 `.env` 中写了 `CHATGPT2API_IMAGE_TAG=sha-f1648e6`，本次 `git pull` 不会覆盖它，必须手动改为 `sha-f6a3f02` 才会回到旧方案。

Git 标签保存源码和部署文件；GHCR 镜像保存已构建的应用。回退运行版本需要切换镜像，单独 `git pull` 或回退 Python 文件不够。
`latest` 会随新构建变化。稳定服务器使用具体的 `sha-xxxxxxx` 标签，并保留 GHCR 中相应的镜像版本，不要删除或重新指向其他镜像。PostgreSQL 和 Nginx 使用各自的镜像标签，应用版本锁定不等于数据库备份。


**存储版本回退注意：** 本版使用共享 data 下的 `image_index.json.pending/` 保存尚未合并的图片元数据。不要删除这个目录。回退到 `sha-8c1da8f` 或更早镜像前，应暂停新请求、等待在途任务结束，并在仍运行本版的 app0 中执行 `python -c "from services.image_storage_service import image_storage_service; print(image_storage_service.compact_index())"`，再切换镜像。完整命令与备份规则见 [性能审查](./PERFORMANCE_AUDIT.md#部署备份与回退注意)。

## 运行版本确认

应用的 `/version` 接口返回应用版本及环境变量提供的镜像标签、构建标识。这些部署标识便于查看，但不是镜像内容的独立证明；确认代码版本应同时检查运行容器的 OCI revision 标签。

默认情况下，`build_version` 会跟随 `CHATGPT2API_IMAGE_TAG`。需要给一次部署加上容易识别的标识时，在服务器 `.env` 中增加或修改：

```env
CHATGPT2API_BUILD_VERSION=release-2026-09-17-a
CHATGPT2API_BUILD_TIME=2026-09-17T00:00:00+08:00
```

更新并重建后验证：

```bash
curl -s http://127.0.0.1:31000/version
docker compose --env-file .env images
```

返回内容中的 `build_version`、`image_tag` 和 `build_time` 来自响应容器的环境变量。`CHATGPT2API_BUILD_VERSION` 只用于标识，不会改变镜像选择；镜像仍由 `CHATGPT2API_IMAGE_TAG` 决定。验证当前修复版时，下列命令应在每个 app 容器输出 `8d9d51e0bd974184b1488e70e95d2c0820d88611`：

```bash
for service in app0 app1 app2 app3 app4 app5 app6 app7; do
  docker inspect "$(docker compose --env-file .env ps -q "$service")" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
done
```

本次先推送应用提交并单独构建 `sha-8d9d51e`，再推送 Compose 版本锁定提交。不要把两个提交合并在一次推送里，却默认认为父提交也生成了同名镜像；普通 `push main` 构建只针对推送后的 HEAD。

不设置 `CHATGPT2API_IMAGE_TAG` 时，日常更新跟随仓库 Compose 默认版本。需要暂停升级或回退时才在 `.env` 中显式指定标签；恢复自动跟随时删除该行。本次写入冲突修复前的版本是 `sha-4a8522e`；更早版本的用途见上表。

## 2026-09-18 账号写入冲突与取号恢复

本版修复 `stale accounts storage revision` 在取号阶段被直接包装成 `no_available_account` 的路径。上一版解决的是快照读取一致性，没有消除写入时因其他账号更新而触发的全池 CAS 冲突。

现在 token 更新与刷新错误记录只比较、保存当前账号涉及的 AT 行，保留同账号并发更新、删除和凭证替换的保护；不推进未完整加载的账号池版本，也不把其他待保存的账号状态误标为已持久化。残余写入冲突释放槽位后继续选择其他候选，遵守请求截止时间和候选去重规则，不无限循环，不降低并发。

17 项新增回归与完整 158 项 Python 测试、前端检查、Compose 校验通过。数据库回放使用隔离 SQLite/WAL，上游返回模拟结果；没有执行用户服务器或 PostgreSQL 实机压测。详细根因和复现命令见 [性能审查](./PERFORMANCE_AUDIT.md)。没有数据库结构或图片索引格式变更。

当前镜像为 `sha-8d9d51e`，对应 [GitHub Actions 构建](https://github.com/1240748922/chatgpt2api-31000/actions/runs/35250767272)。如果 `.env` 锁定旧标签，需要改为这个标签，或删除 `CHATGPT2API_IMAGE_TAG` 行后跟随仓库默认值，再拉取并重建。

## 2026-09-18 账号快照并发读取修复

`accounts changed repeatedly while loading its snapshot` 不是上游额度不足，而是账号快照读取在持续写入时连续失败。新版同一条 SQL 取得账号数据和版本，空集合也有一致版本；不锁住写入，不修改数据库结构，不降低生图或超分并发。保留 CAS 旧版本校验和跨实例导入合并规则，不靠无限换号掩盖存储故障。

新增 17 项回归包含真实 SQLite/WAL 事务交错和取号/token 更新/图片结果返回流程，全套 141 项测试通过。上游使用模拟响应，本次没有在用户服务器或 PostgreSQL 实例执行压测。详见 [性能审查](./PERFORMANCE_AUDIT.md)。本版与 `sha-d8b7cdc` 使用相同的图片索引格式；回退到更早、不支持 pending 的版本仍需遵循上述索引合并要求。

## 2026-09-16 生图链路修复

这版修复了两个会丢失图片结果的处理分支，并改善初始化连接失败的恢复：

- **保留完整生图流**：`finished_successfully` 和 `is_complete=true` 可能只是工具参数或推理消息结束。旧代码即使看到 `end_turn=false` 仍主动关闭 SSE。现在需要明确的轮次结束标记才提前关闭，其余情况继续接收后续图片，仍受既有生图超时预算约束。
- **接收任务接口中的图片**：会话记录尚未更新时，任务接口的 `image_gen_message` 可能已经包含图片。旧代码只读取任务错误，忽略图片输出；现在复用图片提取流程接收这些结果。
- **初始化连接重试**：预热首页遇到瞬时连接失败时，在同一账号上用新连接重试一次，两次合计最多 20 秒，单次最多 10 秒，并遵守请求总截止时间。此时尚未提交生图任务；认证错误和限流不进入这项重试。
- 保留上一版的超时阶段诊断、计时修复、错误隔离和明确失败任务提前结束轮询。

验证依据：本地已有原始日志的 14 条主动关闭记录中，11 条明确带有 `end_turn=false`。从其中提取结构、替换用户文本后，回放测试在修复前丢失后续图片，修复后通过实际 SSE 解码和结果格式化流程，将图片保存到隔离的临时目录。任务接口输出优先于会话记录的测试也由超时变为成功。

57 项 Python 测试及前端运行测试通过。可以在安装项目和测试依赖后，从仓库根目录复现：

```bash
PYTHONPATH="$PWD/src_extract:$PWD/GPT-Register-Tool-main" python -m pytest -q src_extract/tests/test_image_completion_flow.py
```

上述测试验证了具体代码缺陷。尚未在云服务器上部署这版进行真实上游压测，不等于所有上游网络超时都会消失。

后续的存储锁、尝试计时和并发对照说明见 [性能审查](./PERFORMANCE_AUDIT.md)。当前全套 158 项 Python 测试、前端运行检查及 Compose 配置检查通过（上一版已完成浏览器交互验证）；旧日志无需迁移，新增超分排队/处理细分只适用于新请求。本次没有进行用户云服务器真实上游压测，不能据此认定分钟级延迟已解决。升级带有前端修改的版本后刷新网页，必要时 `Ctrl+F5` 清除旧资源缓存。

## 1. 按需把当前应用锁定

服务器先拉取一次包含版本选择功能的部署文件：

```bash
cd /opt/chatgpt2api-31000
git pull --ff-only
nano .env
```

在 `.env` 中添加下面这一行；如果已经有这一项，就修改原来的值，不要重复添加：

```env
CHATGPT2API_IMAGE_TAG=sha-a831bd9
```

保存后执行以下命令，每一步成功后再执行下一步：

```bash
docker compose --env-file .env config -q
docker compose --env-file .env config --images
docker compose --env-file .env pull app0 app1 app2 app3 app4 app5 app6 app7
docker compose --env-file .env up -d --force-recreate app0 app1 app2 app3 app4 app5 app6 app7 gateway
docker compose --env-file .env ps
curl --fail http://127.0.0.1:31000/version
```

配置输出中的应用镜像应以 `:sha-a831bd9` 结尾。如果仍然是 `latest`，检查部署文件是否更新，或者终端是否设置了覆盖 `.env` 的同名环境变量。可用 `unset CHATGPT2API_IMAGE_TAG` 清除终端覆盖后重试。
需要长期锁定当前版本时写入 `.env`；希望日常 `git pull` 跟随仓库默认更新时，删除这项即可。
重建 8 个应用会中断正在处理的请求，请在停止新请求并等待现有任务结束后切换。

## 2. 试新版之前先保留恢复材料

以下命令在项目根目录运行。备份包含密钥，只保存在服务器的 `.runtime/` 中，不要上传 GitHub。

```bash
umask 077
RELEASE_BACKUP=".runtime/release-backups/$(date +%Y%m%d-%H%M%S)"
mkdir -p "$RELEASE_BACKUP"
git rev-parse HEAD > "$RELEASE_BACKUP/source-commit.txt"
docker compose --env-file .env images > "$RELEASE_BACKUP/images.txt"
cp .env config.json docker-compose.yml nginx.conf "$RELEASE_BACKUP/"
cp GPT-Register-Tool-main/config.json "$RELEASE_BACKUP/register-config.json"
docker compose --env-file .env exec -T postgres sh -c 'pg_dump -U "$POSTGRES_USER" -d "$POSTGRES_DB" -Fc' > "$RELEASE_BACKUP/database.dump"
```

确认 `pg_dump` 成功退出。`database.dump` 备份 PostgreSQL 中的账号及记录；图片、注册会话和运行文件位于 Compose 挂载的目录中，需要另行备份。使用自定义挂载路径时，备份实际宿主机目录。跨数据库结构变更升级时，应停止写入后做数据库与文件的一致备份。

还可以在服务器保留当前应用镜像的离线副本，避免以后 GHCR 中的镜像被误删。先确保它已下载成功：

```bash
docker image save -o "$RELEASE_BACKUP/stable-image.tar" ghcr.io/1240748922/chatgpt2api-31000:sha-a831bd9
```

镜像较大，需要预留磁盘空间。离线恢复时用 `docker image load -i 备份路径/stable-image.tar`，跳过在线 `pull`，再执行 `up`。镜像副本不包含数据库和挂载文件。

## 3. 选择一个新版进行测试

当前维护与超分方案的镜像是 `sha-a831bd9`。主动使用该方案时，把 `.env` 改成 `CHATGPT2API_IMAGE_TAG=sha-a831bd9`；如果需要恢复旧方案，改回 `sha-f6a3f02`。

先确认目标提交对应的 Actions 构建成功，记下其前 7 位提交号，然后执行：

```bash
git switch main
git pull --ff-only
nano .env
```

把 `CHATGPT2API_IMAGE_TAG` 的值改成目标的 `sha-xxxxxxx`，再按第 1 节校验、拉取、重建和检查。
保持这个具体版本号，之后拉取镜像不会自动跳到另一版应用。不要用 `latest` 作为需要长期保留的回退目标。

## 4. 新版有问题，切回当前稳定应用

如果只是应用版本有问题、部署文件和数据库结构仍兼容，编辑 `.env`，恢复：

```env
CHATGPT2API_IMAGE_TAG=sha-f6a3f02
```

然后按第 1 节执行校验、拉取、重建和检查。这会替换应用容器，保留现有账号、图片和数据库卷。

如果新版还修改了 Compose 或 Nginx，连部署文件一起恢复。先备份当前文件；若有本地跟踪文件改动，先保存这些改动，工作区干净后执行：

```bash
git fetch origin --tags
git switch --detach stable-before-maintenance-sharp-20260914
```

此时 `.env` 等被 Git 忽略的实际配置仍然保留。把 `.env` 的镜像值改回 `sha-f6a3f02`，然后按第 1 节重新部署。处于稳定标签时不要执行 `git pull`；以后要继续升级，先 `git switch main`，再按第 3 节操作。

检查 8 个应用容器的 `IMAGE` 均为稳定标签且健康，网关可访问，并实际生成一张图片、打开返回的图片 URL。`/version` 中的上游版本号不一定能区分本项目的每次修改，以容器镜像标签和镜像 ID 为准。

不要执行 `docker compose down -v`，它会删除数据库卷。切换镜像不会撤销新版已经写入的数据；如果未来新版包含不兼容的数据库迁移，需要使用升级前的配套备份恢复，恢复到备份时间也会丢失之后新增的数据，不能直接套用普通镜像回退。
