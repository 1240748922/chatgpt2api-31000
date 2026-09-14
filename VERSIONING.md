# 稳定版本、升级和回退

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
| 稳定版（默认） | `sha-f6a3f02` | 新维护方案之前、已补齐 Sharp 运行依赖的版本 |
| 新方案修复版（main 默认） | `sha-ca72dea` | 加入按生图负载延后维护、低库存放行补号，并包含 Sharp 依赖修复 |

先前创建的 `stable-20260913` 标签误把新方案当成稳定版，已弃用；不要用它回退旧方案。为避免已拉取标签的电脑和服务器产生歧义，不重写该标签，改用上面的新稳定标签。
`main` 保留新方案代码，Compose 默认运行 `sha-ca72dea`。直接用源码启动或自行构建 `main` 也会运行新方案；需要旧方案源码时使用 `stable-before-maintenance-sharp-20260914`。
如果之前已经在服务器 `.env` 中写了 `CHATGPT2API_IMAGE_TAG=sha-f1648e6`，本次 `git pull` 不会覆盖它，必须手动改为 `sha-f6a3f02` 才会回到旧方案。

Git 标签保存源码和部署文件；GHCR 镜像保存已构建的应用。回退运行版本需要切换镜像，单独 `git pull` 或回退 Python 文件不够。
`latest` 会随新构建变化。稳定服务器使用具体的 `sha-xxxxxxx` 标签，并保留 GHCR 中相应的镜像版本，不要删除或重新指向其他镜像。PostgreSQL 和 Nginx 使用各自的镜像标签，应用版本锁定不等于数据库备份。

## 1. 先把当前应用锁定

服务器先拉取一次包含版本选择功能的部署文件：

```bash
cd /opt/chatgpt2api-31000
git pull --ff-only
nano .env
```

在 `.env` 中添加下面这一行；如果已经有这一项，就修改原来的值，不要重复添加：

```env
CHATGPT2API_IMAGE_TAG=sha-ca72dea
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

配置输出中的应用镜像应以 `:sha-ca72dea` 结尾。如果仍然是 `latest`，检查部署文件是否更新，或者终端是否设置了覆盖 `.env` 的同名环境变量。可用 `unset CHATGPT2API_IMAGE_TAG` 清除终端覆盖后重试。
Compose 未配置这项时也默认使用该修复版本，但建议写入 `.env`，使今后的 Git 更新继续保留你的选择。
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
docker image save -o "$RELEASE_BACKUP/stable-image.tar" ghcr.io/1240748922/chatgpt2api-31000:sha-ca72dea
```

镜像较大，需要预留磁盘空间。离线恢复时用 `docker image load -i 备份路径/stable-image.tar`，跳过在线 `pull`，再执行 `up`。镜像副本不包含数据库和挂载文件。

## 3. 选择一个新版进行测试

当前新维护方案的修复镜像是 `sha-ca72dea`。主动使用该方案时，把 `.env` 改成 `CHATGPT2API_IMAGE_TAG=sha-ca72dea`；如果需要恢复旧方案，改回 `sha-f6a3f02`。

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
