// Pure display projection. Credential inventory is NOT free generation capacity.
export const readinessLabels = {
  ready: "凭据就绪", unknown: "有效期未知", expiring: "有效期不足", expired: "AT 已过期",
  quarantined: "鉴权待核验", invalid: "确认异常", disabled: "已禁用",
  refreshing: "正在续期", uncertain: "续期结果待确认",
};
export function availabilityDisplay(value, now=Date.now()/1000) {
  const count = x => Number.isSafeInteger(x) && x >= 0 ? x.toLocaleString() : "--";
  const quota = x => ({
    known:count(x?.known_remaining), accounts:count(x?.known_accounts),
    unknown:count(x?.unknown_accounts), unlimited:count(x?.unlimited_accounts),
    separate:(Number.isSafeInteger(x?.unknown_accounts) && x.unknown_accounts > 0)
      || (Number.isSafeInteger(x?.unlimited_accounts) && x.unlimited_accounts > 0),
  });
  const modes = {normal:"允许同步", reduced:"减速同步", probe:"小批试探", paused:"暂缓同步", not_sampled:"等待采样"};
  const maintenance = value?.maintenance || {};
  const p = value?.maintenance_progress || {};
  const known = p.available === true;
  const progressStale = known && (!Number.isFinite(p.sampled_at) || p.sampled_at <= 0 || now-p.sampled_at > 45);
  const active = known && !progressStale && Number.isSafeInteger(p.active) && p.active >= 0 ? p.active : null;
  const batch = p.batch || (active > 0 && p.last_batch?.active > 0 ? p.last_batch : null);
  const phases = {renewal:"AT 续期", quota:"额度同步"};
  const clock = x => typeof x === "number" && Number.isFinite(x) && x > 0
    ? new Date(x*1000).toLocaleTimeString([], {hour12:false}) : "--";
  const total = batch?.total, completed = batch?.completed;
  const ratio = Number.isSafeInteger(total) && total > 0 && Number.isSafeInteger(completed) && completed >= 0
    ? Math.max(0, Math.min(100, completed / total * 100)) : 0;
  let status = "等待维护采样", tone = "neutral";
  if (p.owner === false) status = "非维护实例";
  else if (progressStale) status = "同步采样已过期";
  else if (active > 0) { status = "后台同步中"; tone = "blue"; }
  else if (known && p.state === "stopped") status = "维护已停止";
  else if (known && maintenance.mode === "paused") { status = "同步已暂缓"; tone = "amber"; }
  else if (known && p.state === "waiting") status = "等待下一轮";
  else if (known) status = "检查待同步账号";
  return {
    states: Object.entries(readinessLabels).map(([key, label]) => ({key, label, value:count(value?.counts?.[key])})),
    generation:count(value?.generation_candidates), edits:count(value?.edit_candidates),
    generationQuota:quota(value?.generation_quota), editQuota:quota(value?.edit_quota),
    quotaNote:typeof value?.quota_note === "string" ? value.quota_note
      : "仅合计就绪账号的已知额度；未知额度和无限额套餐单列。文生图与图生图额度有重叠，不能相加；不是实时空闲并发或成功次数保证。",
    quotaUnknown:count(value?.quota_unknown), uploadLimited:count(value?.upload_limited),
    renewable:count(value?.refresh_candidates), manual:count(value?.needs_credentials),
    mode:maintenance.stale ? "采样已过期" : modes[maintenance.mode] || "等待采样",
    reason:Array.isArray(maintenance.reasons) ? maintenance.reasons.filter(x => typeof x === "string").join("；") : "",
    batch:count(maintenance.batch_size), active:count(maintenance.active_images),
    policy:maintenance.policy === "idle" ? "旧版低并发" : "性能调度",
    note:typeof value?.note === "string" ? value.note : "等待账号可用性统计；未知数据不显示为 0。",
    stale:Number(value?.snapshot_age_seconds) > 30,
    progress:{known, stale:progressStale, active:count(active), status, tone,
      phase:phases[batch?.kind] || "后台维护", hasBatch:!!batch,
      batchTotal:count(total), batchCompleted:count(completed), queued:count(batch?.queued), ratio,
      succeeded:count(p.totals?.succeeded), failed:count(p.totals?.failed), skipped:count(p.totals?.skipped),
      completed:count(p.totals?.completed), instance:typeof p.instance === "string" ? p.instance : "--",
      started:clock(p.started_at), nextCheck:clock(p.next_check_at),
      last:p.last_batch ? `${phases[p.last_batch.kind] || "维护"} · 成功 ${count(p.last_batch.succeeded)} / 失败 ${count(p.last_batch.failed)} / 跳过 ${count(p.last_batch.skipped)}` : "暂无已完成批次",
      lastTime:clock(p.last_batch?.finished_at),
    },
    autoDeleteInvalid:value?.cleanup_policy?.auto_remove_invalid_accounts === true ? "已开启"
      : value?.cleanup_policy?.auto_remove_invalid_accounts === false ? "已关闭" : "未读取",
  };
}
