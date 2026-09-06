"use strict";
const ui=window.NovelUI,$=id=>document.getElementById(id);
let data=null, selectedEntity=null, entryId=null, replaced=[], eventLimit=100, reviewLimit=80, dirtyForm=false;
const titles={fact:"事实",state:"状态",event:"事件",relationship:"关系",thread:"线索",note:"笔记",narrative:"叙事分析",entity:"实体"};
function entityName(id){return data.entities.find(e=>e.id===id)?.name||"（缺失实体）";}
function showEvidence(parent,item){for(const e of item.evidence||[])parent.append(ui.node("blockquote",e.quote));}
function statusText(item){return `${item.source==="manual"?"人工锁定":"自动观察"} · 叙述位置 ${item.at_cp??"失效"}`;}
async function load(){data=await ui.api("/api/settings");render();dirtyForm=false;}
function acceptData(value){data=value;render();dirtyForm=false;}
function repopulate(select, values, selected){
  select.replaceChildren();
  for(const v of values){const o=ui.node("option",v.name);o.value=v.id;o.selected=Array.isArray(selected)?selected.includes(v.id):v.id===selected;select.append(o);}
}
function render(){
  $("overview").textContent=`正文 Revision ${data.revision_no} · ${data.entities.length} 个实体 · ${data.events.length} 个事件 · ${data.record_count} 条观察 · ${data.entries.filter(e=>e.anchor_stale&&e.active).length} 处失效人工定位`;
  const query=$("entity-search").value.toLocaleLowerCase();
  const entities=data.entities.filter(e=>e.names.join(" ").toLocaleLowerCase().includes(query));
  repopulate($("entity-select"),entities,selectedEntity);
  if(selectedEntity&&!entities.some(e=>e.id===selectedEntity))selectedEntity=null;
  if(!selectedEntity&&entities.length)selectedEntity=entities[0].id;
  $("entity-select").value=selectedEntity||"";
  for(const id of ["subject","other","participants"]){const old=id==="participants"?[...$(id).selectedOptions].map(o=>o.value):$(id).value;repopulate($(id),data.entities,old);}
  renderEntity();renderTimeline();renderManual();renderReviews();entryFields();
  if(data.errors.length)ui.tell(data.errors.join("\n"),true);
}
function renderEntity(){
  const e=data.entities.find(e=>e.id===selectedEntity), p=data.profiles.find(p=>p.id===selectedEntity&&p.active);
  $("profile-name").value=e?.name||"";$("profile-kind").value=e?.kind||"character";
  $("profile-aliases").value=p?.aliases.join("\n")||"";$("profile-notes").value=p?.notes||"";
  $("entity-meta").textContent=e?`${e.source==="manual"?"人工档案":"自动归并"} · 已出现名称：${e.names.join("、")}`:"新建人工实体；不会自动添加已发生事件。";
  $("disable-profile").disabled=!p;
  $("entity-detail").replaceChildren();
  if(!e)return;
  for(const f of e.facts){
    const box=ui.node("article",undefined,"observation");box.append(ui.node("h4",f.field+(f.conflict?" · 存在冲突":"")+(f.manual_override?" · 采用人工值":"")));
    for(const v of f.values){box.append(ui.node("p",v.value),ui.button("人工修订",()=>editItem("fact",{...v,entity_id:e.id})));showEvidence(box,v);}
    if(f.automatic_values?.length){const d=ui.node("details");d.append(ui.node("summary","查看被人工值覆盖的自动取值"),ui.node("pre",f.automatic_values.map(v=>v.value).join("\n"),"wrapped"));box.append(d);}
    $("entity-detail").append(box);
  }
  if(e.states.length)$("entity-detail").append(ui.node("h4","状态变化记录（不是按故事实际时间自动推算）"));
  for(const st of e.states){const p=ui.node("p",`${st.field}：${st.value} · ${statusText(st)} `);p.append(ui.button("人工修订",()=>editItem("state",{...st,entity_id:e.id})));$("entity-detail").append(p);}
}
function newEntity(){selectedEntity=null;renderEntity();$("profile-name").focus();}
$("new-entity").onclick=newEntity;
$("entity-search").oninput=render;
$("entity-select").onchange=()=>{selectedEntity=$("entity-select").value;renderEntity();};
$("profile-form").onsubmit=async e=>{e.preventDefault();try{const body={expected_version:data.version,name:$("profile-name").value,kind:$("profile-kind").value,aliases:$("profile-aliases").value.split("\n").map(s=>s.trim()).filter(Boolean),notes:$("profile-notes").value,active:true};const value=await ui.api("/api/settings/entities"+(selectedEntity?"/"+selectedEntity:""),{method:selectedEntity?"PUT":"POST",body:JSON.stringify(body)});if(!selectedEntity)selectedEntity=value.entities.find(x=>x.name===body.name&&x.source==="manual")?.id;acceptData(value);ui.tell("人工档案已保存；模型分析不会覆盖。");}catch(err){ui.tell(err.message,true);}};
$("disable-profile").onclick=async()=>{try{const p=data.profiles.find(x=>x.id===selectedEntity);if(!p)return;acceptData(await ui.api(`/api/settings/entities/${p.id}`,{method:"PUT",body:JSON.stringify({expected_version:data.version,name:p.name,kind:p.kind,aliases:p.aliases,notes:p.notes,active:false})}));ui.tell("已停用人工档案；存在对应自动材料时恢复自动资料。");}catch(e){ui.tell(e.message,true);}};
function entryFields(){const k=$("entry-kind").value;
  const shows={"subject-label":["fact","state","relationship"],"other-label":["relationship"],"field-label":["fact","state","relationship","thread"],"op-label":["state"],"thread-label":["thread"],"note-label":["note"],"participants-label":["event","thread"],"story-fields":["event"]};
  for(const [id,kinds] of Object.entries(shows))$(id).hidden=!kinds.includes(k);
}
$("entry-kind").onchange=entryFields;
function editItem(kind,item){
  const manual=data.entries.find(e=>e.id===item.id);
  entryId=manual?.id||null;replaced=manual?.replaces||item.observation_ids||[];
  const p=manual?.payload||item;
  $("entry-kind").value=kind==="narrative"?"note":kind;
  $("subject").value=p.entity_id||p.a||selectedEntity||"";$("other").value=p.b||"";
  $("entry-field").value=p.field||p.label||p.title||"";
  $("entry-text").value=p.value||p.description||p.summary||p.text||"";
  $("operation").value=p.operation||"set";$("thread-status").value=p.status||"open";
  $("note-kind").value=p.kind||"style";$("story-time").value=p.story_time||"";$("story-order").value=p.story_order??"";
  const refs=p.participants||p.related_entities||[];for(const o of $("participants").options)o.selected=refs.includes(o.value);
  $("at-cp").value=manual?.at_cp??item.at_cp??"";
  if(manual?.anchor===null)$("at-cp").value="";
  $("entry-meta").textContent=entryId?`编辑人工条目 ${entryId.slice(0,8)}${manual.anchor_stale?" · 原定位已失效，请重新定位":""}`:"从自动内容建立人工修订；保存后不覆盖原始观察。";
  $("disable-entry").disabled=!entryId;entryFields();$("entry-section").scrollIntoView({behavior:"smooth"});
}
function clearEntry(){entryId=null;replaced=[];$("entry-form").reset();$("entry-meta").textContent="新人工条目。位置为空表示全局 / 初始；事件必须定位到正文。";$("disable-entry").disabled=true;entryFields();}
$("clear-entry").onclick=clearEntry;
$("entry-form").onsubmit=async e=>{e.preventDefault();try{
  const kind=$("entry-kind").value,text=$("entry-text").value,field=$("entry-field").value;
  const refs=[...$("participants").selectedOptions].map(o=>o.value);let payload;
  if(kind==="fact"||kind==="state"){payload={entity_id:$("subject").value,field,value:text};if(kind==="state")payload.operation=$("operation").value;}
  else if(kind==="relationship")payload={a:$("subject").value,b:$("other").value,label:field,description:text};
  else if(kind==="event")payload={summary:text,participants:refs,story_time:$("story-time").value||null,story_order:$("story-order").value===""?null:Number($("story-order").value)};
  else if(kind==="thread")payload={title:field,description:text,status:$("thread-status").value,related_entities:refs};
  else payload={kind:$("note-kind").value,text};
  const body={expected_version:data.version,kind,payload,at_cp:$("at-cp").value===""?null:Number($("at-cp").value),replaces:replaced,active:true};
  const value=await ui.api("/api/settings/entries"+(entryId?"/"+entryId:""),{method:entryId?"PUT":"POST",body:JSON.stringify(body)});acceptData(value);clearEntry();ui.tell("人工条目已保存并锁定。重新分析只能改变自动观察。");
}catch(err){ui.tell(err.message,true);}};
$("disable-entry").onclick=async()=>{try{if(!entryId)return;acceptData(await ui.api(`/api/settings/entries/${entryId}/disable`,{method:"POST",body:JSON.stringify({expected_version:data.version})}));clearEntry();ui.tell("已停用人工覆盖，原始自动观察仍然保留。");}catch(e){ui.tell(e.message,true);}};
$("resolve-line").onclick=async()=>{try{const r=await ui.api(`/api/settings/position?line=${Number($("anchor-line").value)}&edge=end`);$("at-cp").value=r.at_cp;ui.tell(`已定位：${r.excerpt}`);}catch(e){ui.tell(e.message,true);}};
function renderTimeline(){
  const events=[...data.events];if($("event-order").value==="story")events.sort((a,b)=>(a.story_order==null)-(b.story_order==null)||(a.story_order??0)-(b.story_order??0)||a.at_cp-b.at_cp);
  const items=[...events.map(x=>["event",x]),...data.relationships.map(x=>["relationship",x]),...data.threads.map(x=>["thread",x]),...data.narrative.map(x=>["note",x])];
  $("timeline-items").replaceChildren();
  for(const [kind,x] of items.slice(0,eventLimit)){
    const box=ui.node("article",undefined,"observation");
    const title=kind==="relationship"?`${entityName(x.a)} → ${entityName(x.b)}：${x.label}`:x.title||x.summary||x.kind;
    box.append(ui.node("h4",`${titles[kind]} · ${title}`),ui.node("p",x.description||x.text||""),ui.node("small",statusText(x)+(x.story_time?" · 故事时间："+x.story_time:"")+(x.status?" · "+x.status:"")));
    box.append(ui.button("人工修订",()=>editItem(kind,x)));showEvidence(box,x);$("timeline-items").append(box);
  }
  $("more-events").hidden=items.length<=eventLimit;
}
$("event-order").onchange=renderTimeline;$("more-events").onclick=()=>{eventLimit+=100;renderTimeline();};
function renderManual(){
  $("manual-items").replaceChildren();for(const e of data.entries){const row=ui.node("p",`${titles[e.kind]} · ${e.payload.field||e.payload.title||e.payload.kind||e.payload.summary||e.payload.label||""} · ${e.active?"有效":"已停用"}${e.anchor_stale?" · 定位失效":""} `);row.append(ui.button("编辑 / 重新定位",()=>editItem(e.kind,e)));$("manual-items").append(row);}
  if(!data.entries.length)$("manual-items").textContent="尚无人工条目。";
}
function renderReviews(){
  const mode=$("review-filter").value, attention=new Set(data.review_queue.flatMap(q=>q.observation_ids||[]));
  const rows=data.observations.filter(r=>mode==="attention"?attention.has(r.id):r.status===mode);
  $("review-items").replaceChildren();
  for(const r of rows.slice(0,reviewLimit)){
    const box=ui.node("article",undefined,"observation");
    const checked=ui.node("input");checked.type="checkbox";checked.dataset.observationId=r.id;
    const label=ui.node("label","已核对该观察");label.prepend(checked);box.append(label);
    box.append(ui.node("h4",`${titles[r.kind]} · ${r.status}`),ui.node("pre",JSON.stringify(r.payload,null,2),"wrapped"));showEvidence(box,r);
    for(const [decision,label] of [["accepted","接受观察（不是锁定）"],["rejected","拒绝"],["pending","恢复待审"]])box.append(ui.button(label,async()=>{acceptData(await ui.api("/api/settings/review",{method:"POST",body:JSON.stringify({expected_version:data.version,observation_ids:[r.id],decision})}));ui.tell("审核决定已保存。");}));
    $("review-items").append(box);
  }
  if(!rows.length)$("review-items").textContent="此筛选下没有观察。";
  $("more-reviews").hidden=rows.length<=reviewLimit;
}
$("accept-checked").onclick=async()=>{try{
  const ids=[...$("review-items").querySelectorAll("input[data-observation-id]:checked")].map(x=>x.dataset.observationId);
  if(!ids.length){ui.tell("请先核对并勾选需要接纳的观察。");return;}
  if(ids.length>512)throw new Error("一次最多接纳512条，请分批核对");
  if(!confirm(`确认接纳已核对的 ${ids.length} 条观察？来源范围不是事实正确性的证明。`))return;
  acceptData(await ui.api("/api/settings/review",{method:"POST",body:JSON.stringify({expected_version:data.version,observation_ids:ids,decision:"accepted"})}));ui.tell("勾选观察的审核决定已事务保存。");
}catch(e){ui.tell(e.message,true);}};
$("review-filter").onchange=renderReviews;$("more-reviews").onclick=()=>{reviewLimit+=80;renderReviews();};
$("refresh").onclick=async()=>{if(dirtyForm&&!confirm("刷新会丢弃当前未保存的表单内容，继续吗？"))return;try{await load();ui.tell("已重新读取当前设定。");}catch(e){ui.tell(e.message,true);}};
for(const form of document.querySelectorAll("form"))form.addEventListener("input",()=>{dirtyForm=true;});
window.addEventListener("beforeunload",e=>{if(dirtyForm){e.preventDefault();e.returnValue="";}});
(async()=>{try{await ui.init();await load();clearEntry();ui.tell("可以查看自动结果，也可以新增和修改人工设定。");}catch(e){ui.tell(e.message,true);}})();
