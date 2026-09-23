// Pure display projection. Credential inventory is NOT free generation capacity.
export const readinessLabels = {
  ready: "凭据就绪", unknown: "有效期未知", expiring: "有效期不足", expired: "AT 已过期",
  quarantined: "鉴权待核验", invalid: "确认异常", disabled: "已禁用",
};
export function availabilityDisplay(value) {
  const count = x => Number.isSafeInteger(x) && x >= 0 ? x.toLocaleString() : "--";
  const modes = {normal:"正常同步", reduced:"减速同步", probe:"小批试探", paused:"暂缓同步", not_sampled:"等待采样"};
  const maintenance = value?.maintenance || {};
  return {
    states: Object.entries(readinessLabels).map(([key, label]) => ({key, label, value:count(value?.counts?.[key])})),
    generation:count(value?.generation_candidates), edits:count(value?.edit_candidates),
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
