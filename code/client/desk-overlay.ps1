#requires -Version 5.1
param([switch]$SelfTest)
$ErrorActionPreference = 'Stop'
trap {
    if ($SelfTest) { throw $_ }
    Add-Type -AssemblyName System.Windows.Forms
    [System.Windows.Forms.MessageBox]::Show('启动失败，行号：'+$_.InvocationInfo.ScriptLineNumber+'，错误类型：'+$_.Exception.GetType().Name+'。请截图此提示。','电脑陪玩') | Out-Null
    break
}
Add-Type -AssemblyName PresentationFramework, PresentationCore, WindowsBase, System.Windows.Forms, System.Drawing, System.Web.Extensions

# Native callbacks do no I/O. The hook always passes the mouse event to the game.
# Background C# threads avoid PowerShell runspace callbacks and keep the WPF UI responsive.
Add-Type -ReferencedAssemblies System.dll, System.Core.dll, System.Drawing.dll, System.Windows.Forms.dll, System.Web.Extensions.dll -TypeDefinition @'
using System;
using System.IO;
using System.Text;
using System.Net;
using System.Drawing;
using System.Drawing.Imaging;
using System.Drawing.Drawing2D;
using System.Collections.Generic;
using System.Collections.Concurrent;
using System.Diagnostics;
using System.Runtime.InteropServices;
using System.Threading;
using System.Web.Script.Serialization;
using System.Windows.Forms;

public static class DeskNative {
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] static extern uint GetWindowThreadProcessId(IntPtr hwnd, out uint process);
    [DllImport("user32.dll")] public static extern bool RegisterHotKey(IntPtr hwnd, int id, uint modifiers, uint key);
    [DllImport("user32.dll")] public static extern bool UnregisterHotKey(IntPtr hwnd, int id);
    [DllImport("user32.dll", EntryPoint="GetWindowLong")] static extern int GetWindowLong(IntPtr hwnd, int index);
    [DllImport("user32.dll", EntryPoint="SetWindowLong")] static extern int SetWindowLong(IntPtr hwnd, int index, int value);
    [DllImport("user32.dll")] static extern IntPtr SetWindowsHookEx(int id, MouseProc proc, IntPtr module, uint thread);
    [DllImport("user32.dll")] static extern bool UnhookWindowsHookEx(IntPtr hook);
    [DllImport("user32.dll")] static extern IntPtr CallNextHookEx(IntPtr hook, int code, IntPtr wp, IntPtr lp);
    [DllImport("kernel32.dll", CharSet=CharSet.Auto)] static extern IntPtr GetModuleHandle(string name);
    [DllImport("kernel32.dll", SetLastError=true)] static extern IntPtr OpenProcess(uint access, bool inherit, uint id);
    [DllImport("kernel32.dll", CharSet=CharSet.Unicode)] static extern bool QueryFullProcessImageName(IntPtr process, int flags, StringBuilder path, ref int size);
    [DllImport("kernel32.dll")] static extern bool CloseHandle(IntPtr handle);
    [StructLayout(LayoutKind.Sequential)] struct MouseInfo { public int x,y; public uint mouseData,flags,time; public IntPtr extra; }
    delegate IntPtr MouseProc(int code, IntPtr wp, IntPtr lp);
    static MouseProc callback = MouseHook;
    static IntPtr hook = IntPtr.Zero;
    static int requested;
    public static int SideButton = 2, VoiceButton = 1;
    [DllImport("user32.dll")] public static extern short GetAsyncKeyState(int key);
    public class VoiceEdge { public bool down; public double ts; }
    static readonly ConcurrentQueue<VoiceEdge> voiceEdges=new ConcurrentQueue<VoiceEdge>();
    static int voiceDown;
    public static void SetVoiceState(bool down) {
        int value=down?1:0;
        if(Interlocked.Exchange(ref voiceDown,value)!=value)
            voiceEdges.Enqueue(new VoiceEdge {down=down,ts=(DateTime.UtcNow-new DateTime(1970,1,1)).TotalSeconds});
    }
    public static VoiceEdge TakeVoice() { VoiceEdge edge; return voiceEdges.TryDequeue(out edge)?edge:null; }
    static IntPtr MouseHook(int code, IntPtr wp, IntPtr lp) {
        try {
            if (code >= 0 && (wp.ToInt64() == 0x020B || wp.ToInt64() == 0x020C)) {
                MouseInfo info = (MouseInfo)Marshal.PtrToStructure(lp, typeof(MouseInfo));
                int button=(int)(info.mouseData >> 16); bool down=wp.ToInt64()==0x020B;
                if(button==SideButton && down) Interlocked.Exchange(ref requested,1);
                if(button==VoiceButton) SetVoiceState(down);
            }
        } catch { }
        return CallNextHookEx(hook, code, wp, lp);
    }
    public static bool StartHook() { hook=SetWindowsHookEx(14,callback,GetModuleHandle(null),0); return hook!=IntPtr.Zero; }
    public static void StopHook() { if(hook!=IntPtr.Zero) UnhookWindowsHookEx(hook); hook=IntPtr.Zero; }
    public static bool TakeDirect() { return Interlocked.Exchange(ref requested,0)!=0; }
    // 11-07 gamepad: hold BACK + tap LB = look; hold BACK + hold RB = talk. XInput only, polled on a background thread.
    [StructLayout(LayoutKind.Sequential)] struct PadState { public uint packet; public ushort buttons; public byte lt, rt; public short lx, ly, rx, ry; }
    [DllImport("xinput1_4.dll", EntryPoint="XInputGetState")] static extern int PadGet14(int index, out PadState state);
    [DllImport("xinput9_1_0.dll", EntryPoint="XInputGetState")] static extern int PadGet91(int index, out PadState state);
    static Thread padThread;
    static volatile bool padRunning;
    public static volatile bool PadSeen;
    public static volatile bool PadTalking;
    static int PadGet(int index, out PadState state) {
        try { return PadGet14(index, out state); }
        catch (DllNotFoundException) { return PadGet91(index, out state); }
        catch (EntryPointNotFoundException) { return PadGet91(index, out state); }
    }
    public static void StartPad() {
        if (padThread != null) return;
        padRunning = true;
        padThread = new Thread(PadLoop);
        padThread.IsBackground = true;
        padThread.Start();
    }
    public static void StopPad() { padRunning = false; }
    static void PadLoop() {
        const ushort Back = 0x0020, LB = 0x0100, RB = 0x0200;
        ushort previous = 0; bool talking = false; int idle = 0, offPolls = 0;
        while (padRunning) {
            ushort buttons = 0; bool any = false;
            try {
                for (int i = 0; i < 4; i++) {
                    PadState state;
                    if (PadGet(i, out state) == 0) { any = true; buttons |= state.buttons; }
                }
            } catch { padRunning = false; break; }
            PadSeen = any;
            // 11-08 21:08: a Bluetooth pad drops a poll now and then; that read as "released" and every talk
            // came out as a 0.2s clip ("太短了"). A missing read changes nothing, and a release must hold ~0.3s.
            if (!any && talking && ++offPolls < 60) { Thread.Sleep(30); continue; } // pad really gone ~2s: let go
            bool back = (buttons & Back) != 0;
            // Fire when the BACK+LB pair becomes held, whichever key went down first (players press them together).
            bool look = back && (buttons & LB) != 0;
            bool lookBefore = (previous & Back) != 0 && (previous & LB) != 0;
            if (look && !lookBefore) Interlocked.Exchange(ref requested, 1);
            bool wantTalk = back && (buttons & RB) != 0;
            if (wantTalk) offPolls = 0; else if (talking) offPolls++;
            if (wantTalk && !talking) { talking = true; PadTalking = true; SetVoiceState(true); }
            else if (!wantTalk && talking && offPolls >= 10) { talking = false; PadTalking = false; SetVoiceState(false); } // ~0.3s of real release
            previous = buttons;
            // Poll fast while a pad is connected, slowly otherwise.
            if (any) { idle = 0; Thread.Sleep(30); } else { idle++; Thread.Sleep(idle > 3 ? 1000 : 30); }
        }
        PadTalking = false;
        if (talking) SetVoiceState(false);
    }
    public static void SetClickThrough(IntPtr hwnd, bool locked) {
        int style=GetWindowLong(hwnd,-20)|0x80000|0x80; // layered + tool window
        style = locked ? style|0x20|0x08000000 : style & ~0x20 & ~0x08000000;
        SetWindowLong(hwnd,-20,style);
    }
    public static string ForegroundPath() {
        uint id; GetWindowThreadProcessId(GetForegroundWindow(),out id);
        IntPtr process=OpenProcess(0x1000,false,id);
        if(process==IntPtr.Zero) return "";
        try { var path=new StringBuilder(32768); int size=path.Capacity;
            return QueryFullProcessImageName(process,0,path,ref size) ? path.ToString() : "";
        } finally { CloseHandle(process); }
    }
}

