import {d as defineComponent, b as h, r as ref, s as onMounted, x as onUnmounted, m as api}
  from "./index-BhEm-7EJ.js?v=20260925-owner-v17";
import {availabilityDisplay} from "./accountAvailabilityRuntime-v1.js?v=20260925-owner-v2";

let sequence = 0;
const paths = {
  shield:["M12 22s8-4 8-11V5l-8-3-8 3v6c0 7 8 11 8 11", "m9 12 2 2 4-4"],
  image:["M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z", "m3 16 5-5 4 4 4-6 5 7", "M8 7h.01"],
  edit:["M4 16v4h4L20 8l-4-4L4 16Z", "m14 6 4 4"],
  refresh:["M20 7v5h-5", "M4 17v-5h5", "M6.2 6.2A8 8 0 0 1 20 12M4 12a8 8 0 0 0 13.8 5.8"],
  key:["M15.5 3a5.5 5.5 0 0 0-5.3 7L3 17v4h4v-3h3l3-3a5.5 5.5 0 1 0 2.5-12Z", "M16 7h.01"],
  close:["m6 6 12 12", "M6 18 18 6"], chevron:["m9 5 7 7-7 7"],
  clock:["M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18Z", "M12 7v5l3 2"],
};
const glyph = (name, size=18) => h("svg", {width:size,height:size,viewBox:"0 0 24 24",fill:"none",
  stroke:"currentColor","stroke-width":1.7,"stroke-linecap":"round","stroke-linejoin":"round","aria-hidden":"true",focusable:"false"},
  (paths[name] || paths.clock).map(d=>h("path",{d})));

