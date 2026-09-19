// Shared by the account-management import panel. No credentials are persisted
// in browser storage; only the selected durable job ID is remembered.
export const localImportModes = ["access_token", "refresh_token", "session_json", "cpa_json", "sub2api_json"];
export const importStates = {queued: "等待入库", saving: "分批入库中", refresh_pending: "等待 RT 兑换", refreshing: "RT 兑换与入库中", sync_pending: "已入库，等待额度同步", syncing: "后台同步额度中", completed: "已完成", failed: "任务中断"};
const errors = {refresh_token_invalid: "RT 已过期、撤销或无效", refresh_rate_limited: "RT 兑换受到上游限流", refresh_upstream_error: "上游未能完成 RT 兑换", refresh_network_error: "RT 兑换网络异常或超时", import_storage_error: "数据库写入异常", import_sync_error: "额度同步异常", quota_sync_failed: "账号信息或额度同步失败", auth_invalid: "账号认证失效", image_quota_exhausted: "图像额度已用尽", file_upload_throttled: "上传受限", no_available_account: "账号暂不可用"};
export const elapsed = ms => `${(Math.max(0, Number(ms) || 0)/1000).toFixed(2)} 秒`;
export function jobLabel(job) {
  return (importStates[job.status] || job.status) + (job.done && (job.refresh_failed || job.sync_failed) ? "（有失败项）" : "");
}

