# chatgpt2api 31000 本地离线包

本包对应服务器上的 31000 服务：8 个 API 实例（app0-app7）+ Nginx 网关 + PostgreSQL。`data/` 和服务器 PostgreSQL 数据卷均未导出，首次启动会使用全新的空数据库。

## 云服务器部署教程

完整的 GitHub、GHCR、Docker、注册机配置和服务器更新步骤请查看：

[DEPLOYMENT.md](./DEPLOYMENT.md)

当前 `main` 默认使用镜像 `sha-a831bd9`，修复中间消息完成时提前关闭生图 SSE、任务接口已有图片却未接收的问题，并为初始化连接失败增加有时间上限的新连接重试。上一版的阶段诊断、上传受限换号、注册代理检查、后台同步和 CPU 超分修复也保留。
之前认可的旧稳定版仍保留为 `stable-before-maintenance-sharp-20260914`，镜像为 `sha-f6a3f02`。
Compose 默认锁定 `sha-a831bd9`；如果 `.env` 没有设置 `CHATGPT2API_IMAGE_TAG`，服务器直接执行更新命令即可使用该版本。
版本选择、升级前备份和恢复步骤请查看 [VERSIONING.md](./VERSIONING.md)。

## 在本地电脑启动

1. 安装 Docker Desktop（或 Docker Engine + Compose v2）。
2. 将 `chatgpt2api-images.tar` 导入镜像：

   ```sh
   docker load -i chatgpt2api-images.tar
   ```

3. 在本目录启动：

   ```sh
   docker compose --env-file .env up -d
   docker compose ps
   ```

服务入口为 `http://localhost:31000`。如需公网或其他端口，修改 `docker-compose.yml` 的 ports。

`.env` 中的密钥是本地占位值，请在正式使用前修改 `CHATGPT2API_AUTH_KEY`、`CHATGPT2API_MONITOR_CLUSTER_SECRET` 和 `POSTGRES_PASSWORD`；修改后重新创建容器（`docker compose up -d --force-recreate`）。

## 停止和清理

```sh
docker compose down
```

上面的命令保留本地 PostgreSQL 卷；如要连同本地新建的数据库一起删除，使用 `docker compose down -v`。本包不包含服务器账号、图片、调用记录或数据库内容。
