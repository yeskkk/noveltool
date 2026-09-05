"use strict";
const $=id=>document.getElementById(id);
let token="",busy=false,previewData=null,revision=0,hasText=false;
function tell(text,error=false){$("message").textContent=text;$("message").className=error?"message error":"message";}
function enable(){for(const el of document.querySelectorAll("button,input,select"))el.disabled=busy;$("commit").disabled=busy||!previewData||!previewData.can_commit;$("plan").disabled=busy||!hasText;}
async function api(path,options={}){
 const headers={"Accept":"application/json"};if(options.method){headers["X-Noveltool-Token"]=token;headers["Content-Type"]=options.raw?"application/octet-stream":"application/json";}
 const response=await fetch(path,{...options,headers,cache:"no-store",credentials:"same-origin"});const data=await response.json();
 if(!response.ok)throw new Error(Array.isArray(data.detail)?data.detail.map(e=>e.msg).join("；"):data.detail||`HTTP ${response.status}`);return data;
}
async function action(fn){if(busy)return;busy=true;enable();try{await fn();}catch(e){tell(e.message,true);}finally{busy=false;enable();}}
function invalidatePreview(){previewData=null;$("preview-info").textContent="文件或选项已改变，请重新预览；旧预览不能直接确认。";$("preview-text").value="";$("headings").replaceChildren();enable();}
for(const id of ["file","encoding","mode"])$(id).addEventListener("change",invalidatePreview);
$("preview").addEventListener("click",()=>action(async()=>{
 previewData=null;const file=$("file").files[0];if(!file)throw new Error("请先选择 TXT 文件");if(file.size>20*1024*1024)throw new Error("文件超过 20 MiB");
 tell("正在解码与预览，尚未写入正文…");const query=new URLSearchParams({filename:file.name,encoding:$("encoding").value,mode:$("mode").value});
 const data=await api("/api/import/preview?"+query,{method:"POST",body:await file.arrayBuffer(),raw:true});previewData=data;
 $("preview-info").textContent=`${data.filename} · ${data.encoding} · ${data.raw_bytes} 字节 · ${data.char_count} 字 · ${data.block_count} 个正文块${data.preview_truncated?" · 以下仅为开头":""}`;
 $("preview-text").value=data.preview_text;$("headings").replaceChildren();
 for(const h of data.headings){const p=document.createElement("p");p.textContent=`第 ${h.line} 行：${h.title}`;$("headings").append(p);}if(!data.headings.length)$("headings").textContent="没有识别到明显章节标题，不影响导入。";
 tell(data.can_commit?"核对正文是否正确；确认后会立即保存正文与原始文件。":"当前项目已有正文，只能预览；不能覆盖导入。新小说请创建新项目。",!data.can_commit);
}));
$("commit").addEventListener("click",()=>action(async()=>{
 if(!previewData)throw new Error("请重新预览");
 const data=await api("/api/import/commit",{method:"POST",body:JSON.stringify({preview_id:previewData.preview_id,expected_revision_no:previewData.expected_revision_no})});
 previewData=null;revision=data.revision_no;await reload();tell("导入已保存。原始文件与正文版本处于同一事务；现在可以创建分块计划。");
}));
function renderPlan(plan){
 $("chunks").replaceChildren();if(!plan){$("plan-info").textContent="尚无计划。";return;}
 $("plan-info").textContent=`${plan.stale?"⚠ 已过期，请重建（"+plan.stale_reason+"）":"当前有效"} · 基于 Revision ${plan.base_revision_no} · ${plan.chunk_count} 块 · 每块估计上限 ${plan.effective_chunk_limit} · ${plan.notice}`;
 for(const item of plan.chunks.slice(0,200)){
   const d=document.createElement("details"),summary=document.createElement("summary");
   summary.textContent=`第 ${item.ordinal+1} 块 · 核心 ${item.core_chars} 字符 · 重叠 ${item.overlap_chars} 字符 · 估计 ${item.estimated_tokens} token`;d.append(summary);
   const p=document.createElement("p");p.className="hint";p.textContent=item.excerpt||"（过期计划或超出文本预览范围）";d.append(p);
   const pre=document.createElement("pre");pre.className="range-map";pre.textContent=JSON.stringify({core:item.core_ranges,overlap:item.overlap_ranges},null,2);d.append(pre);$("chunks").append(d);
 }
 if(plan.chunk_count>200){const p=document.createElement("p");p.textContent="页面只展示前 200 块；数据库与计划接口保存全部分块。";$("chunks").append(p);}
}
async function reload(){
 const doc=await api("/api/manuscript");revision=doc.revision_no;hasText=doc.block_count>0;
 $("document-info").textContent=`当前 Revision ${revision} · ${doc.char_count} 字 · ${doc.block_count} 个正文块`;
 const saved=await api("/api/import/plan");renderPlan(saved.plan);if(saved.load_error)$("plan-info").textContent=saved.load_error;$("sources").replaceChildren();
 const sources=await api("/api/import/sources");for(const s of sources.sources){const p=document.createElement("p"),a=document.createElement("a");a.href=`/api/import/sources/${encodeURIComponent(s.id)}/raw`;a.textContent=`${s.filename} · ${s.encoding} · ${s.raw_bytes} 字节 · 下载原文件`;p.append(a);$("sources").append(p);}if(!sources.sources.length)$("sources").textContent="尚无原始文件；正文页的直接粘贴/旧式导入只保存正文，不保留上传字节。";
}
$("plan").addEventListener("click",()=>action(async()=>{
 tell("正在按当前正文和预算创建计划…");const settings={target_tokens:Number($("target").value),overlap_tokens:Number($("overlap").value),prompt_reserve:Number($("prompt-reserve").value),output_reserve:Number($("output-reserve").value)};
 const data=await api("/api/import/plan",{method:"POST",body:JSON.stringify({expected_revision_no:revision,settings})});renderPlan(data);tell("分块计划已保存，未调用任何模型；正文改变后需要重建计划。");
}));
$("reload").addEventListener("click",()=>action(async()=>{await reload();tell("已读取当前正文与计划。未确认的文件仍需核对预览版本。");}));
action(async()=>{token=(await api("/api/session")).csrf_token;await reload();tell("按顺序完成预览、确认导入、创建计划。已有正文也可以直接生成计划。");});
