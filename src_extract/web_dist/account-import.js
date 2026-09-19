/* Standalone importer; shares the main console login. */
(() => {
  "use strict";
  function parseInput(text, mode = "auto") {
    const value = text.trim();
    if (!value) return [];
    const token = value => ({[mode === "rt" || (mode === "auto" && value.startsWith("rt.")) ? "refresh_token" : "access_token"]: value});
    function records(parsed) {
      if (Array.isArray(parsed)) return parsed.flatMap(records);
      if (typeof parsed === "string" && parsed.trim()) return [token(parsed.trim())];
      if (!parsed || typeof parsed !== "object") throw new Error("JSON 中存在无效账号记录");
      if (Array.isArray(parsed.accounts) || Array.isArray(parsed.tokens) || Array.isArray(parsed.refresh_tokens)) {
        return [...records(parsed.accounts || []), ...records(parsed.tokens || []),
          ...(parsed.refresh_tokens || []).map(value => ({refresh_token: String(value).trim()}))];
      }
      const containers = [parsed, parsed.credentials, parsed.credential, parsed.tokens, parsed.auth];
      if (!containers.some(obj => obj && ["access_token", "accessToken", "token", "refresh_token", "refreshToken"].some(key => typeof obj[key] === "string" && obj[key].trim()))) {
        throw new Error("JSON 账号缺少 access_token 或 refresh_token");
      }
      return [parsed];
    }
    if (mode === "json" || /^[\[{]/.test(value)) {
      let parsed;
      try { parsed = JSON.parse(value); } catch (_) { throw new Error("JSON 格式错误，请检查文件或粘贴内容；未按 token 导入"); }
      return records(parsed);
    }
    return value.split(/\r?\n/).map(line => line.trim()).filter(Boolean).map(token);
  }
  if (typeof module !== "undefined" && module.exports) module.exports = {parseInput};
  if (typeof document === "undefined") return;
  const $ = id => document.getElementById(id);
  const states = {queued: "等待入库", saving: "分批入库中", refresh_pending: "等待 RT 兑换", refreshing: "RT 兑换与入库中", sync_pending: "已入库，等待额度同步", syncing: "后台同步额度中", completed: "已完成", failed: "任务中断"};
  const errors = {refresh_token_invalid: "RT 已过期、撤销或无效", refresh_rate_limited: "RT 兑换受到上游限流", refresh_upstream_error: "上游未能完成 RT 兑换", refresh_network_error: "RT 兑换网络异常或超时", import_storage_error: "数据库写入异常", import_sync_error: "额度同步异常", quota_sync_failed: "账号信息或额度同步失败", auth_invalid: "账号认证失效", image_quota_exhausted: "图像额度已用尽", file_upload_throttled: "上传受限", no_available_account: "账号暂不可用"};
  let timer, activeId = "", cursor = 0, logLines = [], request = null, epoch = 0;
  const elapsed = ms => `${(Number(ms || 0) / 1000).toFixed(2)} 秒`;
  function headers() {
    const key = localStorage.getItem("chatgpt2api.adminKey") || "";
    return {"Content-Type": "application/json", ...(key ? {Authorization: `Bearer ${key}`} : {})};
  }
  async function api(path, options = {}) {
    const response = await fetch(path, {...options, headers: headers()});
    if (response.status === 401 || response.status === 403) {
      $("login").hidden = false; $("submit").disabled = true;
      $("auth-state").textContent = "请先登录原后台管理员账号，再返回此页；无需另外设置导入密钥。";
      throw new Error("后台登录已失效或没有管理员权限");
    }
    let data;
    try { data = await response.json(); } catch (_) { throw new Error(`服务暂不可用（HTTP ${response.status}），稍后重试`); }
    if (!response.ok) throw new Error(typeof data.detail?.error === "string" ? data.detail.error : `操作失败（HTTP ${response.status}）`);
    return data;
  }
  function show(job) {
    const issues = (job.refresh_failed || 0) + (job.sync_failed || 0);
    const status = (states[job.status] || job.status) + (job.done && issues ? "（有失败项，请查看日志）" : "");
    const duration = ((job.done ? job.updated_at : Date.now()/1000) - job.created_at) * 1000;
    $("job-info").textContent = `${status} · 总耗时 ${elapsed(duration)} · 已处理 ${job.processed ?? job.saved}/${job.total}\n任务 ${job.id}`;
    $("saved").textContent = `${job.saved} / ${job.total}`; $("added").textContent = `${job.added} / ${job.skipped}`;
    $("refresh").textContent = `${job.refresh_done || 0} / ${job.refresh_failed || 0}`; $("quota").textContent = `${job.synced} / ${job.sync_failed}`;
    $("progress").max = job.total || 1; $("progress").value = job.processed ?? job.saved;
    $("retry").hidden = job.status !== "failed";
    const option = Array.from($("history").options || []).find(option => option.value === job.id);
    if (option) option.textContent = `${new Date(job.created_at*1000).toLocaleString()} · ${status} · ${job.total} 条`;
  }
  function formatEvent(event) {
    const message = {
      submitted: `任务已提交，共 ${event.total} 条；输入重复 ${event.duplicates} 条，RT ${event.refresh_total} 条`,
      phase_started: `开始${{save: "分批入库", refresh: "RT 兑换", sync: "额度同步"}[event.phase] || "处理"}`,
      batch_saved: `第 ${event.start}–${event.end} 条：入库 ${event.saved}，新增 ${event.added}，耗时 ${elapsed(event.duration_ms)}`,
      refresh_done: `第 ${event.item} 条：RT 已兑换并保存凭证，耗时 ${elapsed(event.duration_ms)}`,
      refresh_failed: `第 ${event.item} 条：${errors[event.error_code] || "RT 兑换失败"}，耗时 ${elapsed(event.duration_ms)}`,
      quota_batch: `第 ${event.start}–${event.end} 条：额度同步成功 ${event.synced}，失败 ${event.failed}，耗时 ${elapsed(event.duration_ms)}`,
      quota_failed: `第 ${event.start}–${event.end} 条批次：${errors[event.error_code] || "额度同步失败"}`,
      retry_scheduled: `${errors[event.error_code] || "处理失败"}，已安排重试（第 ${event.failures} 次）`,
      job_failed: `${errors[event.error_code] || "任务中断"}；可从断点重试`,
      retry_requested: "已请求从断点重试", completed: "任务处理完成；失败条目请查看上方日志",
    }[event.code] || event.code;
    return `${new Date(event.time*1000).toLocaleTimeString()}  ${message}`;
  }
  async function poll(id, generation) {
    clearTimeout(timer);
    try {
      const [data, logs] = await Promise.all([api(`/api/account-import-jobs/${id}`), api(`/api/account-import-jobs/${id}/events?after=${cursor}`)]);
      if (generation !== epoch) return;
      show(data.job); cursor = logs.next_cursor;
      logLines.push(...logs.events.map(formatEvent)); logLines = logLines.slice(-500);
      $("logs").textContent = logLines.join("\n") || "等待后台处理…";
      if (!data.job.done || logs.events.length === 100) timer = setTimeout(() => poll(id, generation), logs.events.length === 100 ? 100 : 1500);
    } catch (error) {
      if (generation !== epoch) return;
      $("notice").textContent = error.message;
      if ($("login").hidden) timer = setTimeout(() => poll(id, generation), 4000);
    }
  }
  function select(id) {
    clearTimeout(timer); activeId = id; cursor = 0; logLines = []; epoch++;
    localStorage.setItem("chatgpt2api.importJob", id); $("history").value = id;
    if (id) poll(id, epoch);
  }
  async function history(preferred) {
    const {jobs} = await api("/api/account-import-jobs");
    $("auth-state").textContent = "已使用原后台管理员登录状态";
    $("login").hidden = true; $("submit").disabled = false; $("history").replaceChildren();
    for (const job of jobs) {
      const option = document.createElement("option"); option.value = job.id;
      option.textContent = `${new Date(job.created_at*1000).toLocaleString()} · ${states[job.status] || job.status} · ${job.total} 条`;
      $("history").append(option);
    }
    const id = preferred || activeId || localStorage.getItem("chatgpt2api.importJob");
    select(jobs.some(job => job.id === id) ? id : jobs[0]?.id || "");
  }
  $("file").addEventListener("change", () => { $("files").textContent = `${$("file").files.length} 个文件，合计 ${(Array.from($("file").files).reduce((n, f) => n+f.size, 0)/1024/1024).toFixed(2)} MiB`; });
  $("history").addEventListener("change", () => select($("history").value));
  $("reload").addEventListener("click", () => history().catch(error => { $("notice").textContent = error.message; }));
  $("retry").addEventListener("click", async () => {
    $("retry").disabled = true;
    try { await api(`/api/account-import-jobs/${activeId}/retry`, {method: "POST"}); select(activeId); }
    catch (error) { $("notice").textContent = error.message; }
    finally { $("retry").disabled = false; }
  });
  $("submit").addEventListener("click", async () => {
    $("submit").disabled = true; $("notice").textContent = "正在读取内容…";
    try {
      const files = Array.from($("file").files);
      if (files.reduce((n, f) => n+f.size, new Blob([$("payload").value]).size) > 64*1024*1024) throw new Error("文件与粘贴内容合计超过 64 MiB，请拆分导入");
      const accounts = parseInput($("payload").value, $("mode").value);
      for (let i = 0; i < files.length; i++) {
        try { accounts.push(...parseInput(await files[i].text(), files[i].name.toLowerCase().endsWith(".json") ? "json" : $("mode").value)); }
        catch (error) { throw new Error(`第 ${i+1} 个文件：${error.message}`); }
      }
      if (!accounts.length || accounts.length > 50000) throw new Error("每次需要 1～50000 条账号");
      const content = JSON.stringify({accounts, sync_after_import: $("sync").checked});
      if (!request || request.content !== content) request = {content, key: Array.from(crypto.getRandomValues(new Uint32Array(4)), n => n.toString(16)).join("-")};
      const {job} = await api("/api/account-import-jobs", {method: "POST", body: JSON.stringify({...JSON.parse(content), request_key: request.key})});
      request = null; $("payload").value = ""; $("file").value = ""; $("files").textContent = "文件已提交";
      $("notice").textContent = "已提交后台处理，可以继续导入其他文件或离开页面。";
      select(job.id); await history(job.id);
    } catch (error) { $("notice").textContent = error.message; }
    finally { $("submit").disabled = !$("login").hidden; }
  });
  history().catch(error => { $("notice").textContent = error.message; });
})();
