#!/bin/bash
# 服务器端全面诊断和修复脚本

echo "=========================================="
echo "图片 404 问题诊断和修复"
echo "=========================================="

# 颜色定义
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# 1. 检查数据目录挂载
echo -e "\n${YELLOW}[1/6] 检查数据目录挂载${NC}"
echo "当前目录: $(pwd)"
echo "CHATGPT2API_DATA_DIR: ${CHATGPT2API_DATA_DIR:-./data}"

DATA_DIR="${CHATGPT2API_DATA_DIR:-./data}"
if [ -d "$DATA_DIR" ]; then
    echo -e "${GREEN}✓ 数据目录存在: $DATA_DIR${NC}"
    echo "  目录内容:"
    ls -lh "$DATA_DIR" | head -10
else
    echo -e "${RED}✗ 数据目录不存在: $DATA_DIR${NC}"
    exit 1
fi

# 2. 检查 images 目录
echo -e "\n${YELLOW}[2/6] 检查 images 目录${NC}"
IMAGES_DIR="$DATA_DIR/src_extract/data/images"
if [ -d "$IMAGES_DIR" ]; then
    IMAGE_COUNT=$(find "$IMAGES_DIR" -type f \( -name "*.png" -o -name "*.jpg" \) 2>/dev/null | wc -l)
    echo -e "${GREEN}✓ images 目录存在${NC}"
    echo "  图片文件数量: $IMAGE_COUNT"

    if [ $IMAGE_COUNT -gt 0 ]; then
        echo "  最近的图片:"
        find "$IMAGES_DIR" -type f \( -name "*.png" -o -name "*.jpg" \) -printf '%T@ %p\n' 2>/dev/null | sort -rn | head -5 | awk '{print "    " $2}'
    fi
else
    echo -e "${RED}✗ images 目录不存在: $IMAGES_DIR${NC}"
    mkdir -p "$IMAGES_DIR"
    echo -e "${GREEN}✓ 已创建目录${NC}"
fi

# 3. 检查索引文件
echo -e "\n${YELLOW}[3/6] 检查 image_index.json${NC}"
INDEX_FILE="$DATA_DIR/src_extract/data/image_index.json"
if [ -f "$INDEX_FILE" ]; then
    INDEX_SIZE=$(stat -c%s "$INDEX_FILE" 2>/dev/null || stat -f%z "$INDEX_FILE" 2>/dev/null)
    echo -e "${GREEN}✓ 索引文件存在${NC}"
    echo "  文件大小: $INDEX_SIZE 字节"

    if [ $INDEX_SIZE -lt 100 ]; then
        echo -e "${YELLOW}  ⚠ 索引文件可能为空或损坏${NC}"
        NEEDS_REBUILD=1
    else
        # 检查索引中的条目数
        INDEX_COUNT=$(python3 -c "import json; print(len(json.load(open('$INDEX_FILE')).get('items', {})))" 2>/dev/null || echo "0")
        echo "  索引条目数: $INDEX_COUNT"

        if [ "$INDEX_COUNT" -lt "$IMAGE_COUNT" ]; then
            echo -e "${YELLOW}  ⚠ 索引不完整（$INDEX_COUNT < $IMAGE_COUNT）${NC}"
            NEEDS_REBUILD=1
        fi
    fi
else
    echo -e "${RED}✗ 索引文件不存在${NC}"
    NEEDS_REBUILD=1
fi

# 4. 检查特定的 404 文件
echo -e "\n${YELLOW}[4/6] 检查示例 404 文件${NC}"
EXAMPLE_PATH="2026/09/10/1789053756_3e5833e0938852976f89b0b14d5bbc73.png"
EXAMPLE_FILE="$IMAGES_DIR/$EXAMPLE_PATH"
if [ -f "$EXAMPLE_FILE" ]; then
    echo -e "${GREEN}✓ 示例文件存在: $EXAMPLE_PATH${NC}"
    echo "  文件大小: $(stat -c%s "$EXAMPLE_FILE" 2>/dev/null || stat -f%z "$EXAMPLE_FILE" 2>/dev/null) 字节"
