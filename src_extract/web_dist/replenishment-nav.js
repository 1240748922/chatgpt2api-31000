(function () {
  const NAV_MARKER = "data-account-replenishment-nav";
  const PAGE_MARKER = "data-account-replenishment-page";
  const TARGET_PATH = "/#/settings?tab=replenishment";
  const ADMIN_KEY = "chatgpt2api.adminKey";
  const css = `
    .register-page{width:100%;max-width:1440px;margin:0 auto;padding:clamp(20px,3vw,40px);color:hsl(var(--foreground));}
    .register-hero{display:flex;align-items:flex-end;justify-content:space-between;gap:24px;margin-bottom:24px;padding-bottom:22px;border-bottom:1px solid hsl(var(--border));}
    .register-eyebrow{margin-bottom:8px;font-size:11px;font-weight:700;letter-spacing:.12em;color:hsl(var(--muted-foreground));text-transform:uppercase;}
    .register-title{margin:0;font-size:clamp(25px,3vw,36px);line-height:1.1;font-weight:700;letter-spacing:0;}
    .register-subtitle{max-width:680px;margin:10px 0 0;color:hsl(var(--muted-foreground));font-size:14px;line-height:1.7;}
    .register-actions{display:flex;flex-wrap:wrap;gap:8px;justify-content:flex-end;}
    .register-button{min-height:36px;border:1px solid hsl(var(--border));border-radius:8px;background:hsl(var(--card));padding:0 13px;color:hsl(var(--foreground));font-size:13px;cursor:pointer;transition:.15s ease;}
    .register-button:hover{border-color:hsl(var(--foreground)/.3);background:hsl(var(--muted)/.45);}.register-button.primary{border-color:hsl(var(--primary));background:hsl(var(--primary));color:hsl(var(--primary-foreground));}.register-button.danger{border-color:hsl(0 72% 60% / .35);color:hsl(0 72% 45%);}.register-button:disabled{cursor:not-allowed;opacity:.55;}
    .register-layout{display:grid;grid-template-columns:minmax(0,1.35fr) minmax(320px,.65fr);align-items:start;gap:20px;}.register-stack{display:grid;gap:16px;min-width:0;}
    .register-card{border:1px solid hsl(var(--border));border-radius:10px;background:hsl(var(--card));padding:18px;box-shadow:0 1px 2px hsl(var(--foreground)/.03);}.register-card-title{font-size:15px;font-weight:700;}.register-card-help{margin:5px 0 0;color:hsl(var(--muted-foreground));font-size:12px;line-height:1.55;}.register-card-header{display:flex;align-items:flex-start;justify-content:space-between;gap:16px;margin-bottom:16px;}
    .register-metrics{display:grid;grid-template-columns:repeat(5,minmax(0,1fr));gap:10px;}.register-metric{min-width:0;border:1px solid hsl(var(--border));border-radius:8px;background:hsl(var(--background));padding:13px;}.register-metric-label{color:hsl(var(--muted-foreground));font-size:12px;}.register-metric-value{margin-top:8px;font-size:24px;font-weight:700;line-height:1;}.register-metric-value.warning{color:hsl(38 92% 42%);}.register-metric-value.success{color:hsl(142 65% 36%);}.register-metric-value.danger{color:hsl(0 72% 50%);}
    .register-form-grid{display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:14px 16px;}.register-field{min-width:0;}.register-field.wide{grid-column:1/-1;}.register-field label{display:block;margin-bottom:6px;font-size:12px;font-weight:600;}.register-field input{box-sizing:border-box;width:100%;height:36px;border:1px solid hsl(var(--border));border-radius:7px;background:hsl(var(--background));padding:0 10px;color:hsl(var(--foreground));font:inherit;font-size:13px;outline:none;}.register-field input:focus{border-color:hsl(var(--primary));box-shadow:0 0 0 2px hsl(var(--primary)/.14);}.register-field small{display:block;margin-top:5px;color:hsl(var(--muted-foreground));font-size:11px;line-height:1.45;}.register-toggle{display:flex;align-items:center;gap:9px;min-height:36px;font-size:13px;cursor:pointer;}.register-toggle input{width:16px;height:16px;accent-color:hsl(var(--primary));}
    .register-save-row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:18px;padding-top:16px;border-top:1px solid hsl(var(--border));}.register-save-state{color:hsl(var(--muted-foreground));font-size:12px;}.register-save-state.error{color:hsl(0 72% 50%);}.register-log-card{position:sticky;top:20px;height:min(820px,calc(100vh - 40px));min-height:520px;max-height:calc(100vh - 40px);display:flex;flex-direction:column;overflow:hidden;}.register-log-toolbar{flex:0 0 auto;display:flex;align-items:flex-start;justify-content:space-between;gap:10px;margin-bottom:14px;}.register-log-status{display:flex;align-items:center;gap:7px;color:hsl(var(--muted-foreground));font-size:12px;white-space:nowrap;}.register-log-dot{width:8px;height:8px;border-radius:50%;background:hsl(var(--muted-foreground));}.register-log-dot.running{background:hsl(38 92% 50%);}.register-log-dot.success{background:hsl(142 65% 42%);}.register-log-dot.error{background:hsl(0 72% 55%);}.register-log{flex:1 1 auto;height:0;min-height:0;overflow-y:auto;overflow-x:hidden;border:1px solid hsl(var(--border));border-radius:8px;background:#111827;padding:14px;color:#d1d5db;font:12px/1.65 ui-monospace,SFMono-Regular,Consolas,monospace;white-space:pre-wrap;word-break:break-word;}.register-log-empty{display:grid;place-items:center;min-height:260px;color:#6b7280;text-align:center;font:13px/1.6 system-ui,sans-serif;}.register-alert{flex:0 0 auto;margin-bottom:14px;border:1px solid hsl(0 72% 55% / .3);border-radius:8px;background:hsl(0 72% 55% / .07);padding:10px 12px;color:hsl(0 72% 45%);font-size:12px;line-height:1.55;}.register-page *{letter-spacing:0;}
    @media (max-width:1000px){.register-layout{grid-template-columns:1fr}.register-log-card{position:static;height:520px;min-height:360px;max-height:520px}.register-hero{align-items:flex-start;flex-direction:column}.register-actions{justify-content:flex-start}}@media (max-width:640px){.register-page{padding:18px 14px}.register-metrics{grid-template-columns:repeat(2,minmax(0,1fr))}.register-form-grid{grid-template-columns:1fr}.register-field.wide{grid-column:auto}.register-log-card{height:480px;max-height:480px}}
  `;
  function isRegisterRoute(){return window.location.hash.includes("/settings")&&window.location.hash.includes("tab=replenishment")}
  function authHeaders(){const key=window.localStorage.getItem(ADMIN_KEY)||"";return key?{Authorization:`Bearer ${key}`,"Content-Type":"application/json"}:{"Content-Type":"application/json"}}
  async function request(path,options){const response=await fetch(path,{...options,signal:options?.signal||AbortSignal.timeout(15000),headers:{...authHeaders(),...(options?.headers||{})}});const body=await response.json().catch(()=>({}));if(!response.ok){const detail=body?.detail?.error||body?.detail||body?.error||`请求失败（${response.status}）`;throw new Error(typeof detail==="string"?detail:JSON.stringify(detail))}return body}
  function esc(value){return String(value??"").replace(/&/g,"&amp;").replace(/"/g,"&quot;")}
  function field(label,key,value,help,type="text",wide=false){return `<div class="register-field${wide?" wide":""}"><label for="register-${key}">${label}</label><input id="register-${key}" data-register-key="${key}" type="${type}" value="${esc(value)}"><small>${help}</small></div>`}
  function selectField(label,key,value,options,help,wide=false){return `<div class="register-field${wide?" wide":""}"><label for="register-${key}">${label}</label><select id="register-${key}" data-register-key="${key}">${options.map(([option,labelText])=>`<option value="${esc(option)}"${String(value??"")===option?" selected":""}>${labelText}</option>`).join("")}</select><small>${help}</small></div>`}
  function formatLogTimestamp(value){const raw=String(value??"").trim();if(!/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/.test(raw))return raw;const date=new Date(`${raw.slice(0,19).replace(" ","T")}Z`);if(Number.isNaN(date.getTime()))return raw;date.setUTCHours(date.getUTCHours()+8);return date.toISOString().slice(0,19).replace("T"," ")}
  function formatLogEntry(entry){const result=entry||{},metrics=result.post_metrics||result.metrics||{},lines=[];if(result.finished_at)lines.push(`[${formatLogTimestamp(result.finished_at)}]`);if(result.batch_id)lines.push(`批次：${result.batch_id}`);if(result.reason)lines.push(`结果：${result.reason}`);if(result.triggered!==undefined)lines.push(`是否触发：${result.triggered?"是":"否"}`);if(metrics.current_available!==undefined)lines.push(`账号池：当前可用 ${metrics.current_available}，待确认 ${metrics.pending_confirmation_count??metrics.unconfirmed_available??0}，预计可用 ${metrics.estimated_available??0}`);if(result.import)lines.push(`导入：${result.import.imported||0}，跳过：${result.import.skipped||0}，代理继承：${result.import.proxy_assigned?"已设置":"未设置"}`);if(result.quota_refresh)lines.push(`额度同步：成功 ${result.quota_refresh.synced||0}，失败 ${result.quota_refresh.errors||0}，尝试 ${result.quota_refresh.attempts||0} 次`);if(result.quota_refresh?.error)lines.push(`额度同步错误：${result.quota_refresh.error}`);if(result.config_error)lines.push(`配置提示：${result.config_error}`);if(result.log_path)lines.push(`日志文件：${result.log_path}`);if(result.stdout_tail)lines.push(`\n--- 注册机输出 ---\n${result.stdout_tail}`);if(result.stderr_tail)lines.push(`\n--- 错误输出 ---\n${result.stderr_tail}`);if(result.error)lines.push(`错误：${result.error}`);return lines.join("\n")}
  function formatLog(status,error){const history=Array.isArray(status?.log_history)?status.log_history:[],result=status?.last_result||{},sections=history.map(formatLogEntry).filter(Boolean);if(status?.running&&result.reason==="running")sections.push(formatLogEntry({...result,finished_at:"进行中"}));if(!sections.length){const metrics=status?.current_metrics||{};sections.push(formatLogEntry({finished_at:status?.last_checked_at,reason:result.reason||"状态检查",metrics}))}if(error)sections.push(`请求错误：${error}`);return sections.join("\n\n==============================\n\n")||"暂无运行记录\n\n保存配置或执行一次补号后，这里会显示注册机输出和 session 导入结果。"}
  function createPage(host){const page=document.createElement("section");page.className="register-page";page.setAttribute(PAGE_MARKER,"true");page.innerHTML=`<div class="register-hero"><div><div class="register-eyebrow">ACCOUNT POOL AUTOMATION</div><h1 class="register-title">注册机</h1><p class="register-subtitle">根据账号池可用数量自动启动注册工具，生成新 session 后自动导入账号池。</p></div><div class="register-actions"><button class="register-button" data-register-action="refresh">刷新状态</button><button class="register-button primary" data-register-action="run">立即补号</button><button class="register-button danger" data-register-action="force">强制执行</button></div></div><div class="register-layout"><div class="register-stack"><div class="register-metrics"><div class="register-metric"><div class="register-metric-label">当前可用</div><div class="register-metric-value" data-register-metric="available">-</div></div><div class="register-metric"><div class="register-metric-label">待确认</div><div class="register-metric-value" data-register-metric="pending">-</div></div><div class="register-metric"><div class="register-metric-label">目标可用</div><div class="register-metric-value" data-register-metric="target">-</div></div><div class="register-metric"><div class="register-metric-label">当前额度</div><div class="register-metric-value" data-register-metric="quota">-</div></div><div class="register-metric"><div class="register-metric-label">运行状态</div><div class="register-metric-value" data-register-metric="state">未检查</div></div></div><div class="register-card"><div class="register-card-header"><div><div class="register-card-title">补号策略</div><p class="register-card-help">低于阈值时自动触发，补充到目标数量。建议目标数量按生图并发和账号失效周期预留余量。</p></div><label class="register-toggle"><input type="checkbox" data-register-enabled>启用自动补充</label></div><div class="register-form-grid" data-register-strategy></div></div><div class="register-card"><div class="register-card-header"><div><div class="register-card-title">注册机运行参数</div><p class="register-card-help">配置注册工具目录、入口脚本和并发参数。保存后后台注册机按此配置执行。</p></div></div><div class="register-form-grid" data-register-runtime></div><div class="register-save-row"><span class="register-save-state" data-register-save-state>配置读取中...</span><button class="register-button primary" data-register-action="save">保存配置</button></div></div></div><aside class="register-card register-log-card"><div class="register-log-toolbar"><div><div class="register-card-title">运行日志</div><p class="register-card-help">显示最近一次状态检查、注册机输出和 session 导入结果。显示时区：UTC+8。</p></div><div class="register-log-status"><span class="register-log-dot" data-register-log-dot></span><span data-register-log-status>未检查</span></div></div><div class="register-alert" data-register-alert hidden></div><pre class="register-log" data-register-log><span class="register-log-empty">暂无运行记录</span></pre></aside></div>`;host.appendChild(page);return page}
  function setMetric(page,key,value,tone=""){const node=page.querySelector(`[data-register-metric="${key}"]`);if(node){node.textContent=value??"-";node.className=`register-metric-value ${tone}`}}
  function setFields(page,config){page.querySelector("[data-register-strategy]").innerHTML=[field("最低可用账号","minimum_available",config.minimum_available,"低于这个数量时触发补号。","number"),field("目标可用账号","target_available",config.target_available,"补号后尽量达到的可用账号数。","number"),field("检查间隔（秒）","interval_seconds",config.interval_seconds,"后台检查账号池的最小间隔。","number"),field("补号冷却（秒）","cooldown_seconds",config.cooldown_seconds,"成功补号后等待，避免连续启动注册机。","number"),field("单次最大注册数","max_batch_size",config.max_batch_size,"每次最多生成的账号数量。","number"),field("并发 workers","workers",config.workers,"传给注册机的 worker 数。","number"),field("启动超时（秒）","launch_timeout_seconds",config.launch_timeout_seconds,"注册机进程最长运行时间。","number")].join("");page.querySelector("[data-register-runtime]").innerHTML=[selectField("注册模式","registration_mode",config.registration_mode||"password",[["password","密码模式"],["passwordless","邮箱验证码模式"]],"密码模式会保存注册机生成的账号密码，便于后续恢复；邮箱验证码模式不保存账号密码。"),field("工作目录","workdir",config.workdir,"注册机项目所在目录。","text",true),field("输出目录","output_dir",config.output_dir,"注册机保存 session 文件的目录。","text",true),field("入口脚本","entrypoint",config.entrypoint,"默认 chatgpt_phone_reg.py。"),field("Python 可执行文件","python_executable",config.python_executable,"留空时自动寻找项目虚拟环境或当前 Python。"),field("额外参数","extra_args",config.extra_args,"追加传给注册机的命令行参数。","text",true)].join("")}
  function readConfig(page,current){const next={...current};page.querySelectorAll("[data-register-key]").forEach(input=>{next[input.dataset.registerKey]=input.type==="number"?Number(input.value||0):input.value.trim()});next.enabled=!!page.querySelector("[data-register-enabled]")?.checked;return next}
  async function mountPage(){if(!isRegisterRoute())return;const routeView=document.querySelector(".route-view-content");if(!routeView||routeView.dataset.registerHidden==="true")return;const host=routeView.parentElement;routeView.dataset.registerHidden="true";routeView.style.display="none";const shellTitle=document.querySelector("main header h2");if(shellTitle){shellTitle.dataset.registerOriginalTitle=shellTitle.textContent;shellTitle.textContent="注册机"}const page=createPage(host);const runtimeSaveButton=page.querySelector('[data-register-action="save"]');if(runtimeSaveButton){runtimeSaveButton.textContent="保存补号策略与运行参数";const runtimeCard=runtimeSaveButton.closest(".register-card");const runtimeHelp=runtimeCard?.querySelector(".register-card-help");if(runtimeHelp)runtimeHelp.textContent="只保存补号策略、注册机目录、入口脚本和运行参数。邮箱服务与代理请在下方单独保存。"}let config={},status={},statusError="";const saveState=page.querySelector("[data-register-save-state]"),log=page.querySelector("[data-register-log]"),alert=page.querySelector("[data-register-alert]"),logStatus=page.querySelector("[data-register-log-status]"),logDot=page.querySelector("[data-register-log-dot]");
    const strategyCard=page.querySelector("[data-register-strategy]")?.closest(".register-card");if(strategyCard&&!strategyCard.dataset.strategySaveMounted){strategyCard.dataset.strategySaveMounted="true";const strategyHeader=strategyCard.querySelector(".register-card-header"),toggle=strategyCard.querySelector(".register-toggle"),actions=document.createElement("div");actions.className="register-strategy-actions";if(toggle)actions.appendChild(toggle);const strategyButton=document.createElement("button");strategyButton.type="button";strategyButton.className="register-button primary";strategyButton.dataset.registerAction="save-strategy";strategyButton.textContent="保存补号策略";actions.appendChild(strategyButton);if(strategyHeader)strategyHeader.appendChild(actions);const strategyRow=document.createElement("div");strategyRow.className="register-save-row";strategyRow.innerHTML=`<span class="register-save-state" data-register-strategy-save-state>修改策略或开关后，请点击此按钮保存</span>`;strategyCard.appendChild(strategyRow);const strategyState=strategyRow.querySelector("[data-register-strategy-save-state]");strategyButton.addEventListener("click",async()=>{strategyButton.disabled=true;strategyState.className="register-save-state";strategyState.textContent="保存中...";try{const settings=await request("/api/settings"),next={...(settings.settings?.account_replenishment||{})};["minimum_available","target_available","interval_seconds","cooldown_seconds","max_batch_size"].forEach(key=>{const field=page.querySelector(`[data-register-key="${key}"]`);next[key]=Number(field?.value||0)});next.enabled=!!page.querySelector("[data-register-enabled]")?.checked;const result=await request("/api/settings",{method:"PATCH",body:JSON.stringify({revision:settings.revision,account_replenishment:next})});config={...config,...next};page.dataset.registerRevision=result.revision||settings.revision||"";strategyState.textContent=next.enabled?"自动补充已启用并保存":"自动补充已停用并保存";statusError="";renderStatus()}catch(error){strategyState.className="register-save-state error";strategyState.textContent=error.message||"补号策略保存失败"}finally{strategyButton.disabled=false}});page.querySelector("[data-register-enabled]")?.addEventListener("change",()=>{strategyState.className="register-save-state";strategyState.textContent="开关已修改，请点击保存补号策略"})}const runtimeButton=page.querySelector('[data-register-action="save"]');if(runtimeButton){runtimeButton.textContent="保存注册机运行参数";const runtimeHelp=runtimeButton.closest(".register-card")?.querySelector(".register-card-help");if(runtimeHelp)runtimeHelp.textContent="只保存注册机目录、入口脚本、Python 和运行参数；补号阈值请使用上方按钮保存。"}
    function renderStatus(){const metrics=status.current_metrics||{},available=metrics.current_available??metrics.estimated_available??0,pending=metrics.pending_confirmation_count??metrics.unconfirmed_available??0,target=config.target_available||0,quota=metrics.current_quota??metrics.estimated_quota??0,running=!!status.running;setMetric(page,"available",available,target>0&&available<(config.minimum_available||0)?"warning":"success");setMetric(page,"pending",pending,pending>0?"warning":"success");setMetric(page,"target",target);setMetric(page,"quota",quota);setMetric(page,"state",running?"执行中":statusError?"异常":"待命",running?"warning":statusError?"danger":"success");logStatus.textContent=running?"注册机执行中":statusError?"状态异常":"已连接";logDot.className=`register-log-dot ${running?"running":statusError?"error":"success"}`;const followBottom=log.scrollHeight-log.scrollTop-log.clientHeight<48;log.textContent=formatLog(status,statusError);if(followBottom)log.scrollTop=log.scrollHeight;if(statusError){alert.hidden=false;alert.textContent=statusError}else alert.hidden=true}
    async function load(){try{const settings=await request("/api/settings");page.dataset.registerRevision=settings.revision||"";config={...(settings.settings?.account_replenishment||{})};page.querySelector("[data-register-enabled]").checked=!!config.enabled;setFields(page,config);saveState.textContent="配置已加载";statusError=""}catch(error){statusError=error.message;saveState.textContent=error.message||"配置加载失败"}try{status=await request("/api/accounts/replenishment")}catch(error){status={};statusError=error.message}renderStatus()}
    async function save(){const button=page.querySelector('[data-register-action="save"]');button.disabled=true;saveState.className="register-save-state";saveState.textContent="保存中...";try{config=readConfig(page,config);const result=await request("/api/settings",{method:"PATCH",body:JSON.stringify({revision:page.dataset.registerRevision||"",account_replenishment:config})});page.dataset.registerRevision=result.revision||page.dataset.registerRevision||"";saveState.textContent="已保存";statusError="";renderStatus()}catch(error){saveState.className="register-save-state error";saveState.textContent=error.message||"保存失败"}finally{button.disabled=false}}
    async function run(force){const button=page.querySelector(`[data-register-action="${force?"force":"run"}"]`);button.disabled=true;logStatus.textContent=force?"强制执行中":"检查中";logDot.className="register-log-dot running";try{status=await request(`/api/accounts/replenishment/run${force?"?force=true":""}`,{method:"POST",body:"{}"});statusError=""}catch(error){statusError=error.message}finally{button.disabled=false;await load()}}
    page.querySelector('[data-register-action="refresh"]').addEventListener("click",load);page.querySelector('[data-register-action="save"]').addEventListener("click",save);page.querySelector('[data-register-action="run"]').addEventListener("click",()=>run(false));page.querySelector('[data-register-action="force"]').addEventListener("click",()=>run(true));await load()}
  function unmountPage(){const page=document.querySelector(`[${PAGE_MARKER}]`);if(page)page.remove();const shellTitle=document.querySelector("main header h2[data-register-original-title]");if(shellTitle){shellTitle.textContent=shellTitle.dataset.registerOriginalTitle;delete shellTitle.dataset.registerOriginalTitle}const routeView=document.querySelector('.route-view-content[data-register-hidden="true"]');if(routeView){routeView.style.display="";delete routeView.dataset.registerHidden}}
  function addNavItem(){const nav=document.querySelector("#app-sidebar-navigation");if(!nav||nav.querySelector(`[${NAV_MARKER}]`))return;const settingsLink=[...nav.querySelectorAll("a")].find(link=>/#\/settings$/.test(link.getAttribute("href")||"")||link.getAttribute("href")==="/settings");if(!settingsLink)return;const item=settingsLink.cloneNode(true);item.setAttribute(NAV_MARKER,"true");item.setAttribute("href",TARGET_PATH);item.setAttribute("aria-label","注册机");item.querySelectorAll("span").forEach(span=>{if(span.classList.contains("sidebar-label"))span.textContent="注册机"});const path=item.querySelector("path");if(path)path.setAttribute("d","M12 2a3 3 0 0 1 3 3v1.1a7 7 0 0 1 2.5 1.7l.95-.55a2 2 0 1 1 2 3.46l-.95.55c.12.55.18 1.11.18 1.69s-.06 1.14-.18 1.69l.95.55a2 2 0 1 1-2 3.46l-.95-.55a7 7 0 0 1-2.5 1.7V21a3 3 0 1 1-6 0v-1.1a7 7 0 0 1-2.5-1.7l-.95.55a2 2 0 1 1-2-3.46l.95-.55A7.7 7.7 0 0 1 4.33 13c0-.58.06-1.14.18-1.69l-.95-.55a2 2 0 1 1 2-3.46l.95.55A7 7 0 0 1 12 6.1V5a3 3 0 0 1 3-3zM12 9a4 4 0 1 0 0 8 4 4 0 0 0 0-8z");settingsLink.parentElement.appendChild(item)}
  function syncNavItemState(){const item=document.querySelector(`[${NAV_MARKER}]`);if(!item)return;const active=isRegisterRoute();["router-link-active","router-link-exact-active","border-[hsl(var(--primary)_/_0.28)]","bg-[hsl(var(--primary)_/_0.08)]","font-semibold","text-foreground","shadow-[inset_0_0_0_1px_hsl(var(--primary)_/_0.08)]"].forEach(className=>item.classList.remove(className));item.classList.toggle("router-link-active",active);item.classList.toggle("router-link-exact-active",active)}
  function bindNavigation(){document.querySelectorAll("#app-sidebar-navigation a").forEach(link=>{if(link.dataset.registerRouteWatcher)return;link.dataset.registerRouteWatcher="true";link.addEventListener("click",()=>setTimeout(sync,100))})}
  function sync(){addNavItem();bindNavigation();syncNavItemState();if(isRegisterRoute())mountPage();else unmountPage()}
  const style=document.createElement("style");style.textContent=css;document.head.appendChild(style);const observer=new MutationObserver(sync);observer.observe(document.documentElement,{childList:true,subtree:true});window.addEventListener("hashchange",()=>setTimeout(sync,50));sync();
})();

(function () {
  const MANUAL_CARD = "data-register-manual-card";
  const style = document.createElement("style");
  style.textContent = ".register-manual-row{display:flex;align-items:flex-end;gap:10px;flex-wrap:wrap}.register-manual-field{display:grid;gap:6px;min-width:180px}.register-manual-field label{font-size:12px;font-weight:600}.register-manual-field input{box-sizing:border-box;width:100%;height:36px;border:1px solid hsl(var(--border));border-radius:7px;background:hsl(var(--background));padding:0 10px;color:hsl(var(--foreground));font:inherit;font-size:13px;outline:none}.register-manual-field input:focus{border-color:hsl(var(--primary));box-shadow:0 0 0 2px hsl(var(--primary)/.14)}.register-manual-state{min-height:36px;display:flex;align-items:center;color:hsl(var(--muted-foreground));font-size:12px}";
  document.head.appendChild(style);

  function authHeaders() {
    const key = window.localStorage.getItem("chatgpt2api.adminKey") || "";
    return key ? { Authorization: `Bearer ${key}`, "Content-Type": "application/json" } : { "Content-Type": "application/json" };
  }

  async function request(path, options = {}) {
    const response = await fetch(path, { ...options, signal: options.signal || AbortSignal.timeout(15000), headers: { ...authHeaders(), ...(options.headers || {}) } });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) {
      const detail = body?.detail?.error || body?.detail || body?.error || `请求失败（${response.status}）`;
      throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
    }
    return body;
  }

  function mountManualCard() {
    const page = document.querySelector("[data-account-replenishment-page]");
    const strategy = page?.querySelector("[data-register-strategy]")?.closest(".register-card");
    if (!page || !strategy || page.querySelector(`[${MANUAL_CARD}]`)) return;
    const card = document.createElement("div");
    card.className = "register-card";
    card.setAttribute(MANUAL_CARD, "true");
    card.innerHTML = `<div class="register-card-header"><div><div class="register-card-title">手动注册</div><p class="register-card-help">指定本次注册数量，直接启动注册机，不改变自动补号阈值和目标数量。注册完成后仍会自动导入 session 并同步额度。</p></div></div><div class="register-manual-row"><div class="register-manual-field"><label for="register-manual-count">本次注册数量</label><input id="register-manual-count" data-register-manual-count type="number" min="1" max="1000" step="1" value="1"></div><button class="register-button primary" type="button" data-register-action="manual">开始手动注册</button><span class="register-manual-state" data-register-manual-state>请输入 1 到 1000 之间的数量。</span></div>`;
    strategy.parentElement.insertBefore(card, strategy);
    const button = card.querySelector('[data-register-action="manual"]');
    const input = card.querySelector("[data-register-manual-count]");
    const state = card.querySelector("[data-register-manual-state]");
    button.addEventListener("click", async () => {
      const count = Number(input.value);
      if (!Number.isInteger(count) || count < 1 || count > 1000) {
        state.textContent = "数量必须是 1 到 1000 之间的整数。";
        state.className = "register-manual-state error";
        return;
      }
      button.disabled = true;
      input.disabled = true;
      card.dataset.manualActive = "true";
      state.className = "register-manual-state";
      state.textContent = `正在注册 ${count} 个账号...`;
      const liveLog = page.querySelector("[data-register-log]");
      if (liveLog) liveLog.textContent += `\n正在提交手动注册请求：${count} 个账号...`;
      try {
        const result = await request(`/api/accounts/replenishment/run?manual_count=${count}`, { method: "POST", body: "{}" });
        if (result.reason === "started") {
          state.textContent = `已启动 ${result.requested_count || count} 个账号的手动注册，日志正在实时更新。`;
        } else if (result.reason === "failed") {
          card.dataset.manualActive = "false";
          state.className = "register-manual-state error";
          state.textContent = `注册失败：${result.stderr_tail || result.error || "请查看右侧日志"}`;
        } else if (result.reason === "replenished") {
          card.dataset.manualActive = "false";
          state.textContent = `注册完成：请求 ${result.requested_count || count} 个，导入 ${result.import?.imported || 0} 个。`;
        } else if (result.reason === "busy") {
          card.dataset.manualActive = "false";
          state.textContent = "已有注册任务执行中，请查看右侧实时日志。";
        } else {
          card.dataset.manualActive = "false";
          state.textContent = `手动注册结果：${result.reason || "未知状态"}，请查看右侧日志。`;
        }
      } catch (error) {
        card.dataset.manualActive = "false";
        state.className = "register-manual-state error";
        state.textContent = error.message || "手动注册启动失败";
      } finally {
        window.dispatchEvent(new Event("register-status-refresh"));
        button.disabled = false;
        input.disabled = false;
      }
    });
  }

  const observer = new MutationObserver(mountManualCard);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("hashchange", () => setTimeout(mountManualCard, 80));
  mountManualCard();
})();

(function () {
  const ADMIN_KEY = "chatgpt2api.adminKey";
  function isRegisterRoute() { return window.location.hash.includes("/settings") && window.location.hash.includes("tab=replenishment"); }
  function authHeaders() {
    const key = window.localStorage.getItem(ADMIN_KEY) || "";
    return key ? { Authorization: `Bearer ${key}` } : {};
  }
  function formatLogTimestamp(value) {
    const raw = String(value ?? "").trim();
    if (!/^\d{4}-\d{2}-\d{2}[ T]\d{2}:\d{2}:\d{2}/.test(raw)) return raw;
    const date = new Date(`${raw.slice(0, 19).replace(" ", "T")}Z`);
    if (Number.isNaN(date.getTime())) return raw;
    date.setUTCHours(date.getUTCHours() + 8);
    return date.toISOString().slice(0, 19).replace("T", " ");
  }
  function formatLiveLog(status) {
    const history = Array.isArray(status?.log_history) ? status.log_history : [];
    const render = (entry) => {
      const result = entry || {};
      const metrics = result.post_metrics || result.metrics || {};
      const lines = [];
      if (result.finished_at) lines.push(`[${formatLogTimestamp(result.finished_at)}]`);
      if (result.batch_id) lines.push(`批次：${result.batch_id}`);
      if (result.reason) lines.push(`结果：${result.reason}`);
      if (result.triggered !== undefined) lines.push(`是否触发：${result.triggered ? "是" : "否"}`);
      if (metrics.current_available !== undefined) lines.push(`账号池：当前可用 ${metrics.current_available}，待确认 ${metrics.pending_confirmation_count ?? metrics.unconfirmed_available ?? 0}，预计可用 ${metrics.estimated_available ?? 0}`);
      if (result.import) lines.push(`导入：${result.import.imported || 0}，跳过：${result.import.skipped || 0}，代理继承：${result.import.proxy_assigned ? "已设置" : "未设置"}`);
      if (result.quota_refresh) lines.push(`额度同步：成功 ${result.quota_refresh.synced || 0}，失败 ${result.quota_refresh.errors || 0}，尝试 ${result.quota_refresh.attempts || 0} 次`);
      if (result.quota_refresh?.error) lines.push(`额度同步错误：${result.quota_refresh.error}`);
      if (result.config_error) lines.push(`配置提示：${result.config_error}`);
      if (result.log_path) lines.push(`日志文件：${result.log_path}`);
      if (result.stdout_tail) lines.push(`\n--- 注册机输出 ---\n${result.stdout_tail}`);
      if (result.stderr_tail) lines.push(`\n--- 错误输出 ---\n${result.stderr_tail}`);
      if (result.error) lines.push(`错误：${result.error}`);
      return lines.join("\n");
    };
    const sections = history.map(render).filter(Boolean);
    const result = status?.last_result || {};
    if (status?.running && ["starting", "running"].includes(result.reason)) sections.push(render({ ...result, finished_at: "进行中" }));
    if (!sections.length) sections.push(render({ finished_at: status?.last_checked_at, reason: result.reason || "状态检查", metrics: status?.current_metrics || {} }));
    if (status?.last_error) sections.push(`错误：${status.last_error}`);
    return sections.join("\n\n==============================\n\n") || "暂无运行记录\n\n保存配置或执行一次补号后，这里会显示注册机输出和 session 导入结果。";
  }
  let polling = false;
  async function poll() {
    if (polling) return;
    const page = document.querySelector("[data-account-replenishment-page]");
    if (!page || !isRegisterRoute()) return;
    polling = true;
    try {
      const response = await fetch("/api/accounts/replenishment", { headers: authHeaders(), signal: AbortSignal.timeout(8000) });
      if (!response.ok) throw new Error(`状态读取失败（HTTP ${response.status}），将自动重试`);
      const status = await response.json();
      const log = page.querySelector("[data-register-log]");
      if (log) {
        const previousTop = log.scrollTop;
        const followBottom = log.scrollHeight - previousTop - log.clientHeight < 48;
        const nextText = formatLiveLog(status);
        if (log.textContent !== nextText) {
          log.textContent = nextText;
          log.scrollTop = followBottom ? log.scrollHeight : previousTop;
        }
      }
      const metrics = status?.current_metrics || {};
      const available = page.querySelector('[data-register-metric="available"]');
      if (available && metrics.current_available !== undefined) available.textContent = metrics.current_available;
      const pending = page.querySelector('[data-register-metric="pending"]');
      if (pending && metrics.current_available !== undefined) {
        const pendingCount = metrics.pending_confirmation_count ?? metrics.unconfirmed_available ?? 0;
        pending.textContent = pendingCount;
        pending.className = `register-metric-value ${pendingCount > 0 ? "warning" : "success"}`;
      }
      const quota = page.querySelector('[data-register-metric="quota"]');
      if (quota && metrics.current_quota !== undefined) quota.textContent = metrics.current_quota;
      const running = !!status.running;
      const state = page.querySelector('[data-register-metric="state"]');
      if (state) { state.textContent = running ? "执行中" : "待命"; state.className = `register-metric-value ${running ? "warning" : "success"}`; }
      const logStatus = page.querySelector("[data-register-log-status]");
      if (logStatus) logStatus.textContent = running ? "注册机执行中" : "已连接";
      const dot = page.querySelector("[data-register-log-dot]");
      if (dot) dot.className = `register-log-dot ${running ? "running" : "success"}`;
      const manualCard = page.querySelector("[data-register-manual-card]");
      const manualState = manualCard?.querySelector("[data-register-manual-state]");
      if (manualCard?.dataset.manualActive === "true" && manualState) {
        const result = status?.last_result || {};
        if (running) {
          manualState.className = "register-manual-state";
          manualState.textContent = "正在注册，日志会实时显示...";
        } else if (result.ok === false || result.reason === "failed" || status?.last_error) {
          manualCard.dataset.manualActive = "false";
          manualState.className = "register-manual-state error";
          manualState.textContent = `注册失败：${result.stderr_tail || status.last_error || "请查看右侧日志"}`;
        } else if (result.reason === "replenished") {
          manualState.className = "register-manual-state";
          manualState.textContent = `注册完成：导入 ${result.import?.imported || 0} 个账号。`;
          manualCard.dataset.manualActive = "false";
        }
      }
    } catch (error) {
      const label = page.querySelector("[data-register-log-status]");
      if (label) label.textContent = error.message || "连接暂时中断，保留现有日志并自动重试";
      const dot = page.querySelector("[data-register-log-dot]");
      if (dot) dot.className = "register-log-dot error";
    } finally {
      polling = false;
    }
  }
  window.addEventListener("register-status-refresh", poll);
  setInterval(poll, 3000);
  poll();
})();

(function () {
  function isRegisterRoute() { return window.location.hash.includes("/settings") && window.location.hash.includes("tab=replenishment"); }
  function hideLegacyPanel() {
    if (isRegisterRoute()) return;
    const settingsPanel = document.querySelector(".settings-page-panel");
    if (!settingsPanel) return;
    const trigger = [...settingsPanel.querySelectorAll("button")].find((node) => node.textContent.trim() === "立即补号");
    if (!trigger) return;
    const candidate = trigger.closest("div.space-y-4");
    const text = candidate?.textContent || "";
    if (candidate && text.includes("账号自动补充") && text.includes("刷新状态") && text.includes("强制执行")) {
      candidate.remove();
    }
  }
  const observer = new MutationObserver(hideLegacyPanel);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("hashchange", () => setTimeout(hideLegacyPanel, 80));
  hideLegacyPanel();
})();

(function () {
  const PROVIDER_MARKER = "data-register-provider-panel";
  const sources = [
    ["configured", "按注册工具默认配置"],
    ["remail_target", "ReMail 长效邮箱"],
    ["mailbox_file", "Outlook / Hotmail / 邮箱池文件"],
    ["cfworker", "CFWorker 域名邮箱"],
    ["smailr", "Smailr 邮箱"],
    ["phone", "手机号注册"],
  ];
  const authHeaders = () => {
    const key = window.localStorage.getItem("chatgpt2api.adminKey") || "";
    return key ? { Authorization: `Bearer ${key}` } : {};
  };
  async function providerRequest(path, options = {}) {
    const response = await fetch(path, { ...options, headers: { ...authHeaders(), "Content-Type": "application/json", ...(options.headers || {}) } });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(body?.detail?.error || body?.detail || body?.error || `请求失败（${response.status}）`);
    return body;
  }
  const isRoute = () => window.location.hash.includes("/settings") && window.location.hash.includes("tab=replenishment");
  const input = (label, key, help, type = "text", wide = false) => `<div class="register-field${wide ? " wide" : ""}"><label for="register-${key}">${label}</label><input id="register-${key}" data-register-key="${key}" type="${type}"><small>${help}</small></div>`;
  const select = (label, key, options, help, wide = false) => `<div class="register-field${wide ? " wide" : ""}"><label for="register-${key}">${label}</label><select id="register-${key}" data-register-key="${key}">${options.map(([value, text]) => `<option value="${value}">${text}</option>`).join("")}</select><small>${help}</small></div>`;
  const providerInput = (label, key, help, type = "text", wide = false) => `<div class="register-field${wide ? " wide" : ""}"><label for="provider-${key}">${label}</label><input id="provider-${key}" data-provider-key="${key}" type="${type}"><small data-provider-help="${key}">${help}</small></div>`;
  const providerTextarea = (label, key, help) => `<div class="register-field wide"><label for="provider-${key}">${label}</label><textarea id="provider-${key}" data-provider-key="${key}" rows="3" style="box-sizing:border-box;width:100%;border:1px solid hsl(var(--border));border-radius:7px;background:hsl(var(--background));padding:9px 10px;color:hsl(var(--foreground));font:inherit;font-size:13px;resize:vertical"></textarea><small data-provider-help="${key}">${help}</small></div>`;
  const secretProviderKeys = new Set(["remail_api_key", "smailr_api_key", "cfworker_admin_token", "cfworker_api_token", "smsbower_api_key"]);
  function bindSecretField(field) {
    if (!field || field.dataset.secretBound === "true") return;
    field.dataset.secretBound = "true";
    field.addEventListener("focus", () => {
      if (field.dataset.maskedValue && field.value === field.dataset.maskedValue) {
        field.value = "";
        field.type = "password";
      }
    });
    field.addEventListener("blur", () => {
      if (!field.value.trim() && field.dataset.maskedValue) {
        field.value = field.dataset.maskedValue;
        field.type = "text";
      }
    });
  }
  function showMaskedSecret(field, masked) {
    if (!field) return;
    field.dataset.maskedValue = masked || "";
    field.value = masked || "";
    field.type = masked ? "text" : "password";
    bindSecretField(field);
  }
  function value(page, key, fallback = "") {
    return page.querySelector(`[data-register-key="${key}"]`)?.value || fallback;
  }
  function setValue(page, key, next) {
    const node = page.querySelector(`[data-register-key="${key}"]`);
    if (node && next !== undefined && next !== null) node.value = String(next);
  }
  function updateVisibility(page) {
    const source = value(page, "registration_source", "configured");
    page.querySelectorAll("[data-register-provider-only]").forEach((node) => {
      node.hidden = !node.dataset.registerProviderOnly.split(",").includes(source);
    });
    const mailbox = page.querySelector('[data-register-key="mailbox_file"]');
    if (mailbox) mailbox.required = source === "mailbox_file";
  }
  async function hydrate(page) {
    if (page.dataset.registerProviderHydrated === "true") return;
    page.dataset.registerProviderHydrated = "loading";
    try {
      const response = await fetch("/api/settings", { headers: authHeaders() });
      const body = await response.json();
      const config = body?.settings?.account_replenishment || {};
      Object.entries(config).forEach(([key, item]) => setValue(page, key, item));
    } catch (_) {
      // The main registration page renders its own API error state.
    } finally {
      page.dataset.registerProviderHydrated = "true";
      updateVisibility(page);
    }
  }
  function createPanel(page) {
    if (page.querySelector(`[${PROVIDER_MARKER}]`)) return;
    const runtime = page.querySelector("[data-register-runtime]")?.closest(".register-card");
    if (!runtime) return;
    const card = document.createElement("div");
    card.className = "register-card";
    card.setAttribute(PROVIDER_MARKER, "true");
    card.innerHTML = `<div class="register-card-header"><div><div class="register-card-title">注册来源与邮箱</div><p class="register-card-help">选择注册机实际使用的邮箱来源。ReMail 会自动购买邮箱；Outlook / Hotmail 请选择统一邮箱池文件。</p></div></div><div class="register-form-grid">${select("注册来源", "registration_source", sources, "来源会转换为注册工具对应的 CLI 参数。", false)}${input("邮箱池文件", "mailbox_file", "支持 Graph、Outlook / Hotmail、Gmail、iCloud 等统一格式；选择邮箱池文件时必填。", "text", true)}<div class="register-field" data-register-provider-only="remail_target"><label for="register-remail_service_mode">ReMail 服务模式</label><select id="register-remail_service_mode" data-register-key="remail_service_mode"><option value="purchase">purchase（长效邮箱）</option><option value="code">code（验证码模式）</option></select><small>默认使用 purchase，适合持续补充账号池。</small></div><div class="register-field" data-register-provider-only="remail_target"><label for="register-remail_supply">ReMail 供应策略</label><select id="register-remail_supply" data-register-key="remail_supply"><option value="private_first">private_first</option><option value="public_only">public_only</option></select><small>沿用注册工具中的邮箱库存策略。</small></div>${input("ReMail 邮箱后缀", "remail_email_suffix", "例如 outlook.com。", "text", false)}${input("ReMail 项目 ID", "remail_project_id", "对应注册工具配置中的 project_id。", "number", false)}${input("CFWorker 域名", "cfworker_domain", "选择 CFWorker 时使用；留空则由注册工具配置决定。", "text", false)}${input("Smailr 域名", "smailr_domain", "选择 Smailr 时使用；留空则由注册工具配置决定。", "text", false)}</div>`;
    const mailboxUpload = document.createElement("div");
    mailboxUpload.className = "register-field wide";
    mailboxUpload.setAttribute("data-register-provider-only", "mailbox_file");
    mailboxUpload.innerHTML = `<label for="register-mailbox-upload">导入邮箱池文件</label><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><input id="register-mailbox-upload" type="file" accept=".txt,.json,.csv" style="height:36px;flex:1;min-width:220px"><button type="button" class="register-button" data-mailbox-upload-action>上传并使用</button></div><small data-mailbox-upload-state>支持 Outlook / Hotmail、Graph、Gmail、iCloud 等统一格式，上传后自动填入邮箱池文件路径。</small>`;
    card.querySelector(".register-form-grid").appendChild(mailboxUpload);
    const mailboxUploadInput = mailboxUpload.querySelector("#register-mailbox-upload");
    const mailboxUploadState = mailboxUpload.querySelector("[data-mailbox-upload-state]");
    mailboxUpload.querySelector("[data-mailbox-upload-action]").addEventListener("click", async () => {
      const file = mailboxUploadInput.files?.[0];
      if (!file) { mailboxUploadState.textContent = "请先选择邮箱池文件"; return; }
      mailboxUploadState.textContent = "上传中...";
      try {
        const result = await providerRequest("/api/accounts/replenishment/mailbox-file", { method: "POST", body: JSON.stringify({ filename: file.name, content: await file.text() }) });
        setValue(page, "mailbox_file", result.mailbox_file);
        mailboxUploadState.textContent = `已导入：${result.filename}`;
      } catch (error) {
        mailboxUploadState.textContent = error.message || "邮箱池文件导入失败";
      }
    });
    const providerFields = {
      mailbox_file: "mailbox_file",
      remail_service_mode: "remail_target",
      remail_supply: "remail_target",
      remail_email_suffix: "remail_target",
      remail_project_id: "remail_target",
      cfworker_domain: "cfworker",
      smailr_domain: "smailr",
    };
    Object.entries(providerFields).forEach(([key, provider]) => {
      const fieldNode = card.querySelector(`[data-register-key="${key}"]`)?.closest(".register-field");
      if (fieldNode) fieldNode.setAttribute("data-register-provider-only", provider);
    });
    const credentials = document.createElement("div");
    credentials.className = "register-card";
    credentials.setAttribute("data-register-provider-credentials", "true");
    credentials.innerHTML = `<div class="register-card-header"><div><div class="register-card-title">邮箱 / 短信服务配置</div><p class="register-card-help">密钥直接在这里配置并写入注册工具。已配置的密钥不会回显；注册代理地址和代理池支持清空。</p></div></div><div class="register-form-grid">${providerInput("ReMail API Key", "remail_api_key", "ReMail 服务密钥。", "password")} ${providerInput("ReMail 服务地址", "remail_base_url", "默认使用注册工具配置的地址。", "text")} ${providerInput("Smailr API Key", "smailr_api_key", "Smailr 服务密钥。", "password")} ${providerInput("Smailr 服务地址", "smailr_base_url", "默认 https://smailr.com。", "text")} ${providerInput("CFWorker 地址", "cfworker_url", "CFWorker 邮箱服务地址。", "text")} ${providerInput("CFWorker Admin Token", "cfworker_admin_token", "管理接口令牌。", "password")} ${providerInput("CFWorker API Token", "cfworker_api_token", "邮箱接口令牌。", "password")} ${providerInput("SMSBower API Key", "smsbower_api_key", "手机号注册服务密钥。", "password")} ${providerInput("SMSBower 国家代码", "smsbower_country", "例如 38。", "text")} ${providerInput("注册代理地址", "registration_proxy", "固定代理优先级最高；留空后将使用注册代理池。", "text")} ${providerTextarea("注册代理池", "registration_proxy_pool", "每行一个代理，也支持逗号分隔；固定代理地址为空时按池轮换。")}</div><div class="register-save-row"><span class="register-save-state" data-provider-save-state>配置读取中...</span><div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap"><button class="register-button" data-provider-action="test-proxy">测试注册代理</button><button class="register-button primary" data-provider-action="save">保存服务配置</button></div></div><div class="register-save-state" data-proxy-test-state></div>`;
    const credentialFields = {
      remail_api_key: "remail_target",
      remail_base_url: "remail_target",
      smailr_api_key: "smailr",
      smailr_base_url: "smailr",
      cfworker_url: "cfworker",
      cfworker_admin_token: "cfworker",
      cfworker_api_token: "cfworker",
      smsbower_api_key: "phone",
      smsbower_country: "phone",
    };
    credentials.querySelector('[data-provider-action="save"]').textContent = "保存邮箱与代理配置";
    credentials.querySelector(".register-card-help").textContent = "只保存邮箱/短信服务地址、密钥、国家代码和注册代理。补号策略与运行参数请使用上方按钮保存。密钥只显示掩码。";
    credentials.querySelectorAll("[data-provider-key]").forEach((field) => {
      if (secretProviderKeys.has(field.dataset.providerKey)) bindSecretField(field);
    });
    Object.entries(credentialFields).forEach(([key, provider]) => {
      const fieldNode = credentials.querySelector(`[data-provider-key="${key}"]`)?.closest(".register-field");
      if (fieldNode) fieldNode.setAttribute("data-register-provider-only", provider);
    });
    const providerSaveState = credentials.querySelector("[data-provider-save-state]");
    async function loadCredentials() {
      try {
        const values = await providerRequest("/api/accounts/replenishment/provider-config");
        Object.entries(values).forEach(([key, value]) => {
          const field = credentials.querySelector(`[data-provider-key="${key}"]`);
          if (field && value !== undefined && value !== null && !key.includes("api_key") && !key.includes("token")) field.value = String(value);
        });
        const configured = {
          remail_api_key: [values.remail_has_api_key, values.remail_api_key_masked],
          smailr_api_key: [values.smailr_has_api_key, values.smailr_api_key_masked],
          cfworker_admin_token: [values.cfworker_has_admin_token, values.cfworker_admin_token_masked],
          cfworker_api_token: [values.cfworker_has_api_token, values.cfworker_api_token_masked],
          smsbower_api_key: [values.smsbower_has_api_key, values.smsbower_api_key_masked],
        };
        Object.entries(configured).forEach(([key, [hasValue, masked]]) => {
          const field = credentials.querySelector(`[data-provider-key="${key}"]`);
          const help = credentials.querySelector(`[data-provider-help="${key}"]`);
          if (field && hasValue) { showMaskedSecret(field, masked || "已配置"); field.placeholder = "已配置，点击后输入新值"; }
          if (help && hasValue) help.textContent = "已配置；留空保持现有密钥，填写新值可替换。";
        });
        providerSaveState.textContent = "服务配置已加载";
      } catch (error) {
        providerSaveState.textContent = error.message || "服务配置加载失败";
        providerSaveState.className = "register-save-state error";
      }
    }
    async function saveCredentials() {
      const button = credentials.querySelector('[data-provider-action="save"]');
      const payload = {};
      credentials.querySelectorAll("[data-provider-key]").forEach((field) => {
        const value = field.value.trim();
        const key = field.dataset.providerKey;
        if (key === "registration_proxy" || key === "registration_proxy_pool") {
          payload[key] = value;
        } else if (value && value !== field.dataset.maskedValue) {
          payload[key] = value;
        }
      });
      button.disabled = true;
      providerSaveState.className = "register-save-state";
      providerSaveState.textContent = "保存中...";
      try {
        const values = await providerRequest("/api/accounts/replenishment/provider-config", { method: "PATCH", body: JSON.stringify(payload) });
        providerSaveState.textContent = "服务配置已保存";
        Object.entries({
          remail_api_key: [values.remail_has_api_key, values.remail_api_key_masked],
          smailr_api_key: [values.smailr_has_api_key, values.smailr_api_key_masked],
          cfworker_admin_token: [values.cfworker_has_admin_token, values.cfworker_admin_token_masked],
          cfworker_api_token: [values.cfworker_has_api_token, values.cfworker_api_token_masked],
          smsbower_api_key: [values.smsbower_has_api_key, values.smsbower_api_key_masked],
        }).forEach(([key, [hasValue, masked]]) => {
          const field = credentials.querySelector(`[data-provider-key="${key}"]`);
          if (field && hasValue) { showMaskedSecret(field, masked || "已配置"); field.placeholder = "已配置，点击后输入新值"; }
        });
      } catch (error) {
        providerSaveState.className = "register-save-state error";
        providerSaveState.textContent = error.message || "服务配置保存失败";
      } finally {
        button.disabled = false;
      }
    }
    async function testRegistrationProxy() {
      const button = credentials.querySelector('[data-provider-action="test-proxy"]');
      const state = credentials.querySelector("[data-proxy-test-state]");
      const payload = {
        registration_proxy: credentials.querySelector('[data-provider-key="registration_proxy"]')?.value.trim() || "",
        registration_proxy_pool: credentials.querySelector('[data-provider-key="registration_proxy_pool"]')?.value.trim() || "",
      };
      button.disabled = true;
      state.className = "register-save-state";
      state.textContent = "测试中，请等待网络请求完成...";
      try {
        const result = await providerRequest("/api/accounts/replenishment/proxy-test", { method: "POST", body: JSON.stringify(payload) });
        const lines = (result.results || []).map((item) => `${item.label}：${item.ok ? "成功" : "失败"}${item.status ? `，HTTP ${item.status}` : ""}${item.latency_ms ? `，${item.latency_ms} ms` : ""}${item.error ? `，${item.error}` : ""}`);
        state.className = `register-save-state${result.ok ? "" : " error"}`;
        state.textContent = result.tested ? lines.join("；") : (result.error || "没有可测试的代理");
      } catch (error) {
        state.className = "register-save-state error";
        state.textContent = error.message || "代理测试失败";
      } finally {
        button.disabled = false;
      }
    }
    runtime.before(card);
    runtime.before(credentials);
    const source = card.querySelector('[data-register-key="registration_source"]');
    source.addEventListener("change", () => updateVisibility(page));
    credentials.querySelector('[data-provider-action="save"]').addEventListener("click", saveCredentials);
    credentials.querySelector('[data-provider-action="test-proxy"]').addEventListener("click", testRegistrationProxy);
    hydrate(page);
    loadCredentials();
    updateVisibility(page);
  }
  function sync() {
    const page = document.querySelector("[data-account-replenishment-page]");
    if (page && isRoute()) createPanel(page);
  }
  const observer = new MutationObserver(sync);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("hashchange", () => setTimeout(sync, 50));
  sync();
})();

(function () {
  const TITLE_MARKER = "data-register-title-original";
  const settingsNavStyle = document.createElement("style");
  settingsNavStyle.textContent = ".register-page{max-width:1680px}.register-layout{grid-template-columns:minmax(0,1fr) minmax(460px,1fr)}.register-field select{box-sizing:border-box;width:100%;height:36px;border:1px solid hsl(var(--border));border-radius:7px;background:hsl(var(--background));padding:0 10px;color:hsl(var(--foreground));font:inherit;font-size:13px;outline:none}.register-field select:focus{border-color:hsl(var(--primary));box-shadow:0 0 0 2px hsl(var(--primary)/.14)}.register-route-active #app-sidebar-navigation a[data-register-settings-link]{border-color:transparent!important;background:transparent!important;color:hsl(var(--muted-foreground))!important;font-weight:400!important;box-shadow:none!important}.register-route-active #app-sidebar-navigation a[data-account-replenishment-nav]{border-color:hsl(var(--primary)/.28)!important;background:hsl(var(--primary)/.08)!important;color:hsl(var(--foreground))!important;font-weight:600!important;box-shadow:inset 0 0 0 1px hsl(var(--primary)/.08)!important}body:not(.register-route-active) #app-sidebar-navigation a[data-account-replenishment-nav]{border-color:transparent!important;background:transparent!important;color:hsl(var(--muted-foreground))!important;font-weight:400!important;box-shadow:none!important}@media (max-width:1000px){.register-layout{grid-template-columns:1fr}}";
  document.head.appendChild(settingsNavStyle);
  function isRegisterRoute() {
    return window.location.hash.includes("/settings") && window.location.hash.includes("tab=replenishment");
  }
  function syncShellTitle() {
    const title = document.querySelector(".shell-header p.truncate");
    const settingsLink = [...document.querySelectorAll("#app-sidebar-navigation a")].find((link) => /#\/settings$/.test(link.getAttribute("href") || ""));
    if (settingsLink) settingsLink.setAttribute("data-register-settings-link", "true");
    document.body.classList.toggle("register-route-active", isRegisterRoute());
    if (!title) return;
    if (isRegisterRoute()) {
      if (!title.hasAttribute(TITLE_MARKER)) title.setAttribute(TITLE_MARKER, title.textContent || "系统设置");
      if (title.textContent !== "注册机") title.textContent = "注册机";
    } else if (title.hasAttribute(TITLE_MARKER)) {
      const original = title.getAttribute(TITLE_MARKER) || "系统设置";
      if (title.textContent !== original) title.textContent = original;
      title.removeAttribute(TITLE_MARKER);
    }
  }
  const titleObserver = new MutationObserver(syncShellTitle);
  titleObserver.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("hashchange", () => setTimeout(syncShellTitle, 50));
  document.addEventListener("click", (event) => { if (event.target.closest("#app-sidebar-navigation a")) setTimeout(syncShellTitle, 100); }, true);
  syncShellTitle();
})();

(function () {
  const layoutStyle = document.createElement("style");
  layoutStyle.textContent = `.register-page{height:calc(100vh - 72px);box-sizing:border-box;overflow:hidden}.register-layout{height:calc(100% - 112px);min-height:0}.register-stack{min-height:0;overflow-y:auto;padding-right:4px}.register-log-card{height:100%;min-height:0;overflow:hidden}.register-log{min-height:0;max-height:none;overflow:auto}@media (max-width:1000px){.register-page{height:auto;overflow:visible}.register-layout{height:auto}.register-stack{overflow:visible}.register-log-card{height:420px;min-height:420px}}`;
  document.head.appendChild(layoutStyle);

  const ADMIN_KEY = "chatgpt2api.adminKey";
  const STRATEGY_KEYS = ["minimum_available", "target_available", "interval_seconds", "cooldown_seconds", "max_batch_size"];
  const authHeaders = () => {
    const key = window.localStorage.getItem(ADMIN_KEY) || "";
    return key ? { Authorization: `Bearer ${key}`, "Content-Type": "application/json" } : { "Content-Type": "application/json" };
  };
  const isRoute = () => window.location.hash.includes("/settings") && window.location.hash.includes("tab=replenishment");
  async function saveStrategy(page, button, state) {
    button.disabled = true;
    state.className = "register-save-state";
    state.textContent = "保存中...";
    try {
      const settingsResponse = await fetch("/api/settings", { headers: authHeaders() });
      const settings = await settingsResponse.json();
      if (!settingsResponse.ok) throw new Error(settings?.detail?.error || settings?.detail || `读取配置失败（${settingsResponse.status}）`);
      const config = { ...(settings.settings?.account_replenishment || {}) };
      STRATEGY_KEYS.forEach((key) => {
        const field = page.querySelector(`[data-register-key="${key}"]`);
        config[key] = Number(field?.value || 0);
      });
      config.enabled = !!page.querySelector("[data-register-enabled]")?.checked;
      const response = await fetch("/api/settings", {
        method: "PATCH",
        headers: authHeaders(),
        body: JSON.stringify({ revision: settings.revision, account_replenishment: config }),
      });
      const body = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(body?.detail?.error || body?.detail || `保存失败（${response.status}）`);
      state.textContent = config.enabled ? "自动补充已启用并保存" : "自动补充已停用并保存";
      page.dataset.registerRevision = body.revision || settings.revision || "";
    } catch (error) {
      state.className = "register-save-state error";
      state.textContent = error.message || "补号策略保存失败";
    } finally {
      button.disabled = false;
    }
  }
  function mountStrategySave() {
    const page = document.querySelector("[data-account-replenishment-page]");
    if (!page || !isRoute()) return;
    const strategy = page.querySelector("[data-register-strategy]")?.closest(".register-card");
    if (!strategy || strategy.dataset.strategySaveMounted === "true") return;
    strategy.dataset.strategySaveMounted = "true";
    const header = strategy.querySelector(".register-card-header");
    const toggle = strategy.querySelector(".register-toggle");
    const actions = document.createElement("div");
    actions.className = "register-strategy-actions";
    if (toggle) actions.appendChild(toggle);
    const saveButton = document.createElement("button");
    saveButton.type = "button";
    saveButton.className = "register-button primary";
    saveButton.dataset.registerAction = "save-strategy";
    saveButton.textContent = "保存补号策略";
    actions.appendChild(saveButton);
    if (header) header.appendChild(actions);
    const row = document.createElement("div");
    row.className = "register-save-row";
    row.innerHTML = `<span class="register-save-state" data-register-strategy-save-state>修改策略或开关后，请点击此按钮保存</span>`;
    strategy.appendChild(row);
    const state = row.querySelector("[data-register-strategy-save-state]");
    saveButton.addEventListener("click", () => saveStrategy(page, saveButton, state));
    page.querySelector("[data-register-enabled]")?.addEventListener("change", () => {
      state.className = "register-save-state";
      state.textContent = "开关已修改，请点击保存补号策略";
    });
    const runtimeButton = page.querySelector('[data-register-action="save"]');
    if (runtimeButton) {
      runtimeButton.textContent = "保存注册机运行参数";
      const runtimeHelp = runtimeButton.closest(".register-card")?.querySelector(".register-card-help");
      if (runtimeHelp) runtimeHelp.textContent = "只保存注册机目录、入口脚本、Python 和运行参数；补号阈值请使用上方按钮保存。";
    }
  }
  const observer = new MutationObserver(mountStrategySave);
  observer.observe(document.documentElement, { childList: true, subtree: true });
  window.addEventListener("hashchange", () => setTimeout(mountStrategySave, 80));
  mountStrategySave();
})();
