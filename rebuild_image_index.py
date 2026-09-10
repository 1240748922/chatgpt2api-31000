#!/usr/bin/env python3
"""重建图片索引文件"""

import json
import hashlib
from pathlib import Path
from datetime import datetime
from PIL import Image

def get_image_dimensions(image_path: Path) -> tuple[int, int] | None:
    """获取图片尺寸"""
    try:
        with Image.open(image_path) as img:
            return img.size
    except Exception:
        return None

def rebuild_image_index():
    """重建图片索引"""
    # 项目路径
    base_dir = Path(__file__).parent
    data_dir = base_dir / "data"
    images_dir = data_dir / "images"
    index_file = data_dir / "image_index.json"

    if not images_dir.exists():
        print(f"图片目录不存在: {images_dir}")
        return

    print(f"扫描图片目录: {images_dir}")

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

        # 生成唯一 ID
        generation = hashlib.md5(rel_path.encode()).hexdigest()

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
            print(f"已扫描 {count} 个图片文件...")

    print(f"共找到 {count} 个图片文件")

    # 保存索引文件
    index_data = {"items": items}

    # 备份旧索引（如果存在）
    if index_file.exists():
        backup_file = index_file.with_suffix('.json.backup')
        print(f"备份旧索引到: {backup_file}")
        index_file.rename(backup_file)

    # 写入新索引
    print(f"写入索引文件: {index_file}")
    with open(index_file, 'w', encoding='utf-8') as f:
        json.dump(index_data, f, ensure_ascii=False, indent=2)

    print(f"✓ 索引重建完成！共索引 {count} 个图片文件")

if __name__ == '__main__':
    rebuild_image_index()
