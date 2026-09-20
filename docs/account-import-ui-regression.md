# 3.2.19：账号导入弹窗渲染回归

## 根因与复现证据

3.2.18（`ffa402a`）的手写 `LocalAccountImportPanel-v3.js` 把主 bundle 的 `a` 导出当作 `h` 使用。它实际是编译器使用的 `createBaseVNode`，不会像公开渲染 API 一样规范化 class 数组和单个 VNode 子节点。

在完整页面加载已有导入任务及账号处理明细后，Chromium 中出现 `RangeError: Invalid array length`；继续关闭时还可出现 `__asyncLoader` 读取异常。更新中断导致输入框、按钮和弹窗处于不同步的半更新状态。仅断言源文件里有事件处理器、或只测解析函数，无法发现这个问题。这不是通过改按钮文字或要求用户清缓存就能解决的问题。

同一浏览器测试，使用旧版静态资源时失败并报告上述 `Invalid array length`；使用修复后的静态资源时，可完成选文件、检查纯 token、点击提交、关闭及连续重开。所有网络业务接口均为合成响应，不使用真实 token，不写入部署账号池，不访问上游。

## 修复范围

- 使用现有 Vue runtime 的公开 `createVNode`，为手写渲染规范化单个 VNode 子节点，不引入第二份 Vue。
- 恢复原版 ModalHeader、ImportModePanel、Button、Checkbox 和 SVG 图标。保留两栏弹窗和原来的关闭方式，没有另造页面或改全站字体。
- AT/RT 页签从 JSON 对应的 `access_token` / `accessToken`、`refresh_token` / `refreshToken` 字段提取值，输入框每行一个 token；不再把整个 JSON 对象显示在 AT/RT 框里。选错凭据类型会明确提示，不把 AT 当 RT 提交。
- 输入框是唯一提交内容，文件读取完成后立即可编辑、可提交；允许同一文件重新选择。修复 `data` 数组中的多账号被折叠成首个账号的问题。
- CPA/Sub2API 的结构化内容仍保留 source、group、proxy 元数据，不能为了纯 token 显示而丢失这些设置。
- 读取或提交期间关闭弹窗，不让旧实例的异步完成回写新弹窗；已接受的后台任务仍可重新打开查看。
- 后端分批入库、RT 兑换、同步与日志机制保持不变。没有修改生图流程、数据库结构、Compose、Nginx 或用户配置。其他前端文件只统一静态资源版本标识，避免混用不同 URL 的 Vue runtime。

## 验证方式

从仓库根目录执行（需安装项目测试依赖、`pytest`、`playwright` 及其 Chromium）：

```powershell
python -m playwright install chromium
node src_extract/tests/test_account_import_web.cjs
node src_extract/tests/test_web_runtime.cjs
$env:PYTHONPATH="$PWD\src_extract;$PWD\GPT-Register-Tool-main"
python -m pytest src_extract/tests -q
```

浏览器回归覆盖已有日志、AT/RT 各 500 条嵌套 JSON、纯 token 回填、文件与粘贴合并、重复选文件、错误文件、带图标且可点击的提交按钮、反复关闭与重开、读取/提交过程中关闭、恢复后台任务、日志筛选、导入方式切换、目标分组、关闭额度同步以及 Session/CPA/Sub2API 兼容性。

用旧版资源重放同一个用例（预期失败，错误附带在测试输出中）：

```powershell
$env:IMPORT_UI_BASELINE='ffa402a'
python -m pytest src_extract/tests/test_account_import_browser.py::test_history_upload_submit_close_and_reopen -q --tb=short
Remove-Item Env:IMPORT_UI_BASELINE
```

如果仓库有被忽略的 `.runtime` 目录，成功用例会保存合成账号的页面截图到 `.runtime/import-ui-fixed.png`。旧日志和配置不需要迁移。上述验证不是用户服务器上的真实账号导入或上游压测；部署后应确认运行应用版本为 `3.2.19`，并核对运行容器的 OCI revision。
