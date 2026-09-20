// Keep compatibility with schema-5 servers during a rolling deployment.
// A partial/local sample must never look like a complete cluster count.
export function dashboardOperationsDisplay(operations) {
  const count = operations?.active_requests;
  if (!Number.isInteger(count) || count < 0) return {value: "--", caption: "等待并发统计", warning: ""};
  const value = count.toLocaleString();
  const expected = operations.expected_instances, responding = operations.responding_instances;
  const known = ["instance", "cluster"].includes(operations.scope)
    && Number.isInteger(expected) && expected >= 1
    && Number.isInteger(responding) && responding >= 1 && responding <= expected;
  if (!known) return {value, caption: "仅当前实例（旧版统计）", warning: "当前并发仅来自单个实例，请更新全部应用实例后查看集群总数。"};
  if (operations.complete !== true || responding !== expected) {
    return {value: `≥ ${value}`, caption: `已知并发 · ${responding}/${expected} 实例`,
      warning: `并发统计不完整：仅 ${responding}/${expected} 个实例响应，显示已知并发，不代表全局总数。请检查实例健康状态与集群监控配置。`};
  }
  return {value, caption: expected > 1 ? `集群采样 · ${responding}/${expected} 实例` : "当前实例采样", warning: ""};
}
