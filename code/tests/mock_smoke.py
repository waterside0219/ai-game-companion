"""Local-only v4 curl acceptance. Never loads credentials or calls real providers."""
import json
import os
from pathlib import Path
import socket
import shlex
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / '.private/evidence'
OUT.mkdir(parents=True, exist_ok=True)


def curl(args, output=None):
    command = ['curl', '--silent', '--show-error', '--fail', '--max-time', '30', *args]
    print('$ ' + shlex.join(command), flush=True)
    result = subprocess.run(command, check=True, capture_output=True)
    if output:
        output.write_bytes(result.stdout)
        print(f'{output.name}: {len(result.stdout)} bytes', flush=True)
    else:
        print(result.stdout.decode(), flush=True)
    return result.stdout


def main():
    # Fixed local ports are the specified entry point. Never stop another process.
    for port in (8833, 8798):
        with socket.socket() as probe:
            probe.bind(('127.0.0.1', port))
    config = OUT / 'mock.toml'
    config.write_text('host = "127.0.0.1"\nport = 8833\n'
        'device_token = "mock-device-token"\nlog_path = ".private/evidence/mock.jsonl"\n')
    env = {'PATH': os.defpath, 'DESK_PROVIDERS': 'mock'}
    processes = []
    try:
        for module, extra in [('judge.fake_ai', []), ('judge.judge', ['--config', str(config)])]:
            processes.append(subprocess.Popen([sys.executable, '-m', module, *extra],
                cwd=ROOT, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE))
        for attempt in range(40):
            try:
                with socket.create_connection(('127.0.0.1', 8833), timeout=.1):
                    break
            except OSError:
                if any(proc.poll() is not None for proc in processes):
                    raise RuntimeError('mock process failed to start')
                time.sleep(.05)
        auth = ['-H', 'Authorization: Bearer mock-device-token']
        base = 'http://127.0.0.1:8833'
        now = time.time()
        heartbeat = json.dumps({'ts': now, 'paused': False, 'talk_level': 'normal',
            'fg_exe': 'mock-game.exe', 'fg_path': 'C:/Games/mock-game.exe', 'is_game': True})
        curl([*auth, '-H', 'Content-Type: application/json', '-d', heartbeat, base + '/v1/heartbeat'])
        # Fixture comes from a generated color card; no user screenshot is used.
        frame = ROOT / 'tests/frames/01.jpg'
        meta = json.dumps({'ts': time.time(), 'kind': 'auto', 'fg_exe': 'mock-game.exe',
            'is_game': True, 'dhash': '0000000000000000', 'capture_black': False})
        sent = json.loads(curl([*auth, '-F', f'image=@{frame};type=image/jpeg',
            '-F', 'meta=' + meta, base + '/v1/frame']))
        assert sent['sent'], sent
        says = json.loads(curl([*auth, base + '/v1/say?after=0']))
        assert says[0]['text'].startswith('收到：')
        audio = OUT / 'mock.mp3'
        curl([*auth, base + says[0]['audio_url']], output=audio)
        assert audio.read_bytes() == (ROOT / 'judge/mock_silence.mp3').read_bytes()
        # Fill the 20-entry queue to include its steady-state footprint in ps.
        for index in range(21):
            request = urllib.request.Request(base + '/v1/reply',
                data=json.dumps({'text': f'mock memory {index}'}).encode(),
                headers={'Content-Type': 'application/json'}, method='POST')
            with urllib.request.urlopen(request, timeout=3) as response:
                assert response.status == 200
        pid = processes[-1].pid
        command = ['ps', '-o', 'pid=,rss=,comm=', '-p', str(pid)]
        print('$ ' + ' '.join(command))
        output = subprocess.check_output(command, text=True)
        print(output.strip())
        rss = int(output.split()[1])
        print(f'RSS = {rss} KiB = {rss / 1024:.2f} MiB; limit < 60 MiB')
        assert rss < 60 * 1024
        if '--long-poll' in sys.argv:
            started = time.monotonic()
            empty = json.loads(curl([*auth, base + '/v1/say?after=22']))
            elapsed = time.monotonic() - started
            assert empty == [] and 24.5 <= elapsed < 28, (empty, elapsed)
            print(f'Empty long-poll returned [] after {elapsed:.3f}s (25s contract)')
        print('PASS: heartbeat -> frame -> fake AI -> say -> mp3; mock queue memory < 60 MiB')
    finally:
        for proc in processes:
            proc.terminate()
        for proc in processes:
            proc.wait(timeout=5)
            proc.stderr.close()


if __name__ == '__main__':
    main()
