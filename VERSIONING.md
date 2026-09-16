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
| 当前修复版（main 默认） | `sha-ca94823` | 上传/额度限制不消耗普通重试次数；修复跨实例导入被单账号写入隐藏、空池缓存刷新滞后和上传冷却丢失；保留超分耗时与可调超分并发等功能 |
| 上一版阶段诊断修复 | `sha-6064cdb` | 超时阶段与耗时归属修复、换号错误隔离、轮询状态诊断 |
| 更早的上传重试版 | `sha-087cf9c` | 上传受限自动换号、90 秒生图流与 120 秒轮询窗口、注册代理检查 |
| 上一版维护与超分版 | `sha-281017a` | 本次修复之前的维护与 CPU 超分方案 |
| 上一版维护修复镜像 | `sha-26d613e` | 按生图负载延后维护、低库存放行补号和 Sharp 依赖修复 |

先前创建的 `stable-20260913` 标签误把新方案当成稳定版，已弃用；不要用它回退旧方案。为避免已拉取标签的电脑和服务器产生歧义，不重写该标签，改用上面的新稳定标签。
`main` 保留新方案代码，Compose 默认运行 `sha-ca94823`。直接用源码启动或自行构建 `main` 也会运行新方案；需要旧方案源码时使用 `stable-before-maintenance-sharp-20260914`。
如果之前已经在服务器 `.env` 中写了 `CHATGPT2API_IMAGE_TAG=sha-f1648e6`，本次 `git pull` 不会覆盖它，必须手动改为 `sha-f6a3f02` 才会回到旧方案。

Git 标签保存源码和部署文件；GHCR 镜像保存已构建的应用。回退运行版本需要切换镜像，单独 `git pull` 或回退 Python 文件不够。
`latest` 会随新构建变化。稳定服务器使用具体的 `sha-xxxxxxx` 标签，并保留 GHCR 中相应的镜像版本，不要删除或重新指向其他镜像。PostgreSQL 和 Nginx 使用各自的镜像标签，应用版本锁定不等于数据库备份。

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

返回内容中的 `build_version`、`image_tag` 和 `build_time` 来自响应容器的环境变量。`CHATGPT2API_BUILD_VERSION` 只用于标识，不会改变镜像选择；镜像仍由 `CHATGPT2API_IMAGE_TAG` 决定。验证当前修复版时，下列命令应在每个 app 容器输出 `ca94823e0b2a920dbd1f923f9a3afb2ef17465f5`：

```bash
for service in app0 app1 app2 app3 app4 app5 app6 app7; do
  docker inspect "$(docker compose --env-file .env ps -q "$service")" --format '{{ index .Config.Labels "org.opencontainers.image.revision" }}'
done
```

本次先推送应用提交并单独构建 `sha-ca94823`，再推送 Compose 版本锁定提交。不要把两个提交合并在一次推送里，却默认认为父提交也生成了同名镜像；普通 `push main` 构建只针对推送后的 HEAD。

不设置 `CHATGPT2API_IMAGE_TAG` 时，日常更新跟随仓库 Compose 默认版本。需要暂停升级或回退时才在 `.env` 中显式指定标签；恢复自动跟随时删除该行。本次链路修复前的版本是 `sha-6064cdb`，更早的上传重试版本是 `sha-087cf9c`。

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

当前镜像为 `sha-ca94823`，对应 [GitHub Actions 构建](https://github.com/1240748922/chatgpt2api-31000/actions/runs/35131263333)。如果 `.env` 锁定旧标签，需要改为这个标签，或删除 `CHATGPT2API_IMAGE_TAG` 行后跟随仓库默认值，再拉取并重建。本次额外修复和回放说明见 [性能审查](./PERFORMANCE_AUDIT.md)，全套 97 项 Python 测试、前端运行检查和 Compose 配置检查通过；未在用户云服务器执行真实上游压测。

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
