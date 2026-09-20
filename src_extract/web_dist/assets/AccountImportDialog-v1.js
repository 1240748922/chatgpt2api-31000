// Keep the import panel mounted while minimized: drafts, selected job and
// the existing polling controller survive. This component owns no API calls.
import {d as defineComponent, b as vnode, r as ref, p as watch, J as nextTick,
  aK as ModalShell, aA as CloseButton, l as Button} from "./index-BhEm-7EJ.js?v=20260921-dashboard-cluster-v10";
import {M as ModalHeader} from "./ModalHeader-DOAbsAT0.js?v=20260921-dashboard-cluster-v10";

const icon = path => vnode("svg", {width: 16, height: 16, viewBox: "0 0 24 24", fill: "none",
  stroke: "currentColor", "stroke-width": 1.7, "stroke-linecap": "round", "stroke-linejoin": "round", "aria-hidden": "true"},
  [vnode("path", {d: path})]);

export default defineComponent({
  name: "AccountImportDialog",
  props: {open: Boolean, compact: Boolean},
  emits: ["close", "minimize", "restore"],
  setup(props, {emit, slots}) {
    const minimized = ref(false), restoreButton = ref(null), progress = ref("");
    // The original menu changes open from false to true, so it also restores
    // the existing minimized window instead of creating a duplicate panel.
    watch(() => props.open, open => { if (open) minimized.value = false; });
    const updateProgress = value => { progress.value = value; };
    async function minimize() {
      minimized.value = true;
      emit("minimize");
      await nextTick();
      restoreButton.value?.focus();
    }
    function restore() { minimized.value = false; emit("restore"); }
    function close() { minimized.value = false; emit("close"); }
    return () => vnode(ModalShell, {
      open: props.open || minimized.value, bare: true, modal: !minimized.value,
      ariaLabel: minimized.value ? "已最小化的导入任务" : "导入账号", zIndex: 120,
      maxWidth: minimized.value ? "24rem" : "60rem", closeOnOverlay: false, closeOnEscape: false,
      overlayClass: minimized.value ? "account-import-overlay account-import-overlay--minimized" : "ui-overlay-backdrop account-import-overlay",
      rootClass: ["account-import-window", props.compact && "account-import-window--local", minimized.value && "account-import-window--minimized"].filter(Boolean).join(" "),
      onClose: close,
    }, {default: () => [
      minimized.value ? vnode("div", {class: "account-import-dock"}, [
        vnode("button", {ref: restoreButton, type: "button", class: "account-import-dock-restore", "aria-label": "恢复导入窗口", onClick: restore}, [
          icon("M8 3H3v5M16 3h5v5M21 16v5h-5M8 21H3v-5"),
          vnode("span", {class: "account-import-dock-copy"}, [
            vnode("strong", {}, "账号导入"),
            vnode("span", {class: "account-import-dock-status"}, props.compact && progress.value || "窗口已最小化，点击恢复"),
          ]),
        ]),
        vnode(CloseButton, {label: "关闭导入窗口", onClick: close}),
      ]) : null,
      vnode("div", {class: "account-import-preserved", style: {display: minimized.value ? "none" : "flex"}}, [
        vnode(ModalHeader, {title: "导入账号", compact: true, onClose: close}, {actions: () => [
          vnode(Button, {size: "xs", variant: "ghost", title: "最小化", "aria-label": "最小化导入窗口", onClick: minimize},
            {default: () => [icon("M5 12h14")]}),
        ]}),
        vnode("div", {class: "account-import-dialog-body"}, slots.default?.({updateProgress})),
      ]),
    ]});
  },
});
