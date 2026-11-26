import {loadV2} from './r4d.js';
const canvas=document.querySelector('#model'),subtitle=document.querySelector('#subtitle'),status=document.querySelector('#status');
let driver,context,source,fade,queue=[],running=false,disposed=false,dragging=false;
const api=window.deskCompanion={ready:false,receive,hit,mouth:()=>driver?driver.model.parameters.values[driver.model.parameters.ids.indexOf('ParamMouthOpenY')]:0};
function hit(x,y){
  if(!driver)return false;
  const r=canvas.getBoundingClientRect();if(x<r.left||y<r.top||x>=r.right||y>=r.bottom)return false;
  // Reuse r4d's preserved framebuffer: only read one alpha pixel, never the full texture.
  const gl=canvas.getContext('webgl'),p=new Uint8Array(4);
  gl.readPixels(Math.floor((x-r.left)*canvas.width/r.width),canvas.height-1-Math.floor((y-r.top)*canvas.height/r.height),1,1,gl.RGBA,gl.UNSIGNED_BYTE,p);
  return p[3]>12;
}
function caption(text){
  clearTimeout(fade);
  const chars=Array.from(String(text||'').replace(/\s+/g,' ').trim()),pages=[];
  for(let i=0;i<chars.length;i+=120)pages.push(chars.slice(i,i+120).join(''));
  function show(index){
    subtitle.textContent=pages[index]||'';subtitle.style.fontSize='18px';subtitle.style.transition='none';subtitle.style.opacity='1';
    // Fit the full normal 120-character desk_say response; longer API replies page in place.
    for(let size=17;subtitle.scrollHeight>subtitle.clientHeight&&size>=14;size--)subtitle.style.fontSize=size+'px';
    requestAnimationFrame(()=>{subtitle.style.transition='opacity 1.5s';});
    fade=setTimeout(()=>{if(index+1<pages.length)show(index+1);else subtitle.style.opacity='0';},10000);
  }
  show(0);
}
function receive(message){
  if(!message||typeof message!=='object')return;
  if(message.type==='status'){status.textContent=String(message.text||'').slice(0,100);return;}
  if(message.type==='stop'){stop();queue=[];return;}
  if(message.type==='say'){
    if(queue.length>=20)queue.shift();queue.push(message);pump();
  }
}
function stop(){if(source){try{source.stop();}catch{}source.disconnect();source=null;}driver?.setAudioSource(null);}
async function pump(){
  if(running||!driver||disposed)return;running=true;
  try{while(queue.length&&!disposed){
    const message=queue.shift();caption(message.text);
    console.log('say',message.text.length,'audio chars',message.audio?message.audio.length:0);
    if(!message.audio){await new Promise(r=>setTimeout(r,3000));continue;}
    try{
      context??=new AudioContext();await context.resume();
      const bytes=Uint8Array.from(atob(message.audio),c=>c.charCodeAt(0));
      const decoded=await context.decodeAudioData(bytes.buffer);
      if(disposed)break;
      const node=context.createBufferSource();node.buffer=decoded;node.connect(context.destination);source=node;driver.setAudioSource(node);
      console.log('audio start',decoded.duration.toFixed(2),'s ctx',context.state);
      await new Promise(resolve=>{node.onended=resolve;node.start();});
      if(source===node){driver.setAudioSource(null);source=null;}node.disconnect();
    }catch(e){console.error('audio play failed',e&&e.name,e&&e.message);stop();status.textContent='声音未能播放，字幕已保留';}
  }}finally{running=false;}
}
// Electron receives only mouse coordinates and a boolean hit result, no browser privileges.
window.deskHost?.onPointer(({x,y})=>window.deskHost.hit(dragging||hit(x,y)));
window.addEventListener('pointermove',e=>{if(!dragging)window.deskHost?.hit(hit(e.clientX,e.clientY));});
window.addEventListener('pointerdown',e=>{
  if(e.button!==0||!hit(e.clientX,e.clientY))return;
  dragging=true;canvas.setPointerCapture(e.pointerId);window.deskHost?.drag(true);
});
function endDrag(){if(dragging){dragging=false;window.deskHost?.drag(false);}}
window.addEventListener('pointerup',endDrag);window.addEventListener('pointercancel',endDrag);
window.addEventListener('blur',endDrag);
window.addEventListener('pagehide',()=>{disposed=true;stop();clearTimeout(fade);driver?.dispose();context?.close();});
// a fullscreen game covers the window and marks it hidden; keep speaking anyway, the player still needs to hear.
window.deskHost?.onMessage(receive);
try{
  driver=await loadV2(canvas);api.ready=true;status.textContent='';pump();
  function frame(t){if(disposed)return;driver.frame(t);requestAnimationFrame(frame);}requestAnimationFrame(frame);
}catch{status.textContent='模型加载失败';}
