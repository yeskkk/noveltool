"use strict";
(() => {
  const U=window.NovelUI, $=id=>document.getElementById(id);
  let catalog=null, proposal=null, draft=null, dirty=false, working=false;
  const kinds={character:"人物",location:"地点",organization:"组织",item:"物品",setting:"世界设定"};
  function changed(){dirty=true;}
  function input(parent,label,value,write,multiline=false){
    const wrap=U.node("label",label), field=U.node(multiline?"textarea":"input");
    field.value=value??""; if(multiline)field.rows=3;
    field.addEventListener("input",()=>{write(field.value);changed();});wrap.append(field);parent.append(wrap);return field;
  }
  function select(parent,label,value,options,write){
    const wrap=U.node("label",label),field=U.node("select");
    Object.entries(options).forEach(([v,t])=>{const o=U.node("option",t);o.value=v;field.append(o);});field.value=value;
    field.addEventListener("change",()=>{write(field.value);changed();});wrap.append(field);parent.append(wrap);
  }
  function list(parent,title,array,render,empty){
    const section=U.node("section");section.append(U.node("h3",title));
    array.forEach((item,i)=>{const card=U.node("div",undefined,"card");card.append(U.node("h4",`${title} ${i+1}`));render(card,item);
      card.append(U.button("删除此项",()=>{array.splice(i,1);changed();draw();}));section.append(card);});
    section.append(U.button(`增加${title}`,()=>{array.push(empty());changed();draw();}));parent.append(section);
  }
  function draw(){
    $("editor").hidden=!proposal?.draft; if(!proposal?.draft)return;
    const accepted=proposal.status==="accepted";
    $("proposal-info").textContent=`${proposal.status} · 草稿版本 ${proposal.version}${proposal.stale?" · 基线已改变，不能直接确认":""}${proposal.requires_review?" · 经过修复，请核对含义":""}`;
    $("draft-fields").disabled=accepted;$("save-draft").disabled=accepted;$("accept").disabled=accepted||proposal.stale;
    const parent=$("draft-body");parent.replaceChildren();
    input(parent,"故事前提摘要",draft.premise_summary,v=>draft.premise_summary=v,true);
    list(parent,"实体",draft.entities,(card,e)=>{
      input(card,"名称",e.name,v=>e.name=v);select(card,"类别",e.kind,kinds,v=>e.kind=v);
      input(card,"别名（每行一个）",e.aliases.join("\n"),v=>e.aliases=v.split("\n").map(x=>x.trim()).filter(Boolean),true);
      list(card,"初始事实",e.facts,(box,f)=>{input(box,"字段",f.field,v=>f.field=v);input(box,"取值",f.value,v=>f.value=v,true);},()=>({field:"",value:""}));
    },()=>({name:"",kind:"character",aliases:[],facts:[]}));
    list(parent,"关系",draft.relationships,(card,r)=>{
      input(card,"主体名称 A",r.a,v=>r.a=v);input(card,"对象名称 B",r.b,v=>r.b=v);input(card,"关系类型（有方向）",r.label,v=>r.label=v);input(card,"描述",r.description,v=>r.description=v,true);
    },()=>({a:"",b:"",label:"",description:""}));
    list(parent,"潜在线索",draft.threads,(card,t)=>{
      input(card,"标题",t.title,v=>t.title=v);input(card,"描述",t.description,v=>t.description=v,true);
      input(card,"关联实体名称（每行一个）",t.related_entities.join("\n"),v=>t.related_entities=v.split("\n").map(x=>x.trim()).filter(Boolean),true);
    },()=>({title:"",description:"",related_entities:[]}));
    const styles=U.node("section");styles.append(U.node("h3","风格建议"));
    draft.style_notes.forEach((v,i)=>{const box=U.node("div");input(box,`建议 ${i+1}`,v,text=>draft.style_notes[i]=text,true);box.append(U.button("删除这条建议",()=>{draft.style_notes.splice(i,1);changed();draw();}));styles.append(box);});
    styles.append(U.button("增加风格建议",()=>{draft.style_notes.push("");changed();draw();}));parent.append(styles);
  }
  async function history(){
    const data=await U.api("/api/ideas");$("history").replaceChildren();
    for(const r of data.proposals)$("history").append(U.button(`${r.created_at} · ${r.status}${r.stale?"（过期）":""}`,async()=>{
      if(dirty&&!confirm("当前提案有未保存编辑，是否放弃并打开历史？"))return;
      const p=await U.api(`/api/ideas/${r.id}`);load(p);$("idea-text").value=p.idea_text;
    }));
    if(!data.proposals.length)$("history").append(U.node("p","还没有构思提案。"));
  }
  function load(p){proposal=p;draft=p.draft?structuredClone(p.draft):null;dirty=false;draw();
    if(!p.draft)U.tell(p.error||`提案状态：${p.status}`);}
  async function refresh(){
    catalog=await U.api("/api/settings");$("project-info").textContent=`当前正文 ${catalog.text_length} 字符，${catalog.entities.length} 个实体。构思使用分析模型。`;
    $("generate").disabled=working||catalog.text_length>0;
    if(proposal)load(await U.api(`/api/ideas/${proposal.id}`));await history();
  }
  $("refresh").addEventListener("click",()=>{if(dirty&&!confirm("有未保存草稿编辑，确定刷新？"))return;refresh().catch(e=>U.tell(e.message,true));});
  $("idea-form").addEventListener("submit",async event=>{
    event.preventDefault();if(working)return;if(dirty&&!confirm("将生成新提案，放弃未保存编辑？"))return;
    working=true;$("generate").disabled=true;U.tell("正在生成提案；尚未写入设定。模型请求完成后会保留结果。");
    try{const p=await U.api("/api/ideas",{method:"POST",body:JSON.stringify({idea_text:$("idea-text").value,expected_version:catalog.version,max_tokens:Number($("output-tokens").value)})});load(p);await history();U.tell("提案已保存。请检查并编辑，确认前不会影响设定。");}
    catch(e){U.tell(e.message,true);await history().catch(()=>{});}finally{working=false;$("generate").disabled=catalog?.text_length>0;}
  });
  $("save-draft").addEventListener("click",async()=>{try{load(await U.api(`/api/ideas/${proposal.id}/draft`,{method:"PUT",body:JSON.stringify({expected_proposal_version:proposal.version,draft})}));U.tell("提案草稿已保存，正式设定未变。");}catch(e){U.tell(e.message,true);}});
  $("accept").addEventListener("click",async()=>{if(!confirm("确认把当前草稿写成人工初始设定？这不会生成正文。"))return;
    try{const r=await U.api(`/api/ideas/${proposal.id}/accept`,{method:"POST",body:JSON.stringify({expected_proposal_version:proposal.version,expected_version:catalog.version,draft})});catalog=r.settings;load(r.proposal);await history();U.tell("已在一个事务中确认初始设定。到“设定”页继续编辑；正文仍为空。");}catch(e){U.tell(e.message,true);}});
  window.addEventListener("beforeunload",e=>{if(dirty){e.preventDefault();e.returnValue="";}});
  U.init().then(refresh).then(()=>U.tell("输入想法，或者打开已有提案。页面读取不会调用模型。")).catch(e=>U.tell(e.message,true));
})();
