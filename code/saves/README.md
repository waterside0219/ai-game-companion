# 读档

电脑上的小程序（`client/desk-overlay.ps1`）盯着存档文件，游戏一写存档，就把文件原样传到云上，云上存最近几份，另记一个 `latest.json` 指向最新的那份。下面两个脚本在任何一台机器上读这份拷贝，打出一张进度卡给 AI 看。全程只读，从不往回写。

## 女神异闻录 5 皇家版（PC）

```
git clone https://github.com/j0nnyDiGITAL/change-of-heart saves/p5r/change-of-heart
pip install pycryptodome cryptography psutil
python3 saves/p5r/read_save.py DATA.DAT          # 进度卡
python3 saves/p5r/read_save.py DATA.DAT --json   # 给程序用
```

解析借的是 [change-of-heart](https://github.com/j0nnyDiGITAL/change-of-heart)（MIT）的 `core/`，各项数据在文件里的位置都是它整理的。

## 影之刃零（PC）

```
export PBZ_SAVE_KEY=<64 位十六进制>
node saves/pbz/pbz_read.mjs PBZSaveGame.sav out/
python3 saves/pbz/pbz_summary.py out/
```

- 存档是一个容器，目录和每块数据都用 AES-256-GCM 加密。密钥是全体玩家共用的一把，存档编辑社区公开整理过，这里不放，自己找到后放进 `PBZ_SAVE_KEY`。
- 目录和存档槽的结构照 [Duucking/pbz-save-editing-guide](https://github.com/Duucking/pbz-save-editing-guide)（MIT）的 `pbz_dump.js`。
- 有的块用 Kraken 压缩，要一个编成 wasm 的 [ooz](https://github.com/powzix/ooz)，放在 `saves/pbz/wasm/ooz.mjs`（导出 `_ooz_decompress`、`_malloc`、`_free`、`HEAPU8`）。
- `pbz_summary.py` 按每个存档槽自己写的存档时间挑最新的一个。
