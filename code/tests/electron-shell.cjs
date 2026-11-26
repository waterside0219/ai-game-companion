const assert=require('node:assert/strict'),fs=require('node:fs'),vm=require('node:vm'),path=require('node:path');
const file=path.resolve('win/live2d/main.cjs');
assert.ok(fs.existsSync(file),'Electron shell must exist');
const handlers={},events={},messages=[];let options,window,ready,interval,cursor={x:1800,y:700},saved;
const app={commandLine:{appendSwitch(){}},whenReady:()=>({then:f=>ready=f}),on:(n,f)=>events[n]=f,quit(){},getPath:()=>'/mock/appdata'};
class Window {constructor(o){options=o;window=this;this.pos=[o.x,o.y];this.webContents={on(){},setWindowOpenHandler(){},send:(...a)=>messages.push(a)};}
 on(n,f){events[n]=f;}once(n,f){events[n]=f;}loadURL(u){this.url=u;}setAlwaysOnTop(){}setIgnoreMouseEvents(v,o){this.ignore=v;this.forward=o;}showInactive(){}getPosition(){return this.pos;}setPosition(x,y){this.pos=[x,y];}isDestroyed(){return false;}}
const electron={app,BrowserWindow:Window,ipcMain:{on:(n,f)=>handlers[n]=f},protocol:{registerSchemesAsPrivileged(){},handle(){}},net:{},session:{defaultSession:{setPermissionRequestHandler(){}}},screen:{getPrimaryDisplay:()=>({workArea:{x:0,y:0,width:1920,height:1080}}),getAllDisplays:()=>[{workArea:{x:0,y:0,width:1920,height:1080}}],getCursorScreenPoint:()=>cursor,on(){}}};
const fakeFs={...fs,mkdirSync(){},readFileSync(){return '{"x":99999,"y":99999}';},writeFileSync:(p,s)=>saved=JSON.parse(s),renameSync(){}};
let input;const sandbox={require:n=>n==='electron'?electron:n==='node:fs'?fakeFs:n==='node:readline'?{createInterface:()=>({on:(n,f)=>{if(n==='line')input=f;}})}:require(n),__dirname:path.dirname(file),process:{stdin:{},on(){}},setInterval:f=>{interval=f;return 1;},clearInterval(){},console,Buffer,URL,setTimeout,clearTimeout};
vm.runInNewContext(fs.readFileSync(file,'utf8'),sandbox);ready();
assert.equal(options.transparent,true);assert.equal(options.frame,false);assert.equal(options.focusable,false);
assert.equal(options.webPreferences.nodeIntegration,false);assert.equal(options.webPreferences.contextIsolation,true);assert.equal(options.webPreferences.sandbox,true);
assert.deepEqual(window.pos,[1484,444]);assert.equal(window.ignore,true);
interval();assert.equal(messages.at(-1)[0],'desk:pointer');
handlers['desk:hit']({sender:window.webContents},true);assert.equal(window.ignore,false);
handlers['desk:hit']({sender:window.webContents},false);assert.equal(window.ignore,true);assert.equal(window.forward.forward,true);
handlers['desk:drag']({sender:window.webContents},true);cursor={x:1700,y:600};interval();assert.deepEqual(window.pos,[1384,344]);
handlers['desk:drag']({sender:window.webContents},false);assert.deepEqual(saved,{x:1384,y:344});
input(JSON.stringify({type:'say',text:'mock',audio:'SUQz'}));events['ready-to-show']();assert.ok(messages.some(x=>x[0]==='desk:message'&&x[1].text==='mock'));
console.log('PASS: transparent/no-focus/sandbox, offscreen restore, alpha hit/forward, drag/save, stdin delivery');
