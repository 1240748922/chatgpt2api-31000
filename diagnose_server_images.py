#!/usr/bin/env python3
"""诊断服务器图片索引问题"""

import json
from pathlib import Path

def diagnose_image_index():
    """诊断图片索引问题"""

    # src_extract 的路径
    base_dir = Path(__file__).parent / "src_extract"
    data_dir = base_dir / "data"
    images_dir = data_dir / "images"
    index_file = data_dir / "image_index.json"

    print("=" * 70)
    print("图片索引诊断工具")
    print("=" * 70)

    # 1. 检查索引文件
    print("\n[1] 检查索引文件")
    if not index_file.exists():
        print(f"  ✗ 索引文件不存在: {index_file}")
        return

    print(f"  ✓ 索引文件存在: {index_file}")

    # 2. 读取索引内容
    print("\n[2] 读取索引内容")
    try:
        with open(index_file, 'r', encoding='utf-8') as f:
            index_data = json.load(f)
        items = index_data.get('items', {})
        print(f"  ✓ 索引中的图片数量: {len(items)}")
    except Exception as e:
        print(f"  ✗ 读取索引失败: {e}")
        return

    # 3. 扫描实际文件
    print("\n[3] 扫描实际图片文件")
    image_extensions = {'.png', '.jpg', '.jpeg', '.webp'}
    actual_files = []

    if images_dir.exists():
        for image_path in images_dir.rglob('*'):
            if image_path.is_file() and image_path.suffix.lower() in image_extensions:
                rel_path = image_path.relative_to(images_dir).as_posix()
                actual_files.append((rel_path, image_path))
        print(f"  ✓ 实际图片文件数量: {len(actual_files)}")
    else:
        print(f"  ✗ 图片目录不存在: {images_dir}")
        return

    # 4. 对比分析
    print("\n[4] 对比分析")
    indexed_paths = set(items.keys())
    actual_paths = {rel_path for rel_path, _ in actual_files}

    # 存在于磁盘但不在索引中的文件（会导致 404）
    missing_in_index = actual_paths - indexed_paths
    # 存在于索引但不在磁盘中的文件
    missing_in_disk = indexed_paths - actual_paths

    print(f"  - 磁盘文件但未索引（会 404）: {len(missing_in_index)} 个")
    print(f"  - 索引记录但无文件: {len(missing_in_disk)} 个")
    print(f"  - 正常（已索引且有文件）: {len(indexed_paths & actual_paths)} 个")

    # 5. 显示未索引的文件
    if missing_in_index:
        print("\n[5] 未索引的文件（这些会返回 404）:")
        for i, path in enumerate(sorted(missing_in_index)[:20], 1):
            print(f"  {i}. {path}")
        if len(missing_in_index) > 20:
            print(f"  ... 还有 {len(missing_in_index) - 20} 个文件")

    # 6. 显示索引但无文件的记录
    if missing_in_disk:
        print("\n[6] 索引中但文件已丢失:")
        for i, path in enumerate(sorted(missing_in_disk)[:10], 1):
            print(f"  {i}. {path}")
        if len(missing_in_disk) > 10:
            print(f"  ... 还有 {len(missing_in_disk) - 10} 个记录")

    # 7. 检查特定文件
    print("\n[7] 检查示例文件")
    example_path = "2026/09/10/1789053756_3e5833e0938852976f89b0b14d5bbc73.png"

    example_file = images_dir / example_path
    print(f"  示例路径: {example_path}")
    print(f"  文件存在: {'✓ 是' if example_file.exists() else '✗ 否'}")
    print(f"  索引存在: {'✓ 是' if example_path in indexed_paths else '✗ 否 (会返回 404!)'}")

    # 8. 解决方案
    print("\n" + "=" * 70)
    if missing_in_index:
        print("⚠️  发现问题：有 {} 个图片文件未被索引".format(len(missing_in_index)))
        print("\n解决方案:")
        print("  在服务器上运行以下命令重建索引：")
        print("  python fix_image_index.py")
    else:
        print("✓ 未发现索引问题")
    print("=" * 70)

if __name__ == '__main__':
    diagnose_image_index()
