"""玩家的电脑两分钟没送新画面，就告诉 AI，并把那张旧画面挪开。

电脑关机不会发"我关了"，只是不再送画面；AI 会拿着关机前最后一张 latest.jpg 当成眼前，还往电脑上说话。

cron 每分钟跑一次，不花模型额度：
- latest.jpg 比 QUIET 秒还旧 → 改名成 last-before-quiet.jpg（再读 latest.jpg 会直接读不到），
  往 AI 的 tmux 敲一条"电脑没动静了"，同一段安静只敲一次。
- 新画面来了 judge 会重新写 latest.jpg，这里什么都不用做。
"""
import os
import sys
import time
from datetime import datetime, timezone, timedelta

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from status_reporter import TARGET, claude_pane, tmux  # noqa: E402

DIR = '/dev/shm/desk'
LATEST = os.path.join(DIR, 'latest.jpg')
STALE = os.path.join(DIR, 'last-before-quiet.jpg')
QUIET = 120


def main(now=None):
    now = now or time.time()
    try:
        taken = os.path.getmtime(LATEST)
    except OSError:
        return 0  # 没有最新画面：要么从没开过，要么已经报过并挪走了
    age = now - taken
    if age < QUIET:
        return 0
    os.replace(LATEST, STALE)
    when = datetime.fromtimestamp(taken, timezone(timedelta(hours=8)))
    clock = datetime.fromtimestamp(now, timezone(timedelta(hours=8)))
    message = (f'[电脑陪玩 {clock:%H:%M}] 这是机器报的，不是玩家发的消息：玩家的电脑已经 {int(age // 60)} 分钟没送新画面了，'
               f'最后一张是 {when:%H:%M:%S} 的。多半是关了电脑或离开了。'
               '那张旧画面已挪成 /dev/shm/desk/last-before-quiet.jpg，不是此刻；'
               '别再往电脑上说话、别再发游戏的话。新画面来了会照常报。')
    pane = claude_pane()
    if pane is None:
        print('desk quiet: no claude pane', file=sys.stderr, flush=True)
        return 1
    tmux(['has-session', '-t', TARGET])
    tmux(['send-keys', '-t', pane, '-l', message])
    time.sleep(1)
    tmux(['send-keys', '-t', pane, 'Enter'])
    print(f'desk quiet: told main session, last frame {when:%H:%M:%S}', file=sys.stderr, flush=True)
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
