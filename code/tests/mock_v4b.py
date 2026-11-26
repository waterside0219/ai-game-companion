"""Three real local Python processes, fake tmux/state only. No deployment script is executed."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
OUT = ROOT / '.private/evidence-v4b'


def fake_tmux(args, **kwargs):
    import json
    from types import SimpleNamespace
    state = json.loads((OUT/'tmux-state.json').read_text())
    assert args[0] == 'tmux'
    operation = args[3]
    output = '%1 '+state['command'] if operation == 'display-message' else state['pane'] if operation == 'capture-pane' else ''
    if operation == 'send-keys':
        record = {'literal': '-l' in args, 'value': args[-1]}
        with (OUT/'injected.jsonl').open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=False)+'\n')
    return SimpleNamespace(returncode=0, stdout=output)


def role(name):
    if name == 'entry':
        from entry.desk_entry import DeskEntry, make_server
        entry = DeskEntry(state_file=OUT/'work-state.json', run=fake_tmux, database=OUT/'events.sqlite3')
        server = make_server(entry)
        try:
            server.serve_forever()
        finally:
            server.server_close(); entry.close()
    elif name == 'status':
        import json
        from entry.status_reporter import Reporter, is_idle, post_json
        def post(path, body):
            result = post_json(path, body)
            with (OUT/'status.jsonl').open('a', encoding='utf-8') as stream:
                stream.write(json.dumps(body)+'\n')
            return result
        Reporter(probe=lambda: is_idle(OUT/'work-state.json', fake_tmux), post=post).run()
    else:
        raise ValueError('unknown mock role')


def main():
    import json
    import os
    import shlex
    import socket
    import subprocess
    import time

    OUT.mkdir(exist_ok=True)
    for port in (8833, 8798):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    for name in ('injected.jsonl','status.jsonl','events.sqlite3','events.sqlite3-journal'):
        (OUT/name).unlink(missing_ok=True)
    (OUT/'work-state.json').write_text(json.dumps({'state': 'idle', 'at': time.time()}))
    (OUT/'tmux-state.json').write_text(json.dumps({'command': 'claude', 'pane': 'mock prompt'}))
    config = OUT/'mock.toml'
    config.write_text('host="127.0.0.1"\nport=8833\ndevice_token="mock-device-token"\n'
                      'log_path=".private/evidence-v4b/judge.jsonl"\n')
    env = {'PATH': os.defpath, 'DESK_PROVIDERS': 'mock'}
    commands = [
        [sys.executable, '-B', '-S', '-m', 'judge.judge', '--config', str(config)],
        [sys.executable, '-B', '-S', str(Path(__file__).resolve()), '--role', 'entry'],
        [sys.executable, '-B', '-S', str(Path(__file__).resolve()), '--role', 'status'],
    ]
    children, streams = [], []

    def curl(*args, output=None):
        command = ['curl', '--silent', '--show-error', '--fail', '--max-time', '10', *args]
        print('$ '+shlex.join(command), flush=True)
        data = subprocess.check_output(command)
        if output:
            output.write_bytes(data); print(f'{output.name}: {len(data)} bytes', flush=True)
        else:
            print(data.decode(), flush=True)
        return data

    try:
        for name, command in zip(('judge','entry','status'), commands):
            stream = (OUT/(name+'-process.log')).open('w')
            streams.append(stream)
            children.append(subprocess.Popen(command, cwd=ROOT, env=env, stdout=stream, stderr=stream))
        deadline = time.monotonic()+8
        while True:
            if any(child.poll() is not None for child in children):
                raise RuntimeError('mock process exited; see process logs')
            try:
                with socket.create_connection(('127.0.0.1',8798),timeout=.1):
                    ready = (OUT/'status.jsonl').exists() and 'true' in (OUT/'status.jsonl').read_text()
                if ready:
                    break
            except OSError:
                pass
            if time.monotonic()>deadline:
                raise RuntimeError('mock services did not become ready')
            time.sleep(.05)
        auth = ['-H', 'Authorization: Bearer mock-device-token']
        base = 'http://127.0.0.1:8833'
        heartbeat = {'ts':time.time(),'paused':False,'talk_level':'normal',
                     'fg_exe':'mock-game.exe','fg_path':'C:/Games/mock-game.exe','is_game':True}
        curl(*auth, '-H','Content-Type: application/json','-d',json.dumps(heartbeat),base+'/v1/heartbeat')
        meta = {'ts':time.time(),'kind':'auto','fg_exe':'mock-game.exe','is_game':True,
                'dhash':'0000000000000000','capture_black':False}
        result = json.loads(curl(*auth,'-F',f'image=@{ROOT}/tests/frames/01.jpg;type=image/jpeg',
                            '-F','meta='+json.dumps(meta),base+'/v1/frame'))
        assert result['sent'], result
        records = [json.loads(line) for line in (OUT/'injected.jsonl').read_text().splitlines()]
        literals = [record['value'] for record in records if record['literal']]
        assert len(literals)==1 and '\n' not in literals[0]
        assert f'event_id={result["event_id"]}' in literals[0] and '画面：角色倒在地上' in literals[0]
        assert records[-1] == {'literal':False,'value':'Enter'}
        print('FAKE TMUX INJECTION: '+literals[0],flush=True)
        print('STATUS LOG: '+(OUT/'status.jsonl').read_text().strip(),flush=True)
        text = 'mock 验收：已收到这一帧'
        command = [sys.executable,'-B','-S',str(ROOT/'entry/desk_say.py'),result['event_id'],text]
        print('$ '+shlex.join(command),flush=True)
        assert subprocess.check_output(command,cwd=ROOT,env=env,text=True).strip()=='ok'
        print('ok',flush=True)
        says = json.loads(curl(*auth,base+'/v1/say?after=0'))
        assert says == [{'id':1,'text':text,'audio_url':'/v1/audio/1.mp3'}], says
        audio = curl(*auth,base+says[0]['audio_url'],output=OUT/'reply.mp3')
        assert audio==(ROOT/'judge/mock_silence.mp3').read_bytes()
        command = ['ps','-o','pid=,rss=,command=','-p',','.join(str(child.pid) for child in children)]
        print('$ '+shlex.join(command),flush=True)
        measured = subprocess.check_output(command,text=True)
        print(measured.strip(),flush=True)
        rows = measured.splitlines()
        assert len(rows)==3
        total = sum(int(row.split()[1]) for row in rows)
        print(f'TOTAL RSS: {total} KiB = {total/1024:.2f} MiB; budget < 80 MB (80000000 bytes)',flush=True)
        assert total*1024<80000000, 'three-process memory budget exceeded'
        print('PASS: judge -> desk_entry -> fake tmux; explicit desk_say -> say + mp3; status reporter; memory',flush=True)
    finally:
        for child in children:
            if child.poll() is None:
                child.terminate()
        for child in children:
            child.wait(timeout=5)
        for stream in streams:
            stream.close()


if __name__ == '__main__':
    if len(sys.argv)==3 and sys.argv[1]=='--role':
        role(sys.argv[2])
    else:
        main()
