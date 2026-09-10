# 性能优化建议

## 问题分析

响应时间越来越长的原因：

1. **账号并发竞争** - 多个请求竞争同一批账号
2. **数据库连接不足** - 连接池被占满，后续请求等待
3. **线程池阻塞** - ChatGPT 流式响应占用线程时间长

## 优化配置

### 1. 增加数据库连接池

在 `docker-compose.yml` 中修改：

```yaml
environment: &env
  DATABASE_POOL_SIZE: 20           # 从 10 增加到 20
  DATABASE_MAX_OVERFLOW: 30        # 从 10 增加到 30
  DATABASE_POOL_TIMEOUT_SECONDS: 60 # 从 30 增加到 60
```

**理由：** 每个实例最多 50 个数据库连接（20+30），8 个实例总共 400 个连接。

### 2. 调整线程池配置

```yaml
  CHATGPT2API_THREAD_TOKENS: 200   # 从 160 增加到 200
  CHATGPT2API_IMAGE_THREAD_TOKENS: 100 # 从 140 降到 100
```

**理由：** 
- 增加主线程池容量，减少排队
- 图片生成较慢，降低并发避免阻塞

### 3. 增加可用账号数量

检查你的账号数量：
- 理想情况：每个 shard 至少 5-10 个可用账号
- 8 个 shard × 10 账号 = 80 个账号

### 4. PostgreSQL 优化

在 `docker-compose.yml` 的 postgres 服务中添加：

```yaml
postgres:
  image: postgres:17
  command:
    - postgres
    - -c
    - max_connections=500          # 默认 100，增加到 500
    - -c
    - shared_buffers=256MB         # 默认较小，增加缓存
    - -c
    - effective_cache_size=1GB
    - -c
    - maintenance_work_mem=128MB
```

## 临时快速修复

如果不想重启服务，可以：

### 降低并发测试数

在压测工具中：
- 并发数：改为 5-10（而不是 20）
- 总请求数：改为 50（而不是 100）

这样可以避免瞬间耗尽资源。

## 监控和诊断

### 1. 检查账号状态

访问你的 API 管理界面：
```
https://cc.deepwl.cn/api/accounts
```

查看可用账号数量和状态。

### 2. 检查实时监控

访问：
```
https://cc.deepwl.cn/api/monitor/realtime
```

查看：
- `account_wait_ms` - 等待账号的时间
- `entry_queue` - 入口排队情况
- `active_requests` - 当前活跃请求数

### 3. 数据库连接监控

SSH 到服务器：
```bash
docker-compose exec postgres psql -U chatgpt2api -c "SELECT count(*) FROM pg_stat_activity;"
```

查看当前数据库连接数。

## 推荐的完整配置

创建 `.env` 文件：
```bash
# 数据库配置
DATABASE_POOL_SIZE=20
DATABASE_MAX_OVERFLOW=30
DATABASE_POOL_TIMEOUT_SECONDS=60

# 线程池配置
CHATGPT2API_THREAD_TOKENS=200
CHATGPT2API_IMAGE_THREAD_TOKENS=100

# PostgreSQL
POSTGRES_MAX_CONNECTIONS=500
```

然后重启：
```bash
docker-compose down
docker-compose up -d
```

## 预期效果

优化后：
- 响应时间：应该稳定在 10-30s
- 不会越来越慢
- 并发 20 个请求应该能正常处理

如果还是慢，说明瓶颈在 ChatGPT API 本身或网络，而不是你的服务器配置。