// Display is a child process fed through a private named pipe (Electron's main
// process has no usable stdin on Windows). Never a local HTTP service.
public sealed class DeskDisplay : IDisposable {
    readonly Process process; readonly object writeLock=new object();
    readonly JavaScriptSerializer json=new JavaScriptSerializer { MaxJsonLength=1600000 };
    readonly System.IO.Pipes.NamedPipeServerStream pipe; bool connected;
    readonly List<byte[]> backlog=new List<byte[]>();
    public DeskDisplay(string root) {
        var name="desk-companion-"+Guid.NewGuid().ToString("N");
        pipe=new System.IO.Pipes.NamedPipeServerStream(name,System.IO.Pipes.PipeDirection.Out,1,System.IO.Pipes.PipeTransmissionMode.Byte,System.IO.Pipes.PipeOptions.Asynchronous);
        pipe.BeginWaitForConnection(ar=>{
            try { pipe.EndWaitForConnection(ar); lock(writeLock){ connected=true; foreach(var b in backlog) pipe.Write(b,0,b.Length); backlog.Clear(); pipe.Flush(); } } catch {}
        },null);
        // Chromium's own log catches native crashes that never reach main.cjs.
        var chromiumLog=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData),"desk-companion","electron-chromium.log");
        var info=new ProcessStartInfo(Path.Combine(root,"electron","electron.exe"),"--enable-logging=file --log-file=\""+chromiumLog+"\" --desk-pipe="+name+" \""+Path.Combine(root,"live2d")+"\"");
        info.UseShellExecute=false;info.CreateNoWindow=true;
        process=Process.Start(info);
    }
    public bool Send(object value) {
        try {
            var bytes=Encoding.UTF8.GetBytes(json.Serialize(value)+"\n");
            lock(writeLock) {
                if(process.HasExited)return false;
                if(!connected){ if(backlog.Count>=20)backlog.RemoveAt(0); backlog.Add(bytes); return true; }
                pipe.Write(bytes,0,bytes.Length);pipe.Flush();
            }
            return true;
        } catch {return false;}
    }
    public void Dispose() {try {lock(writeLock){pipe.Dispose();}if(!process.WaitForExit(1500))process.Kill();}catch{}process.Dispose();}
}

// One 30-second in-memory WinMM buffer. CALLBACK_NULL means no unmanaged callback lifetime.
public sealed class DeskRecorder : IDisposable {
    [StructLayout(LayoutKind.Sequential,Pack=2)] struct Format {public ushort tag,channels;public uint rate,bytes;public ushort align,bits,extra;}
    [StructLayout(LayoutKind.Sequential)] struct Header {public IntPtr data;public uint length,recorded;public UIntPtr user;public uint flags,loops;public IntPtr next;public UIntPtr reserved;}
    [DllImport("winmm.dll")] static extern uint waveInOpen(out IntPtr handle,uint device,ref Format format,IntPtr callback,IntPtr instance,uint flags);
    [DllImport("winmm.dll")] static extern uint waveInPrepareHeader(IntPtr handle,IntPtr header,uint size);
    [DllImport("winmm.dll")] static extern uint waveInAddBuffer(IntPtr handle,IntPtr header,uint size);
    [DllImport("winmm.dll")] static extern uint waveInStart(IntPtr handle);
    [DllImport("winmm.dll")] static extern uint waveInReset(IntPtr handle);
    [DllImport("winmm.dll")] static extern uint waveInUnprepareHeader(IntPtr handle,IntPtr header,uint size);
    [DllImport("winmm.dll")] static extern uint waveInClose(IntPtr handle);
    readonly ConcurrentQueue<DeskNative.VoiceEdge> edges=new ConcurrentQueue<DeskNative.VoiceEdge>();
    readonly ManualResetEvent stop=new ManualResetEvent(false);readonly Thread worker;readonly DeskClient client;
    IntPtr handle,data,header;bool prepared,pressed;double began;
    public volatile bool Recording;
    static double Now(){return (DateTime.UtcNow-new DateTime(1970,1,1)).TotalSeconds;}
    public DeskRecorder(DeskClient owner){client=owner;worker=new Thread(Loop);worker.IsBackground=true;worker.Start();}
    public void Edge(DeskNative.VoiceEdge edge){edges.Enqueue(edge);}
    static void Check(uint code){if(code!=0)throw new InvalidOperationException("microphone unavailable");}
    void Begin(){
        var format=new Format {tag=1,channels=1,rate=16000,bytes=32000,align=2,bits=16,extra=0};
        Check(waveInOpen(out handle,0xffffffff,ref format,IntPtr.Zero,IntPtr.Zero,0));
        data=Marshal.AllocHGlobal(960000);header=Marshal.AllocHGlobal(Marshal.SizeOf(typeof(Header)));
        Marshal.StructureToPtr(new Header {data=data,length=960000},header,false);
        Check(waveInPrepareHeader(handle,header,(uint)Marshal.SizeOf(typeof(Header))));prepared=true;
        Check(waveInAddBuffer(handle,header,(uint)Marshal.SizeOf(typeof(Header))));Check(waveInStart(handle));
        began=Now();Recording=true;
    }
    public static byte[] Wave(byte[] pcm,int count){
        using(var stream=new MemoryStream())using(var writer=new BinaryWriter(stream)){
            writer.Write(Encoding.ASCII.GetBytes("RIFF"));writer.Write(36+count);writer.Write(Encoding.ASCII.GetBytes("WAVEfmt "));
            writer.Write(16);writer.Write((short)1);writer.Write((short)1);writer.Write(16000);writer.Write(32000);writer.Write((short)2);writer.Write((short)16);
            writer.Write(Encoding.ASCII.GetBytes("data"));writer.Write(count);writer.Write(pcm,0,count);return stream.ToArray();
        }
    }
    void Finish(double released,bool send){
        try {
            if(handle!=IntPtr.Zero)waveInReset(handle);
            if(send && Recording && header!=IntPtr.Zero){
                var h=(Header)Marshal.PtrToStructure(header,typeof(Header));int count=(int)Math.Min(h.recorded,960000U)&~1;
                if(count>=6400){var pcm=new byte[count];Marshal.Copy(data,pcm,0,count);client.SendVoice(Wave(pcm,count),released,count/32);}
                else client.SetVoiceStatus("太短了，按住说完再松开");
            }
        } finally {
            Recording=false;
            if(prepared)waveInUnprepareHeader(handle,header,(uint)Marshal.SizeOf(typeof(Header)));
            if(handle!=IntPtr.Zero)waveInClose(handle);
            if(header!=IntPtr.Zero)Marshal.FreeHGlobal(header);if(data!=IntPtr.Zero)Marshal.FreeHGlobal(data);
            handle=header=data=IntPtr.Zero;prepared=false;
        }
    }
    void Loop(){
        try {while(!stop.WaitOne(25)){
            DeskNative.VoiceEdge edge;
            while(edges.TryDequeue(out edge)){
                if(edge.down && !pressed){pressed=true;try{Begin();}catch{Finish(Now(),false);client.SetVoiceStatus("麦克风不可用：检查隐私权限、默认输入设备或占用");}}
                else if(!edge.down){pressed=false;if(Recording)Finish(edge.ts,true);}
            }
            if(Recording && Now()-began>=30)Finish(began+30,true); // remains pressed until physical release: send once
        }}finally{Finish(Now(),false);}
    }
    public void Dispose(){stop.Set();worker.Join(2000);}
}

