import{d as I,o as _,c as K,a as o,t as k,p as H,D as $,f as i,b as l,e as d,O as g,g as h,l as E,r as A,G as m}from"./index-BhEm-7EJ.js?v=20260921-account-import-render-v8";import{_ as y}from"./_plugin-vue_export-helper-DlAUqK2U.js?v=20260921-account-import-render-v8";import{C as G}from"./ConsoleSegmentedTabs-ByZehyp7.js?v=20260921-account-import-render-v8";import{M as U}from"./ModalBody-DbDsAuvM.js?v=20260921-account-import-render-v8";import{M as L,_ as Y}from"./useConfirmDialog-BPwqO30J.js?v=20260921-account-import-render-v8";import{M as z}from"./ModalHeader-DOAbsAT0.js?v=20260921-account-import-render-v8";import{u as R}from"./usePublicRuntimeConfig-B2ANqaLQ.js?v=20260921-account-import-render-v8";import"./settings-CkHUE9aB.js?v=20260921-account-import-render-v8";const x={class:"code-block scrollbar-slim"},M=I({__name:"CodeBlock",props:{content:{}},setup(e){return(t,P)=>(_(),K("pre",x,[o("code",null,k(e.content),1)]))}}),O=y(M,[["__scopeId","data-v-7b183f75"]]);function V(e){return String(e||"").trim().replace(/\/+$/,"")}function v(e){return`${V(e)}/v1/search`}function q(e){const t=v(e.baseUrl);return e.language==="en"?`---
name: chatgpt2api-search
description: Use when current web search is needed through this chatgpt2api server. Call the configured HTTP search endpoint with a prompt and return the answer with source URLs.
---

# ChatGPT2API Search

Use this skill when the user asks for current web search, online lookup, recent information, or source-backed answers.

## Request

POST ${t}

Headers:

Authorization: Bearer \${CHATGPT2API_API_KEY}
Content-Type: application/json

JSON body:

{
  "prompt": "<search question>"
}

## Response handling

- Use \`answer\` as the main response.
- Include source URLs from \`sources\` when available.
- If the endpoint returns an error, summarize the error and ask whether to retry.

## Authentication

- Read the API key from the \`CHATGPT2API_API_KEY\` environment variable at request time.
- Never write the real API key into this skill file, prompts, logs, or source control.
- If the environment variable is missing, stop and ask the user to configure it before sending a request.

Set it before starting the client that runs this skill (replace \`<your-api-key>\`):

- PowerShell: \`$env:CHATGPT2API_API_KEY = '<your-api-key>'\`
- Bash / zsh: \`export CHATGPT2API_API_KEY='<your-api-key>'\``:`---
name: chatgpt2api-search
description: 当用户需要联网搜索、查询最新信息、核实事实或需要来源链接时，调用本地 chatgpt2api 搜索接口。
---

# ChatGPT2API 搜索

当用户要求联网搜索、查询最新信息、核实资料、查新闻、查价格、查文档更新或需要来源链接时，使用这个 skill。

## 接口

POST ${t}

Headers:

Authorization: Bearer \${CHATGPT2API_API_KEY}
Content-Type: application/json

Body:

{
  "prompt": "<用户要搜索的问题>"
}

## 返回处理

- 使用接口返回的 \`answer\` 作为主要回答。
- 如果有 \`sources\`，在回答里附上来源链接。
- 如果接口报错，简要说明错误并询问是否重试。

## 鉴权

- 发起请求时从 \`CHATGPT2API_API_KEY\` 环境变量读取 API Key。
- 不要把真实 API Key 写入本 Skill、提示词、日志或版本控制。
- 如果环境变量未设置，停止请求并提示用户先完成配置。

启动运行此 Skill 的客户端之前先设置环境变量（将 \`<your-api-key>\` 替换为实际密钥）：

- PowerShell：\`$env:CHATGPT2API_API_KEY = '<your-api-key>'\`
- Bash / zsh：\`export CHATGPT2API_API_KEY='<your-api-key>'\``}function b(e){const t=q(e);return e.language==="en"?`Please install a local web-search skill on this machine.

Requirements:
1. Install this as a local skill according to the current environment's skill rules.
2. Skill name: chatgpt2api-search
3. File name: SKILL.md
4. Only create or update this skill file.
5. Do not write a real API key into the skill. It must read \`CHATGPT2API_API_KEY\` from the environment at runtime.

SKILL.md content:

\`\`\`markdown
${t}
\`\`\``:`请帮我在本机安装一个用于联网搜索的 skill。

要求：
1. 按当前环境的 skill 安装规范，把它安装成本地 skill。
2. skill 名称为：chatgpt2api-search
3. 文件名为：SKILL.md
4. 只创建或更新这个 skill 文件，不要修改其他无关文件。
5. 不要把真实 API Key 写入 skill，运行时必须从环境变量 \`CHATGPT2API_API_KEY\` 读取。

SKILL.md 内容：

\`\`\`markdown
${t}
\`\`\``}async function N(e){return await e.loadRuntimeConfig(),b({baseUrl:e.getBaseUrl(),language:e.language})}const D={class:"search-skill-meta"},j={class:"search-skill-auth"},F={class:"search-skill-code"},J={class:"search-skill-warning"},Q=I({__name:"StudioSearchSkillModal",props:{open:{type:Boolean}},emits:["close","copy"],setup(e,{emit:t}){const P=e,c=t,s=A("zh"),n=A(!1),S=[{label:"中文",value:"zh"},{label:"English",value:"en"}],{apiBaseUrl:u,loadPublicRuntimeConfig:f}=R(),T=m(()=>v(u.value)),C=m(()=>b({baseUrl:u.value,language:s.value})),w=m(()=>n.value?s.value==="zh"?"正在读取接口":"Loading endpoint":s.value==="zh"?"复制安装指令":"Copy install prompt");async function B(){if(!n.value){n.value=!0;try{const r=await N({language:s.value,getBaseUrl:()=>u.value,loadRuntimeConfig:f});c("copy",r)}finally{n.value=!1}}}return H(()=>P.open,r=>{r&&f()},{immediate:!0}),(r,a)=>(_(),$(Y,{open:e.open,"aria-label":"搜索 Skill","panel-class":"studio-search-skill-modal","close-on-backdrop":"",onClose:a[2]||(a[2]=p=>c("close"))},{default:i(()=>[l(z,{title:"搜索 Skill",subtitle:"将当前搜索接口安装为本地 Skill",compact:"",onClose:a[0]||(a[0]=p=>c("close"))}),l(U,{density:"compact",class:"search-skill-body"},{default:i(()=>[o("div",D,[a[4]||(a[4]=o("span",{class:"search-skill-meta-label"},"接口",-1)),o("code",null,k(T.value),1),o("span",j,[l(d(g),{icon:"lucide:shield-check",class:"h-3.5 w-3.5"}),a[3]||(a[3]=h(" 密钥由环境变量提供 ",-1))])]),l(G,{modelValue:s.value,"onUpdate:modelValue":a[1]||(a[1]=p=>s.value=p),options:S,"aria-label":"安装指令语言",fit:"content"},null,8,["modelValue"]),o("div",F,[l(O,{content:C.value},null,8,["content"])])]),_:1}),l(L,{align:"between",compact:""},{default:i(()=>[o("span",J,[l(d(g),{icon:"lucide:shield-check",class:"h-3.5 w-3.5"}),a[5]||(a[5]=h(" 安装指令不包含当前密钥；运行前设置 CHATGPT2API_API_KEY ",-1))]),l(d(E),{size:"sm",variant:"primary",disabled:n.value,onClick:B},{default:i(()=>[h(k(w.value),1)]),_:1},8,["disabled"])]),_:1})]),_:1},8,["open"]))}}),se=y(Q,[["__scopeId","data-v-5db8d5b8"]]);export{se as default};
