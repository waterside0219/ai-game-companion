"""Called only when the AI chooses to say the supplied words."""
import argparse
from functools import partial
import http.client
import sys

if __package__:
    from .status_reporter import post_json
else:
    from status_reporter import post_json


def main(argv=None, post=partial(post_json, timeout=20)):
    parser = argparse.ArgumentParser(description='把 AI 写的一句话送到电脑字幕和声音。')
    parser.add_argument('event_id')
    parser.add_argument('text')
    args = parser.parse_args(argv)
    if not args.event_id.strip() or not args.text.strip():
        print('失败：event_id 和文本不能为空。', file=sys.stderr)
        return 1
    text = args.text
    if len(text) > 120:
        text = text[:120]
        print('提示：文本超过 120 字，已截断。', file=sys.stderr)
    try:
        # Real TTS can take 15s in judge; let the local reply complete before reporting failure.
        post('/v1/reply', {'event_id': args.event_id, 'text': text})
    except (OSError, ValueError, http.client.HTTPException) as error:
        print(f'失败：{error}', file=sys.stderr)
        return 1
    print('ok')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
