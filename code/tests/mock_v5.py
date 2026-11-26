"""v5 curl integration using the v4b three-process fake-tmux fixture; loopback only."""
import sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
import mock_v4b as old
OUT=ROOT/'.private/evidence/v5-e2e'
old.OUT=OUT


def main():
    import json,os,shlex,socket,subprocess,time,wave
    OUT.mkdir(exist_ok=True)
    for port in (8833,8798):
        with socket.socket() as probe:probe.bind(('127.0.0.1',port))
    for name in ('injected.jsonl','status.jsonl','events.sqlite3','events.sqlite3-journal'):(OUT/name).unlink(missing_ok=True)
    (OUT/'work-state.json').write_text('{"state":"idle"}')
    (OUT/'tmux-state.json').write_text('{"command":"claude","pane":"mock prompt"}')
    (OUT/'mock.toml').write_text('host="127.0.0.1"\nport=8833\ndevice_token="mock-device-token"\nlog_path=".private/evidence/v5-e2e/judge.jsonl"\n')
    audio=OUT/'voice.wav'
    with wave.open(str(audio),'wb') as w:w.setnchannels(1);w.setsampwidth(2);w.setframerate(16000);w.writeframes(b'\0\0'*480000)
    env={'PATH':os.defpath,'DESK_PROVIDERS':'mock'}
    commands=[[sys.executable,'-B','-S','-m','judge.judge','--config',str(OUT/'mock.toml')],
              [sys.executable,'-B','-S',str(Path(__file__).resolve()),'--role','entry'],
              [sys.executable,'-B','-S',str(Path(__file__).resolve()),'--role','status']]
    children=[];streams=[]
    def curl(*args):
        command=['curl','--silent','--show-error','--fail','--max-time','22',*args]
        print('$ '+shlex.join(command),flush=True);data=subprocess.check_output(command);print(data.decode(),flush=True);return json.loads(data)
    def memory(label, enforce=True):
        command=['ps','-o','pid=,rss=,command=','-p',','.join(str(c.pid) for c in children)]
        print('$ '+shlex.join(command),flush=True);rows=subprocess.check_output(command,text=True);print(rows.strip(),flush=True)
        rss=sum(int(row.split()[1]) for row in rows.splitlines());print(f'{label}: {rss} KiB = {rss*1024/1000000:.3f} MB; limit <60 MB',flush=True)
        if enforce:assert rss*1024<60000000,(label,rss)
    try:
        for name,command in zip(('judge','entry','status'),commands):
            stream=(OUT/(name+'-process.log')).open('w');streams.append(stream);children.append(subprocess.Popen(command,cwd=ROOT,env=env,stdout=stream,stderr=stream))
        deadline=time.monotonic()+8
        while not ((OUT/'status.jsonl').exists() and 'true' in (OUT/'status.jsonl').read_text()):
            assert time.monotonic()<deadline and all(c.poll() is None for c in children),'mock startup failed'
            time.sleep(.05)
        base='http://127.0.0.1:8833';auth=['-H','Authorization: Bearer mock-device-token'];memory('startup RSS',enforce=False)
        # Original frame -> entry path still delivers through these exact three processes.
        curl(*auth,'-H','Content-Type: application/json','-d',json.dumps({'ts':time.time(),'paused':False,'talk_level':'normal','fg_exe':'mock.exe','fg_path':'C:/Games/mock.exe','is_game':True}),base+'/v1/heartbeat')
        frame=curl(*auth,'-F',f'image=@{ROOT}/tests/frames/01.jpg;type=image/jpeg','-F','meta='+json.dumps({'ts':time.time(),'kind':'auto','fg_exe':'mock.exe','is_game':True,'dhash':'0000000000000000'}),base+'/v1/frame');assert frame['sent']
        result=curl(*auth,'-F',f'audio=@{audio};type=audio/wav','-F','meta='+json.dumps({'ts':time.time(),'ms':30000}),base+'/v1/voice');assert result['sent']
        lines=[json.loads(line) for line in (OUT/'injected.jsonl').read_text().splitlines()];typed=[line['value'] for line in lines if line['literal']]
        assert len(typed)==2 and '这是玩家在电脑前按住侧键对你说的话' in typed[1] and 'mock：这是按住侧键说的话' in typed[1]
        assert all('\n' not in line for line in typed)
        print('FAKE TMUX VOICE: '+typed[1],flush=True)
        # Explicit fixture text supplied only by this test; production never generates the AI replies.
        command=[sys.executable,'-B','-S',str(ROOT/'entry/desk_say.py'),result['event_id'],'mock 验收：语音入口已送达']
        print('$ '+shlex.join(command),flush=True);assert subprocess.check_output(command,cwd=ROOT,env=env,text=True).strip()=='ok'
        says=curl(*auth,base+'/v1/say?after=0');assert says[0]['text']=='mock 验收：语音入口已送达'
        reply=subprocess.check_output(['curl','-fsS','--max-time','5',*auth,base+says[0]['audio_url']]);assert reply==(ROOT/'judge/mock_silence.mp3').read_bytes()
        memory('after 30s WAV + reply RSS')
        print('PASS: max 30s WAV curl -> mock transcribe -> real entry -> fake tmux; explicit desk_say -> say/audio; v4 frame still passes; 3-process RSS <60MB',flush=True)
    finally:
        for c in children:
            if c.poll() is None:c.terminate()
        for c in children:c.wait(timeout=5)
        for s in streams:s.close()

if __name__=='__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--role':old.role(sys.argv[2])
    else:main()
