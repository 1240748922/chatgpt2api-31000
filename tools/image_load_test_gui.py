#!/usr/bin/env python3
"""Tkinter GUI for tools/image_load_test.py."""

from __future__ import annotations

import concurrent.futures
import json
import queue
import statistics
import threading
import tkinter as tk
from dataclasses import asdict
from pathlib import Path
from tkinter import filedialog, messagebox, ttk

from image_load_test import Result, run_one


class LoadTestGui(tk.Tk):
    def __init__(self) -> None:
        super().__init__()
        self.title("生图接口压测工具")
        self.geometry("1040x680")
        self.minsize(900, 560)
        self.events: queue.Queue[tuple[str, object]] = queue.Queue()
        self.stop_event = threading.Event()
        self.worker: threading.Thread | None = None
        self.results: list[Result] = []
        self.vars = {
            "base_url": tk.StringVar(value="http://127.0.0.1:31000"),
            "endpoint": tk.StringVar(value="/v1/images/generations"),
            "api_key": tk.StringVar(),
            "requests": tk.StringVar(value="10"),
            "concurrency": tk.StringVar(value="1"),
            "model": tk.StringVar(value="gpt-image-2.5"),
            "request_timeout": tk.StringVar(value="190"),
            "image_timeout": tk.StringVar(value="30"),
            "prompt": tk.StringVar(value="A simple red apple on a white background"),
            "jsonl": tk.StringVar(),
        }
        self._build()
        self.after(100, self._drain_events)

    def _build(self) -> None:
        settings = ttk.LabelFrame(self, text="压测配置", padding=10)
        settings.pack(fill="x", padx=10, pady=10)
        fields = [
            ("服务地址", "base_url", 0, 0), ("接口路径", "endpoint", 0, 2),
            ("API 密钥", "api_key", 1, 0), ("模型", "model", 1, 2),
            ("请求总数", "requests", 2, 0), ("并发数", "concurrency", 2, 2),
            ("请求超时(s)", "request_timeout", 3, 0), ("图片超时(s)", "image_timeout", 3, 2),
            ("提示词", "prompt", 4, 0),
        ]
        for label, key, row, col in fields:
            ttk.Label(settings, text=label).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=4)
            entry = ttk.Entry(settings, textvariable=self.vars[key], show="*" if key == "api_key" else "")
            entry.grid(row=row, column=col + 1, sticky="ew", padx=(0, 18), pady=4)
        settings.columnconfigure(1, weight=1)
        settings.columnconfigure(3, weight=1)

        actions = ttk.Frame(self, padding=(10, 0))
        actions.pack(fill="x")
        self.start_button = ttk.Button(actions, text="开始压测", command=self.start)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(actions, text="停止", command=self.stop, state="disabled")
        self.stop_button.pack(side="left", padx=8)
        ttk.Button(actions, text="清空结果", command=self.clear).pack(side="left")
        ttk.Label(actions, text="结果文件（可选）").pack(side="left", padx=(24, 6))
        ttk.Entry(actions, textvariable=self.vars["jsonl"], width=34).pack(side="left")
        ttk.Button(actions, text="选择", command=self.choose_file).pack(side="left", padx=6)

        self.progress = ttk.Progressbar(self, mode="determinate")
        self.progress.pack(fill="x", padx=10, pady=(8, 4))
        self.summary = ttk.Label(self, text="等待开始")
        self.summary.pack(anchor="w", padx=10, pady=(0, 6))
        self.details = ttk.Label(self, text="统计：暂无数据", justify="left")
        self.details.pack(anchor="w", padx=10, pady=(0, 8))

        table_frame = ttk.Frame(self, padding=(10, 0))
        table_frame.pack(fill="both", expand=True)
        columns = ("id", "status", "api_elapsed", "image", "image_elapsed", "elapsed", "bytes", "error")
        self.table = ttk.Treeview(table_frame, columns=columns, show="headings")
        headings = {
            "id": "编号", "status": "接口状态", "api_elapsed": "接口耗时(s)",
            "image": "图片 URL 状态", "image_elapsed": "图片校验耗时(s)",
            "elapsed": "总耗时(s)", "bytes": "图片大小", "error": "错误",
        }
        widths = {
            "id": 55, "status": 80, "api_elapsed": 95, "image": 110,
            "image_elapsed": 115, "elapsed": 90, "bytes": 95, "error": 390,
        }
        for key in columns:
            self.table.heading(key, text=headings[key], command=lambda column=key: self._sort_table(column, False))
            self.table.column(key, width=widths[key], anchor="w")
        scroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.table.yview)
        self.table.configure(yscrollcommand=scroll.set)
        self.table.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")

    def choose_file(self) -> None:
        path = filedialog.asksaveasfilename(defaultextension=".jsonl", filetypes=[("JSON Lines", "*.jsonl")])
        if path:
            self.vars["jsonl"].set(path)

    def _settings(self) -> dict[str, object]:
        def integer(name: str) -> int:
            value = int(self.vars[name].get())
            if value < 1:
                raise ValueError(f"{name} 必须大于 0")
            return value

        base = self.vars["base_url"].get().strip().rstrip("/")
        endpoint = "/" + self.vars["endpoint"].get().strip().lstrip("/")
        key = self.vars["api_key"].get().strip()
        if not base or not key:
            raise ValueError("服务地址和 API 密钥不能为空")
        return {
            "url": base + endpoint, "api_key": key, "requests": integer("requests"),
            "concurrency": integer("concurrency"), "model": self.vars["model"].get().strip(),
            "request_timeout": float(self.vars["request_timeout"].get()),
            "image_timeout": float(self.vars["image_timeout"].get()),
            "prompt": self.vars["prompt"].get().strip(), "jsonl": self.vars["jsonl"].get().strip(),
        }

    def start(self) -> None:
        if self.worker and self.worker.is_alive():
            return
        try:
            settings = self._settings()
        except ValueError as exc:
            messagebox.showerror("配置错误", str(exc))
            return
        self.clear()
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.progress.configure(maximum=settings["requests"], value=0)
        self.worker = threading.Thread(target=self._run, args=(settings,), daemon=True)
        self.worker.start()

    def stop(self) -> None:
        self.stop_event.set()
        self.summary.configure(text="正在停止，等待当前请求结束…")

    def clear(self) -> None:
        self.results.clear()
        for item in self.table.get_children():
            self.table.delete(item)
        self.progress.configure(value=0)
        self.summary.configure(text="等待开始")
        self.details.configure(text="统计：暂无数据")

    def _sort_table(self, column: str, reverse: bool) -> None:
        items = [(self.table.set(item, column), item) for item in self.table.get_children("")]
        try:
            items.sort(key=lambda pair: float(pair[0]), reverse=reverse)
        except ValueError:
            items.sort(key=lambda pair: pair[0], reverse=reverse)
        for index, (_value, item) in enumerate(items):
            self.table.move(item, "", index)
        self.table.heading(column, command=lambda: self._sort_table(column, not reverse))

    @staticmethod
    def _error_category(result: Result) -> str:
        if result.status is None:
            return "连接/客户端异常"
        if result.status >= 500:
            return f"HTTP {result.status}"
        if result.status >= 400:
            return f"HTTP {result.status}"
        if not result.image_url_found:
            return "缺少图片 URL"
        if result.image_status and result.image_status >= 400:
            return f"图片 HTTP {result.image_status}"
        if result.error:
            return "图片 URL 校验异常"
        return "成功"

    def _update_stats(self) -> None:
        if not self.results:
            return
        values = sorted(r.elapsed_s for r in self.results)
        p50 = values[min(len(values) - 1, int((len(values) - 1) * 0.50))]
        p95 = values[min(len(values) - 1, int((len(values) - 1) * 0.95))]
        api_values = sorted(r.api_elapsed_s for r in self.results)
        api_p50 = api_values[min(len(api_values) - 1, int((len(api_values) - 1) * 0.50))]
        api_p95 = api_values[min(len(api_values) - 1, int((len(api_values) - 1) * 0.95))]
        http_ok = sum(r.status is not None and 200 <= r.status < 300 for r in self.results)
        image_ok = sum(r.image_status is not None and 200 <= r.image_status < 300 for r in self.results)
        slow60 = sum(r.elapsed_s > 60 for r in self.results)
        slow140 = sum(r.elapsed_s > 140 for r in self.results)
        categories: dict[str, int] = {}
        for result in self.results:
            category = self._error_category(result)
            if category != "成功":
                categories[category] = categories.get(category, 0) + 1
        errors = "，".join(f"{key} {value}" for key, value in categories.items()) or "无"
        self.details.configure(
            text=(f"接口：平均 {statistics.mean(api_values):.1f}s | P50 {api_p50:.1f}s | "
                  f"P95 {api_p95:.1f}s | 最大 {max(api_values):.1f}s\n"
                  f"总耗时：平均 {statistics.mean(values):.1f}s | P50 {p50:.1f}s | "
                  f"P95 {p95:.1f}s | 最大 {max(values):.1f}s | >60s {slow60} | >140s {slow140}\n"
                  f"HTTP 成功率 {http_ok}/{len(self.results)} | 图片有效率 {image_ok}/{len(self.results)}\n"
                  f"失败分类：{errors}")
        )

    def _run(self, settings: dict[str, object]) -> None:
        args = type("Args", (), settings)()
        args.size = "1024x1024"
        args.max_image_bytes = 50 * 1024 * 1024
        headers = {"Authorization": f"Bearer {settings['api_key']}", "Content-Type": "application/json"}
        with concurrent.futures.ThreadPoolExecutor(max_workers=settings["concurrency"]) as pool:
            futures = [pool.submit(run_one, i, args, headers) for i in range(1, settings["requests"] + 1)]
            for future in concurrent.futures.as_completed(futures):
                if self.stop_event.is_set():
                    for pending in futures:
                        pending.cancel()
                    break
                self.events.put(("result", future.result()))
        self.events.put(("done", settings))

    def _drain_events(self) -> None:
        try:
            while True:
                kind, payload = self.events.get_nowait()
                if kind == "result":
                    result = payload
                    self.results.append(result)
                    self.progress.configure(value=len(self.results))
                    image = f"HTTP {result.image_status}" if result.image_status else "失败"
                    status = str(result.status or "失败")
                    self.table.insert(
                        "", "end",
                        values=(
                            result.index, status, f"{result.api_elapsed_s:.1f}", image,
                            f"{result.image_elapsed_s:.1f}", f"{result.elapsed_s:.1f}",
                            result.image_bytes,
                            self._error_category(result) + (": " + result.error[:180] if result.error else ""),
                        ),
                    )
                    self.summary.configure(text=f"已完成 {len(self.results)} 个请求")
                    self._update_stats()
                elif kind == "done":
                    self._finish(payload)
        except queue.Empty:
            pass
        self.after(100, self._drain_events)

    def _finish(self, settings: dict[str, object]) -> None:
        self.start_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        total = len(self.results)
        http_ok = sum(r.status is not None and 200 <= r.status < 300 for r in self.results)
        image_ok = sum(r.image_status is not None and 200 <= r.image_status < 300 for r in self.results)
        self.summary.configure(text=f"完成：HTTP {http_ok}/{total}，图片 URL 有效 {image_ok}/{total}")
        self._update_stats()
        path = str(settings.get("jsonl") or "")
        if path:
            try:
                Path(path).write_text("\n".join(json.dumps(asdict(r), ensure_ascii=False) for r in self.results) + "\n", encoding="utf-8")
            except OSError as exc:
                messagebox.showerror("保存失败", str(exc))


if __name__ == "__main__":
    LoadTestGui().mainloop()
