#!/usr/bin/env python3
"""
服务器端图片索引修复工具
用于修复图片文件存在但返回 404 的问题
"""

import json
import hashlib
from pathlib import Path
from datetime import datetime
from uuid import uuid4
import sys

def get_image_dimensions(image_path: Path):
    """获取图片尺寸"""
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            return img.size
    except Exception:
        return None

def fix_server_image_index():
    """修复服务器端图片索引"""

    # 确定项目路径 - 根据实际部署调整
    script_dir = Path(__file__).parent

    # 尝试多个可能的路径
    possible_paths = [
        script_dir / "src_extract",
        script_dir,
        Path("/app/src_extract") if Path("/app").exists() else None,
    ]

    base_dir = None
    for path in possible_paths:
        if path and path.exists() and (path / "data").exists():
            base_dir = path
            break

    if not base_dir:
        print("错误: 无法找到项目目录")
        print("请确认脚本位置或手动指定路径")
        return False

    data_dir = base_dir / "data"
    images_dir = data_dir / "images"
    index_file = data_dir / "image_index.json"

    print("=" * 70)
    print("服务器端图片索引修复工具")
    print("=" * 70)
    print(f"项目目录: {base_dir}")
    print(f"数据目录: {data_dir}")
    print(f"图片目录: {images_dir}")
    print(f"索引文件: {index_file}")

    if not images_dir.exists():
        print(f"\n错误: 图片目录不存在 {images_dir}")
        return False

    # 1. 读取现有索引
    print("\n[步骤 1] 读取现有索引")
    existing_items = {}
    if index_file.exists():
        try:
            with open(index_file, 'r', encoding='utf-8') as f:
                data = json.load(f)
                existing_items = data.get('items', {})
            print(f"  ✓ 现有索引条目: {len(existing_items)}")
        except Exception as e:
            print(f"  ⚠ 读取现有索引失败: {e}")
            print(f"  将创建新索引")
    else:
        print(f"  索引文件不存在，将创建新文件")

    # 2. 扫描图片文件
    print("\n[步骤 2] 扫描图片文件")
    image_extensions = {'.png', '.jpg', '.jpeg', '.webp'}
    scanned_files = []

    for image_path in images_dir.rglob('*'):
        if not image_path.is_file():
            continue
        if image_path.suffix.lower() not in image_extensions:
            continue

        rel_path = image_path.relative_to(images_dir).as_posix()
        scanned_files.append((rel_path, image_path))

    print(f"  ✓ 扫描到图片文件: {len(scanned_files)}")

    # 3. 比对并更新索引
    print("\n[步骤 3] 更新索引")
    updated_items = dict(existing_items)  # 保留现有索引
    added_count = 0
    updated_count = 0

    for rel_path, image_path in scanned_files:
        stat = image_path.stat()
        file_size = stat.st_size
        mtime = datetime.fromtimestamp(stat.st_mtime)

        # 检查是否需要添加或更新
        existing_item = existing_items.get(rel_path)
        needs_update = False

        if not existing_item:
            needs_update = True
            added_count += 1
        elif existing_item.get('size') != file_size:
            # 文件大小改变，需要更新
            needs_update = True
            updated_count += 1

        if needs_update:
            # 获取图片尺寸
            dimensions = get_image_dimensions(image_path)

            # 从路径提取日期
            path_parts = rel_path.split('/')
            if len(path_parts) >= 3 and all(p.isdigit() for p in path_parts[:3]):
                date_str = '-'.join(path_parts[:3])
            else:
                date_str = mtime.strftime('%Y-%m-%d')

            # 保留原有的 generation 或生成新的
            generation = existing_item.get('generation') if existing_item else uuid4().hex

            # 构建索引项
            item = {
                "rel": rel_path,
                "path": rel_path,
                "name": image_path.name,
                "date": date_str,
                "size": file_size,
                "created_at": existing_item.get('created_at') if existing_item else mtime.strftime('%Y-%m-%d %H:%M:%S'),
                "storage": "local",
                "local": True,
                "webdav": False,
                "generation": generation,
            }

            if dimensions:
                item["width"] = dimensions[0]
                item["height"] = dimensions[1]

            updated_items[rel_path] = item

    print(f"  ✓ 新增索引: {added_count}")
    print(f"  ✓ 更新索引: {updated_count}")
    print(f"  ✓ 总索引数: {len(updated_items)}")

    # 4. 保存索引
    print("\n[步骤 4] 保存索引")

    # 备份现有索引
    if index_file.exists():
        backup_file = index_file.with_suffix('.json.backup')
        try:
            with open(index_file, 'r', encoding='utf-8') as f:
                backup_content = f.read()
            with open(backup_file, 'w', encoding='utf-8') as f:
                f.write(backup_content)
            print(f"  ✓ 已备份到: {backup_file.name}")
        except Exception as e:
            print(f"  ⚠ 备份失败: {e}")

    # 写入新索引
    try:
        index_data = {"items": updated_items}
        with open(index_file, 'w', encoding='utf-8') as f:
            json.dump(index_data, f, ensure_ascii=False, indent=2)
        print(f"  ✓ 索引已保存")
    except Exception as e:
        print(f"  ✗ 保存失败: {e}")
        return False

    # 5. 验证修复
    print("\n[步骤 5] 验证修复")
    example_path = "2026/09/10/1789053756_3e5833e0938852976f89b0b14d5bbc73.png"
    example_file = images_dir / example_path

    if example_file.exists():
        print(f"  示例文件: {example_path}")
        print(f"    - 文件存在: ✓")
        print(f"    - 已索引: {'✓' if example_path in updated_items else '✗'}")

    print("\n" + "=" * 70)
    print("✓ 修复完成！")
    print(f"  - 新增: {added_count} 个")
    print(f"  - 更新: {updated_count} 个")
    print(f"  - 总计: {len(updated_items)} 个")
    print("\n现在请重启服务或等待自动重载，所有图片 URL 应该能正常访问了")
    print("=" * 70)

    return True

if __name__ == '__main__':
    try:
        success = fix_server_image_index()
        sys.exit(0 if success else 1)
    except Exception as e:
        print(f"\n✗ 执行失败: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)
