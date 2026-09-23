import {d as defineComponent, b as h, r as ref, s as onMounted, x as onUnmounted, m as api, O as Icon}
  from "./index-BhEm-7EJ.js?v=20260924-account-readiness-v13";
import {availabilityDisplay} from "./accountAvailabilityRuntime-v1.js?v=20260924-readiness-v1";

export default defineComponent({
  name:"AccountAvailability",
  setup() {
    const snapshot = ref(null), busy = ref(false), error = ref("");
    let disposed = false, timer;
    async function refresh() {
      if (disposed || busy.value || document.hidden) return;
      busy.value = true;
      try {
        const data = await api.get("/api/accounts/availability");
        if (!disposed) { snapshot.value = data; error.value = ""; }
      } catch (_) {
        if (!disposed) error.value = "可用性统计暂不可用，保留上次采样；不影响导入和账号操作。";
      } finally { if (!disposed) busy.value = false; }
    }
    onMounted(() => { refresh(); timer = window.setInterval(refresh, 15000); });
    onUnmounted(() => { disposed = true; window.clearInterval(timer); });
    return () => {
      const view = availabilityDisplay(snapshot.value), maintenance = snapshot.value?.maintenance;
      const signals = maintenance?.signals || {};
      const ms = x => typeof x === "number" && Number.isFinite(x) ? `${Math.round(x)}ms` : "--";
      return h("section", {"aria-label":"账号可用性", class:"rounded-xl border border-border bg-card p-4", style:"min-width:0;margin:12px 0"}, [
        h("div", {class:"flex flex-wrap items-center justify-between gap-2"}, [
          h("div", {}, [h("h2", {class:"font-semibold text-foreground"}, "账号可用性"),
            h("p", {class:"text-xs text-muted-foreground"}, "全池凭据预评估 · 不等于实时空闲槽位")]),
          h("button", {type:"button", "aria-label":"刷新账号可用性", title:"刷新账号可用性", disabled:busy.value,
            class:"rounded-lg border border-border p-2", onClick:refresh}, [h(Icon, {icon:"lucide:refresh-cw", width:16})]),
        ]),
        h("div", {style:"display:grid;grid-template-columns:repeat(auto-fit,minmax(100px,1fr));gap:8px;margin-top:12px"},
          view.states.map(item => h("div", {key:item.key, class:"rounded-lg bg-muted/40 p-3", "data-readiness-state":item.key}, [
            h("div", {class:"text-xs text-muted-foreground"}, item.label),
            h("div", {class:"mt-1 text-lg font-semibold tabular-nums", style:item.key === "ready" ? "color:#059669" : ""}, item.value),
          ]))),
        h("p", {class:"mt-3 text-sm"}, `已知有效期且满足当前额度规则：文生图候选 ${view.generation} · 图生图候选 ${view.edits}`),
        h("p", {class:"mt-1 text-xs text-muted-foreground"}, `有 RT 可尝试恢复 ${view.renewable} · 需补充凭据 ${view.manual} · 额度未知 ${view.quotaUnknown} · 上传冷却 ${view.uploadLimited}（补充指标不互斥）`),
        h("div", {class:"mt-3 rounded-lg border border-border p-3", "aria-label":"后台同步策略"}, [
          h("p", {class:"text-sm font-medium"}, `后台账号维护：${view.mode} · 每批 ${view.batch} 个 · ${view.policy}`),
          h("p", {class:"mt-1 text-xs text-muted-foreground"}, view.reason),
          h("p", {class:"mt-1 text-xs text-muted-foreground"}, `最近决策使用的最慢实例指标：数据库 P95 ${ms(signals.database_ms)} · 写锁等待 P95 ${ms(signals.writer_wait_ms)} · 账号接口 P95 ${ms(signals.upstream_ms)} · 活跃生图 ${view.active}（仅参考）`),
          maintenance?.sampled_at ? h("p", {class:"mt-1 text-xs text-muted-foreground"}, `调度采样：${new Date(maintenance.sampled_at * 1000).toLocaleTimeString()} · 耗时样本窗口 60 秒`) : null,
        ]),
        h("p", {class:"mt-2 text-xs text-muted-foreground"}, view.note),
        snapshot.value?.sampled_at ? h("p", {class:"mt-1 text-xs text-muted-foreground"}, `采样时间：${new Date(snapshot.value.sampled_at).toLocaleTimeString()} · ${view.stale ? "账号快照更新延迟" : "约每 15 秒刷新"}`) : null,
        error.value ? h("p", {role:"status", class:"mt-2 text-xs text-amber-600"}, error.value) : null,
      ]);
    };
  },
});