public class DeskReply { public long id; public string text; public string audio_url; }
public class DeskClient : IDisposable {
    public volatile bool Paused;
    public volatile string TalkLevel="normal";
    public volatile string Status="连接中";
    public readonly ConcurrentQueue<DeskReply> Replies=new ConcurrentQueue<DeskReply>();
    readonly string baseUrl, token, logPath;
    readonly string[] games;
    readonly JavaScriptSerializer json=new JavaScriptSerializer();
    readonly object jsonLock=new object(), logLock=new object(), hashLock=new object();
    readonly ManualResetEvent stop=new ManualResetEvent(false);
    int directInFlight, voiceInFlight;
    public DeskDisplay Display;
    public volatile string VoiceStatus="";
    public DateTime VoiceStatusUntil=DateTime.MinValue;
    public void SetVoiceStatus(string text){VoiceStatus=text;VoiceStatusUntil=DateTime.UtcNow.AddSeconds(6);}
    public void SendVoice(byte[] wav,double released,int ms){
        if(stop.WaitOne(0)||Interlocked.CompareExchange(ref voiceInFlight,1,0)!=0){SetVoiceStatus("上一句还在处理，请稍等");return;}
        SetVoiceStatus("正在听清…");
        ThreadPool.QueueUserWorkItem(delegate {
            try {
                if(Now()-released>=20){SetVoiceStatus("这句已超时，请再说一次");return;}
                string boundary="desk-voice-"+Guid.NewGuid().ToString("N");byte[] body;
                using(var output=new MemoryStream()){
                    byte[] a=Encoding.UTF8.GetBytes("--"+boundary+"\r\nContent-Disposition: form-data; name=\"audio\"; filename=\"voice.wav\"\r\nContent-Type: audio/wav\r\n\r\n");
                    output.Write(a,0,a.Length);output.Write(wav,0,wav.Length);
                    a=Encoding.UTF8.GetBytes("\r\n--"+boundary+"\r\nContent-Disposition: form-data; name=\"meta\"\r\nContent-Type: application/json\r\n\r\n"+Encode(new {ts=released,ms=ms})+"\r\n--"+boundary+"--\r\n");output.Write(a,0,a.Length);body=output.ToArray();
                }
                var response=Decode<Dictionary<string,object>>(Encoding.UTF8.GetString(Request("/v1/voice","POST",body,"multipart/form-data; boundary="+boundary,Math.Max(1,(int)((20-(Now()-released))*1000)))));
                string gate=response.ContainsKey("gate")?Convert.ToString(response["gate"]):"send";
                SetVoiceStatus(gate=="sent"?"已送到":gate=="voice_hour"?"说太快啦，歇一下":gate=="noise"?"没听清，请再说一次":gate=="expired"?"这句已超时，请再说一次":gate=="disabled"?"语音接口还没开":"未能送达，请稍后再说");
            }catch {SetVoiceStatus("语音发送失败，请检查连接");Log("语音发送失败");}
            finally {Interlocked.Exchange(ref voiceInFlight,0);}
        });
    }
    readonly Thread captureThread, heartbeatThread, pollThread, saveThread;
    // read-only save watcher. Sends the newest P5R DATA.DAT after the game writes it; never writes saves.
    DateTime lastSaveSent=DateTime.MinValue;
    static System.Collections.Generic.IEnumerable<string> SaveRoots() {
        string roaming=Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        string local=Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData);
        foreach(string b in new[]{Path.Combine(roaming,"SEGA","P5R","Steam"),Path.Combine(local,"SEGA","P5R","Steam")}) if(Directory.Exists(b)) yield return b;
        foreach(string s in new[]{@"C:\Program Files (x86)\Steam\userdata",@"C:\Program Files\Steam\userdata",@"D:\Steam\userdata",@"E:\Steam\userdata",@"D:\SteamLibrary\userdata"}) {
            if(!Directory.Exists(s)) continue;
            string[] users; try{users=Directory.GetDirectories(s);}catch{continue;}
            foreach(string u in users){string r=Path.Combine(u,"1687950","remote"); if(Directory.Exists(r)) yield return r;}
        }
    }
    // Phantom Blade Zero (Steam 4115450) save, read-only: <lib>\steamapps\common\PBZ\ProjectPBZ\Saved\SaveGames\Steam\<id>\PBZSaveGame.sav
    DateTime lastPbzSent=DateTime.MinValue;
    static System.Collections.Generic.IEnumerable<string> SteamLibraries() {
        var seen=new System.Collections.Generic.HashSet<string>(StringComparer.OrdinalIgnoreCase);
        var libs=new System.Collections.Generic.List<string>{@"C:\Program Files (x86)\Steam",@"C:\Program Files\Steam",@"D:\Steam",@"E:\Steam",@"F:\Steam",@"D:\SteamLibrary",@"E:\SteamLibrary",@"F:\SteamLibrary",@"C:\SteamLibrary"};
        foreach(string steam in new[]{@"C:\Program Files (x86)\Steam",@"C:\Program Files\Steam",@"D:\Steam"}) {
            string vdf=Path.Combine(steam,"steamapps","libraryfolders.vdf");
            try { if(File.Exists(vdf)) foreach(System.Text.RegularExpressions.Match m in System.Text.RegularExpressions.Regex.Matches(File.ReadAllText(vdf),"\"path\"\\s+\"([^\"]+)\"")) libs.Add(m.Groups[1].Value.Replace(@"\\",@"\")); } catch {}
        }
        foreach(string l in libs) if(seen.Add(l)) yield return l;
    }
    static System.Collections.Generic.IEnumerable<string> PbzRoots() {
        foreach(string lib in SteamLibraries()) { string r=Path.Combine(lib,"steamapps","common","PBZ","ProjectPBZ","Saved","SaveGames"); if(Directory.Exists(r)) yield return r; }
        string local=Path.Combine(Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),"ProjectPBZ","Saved","SaveGames");
        if(Directory.Exists(local)) yield return local;
    }
    DateTime SendNewest(string game,System.Collections.Generic.IEnumerable<string> roots,string pattern,DateTime last) {
        FileInfo newest=null;
        foreach(string root in roots) {
            string[] files; try{files=Directory.GetFiles(root,pattern,SearchOption.AllDirectories);}catch{continue;}
            foreach(string f in files) { var info=new FileInfo(f); if(newest==null||info.LastWriteTimeUtc>newest.LastWriteTimeUtc) newest=info; }
        }
        if(newest==null || newest.LastWriteTimeUtc<=last || (DateTime.UtcNow-newest.LastWriteTimeUtc).TotalSeconds<=5 || newest.Length<=0 || newest.Length>=8*1024*1024) return last;
        byte[] data;
        using(var fs=new FileStream(newest.FullName,FileMode.Open,FileAccess.Read,FileShare.ReadWrite|FileShare.Delete)) using(var ms=new MemoryStream()){fs.CopyTo(ms);data=ms.ToArray();}
        string slot=newest.Directory!=null?newest.Directory.Name:"";
        string boundary="desk-save-"+Guid.NewGuid().ToString("N");byte[] body;
        using(var output=new MemoryStream()){
            byte[] a=Encoding.UTF8.GetBytes("--"+boundary+"\r\nContent-Disposition: form-data; name=\"save\"; filename=\""+newest.Name+"\"\r\nContent-Type: application/octet-stream\r\n\r\n");
            output.Write(a,0,a.Length);output.Write(data,0,data.Length);
            a=Encoding.UTF8.GetBytes("\r\n--"+boundary+"\r\nContent-Disposition: form-data; name=\"meta\"\r\nContent-Type: application/json\r\n\r\n"+Encode(new {game=game,slot=slot,mtime=(newest.LastWriteTimeUtc-new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds})+"\r\n--"+boundary+"--\r\n");output.Write(a,0,a.Length);body=output.ToArray();
        }
        Request("/v1/save","POST",body,"multipart/form-data; boundary="+boundary,30000);
        Log("存档已同步 "+game+" "+slot);
        return newest.LastWriteTimeUtc;
    }
    void SaveLoop() {
        if(stop.WaitOne(20000)) return;
        while(!stop.WaitOne(0)) {
            try { lastSaveSent=SendNewest("p5r",SaveRoots(),"DATA.DAT",lastSaveSent); } catch { Log("存档同步失败，稍后重试"); }
            try { lastPbzSent=SendNewest("pbz",PbzRoots(),"PBZSaveGame.sav",lastPbzSent); } catch { Log("影之刃存档同步失败，稍后重试"); }
            if(stop.WaitOne(15000)) break;
        }
    }
    string lastHash=null;
    double lastSent=0;
    long after=0;
    public DeskClient(string url,string deviceToken,string[] gameList,string log) {
        Uri parsed=new Uri(url);
        if(parsed.Scheme!="https" && !(parsed.Scheme=="http" && parsed.IsLoopback))
            throw new ArgumentException("服务地址必须是 HTTPS");
        baseUrl=url.TrimEnd('/'); token=deviceToken; games=gameList; logPath=log;
        ServicePointManager.SecurityProtocol |= SecurityProtocolType.Tls12;
        ServicePointManager.DefaultConnectionLimit=8;
        captureThread=new Thread(CaptureLoop); heartbeatThread=new Thread(HeartbeatLoop); pollThread=new Thread(PollLoop); saveThread=new Thread(SaveLoop);
        captureThread.IsBackground=heartbeatThread.IsBackground=pollThread.IsBackground=saveThread.IsBackground=true;
    }
    public void Start() { captureThread.Start(); heartbeatThread.Start(); pollThread.Start(); saveThread.Start(); }
    public void Direct() {
        if(stop.WaitOne(0) || Interlocked.CompareExchange(ref directInFlight,1,0)!=0) return;
        ThreadPool.QueueUserWorkItem(delegate {
            try { if(!stop.WaitOne(0)) SendFrame(true); }
            finally { Interlocked.Exchange(ref directInFlight,0); }
        });
    }
    public void Dispose() { stop.Set(); }
    static double Now() { return (DateTime.UtcNow-new DateTime(1970,1,1,0,0,0,DateTimeKind.Utc)).TotalSeconds; }
    string Encode(object value) { lock(jsonLock) return json.Serialize(value); }
    T Decode<T>(string value) { lock(jsonLock) return json.Deserialize<T>(value); }
    public void Log(string message) {
        // Only our fixed status messages / spoken text, never HTTP headers, screenshots or exception bodies.
        try { lock(logLock) {
            string line=DateTime.Now.ToString("s")+" "+message+Environment.NewLine;
            if(File.Exists(logPath) && new FileInfo(logPath).Length+Encoding.UTF8.GetByteCount(line)>1024*1024) File.WriteAllText(logPath,"");
            File.AppendAllText(logPath,line,Encoding.UTF8);
        }} catch { }
    }
    byte[] Request(string path,string method,byte[] body,string contentType,int timeout) {
        var request=(HttpWebRequest)WebRequest.Create(baseUrl+path);
        request.Method=method; request.Timeout=timeout; request.ReadWriteTimeout=timeout;
        request.AllowAutoRedirect=false; request.KeepAlive=true;
        request.Headers[HttpRequestHeader.Authorization]="Bearer "+token;
        request.Proxy=WebRequest.GetSystemWebProxy();
        if(body!=null) { request.ContentType=contentType; request.ContentLength=body.Length;
            using(var stream=request.GetRequestStream()) stream.Write(body,0,body.Length); }
        try { using(var response=request.GetResponse()) using(var stream=response.GetResponseStream()) using(var output=new MemoryStream()) {
            var buffer=new byte[8192]; int count;
            while((count=stream.Read(buffer,0,buffer.Length))>0) {
                if(output.Length+count>1200000) throw new InvalidDataException("response too large");
                output.Write(buffer,0,count);
            }
            return output.ToArray();
        }} catch(WebException ex) { if(ex.Response!=null) ex.Response.Close(); throw; }
    }
    public static bool IsGamePath(string path,string exe,string[] allow) {
        path=(path??"").Replace('/','\\').ToLowerInvariant();
        if(path.Contains("\\steamapps\\common\\")||path.Contains("\\epic games\\")||path.Contains("\\wegameapps\\")) return true;
        foreach(string name in allow) if(String.Equals(Path.GetFileNameWithoutExtension(name),Path.GetFileNameWithoutExtension(exe),StringComparison.OrdinalIgnoreCase)) return true;
        return false;
    }
    void Foreground(out string path,out string exe,out bool game) {
        path=DeskNative.ForegroundPath(); exe=Path.GetFileName(path); game=IsGamePath(path,exe,games);
    }
    void HeartbeatLoop() {
        while(!stop.WaitOne(0)) {
            double began=Now();
            try {
                string path,exe; bool game; Foreground(out path,out exe,out game);
                byte[] data=Encoding.UTF8.GetBytes(Encode(new { ts=Now(),paused=Paused,talk_level=TalkLevel,fg_exe=exe,fg_path=path,is_game=game }));
                Request("/v1/heartbeat","POST",data,"application/json",3000);
            } catch { Log("心跳未送达，稍后重试"); }
            if(stop.WaitOne(Math.Max(0,(int)(5000-(Now()-began)*1000)))) break;
        }
    }
    // a side-key look is sent sharp (up to 1920 wide) so the AI can read small numbers; auto stays 768.
    static Bitmap Capture(int maxWidth) {
        Rectangle bounds=Screen.PrimaryScreen.Bounds;
        using(var original=new Bitmap(bounds.Width,bounds.Height,PixelFormat.Format24bppRgb)) {
            using(var graphics=Graphics.FromImage(original)) graphics.CopyFromScreen(bounds.Location,Point.Empty,bounds.Size);
            int width=Math.Min(maxWidth,bounds.Width);
            int height=Math.Max(1,(int)Math.Round(bounds.Height*(double)width/bounds.Width));
            var resized=new Bitmap(width,height,PixelFormat.Format24bppRgb);
            using(var graphics=Graphics.FromImage(resized)) { graphics.InterpolationMode=InterpolationMode.HighQualityBilinear;
                graphics.DrawImage(original,0,0,resized.Width,resized.Height); }
            return resized;
        }
    }
    public static byte[] EncodeJpeg(Bitmap bitmap) { return EncodeJpeg(bitmap,70L); }
    public static byte[] EncodeJpeg(Bitmap bitmap,long quality) {
        ImageCodecInfo codec=null;
        foreach(var candidate in ImageCodecInfo.GetImageEncoders()) if(candidate.MimeType=="image/jpeg") codec=candidate;
        using(var stream=new MemoryStream()) using(var options=new EncoderParameters(1)) {
            options.Param[0]=new EncoderParameter(System.Drawing.Imaging.Encoder.Quality,quality);
            bitmap.Save(stream,codec,options); return stream.ToArray();
        }
    }
    public static bool IsBlack(Bitmap bitmap) {
        int black=0,total=0;
        for(int y=0;y<bitmap.Height;y+=Math.Max(1,bitmap.Height/24)) for(int x=0;x<bitmap.Width;x+=Math.Max(1,bitmap.Width/32)) {
            Color pixel=bitmap.GetPixel(x,y); total++; if(pixel.R<12 && pixel.G<12 && pixel.B<12) black++;
        }
        return black>=total*0.98;
    }
    public static string DHash(Bitmap bitmap) {
        using(var small=new Bitmap(9,8)) {
            using(var graphics=Graphics.FromImage(small)) graphics.DrawImage(bitmap,0,0,9,8);
            ulong hash=0;
            for(int y=0;y<8;y++) for(int x=0;x<8;x++) {
                Color a=small.GetPixel(x,y), b=small.GetPixel(x+1,y);
                int left=a.R*299+a.G*587+a.B*114, right=b.R*299+b.G*587+b.B*114;
                hash=(hash<<1)|(left>right?1UL:0UL);
            }
            return hash.ToString("x16");
        }
    }
    public static int HashDistance(string a,string b) {
        ulong value=Convert.ToUInt64(a,16)^Convert.ToUInt64(b,16); int count=0;
        while(value!=0) { value&=value-1; count++; } return count;
    }
    byte[] FrameBody(byte[] image,string meta,string boundary) {
        using(var output=new MemoryStream()) {
            byte[] start=Encoding.UTF8.GetBytes("--"+boundary+"\r\nContent-Disposition: form-data; name=\"image\"; filename=\"frame.jpg\"\r\nContent-Type: image/jpeg\r\n\r\n");
            output.Write(start,0,start.Length); output.Write(image,0,image.Length);
            byte[] end=Encoding.UTF8.GetBytes("\r\n--"+boundary+"\r\nContent-Disposition: form-data; name=\"meta\"\r\nContent-Type: application/json\r\n\r\n"+meta+"\r\n--"+boundary+"--\r\n");
            output.Write(end,0,end.Length); return output.ToArray();
        }
    }
    void CaptureLoop() {
        while(!stop.WaitOne(0)) {
            double began=Now(); SendFrame(false);
            if(stop.WaitOne(Math.Max(0,(int)(3000-(Now()-began)*1000)))) break;
        }
    }
    void SendFrame(bool explicitLook) {
        // Direct uses its own bounded worker so a slow auto upload cannot delay the requested capture.
        try {
            string path,exe; bool game; Foreground(out path,out exe,out game);
            if(!explicitLook && (Paused || !game)) { Status="闭眼"; return; }
            double captured=Now();
            using(var bitmap=Capture(explicitLook?1920:768)) {
                bool black=IsBlack(bitmap); string hash=DHash(bitmap);
                lock(hashLock) {
                    if(!explicitLook && lastHash!=null && captured-lastSent<180 && HashDistance(hash,lastHash)<=6) {
                        if(!black) Status="看着"; return;
                    }
                }
                byte[] image=explicitLook?EncodeJpeg(bitmap,85L):EncodeJpeg(bitmap);
                if(image.Length>(explicitLook?2*1024*1024:120*1024)) { Status="画面较复杂，本帧跳过"; Log("JPEG 超过 120KB，跳过本帧"); return; }
                string meta=Encode(new {ts=captured,kind=explicitLook?"direct":"auto",fg_exe=exe,is_game=game,dhash=hash,capture_black=black});
                string boundary="desk-"+Guid.NewGuid().ToString("N");
                string result=Encoding.UTF8.GetString(Request("/v1/frame","POST",FrameBody(image,meta,boundary),"multipart/form-data; boundary="+boundary,8000));
                var response=Decode<Dictionary<string,object>>(result);
                string gate=response.ContainsKey("gate") ? Convert.ToString(response["gate"]) : "";
                // A stale/paused heartbeat can reject a just-resumed frame; let the next capture retry.
                if(gate!="heartbeat") {
                    lock(hashLock) { if(captured>=lastSent) { lastHash=hash; lastSent=captured; } }
                }
                Status=black?"画面全黑，请改无边框窗口":gate=="direct_store"?"直看待服务端开通":(Paused||!game)?"闭眼":"看着";
            }
        } catch { Status="离线，自动重连"; Log("画面发送失败，稍后重试"); }
    }
    void PollLoop() {
        while(!stop.WaitOne(0)) {
            try {
                string data=Encoding.UTF8.GetString(Request("/v1/say?after="+after,"GET",null,null,30000));
                foreach(var reply in Decode<DeskReply[]>(data)) {
                    // Bound the local backlog as well; drop expired backlog after reconnection.
                    while(Replies.Count>=20) { DeskReply old; Replies.TryDequeue(out old); }
                    Replies.Enqueue(reply); after=reply.id;
                    if(Display!=null){
                        string audio=null;try{if(reply.audio_url!=null)audio=Convert.ToBase64String(ReadAudio(reply.audio_url));}catch{SetVoiceStatus("声音未能下载，字幕已保留");}
                        if(!Display.Send(new {type="say",text=reply.text,audio=audio}))SetVoiceStatus("Live2D 窗口已退出，请重启电脑陪玩");
                    }
                }
            } catch { Log("字幕连接断开，稍后重试"); if(stop.WaitOne(2000)) break; }
        }
    }
    public DeskReply TakeReply() { DeskReply value; return Replies.TryDequeue(out value) ? value : null; }
    public void DownloadAudio(string relative,string destination) {
        // Only same-origin audio paths; never forward the device token to a returned external URL.
        if(!System.Text.RegularExpressions.Regex.IsMatch(relative??"",@"^/v1/audio/[1-9][0-9]*\.mp3$")) throw new InvalidDataException("invalid audio path");
        File.WriteAllBytes(destination,ReadAudio(relative));
    }
    byte[] ReadAudio(string relative) {
        if(!System.Text.RegularExpressions.Regex.IsMatch(relative??"",@"^/v1/audio/[1-9][0-9]*\.mp3$"))throw new InvalidDataException("invalid audio path");
        return Request(relative,"GET",null,null,5000);
    }
}
'@
function Update-DeskConfig($value) {
    # A missing voice_button identifies a v4 config. Migrate the old default exactly once.
    if(-not $value.PSObject.Properties['voice_button']) {
        if($value.side_button -eq 'XBUTTON1'){$value.side_button='XBUTTON2'}
        $value | Add-Member NoteProperty voice_button 'XBUTTON1'
    }
    if(-not $value.PSObject.Properties['live2d']){$value | Add-Member NoteProperty live2d $true}
    if(-not $value.PSObject.Properties['bottom_subtitles']){$value | Add-Member NoteProperty bottom_subtitles $false}
    if($value.side_button -notin @('XBUTTON1','XBUTTON2') -or $value.voice_button -notin @('XBUTTON1','XBUTTON2') -or $value.side_button -eq $value.voice_button){throw '侧键配置冲突，请对调 side_button 与 voice_button'}
}
if ($SelfTest) { return }

$script:appDir = Join-Path $env:APPDATA 'desk-companion'
New-Item -ItemType Directory -Force -Path $script:appDir | Out-Null
$script:configPath = Join-Path $script:appDir 'config.json'
$script:logPath = Join-Path $script:appDir 'log.txt'
try {
    Write-Host '[1/5] 检查是否已在运行'
    # Single instance without [ref] binding; an abandoned mutex from a crashed run counts as ours.
    $script:mutex=[System.Threading.Mutex]::new($false,'Local\desk-companion')
    try { $createdNew=$script:mutex.WaitOne(0) } catch [System.Threading.AbandonedMutexException] { $createdNew=$true }
    if(-not $createdNew){
        [System.Windows.MessageBox]::Show('电脑陪玩已经在运行了。看右下角托盘的小图标，右键退出后再重新打开。','电脑陪玩') | Out-Null
        exit
    }
    $script:ownsMutex=$true
    if (-not (Test-Path $script:configPath)) { Copy-Item "$PSScriptRoot\config.example.json" $script:configPath }
    Write-Host '[2/5] 读取配置' $script:configPath
    $script:config = Get-Content -Raw -Encoding UTF8 $script:configPath | ConvertFrom-Json
    Update-DeskConfig $script:config
    $script:config | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $script:configPath
    if ([string]::IsNullOrWhiteSpace($script:config.device_token)) {
        [System.Windows.MessageBox]::Show('请先把设备口令填进 device_token，保存后重新双击 start.bat。配置文件即将打开。','电脑陪玩') | Out-Null
        Start-Process notepad.exe -ArgumentList ('"' + $script:configPath + '"')
        exit
    }
    if ($script:config.talk_level -notin @('quiet','normal','chatty')) { throw 'talk_level 配置不正确' }
    Write-Host '[3/5] 连接服务' $script:config.base_url
    $gameList=[string[]]@($script:config.games | Where-Object {$_})
    $script:client = [DeskClient]::new([string]$script:config.base_url,[string]$script:config.device_token,$gameList,[string]$script:logPath)
    $script:client.Paused = [bool]$script:config.paused
    $script:client.TalkLevel = $script:config.talk_level
    Write-Host '[4/5] 启动 Live2D'
    # before Live2D starts, pull any changed companion files the server lists (the launcher only refreshes this script).
    try {
        [Net.ServicePointManager]::SecurityProtocol=[Net.SecurityProtocolType]::Tls12
        $sync=New-Object System.Net.WebClient; $sync.Encoding=[System.Text.Encoding]::UTF8
        $base=([string]$script:config.base_url).TrimEnd('/')
        $sync.Headers['Authorization']='Bearer '+[string]$script:config.device_token
        $manifest=$sync.DownloadString($base+'/v1/client/manifest.json') | ConvertFrom-Json
        $sha=[System.Security.Cryptography.SHA256]::Create()
        foreach($entry in $manifest.files.PSObject.Properties){
            $rel=[string]$entry.Name
            if($rel -eq 'desk-overlay.ps1' -or $rel -notmatch '^[A-Za-z0-9_\-]+(/[A-Za-z0-9_\-]+)*\.[A-Za-z0-9]+$'){continue}
            $local=Join-Path $PSScriptRoot ($rel -replace '/','\')
            $have=if(Test-Path $local){-join ($sha.ComputeHash([System.IO.File]::ReadAllBytes($local)) | ForEach-Object {$_.ToString('x2')})}else{''}
            if($have -eq [string]$entry.Value){continue}
            $sync.Headers['Authorization']='Bearer '+[string]$script:config.device_token
            $bytes=$sync.DownloadData($base+'/v1/client/f/'+$rel)
            if((-join ($sha.ComputeHash($bytes) | ForEach-Object {$_.ToString('x2')})) -ne [string]$entry.Value){$script:client.Log('同步跳过 '+$rel+'：校验不符');continue}
            New-Item -ItemType Directory -Force -Path (Split-Path -Parent $local) | Out-Null
            if(Test-Path $local){Copy-Item $local ($local+'.prev') -Force}
            [System.IO.File]::WriteAllBytes($local,$bytes)
            $script:client.Log('已同步 '+$rel)
        }
        $sha.Dispose()
    } catch { $script:client.Log('同步文件跳过：'+$_.Exception.Message) }
    if($script:config.live2d){$script:display=[DeskDisplay]::new($PSScriptRoot);$script:client.Display=$script:display}
    $script:recorder=[DeskRecorder]::new($script:client)
    $script:lastDisplayStatus='' 

    [xml]$xaml = @'
<Window xmlns="http://schemas.microsoft.com/winfx/2006/xaml/presentation" xmlns:x="http://schemas.microsoft.com/winfx/2006/xaml"
    WindowStyle="None" AllowsTransparency="True" Background="Transparent" Topmost="True" ShowInTaskbar="False" ShowActivated="False" ResizeMode="NoResize" Height="190">
  <Border x:Name="Panel" Background="#01000000" CornerRadius="12" Padding="18,10" BorderThickness="1" BorderBrush="Transparent">
    <Grid>
      <Grid.RowDefinitions><RowDefinition Height="*"/><RowDefinition Height="28"/></Grid.RowDefinitions>
      <TextBlock x:Name="Subtitle" Foreground="White" TextWrapping="Wrap" TextAlignment="Center" VerticalAlignment="Bottom" FontFamily="Microsoft YaHei UI">
        <TextBlock.Effect><DropShadowEffect BlurRadius="6" ShadowDepth="1" Opacity="1" Color="Black"/></TextBlock.Effect>
      </TextBlock>
      <TextBlock x:Name="StatusText" Grid.Row="1" Foreground="#CCFFFFFF" FontSize="12" HorizontalAlignment="Right" VerticalAlignment="Center"/>
    </Grid>
  </Border>
</Window>
'@
    $reader = New-Object System.Xml.XmlNodeReader $xaml
    $script:window = [Windows.Markup.XamlReader]::Load($reader)
    $script:subtitle = $script:window.FindName('Subtitle')
    $script:statusText = $script:window.FindName('StatusText')
    $script:panel = $script:window.FindName('Panel')
    $script:window.Width = [Math]::Min([Math]::Max([double]400,[double]$script:config.width),[System.Windows.SystemParameters]::PrimaryScreenWidth)
    $script:subtitle.FontSize = [Math]::Min([double]64,[Math]::Max([double]14,[double]$script:config.font_size))
    $script:window.Left = if ($script:config.left -lt 0) { ([System.Windows.SystemParameters]::PrimaryScreenWidth-$script:window.Width)/2 } else { [double]$script:config.left }
    $script:window.Top = if ($script:config.top -lt 0) { [System.Windows.SystemParameters]::PrimaryScreenHeight-230 } else { [double]$script:config.top }
    $script:locked = $true
    $script:lastSubtitle = [DateTime]::MinValue
    $script:queue = New-Object 'System.Collections.Generic.Queue[object]'
    $script:player = New-Object System.Windows.Media.MediaPlayer
    $script:playing = $false
    $script:nextSubtitle = [DateTime]::MinValue
    $script:audioPath = $null
    $script:audioJob = $null
    $script:audioDir = Join-Path $script:appDir 'audio'
    New-Item -ItemType Directory -Force $script:audioDir | Out-Null
    Get-ChildItem $script:audioDir -Filter '*.mp3' | Remove-Item -Force

    function Save-Config {
        $script:config.left=$script:window.Left; $script:config.top=$script:window.Top
        $script:config.font_size=$script:subtitle.FontSize
        $script:config.paused=$script:client.Paused; $script:config.talk_level=$script:client.TalkLevel
        $script:config | ConvertTo-Json -Depth 5 | Set-Content -Encoding UTF8 $script:configPath
    }
    function Toggle-Pause { $script:client.Paused=-not $script:client.Paused; Save-Config }
    function Cycle-Talk {
        $levels=@('quiet','normal','chatty'); $i=[Array]::IndexOf($levels,$script:client.TalkLevel)
        $script:client.TalkLevel=$levels[($i+1)%3]; Save-Config
    }
    function Toggle-Lock {
        $script:locked=-not $script:locked
        [DeskNative]::SetClickThrough($script:hwnd,$script:locked)
        $script:panel.Background=if($script:locked){[Windows.Media.BrushConverter]::new().ConvertFromString('#01000000')}else{[Windows.Media.BrushConverter]::new().ConvertFromString('#990D1520')}
        $script:panel.BorderBrush=if($script:locked){[Windows.Media.Brushes]::Transparent}else{[Windows.Media.Brushes]::LightSteelBlue}
        if(-not $script:locked){$script:window.Activate() | Out-Null}
        Save-Config
    }
    function Finish-Audio {
        $script:player.Close(); $script:playing=$false
        if($script:audioPath){Remove-Item $script:audioPath -Force -ErrorAction SilentlyContinue; $script:audioPath=$null}
    }
    $script:player.add_MediaEnded({ Finish-Audio })
    $script:player.add_MediaFailed({ $script:client.Log('音频播放失败，字幕已显示'); Finish-Audio })
    $script:window.add_MouseLeftButtonDown({ if(-not $script:locked){$script:window.DragMove(); Save-Config} })
    $script:window.add_MouseWheel({ param($sender,$event)
        if(-not $script:locked){$script:subtitle.FontSize=[Math]::Min([double]64,[Math]::Max([double]14,[double]$script:subtitle.FontSize+[Math]::Sign($event.Delta)*2)); Save-Config}
    })

    function Register-Shortcut([int]$id,[string]$name) {
        $parts=$name.Split('+'); [uint32]$mods=0x4000 # MOD_NOREPEAT
        foreach($part in $parts[0..($parts.Length-1)]) {
            if($part -eq 'Ctrl'){$mods=$mods -bor 2}; if($part -eq 'Alt'){$mods=$mods -bor 1}
            if($part -eq 'Shift'){$mods=$mods -bor 4}; if($part -eq 'Win'){$mods=$mods -bor 8}
        }
        $key=[System.Windows.Forms.Keys][Enum]::Parse([System.Windows.Forms.Keys],$parts[-1],$true)
        if(-not [DeskNative]::RegisterHotKey($script:hwnd,$id,$mods,[uint32]$key)){$script:client.Log('快捷键被占用：'+$name)}
    }
    $script:window.add_SourceInitialized({
        $script:hwnd=[System.Windows.Interop.WindowInteropHelper]::new($script:window).Handle
        $script:source=[System.Windows.Interop.HwndSource]::FromHwnd($script:hwnd)
        $script:messageHook=[System.Windows.Interop.HwndSourceHook]{param($hwnd,$msg,$wParam,$lParam,[ref]$handled)
            if($msg -eq 0x0312){switch($wParam.ToInt32()){1 {Toggle-Pause} 2 {Cycle-Talk} 3 {Toggle-Lock}}; $handled.Value=$true}
            return [IntPtr]::Zero
        }
        $script:source.AddHook($script:messageHook)
        Register-Shortcut 1 $script:config.hotkeys.pause
        Register-Shortcut 2 $script:config.hotkeys.talk
        Register-Shortcut 3 $script:config.hotkeys.unlock
        [DeskNative]::VoiceButton=if($script:config.voice_button -eq 'XBUTTON2'){2}else{1}
        [DeskNative]::SideButton=if($script:config.side_button -eq 'XBUTTON2'){2}else{1}
        if(-not [DeskNative]::StartHook()){$script:client.Log('侧键监听失败')}
        if($script:config.gamepad -ne $false){[DeskNative]::StartPad()}
        [DeskNative]::SetClickThrough($script:hwnd,$true)
    })
    $script:tray=New-Object System.Windows.Forms.NotifyIcon
    $script:tray.Icon=[System.Drawing.SystemIcons]::Information
    $script:tray.Text='电脑陪玩'
    $menu=New-Object System.Windows.Forms.ContextMenuStrip
    $pauseItem=$menu.Items.Add('闭眼 / 睁眼'); $pauseItem.add_Click({Toggle-Pause})
    $talkItem=$menu.Items.Add('切换话量'); $talkItem.add_Click({Cycle-Talk})
    $lockItem=$menu.Items.Add('移动 / 锁定字幕条'); $lockItem.add_Click({Toggle-Lock})
    $exitItem=$menu.Items.Add('退出'); $exitItem.add_Click({$script:window.Close()})
    $script:tray.ContextMenuStrip=$menu; $script:tray.Visible=$true

    $script:timer=New-Object System.Windows.Threading.DispatcherTimer
    $script:timer.Interval=[TimeSpan]::FromMilliseconds(100)
    $script:timer.add_Tick({
        if([DeskNative]::TakeDirect()){$script:client.Direct()}
        # Recover a missed release; this never creates a synthetic press.
        $vk=if([DeskNative]::VoiceButton -eq 1){0x05}else{0x06}
        # The mouse watchdog must not cut off a talk held on the gamepad.
        if(-not [DeskNative]::PadTalking -and ([DeskNative]::GetAsyncKeyState($vk) -band 0x8000) -eq 0){[DeskNative]::SetVoiceState($false)}
        while($null -ne ($edge=[DeskNative]::TakeVoice())){$script:recorder.Edge($edge)}
        $level=@{quiet='安静';normal='正常';chatty='话多'}[$script:client.TalkLevel]
        $state=if($script:client.Paused){'闭眼'}else{$script:client.Status}
        $voiceStatus=if($script:recorder.Recording){'在听…'}elseif([DateTime]::UtcNow -lt $script:client.VoiceStatusUntil){$script:client.VoiceStatus}else{''}
        $displayStatus=if($voiceStatus){$voiceStatus}else{$state+' · '+$level}
        $script:statusText.Text=$displayStatus+$(if(-not $script:locked){' · 拖动位置 / 滚轮字号'}else{''})
        $script:panel.Visibility=if($script:config.bottom_subtitles -or -not $script:locked -or -not $script:config.live2d){'Visible'}else{'Hidden'}
        if($script:display -and $displayStatus -ne $script:lastDisplayStatus){$null=$script:display.Send(@{type='status';text=$displayStatus});$script:lastDisplayStatus=$displayStatus}
        while($null -ne ($reply=$script:client.TakeReply())){
            while($script:queue.Count -ge 20){$null=$script:queue.Dequeue()}; $script:queue.Enqueue($reply)
        }
        # Download asynchronously in a separate runspace; MediaPlayer stays on its WPF thread.
        if($script:audioJob -and $script:audioJob.Handle.IsCompleted){
            try {
                $null=$script:audioJob.PowerShell.EndInvoke($script:audioJob.Handle)
                if(Test-Path $script:audioPath){$script:player.Open([Uri]::new($script:audioPath)); $script:player.Play()}
                else {Finish-Audio}
            } catch {Finish-Audio}
            finally {$script:audioJob.PowerShell.Dispose(); $script:audioJob=$null}
        }
        if(-not $script:playing -and -not $script:audioJob -and $script:queue.Count -gt 0 -and [DateTime]::UtcNow -ge $script:nextSubtitle){
            $reply=$script:queue.Dequeue(); $script:subtitle.Text=$reply.text; $script:subtitle.Opacity=1
            $script:lastSubtitle=[DateTime]::UtcNow; $script:client.Log($reply.text)
            $script:nextSubtitle=[DateTime]::UtcNow.AddSeconds(3)
            if($reply.audio_url -and -not $script:config.live2d){
                $script:playing=$true; $script:audioPath=Join-Path $script:audioDir ($reply.id.ToString()+'.mp3')
                $ps=[PowerShell]::Create()
                $null=$ps.AddScript('param($client,$url,$file) $client.DownloadAudio($url,$file)').AddArgument($script:client).AddArgument($reply.audio_url).AddArgument($script:audioPath)
                $script:audioJob=@{PowerShell=$ps;Handle=$ps.BeginInvoke()}
            }
        }
        # lastSubtitle starts at MinValue, so elapsed can be huge: keep Math.Max on the double overload.
        $elapsed=([DateTime]::UtcNow-$script:lastSubtitle).TotalSeconds
        if($elapsed -ge 10){$script:subtitle.Opacity=[Math]::Max([double]0,[double](1-($elapsed-10)/1.5))}
    })
    Write-Host '[5/5] 已启动，右下角应出现他的形象；关掉这个窗口会一起退出'
    $script:client.Start(); $script:timer.Start()
    $null=$script:window.ShowDialog()
} catch {
    # Startup errors are local and must be visible even though start.bat hides the console.
    $detail='行号：'+$_.InvocationInfo.ScriptLineNumber+'；'+("$($_.InvocationInfo.Line)").Trim()+'；'+$_.Exception.Message
    try{Add-Content -Encoding UTF8 $script:logPath ('[startup-error] '+$detail+' | '+($_.ScriptStackTrace -replace '\r?\n',' / '))}catch{}
    [System.Windows.MessageBox]::Show('启动失败。错误类型：'+$_.Exception.GetType().Name+'。'+$detail+'。请截图此提示。','电脑陪玩') | Out-Null
} finally {
    if($script:timer){$script:timer.Stop()}
    if($script:recorder){$script:recorder.Dispose()}
    if($script:client){$script:client.Dispose()}
    if($script:display){$script:display.Dispose()}
    [DeskNative]::StopHook()
    [DeskNative]::StopPad()
    if($script:hwnd){1..3 | ForEach-Object {[DeskNative]::UnregisterHotKey($script:hwnd,$_) | Out-Null}}
    if($script:audioJob){$script:audioJob.PowerShell.Stop(); $script:audioJob.PowerShell.Dispose()}
    if($script:player){$script:player.Close()}
    if($script:audioDir){Get-ChildItem $script:audioDir -Filter '*.mp3' | Remove-Item -Force -ErrorAction SilentlyContinue}
    if($script:tray){$script:tray.Dispose()}
    if($script:ownsMutex){$script:mutex.ReleaseMutex()}
    if($script:mutex){$script:mutex.Dispose()}
}