else
    echo -e "${RED}✗ 示例文件不存在: $EXAMPLE_PATH${NC}"
fi

# 5. 重建索引（如果需要）
if [ "$NEEDS_REBUILD" = "1" ]; then
    echo -e "\n${YELLOW}[5/6] 重建索引文件${NC}"

    cd "$DATA_DIR/src_extract/data" || exit 1

    # 备份旧索引
    if [ -f "image_index.json" ]; then
        cp image_index.json "image_index.json.backup.$(date +%s)"
        echo "  ✓ 已备份旧索引"
    fi

    # 使用 Python 重建索引
    python3 << 'PYTHON_EOF'
import json
from pathlib import Path
from datetime import datetime
from uuid import uuid4

images_dir = Path("images")
if not images_dir.exists():
    print("  ✗ images 目录不存在")
    exit(1)

items = {}
count = 0

for img in images_dir.rglob("*"):
    if not img.is_file():
        continue
    if img.suffix.lower() not in {'.png', '.jpg', '.jpeg', '.webp'}:
        continue

    rel = img.relative_to(images_dir).as_posix()
    stat = img.stat()

    # 从路径提取日期
    path_parts = rel.split('/')
    if len(path_parts) >= 3 and all(p.isdigit() for p in path_parts[:3]):
        date_str = '-'.join(path_parts[:3])
    else:
        date_str = datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d')

    items[rel] = {
        "rel": rel,
        "path": rel,
        "name": img.name,
        "date": date_str,
        "size": stat.st_size,
        "created_at": datetime.fromtimestamp(stat.st_mtime).strftime('%Y-%m-%d %H:%M:%S'),
        "storage": "local",
        "local": True,
        "webdav": False,
        "generation": uuid4().hex
    }
    count += 1

with open("image_index.json", "w") as f:
    json.dump({"items": items}, f, indent=2)

print(f"  ✓ 已重建索引，包含 {count} 个图片")
PYTHON_EOF

    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✓ 索引重建成功${NC}"
    else
        echo -e "${RED}✗ 索引重建失败${NC}"
        exit 1
    fi
else
    echo -e "\n${GREEN}[5/6] 索引文件正常，无需重建${NC}"
fi

# 6. 验证修复
echo -e "\n${YELLOW}[6/6] 验证修复结果${NC}"
if [ -f "$INDEX_FILE" ]; then
    NEW_COUNT=$(python3 -c "import json; print(len(json.load(open('$INDEX_FILE')).get('items', {})))" 2>/dev/null || echo "0")
    echo "  索引条目数: $NEW_COUNT"

    if [ "$NEW_COUNT" -gt 0 ]; then
        echo -e "${GREEN}✓ 索引验证通过${NC}"
    fi
fi

# 7. 建议
echo -e "\n${YELLOW}=========================================="
echo "修复完成"
echo "==========================================${NC}"

if [ "$NEEDS_REBUILD" = "1" ]; then
    echo -e "\n${GREEN}✓ 已重建索引文件${NC}"
    echo -e "\n下一步操作:"
    echo "  1. 重启服务: docker-compose restart"
    echo "  2. 测试 URL: curl -I https://cc.deepwl.cn/images/2026/09/10/1789053756_3e5833e0938852976f89b0b14d5bbc73.png"
else
    echo -e "\n${GREEN}✓ 索引文件正常${NC}"
    echo -e "\n如果仍有 404 问题，可能是:"
    echo "  1. Docker 容器挂载路径不一致"
    echo "  2. Nginx 配置问题"
    echo "  3. 文件权限问题"
    echo -e "\n检查命令:"
    echo "  docker-compose exec gateway ls -la /app/data/src_extract/data/images/"
    echo "  docker-compose exec app0 ls -la /app/data/src_extract/data/"
fi

echo ""
