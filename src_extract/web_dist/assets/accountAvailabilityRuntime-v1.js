// Pure display projection. Credential inventory is NOT free generation capacity.
export const readinessLabels = {
  ready: "凭据就绪", unknown: "有效期未知", expiring: "有效期不足", expired: "AT 已过期",
  quarantined: "鉴权待核验", invalid: "确认异常", disabled: "已禁用",
  refreshing: "正在续期", uncertain: "续期结果待确认",
};
export function availabilityDisplay(value) {
  const count = x => Number.isSafeInteger(x) && x >= 0 ? x.toLocaleString() : "--";
  const quota = x => ({
    known:count(x?.known_remaining), accounts:count(x?.known_accounts),
    unknown:count(x?.unknown_accounts), unlimited:count(x?.unlimited_accounts),
    separate:(Number.isSafeInteger(x?.unknown_accounts) && x.unknown_accounts > 0)
      || (Number.isSafeInteger(x?.unlimited_accounts) && x.unlimited_accounts > 0),
  });
  const modes = {normal:"正常同步", reduced:"减速同步", probe:"小批试探", paused:"暂缓同步", not_sampled:"等待采样"};
  const maintenance = value?.maintenance || {};
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
  };
}
