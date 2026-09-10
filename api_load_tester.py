#!/usr/bin/env python3
"""
API 压测工具 - 带图形界面
支持自定义 API 端点、请求头、请求体
实时显示成功率、响应时间、错误统计
"""

import tkinter as tk
from tkinter import ttk, scrolledtext, messagebox
import threading
import time
import json
import requests
from datetime import datetime
from collections import defaultdict
import queue

class APILoadTester:
    def __init__(self, root):
        self.root = root
        self.root.title("API 压测工具")
        self.root.geometry("1000x700")

        # 测试状态
        self.is_running = False
        self.results = []
        self.error_count = defaultdict(int)
        self.result_queue = queue.Queue()

        # 创建界面
        self.create_widgets()

        # 启动结果更新线程
        self.update_results()

    def create_widgets(self):
        # ===== 配置区域 =====
        config_frame = ttk.LabelFrame(self.root, text="测试配置", padding=10)
        config_frame.pack(fill=tk.X, padx=10, pady=5)

        # API URL
        ttk.Label(config_frame, text="API URL:").grid(row=0, column=0, sticky=tk.W, pady=2)
        self.url_entry = ttk.Entry(config_frame, width=60)
        self.url_entry.grid(row=0, column=1, columnspan=2, sticky=tk.EW, pady=2)
        self.url_entry.insert(0, "http://localhost:31000/v1/chat/completions")

        # HTTP 方法
        ttk.Label(config_frame, text="方法:").grid(row=1, column=0, sticky=tk.W, pady=2)
        self.method_var = tk.StringVar(value="POST")
        method_combo = ttk.Combobox(config_frame, textvariable=self.method_var,
                                     values=["GET", "POST", "PUT", "DELETE"], width=10)
        method_combo.grid(row=1, column=1, sticky=tk.W, pady=2)

        # API Key
        ttk.Label(config_frame, text="API Key:").grid(row=2, column=0, sticky=tk.W, pady=2)
        self.api_key_entry = ttk.Entry(config_frame, width=60, show="*")
        self.api_key_entry.grid(row=2, column=1, columnspan=2, sticky=tk.EW, pady=2)

        # 显示/隐藏 API Key
        self.show_key_var = tk.BooleanVar()
        ttk.Checkbutton(config_frame, text="显示", variable=self.show_key_var,
                       command=self.toggle_api_key).grid(row=2, column=3, pady=2)

        # 请求头
        ttk.Label(config_frame, text="请求头 (JSON):").grid(row=3, column=0, sticky=tk.NW, pady=2)
        self.headers_text = tk.Text(config_frame, height=3, width=60)
        self.headers_text.grid(row=3, column=1, columnspan=2, sticky=tk.EW, pady=2)
        self.headers_text.insert("1.0", '{"Content-Type": "application/json"}')

        # 请求体
        ttk.Label(config_frame, text="请求体 (JSON):").grid(row=4, column=0, sticky=tk.NW, pady=2)
        self.body_text = tk.Text(config_frame, height=5, width=60)
        self.body_text.grid(row=4, column=1, columnspan=2, sticky=tk.EW, pady=2)
        default_body = '''{
  "model": "gpt-4",
  "messages": [{"role": "user", "content": "Hello"}],
  "stream": false
}'''
        self.body_text.insert("1.0", default_body)

        # 并发配置
        params_frame = ttk.Frame(config_frame)
        params_frame.grid(row=5, column=0, columnspan=3, sticky=tk.EW, pady=5)

        ttk.Label(params_frame, text="并发数:").pack(side=tk.LEFT, padx=5)
        self.concurrency_var = tk.IntVar(value=10)
        ttk.Spinbox(params_frame, from_=1, to=100, textvariable=self.concurrency_var,
                   width=10).pack(side=tk.LEFT, padx=5)

        ttk.Label(params_frame, text="请求总数:").pack(side=tk.LEFT, padx=5)
        self.total_requests_var = tk.IntVar(value=100)
        ttk.Spinbox(params_frame, from_=1, to=10000, textvariable=self.total_requests_var,
                   width=10).pack(side=tk.LEFT, padx=5)

        ttk.Label(params_frame, text="超时(秒):").pack(side=tk.LEFT, padx=5)
        self.timeout_var = tk.IntVar(value=30)
        ttk.Spinbox(params_frame, from_=1, to=300, textvariable=self.timeout_var,
                   width=10).pack(side=tk.LEFT, padx=5)

        # 控制按钮
        button_frame = ttk.Frame(config_frame)
        button_frame.grid(row=6, column=0, columnspan=3, pady=10)

        self.start_button = ttk.Button(button_frame, text="开始测试",
                                       command=self.start_test, width=15)
        self.start_button.pack(side=tk.LEFT, padx=5)

        self.stop_button = ttk.Button(button_frame, text="停止测试",
                                      command=self.stop_test, state=tk.DISABLED, width=15)
        self.stop_button.pack(side=tk.LEFT, padx=5)

        ttk.Button(button_frame, text="清空结果",
                  command=self.clear_results, width=15).pack(side=tk.LEFT, padx=5)

        config_frame.columnconfigure(1, weight=1)

        # ===== 统计区域 =====
        stats_frame = ttk.LabelFrame(self.root, text="实时统计", padding=10)
        stats_frame.pack(fill=tk.X, padx=10, pady=5)

        self.stats_text = tk.StringVar(value="等待测试...")
        ttk.Label(stats_frame, textvariable=self.stats_text, font=("Arial", 10)).pack()

        # ===== 结果区域 =====
        results_frame = ttk.LabelFrame(self.root, text="测试结果", padding=10)
        results_frame.pack(fill=tk.BOTH, expand=True, padx=10, pady=5)

        # 创建 Notebook (标签页)
        notebook = ttk.Notebook(results_frame)
        notebook.pack(fill=tk.BOTH, expand=True)

        # 实时日志标签页
        log_frame = ttk.Frame(notebook)
        notebook.add(log_frame, text="实时日志")

        self.log_text = scrolledtext.ScrolledText(log_frame, wrap=tk.WORD, height=15)
        self.log_text.pack(fill=tk.BOTH, expand=True)

        # 错误统计标签页
        error_frame = ttk.Frame(notebook)
        notebook.add(error_frame, text="错误统计")

        self.error_text = scrolledtext.ScrolledText(error_frame, wrap=tk.WORD, height=15)
        self.error_text.pack(fill=tk.BOTH, expand=True)

        # 详细结果标签页
        detail_frame = ttk.Frame(notebook)
        notebook.add(detail_frame, text="详细结果")

        self.detail_text = scrolledtext.ScrolledText(detail_frame, wrap=tk.WORD, height=15)
        self.detail_text.pack(fill=tk.BOTH, expand=True)

    def toggle_api_key(self):
        if self.show_key_var.get():
            self.api_key_entry.config(show="")
        else:
            self.api_key_entry.config(show="*")

    def log(self, message, level="INFO"):
        timestamp = datetime.now().strftime("%H:%M:%S")
        log_message = f"[{timestamp}] [{level}] {message}\n"
        self.result_queue.put(("log", log_message))

    def update_results(self):
        """定期更新界面"""
        try:
            while True:
                msg_type, data = self.result_queue.get_nowait()

                if msg_type == "log":
                    self.log_text.insert(tk.END, data)
                    self.log_text.see(tk.END)
                elif msg_type == "stats":
                    self.stats_text.set(data)
                elif msg_type == "error":
                    self.error_text.delete("1.0", tk.END)
                    self.error_text.insert("1.0", data)
                elif msg_type == "detail":
                    self.detail_text.insert(tk.END, data)
                    self.detail_text.see(tk.END)
        except queue.Empty:
            pass

        self.root.after(100, self.update_results)

    def validate_config(self):
        """验证配置"""
        url = self.url_entry.get().strip()
        if not url:
            messagebox.showerror("错误", "请输入 API URL")
            return False

        if not url.startswith(("http://", "https://")):
            messagebox.showerror("错误", "URL 必须以 http:// 或 https:// 开头")
            return False

        # 验证 JSON
        try:
            headers = self.headers_text.get("1.0", tk.END).strip()
            if headers:
                json.loads(headers)
        except json.JSONDecodeError as e:
            messagebox.showerror("错误", f"请求头 JSON 格式错误: {e}")
            return False

        if self.method_var.get() in ["POST", "PUT"]:
            try:
                body = self.body_text.get("1.0", tk.END).strip()
                if body:
                    json.loads(body)
            except json.JSONDecodeError as e:
                messagebox.showerror("错误", f"请求体 JSON 格式错误: {e}")
                return False

        return True

    def start_test(self):
        """开始测试"""
        if not self.validate_config():
            return

        self.is_running = True
        self.results = []
        self.error_count.clear()

        self.start_button.config(state=tk.DISABLED)
        self.stop_button.config(state=tk.NORMAL)

        self.log("开始压测...")

        # 启动测试线程
        thread = threading.Thread(target=self.run_test, daemon=True)
        thread.start()

    def stop_test(self):
        """停止测试"""
        self.is_running = False
        self.log("正在停止测试...", "WARN")
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)

    def clear_results(self):
        """清空结果"""
        self.log_text.delete("1.0", tk.END)
        self.error_text.delete("1.0", tk.END)
        self.detail_text.delete("1.0", tk.END)
        self.results = []
        self.error_count.clear()
        self.stats_text.set("等待测试...")
        self.log("已清空结果")

    def make_request(self, request_id):
        """执行单个请求"""
        url = self.url_entry.get().strip()
        method = self.method_var.get()
        api_key = self.api_key_entry.get().strip()
        timeout = self.timeout_var.get()

        # 构建请求头
        headers = {}
        headers_json = self.headers_text.get("1.0", tk.END).strip()
        if headers_json:
            headers = json.loads(headers_json)

        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"

        # 构建请求体
        body = None
        if method in ["POST", "PUT"]:
            body_json = self.body_text.get("1.0", tk.END).strip()
            if body_json:
                body = json.loads(body_json)

        # 发送请求
        start_time = time.time()
        try:
            if method == "GET":
                response = requests.get(url, headers=headers, timeout=timeout)
            elif method == "POST":
                response = requests.post(url, headers=headers, json=body, timeout=timeout)
            elif method == "PUT":
                response = requests.put(url, headers=headers, json=body, timeout=timeout)
            elif method == "DELETE":
                response = requests.delete(url, headers=headers, timeout=timeout)

            elapsed = time.time() - start_time

            result = {
                "id": request_id,
                "status_code": response.status_code,
                "elapsed": elapsed,
                "success": 200 <= response.status_code < 300,
                "error": None,
                "response_size": len(response.content)
            }

            if not result["success"]:
                result["error"] = f"HTTP {response.status_code}"
                try:
                    error_body = response.json()
                    if "error" in error_body:
                        result["error"] += f": {error_body['error']}"
                except:
                    pass

            return result

        except requests.exceptions.Timeout:
            elapsed = time.time() - start_time
            return {
                "id": request_id,
                "status_code": 0,
                "elapsed": elapsed,
                "success": False,
                "error": "请求超时"
            }
        except Exception as e:
            elapsed = time.time() - start_time
            return {
                "id": request_id,
                "status_code": 0,
                "elapsed": elapsed,
                "success": False,
                "error": str(e)
            }

    def run_test(self):
        """执行压测"""
        concurrency = self.concurrency_var.get()
        total_requests = self.total_requests_var.get()

        from concurrent.futures import ThreadPoolExecutor, as_completed

        start_time = time.time()
        completed = 0

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {executor.submit(self.make_request, i): i
                      for i in range(total_requests)}

            for future in as_completed(futures):
                if not self.is_running:
                    executor.shutdown(wait=False)
                    break

                result = future.result()
                self.results.append(result)
                completed += 1

                # 更新统计
                if result["error"]:
                    self.error_count[result["error"]] += 1

                # 计算统计信息
                success_count = sum(1 for r in self.results if r["success"])
                error_count = len(self.results) - success_count
                avg_time = sum(r["elapsed"] for r in self.results) / len(self.results)

                success_rate = (success_count / len(self.results)) * 100

                stats = (f"进度: {completed}/{total_requests} | "
                        f"成功: {success_count} ({success_rate:.1f}%) | "
                        f"失败: {error_count} | "
                        f"平均响应: {avg_time:.3f}s")

                self.result_queue.put(("stats", stats))

                # 日志
                status = "✓" if result["success"] else "✗"
                log_msg = f"{status} 请求 #{result['id']+1}: {result['status_code']} - {result['elapsed']:.3f}s"
                if result["error"]:
                    log_msg += f" - {result['error']}"
                self.log(log_msg, "INFO" if result["success"] else "ERROR")

        # 测试完成
        total_time = time.time() - start_time

        self.log(f"\n测试完成! 总耗时: {total_time:.2f}s", "INFO")
        self.log(f"总请求数: {len(self.results)}", "INFO")
        self.log(f"成功: {success_count}, 失败: {error_count}", "INFO")
        self.log(f"成功率: {success_rate:.2f}%", "INFO")
        self.log(f"平均响应时间: {avg_time:.3f}s", "INFO")

        if self.results:
            min_time = min(r["elapsed"] for r in self.results)
            max_time = max(r["elapsed"] for r in self.results)
            self.log(f"最快: {min_time:.3f}s, 最慢: {max_time:.3f}s", "INFO")

        # 更新错误统计
        if self.error_count:
            error_summary = "错误统计:\n\n"
            for error, count in sorted(self.error_count.items(), key=lambda x: -x[1]):
                error_summary += f"{error}: {count} 次\n"
            self.result_queue.put(("error", error_summary))

        # 详细结果
        detail_summary = "详细结果 (前 50 条):\n\n"
        for result in self.results[:50]:
            detail_summary += f"请求 #{result['id']+1}:\n"
            detail_summary += f"  状态码: {result['status_code']}\n"
            detail_summary += f"  响应时间: {result['elapsed']:.3f}s\n"
            detail_summary += f"  成功: {'是' if result['success'] else '否'}\n"
            if result['error']:
                detail_summary += f"  错误: {result['error']}\n"
            detail_summary += "\n"

        self.result_queue.put(("detail", detail_summary))

        self.is_running = False
        self.start_button.config(state=tk.NORMAL)
        self.stop_button.config(state=tk.DISABLED)


if __name__ == "__main__":
    root = tk.Tk()
    app = APILoadTester(root)
    root.mainloop()
