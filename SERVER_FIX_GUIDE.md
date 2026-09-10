# 服务器端图片 404 问题修复指南

## 问题描述
部分图片 URL 返回 404，例如：
```
https://cc.deepwl.cn/images/2026/09/10/1789053756_3e5833e0938852976f89b0b14d5bbc73.png
```

## 根本原因
图片文件存在于服务器磁盘上，但 `image_index.json` 索引文件中没有对应的记录，导致系统返回 404。

## 解决步骤

### 方法 1: 自动修复（推荐）

1. **上传修复脚本到服务器**
   将 `fix_server_images.py` 上传到服务器项目根目录

2. **SSH 连接到服务器**
   ```bash
   ssh user@cc.deepwl.cn
   ```

3. **进入项目目录**
   ```bash
   cd /path/to/your/project  # 替换为实际路径
   ```

4. **运行修复脚本**
   ```bash
   python3 fix_server_images.py
   ```

5. **重启服务（如果需要）**
   ```bash
   # 如果是使用 systemd
   sudo systemctl restart your-service-name
   
   # 或者如果是 Docker
   docker restart your-container-name
   
   # 或者如果使用 supervisor
   sudo supervisorctl restart your-app
   ```

### 方法 2: 手动修复

如果无法运行脚本，可以手动创建索引：

1. **SSH 连接到服务器并进入数据目录**
   ```bash
   cd /path/to/project/src_extract/data
   ```

2. **检查图片文件**
   ```bash
   ls -lh images/2026/09/10/
   ```

3. **手动创建或更新索引**
   ```bash
   # 备份现有索引（如果存在）
   cp image_index.json image_index.json.backup
   
   # 运行 Python 创建索引
   python3 << 'EOF'
   import json
   from pathlib import Path
   from datetime import datetime
   from uuid import uuid4
   
   images_dir = Path("images")
   items = {}
   
   for img in images_dir.rglob("*.png"):
       rel = img.relative_to(images_dir).as_posix()
       stat = img.stat()
       items[rel] = {
           "rel": rel,
           "path": rel,
           "name": img.name,
           "date": "-".join(rel.split("/")[:3]),
           "size": stat.st_size,
           "created_at": datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
           "storage": "local",
           "local": True,
           "webdav": False,
           "generation": uuid4().hex
       }
   
   with open("image_index.json", "w") as f:
       json.dump({"items": items}, f, indent=2)
   
   print(f"已创建索引，包含 {len(items)} 个图片")
   EOF
   ```

### 方法 3: 使用 API 触发刷新（如果项目支持）

如果项目有管理接口：

```bash
# 访问管理界面触发索引刷新
curl -X POST https://cc.deepwl.cn/api/images/refresh-index \
  -H "Authorization: Bearer YOUR_TOKEN"
```

## 验证修复

修复完成后，验证问题是否解决：

```bash
# 测试之前 404 的图片
curl -I https://cc.deepwl.cn/images/2026/09/10/1789053756_3e5833e0938852976f89b0b14d5bbc73.png

# 应该返回 HTTP 200
```

## 防止未来出现此问题

1. **定期备份索引文件**
   ```bash
   # 添加到 crontab
   0 2 * * * cp /path/to/src_extract/data/image_index.json /path/to/backups/image_index.json.$(date +\%Y\%m\%d)
   ```

2. **启用自动索引刷新**
   检查项目配置中的 `refresh_index` 设置是否启用

3. **监控索引状态**
   添加监控脚本检测索引文件和实际文件的一致性

## 常见问题

### Q: 为什么有些图片能访问，有些不能？
A: 能访问的图片在索引中有记录，不能访问的图片可能是：
   - 最近生成但索引未更新
   - 索引文件损坏或丢失部分记录
   - 手动复制的文件未添加到索引

### Q: 修复后是否需要重启服务？
A: 通常需要。索引文件在服务启动时加载，修改后需要重启服务才能生效。

### Q: 会影响现有的图片访问吗？
A: 不会。修复脚本会保留现有索引记录，只添加缺失的条目。

## 联系支持
如果问题仍未解决，请提供：
- 服务器日志
- `image_index.json` 文件内容（前 100 行）
- 图片目录结构 `ls -R images/ | head -100`
