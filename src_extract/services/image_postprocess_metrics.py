"""Shared whitelist for persisted attempts and realtime postprocess events."""

POSTPROCESS_METRIC_LABELS = {
    "upscale_ms": "超分总耗时",
    "upscale_queue_ms": "超分排队",
    "upscale_exec_ms": "超分处理",
    "storage_ms": "保存图片",
    "storage_lock_ms": "图片锁等待",
    "storage_write_ms": "图片文件写入",
    "storage_remote_ms": "远程存储上传",
    "storage_catalog_ms": "图片索引提交",
    "postprocess_ms": "后处理总计",
}
