// This repository ships built Vue assets. Keep the new panel readable and
// use the existing application's Vue runtime, HTTP client, modal and theme.
import {d as defineComponent, a as h, b as createVNode, l as Button, r as ref, G as computed, s as onMounted,
  x as onUnmounted, ap as onDeactivated, aG as onActivated, m as api} from "./index-BhEm-7EJ.js";
import {createImportController, readImportInputs, formatEvent, elapsed, jobLabel} from "./accountImportRuntime-v3.js";

const titles = {access_token: "导入 Access Token", refresh_token: "导入 Refresh Token", session_json: "导入 Session JSON", cpa_json: "导入 CPA JSON 文件", sub2api_json: "导入 Sub2API JSON 文件"};
export default defineComponent({
  name: "LocalAccountImportPanel",
  props: {mode: {default: "access_token"}, targetGroupId: {default: null}},
  emits: ["busy-change", "accounts-changed"],
  setup(props, {emit}) {
    const text = ref(""), files = ref([]), fileInput = ref(null), sync = ref(true), reading = ref(false), validation = ref("");
    const state = ref({jobs: [], job: null, events: [], busy: false, notice: "", connection: ""});
    let disposed = false;
    const busy = computed(() => reading.value || state.value.busy);
    let storage;
    try { storage = window.localStorage; } catch (_) {}
    const controller = createImportController({api, storage,
      onUpdate: value => { state.value = value; emit("busy-change", reading.value || value.busy); },
      onAccountsChanged: () => emit("accounts-changed"),
    });
    onMounted(() => controller.history());
    onActivated(controller.resume);
    onDeactivated(controller.stop);
    onUnmounted(() => { disposed = true; controller.stop(); emit("busy-change", false); });
    async function submit() {
      if (busy.value) return;
      reading.value = true; validation.value = ""; emit("busy-change", true);
      try {
        const accounts = await readImportInputs({text: text.value, files: files.value, mode: props.mode});
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
    return () => {
      const current = state.value, job = current.job;
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
        h("label", {class: "block text-xs"}, [h("span", {class: "ui-field-label"}, "选择文件（可多选，与粘贴内容合并）"),
          h("input", {ref: fileInput, type: "file", multiple: true, disabled: busy.value,
            accept: ".txt,.json,text/plain,application/json", class: "block w-full text-xs",
            onChange: event => { files.value = Array.from(event.target.files || []); }})]),
        files.value.length ? h("p", {class: "text-xs text-muted-foreground"}, `已选择 ${files.value.length} 个文件`) : null,
        h("label", {class: "flex items-center gap-2 text-xs"}, [h("input", {type: "checkbox", checked: sync.value, disabled: busy.value, onChange: event => {sync.value = event.target.checked;}}), "入库后在后台同步账号信息与额度"]),
        h("div", {class: "flex flex-wrap justify-end gap-2"}, [button("刷新任务列表", () => controller.history()), button(busy.value ? "正在提交…" : "开始导入", submit, busy.value || (!text.value.trim() && !files.value.length), true)]),
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
          h("pre", {class: "max-h-48 overflow-auto whitespace-pre-wrap break-all rounded-lg border border-border bg-muted/30 p-3 text-xs leading-5", "aria-label": "导入日志"}, current.events.map(formatEvent).join("\n") || "暂无导入日志"),
          h("p", {class: "text-xs text-muted-foreground"}, "显示最近 500 条日志。关闭窗口后任务继续执行，重新打开可恢复查看。"),
        ]),
      ]);
    };
  },
});
