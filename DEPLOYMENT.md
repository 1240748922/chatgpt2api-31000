# Git 与 Docker 部署

## 上传前

仓库只保存源码和部署文件，不提交以下内容：

- `.env`、`config.json`
- `data/`、数据库文件、图片和调用记录
- 账号 session、注册工具运行目录
- ReMail、代理池和其他 API 密钥
- Docker 镜像导出包

首次准备配置（Linux 服务器）：

```bash
cp .env.example .env
cp config.example.json config.json
cp GPT-Register-Tool-main/config.example.json GPT-Register-Tool-main/config.json
```

然后只在服务器上的 `.env`、`config.json` 和
`GPT-Register-Tool-main/config.json` 中填写真实配置。三个文件都已经被
`.gitignore` 排除，不会上传到 GitHub。

## GHCR 自动发布

仓库包含 `Dockerfile` 和 GitHub Actions 工作流：

```text
.github/workflows/build-image.yml
```

每次向 `main` 推送代码后，GitHub Actions 会自动构建并发布：

```text
ghcr.io/1240748922/chatgpt2api-31000:latest
```

工作流使用 GitHub 自动生成的 `GITHUB_TOKEN`，不需要把 Docker 密钥写入仓库。

首次使用时，在 GitHub 仓库的 `Actions` 页面确认工作流成功，然后在服务器创建一个
只读 GHCR Token，并登录：

```bash
echo "$GHCR_READ_TOKEN" | docker login ghcr.io -u 1240748922 --password-stdin
```

GHCR Token 只保存在服务器，不放进 `.env`、GitHub 仓库或 Docker Compose 文件。

## 首次启动

```bash
git clone https://github.com/1240748922/chatgpt2api-31000.git
cd chatgpt2api-31000
cp .env.example .env
cp config.example.json config.json
cp GPT-Register-Tool-main/config.example.json GPT-Register-Tool-main/config.json
docker compose --env-file .env pull
docker compose --env-file .env up -d
docker compose --env-file .env ps
```

## 更新服务

代码推送并等待 GitHub Actions 成功后，在服务器执行：

```bash
cd chatgpt2api-31000
git pull --ff-only
docker compose pull
docker compose up -d --force-recreate
docker compose ps
```

镜像采用 Docker layer cache，只有发生变化的层会重新传输。当前 Compose 使用 `latest`，
适合你的单服务器部署；需要严格回滚时，可以把 Compose 中的标签改为 Actions 发布的
SHA 标签。

## 注册工具

注册工具源码可以放在同一私有仓库的 `GPT-Register-Tool-main/` 目录，也可以作为单独私有仓库在构建阶段取入。其 `config.json`、`sessions/`、`runtime/` 和 `.venv/` 必须留在服务器或运行卷中，不提交到 Git。

当前镜像会包含注册工具源码，Compose 另外挂载服务器上的 `config.json`、`sessions/`
和 `runtime/`，所以重新拉取镜像不会覆盖注册机配置和运行数据。

## 发布前检查

```bash
python -m compileall -q src_extract GPT-Register-Tool-main/sms_tool
docker compose --env-file .env config
docker compose ps
```

确认 8 个 `app` 实例、网关和 PostgreSQL 均为 healthy 后，再开始生产流量切换。
