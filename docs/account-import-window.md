# 3.2.20：固定导入窗口与最小化

## 修改范围

- “导入账号”和“任务日志”使用页签切换，提交成功后自动显示任务日志，不再把表单、统计、明细与原始日志纵向堆在一起。
- 明细和原始日志各自使用固定区域内滚动。明细每页渲染 100 条，筛选和搜索对当前已加载的全部记录生效。这只是前端展示分页，不改变后台入库批次、导入数量或兑换并发。
- 标题栏增加减号图标；最小化后右下角显示任务阶段与进度。再次点击浮动条或者原“导入 / 添加”菜单可以恢复。
- 最小化时保留同一个面板和轮询控制器，不重新提交任务，保留输入草稿、同步选项、当前页签和筛选条件；释放页面遮罩、焦点限制和滚动锁。
- 关闭浮动条仍不会取消已经提交的后台任务。重新打开可以通过原有任务历史恢复查看。未提交的输入草稿只在内存中保留，不写入浏览器存储。

导入控制器、解析函数和后台服务保持不变；未修改分块/批量入库、RT 兑换、额度同步、生图、数据库结构、Compose、Nginx 和并发配置。

## 原因和验证

该仓库只有编译后的前端，没有 Tailwind 重编译过程。3.2.19 使用的 `max-h-64` / `max-h-48` 类在现有样式文件里没有对应规则，实际上没有限制明细高度。Chromium 中放入 1002 条合成处理记录后，原表格容器的 `clientHeight` 和 `scrollHeight` 都为 31092，不能在容器内部滚动。本版使用独立、带命名空间的明确 CSS，不依赖不存在的工具类。

完整页面浏览器测试使用合成 API 响应，不写部署账号库，不访问真实上游。覆盖 1000 条明细、400 条原始日志、分页、内部滚动、桌面/窄屏布局、最小化后读取文件和提交完成、持续轮询、重复恢复不重复提交、恢复草稿/筛选、跨页面返回、关闭浮动条、原 OAuth 控件以及之前的 AT/RT/JSON 导入交互。

```powershell
python -m playwright install chromium
python -m pytest src_extract/tests/test_account_import_browser.py -q
node src_extract/tests/test_account_import_web.cjs
node src_extract/tests/test_web_runtime.cjs
$env:PYTHONPATH="$PWD\src_extract;$PWD\GPT-Register-Tool-main"
python -m pytest src_extract/tests -q
```

重放旧版高度缺陷（预期失败在滚动区域高度断言）：

```powershell
$env:IMPORT_UI_BASELINE='636dce8'
python -m pytest src_extract/tests/test_account_import_browser.py::test_large_log_scroll_is_bounded -q --tb=short
Remove-Item Env:IMPORT_UI_BASELINE
```

本地截图位于被忽略的 `.runtime/import-ui-fixed.png`、`.runtime/import-tabs-logs.png`、`.runtime/import-minimized.png`，全部为合成账号。升级无需迁移数据或配置；部署后应确认应用版本 `3.2.20` 及相应容器 OCI revision。
