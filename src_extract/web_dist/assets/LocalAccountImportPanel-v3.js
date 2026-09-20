// This repository ships built Vue assets. Keep the new panel readable and
// use the existing application's Vue runtime, HTTP client, modal and theme.
import {d as defineComponent, a as h, b as createVNode, l as Button, r as ref, G as computed, s as onMounted,
  x as onUnmounted, m as api} from "./index-BhEm-7EJ.js?v=20260920-account-import-fix-v4";
import {createImportController, readImportInputs, formatEvent, elapsed, jobLabel} from "./accountImportRuntime-v3.js?v=20260920-account-import-fix-v4";

const titles = {access_token: "导入 Access Token", refresh_token: "导入 Refresh Token", session_json: "导入 Session JSON", cpa_json: "导入 CPA JSON 文件", sub2api_json: "导入 Sub2API JSON 文件"};
export default defineComponent({
  name: "LocalAccountImportPanel",
  props: {mode: {default: "access_token"}, targetGroupId: {default: null}},
  emits: ["busy-change", "accounts-changed"],
  setup(props, {emit}) {
    const text = ref(""), files = ref([]), fileInput = ref(null), sync = ref(true), reading = ref(false), validation = ref("");
    const itemFilter = ref("all"), itemSearch = ref("");
    const state = ref({jobs: [], job: null, events: [], items: [], busy: false, notice: "", connection: ""});
    let disposed = false;
    const busy = computed(() => reading.value || state.value.busy);
    let storage;
    try { storage = window.localStorage; } catch (_) {}
    const controller = createImportController({api, storage,
      onUpdate: value => { state.value = value; emit("busy-change", reading.value || value.busy); },
      onAccountsChanged: () => emit("accounts-changed"),
    });
    // This panel lives in a normal modal, not a KeepAlive boundary. Using
    // activated/deactivated here can stop an active controller without a
    // matching unmount, leaving a stale modal instance behind on reopen.
    onMounted(() => controller.history());
    onUnmounted(() => { disposed = true; controller.stop(); emit("busy-change", false); });
    const hasInput = computed(() => Boolean(text.value.trim() || files.value.length));
    const selectedFileNames = computed(() => files.value.map(file => file.name).filter(Boolean));
    function currentFiles() {
      // Keep a native-input fallback for browsers that replace FileList during
      // a modal repaint before Vue receives the change event.
      return files.value.length ? files.value : Array.from(fileInput.value?.files || []);
    }
    async function onFileChange(event) {
      const target = event?.target;
      const current = event?.currentTarget;
      const input = target?.files ? target : current;
      const selected = Array.from(input?.files || []);
      files.value = selected;
      validation.value = "";
      if (!selected.length || reading.value) return;
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
        text.value = JSON.stringify(accounts, null, 2);
        files.value = [];
        if (fileInput.value) fileInput.value.value = "";
        validation.value = `已读取 ${selected.length} 个文件，共 ${accounts.length} 条，内容已填入上方输入框`;
      } catch (error) {
        validation.value = error.message;
      } finally {
        reading.value = false;
        emit("busy-change", state.value.busy);
      }
    }
    async function submit() {
      if (busy.value) return;
      const selected = currentFiles();
      if (!text.value.trim() && !selected.length) {
        validation.value = "请选择账号文件或粘贴账号内容";
        return;
      }
      reading.value = true; validation.value = ""; emit("busy-change", true);
      try {
        const accounts = await readImportInputs({text: text.value, files: selected, mode: props.mode});
        if (disposed) return;
        if (await controller.submit({accounts, syncAfterImport: sync.value, targetGroupId: props.targetGroupId})) {
          text.value = ""; files.value = []; if (fileInput.value) fileInput.value.value = "";
        }
      } catch (error) { validation.value = error.message; }
      finally { reading.value = false; emit("busy-change", state.value.busy); }
    }
    const button = (label, onClick, disabled = busy.value, primary = false) => createVNode(Button, {
      size: "xs", variant: primary ? "primary" : "outline", onClick, disabled,
    }, {default: () => label});
    const metric = (label, value) => h("div", {class: "min-w-0"}, [h("div", {class: "text-muted-foreground text-xs"}, label), h("div", {class: "mt-1 font-medium tabular-nums text-sm"}, value)]);
    const stageLabel = stage => ({save: "入库", refresh: "RT 兑换", quota: "额度同步"}[stage] || stage || "处理");
    const statusClass = status => ({success: "text-emerald-600", failed: "text-red-600", skipped: "text-muted-foreground", info: "text-amber-600"}[status] || "text-muted-foreground");
    return () => {
      const current = state.value, job = current.job;
      const allItems = current.items || [];
      const query = itemSearch.value.trim().toLowerCase();
      const visibleItems = allItems.filter(item =>
        (itemFilter.value === "all" || item.status === itemFilter.value) &&
        (!query || String(item.account_label || "").toLowerCase().includes(query))
      );
      const itemCounts = allItems.reduce((counts, item) => { counts[item.status] = (counts[item.status] || 0) + 1; return counts; }, {});
      return h("section", {class: "space-y-3", "aria-label": "本地账号导入"}, [
        h("div", {}, [h("h3", {class: "text-sm font-medium"}, titles[props.mode]),
          h("p", {class: "mt-1 text-xs leading-6 text-muted-foreground"}, props.mode === "refresh_token"
            ? "一行一个 RT，支持 TXT / JSON 多文件。兑换成功后分批入库，失败项记录在下方日志。"
            : "粘贴账号内容或多选 TXT / JSON 文件。支持 AT、包含 RT 的 JSON，以及单个账号或账号数组。"),
          h("p", {class: "text-xs text-muted-foreground"}, "重复账号跳过，保留现有状态与额度；需要覆盖完整配置时使用“导入完整备份文件”。")]),
        h("label", {class: "block text-xs"}, [h("span", {class: "ui-field-label"}, "账号内容"),
          h("textarea", {value: text.value, onInput: event => {text.value = event.target.value;}, rows: "5", disabled: busy.value,
            class: "ui-textarea-sm font-mono", spellcheck: false, autocomplete: "off",
            placeholder: props.mode === "refresh_token" ? "一行一个 refresh token" : props.mode === "access_token" ? "一行一个 access token，或粘贴账号 JSON" : "粘贴账号 JSON"})]),
        h("label", {class: "block text-xs"}, [h("span", {class: "ui-field-label"}, "选择文件（自动识别并填入上方输入框）"),
          h("input", {ref: fileInput, type: "file", multiple: true, disabled: busy.value,
            accept: ".txt,.json,text/plain,application/json", class: "block w-full text-xs", "aria-label": "选择账号文件",
            onChange: onFileChange})]),
        selectedFileNames.value.length ? h("p", {class: "text-xs text-muted-foreground break-all"}, `已选择 ${selectedFileNames.value.length} 个文件：${selectedFileNames.value.join("、")}`) : null,
        h("label", {class: "flex items-center gap-2 text-xs"}, [h("input", {type: "checkbox", checked: sync.value, disabled: busy.value, onChange: event => {sync.value = event.target.checked;}}), "入库后在后台同步账号信息与额度"]),
        h("div", {class: "flex flex-wrap justify-end gap-2"}, [button("刷新任务列表", () => controller.history()), button(reading.value ? "读取文件中…" : busy.value ? "正在提交…" : "开始导入", submit, busy.value || !hasInput.value, true)]),
        validation.value || current.notice ? h("p", {role: "status", class: "text-xs leading-5 break-words"}, validation.value || current.notice) : null,
        h("div", {class: "border-t border-border pt-3 space-y-3"}, [
          h("label", {class: "block text-xs"}, [h("span", {class: "ui-field-label"}, "导入任务与日志"),
            h("select", {class: "ui-input-sm w-full", "aria-label": "选择导入任务", value: current.selected || "", disabled: busy.value,
              onChange: event => controller.select(event.target.value)}, current.jobs.length
                ? current.jobs.map(job => h("option", {value: job.id, key: job.id}, `${new Date(job.created_at*1000).toLocaleString()} · ${jobLabel(job)} · ${job.total} 条`))
                : [h("option", {value: ""}, "暂无任务")])]),
          current.connection ? h("p", {role: "alert", class: "text-xs text-amber-600"}, current.connection) : null,
          job ? h("div", {class: "space-y-2"}, [
            h("p", {class: "text-xs leading-5", role: "status"}, `${jobLabel(job)} · 总耗时 ${elapsed(((job.done ? job.updated_at : Date.now()/1000)-job.created_at)*1000)} · 已处理 ${job.processed ?? job.saved}/${job.total}`),
            h("progress", {max: job.total || 1, value: job.processed ?? job.saved, class: "w-full h-2", "aria-label": "导入进度"}),
            h("div", {class: "grid grid-cols-2 gap-3"}, [metric("已入库 / 总数", `${job.saved} / ${job.total}`), metric("新增 / 跳过", `${job.added} / ${job.skipped}`), metric("RT 已处理 / 失败", `${job.refresh_done || 0} / ${job.refresh_failed || 0}`), metric("额度同步成功 / 失败", `${job.synced} / ${job.sync_failed}`)]),
            job.status === "failed" ? button("从断点重试中断任务", controller.retry) : null,
          ]) : null,
          job ? h("div", {class: "rounded-xl border border-border bg-muted/20 p-3 space-y-3", "aria-label": "导入进度明细"}, [
            h("div", {class: "flex items-center justify-between gap-3"}, [
              h("div", {}, [h("div", {class: "text-sm font-medium"}, "账号处理明细"), h("div", {class: "text-xs text-muted-foreground mt-1"}, `成功 ${itemCounts.success || 0} · 失败 ${itemCounts.failed || 0} · 跳过 ${itemCounts.skipped || 0}`)]),
              h("span", {class: "text-xs text-muted-foreground tabular-nums"}, `${visibleItems.length} / ${allItems.length}`),
            ]),
            h("div", {class: "flex flex-wrap gap-2"}, [
              h("select", {class: "ui-input-sm text-xs", value: itemFilter.value, "aria-label": "筛选导入结果", onChange: event => {itemFilter.value = event.target.value;}}, [
                h("option", {value: "all"}, "全部状态"), h("option", {value: "success"}, "成功"), h("option", {value: "failed"}, "失败"), h("option", {value: "skipped"}, "跳过"),
              ]),
              h("input", {class: "ui-input-sm min-w-48 flex-1 text-xs", value: itemSearch.value, placeholder: "按邮箱搜索", "aria-label": "搜索邮箱", onInput: event => {itemSearch.value = event.target.value;}}),
            ]),
            h("div", {class: "max-h-64 overflow-auto rounded-lg border border-border bg-background"}, visibleItems.length ? h("table", {class: "w-full text-xs"}, [
              h("thead", {class: "sticky top-0 bg-muted/90 text-left text-muted-foreground"}, h("tr", {}, [h("th", {class: "px-2 py-2 font-medium"}, "邮箱 / 账号"), h("th", {class: "px-2 py-2 font-medium"}, "阶段"), h("th", {class: "px-2 py-2 font-medium"}, "状态"), h("th", {class: "px-2 py-2 font-medium"}, "结果")])),
              h("tbody", {}, visibleItems.map((item, index) => h("tr", {key: `${item.event_id || "event"}-${item.index || index}-${index}`, class: "border-t border-border/70 align-top"}, [
                h("td", {class: "max-w-64 break-all px-2 py-2 font-mono"}, item.account_label || "未知账号"),
                h("td", {class: "whitespace-nowrap px-2 py-2 text-muted-foreground"}, stageLabel(item.stage)),
                h("td", {class: `whitespace-nowrap px-2 py-2 font-medium ${statusClass(item.status)}`}, item.status_label || item.status || "处理中"),
                h("td", {class: "min-w-56 break-words px-2 py-2 text-muted-foreground"}, item.message || item.error_code || "—"),
              ]))),
            ]) : h("div", {class: "p-4 text-center text-xs text-muted-foreground"}, job.done ? "暂无匹配结果" : "等待账号处理结果…")),
          ]) : null,
          h("details", {class: "rounded-lg border border-border bg-muted/10"}, [
            h("summary", {class: "cursor-pointer px-3 py-2 text-xs font-medium"}, "查看原始导入日志"),
            h("pre", {class: "max-h-48 overflow-auto whitespace-pre-wrap break-all border-t border-border p-3 text-xs leading-5", "aria-label": "导入日志"}, current.events.map(formatEvent).join("\n") || "暂无导入日志"),
          ]),
          h("p", {class: "text-xs text-muted-foreground"}, "导入任务在后台继续执行；临时 502/503/504 会自动重试，重新打开窗口可恢复进度。"),
        ]),
      ]);
    };
  },
});
