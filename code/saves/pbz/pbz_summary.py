#!/usr/bin/env python3
"""影之刃零：读 pbz_read.mjs 解出来的目录，挑最新的存档槽，打印一行进度。

用法: pbz_summary.py <pbz_read.mjs 的输出目录>
"""
import glob
import json
import os
import sys

MAPS = {'PBZ_Xigu_WP': '悲泣溪谷'}  # 地图内部名 → 中文名，见到新的就往里加


def stamp(slot):
    return slot.get('PBZGameSystem', {}).get('DateTimeString', '') or ''


def main(out_dir):
    slots = []
    for path in glob.glob(os.path.join(out_dir, '*Save*.json')):
        try:
            with open(path, encoding='utf-8') as handle:
                slots.append((json.load(handle), os.path.basename(path)))
        except (OSError, ValueError):
            pass
    if not slots:
        raise SystemExit('没有找到存档槽')
    # 按每个槽自己写下的存档时间挑最新的。按别的时间（比如最近摸过的铃铛）挑，会挑到旧槽。
    data, name = max(slots, key=lambda item: stamp(item[0]))
    game, player, bells = data.get('PBZGameSystem', {}), data.get('Player', {}), data.get('BellManager', {})
    level = game.get('CurrentLevelName', '?')
    print('用槽', name, '存于', stamp(data))
    print(f"地图 {MAPS.get(level, level)}（{level}）| 剩余天数 {player.get('RemainingDays')} | 尘(钱) {player.get('Dust')}"
          f" | 已点铃铛 {len(bells.get('ActivateBellIDs', []))} 个，最近 {bells.get('SaveBellID')}"
          f" | 难度字节 {game.get('DifficultModeByte')} | 教程中 {game.get('IsInTutorial')}")


if __name__ == '__main__':
    if len(sys.argv) != 2:
        raise SystemExit(__doc__)
    main(sys.argv[1])
