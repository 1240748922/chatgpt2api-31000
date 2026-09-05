# Git 与 Docker 部署

## 上传前

仓库只保存源码和部署文件，不提交以下内容：

- `.env`、`config.json`
- `data/`、数据库文件、图片和调用记录
- 账号 session、注册工具运行目录
- ReMail、代理池和其他 API 密钥
- Docker 镜像导出包

首次准备配置：

```bash
copy .env.example .env
copy config.example.json config.json
```

然后只在服务器上的 `.env` 和注册工具 `config.json` 中填写真实配置。

## 当前导出包的启动方式

当前 `docker-compose.yml` 使用预构建镜像 `chatgpt2api:1000rpm-monitor`，没有现场构建步骤。临时发布某个已经构建好的版本时：

```bash
docker load -i chatgpt2api-images.tar.gz
docker compose --env-file .env up -d
```

镜像导出包必须由最新源码重新构建，否则 Git 中的修改不会进入容器。

## 推荐的长期发布方式

使用 Git 私有仓库存放源码，使用 GHCR 或其他私有镜像仓库存放构建后的 Docker 镜像。每次发布使用明确版本号：

```bash
docker build -t ghcr.io/ORG/chatgpt2api:VERSION .
docker push ghcr.io/ORG/chatgpt2api:VERSION
```

服务器保留 `.env`、`config.json` 和数据卷，只更新镜像版本：

```bash
docker compose pull
docker compose up -d --force-recreate
docker compose ps
```

不要在生产环境直接使用 `latest`，也不要把生产服务器当作 Git 工作区直接执行无版本的 `git pull`。使用版本标签可以回滚到上一版镜像。

## 注册工具

注册工具源码可以放在同一私有仓库的 `GPT-Register-Tool-main/` 目录，也可以作为单独私有仓库在构建阶段取入。其 `config.json`、`sessions/`、`runtime/` 和 `.venv/` 必须留在服务器或运行卷中，不提交到 Git。

当前 Compose 的应用镜像必须包含注册工具源码，或者通过运行时卷挂载到应用预期的工作目录；仅上传 `src_extract` 不会自动包含注册工具。

## 发布前检查

```bash
python -m compileall -q src_extract
docker compose config
docker compose ps
```

确认 8 个 `app` 实例、网关和 PostgreSQL 均为 healthy 后，再开始生产流量切换。
