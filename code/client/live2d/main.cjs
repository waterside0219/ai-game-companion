'use strict';
const {app,BrowserWindow,ipcMain,protocol,net,session,screen}=require('electron');
const fs=require('node:fs'),path=require('node:path'),{pathToFileURL}=require('node:url'),readline=require('node:readline');
app.commandLine.appendSwitch('autoplay-policy','no-user-gesture-required');
// a fullscreen game must not make Chromium treat the companion as hidden and drop the audio.
app.commandLine.appendSwitch('disable-features','CalculateNativeWinOcclusion');
protocol.registerSchemesAsPrivileged([{scheme:'desk',privileges:{standard:true,secure:true,supportFetchAPI:true,stream:true}}]);
let win,drag=null,timer,ready=false,backlog=[],lastHit=0;
// Diagnostics for first Windows runs: append-only, no tokens or audio are written.
const logFile=path.join(app.getPath('appData'),'desk-companion','live2d.log');
const L=(...a)=>{try{fs.mkdirSync(path.dirname(logFile),{recursive:true});fs.appendFileSync(logFile,new Date().toISOString()+' '+a.map(String).join(' ')+'\n');}catch{}};
process.on('uncaughtException',e=>L('uncaught',e&&e.stack||e));
app.on('child-process-gone',(_e,d)=>L('child-gone',d.type,d.reason,d.exitCode));
L('start',process.versions.electron,process.argv.join(' '));
const width=420,height=620;
app.whenReady().then(()=>{
  L('ready');
  const directory=path.join(app.getPath('appData'),'desk-companion'),positionFile=path.join(directory,'live2d-position.json');
  fs.mkdirSync(directory,{recursive:true});
  const area=screen.getPrimaryDisplay().workArea;
  let position={x:area.x+Math.max(0,area.width-width-16),y:area.y+Math.max(0,area.height-height-16)};
  try{const p=JSON.parse(fs.readFileSync(positionFile,'utf8'));
    if(Number.isFinite(p.x)&&Number.isFinite(p.y)&&screen.getAllDisplays().some(({workArea:a})=>p.x>=a.x&&p.y>=a.y&&p.x+width<=a.x+a.width&&p.y+height<=a.y+a.height))position=p;
  }catch{}
  protocol.handle('desk',request=>{
    const u=new URL(request.url),file=path.resolve(__dirname,'.'+decodeURIComponent(u.pathname));
    if(u.hostname!=='app'||!file.startsWith(__dirname+path.sep))return new Response('',{status:403});
    return net.fetch(pathToFileURL(file).toString());
  });
  session.defaultSession.setPermissionRequestHandler((_web,_permission,callback)=>callback(false));
  win=new BrowserWindow({width,height,minWidth:width,maxWidth:width,minHeight:height,maxHeight:height,x:Math.round(position.x),y:Math.round(position.y),transparent:true,frame:false,backgroundColor:'#00000000',alwaysOnTop:true,skipTaskbar:true,focusable:false,resizable:false,show:false,hasShadow:false,
    webPreferences:{preload:path.join(__dirname,'preload.cjs'),contextIsolation:true,nodeIntegration:false,sandbox:true,backgroundThrottling:false}});
  win.setAlwaysOnTop(true,'screen-saver');win.setIgnoreMouseEvents(true,{forward:true});
  win.webContents.setWindowOpenHandler(()=>({action:'deny'}));win.webContents.on('will-navigate',e=>e.preventDefault());
  win.webContents.on('console-message',(_e,level,message,line,source)=>L('console',level,message,source+':'+line));
  win.webContents.on('did-fail-load',(_e,code,desc,url)=>L('fail-load',code,desc,url));
  win.webContents.on('render-process-gone',(_e,d)=>L('render-gone',d.reason,d.exitCode));
  win.webContents.on('did-finish-load',()=>L('loaded',JSON.stringify(win.getBounds())));
  // Some GPUs never emit ready-to-show for transparent windows; show anyway after 6s.
  setTimeout(()=>{if(!ready&&!win.isDestroyed()){L('ready-to-show timeout; forcing show');ready=true;win.showInactive();for(const m of backlog)win.webContents.send('desk:message',m);backlog=[];}},6000);
  win.once('ready-to-show',()=>{L('ready-to-show',JSON.stringify(win.getBounds()));ready=true;win.showInactive();for(const m of backlog)win.webContents.send('desk:message',m);backlog=[];});
  const save=()=>{const [x,y]=win.getPosition();try{fs.writeFileSync(positionFile,JSON.stringify({x,y}));}catch{win.webContents.send('desk:message',{type:'status',text:'位置未能保存，请检查文件夹权限'});}};
  ipcMain.on('desk:hit',(event,hit)=>{if(event.sender!==win.webContents||typeof hit!=='boolean')return;lastHit=Date.now();if(!drag)win.setIgnoreMouseEvents(!hit,{forward:true});});
  ipcMain.on('desk:drag',(event,down)=>{
    if(event.sender!==win.webContents||typeof down!=='boolean')return;
    if(down){drag={cursor:screen.getCursorScreenPoint(),position:win.getPosition()};win.setIgnoreMouseEvents(false);}
    else if(drag){drag=null;save();win.setIgnoreMouseEvents(true,{forward:true});}
  });
  // Poll native cursor too: forwarded moves may stop across different integrity levels.
  // Fail open to the game if the renderer stops returning alpha hit results.
  timer=setInterval(()=>{
    if(win.isDestroyed())return;
    const p=screen.getCursorScreenPoint();
    // setPosition grows the window on fractional DPI (Electron/Windows); setBounds pins the size.
    if(drag)win.setBounds({x:Math.round(drag.position[0]+p.x-drag.cursor.x),y:Math.round(drag.position[1]+p.y-drag.cursor.y),width,height});
    else if(Date.now()-lastHit>250)win.setIgnoreMouseEvents(true,{forward:true});
    const [x,y]=win.getPosition();win.webContents.send('desk:pointer',{x:p.x-x,y:p.y-y});
  },33);
  win.on('closed',()=>{clearInterval(timer);app.quit();});
  win.loadURL('desk://app/index.html');
});
// The parent PowerShell owns network/auth/microphone. No token or remote URL enters the page.
function handle(line){
  if(line.length>1500000)return;
  try{const m=JSON.parse(line);if(!m||!['say','status','stop'].includes(m.type))return;
    if(m.type==='say'&&(typeof m.text!=='string'||m.text.length>4000||(m.audio!=null&&(typeof m.audio!=='string'||m.audio.length>1400000))))return;
    if(m.type==='status'&&typeof m.text!=='string')return;
    if(ready)win.webContents.send('desk:message',m);else{if(backlog.length>=20)backlog.shift();backlog.push(m);}
  }catch{}
}
// Electron's main process gets no usable stdin on Windows, so PowerShell serves a
// private named pipe (random name passed as --desk-pipe=) and we read lines from it.
// Quit only once the parent process is gone (checked every 5s), so no orphan is left.
const parentAlive=()=>{try{process.kill(process.ppid,0);return true;}catch{return false;}};
const pipeArg=process.argv.find(a=>a.startsWith('--desk-pipe='));
const stream=pipeArg?require('node:net').connect('\\\\.\\pipe\\'+pipeArg.slice('--desk-pipe='.length)):process.stdin;
if(pipeArg)stream.on('connect',()=>L('pipe connected')).on('error',e=>L('pipe error',e.message));
const input=readline.createInterface({input:stream,crlfDelay:Infinity});
input.on('line',handle);
input.on('close',()=>{L('input closed; parent alive',parentAlive());if(!parentAlive())app.quit();});
setInterval(()=>{if(!parentAlive()){L('parent gone; quitting');app.quit();}},5000);
app.on('before-quit',()=>L('before-quit'));app.on('will-quit',()=>L('will-quit'));
process.on('exit',code=>L('exit',code));
app.on('window-all-closed',()=>{L('window-all-closed');app.quit();});
