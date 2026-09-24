import {d as defineComponent, b as h, r as ref, s as onMounted, x as onUnmounted, m as api, O as Icon}
  from "./index-BhEm-7EJ.js?v=20260924-ready-quota-v15";
import {availabilityDisplay} from "./accountAvailabilityRuntime-v1.js?v=20260924-readiness-v3";

export default defineComponent({
  name:"AccountAvailability",
  setup() {
    const snapshot = ref(null), busy = ref(false), error = ref(""), dialog = ref(null);
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
    onUnmounted(() => { disposed = true; window.clearInterval(timer); dialog.value?.close(); });
    const iconButton = (name, icon, click, disabled=false) => h("button", {type:"button", "aria-label":name,
      title:name, disabled, onClick:click, class:"rounded-lg p-2 text-muted-foreground hover:bg-muted"}, [h(Icon,{icon,width:16})]);
    const number = n => Number.isSafeInteger(n) && n >= 0 ? n.toLocaleString() : "--";
    return () => {
      const view = availabilityDisplay(snapshot.value), maintenance = snapshot.value?.maintenance;
      const signals = maintenance?.signals || {}, prewarm = snapshot.value?.prewarm || {};
      const ms = x => typeof x === "number" && Number.isFinite(x) ? `${Math.round(x)}ms` : "--";
      const tile = (key, label, value, quota) => h("div", {key, class:"rounded-lg bg-muted/40 p-3", "data-availability-metric":key, style:"min-width:0;overflow-wrap:anywhere"}, [
        h("div", {class:"text-xs text-muted-foreground"}, label),
        h("div", {class:"mt-1 text-xl font-semibold tabular-nums text-foreground"}, value),
        quota ? h("div", {class:"mt-1 text-xs text-muted-foreground tabular-nums", "data-ready-quota":key,
          title:`${view.quotaNote} 额度未知 ${quota.unknown} 个；无限额套餐 ${quota.unlimited} 个。`},
          `就绪额度 ${quota.known}${quota.separate ? " *" : ""}`) : null,
      ]);
      const quotaDetail = (key, label, quota) => h("div", {"data-ready-quota-detail":key, class:"mt-2"}, [
        h("p",{},`${label}：已知额度 ${quota.known}（${quota.accounts} 个账号）`),
        h("p",{class:"mt-1 text-muted-foreground"},`未计入数字：额度未知 ${quota.unknown} 个 · 无限额套餐 ${quota.unlimited} 个`),
      ]);
      return h("section", {"aria-label":"账号可用性", class:"rounded-xl border border-border bg-card p-4", style:"min-width:0;margin:12px 0"}, [
        h("div", {class:"flex flex-wrap items-center justify-between gap-2"}, [
          h("h2", {class:"text-sm font-medium text-foreground"}, "账号可用性"),
          h("div", {class:"flex items-center gap-2 text-xs text-muted-foreground"}, [
            h("span", {title:view.reason}, view.mode),
            h("button", {type:"button", class:"rounded-lg px-2 py-1 hover:bg-muted", onClick:()=>dialog.value?.showModal()}, "详情"),
            iconButton("刷新账号可用性","lucide:refresh-cw",refresh,busy.value),
          ]),
        ]),
        h("div", {style:"display:grid;grid-template-columns:repeat(auto-fit,minmax(min(130px,100%),1fr));gap:8px;margin-top:8px"}, [
          tile("generation","文生图候选",view.generation,view.generationQuota), tile("edits","图生图候选",view.edits,view.editQuota),
          tile("recovery","可尝试恢复",view.renewable), tile("manual","待补凭据",view.manual),
        ]),
        error.value ? h("p", {role:"status", class:"mt-2 text-xs text-amber-600"}, error.value) : null,
        h("dialog", {ref:dialog, "aria-label":"账号可用性详情", class:"rounded-xl border border-border bg-card text-foreground p-5",
          style:"width:min(640px,calc(100vw - 32px));max-height:80vh;overflow:auto;margin:auto", onClick:e=>{if(e.target===dialog.value){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)dialog.value.close();}}}, [
          h("div", {class:"flex items-center justify-between"}, [h("h3",{class:"text-sm font-medium"},"账号可用性详情"),
            iconButton("关闭可用性详情","lucide:x",()=>dialog.value?.close())]),
          h("p", {class:"mt-2 text-xs text-muted-foreground"}, snapshot.value?.policy === "strict" ? "严格准入 · 未知或过期 AT 不参与生图" : "兼容准入 · 凭据就绪预评估"),
          h("div", {style:"display:grid;grid-template-columns:repeat(auto-fit,minmax(140px,1fr));gap:12px;margin-top:16px"}, view.states.map(item=>
            h("div", {key:item.key,"data-readiness-state":item.key,class:"text-sm flex justify-between gap-2"},[
              h("span",{class:"text-muted-foreground"},item.label),h("span",{class:"tabular-nums"},item.value)]))),
          h("div", {class:"mt-4 border-t border-border pt-3 text-xs", "aria-label":"就绪额度详情"},[
            h("h4",{class:"font-medium"},"就绪额度"),
            quotaDetail("generation","文生图",view.generationQuota), quotaDetail("edits","图生图",view.editQuota),
            h("p",{class:"mt-2 text-muted-foreground"},view.quotaNote),
          ]),
          h("div", {class:"mt-4 border-t border-border pt-3 text-xs text-muted-foreground", "aria-label":"后台同步策略"},[
            h("p",{},`后台维护：${view.mode} · 每批 ${view.batch} 个 · ${view.policy}`),
            h("p",{class:"mt-2"},view.reason),
            h("p",{class:"mt-2"},`数据库 P95 ${ms(signals.database_ms)} · 写锁 P95 ${ms(signals.writer_wait_ms)} · 账号接口 P95 ${ms(signals.upstream_ms)}`),
            h("p",{class:"mt-2"},`活跃生图 ${view.active}（参考值） · 额度未知 ${view.quotaUnknown} · 上传冷却 ${view.uploadLimited}`),
            h("p",{class:"mt-2"},`本实例预热：${number(prewarm.ready)} / ${number(prewarm.target)} · 命中 ${number(prewarm.hit)} · 未命中 ${number(prewarm.miss)}`),
            h("p",{class:"mt-2"},view.note),
            snapshot.value?.sampled_at ? h("p",{class:"mt-2"},`采样：${new Date(snapshot.value.sampled_at).toLocaleTimeString()} · ${view.stale ? "快照更新延迟" : "约每 15 秒刷新"}`) : null,
          ]),
        ]),
      ]);
    };
  },
});
