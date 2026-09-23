// This repository ships built Vue assets. Keep the new panel readable and
// use the existing application's Vue runtime, HTTP client, modal and theme.
import {d as defineComponent, b as createVNode, l as Button, O as Icon, a5 as Checkbox, r as ref, G as computed, s as onMounted,
  x as onUnmounted, m as api} from "./index-BhEm-7EJ.js?v=20260924-account-readiness-v13";
import {createImportController, readImportInputs, formatImportInput, formatEvent, elapsed, jobLabel, importHistoryLimit, importEventLimit} from "./accountImportRuntime-v3.js?v=20260924-account-readiness-v13";
import {I as ImportModePanel} from "./ImportModePanel-D37CU3pc.js?v=20260924-account-readiness-v13";

// The bundle's `a` export is createBaseVNode, a compiler-only helper: it
// does not normalize classes or a single VNode child. Use public createVNode
// for handwritten render functions and wrap a single child like Vue's h().
const h = (type, props, children) => createVNode(type, props,
  children?.__v_isVNode ? [children] : children);

const titles = {access_token: "导入 Access Token", refresh_token: "导入 Refresh Token", session_json: "导入 Session JSON", cpa_json: "导入 CPA JSON 文件", sub2api_json: "导入 Sub2API JSON 文件"};
export default defineComponent({
  name: "LocalAccountImportPanel",
  props: {mode: {default: "access_token"}, targetGroupId: {default: null}},
  emits: ["busy-change", "accounts-changed", "progress-change"],
  setup(props, {emit}) {
    const text = ref(""), fileInput = ref(null), sync = ref(true), reading = ref(false), validation = ref("");
    const itemFilter = ref("all"), itemSearch = ref(""), itemPage = ref(1);
    const view = ref("input"), logView = ref("items"), pageSize = 100;
    const state = ref({jobs: [], job: null, events: [], items: [], busy: false, notice: "", connection: ""});
    let disposed = false, selectedJob = "";
    const busy = computed(() => reading.value || state.value.busy);
    let storage;
    try { storage = window.localStorage; } catch (_) {}
    const controller = createImportController({api, storage,
      onUpdate: value => {
        state.value = value;
        if (value.selected !== selectedJob) { selectedJob = value.selected; itemPage.value = 1; }
        emit("busy-change", reading.value || value.busy);
        emit("progress-change", value.job ? `${jobLabel(value.job)} · ${value.job.processed ?? value.job.saved}/${value.job.total}` : "尚未提交任务");
      },
      onAccountsChanged: () => emit("accounts-changed"),
    });
    // This panel lives in a normal modal, not a KeepAlive boundary. Using
    // activated/deactivated here can stop an active controller without a
    // matching unmount, leaving a stale modal instance behind on reopen.
    onMounted(() => controller.history());
    onUnmounted(() => { disposed = true; controller.stop(); emit("busy-change", false); });
    const hasInput = computed(() => Boolean(text.value.trim()));
    async function onFileChange(event) {
      const target = event?.target;
      const current = event?.currentTarget;
      const input = target?.files ? target : current;
      const selected = Array.from(input?.files || []);
      if (!selected.length || busy.value) return;
      validation.value = "";
      reading.value = true;
      emit("busy-change", true);
      try {
        // Read files immediately and put normalized records into the same
        // textarea used by pasted AT/RT content. JSON fields and rt.* lines
        // are classified by parseInput; opaque RT values still follow the
        // currently selected RT tab. The textarea is the single source of
        // truth used by submit(), so the button becomes usable as soon as
        // this read finishes.
        const accounts = await readImportInputs({text: text.value, files: selected, mode: props.mode});
        if (disposed) return;
        text.value = formatImportInput(accounts, props.mode);
        validation.value = `已读取 ${selected.length} 个文件，共 ${accounts.length} 条，内容已填入上方输入框`;
      } catch (error) {
        if (!disposed) validation.value = error.message;
      } finally {
        if (input) input.value = "";
        if (!disposed) { reading.value = false; emit("busy-change", state.value.busy); }
      }
    }
    async function submit() {
      if (busy.value) return;
      if (!text.value.trim()) {
        validation.value = "请选择账号文件或粘贴账号内容";
        return;
      }
      reading.value = true; validation.value = ""; emit("busy-change", true);
      try {
        const accounts = await readImportInputs({text: text.value, mode: props.mode});
        if (disposed) return;
        if (await controller.submit({accounts, syncAfterImport: sync.value, targetGroupId: props.targetGroupId}) && !disposed) {
          text.value = "";
          view.value = "logs";
        }
      } catch (error) { if (!disposed) validation.value = error.message; }
      finally { if (!disposed) { reading.value = false; emit("busy-change", state.value.busy); } }
    }
    const button = (label, onClick, disabled = busy.value, primary = false, icon = "") => createVNode(Button, {
      size: "xs", variant: primary ? "primary" : "outline", onClick, disabled,
    }, {default: () => [icon ? createVNode(Icon, {icon, class: "h-3.5 w-3.5", "aria-hidden": "true"}) : null, label]});
    const metric = (label, value) => h("div", {class: "min-w-0"}, [h("div", {class: "text-muted-foreground text-xs"}, label), h("div", {class: "mt-1 font-medium tabular-nums text-sm"}, value)]);
    const stageLabel = stage => ({save: "入库", refresh: "RT 兑换", quota: "额度同步"}[stage] || stage || "处理");
    const statusClass = status => ({success: "text-emerald-600", failed: "text-red-600", skipped: "text-muted-foreground", info: "text-amber-600"}[status] || "text-muted-foreground");
    const tabs = (items, selected, label, prefix) => h("div", {class: "account-import-tabs", role: "tablist", "aria-label": label}, items.map(([id, title], index) =>
      h("button", {type: "button", role: "tab", class: "account-import-tab", id: `${prefix}-${id}-tab`, "aria-controls": `${prefix}-${id}`,
        "aria-selected": selected.value === id, tabindex: selected.value === id ? 0 : -1,
        onClick: () => {selected.value = id;},
        onKeydown: event => {
          const keys = {ArrowLeft: (index+items.length-1)%items.length, ArrowRight: (index+1)%items.length, Home: 0, End: items.length-1};
          if (!(event.key in keys)) return;
          event.preventDefault();
          selected.value = items[keys[event.key]][0];
          event.currentTarget.parentElement.children[keys[event.key]].focus();
        },
      }, title)));
    return () => {
      const current = state.value, job = current.job;
      const allItems = current.items || [];
      const query = itemSearch.value.trim().toLowerCase();
      const visibleItems = allItems.filter(item =>
        (itemFilter.value === "all" || item.status === itemFilter.value) &&
        (!query || String(item.account_label || "").toLowerCase().includes(query))
      );
      const itemCounts = allItems.reduce((counts, item) => { counts[item.status] = (counts[item.status] || 0) + 1; return counts; }, {});
      const pages = Math.max(1, Math.ceil(visibleItems.length/pageSize));
      const page = Math.min(itemPage.value, pages);
      const rows = visibleItems.slice((page-1)*pageSize, page*pageSize);
      return h("section", {class: "account-import-panel", "aria-label": "本地账号导入"}, [
        tabs([["input", "导入账号"], ["logs", "任务日志"]], view, "导入窗口视图", "account-import-view"),
        h("section", {class: "account-import-form", role: "tabpanel", id: "account-import-view-input", "aria-labelledby": "account-import-view-input-tab",
          style: {display: view.value === "input" ? "flex" : "none"}}, [
        createVNode(ImportModePanel, {title: titles[props.mode], description: props.mode === "refresh_token"
          ? "一行一个 RT。选择 TXT / JSON 文件后，按 refresh_token / refreshToken 键名提取并填入输入框。"
          : props.mode === "access_token"
            ? "一行一个 AT。选择 TXT / JSON 文件后，按 access_token / accessToken 键名提取并填入输入框。"
            : "支持单个账号或账号数组，自动识别 JSON 中的 AT / RT。后台分批入库，保留进度与日志。"}),
        h("label", {class: "account-import-input-field text-xs"}, [h("span", {class: "ui-field-label"}, props.mode === "refresh_token" ? "Refresh Token" : props.mode === "access_token" ? "Access Token" : "账号内容"),
          h("textarea", {value: text.value, "aria-label": "账号内容", onInput: event => {text.value = event.target.value;}, rows: "10", disabled: busy.value,
            class: "ui-textarea-sm font-mono", spellcheck: false, autocomplete: "off",
            placeholder: props.mode === "refresh_token" ? "一行一个 refresh token" : props.mode === "access_token" ? "一行一个 access token，或粘贴账号 JSON" : "粘贴账号 JSON"})]),
        h("input", {ref: fileInput, type: "file", multiple: true, disabled: busy.value,
          accept: ".txt,.json,text/plain,application/json", class: "hidden", "aria-label": "选择账号文件", onChange: onFileChange}),
        createVNode(Checkbox, {"model-value": sync.value, disabled: busy.value,
          "onUpdate:modelValue": value => {sync.value = Boolean(value);}}, {default: () => "入库后在后台同步账号信息与额度"}),
        h("div", {class: "flex flex-wrap justify-end gap-2"}, [
          button(reading.value ? "读取文件中…" : "读取 TXT / JSON 文件", () => fileInput.value?.click(), busy.value, false, "lucide:paperclip"),
          button(state.value.busy ? "正在提交…" : "开始导入", submit, busy.value || !hasInput.value, true, "lucide:cloud-upload"),
        ]),
        validation.value || current.notice ? h("p", {role: "status", class: "account-import-notice"}, validation.value || current.notice) : null,
        ]),
        h("section", {class: "account-import-log-view", role: "tabpanel", id: "account-import-view-logs", "aria-labelledby": "account-import-view-logs-tab",
          style: {display: view.value === "logs" ? "flex" : "none"}}, [
          h("div", {class: "account-import-log-toolbar"}, [
            h("select", {class: "ui-input-sm account-import-job-select", "aria-label": "选择导入任务", "aria-describedby": "account-import-history-note",
              title: `仅显示最近 ${importHistoryLimit} 个任务；不会删除后台历史`, value: current.selected || "", disabled: busy.value,
              onChange: event => controller.select(event.target.value)}, current.jobs.length
                ? current.jobs.map(job => h("option", {value: job.id, key: job.id}, `${new Date(job.created_at*1000).toLocaleString()} · ${jobLabel(job)} · ${job.total} 条`))
                : [h("option", {value: ""}, "暂无任务")]),
            button("刷新", () => controller.history(), busy.value, false, "lucide:refresh-cw"),
          ]),
          h("p", {id: "account-import-history-note", class: "account-import-history-note"}, `仅展示最近 ${importHistoryLimit} 个任务；每个任务展示最近 ${importEventLimit} 条日志事件（可含多条账号明细）。后台历史不受此显示上限影响。`),
          current.connection ? h("p", {role: "alert", class: "text-xs text-amber-600"}, current.connection) : null,
          job ? h("div", {class: "space-y-2", style: {flexShrink: 0}}, [
            h("p", {class: "text-xs leading-5", role: "status"}, `${jobLabel(job)} · 总耗时 ${elapsed(((job.done ? job.updated_at : Date.now()/1000)-job.created_at)*1000)} · 已处理 ${job.processed ?? job.saved}/${job.total}`),
            h("progress", {max: job.total || 1, value: job.processed ?? job.saved, class: "w-full h-2", "aria-label": "导入进度"}),
            h("div", {class: "account-import-summary"}, [metric("已入库 / 总数", `${job.saved} / ${job.total}`), metric("新增 / 跳过", `${job.added} / ${job.skipped}`), metric("RT 处理 / 失败", `${job.refresh_done || 0} / ${job.refresh_failed || 0}`), metric("额度成功 / 失败", `${job.synced} / ${job.sync_failed}`)]),
            job.status === "failed" ? button("从断点重试中断任务", controller.retry) : null,
          ]) : null,
          tabs([["items", "处理明细"], ["raw", "原始日志"]], logView, "任务日志视图", "account-import-log"),
          h("section", {class: "account-import-records", role: "tabpanel", id: "account-import-log-items", "aria-labelledby": "account-import-log-items-tab",
            style: {display: logView.value === "items" ? "flex" : "none"}}, [
            h("div", {class: "account-import-record-toolbar"}, [
              h("select", {class: "ui-input-sm text-xs", value: itemFilter.value, "aria-label": "筛选导入结果", onChange: event => {itemFilter.value = event.target.value; itemPage.value = 1;}}, [
                h("option", {value: "all"}, "全部状态"), h("option", {value: "success"}, "成功"), h("option", {value: "failed"}, "失败"), h("option", {value: "skipped"}, "跳过"),
              ]),
              h("input", {class: "ui-input-sm account-import-search text-xs", value: itemSearch.value, placeholder: "按邮箱搜索", "aria-label": "搜索邮箱", onInput: event => {itemSearch.value = event.target.value; itemPage.value = 1;}}),
            ]),
            h("p", {class: "text-xs text-muted-foreground", style: {flexShrink: 0}}, `处理记录：成功 ${itemCounts.success || 0} · 失败 ${itemCounts.failed || 0} · 跳过 ${itemCounts.skipped || 0}`),
            h("div", {class: "account-import-table-scroll", tabindex: 0, "aria-label": "账号明细滚动区域"}, rows.length ? h("table", {"aria-label": "账号处理明细"}, [
              h("thead", {}, h("tr", {}, ["邮箱 / 账号", "阶段", "状态", "结果"].map(label => h("th", {}, label)))),
              h("tbody", {}, rows.map((item, index) => h("tr", {key: `${item.event_id || "event"}-${item.index || index}-${index}`}, [
                h("td", {class: "font-mono"}, item.account_label || "未知账号"),
                h("td", {class: "whitespace-nowrap text-muted-foreground"}, stageLabel(item.stage)),
                h("td", {class: `whitespace-nowrap font-medium ${statusClass(item.status)}`}, item.status_label || item.status || "处理中"),
                h("td", {class: "text-muted-foreground"}, item.message || item.error_code || "—"),
              ]))),
            ]) : h("div", {class: "p-4 text-center text-xs text-muted-foreground"}, !job || job.done ? "暂无匹配结果" : "等待账号处理结果…")),
            h("div", {class: "account-import-pagination"}, [
              h("span", {class: "text-xs text-muted-foreground", "aria-label": "明细分页"}, `第 ${page} / ${pages} 页 · ${visibleItems.length} 条记录`),
              h("div", {class: "flex gap-2"}, [button("上一页", () => {itemPage.value = page-1;}, page <= 1), button("下一页", () => {itemPage.value = page+1;}, page >= pages)]),
            ]),
          ]),
          h("pre", {class: "account-import-raw-log", role: "tabpanel", id: "account-import-log-raw", "aria-labelledby": "account-import-log-raw-tab", "aria-label": "导入日志", tabindex: 0,
            style: {display: logView.value === "raw" ? "block" : "none"}}, current.events.map(formatEvent).join("\n") || "暂无导入日志"),
        ]),
        h("p", {class: "account-import-footer"}, "最小化或关闭窗口不会取消已提交的后台导入任务。"),
      ]);
    };
  },
});
