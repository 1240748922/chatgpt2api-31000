# 注册出口与账号运行出口隔离（3.2.49）

## 改动原因与边界

旧的自动补号流程在注册进程完成后读取 `registration_proxy`；固定代理或单条代理池会被回填到账号的 `proxy`。这会把注册专用、可能短期轮换的出口变成后续额度同步及生图的账号级覆盖。

本版取消这个回填。自动补号的 `_import_sessions()` 在调用账号导入前去除 session 的 `proxy`、`proxy_group_id`、`proxy_mode`、`registration_proxy` 和 `account_proxy` 字段；不改原 session 文件，不写强制直连，不改注册代理配置。session 筛选、批次隔离、重复路径去重、批量导入和额度同步重试保持原样。

- 新账号无单独代理绑定：有账号组则沿用已有组规则，否则使用全局默认出口；没有代理时直连。
- 已有账号不迁移：缺少注册/手动绑定的可靠出处，不能自动批量清空。重复补号导入保留原手动绑定，需要解绑的历史账号可通过账号编辑选择“使用默认出口”。
- 普通手动导入保留原行为，不过滤用户显式指定的代理。
- 注册预检、注册连接及注册失败后的出口重试仍使用注册机代理。
- 未改变 AT/RT 准入、额度调度、分片租约、生图重试、预热、并发或数据库结构。

## 日志与编辑显示

新补号结果包含 `proxy_assigned=false` 和 `proxy_policy=inherit`；初始日志和轮询都显示“账号出口：默认规则（不绑定注册代理）”。没有新字段的历史记录仍根据 `proxy_assigned` 显示原绑定结果，不把历史批次误标为未绑定。

账号列表的 `custom`、主机端口标签是脱敏投影。编辑框从 `configuration.proxy` 读取完整值，避免把 `custom` 保存为地址；代理摘要继续脱敏。仅改编辑控件以及相关缓存引用，不改变 Vue 运行时 URL。

## 离线复验

PowerShell 在仓库根目录运行（只使用合成账号、临时 SQLite 及拦截的上游，绝不实际注册或生图）：

```powershell
$env:PYTHONIOENCODING='utf-8'
$env:PYTHONPATH="$pwd\src_extract;$pwd\GPT-Register-Tool-main"
python -m pytest src_extract/tests/test_replenishment_proxy_import.py src_extract/tests/test_replenishment_status.py GPT-Register-Tool-main/tests/test_registration_proxy_persistence.py -q
python -m pytest src_extract/tests/test_account_proxy_browser.py src_extract/tests/test_replenishment_browser.py -q
node src_extract/tests/test_account_proxy_web.cjs
node src_extract/tests/test_web_runtime.cjs
```

`test_replenishment_proxy_import.py` 使用真实临时数据库验证注册 session → 批量导入 → 持久化账号 → 运行时出口，以及完整 `_run_once()` → 默认出口下额度同步 → 持久化任务结果。覆盖 HTTP/SOCKS、旧残缺 `custom`、失效代理组、强制直连字段的隔离，已有手动代理保留及全局/账号组继承。浏览器测试使用实际发布资产，验证编辑、测试、保存、取消和新旧日志；既有注册机滚动回归也保留。

## 用户更新后核验

1. 先核对 `/version` 为 `3.2.49`，镜像标签须与 `VERSIONING.md` 的已发布标签一致；不要只靠 `git pull` 判断应用已更新。
2. 按 Ctrl+F5 刷新。下一次按原配置补号后，新导入账号的独立代理应为空/使用默认出口；注册配置中的代理保持不变。
3. 查看补号日志应显示不绑定注册代理，再核对导入与额度同步结果。注册出口的 403 仍属于注册预检失败，不会因这项隔离自动消失。
4. 旧账号不会自动解绑。若确实要移除其代理，先确认账号范围，再使用账号编辑/批量编辑选择使用默认出口。

尚未提交的 `test_image_quota_cooldown.py` 草稿不属于本版，完整回归显式排除它。
