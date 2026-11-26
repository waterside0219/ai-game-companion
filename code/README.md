# 代码

```
Windows 电脑（你在玩）                  云上一台小机器                                  AI 那头
┌──────────────────────────┐        ┌──────────────────────────────────┐        ┌────────────────────┐
│ client/desk-overlay.ps1  │ /v1/*  │ judge/judge.py   127.0.0.1:8833  │        │ tmux 里一直开着的   │
│   截屏、侧键、录音、读档  │ ─────► │   看图 → 判断值不值得 → 发事件    │        │ AI 会话             │
│ client/live2d/           │ ◄───── │   合成声音、存档、发客户端更新    │        │                    │
│   桌面上他的形象、字幕    │        │ entry/desk_entry.py 127.0.0.1:8798│ ─────► │ 闲着的时候才敲进去  │
│                          │        │   AI 闲了才把事件敲进它的窗格      │ ◄───── │ entry/desk_say.py   │
│                          │        │ entry/status_reporter.py 报忙闲   │        │   回一句话          │
└──────────────────────────┘        └──────────────────────────────────┘        └────────────────────┘
```

| 目录 | 跑在哪 | 干什么 |
|---|---|---|
| [`client/`](client) | Windows | `desk-overlay.ps1` 是主程序（PowerShell 5.1 + 内嵌 C#）：只在前台是游戏时每隔几秒截一帧（只在内存），鼠标侧键/手柄组合键"截这一帧给他看""按住说话"，字幕条，播放他的声音，盯存档文件。`live2d/` 是桌面右下角他的形象（Electron 透明窗口，点得穿）。`update-and-start.ps1` 启动前从云上拉最新的客户端文件，校验 sha256 再换。 |
| [`judge/`](judge) | 云上 | `judge.py` 收电脑传来的东西：画面先过"变没变"，再请看图模型写一句描述和"局面五栏"，再问判断模型"这会儿值不值得开口"，值得才发给入口；AI 的回话在这里合成声音，等电脑来取。`providers.py` 是调模型的地方（`DESK_PROVIDERS=mock` 时全用假的，不花钱）。 |
| [`entry/`](entry) | 云上（和 AI 同一台） | `desk_entry.py` 收事件，确认 AI 的窗格闲着（没在跑、没在打字）才敲进去，同一事件只敲一次。`desk_say.py` 是 AI 回话用的。`status_reporter.py` 每 5 秒把 AI 忙不忙告诉 judge。`desk_quiet.py` 发现电脑两分钟没送画面，就告诉 AI"电脑没动静了"，别再对着旧画面说话。 |
| [`saves/`](saves) | 哪台都行 | 两个游戏的存档读法，见 [saves/README.md](saves/README.md)。 |
| [`deploy/`](deploy) | 云上 | 三个常驻进程的启动脚本（cron 每分钟拉一次，`flock` 保证只有一个）。 |
| [`tests/`](tests) | | `python3 -m unittest discover -s tests` |

## 电脑和云上之间

电脑只认识一个地址（`config.json` 的 `base_url`），每个请求都带 `Authorization: Bearer <设备口令>`。电脑上那个程序里没有任何模型钥匙。

| | 干什么 |
|---|---|
| `POST /v1/heartbeat` | 我还在、前台是什么程序 |
| `POST /v1/frame` | 一帧画面（自动的，或者人按键要他看的） |
| `POST /v1/voice` | 按住说话录的一句 |
| `POST /v1/save` | 存档文件原样一份 |
| `GET /v1/say` | 有没有他的新回话 |
| `GET /v1/audio/<n>.mp3` | 回话的声音 |
| `GET /v1/client/manifest.json`、`/v1/client/<文件>` | 客户端更新 |

`/v1/status`（忙闲）和 `/v1/reply`（AI 回话）只收本机来的请求。

## 几个开关

| 在哪 | 是什么 |
|---|---|
| `.private/config.toml` | 设备口令、MiniMax 钥匙文件和 voice_id、`competitive_games`（列在这里的游戏，两句不要紧的话之间只隔 20 秒，别的游戏隔 40 秒；电脑上 `talk_level` 选 quiet/normal/chatty 再乘 1.5/1/0.6）、`transcribe_enabled`（按住说话要不要转字） |
| `.private/providers.env` | `DESK_PROVIDERS=real` 才真调模型；`OPENROUTER_API_KEY`；`DESK_KEYTERMS`（常说的名字，听写更准） |
| 环境变量 | `DESK_TMUX_TARGET`（AI 的 tmux 窗格，默认 `ai`）、`DESK_TMUX_SOCKET`、`DESK_WORK_STATE`（AI 忙闲状态文件）、`DESK_HANDBOOK_DIR`（每个游戏的攻略本子）、`DESK_EXTRA_RULE`（接在每条事件后面的你自己的规矩） |
