"use strict";
// Reader is immutable in the browser. Draft has its own explicit base revision.
const $ = id => document.getElementById(id);
let token = "", loaded = null, target = null, draftDirty = false, busy = false;
function tell(message, error=false) { $("message").textContent=message; $("message").className=error?"message error":"message"; }
function cp(text, offset) { return Array.from(text.slice(0, offset)).length; }
function u16(text, count) { return Array.from(text).slice(0, count).join("").length; }
function count(text) { return Array.from(text).filter(c => !/[\u0009-\u000d\u001c-\u0020\u0085\u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000]/u.test(c)).length; }
function draftState() {
  $("draft-state").textContent = draftDirty ? "草稿尚未提交" : "正文已确认";
  $("draft-count").textContent = `${count($("draft").value)} 字（空白不计，标点计数）`;
  $("commit").disabled = busy || !target || target.kind === "import";
}
async function api(path, options={}) {
  const headers={"Accept":"application/json"};
  if (options.method && options.method!=="GET") headers["X-Noveltool-Token"]=token;
  if (options.body) headers["Content-Type"]="application/json";
  const response=await fetch(path,{...options,headers,cache:"no-store",credentials:"same-origin"});
  const data=await response.json();
  if (!response.ok) throw new Error(Array.isArray(data.detail)?data.detail.map(x=>`${x.loc.join(".")}: ${x.msg}`).join("；"):data.detail||`HTTP ${response.status}`);
  return data;
}
function confirmDiscard() { return !draftDirty || window.confirm("草稿尚未提交。确定放弃当前草稿吗？"); }
function clearDraft() { $("draft").value=""; target=null; draftDirty=false; $("target").textContent="尚未指定替换范围；可以直接手工追加。"; draftState(); }
async function show(data) {
  loaded=data; $("manuscript").value=data.text; $("revision").textContent=`Revision ${data.revision_no}`;
  $("counts").textContent=`${data.char_count} 字 · ${data.block_count} 块 · ${data.line_count} 逻辑行`;
  const status=await api("/api/status"); $("title").textContent=status.title;
  $("save-status").textContent=status.last_save_error?"上次保存失败，请重试":"已确认正文已写盘";
  $("stale").hidden=status.manuscript_revision_no===data.revision_no;
  const history=await api("/api/revisions"); $("history").replaceChildren();
  for (const r of history.revisions) { const row=document.createElement("p"); row.textContent=`#${r.revision_no} · ${r.kind} · ${new Date(r.created_at).toLocaleString()}${r.instruction?" · "+r.instruction:""}`; $("history").append(row); }
  if (!history.revisions.length) $("history").textContent="尚无正文修改";
}
async function action(work) {
  if (busy || !loaded) return; busy=true;
  for (const b of document.querySelectorAll("button")) b.disabled=true;
  // Freeze editable text until the response; otherwise a slow commit could erase
  // newly typed text when its old snapshot completes.
  $("draft").readOnly=true;
  try { await work(); } catch(e) { tell(e.message,true); }
  finally { busy=false; $("draft").readOnly=false; for(const b of document.querySelectorAll("button")) b.disabled=false; draftState(); }
}
function setTarget(kind,start,end,text) {
  target={kind,start,end,selected_text:text,revision_no:loaded.revision_no};
  $("draft").value=text; draftDirty=true;
  $("target").textContent=kind==="full"?`整篇编辑，基于 Revision ${loaded.revision_no}`:`替换 Unicode 区间 [${start}, ${end})，基于 Revision ${loaded.revision_no}`;
  draftState();
}
$("draft").addEventListener("input",()=>{ draftDirty=true; draftState(); });
$("select").addEventListener("click",()=>action(async()=>{
  if (!confirmDiscard()) return;
  const reader=$("manuscript"); const start=cp(reader.value,reader.selectionStart),end=cp(reader.value,reader.selectionEnd);
  setTarget("range",start,end,reader.value.slice(reader.selectionStart,reader.selectionEnd));
  tell(start===end?"已建立插入位置；输入内容后确认。":"已将选区读入草稿，可以编辑后确认替换。");
}));
$("select-lines").addEventListener("click",()=>action(async()=>{
  if (!confirmDiscard()) return;
  const data=await api("/api/manuscript/line-range",{method:"POST",body:JSON.stringify({expected_revision_no:loaded.revision_no,first_line:Number($("first-line").value),last_line:Number($("last-line").value)})});
  setTarget("range",data.start_cp,data.end_cp,data.selected_text);
  $("manuscript").setSelectionRange(u16(loaded.text,data.start_cp),u16(loaded.text,data.end_cp));
  tell("行范围已载入草稿，逻辑行的末尾换行包含在选区内。");
}));
$("edit-all").addEventListener("click",()=>action(async()=>{ if(confirmDiscard()) setTarget("full",0,Array.from(loaded.text).length,loaded.text); }));
$("commit").addEventListener("click",()=>action(async()=>{
  if (!target || target.kind==="import") throw new Error("请先建立替换范围");
  const body={expected_revision_no:target.revision_no,text:$("draft").value};
  let path="/api/manuscript/edit";
  if(target.kind==="range") { path="/api/manuscript/replace"; Object.assign(body,{start_cp:target.start,end_cp:target.end,selected_text:target.selected_text}); }
  const data=await api(path,{method:"POST",body:JSON.stringify(body)}); clearDraft(); await show(data); tell("正文已确认并保存到 SQLite；可撤销。");
}));
$("append").addEventListener("click",()=>action(async()=>{
  if(target && target.kind!=="import" && !window.confirm("这会把草稿追加到末尾，而不是替换原选区。继续吗？")) return;
  const data=await api("/api/manuscript/append",{method:"POST",body:JSON.stringify({expected_revision_no:target?target.revision_no:loaded.revision_no,text:$("draft").value})});
  clearDraft(); await show(data); tell("新段落已追加并写盘。");
}));
$("discard").addEventListener("click",()=>action(async()=>{if(confirmDiscard()) clearDraft();}));
$("undo").addEventListener("click",()=>action(async()=>{
  if(!confirmDiscard() || !window.confirm("撤销最近一次尚未撤销的正文修改？")) return;
  const data=await api("/api/revisions/undo",{method:"POST",body:JSON.stringify({expected_revision_no:loaded.revision_no})});
  clearDraft(); await show(data); tell("已撤销并保存，历史仍然保留。");
}));
$("reload").addEventListener("click",()=>action(async()=>{if(confirmDiscard()) {await show(await api("/api/manuscript"));clearDraft();tell("已重新载入正文。");}}));
$("file").addEventListener("change",()=>action(async()=>{
  const file=$("file").files[0]; if(!file || !confirmDiscard()) return;
  if(file.size>20*1024*1024) throw new Error("TXT 文件超过 20 MiB 限制");
  let text;
  try {text=new TextDecoder("utf-8",{fatal:true}).decode(await file.arrayBuffer());}
  catch {throw new Error("此页只接收有效 UTF-8 TXT；请先转换编码。");}
  text=text.replace(/^\uFEFF/,"").replace(/\r\n?/g,"\n");
  target={kind:"import",revision_no:loaded.revision_no};$("draft").value=text;draftDirty=true;
  $("target").textContent=`导入预览：${file.name}；确认前不会改变正文。`;draftState();tell("文件已读入草稿，请核对后确认导入。");
}));
$("import").addEventListener("click",()=>action(async()=>{
  if(loaded.text.length) throw new Error("已有正文，禁止覆盖导入。请建立新项目。");
  const data=await api("/api/manuscript/import",{method:"POST",body:JSON.stringify({expected_revision_no:target?target.revision_no:loaded.revision_no,text:$("draft").value,paragraph_mode:$("paragraph-mode").value})});
  clearDraft();await show(data);tell("正文已导入并立即保存。");
}));
window.addEventListener("beforeunload",event=>{if(draftDirty){event.preventDefault();event.returnValue="";}});
(async()=>{
  try {token=(await api("/api/session")).csrf_token;await show(await api("/api/manuscript"));draftState();tell("已载入正文。草稿需显式确认，旧版本不会被覆盖。");
    setInterval(async()=>{if(busy)return;try{const s=await api("/api/status");$("stale").hidden=s.manuscript_revision_no===loaded.revision_no;}catch{tell("无法联系本地服务；请保留草稿，确认服务状态。",true);}},3000);
  }catch(e){tell(e.message,true);}
})();

$("ai-rewrite").addEventListener('click',()=>action(async()=>{
 if(!confirmDiscard())return;
 const el=$("manuscript"),a=cp(el.value,el.selectionStart),b=cp(el.value,el.selectionEnd);
 if(a===b)throw new Error('先选中非空原文；也可先按行定位，再使用这个按钮。');
 draftDirty=false;location.href=`/generation?mode=rewrite&revision=${loaded.revision_no}&start=${a}&end=${b}`;
}));
