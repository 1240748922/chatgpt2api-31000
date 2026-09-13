# GitHub + GHCR + Docker 部署教程

本文用于将 `chatgpt2api-31000` 部署到 Linux 云服务器，方便以后在 GitHub 页面直接查看。

当前部署结构为：8 个 API 实例（`app0` 到 `app7`）+ Nginx 网关 + PostgreSQL + 注册工具。

## 0. 先理解两个 Token

部署时通常需要两套不同的 GitHub Token：

| Token | 用途 | 主要权限 |
| --- | --- | --- |
| GitHub 仓库 Token | `git clone`、`git pull` 源码 | 私有仓库的 `Contents: Read` |
| GHCR Token | `docker login`、`docker pull` 镜像 | `read:packages` |

GitHub 仓库 Token 用于下载源码，GHCR Token 用于下载 Docker 镜像。遇到认证错误时，先确认使用的 Token 是否对应正确的操作。

不要把任何 Token、API Key、代理密码、邮箱密码、账号 session 或真实配置写入 GitHub。

## 一、先确认 GitHub 镜像构建成功

打开仓库：

[chatgpt2api-31000](https://github.com/1240748922/chatgpt2api-31000)

进入：

```text
Actions
```

找到：

```text
Build and publish container image
```

等待状态变成绿色的 `Success`。

每次成功构建都会发布 `latest` 和 `sha-提交号前7位` 两种标签。
当前稳定应用的构建已成功，镜像地址是：

```text
ghcr.io/1240748922/chatgpt2api-31000:sha-16e7193
```

首次构建可能需要较长时间，因为会安装 Python 依赖、Playwright 和 Chromium。

Compose 默认锁定这版应用，避免拉取镜像时意外切换到新版。
完整版本说明和回退步骤见 [VERSIONING.md](./VERSIONING.md)。

## 二、准备云服务器

以下以 Ubuntu 22.04/24.04 为例。先通过 SSH 登录服务器：

```bash
ssh root@你的服务器IP
```

安装 Git、Docker：

```bash
apt update
apt install -y git ca-certificates curl
curl -fsSL https://get.docker.com | sh
systemctl enable docker
systemctl start docker
```

验证：

```bash
docker --version
docker compose version
git --version
```

如果云服务器有安全组或防火墙，需要放行端口：

```bash
ufw allow 22/tcp
ufw allow 31000/tcp
ufw enable
```

如果你使用云厂商控制台，还需要在云厂商的安全组中放行：

```text
TCP 31000
```

## 三、创建 GHCR 读取令牌

在 GitHub 网页中：

```text
头像
→ Settings
→ Developer settings
→ Personal access tokens
→ Tokens (classic)
→ Generate new token
```

勾选：

```text
read:packages
```

生成 Token 后只保存到服务器，不要上传到仓库，也不要发给别人。

在服务器登录 GHCR：

```bash
read -rsp "GHCR Token: " GHCR_READ_TOKEN
echo
printf '%s' "$GHCR_READ_TOKEN" | docker login ghcr.io -u 1240748922 --password-stdin
unset GHCR_READ_TOKEN
```

看到以下内容说明成功：

```text
Login Succeeded
```

如果出现 `denied`，确认输入的是 GHCR Token 而不是 GitHub 登录密码，Token 包含 `read:packages`，用户名是 GitHub 用户名 `1240748922`，并且该账号有权访问这个镜像包。

## 四、下载项目

在服务器执行：

```bash
cd /opt
git clone https://github.com/1240748922/chatgpt2api-31000.git
cd chatgpt2api-31000
```

命令中必须使用纯文本 HTTPS 地址，不能把网页上的 Markdown 链接直接粘贴到终端。例如可用的地址是：

```text
https://github.com/1240748922/chatgpt2api-31000.git
```

如果仓库是私有的，Git 提示输入账号时填写：

```text
Username: 你的 GitHub 用户名
Password: 粘贴 GitHub 仓库 Token，不是 GitHub 登录密码
```

GitHub 已经不支持用账号登录密码进行 Git HTTPS 操作。GitHub 仓库 Token 可在以下位置创建：

```text
GitHub Settings
→ Developer settings
→ Personal access tokens
→ Fine-grained tokens
→ Generate new token
```

选择仓库 `chatgpt2api-31000`，将 `Repository permissions → Contents` 设置为 `Read-only`。

以后建议项目固定放在：

```text
/opt/chatgpt2api-31000
```

## 五、创建服务器配置文件

复制主项目配置：

```bash
cp .env.example .env
cp config.example.json config.json
```

复制注册机配置：

```bash
cp GPT-Register-Tool-main/config.example.json GPT-Register-Tool-main/config.json
```

创建数据目录：

```bash
mkdir -p data
mkdir -p GPT-Register-Tool-main/sessions
mkdir -p GPT-Register-Tool-main/runtime
```

这几个文件和目录已被 `.gitignore` 排除，不会上传到 GitHub；Compose 也会把它们挂载到容器中，所以拉取新镜像不会覆盖服务器上的真实配置和运行数据。

当前需要保留在服务器上的配置和数据如下：

```text
.env
config.json
data/
GPT-Register-Tool-main/config.json
GPT-Register-Tool-main/sessions/
GPT-Register-Tool-main/runtime/
```

这些内容不会通过 Git 上传，也不会被新的 Docker 镜像覆盖。

## 六点一、新维护方案（仅测试版）

默认稳定镜像 `sha-16e7193` 不包含以下新方案。只有主动选择 `sha-f1648e6` 或后续包含该方案的镜像时，这些负载门控才会生效。普通稳定版不需要配置以下三项变量。

后台账号同步、自动清理和自动补号会读取 8 个实例的实时生图负载。默认只有在生图活动数不超过 2 且没有等待队列时才执行维护，因此不会为了维护任务降低生图线程池并发。

当已确认可用账号低于“最低可用”值时，自动补号作为紧急任务放行；注册机仍在独立后台线程和子进程中运行，成功账号会逐个导入并同步额度。手动注册也会绕过低负载等待，但不会修改生图并发配置。

如需调整门槛，在服务器 `.env` 中设置：

```env
CHATGPT2API_MAINTENANCE_IMAGE_ACTIVE_MAX=2
CHATGPT2API_MAINTENANCE_IMAGE_WAITING_MAX=0
CHATGPT2API_MAINTENANCE_RETRY_SECONDS=30
```

修改后执行 `docker compose --env-file .env up -d --force-recreate` 使配置生效。

8 个 API 实例和 Nginx 网关必须使用同一个服务器 `data/` 目录。当前
`docker-compose.yml` 会将该目录以读写方式挂载到 `app0` 到 `app7`，以只读方式挂载到
`gateway`；网关会直接提供本地图片，避免图片 URL 再经过 `least_conn` 随机分流。

## 六、编辑主项目配置

打开 `.env`：

```bash
nano .env
```

至少修改这些内容：

```env
CHATGPT2API_IMAGE_TAG=sha-16e7193
POSTGRES_PASSWORD=改成一个长密码
CHATGPT2API_AUTH_KEY=改成你的API访问密钥
CHATGPT2API_MONITOR_CLUSTER_SECRET=改成一个随机字符串
CHATGPT2API_DATA_DIR=./data
CHATGPT2API_CONFIG_FILE=./config.json
REGISTER_TOOL_CONFIG_FILE=./GPT-Register-Tool-main/config.json
REGISTER_TOOL_SESSIONS_DIR=./GPT-Register-Tool-main/sessions
REGISTER_TOOL_RUNTIME_DIR=./GPT-Register-Tool-main/runtime
```

保存 nano：

```text
Ctrl + O
回车
Ctrl + X
```

`CHATGPT2API_AUTH_KEY` 是你的 API 客户端调用接口时使用的密钥，不是 GitHub 密钥。

## 七、编辑注册机配置

打开：

```bash
nano GPT-Register-Tool-main/config.json
```

填写你实际使用的：

- ReMail 配置
- Outlook 或其他邮箱配置
- 代理地址
- 代理池
- 注册模式
- 邮箱服务密钥
- 其他注册机参数

不要把真实配置写入：

```text
config.example.json
```

只写入：

```text
GPT-Register-Tool-main/config.json
```

## 八、检查 Compose 配置

先确认三个真实配置文件存在：

```bash
test -f .env && test -f config.json && test -f GPT-Register-Tool-main/config.json
```

执行：

```bash
docker compose --env-file .env config -q
```

没有输出就表示 Compose 配置格式正确。

如果出现配置错误，先不要启动，把错误信息保留。

不要把 `docker compose config` 的完整输出公开，因为其中可能包含配置值。

## 九、拉取镜像并启动

先拉取 GHCR 镜像：

```bash
docker compose --env-file .env pull
```

然后启动：

```bash
docker compose --env-file .env up -d
```

查看容器状态：

```bash
docker compose --env-file .env ps
```

确认挂载一致：

```bash
for c in $(docker compose ps -q app0 app6 gateway); do
  echo "===== $c ====="
  docker inspect "$c" --format '{{range .Mounts}}{{println .Source "->" .Destination}}{{end}}'
done
```

`app0`、`app6` 和 `gateway` 都应该能看到同一个宿主机 `data` 路径对应
`/app/src_extract/data`。其中 `gateway` 显示为只读是正常的。

正常情况下应该包含：

```text
postgres
app0
app1
app2
app3
app4
app5
app6
app7
gateway
```

查看日志：

```bash
docker compose --env-file .env logs -f --tail=100
```

退出日志查看：

```text
Ctrl + C
```

注意，`Ctrl + C` 只退出日志，不会停止容器。

## 十、访问系统

浏览器打开：

```text
http://你的服务器IP:31000
```

接口测试：

```bash
curl http://127.0.0.1:31000/version
```

如果服务器外部访问不到，检查：

```bash
docker compose ps
ufw status
```

并确认云厂商安全组已经放行 TCP `31000`。

## 十一、以后更新项目

本地修改代码后，在 GitHub Desktop 中：

```text
Commit to main
Push origin
```

然后打开 GitHub 的 `Actions` 页面，等待：

```text
Build and publish container image
```

变成绿色成功。

升级前按 [VERSIONING.md](./VERSIONING.md) 备份配置和数据库，并记录当前镜像。
回到服务器执行：

```bash
cd /opt/chatgpt2api-31000
git pull --ff-only
```

编辑 `.env` 的 `CHATGPT2API_IMAGE_TAG`，填入这次成功构建的 `sha-提交号前7位`。
例如 `sha-16e7193` 表示新维护方案之前的稳定应用，`sha-f1648e6` 表示新方案测试版。然后执行：

```bash
docker compose --env-file .env config -q
docker compose --env-file .env pull
docker compose --env-file .env up -d --force-recreate
docker compose --env-file .env ps
```

没有修改镜像版本时，`pull` 和 `up` 会继续使用锁定的应用镜像。
`git pull` 仍会更新 Compose、Nginx 等部署文件，因此它也应当视为一次部署变更。

不需要删除容器或数据库卷。

以后不需要：

```text
手动上传 tar.gz
docker load
重新安装 Python
重新安装注册机依赖
```

因为这些内容已经由 GitHub Actions 构建进 Docker 镜像。

## 十二、重要注意事项

更新时不要执行：

```bash
docker compose down -v
```

这个命令会删除 PostgreSQL 数据卷，可能导致账号、调用记录等数据丢失。

普通停止使用：

```bash
docker compose down
```

重新启动使用：

```bash
docker compose up -d
```

服务器上的这些文件不要删除：

```text
.env
config.json
data/
GPT-Register-Tool-main/config.json
GPT-Register-Tool-main/sessions/
GPT-Register-Tool-main/runtime/
```

最终结构大致是：

```text
/opt/chatgpt2api-31000/
├── .env
├── config.json
├── docker-compose.yml
├── Dockerfile
├── data/
├── src_extract/
├── GPT-Register-Tool-main/
│   ├── config.json
│   ├── sessions/
│   └── runtime/
└── .github/
    └── workflows/
        └── build-image.yml
```

日常更新按第十一节操作；新版异常时按 [VERSIONING.md](./VERSIONING.md) 切回稳定版。

如果 GitHub Actions 首次构建失败，进入对应的工作流查看具体错误日志即可。

## 十三、常见问题

### `git clone` 报 `Authentication failed`

GitHub 不接受账号登录密码进行 Git HTTPS 操作。用户名填写 GitHub 用户名，Password 位置粘贴具有仓库读取权限的 GitHub Token。不要粘贴只用于 GHCR 的 Token。

### `docker login ghcr.io` 报 `denied`

检查 GHCR Token 是否包含 `read:packages`，用户名是否正确，以及镜像包是否属于当前账号或当前账号有权访问。

### `docker compose pull` 报 `pull access denied`

重新登录 GHCR 后再拉取：

```bash
docker logout ghcr.io
docker login ghcr.io -u 1240748922
docker compose --env-file .env pull
```

Password 提示处粘贴 GHCR Token，不要粘贴 GitHub 登录密码。

### 更新后配置似乎消失

确认操作目录是服务器上的 `/opt/chatgpt2api-31000`，并确认修改的是 `.env`、`config.json` 和 `GPT-Register-Tool-main/config.json`，而不是 `*.example` 示例文件。
