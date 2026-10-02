"use strict";
const $ = id => document.getElementById(id);
const colors = {perfect:"#66717c", cool:"#f1c84b", good:"#51c878", miss:"#ed5858"};
const ANALYSIS_SCHEMA_VERSION = 52, MAX_CHART_BYTES = 256*1024*1024, MAX_REPLAY_BYTES = 128*1024*1024;
let data = null, playing = false, chartTime = 0, anchorTime = 0, anchorPerf = 0, audioUrl = null, realtime = null, exportJobId = null, exportTimer = null, densitySeeking = false;
const canvas = $("track"), ctx = canvas.getContext("2d"), audio = $("audio");
const densityCurveCanvas=$("densityCurve"),densityCurveCtx=densityCurveCanvas.getContext("2d"),densityCursorCanvas=$("densityCursor"),densityCursorCtx=densityCursorCanvas.getContext("2d");

function formatTime(ms) { ms=Math.max(0,ms); const m=Math.floor(ms/60000),s=Math.floor(ms/1000)%60,x=Math.floor(ms%1000); return `${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}.${String(x).padStart(3,"0")}`; }
function setStatus(text,error=false){ $("status").textContent=text; $("status").classList.toggle("error",error); }
function setExportStatus(text,error=false){$("exportStatus").textContent=text;$("exportStatus").classList.toggle("error",error);}
async function loadVersion(){try{const response=await fetch("/api/version"),body=await response.json();if(response.ok)$("appInfo").textContent=`Server v${body.version} · by Winter_Melon`;}catch(error){console.warn("Unable to load server version",error);}}
async function upload(path,file){ const response=await fetch(path,{method:"POST",headers:{"X-Filename":file.name,"Content-Type":"application/octet-stream"},body:file}); const body=await response.json(); if(!response.ok) throw new Error(body.error?.message||"上传失败"); }
async function analyze(){
  const chart=$("chartFile").files[0], replay=$("replayFile").files[0]; if(!chart||!replay){setStatus("请选择 .mcz/.osz 和 .mrv",true);return;}
  if(chart.size>MAX_CHART_BYTES){setStatus("谱面包不能超过 256 MiB",true);return;}
  if(replay.size>MAX_REPLAY_BYTES){setStatus("回放不能超过 128 MiB",true);return;}
  try { setStatus("正在上传与分析…"); await upload("/api/chart",chart); await upload("/api/replay",replay); const response=await fetch("/api/analyze",{method:"POST"}); const body=await response.json(); if(!response.ok)throw new Error(body.error?.message||"分析失败"); if(body.schemaVersion!==ANALYSIS_SCHEMA_VERSION)throw new Error("前后端版本不一致，请停止并重新运行 Python 服务后刷新页面"); load(body); setStatus("分析完成"); }
  catch(error){ setStatus(error.message,true); }
}
function load(result){
  data=result; playing=false; chartTime=data.timeline.startMs; $("play").textContent="播放"; $("workspace").classList.remove("hidden");
  const formatName=data.meta.chartFormat==="osu-mania"?"osu!mania":"Malody", modName=data.meta.activeMods.length?data.meta.activeMods.join(" + "):"No Mod"; $("title").textContent=data.meta.title; $("meta").textContent=`${data.meta.artist} · ${data.meta.keyCount}K · ${formatName} · ${data.meta.version} · ${modName} · ${data.meta.player}`;
  const labels=[['总键数','totalNotes'],['Accuracy','accuracy'],['Perfect','perfect'],['Cool','cool'],['Good','good'],['Miss','miss'],['额外输入','extraInputs'],['长条重接','holdRegrabs']], formatStat=(value,key)=>key==='accuracy'?`${value.toFixed(2)}%`:value; $("summary").innerHTML=labels.map(([label,key])=>`<div><span>${label}</span><strong>${formatStat(data.summary[key],key)}</strong><small>${data.summary.byLane[key].map(value=>formatStat(value,key)).join(" · ")}</small></div>`).join("");
  $("warnings").textContent=data.warnings.join("\n"); $("progress").min=Math.floor(data.timeline.startMs); $("progress").max=Math.ceil(data.timeline.endMs); $("progress").value=chartTime; $("currentTimeInput").min=Math.floor(data.timeline.startMs); $("currentTimeInput").max=Math.ceil(data.timeline.endMs); $("currentTimeInput").value=Math.round(chartTime);
  if(audioUrl)URL.revokeObjectURL(audioUrl); audioUrl=data.timeline.audioAvailable?`/api/audio?v=${Date.now()}`:null; audio.src=audioUrl||""; prepareRealtime(); draw();
  $("densityMaximum").textContent=data.density.maxCount; requestAnimationFrame(()=>{drawDensityCurve();updateDensityCursor();});
  setExportStatus(data.timeline.audioAvailable?"固定 60 FPS，包含谱面音频":"谱面无可用音频，将导出静音视频");
}
function prepareRealtime(){
  const events=data.notes.map(note=>({timeMs:note.judgementTimeMs,judgement:note.judgement})).sort((a,b)=>a.timeMs-b.timeMs),prefix=[{perfect:0,cool:0,good:0,miss:0}];
  for(const event of events){const next={...prefix[prefix.length-1]};next[event.judgement]++;prefix.push(next);} realtime={events,prefix};
}
function realtimeAt(timeMs){
  let low=0,high=realtime.events.length;while(low<high){const middle=(low+high)>>1;if(realtime.events[middle].timeMs<=timeMs)low=middle+1;else high=middle;}
  const counts=realtime.prefix[low],total=counts.perfect+counts.cool+counts.good+counts.miss,accuracy=total?(counts.perfect*100+counts.cool*75+counts.good*40)/total:100;
  return {accuracy,...counts};
}
function updateRealtime(){if(!realtime)return;const stats=realtimeAt(chartTime);$("realtimeAccuracy").textContent=`${stats.accuracy.toFixed(2)}%`;for(const key of ["Perfect","Cool","Good","Miss"])$("realtime"+key).textContent=stats[key.toLowerCase()];}
function nearestDensity(timeMs){
  const samples=data?.density.samples;if(!samples?.length)return null;let low=0,high=samples.length;while(low<high){const middle=(low+high)>>1;if(samples[middle].timeMs<timeMs)low=middle+1;else high=middle;}
  if(low===0)return samples[0];if(low===samples.length)return samples[samples.length-1];return timeMs-samples[low-1].timeMs<=samples[low].timeMs-timeMs?samples[low-1]:samples[low];
}
function niceDensityScale(maxCount){
  if(maxCount<=0)return {ceiling:1,step:1};const rough=maxCount/4,power=10**Math.floor(Math.log10(rough)),fraction=rough/power,unit=fraction<=1?1:fraction<=2?2:fraction<=5?5:10,step=Math.max(1,unit*power);return {ceiling:Math.ceil(maxCount/step)*step,step};
}
function prepareDensityCanvas(canvas,context,width,height){const ratio=window.devicePixelRatio||1;canvas.width=Math.max(1,Math.round(width*ratio));canvas.height=Math.max(1,Math.round(height*ratio));context.setTransform(ratio,0,0,ratio,0,0);}
function smoothDensitySamples(samples){
  return samples.map((sample,index)=>{let total=0,count=0;for(let i=Math.max(0,index-2);i<=Math.min(samples.length-1,index+2);i++){total+=samples[i].count;count++;}return {...sample,count:total/count};});
}
function drawDensityCurve(){
  if(!data)return;const plot=$("densityPlot"),width=plot.clientWidth,height=plot.clientHeight;if(!width||!height)return;prepareDensityCanvas(densityCurveCanvas,densityCurveCtx,width,height);prepareDensityCanvas(densityCursorCanvas,densityCursorCtx,width,height);
  const {ceiling,step}=niceDensityScale(data.density.maxCount),axis=$("densityAxis");axis.innerHTML="";densityCurveCtx.clearRect(0,0,width,height);
  for(let value=0;value<=ceiling;value+=step){const y=height-value/ceiling*height;densityCurveCtx.strokeStyle="#26313b";densityCurveCtx.lineWidth=1;densityCurveCtx.beginPath();densityCurveCtx.moveTo(0,Math.min(height-0.5,Math.max(.5,y)));densityCurveCtx.lineTo(width,Math.min(height-0.5,Math.max(.5,y)));densityCurveCtx.stroke();const label=document.createElement("span");label.textContent=value;label.style.bottom=`${value/ceiling*100}%`;axis.appendChild(label);}
  const rawSamples=data.density.samples,samples=smoothDensitySamples(rawSamples),start=data.timeline.startMs,duration=data.timeline.endMs-start;if(!samples.length||duration<=0)return;const point=(sample)=>({x:(sample.timeMs-start)/duration*width,y:height-sample.count/ceiling*height});
  densityCurveCtx.beginPath();for(let i=0;i<samples.length;i++){const p=point(samples[i]);if(i===0)densityCurveCtx.moveTo(p.x,p.y);else densityCurveCtx.lineTo(p.x,p.y);}densityCurveCtx.lineTo(width,height);densityCurveCtx.lineTo(0,height);densityCurveCtx.closePath();densityCurveCtx.fillStyle="#66d9c426";densityCurveCtx.fill();
  densityCurveCtx.beginPath();for(let i=0;i<samples.length;i++){const p=point(samples[i]);if(i===0)densityCurveCtx.moveTo(p.x,p.y);else densityCurveCtx.lineTo(p.x,p.y);}densityCurveCtx.strokeStyle="#66d9c4";densityCurveCtx.lineWidth=2;densityCurveCtx.lineJoin="round";densityCurveCtx.stroke();updateDensityCursor();
}
function updateDensityCursor(){
  if(!data)return;const plot=$("densityPlot"),width=plot.clientWidth,height=plot.clientHeight;if(!width||!height)return;densityCursorCtx.clearRect(0,0,width,height);const ratio=(chartTime-data.timeline.startMs)/(data.timeline.endMs-data.timeline.startMs),x=Math.max(0,Math.min(width,ratio*width));densityCursorCtx.strokeStyle="#fff";densityCursorCtx.lineWidth=1;densityCursorCtx.beginPath();densityCursorCtx.moveTo(x,0);densityCursorCtx.lineTo(x,height);densityCursorCtx.stroke();const sample=nearestDensity(chartTime);$("densityCurrent").textContent=sample?.count??0;
}
function showDensityTooltip(event){
  if(!data)return;const plot=$("densityPlot"),rect=plot.getBoundingClientRect(),x=Math.max(0,Math.min(rect.width,event.clientX-rect.left)),time=data.timeline.startMs+x/rect.width*(data.timeline.endMs-data.timeline.startMs),sample=nearestDensity(time),tooltip=$("densityTooltip");if(!sample)return;tooltip.textContent=`${formatTime(sample.timeMs)} · ${sample.count} Notes / 1s`;tooltip.style.left=`${Math.max(65,Math.min(rect.width-65,x))}px`;tooltip.classList.remove("hidden");
}
function seekDensity(event){
  if(!data)return;const rect=$("densityPlot").getBoundingClientRect();if(!rect.width)return;const x=Math.max(0,Math.min(rect.width,event.clientX-rect.left));setTime(data.timeline.startMs+x/rect.width*(data.timeline.endMs-data.timeline.startMs));showDensityTooltip(event);
}
function effectiveOffset(){
  return data.timeline.audioOffsetMs + Number($("audioDelay").value);
}
function syncAudio(force=false){ if(!data?.timeline.audioAvailable||audio.readyState===0)return; const wanted=(chartTime-effectiveOffset())/1000; if(wanted<0){audio.pause();return;} if(force||Math.abs(audio.currentTime-wanted)>.08)audio.currentTime=Math.min(Math.max(wanted,0),audio.duration||Infinity); if(playing&&audio.paused)audio.play().catch(()=>{}); }
function setTime(value,forceAudio=true){ chartTime=Math.min(data.timeline.endMs,Math.max(data.timeline.startMs,value)); anchorTime=chartTime;anchorPerf=performance.now();$("progress").value=chartTime;if(forceAudio)syncAudio(true);draw(); }
function toggle(){ if(!data)return; playing=!playing; if(playing&&chartTime>=data.timeline.endMs)chartTime=data.timeline.startMs; anchorTime=chartTime;anchorPerf=performance.now();$("play").textContent=playing?"暂停":"播放"; if(playing)syncAudio(true);else audio.pause();requestAnimationFrame(frame); }
function frame(now){ if(!playing)return; chartTime=anchorTime+(now-anchorPerf); if(chartTime>=data.timeline.endMs){chartTime=data.timeline.endMs;playing=false;audio.pause();$("play").textContent="播放";} syncAudio(false);draw();if(playing)requestAnimationFrame(frame); }
function judgementColor(judgement){return judgement==="perfect"?"#fff":colors[judgement];}
function eventY(timeMs,judgeY,speed){return judgeY-(timeMs-chartTime)*speed;}
function strokeHold(note,fromMs,toMs,color,laneW,judgeY,speed){
  if(toMs<=fromMs)return; const y1=eventY(fromMs,judgeY,speed),y2=eventY(toMs,judgeY,speed),x1=note.lane*laneW+4,x2=x1+laneW-8;
  ctx.strokeStyle=color;ctx.lineWidth=color===colors.miss?2:1;ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x1,y2);ctx.moveTo(x2,y1);ctx.lineTo(x2,y2);ctx.moveTo(x1,y2);ctx.lineTo(x2,y2);ctx.stroke();
  ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x2,y1);ctx.stroke();
}
function strokeSegmentedHold(note,noteColor,laneW,judgeY,speed){
  const x1=note.lane*laneW+4,x2=x1+laneW-8,rawTransitions=[];
  const addTransition=(timeMs,color)=>rawTransitions.push({timeMs:Math.max(note.timeMs,timeMs),color,order:rawTransitions.length});
  addTransition(note.timeMs,noteColor);
  if(note.releaseBreaksCombo&&note.releaseTimeMs<note.endTimeMs)addTransition(note.releaseTimeMs,colors.miss);
  for(const regrab of note.regrabs||[]){
    if(regrab.timeMs<note.endTimeMs)addTransition(regrab.timeMs,"#fff");
    if(regrab.releaseBreaksCombo&&regrab.releaseTimeMs<note.endTimeMs)addTransition(regrab.releaseTimeMs,colors.miss);
  }
  rawTransitions.sort((a,b)=>a.timeMs-b.timeMs||a.order-b.order);
  const transitions=[];
  for(const transition of rawTransitions){
    const previous=transitions[transitions.length-1];
    if(previous?.timeMs===transition.timeMs)previous.color=transition.color;
    else transitions.push(transition);
  }
  for(let i=0;i<transitions.length;i++){
    const current=transitions[i],end=Math.min(note.endTimeMs,transitions[i+1]?.timeMs??note.endTimeMs),y1=eventY(current.timeMs,judgeY,speed),y2=eventY(end,judgeY,speed);
    ctx.strokeStyle=current.color;ctx.lineWidth=current.color===colors.miss?2:1;ctx.beginPath();ctx.moveTo(x1,y1);ctx.lineTo(x1,y2);ctx.moveTo(x2,y1);ctx.lineTo(x2,y2);ctx.stroke();
  }
  ctx.strokeStyle=noteColor;ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(x1,eventY(note.timeMs,judgeY,speed));ctx.lineTo(x2,eventY(note.timeMs,judgeY,speed));ctx.stroke();
  const finalColor=transitions[transitions.length-1].color;ctx.strokeStyle=finalColor;ctx.lineWidth=finalColor===colors.miss?2:1;ctx.beginPath();ctx.moveTo(x1,eventY(note.endTimeMs,judgeY,speed));ctx.lineTo(x2,eventY(note.endTimeMs,judgeY,speed));ctx.stroke();
}
function strokeInput(lane,timeMs,color,laneW,judgeY,speed){
  const y=eventY(timeMs,judgeY,speed),x=lane*laneW,margin=laneW*.2;ctx.strokeStyle=color;ctx.lineWidth=5;ctx.beginPath();ctx.moveTo(x+margin,y);ctx.lineTo(x+laneW-margin,y);ctx.stroke();
}
function draw(){
  if(!data)return; const w=canvas.width,h=canvas.height,k=data.meta.keyCount,laneW=w/k,judgeY=h-55,speed=Number($("fallSpeed").value)/1000;
  ctx.clearRect(0,0,w,h); ctx.fillStyle="#080b0e";ctx.fillRect(0,0,w,h);ctx.strokeStyle="#29323b";ctx.lineWidth=1;for(let i=1;i<k;i++){ctx.beginPath();ctx.moveTo(i*laneW,0);ctx.lineTo(i*laneW,h);ctx.stroke();}
  const before=(judgeY+30)/speed,after=(h-judgeY+30)/speed,visibleStart=chartTime-after,visibleEnd=chartTime+before;
  const noteStart=lowerBound(data.notes,visibleStart-data.timeline.maxHoldDurationMs),noteEnd=lowerBound(data.notes,visibleEnd,true);
  for(let i=noteStart;i<noteEnd;i++){
    const note=data.notes[i],end=note.endTimeMs??note.timeMs;if(end<visibleStart)continue;const noteColor=judgementColor(note.judgement);
    if(note.endTimeMs===null){const y=eventY(note.timeMs,judgeY,speed);ctx.strokeStyle=noteColor;ctx.lineWidth=note.judgement==="miss"?2:1;ctx.strokeRect(note.lane*laneW+4,y-8,laneW-8,16);continue;}
    if(note.releaseBreaksCombo||note.regrabs?.length)strokeSegmentedHold(note,noteColor,laneW,judgeY,speed);
    else strokeHold(note,note.timeMs,note.endTimeMs,noteColor,laneW,judgeY,speed);
  }
  const inputStart=lowerBound(data.inputs,visibleStart-data.timeline.maxInputDurationMs),inputEnd=lowerBound(data.inputs,visibleEnd,true);
  for(let i=inputStart;i<inputEnd;i++){
    const input=data.inputs[i],release=input.releaseTimeMs;if((release??input.timeMs)<visibleStart)continue;
    if(release!==null){const x=(input.lane+.5)*laneW;ctx.save();ctx.strokeStyle="#fff";ctx.lineWidth=2;ctx.setLineDash([6,6]);ctx.beginPath();ctx.moveTo(x,eventY(input.timeMs,judgeY,speed));ctx.lineTo(x,eventY(release,judgeY,speed));ctx.stroke();ctx.restore();}
    strokeInput(input.lane,input.timeMs,judgementColor(input.judgement),laneW,judgeY,speed);
  }
  ctx.strokeStyle="#e6edf3";ctx.lineWidth=2;ctx.beginPath();ctx.moveTo(0,judgeY);ctx.lineTo(w,judgeY);ctx.stroke();
  $("clock").textContent=`${formatTime(chartTime)} / ${formatTime(data.timeline.endMs)}`;$("progress").value=chartTime;if(document.activeElement!==$("currentTimeInput"))$("currentTimeInput").value=Math.round(chartTime);updateRealtime();updateDensityCursor();
}
function lowerBound(items,time,inclusive=false){let low=0,high=items.length;while(low<high){const middle=(low+high)>>1;if(items[middle].timeMs<time||inclusive&&items[middle].timeMs===time)low=middle+1;else high=middle;}return low;}
function bindSetting(rangeId,numberId,onchange){const range=$(rangeId),number=$(numberId);range.addEventListener("input",()=>{number.value=range.value;onchange?.();draw();});number.addEventListener("change",()=>{const value=Math.min(Number(range.max),Math.max(Number(range.min),Number(number.value)));if(!Number.isFinite(value))return;number.value=value;range.value=value;onchange?.();draw();});}
function setExporting(active){
  $("exportVideo").textContent=active?"取消导出":"导出 MP4";for(const id of ["analyze","chartFile","replayFile","exportChartInfo","exportRealtime","trackWidth","trackWidthNumber","trackHeight","trackHeightNumber","fallSpeed","fallSpeedNumber","audioDelay","audioDelayNumber"])$(id).disabled=active;
  $("exportProgress").classList.toggle("hidden",!active);
}
async function exportVideo(){
  if(exportJobId){try{await fetch(`/api/export/cancel?id=${encodeURIComponent(exportJobId)}`,{method:"POST"});setExportStatus("正在取消…");}catch(error){setExportStatus(error.message,true);}return;}
  if(!data){setExportStatus("请先载入谱面和回放",true);return;}
  try{
    const response=await fetch("/api/export",{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify({showChartInfo:$("exportChartInfo").checked,showRealtimeJudgements:$("exportRealtime").checked,trackWidth:Number($("trackWidth").value),trackHeight:Number($("trackHeight").value),fallSpeed:Number($("fallSpeed").value),audioDelayMs:Number($("audioDelay").value)})}),body=await response.json();
    if(!response.ok)throw new Error(body.error?.message||"无法开始导出");exportJobId=body.id;setExporting(true);setExportStatus("正在准备视频…");pollExport();
  }catch(error){setExportStatus(error.message,true);}
}
async function pollExport(){
  if(!exportJobId)return;try{const response=await fetch(`/api/export/status?id=${encodeURIComponent(exportJobId)}`),body=await response.json();if(!response.ok)throw new Error(body.error?.message||"无法读取导出进度");
    const total=body.totalFrames||1;$("exportProgress").max=total;$("exportProgress").value=body.renderedFrames;const percent=Math.floor(body.renderedFrames/total*100);
    if(body.state==="queued"||body.state==="running"){setExportStatus(`正在导出：${percent}%（${body.renderedFrames} / ${body.totalFrames||"?"} 帧）`);exportTimer=setTimeout(pollExport,500);return;}
    const completedId=exportJobId;exportJobId=null;setExporting(false);
    if(body.state==="complete"){setExportStatus(body.warning||"导出完成，正在下载");const link=document.createElement("a");link.href=`/api/export/download?id=${encodeURIComponent(completedId)}`;link.click();}
    else if(body.state==="cancelled")setExportStatus("导出已取消");else setExportStatus(body.error||"导出失败",true);
  }catch(error){exportJobId=null;setExporting(false);setExportStatus(error.message,true);}
}
$("analyze").addEventListener("click",analyze);$("play").addEventListener("click",toggle);$("restart").addEventListener("click",()=>data&&setTime(data.timeline.startMs));$("progress").addEventListener("input",()=>data&&setTime(Number($("progress").value)));
$("currentTimeInput").addEventListener("change",()=>data&&setTime(Number($("currentTimeInput").value)));
$("exportVideo").addEventListener("click",exportVideo);
$("densityPlot").addEventListener("pointerdown",event=>{if(event.button!==0||!data)return;densitySeeking=true;$("densityPlot").setPointerCapture(event.pointerId);seekDensity(event);});
$("densityPlot").addEventListener("pointermove",event=>{if(densitySeeking)seekDensity(event);else showDensityTooltip(event);});
$("densityPlot").addEventListener("pointerup",event=>{if(!densitySeeking)return;seekDensity(event);densitySeeking=false;$("densityPlot").releasePointerCapture(event.pointerId);});
$("densityPlot").addEventListener("pointercancel",()=>densitySeeking=false);$("densityPlot").addEventListener("pointerleave",()=>{if(!densitySeeking)$("densityTooltip").classList.add("hidden");});
new ResizeObserver(()=>drawDensityCurve()).observe($("densityPlot"));
bindSetting("trackWidth","trackWidthNumber",()=>canvas.width=Number($("trackWidth").value));bindSetting("trackHeight","trackHeightNumber",()=>canvas.height=Number($("trackHeight").value));bindSetting("fallSpeed","fallSpeedNumber");bindSetting("audioDelay","audioDelayNumber",()=>data&&syncAudio(true));
audio.addEventListener("loadedmetadata",()=>data&&syncAudio(true));
loadVersion();
