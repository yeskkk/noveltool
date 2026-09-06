"use strict";
const $=id=>document.getElementById(id);
let token="",busy=false;
function tell(text,error=false){$("message").textContent=text;$("message").className=error?"message error":"message";}
async function api(path,options={}){
  const headers={"Accept":"application/json"};if(options.method){headers["X-Noveltool-Token"]=token;headers["Content-Type"]="application/json";}
  const response=await fetch(path,{...options,headers,cache:"no-store",credentials:"same-origin"});const data=await response.json();
  if(!response.ok){const e=new Error(Array.isArray(data.detail)?data.detail.map(x=>x.msg).join("；"):data.detail||`HTTP ${response.status}`);e.partial=data.partial_text||"";e.raw=data.raw_output||"";throw e;}return data;
}
async function loadRuns(){const data=await api("/api/llm/runs");$("runs").replaceChildren();for(const run of data.runs){const row=document.createElement("p");row.textContent=`${run.status} · 上限 ${run.requested_max_tokens??"?"} · 输入估计 ${run.input_token_estimate??"?"} · 结束 ${run.finish_reason??"未知"} · 用量 ${run.usage_json??"{}"} · ${run.model} · ${run.elapsed_ms} ms · ${run.error_message||run.purpose} · ${run.retained?"日志内容开启":"仅元数据"} · 校验：${run.validation_status||"不适用"}${run.validation_error?" · "+run.validation_error:""}`;$("runs").append(row);}if(!data.runs.length)$("runs").textContent="尚无模型调用";}
$("send").addEventListener("click",async()=>{
  if(busy)return;busy=true;$("send").disabled=true;$("result").value="";$("result-meta").textContent="";tell("正在等待模型响应…");
  try{const data=await api("/api/llm/test",{method:"POST",body:JSON.stringify({model_slot:$("slot").value,prompt:$("prompt").value,max_tokens:Number($("max-tokens").value)})});$("result").value=data.text;$("result-meta").textContent=`${data.run_id} · ${JSON.stringify(data.usage)}`;tell(data.notice);}
  catch(e){$("result").value=e.partial;$("result-meta").textContent=e.partial?"不完整响应，仅供检查，未当作成功结果":"";tell(e.message,true);}
  finally{busy=false;$("send").disabled=false;try{await loadRuns();}catch(e){tell(e.message,true);}}
});
$("refresh-runs").addEventListener("click",()=>loadRuns().catch(e=>tell(e.message,true)));
(async()=>{try{token=(await api("/api/session")).csrf_token;const data=await api("/api/config");$("model-config").textContent=`${data.config.api_base_url} · 写作：${data.config.writer_model||"未配置"} · 分析：${data.config.analysis_model||"未配置"}`;await loadRuns();tell("先确认模型名和服务地址，再发送测试。结果不会改变小说。");}catch(e){tell(e.message,true);}})();

async function structuredAction(useModel){
  if(busy)return;busy=true;for(const b of document.querySelectorAll("button"))b.disabled=true;
  $("source").readOnly=true;$("raw-output").readOnly=true;$("parsed-output").value="";$("validation-meta").textContent="校验中…";
  tell(useModel?"正在按项目协议调用分析模型；小问题模式会逐项请求并保留检查点…":"只在本地检查，不发送网络请求…");
  try{
    const body=useModel?{source:$("source").value}:{source:$("source").value,raw_output:$("raw-output").value};
    const data=await api(useModel?"/api/llm/structured-test":"/api/llm/validate",{method:"POST",body:JSON.stringify(body)});
    if(useModel)$("raw-output").value=data.original_output;
    $("parsed-output").value=JSON.stringify(data.value,null,2);
    $("validation-meta").textContent=`修复：${data.repairs.length?data.repairs.join(" → "):"无"} · ${data.requires_review?"修复可能改变含义，必须人工核对":"格式与引用通过，不代表内容一定正确"}`;
    tell(data.notice);
  }catch(e){if(e.raw||e.partial)$("raw-output").value=e.raw||e.partial;$("validation-meta").textContent="没有生成可接受结果；原文与设定未改变";tell(e.message,true);}
  finally{busy=false;$("source").readOnly=false;$("raw-output").readOnly=false;for(const b of document.querySelectorAll("button"))b.disabled=false;try{await loadRuns();}catch(e){tell(e.message,true);}}
}
$("structured-send").addEventListener("click",()=>structuredAction(true));
$("validate-local").addEventListener("click",()=>structuredAction(false));
