'use strict';
const $=id=>document.getElementById(id);
let trackingBusy=false;
let previewing=false,mirrored=false,rotated=false,busy=false,lastFrames=0,lastTime=performance.now();
function error(text){$('error').textContent=text||'';$('error').hidden=!text;}
async function request(path,body){const response=await fetch(path,body?{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)}:{cache:'no-store'});const data=await response.json();if(!response.ok)throw Error(data.error||'摄像头请求失败');return data;}
function buttons(){ $('start').disabled=busy||previewing;$('stop').disabled=busy||!previewing;$('snapshot').classList.toggle('disabled',!previewing); }
function transform(){ $('camera-image').style.transform=`rotate(${rotated?180:0}deg) scaleX(${mirrored?-1:1})`;$('mirror').textContent='镜像：'+(mirrored?'开':'关');$('mirror').setAttribute('aria-pressed',String(mirrored));$('rotate').setAttribute('aria-pressed',String(rotated));$('rotate').textContent=rotated?'恢复方向':'旋转 180°'; }
async function start(){if(busy)return;busy=true;error('');$('placeholder').hidden=false;$('placeholder-title').textContent='正在打开摄像头…';$('placeholder-detail').textContent='正在从开发板读取画面';try{await request('/api/camera/actions',{action:'start'});previewing=true;$('camera-image').hidden=false;$('camera-image').src='/api/camera/stream.mjpg?t='+Date.now();}catch(e){error(e.message);previewing=false;$('placeholder-title').textContent='摄像头暂不可用';$('placeholder-detail').textContent=e.message;}finally{busy=false;buttons();}}
async function stop(){if(busy)return;busy=true;previewing=false;$('camera-image').removeAttribute('src');$('camera-image').hidden=true;$('placeholder').hidden=false;$('placeholder-title').textContent='预览已停止';$('placeholder-detail').textContent='点击“开始预览”重新查看画面';$('badge').textContent='预览已停止';$('badge').classList.remove('live');$('live-dot').classList.remove('live');try{await request('/api/camera/actions',{action:'stop'});}catch(e){error(e.message);}finally{busy=false;buttons();}}
$('camera-image').addEventListener('load',()=>{if(previewing){$('placeholder').hidden=true;error('');}});
$('camera-image').addEventListener('error',()=>{if(previewing){$('placeholder').hidden=false;$('placeholder-title').textContent='画面连接已中断';$('placeholder-detail').textContent='请查看下方状态，点击停止后可重新开始。';error('未收到实时画面，请检查摄像头连接。');}});
async function trackingAction(action){
 if(trackingBusy)return;trackingBusy=true;error('');$('track-start').disabled=true;$('track-stop').disabled=true;
 if(action==='stop')$('tracking-state').textContent='正在停止并回中…';
 try{renderTracking(await request('/api/robot/tracking',{action}));}catch(e){error(e.message);}
 finally{trackingBusy=false;}
}
function renderTracking(state){
 const phases={stopped:'已停止',connecting:'正在连接头板',tracking:'正在跟随最高置信度人脸',waiting_face:'等待人脸，眼球回中',error:'跟随已停止：设备异常'};
 $('tracking-state').textContent=phases[state.phase]||state.phase;
 const hw=state.hardware||{},pose=hw.eye_positions_deg,target=state.target;
 $('tracking-detail').textContent=state.error||hw.error||[
 target?`目标置信度 ${(target.confidence*100).toFixed(1)}%`:'没有有效人脸时平滑回中',
 pose?`水平 ${pose.s10.toFixed(1)}° · 垂直 ${pose.s7.toFixed(1)}°`:'仅控制S7/S10，正在等待头板状态'
 ].join(' · ');
 if(!trackingBusy){$('track-start').disabled=!!state.enabled;$('track-stop').disabled=!state.enabled;}
}
async function poll(){try{const state=await request('/api/camera/status');
renderTracking(await request('/api/robot/tracking'));const phase={opening:'正在打开摄像头',streaming:'正常取帧',stopped:'已停止采集',error:'读取失败',stopping:'正在关闭摄像头'};const selected=state.selected_head;
$('head-confidence').textContent=state.detector==='yolo'?(selected?`${(selected.confidence*100).toFixed(1)}%`:'未检测到有效目标'):'未启用检测';
$('head-detail').textContent=state.detector_error||'每帧只显示最高分的一个框。';
if(state.detector_error)error('YOLO 检测异常：'+state.detector_error);
const frames=state.frames||0;const now=performance.now();const measured=Math.max(0,(frames-lastFrames)/((now-lastTime)/1000));lastFrames=frames;lastTime=now;$('capture-state').textContent=phase[state.phase]||state.phase||'等待画面';$('device').textContent=String(state.camera??state.device??'/dev/video0');$('capture-detail').textContent=state.error|| (state.vision_in_use?'人物检测同时使用这一路画面。':'画面只用于本地实时预览。');if(state.width&&state.height)$('resolution').textContent=`${state.width} × ${state.height}`;$('frame-info').textContent=`已读取 ${frames} 帧 · ${measured.toFixed(1)} 帧/秒`;$('orientation-info').textContent=`画面方向校正 ${state.rotation||0}°`;
if(previewing&&state.phase==='streaming'){$('badge').textContent='实时画面已连接';$('badge').classList.add('live');$('live-dot').classList.add('live');if($('camera-image').naturalWidth>0)$('placeholder').hidden=true;}
if(state.phase==='error'){error(state.error||'摄像头读取失败');$('badge').textContent='摄像头异常';$('badge').classList.remove('live');}
}catch(e){$('badge').textContent='本机服务未连接';error('无法连接本机服务，请确认网页服务仍在运行。');}setTimeout(poll,1000);}
$('track-start').addEventListener('click',()=>trackingAction('start'));$('track-stop').addEventListener('click',()=>trackingAction('stop'));
$('start').addEventListener('click',start);$('stop').addEventListener('click',stop);$('mirror').addEventListener('click',()=>{mirrored=!mirrored;transform();});$('rotate').addEventListener('click',()=>{rotated=!rotated;transform();});
buttons();start();poll();