export default defineComponent({
  name:"AccountAvailability",
  setup() {
    const snapshot=ref(null), live=ref(null), busy=ref(false), error=ref(""), progressError=ref(""), dialog=ref(null), tab=ref("progress");
    const uid=`availability-${++sequence}`;
    let disposed=false, timer, detailTimer, liveBusy=false, liveSupported=true;
    function latest() {
      const source=snapshot.value || {};
      return live.value && (live.value.maintenance_progress?.sampled_at || 0) >= (source.maintenance_progress?.sampled_at || 0)
        ? {...source,...live.value} : source;
    }
    async function refresh() {
      if(disposed || busy.value || document.hidden) return;
      busy.value=true;
      try {
        const data=await api.get("/api/accounts/availability");
        if(!disposed) { snapshot.value=data; error.value=""; }
      } catch (_) {
        if(!disposed) error.value="可用性统计暂不可用，保留上次采样；不影响导入和账号操作。";
      } finally { if(!disposed) busy.value=false; }
    }
    async function refreshProgress() {
      if(disposed || liveBusy || !liveSupported || !dialog.value?.open || document.hidden) return;
      liveBusy=true;
      try {
        const data=await api.get("/api/accounts/maintenance-status");
        if(!disposed) {
          if(data?.maintenance_progress) { live.value=data; progressError.value=""; }
          else liveSupported=false;
        }
      } catch (_) {
        if(!disposed) progressError.value="同步进度暂不可用，当前显示最近一次采样。";
      } finally { liveBusy=false; }
    }
    function open() { dialog.value?.showModal(); refreshProgress(); }
    function close() { dialog.value?.close(); }
    onMounted(()=>{refresh();timer=window.setInterval(refresh,15000);detailTimer=window.setInterval(refreshProgress,3000);});
    onUnmounted(()=>{disposed=true;window.clearInterval(timer);window.clearInterval(detailTimer);close();});
    const iconButton=(name,icon,click,disabled=false)=>h("button",{type:"button","aria-label":name,title:name,disabled,onClick:click,
      class:"av-icon-button"},[glyph(icon,16)]);
    const labelValue=(label,value)=>h("div",{class:"av-label-value"},[h("span",{},label),h("strong",{},value)]);
    const number=x=>Number.isSafeInteger(x)&&x>=0?x.toLocaleString():"--";
    const ms=x=>typeof x==="number"&&Number.isFinite(x)?`${Math.round(x).toLocaleString()} ms`:"--";
    const tabs=[{key:"progress",label:"同步进度"},{key:"credentials",label:"凭据与额度"},{key:"rules",label:"处理规则"}];
    function moveTab(event,index) {
      const move={ArrowRight:1,ArrowLeft:-1,Home:-index,End:tabs.length-1-index}[event.key];
      if(move===undefined) return;
      event.preventDefault(); const next=(index+move+tabs.length)%tabs.length;
      tab.value=tabs[next].key; event.currentTarget.parentElement.querySelectorAll('[role="tab"]')[next]?.focus();
    }
    return ()=>{
      const data=latest(), view=availabilityDisplay(data), p=view.progress;
      const signals=data.maintenance?.signals || {}, prewarm=data.prewarm || {};
      const pill=()=>h("span",{class:`av-status av-tone-${p.tone}`,"data-maintenance-status":""},[h("i",{}),p.status]);
      const metric=(key,label,value,icon,quota,note)=>h("div",{class:"av-metric","data-availability-metric":key},[
        h("div",{class:"av-metric-label"},[glyph(icon,15),h("span",{},label)]),
        h("strong",{class:"av-metric-number"},value),
        quota?h("span",{class:"av-metric-note","data-ready-quota":key,title:`${view.quotaNote} 额度未知 ${quota.unknown} 个；无限额套餐 ${quota.unlimited} 个。`},`就绪额度 ${quota.known}${quota.separate?" *":""}`)
          :h("span",{class:"av-metric-note"},note),
      ]);
      const stat=(key,label,value,tone="")=>h("div",{class:`av-stat ${tone}`,"data-maintenance-count":key},[
        h("span",{},label),h("strong",{title:value,class:value.length>8?"av-long-number":""},value),
      ]);
      const pane=(key,children)=>h("div",{role:"tabpanel",id:`${uid}-${key}-panel`,"aria-labelledby":`${uid}-${key}-tab`,hidden:tab.value!==key,tabindex:0},children);
      const quotaDetail=(key,label,quota)=>h("section",{class:"av-quota","data-ready-quota-detail":key},[
        h("div",{class:"av-section-title"},[glyph(key==="generation"?"image":"edit",16),h("h4",{},label)]),
        h("div",{class:"av-quota-number"},[h("strong",{},quota.known),h("span",{},"已知就绪额度")]),
        labelValue("有已知额度的账号",`${quota.accounts} 个`),
        h("p",{class:"av-caption"},`未计入：额度未知 ${quota.unknown} 个 · 无限额套餐 ${quota.unlimited} 个`),
      ]);
      return h("section",{"aria-label":"账号可用性",class:"account-availability"},[
        h("div",{class:"av-header"},[
          h("div",{class:"av-title"},[h("span",{class:"av-heading-icon"},[glyph("shield",17)]),h("h2",{},"账号可用性")]),
          h("div",{class:"av-actions"},[pill(),h("button",{type:"button",class:"av-details-button",onClick:open},["详情",glyph("chevron",13)]),
            iconButton("刷新账号可用性","refresh",refresh,busy.value)]),
        ]),
        h("div",{class:"av-metrics"},[
          metric("generation","文生图候选",view.generation,"image",view.generationQuota),
          metric("edits","图生图候选",view.edits,"edit",view.editQuota),
          metric("recovery","可尝试恢复",view.renewable,"refresh",null,view.hasUnverified?`另有 ${view.unverified} 个历史失败待复核`:"等待验证或续期"),
          metric("manual","待补凭据",view.manual,"key",null,"需补充有效 AT / RT"),
        ]),
        h("div",{class:"av-summary", "aria-label":"后台同步概况"},[
          h("span",{class:"av-summary-primary"},[glyph("refresh",14),"正在处理 ",h("strong",{"data-maintenance-active":""},p.active)," 个",p.hasBatch?` · ${p.phase}`:""]),
          h("span",{},`本进程已处理 ${p.completed} 次`),
          h("span",{class:"av-summary-hint"},"候选不等于空闲槽位"),
        ]),
        error.value?h("p",{role:"status",class:"av-warning"},error.value):null,
        h("dialog",{ref:dialog,"aria-label":"账号可用性详情",class:"availability-dialog",onClick:e=>{
          if(e.target===dialog.value){const r=e.target.getBoundingClientRect();if(e.clientX<r.left||e.clientX>r.right||e.clientY<r.top||e.clientY>r.bottom)close();}
        }},[
          h("header",{class:"av-dialog-header"},[
            h("div",{},[h("div",{class:"av-title"},[h("span",{class:"av-heading-icon"},[glyph("shield",18)]),h("h3",{},"账号可用性详情")]),
              h("p",{class:"av-caption"},data.policy==="strict"?"严格准入 · 未知或过期 AT 不参与生图":"凭据就绪预评估 · 不代表实时空闲并发")]),
            iconButton("关闭可用性详情","close",close),
          ]),
          h("nav",{class:"av-tabs",role:"tablist","aria-label":"可用性详情分类"},tabs.map((item,index)=>h("button",{
            type:"button",role:"tab",id:`${uid}-${item.key}-tab`,"aria-controls":`${uid}-${item.key}-panel`,
            "aria-selected":tab.value===item.key,tabindex:tab.value===item.key?0:-1,
            onClick:()=>tab.value=item.key,onKeydown:e=>moveTab(e,index),
          },item.label))),
          h("div",{class:"av-dialog-body"},[
            pane("progress",[
              h("section",{class:"av-progress-section","aria-label":"后台同步进度"},[
                h("div",{class:"av-section-heading"},[h("h4",{},"后台处理进度"),pill()]),
                h("div",{class:"av-stats"},[stat("active","正在处理",p.active,"av-primary-stat"),stat("succeeded","累计成功",p.succeeded),stat("failed","累计失败",p.failed),stat("skipped","累计跳过",p.skipped)]),
                h("p",{class:"av-caption"},"仅统计后台 AT 续期与额度同步；包含处理过程中的等待，不含手动同步、导入及鉴权核验任务。"),
                h("div",{class:"av-batch"},[
                  h("div",{class:"av-label-value"},[h("span",{},p.hasBatch?`当前批次 · ${p.phase}`:p.known?"当前没有执行中的批次":"尚未取得维护实例的批次信息"),h("strong",{},p.hasBatch?`${p.batchCompleted} / ${p.batchTotal}`:`下次检查 ${p.nextCheck}`)]),
                  p.hasBatch?h("div",{class:"av-progress-track",role:"progressbar","aria-label":"当前批次进度","aria-valuenow":Math.round(p.ratio),"aria-valuemin":0,"aria-valuemax":100},[h("div",{style:{width:`${p.ratio}%`}})]):null,
                  p.hasBatch?h("p",{class:"av-caption"},`本批等待执行 ${p.queued} 个 · 调度上限每批 ${view.batch} 个`):null,
                  h("p",{class:"av-caption"},`最近一批：${p.last}${p.lastTime!=="--"?` · ${p.lastTime}`:""}`),
                ]),
                h("p",{class:"av-caption"},`累计为本进程启动以来的处理次数，同一账号可能重复计数；进程重启后归零。来源 ${p.instance} · 启动 ${p.started}`),
              ]),
              h("section",{class:"av-section","aria-label":"后台同步策略"},[
                h("div",{class:"av-section-heading"},[h("h4",{},"性能调度"),h("span",{class:"av-caption"},`${view.mode} · 每批最多 ${view.batch} 个`)]),
                h("p",{class:"av-policy-reason"},view.reason || "等待性能采样"),
                h("div",{class:"av-signals"},[labelValue("数据库 P95",ms(signals.database_ms)),labelValue("写锁 P95",ms(signals.writer_wait_ms)),labelValue("账号接口 P95",ms(signals.upstream_ms))]),
                h("div",{class:"av-signals"},[labelValue("活跃生图",view.active),labelValue("额度未知",view.quotaUnknown),labelValue("上传冷却",view.uploadLimited)]),
                h("p",{class:"av-caption"},`本实例预热 ${number(prewarm.ready)} / ${number(prewarm.target)} · 命中 ${number(prewarm.hit)} · 未命中 ${number(prewarm.miss)}`),
              ]),
            ]),
            pane("credentials",[
              h("section",{class:"av-section"},[h("h4",{},"凭据分布"),h("div",{class:"av-state-grid"},view.states.map(item=>h("div",{
                key:item.key,class:`av-state av-state-${item.key}`,"data-readiness-state":item.key,
              },[h("span",{},item.label),h("strong",{},item.value)])))]),
              h("section",{class:"av-section","aria-label":"凭据恢复分类"},[
                h("h4",{},"凭据恢复"),
                h("div",{class:"av-signals"},[labelValue("常规恢复候选",view.renewable),labelValue("历史失败待复核",view.unverified),labelValue("待补凭据",view.manual)]),
                h("p",{class:"av-caption"},"历史 RT 失败不代表当前凭据已确认失效：单独统计、排在常规续期候选之后复核，不据此自动删除账号。"),
              ]),
              h("div",{class:"av-quota-grid","aria-label":"就绪额度详情"},[quotaDetail("generation","文生图",view.generationQuota),quotaDetail("edits","图生图",view.editQuota)]),
              h("p",{class:"av-note"},view.quotaNote),
            ]),
            pane("rules",[
              h("section",{class:"av-rule"},[h("span",{class:"av-heading-icon"},[glyph("refresh")]),h("div",{},[
                h("h4",{},"可以恢复的账号，后台分批处理"),h("p",{},"AT 过期且仍有未确认失效的 RT，会等待后台续期；压力较高时减速或暂缓。可尝试恢复不代表 RT 已验证有效，也不保证全部能恢复。")])]),
              h("section",{class:"av-rule"},[h("span",{class:"av-heading-icon"},[glyph("key")]),h("div",{},[
                h("h4",{},"续期失败先标记，不在这一步删除"),h("p",{},"明确失效的 RT 不再进入后台续期候选。AT 仍有效的账号保留可用；AT 也不可用时需补充新凭据，可在账号管理中预览并手动清理 AT/RT 失效账号。")])]),
              h("section",{class:"av-rule"},[h("span",{class:"av-heading-icon"},[glyph("shield")]),h("div",{},[
                h("div",{class:"av-section-heading"},[h("h4",{},"上游确认异常，遵循原清理设置"),h("span",{class:"av-setting-state"},`自动移除异常：${view.autoDeleteInvalid}`)]),
                h("p",{},"只有进入确认异常处理流程，才按该开关决定自动删除或保留。AT 过期、待补凭据、临时超时不等于已确认账号异常。本面板只读，不修改清理设置或执行删除。")])]),
            ]),
            progressError.value?h("p",{class:"av-warning",role:"status"},progressError.value):null,
          ]),
          h("footer",{class:"av-dialog-footer"},[h("span",{},`${view.stale?"账号快照更新延迟":"账号统计约 15 秒刷新"} · 打开详情时同步进度约 3 秒刷新`),
            h("button",{type:"button",class:"av-secondary-button",onClick:close},"完成")]),
        ]),
      ]);
    };
  },
});