export function parseInput(text, mode = "auto") {
  const value = text.trim();
  if (!value) return [];
  const token = value => ({[mode === "rt" || mode === "refresh_token" || value.startsWith("rt.") ? "refresh_token" : "access_token"]: value});
  function records(parsed) {
    if (Array.isArray(parsed)) return parsed.flatMap(records);
    if (typeof parsed === "string" && parsed.trim()) return [token(parsed.trim())];
    if (!parsed || typeof parsed !== "object") throw new Error("JSON 中存在无效账号记录");
    const containers = [parsed.credentials, parsed.credential, parsed.tokens, parsed.auth, parsed];
    if (containers.some(obj => obj && ["access_token", "accessToken", "token", "refresh_token", "refreshToken"].some(key => typeof obj[key] === "string" && obj[key].trim()))) return [parsed];
    const groups = ["accounts", "items", "results", "tokens"].filter(key => Array.isArray(parsed[key]));
    const result = groups.flatMap(key => records(parsed[key]));
    if (Array.isArray(parsed.refresh_tokens)) {
      for (const value of parsed.refresh_tokens) {
        if (typeof value !== "string" || !value.trim()) throw new Error("JSON 中存在无效 RT");
        result.push({refresh_token: value.trim()});
      }
    }
    if (parsed.data && typeof parsed.data === "object") result.push(...records(parsed.data));
    if (result.length) return result;
    throw new Error("JSON 账号缺少 access_token 或 refresh_token");
  }
  if (["json", "session_json", "cpa_json", "sub2api_json"].includes(mode) || /^[\[{]/.test(value)) {
    let parsed;
    try { parsed = JSON.parse(value); } catch (_) { throw new Error("JSON 格式错误，请检查文件或粘贴内容；未按 token 导入"); }
    return records(parsed);
  }
  return value.split(/\r?\n/).map(line => line.trim()).filter(line => line && !line.startsWith("#")).map(token);
}

export async function readImportInputs({text = "", files = [], mode = "access_token"}) {
  const selected = Array.from(files);
  if (selected.reduce((n, f) => n+f.size, new Blob([text]).size) > 64*1024*1024) throw new Error("文件与粘贴内容合计超过 64 MiB，请拆分导入");
  const accounts = parseInput(text, mode);
  for (let i = 0; i < selected.length; i++) {
    try {
      const values = parseInput(await selected[i].text(), selected[i].name.toLowerCase().endsWith(".json") ? "json" : mode);
      for (const item of values) accounts.push(item);
    } catch (error) { throw new Error(`第 ${i+1} 个文件：${error.message}`); }
    if (accounts.length > 50000) throw new Error("每次最多导入 50000 条账号");
  }
  if (!accounts.length || accounts.length > 50000) throw new Error("每次需要 1～50000 条账号");
  const source = ["cpa_json", "sub2api_json"].includes(mode) ? "codex" : "web";
  return accounts.map(item => ({...item, source_type: item.source_type || source}));
}

export function formatEvent(event) {
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
    retry_requested: "已请求从断点重试", completed: "任务处理完成",
  }[event.code] || "任务进度已更新";
  return `${new Date(event.time*1000).toLocaleTimeString()}  ${message}`;
}

export function createImportController({api, onUpdate, onAccountsChanged = () => {}, storage,
  schedule = setTimeout, cancel = clearTimeout, requestKey = () => Array.from(crypto.getRandomValues(new Uint32Array(4)), n => n.toString(16)).join("-")}) {
  const key = "chatgpt2api.importJob";
  let state = {jobs: [], job: null, events: [], selected: "", busy: false, notice: "", connection: ""};
  let epoch = 0, active = true, timer, cursor = 0, request = null, historyEpoch = 0;
  let lastSaved = 0, lastRefresh = 0, refreshedTerminal = "";
  const update = changes => { state = {...state, ...changes}; if (active) onUpdate(state); };
  const remember = id => { try { storage?.setItem(key, id); } catch (_) {} };
  const readRemembered = () => { try { return storage?.getItem(key); } catch (_) { return null; } };
  const message = error => typeof error?.response?.data?.detail?.error === "string" ? error.response.data.detail.error : error?.message || "连接失败，请重试";
  const refreshAccounts = job => {
    const now = Date.now(), terminal = job.done && refreshedTerminal !== job.id;
    if (terminal || job.saved > lastSaved && now-lastRefresh >= 5000) {
      lastSaved = job.saved; lastRefresh = now;
      if (terminal) refreshedTerminal = job.id;
      Promise.resolve().then(onAccountsChanged).catch(() => {});
    }
  };
  async function poll(id, generation) {
    try {
      const [data, logs] = await Promise.all([api.get(`/api/account-import-jobs/${id}`), api.get(`/api/account-import-jobs/${id}/events?after=${cursor}`)]);
      if (!active || epoch !== generation) return;
      cursor = logs.next_cursor;
      update({job: data.job, jobs: state.jobs.map(job => job.id === id ? data.job : job),
        events: [...state.events, ...logs.events].slice(-500), connection: ""});
      refreshAccounts(data.job);
      if (!data.job.done || logs.events.length === 100) timer = schedule(() => poll(id, generation), logs.events.length === 100 ? 100 : 1500);
    } catch (error) {
      if (!active || epoch !== generation) return;
      update({connection: message(error)});
      if (![401,403,404].includes(error?.response?.status)) timer = schedule(() => poll(id, generation), 4000);
    }
  }
  function select(id) {
    cancel(timer); epoch++; cursor = 0; lastSaved = 0; lastRefresh = 0; refreshedTerminal = "";
    remember(id);
    update({selected: id, job: state.jobs.find(job => job.id === id) || null, events: [], connection: ""});
    if (id && active) return poll(id, epoch);
  }
  async function history(preferred) {
    const generation = ++historyEpoch;
    try {
      const {jobs} = await api.get("/api/account-import-jobs");
      if (!active || historyEpoch !== generation) return;
      const selected = preferred || state.selected || readRemembered();
      update({jobs, connection: ""});
      await select(jobs.some(job => job.id === selected) ? selected : jobs[0]?.id || "");
    } catch (error) { if (active && historyEpoch === generation) update({connection: message(error)}); }
  }
  async function submit({accounts, syncAfterImport = true, targetGroupId = null}) {
    if (state.busy) return false;
    update({busy: true, notice: "正在提交导入任务…"});
    try {
      const content = JSON.stringify({accounts, sync_after_import: syncAfterImport, target_group_id: targetGroupId});
      if (!request || request.content !== content) request = {content, key: requestKey()};
      const {job} = await api.post("/api/account-import-jobs", {...JSON.parse(content), request_key: request.key});
      request = null; remember(job.id);
      if (!active) return true;
      historyEpoch++; // an older history fetch must not replace the new selection
      update({jobs: [job, ...state.jobs.filter(item => item.id !== job.id)], notice: "已提交后台处理，可关闭窗口或继续导入。进度和日志可再次打开此窗口查看。"});
      select(job.id);
      return true;
    } catch (error) { if (active) update({notice: message(error)}); return false; }
    finally { update({busy: false}); }
  }
  async function retry() {
    if (state.busy || !state.selected) return;
    update({busy: true});
    try {
      const {job} = await api.post(`/api/account-import-jobs/${state.selected}/retry`);
      if (active) { update({job, notice: "已从断点恢复任务"}); select(job.id); }
    } catch (error) { if (active) update({notice: message(error)}); }
    finally { update({busy: false}); }
  }
  function stop() { active = false; epoch++; historyEpoch++; cancel(timer); }
  function resume() { if (active) return; active = true; update({}); history(); }
  update({});
  return {history, select, submit, retry, stop, resume};
}
