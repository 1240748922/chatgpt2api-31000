# 网关路由修复：dynamic-backends-v1

## 现网证据

2026-09-24，应用版本 3.2.36：

| 不带密钥的请求 | 重载前 | Nginx 平滑重载后 |
| --- | --- | --- |
| app0 自身 `/version` | 200，3.2.36 | — |
| app0 自身 `/api/dashboard` | 401 | — |
| gateway `/version` | 200，3.2.36 | — |
| gateway `/api/dashboard` | 404，`Not Found` | 401 |

401 是本次无凭据探测的预期结果，代表路由存在；不是用户密钥失效的证据。
现象确认在网关转发层，重载后恢复与旧地址解析相符。未获取现网旧 IP
究竟被哪个容器占用的证据，不能仅凭 404 指定是 importer。

旧配置的生图池已经使用 `server ... resolve`，但 `/api/`、图任务、图片后备
以及导入路径使用 `proxy_pass http://app0:80` / `http://importer:80`。
这些静态目标在加载配置时解析；顶层 `resolver` 本身不会让它们定期重新解析。
重建应用后旧 IP 不再对应原角色，可能出现 `/version` 正常而管理接口 404/502。

## 修复范围

- 管理/图任务/图片后备仍固定到 app0；导入仍固定到 importer。
- 为两个角色分别使用带共享 `zone` 和 `resolve` 的 upstream，随 Docker DNS 更新。
- 不改 8 实例生图池、分片、请求路径/查询参数、超时、并发、密钥或数据。
- 不添加跨角色兜底、404 重试或非幂等 POST 重试，避免重复生图/重复导入。
- 标记为 `# gateway-routing: dynamic-backends-v1`。这是宿主机挂载的网关配置，
  应用继续用 3.2.36 / `sha-3c1535b`，不需要升级应用镜像。

Docker DNS 的缓存有效期仍为 10 秒。更换地址到重新解析之间仍可能有短暂错误；
本补丁消除的是“持续指向旧地址、必须人工重载”的问题，不保证容器重建无中断。

## 立即恢复（不修改配置、不重启应用）

```bash
docker compose --env-file .env exec -T gateway sh -c 'nginx -t && nginx -s reload'
docker compose --env-file .env exec -T app0 curl --max-time 10 -sS -i http://gateway/api/dashboard
```

第二条不带密钥，预期 401；浏览器用已有登录态重新加载概览验证 200。
平滑重载保留旧 worker 处理在途请求，不等同于重建网关。

## 安装永久配置

先备份并拉取；如果存在本地冲突，停止处理冲突，不要强制覆盖：

```bash
cp -p nginx.conf "nginx.conf.before-dynamic-dns-$(date +%Y%m%d-%H%M%S)"
git pull --ff-only
docker compose --env-file .env config -q
docker compose --env-file .env exec -T gateway nginx -T 2>&1 | grep 'gateway-routing:'
```

若容器可见 `dynamic-backends-v1`，执行上面的校验+平滑重载即可。
若宿主机新文件有标记、容器 `nginx -T` 没有：当前 Compose 是单文件只读挂载，
Git 换文件可能换了 inode，容器仍看到旧文件。**重复 reload 不会解决这个挂载差异。**
可以先用立即恢复步骤继续运行，待低峰暂停新请求、在途请求结束后，仅重建网关：

```bash
docker compose --env-file .env stop -t 600 gateway &&
docker compose --env-file .env up -d --no-deps --force-recreate gateway
docker compose --env-file .env exec -T gateway nginx -T 2>&1 | grep 'gateway-routing:'
docker compose --env-file .env exec -T app0 curl --max-time 10 -sS -i http://gateway/api/dashboard
```

这会有网关切换窗口，不是无中断升级；不重建 app0–app7、importer 或 PostgreSQL。
不要为网关配置更新执行全服务 `up --force-recreate`。`/version` 仍应是 3.2.36，
本次以配置标记和真实接口状态验收。

## 可重放的容器回归

有 Linux Docker 引擎的机器执行：

```bash
python3 scripts/test_gateway_routing.py
```

脚本创建独立网络、两个 Nginx（旧静态代理对照组/新动态代理）和合成 HTTP 服务，
不使用真实账号、数据库或上游；退出只删除自己创建的随机命名测试容器/网络。
模拟 app0 更换 IP、旧 IP 被导入角色占用，证明旧组 `/version` 正常但概览 404，
新组无需 reload 恢复到正确 app0。再覆盖 importer IP 复用、路径/参数/POST 内容、
图片后备、内部路由隔离，以及平滑 reload 期间在途合成 SSE 完整返回。
生产环境的角色/IP 分配不由测试模拟结果替代；现网恢复以表中用户回传为依据。
