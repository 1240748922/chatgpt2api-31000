#!/usr/bin/env python3
"""修复图片索引文件 - 专门针对 src_extract 项目"""

import json
import hashlib
from pathlib import Path
from datetime import datetime
from uuid import uuid4

def get_image_dimensions(image_path: Path) -> tuple[int, int] | None:
    """获取图片尺寸"""
    try:
        from PIL import Image
        with Image.open(image_path) as img:
            return img.size
    except Exception as e:
        print(f"  警告: 无法读取图片尺寸 {image_path.name}: {e}")
        return None

def rebuild_image_index():
    """重建 src_extract 的图片索引"""
    # src_extract 的路径
    base_dir = Path(__file__).parent / "src_extract"
    data_dir = base_dir / "data"
    images_dir = data_dir / "images"
    index_file = data_dir / "image_index.json"

    print(f"项目目录: {base_dir}")
    print(f"数据目录: {data_dir}")
    print(f"图片目录: {images_dir}")

    if not images_dir.exists():
        print(f"\n✓ 图片目录不存在，创建目录: {images_dir}")
        images_dir.mkdir(parents=True, exist_ok=True)

    print(f"\n扫描图片目录: {images_dir}")

    # 扫描所有图片文件
    image_extensions = {'.png', '.jpg', '.jpeg', '.webp'}
    items = {}
    count = 0

    for image_path in images_dir.rglob('*'):
        if not image_path.is_file():
            continue

        if image_path.suffix.lower() not in image_extensions:
            continue

        # 计算相对路径
        rel_path = image_path.relative_to(images_dir).as_posix()

        # 获取文件信息
        stat = image_path.stat()
        file_size = stat.st_size
        mtime = datetime.fromtimestamp(stat.st_mtime)

        # 获取图片尺寸
        dimensions = get_image_dimensions(image_path)

        # 从路径提取日期（格式：YYYY/MM/DD/filename.png）
        path_parts = rel_path.split('/')
        if len(path_parts) >= 4:
            date_str = '-'.join(path_parts[:3])
        else:
            date_str = mtime.strftime('%Y-%m-%d')

        # 生成唯一 generation ID
        generation = uuid4().hex

        # 构建索引项
        item = {
            "rel": rel_path,
            "path": rel_path,
            "name": image_path.name,
            "date": date_str,
            "size": file_size,
            "created_at": mtime.strftime('%Y-%m-%d %H:%M:%S'),
            "storage": "local",
            "local": True,
            "webdav": False,
            "generation": generation,
        }

        # 添加尺寸信息
        if dimensions:
            item["width"] = dimensions[0]
            item["height"] = dimensions[1]

        items[rel_path] = item
        count += 1

        if count % 100 == 0:
            print(f"  已扫描 {count} 个图片文件...")

    print(f"\n共找到 {count} 个图片文件")

    # 保存索引文件
    index_data = {"items": items}

    # 备份旧索引（如果存在）
    if index_file.exists():
        backup_file = index_file.with_suffix('.json.backup')
        print(f"备份旧索引到: {backup_file}")
        with open(index_file, 'r', encoding='utf-8') as f:
            old_content = f.read()
        with open(backup_file, 'w', encoding='utf-8') as f:
            f.write(old_content)

    # 写入新索引
    print(f"写入索引文件: {index_file}")
    with open(index_file, 'w', encoding='utf-8') as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)

    print(f"\n✓ 索引文件已创建/更新！")
    print(f"  - 索引文件路径: {index_file}")
    print(f"  - 图片数量: {count}")
    print(f"  - 状态: {'空索引（正常）' if count == 0 else '包含 ' + str(count) + ' 个图片'}")

    # 验证索引文件
    if index_file.exists():
        print(f"\n✓ 验证: 索引文件已成功创建")
        with open(index_file, 'r', encoding='utf-8') as f:
            verify_data = json.load(f)
            verify_count = len(verify_data.get('items', {}))
            print(f"  - 验证通过: 索引包含 {verify_count} 个条目")
    else:
        print(f"\n✗ 错误: 索引文件创建失败")
        return False

    return True

if __name__ == '__main__':
    print("=" * 60)
    print("修复图片索引文件 - ChatGPT2API")
    print("=" * 60)

    success = rebuild_image_index()

    print("\n" + "=" * 60)
    if success:
        print("✓ 修复完成！")
        print("\n说明:")
        print("1. 如果图片目录是空的，这是正常的")
        print("2. 当新图片生成时，系统会自动添加到索引中")
        print("3. 现在所有图片 URL 请求应该能正常工作了")
    else:
        print("✗ 修复失败，请检查错误信息")
    print("=" * 60)
