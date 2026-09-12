# 压测工具

`image_load_test.py` 默认请求 `http://127.0.0.1:31000/v1/images/generations`。
每个请求在收到生成结果后，会继续访问 `data[].url`，因此统计结果同时包含接口成功率和图片 URL 成功率。工具会分别记录三种耗时：

```text
api_elapsed_s    接口请求开始到接口响应返回的耗时，最接近号池系统的生图耗时
image_elapsed_s  接口返回后访问图片 URL 的耗时，包含图片下载和 URL 校验
elapsed_s        整个压测请求的总耗时，约等于 api_elapsed_s + image_elapsed_s
```

对比号池系统日志时，请使用图形界面的“接口耗时”或结果文件中的 `api_elapsed_s`；“总耗时”还包含图片 URL 校验时间，不能直接与后端生图耗时比较。

## 图形界面

```bash
python tools/image_load_test_gui.py
```

图形界面会实时显示完成数量、HTTP 成功率、图片 URL 有效率、接口耗时和总耗时的平均值、P50、P95、最大值、超过 60/140 秒数量及失败分类。明细表会分别显示接口耗时、图片 URL 校验耗时和总耗时。点击表头可以排序。

```bash
python tools/image_load_test.py \
  --api-key "$CHATGPT2API_AUTH_KEY" \
  --requests 20 \
  --concurrency 5
```

常用参数：

```text
--base-url             服务地址，默认 http://127.0.0.1:31000
--endpoint             接口路径，默认 /v1/images/generations
-n, --requests         请求总数，默认 10
-c, --concurrency      同时请求数，默认 1
--request-timeout      生图请求超时秒数，默认 190
--image-timeout        图片 URL 访问超时秒数，默认 30
--jsonl result.jsonl   保存每个请求的明细
```

退出码为 `0` 表示所有接口请求和图片 URL 都返回 2xx；只要有一个失败就返回 `1`。
