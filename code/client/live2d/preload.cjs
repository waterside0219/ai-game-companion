const {contextBridge,ipcRenderer}=require('electron');
contextBridge.exposeInMainWorld('deskHost',{
  onMessage:callback=>ipcRenderer.on('desk:message',(_event,value)=>callback(value)),
  onPointer:callback=>ipcRenderer.on('desk:pointer',(_event,value)=>callback(value)),
  hit:value=>ipcRenderer.send('desk:hit',Boolean(value)),
  drag:value=>ipcRenderer.send('desk:drag',Boolean(value))
});
