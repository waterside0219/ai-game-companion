"""Shared fail-closed idle probe and the local five-second status reporter."""
import http.client
import os
import json
import re
from pathlib import Path
import subprocess
import sys
import time

# AI 跑在哪个 tmux 窗格里。DESK_TMUX_SOCKET 不设就用 tmux 默认的 socket。
SOCK = os.environ.get('DESK_TMUX_SOCKET') or os.path.join(os.environ.get('TMUX_TMPDIR', '/tmp'), f'tmux-{os.getuid()}', 'default')
TARGET = os.environ.get('DESK_TMUX_TARGET', 'ai')
# AI 那头的钩子写的忙/闲状态：{"state": "work" 或 "idle", "at": 时间戳}
STATE_FILE = Path(os.environ.get('DESK_WORK_STATE', Path(__file__).resolve().parents[1] / '.private' / 'work-state.json'))
# Entry's successful injection includes a one-second wait; keep probes within judge's 2s HTTP timeout.
TMUX_TIMEOUT = 0.15


def tmux(args, run=subprocess.run):
    result = run(['tmux', '-S', SOCK, *args], capture_output=True, text=True,
                 encoding='utf-8', errors='replace', timeout=TMUX_TIMEOUT, check=False)
    if result.returncode != 0:
        raise OSError('tmux command unavailable')
    return result.stdout


def claude_pane(run=subprocess.run, target=TARGET):
    try:
        value = tmux(['display-message', '-p', '-t', target, '#{pane_id} #{pane_current_command}'], run).strip()
        pane, command = value.split(' ', 1)
        return pane if re.fullmatch(r'%[0-9]+', pane) and command == 'claude' else None
    except (OSError, subprocess.SubprocessError, ValueError, AttributeError):
        return None


SPINNER = re.compile(r'…\s*\((\d+m\s*)?\d+s\b')


def idle_pane(state_file=STATE_FILE, run=subprocess.run):
    """Return the checked pane identity, so checking and injection use the same pane."""
    try:
        pane = claude_pane(run)
        if pane is None:
            return None
        state = json.loads(Path(state_file).read_text(encoding='utf-8'))
        output = tmux(['capture-pane', '-p', '-t', pane], run)
        tail = '\n'.join(output.splitlines()[-15:])
        # newer Claude Code no longer prints "esc to interrupt"; the live spinner line looks like
        # "✢ Photosynthesizing… (1m 18s · ↓ 1.8k tokens". A stale "work" state (hook fired by another
        # process, no Stop) kept the player's voice queued for 10 minutes, so the pane itself is the authority.
        if 'esc to interrupt' in tail.casefold() or SPINNER.search(tail):
            return None
        if isinstance(state, dict) and state.get('state') == 'work' and time.time() - float(state.get('at', 0)) < 15:
            return None
        return pane
    except (OSError, subprocess.SubprocessError, ValueError, TypeError, AttributeError):
        return None


def is_idle(state_file=STATE_FILE, run=subprocess.run):
    return idle_pane(state_file, run) is not None


def post_json(path, value, *, port=8833, timeout=2):
    """Fixed loopback destination; no proxy, token, provider or remote transport."""
    connection = http.client.HTTPConnection('127.0.0.1', port, timeout=timeout)
    try:
        connection.request('POST', path, json.dumps(value, ensure_ascii=False).encode('utf-8'),
                           {'Content-Type': 'application/json'})
        response = connection.getresponse()
        body = response.read(65537)
        if response.status != 200:
            raise OSError(f'desk HTTP {response.status}')
        if len(body) > 65536:
            raise ValueError('desk response too large')
        result = json.loads(body)
        if not isinstance(result, dict) or result.get('ok') is not True:
            raise ValueError('desk did not acknowledge request')
        return result
    finally:
        connection.close()


class Reporter:
    def __init__(self, probe=is_idle, post=post_json, clock=time.monotonic, log=None):
        self.probe, self.post, self.clock = probe, post, clock
        self.log = log or (lambda text: print(text, file=sys.stderr, flush=True))
        self.last_idle = None
        self.last_attempt = None
        self.last_log = None

    def tick(self):
        now = self.clock()
        try:
            idle = self.probe() is True
        except Exception:
            idle = False
        if self.last_attempt is None or idle != self.last_idle or now - self.last_attempt >= 5:
            self.last_idle, self.last_attempt = idle, now
            try:
                self.post('/v1/status', {'idle': idle})
            except (OSError, ValueError, http.client.HTTPException):
                if self.last_log is None or now - self.last_log >= 60:
                    self.log('desk status unavailable; retrying quietly')
                    self.last_log = now
        return idle

    def run(self):
        # Poll for changes every second; a stable state is posted every five seconds.
        while True:
            started = self.clock()
            self.tick()
            time.sleep(max(0, 1 - (self.clock() - started)))


if __name__ == '__main__':
    try:
        Reporter().run()
    except KeyboardInterrupt:
        pass
