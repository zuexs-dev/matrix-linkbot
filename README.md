# matrix-link-bot

A Matrix bot that watches rooms for Instagram and YouTube links and re-posts them as inline videos. It uses [matrix-nio](https://github.com/matrix-nio/matrix-nio) and [yt-dlp](https://github.com/yt-dlp/yt-dlp), and runs natively under systemd without Docker.

> [!WARNING]
> This is basically 100% vibe coded using Claude and Qwen 3.8 27B. Installer beware.

## Features

| Feature | Details |
|---|---|
| Instagram | Posts, reels and TV links. Tracking params (`?igsh=` etc.) are stripped before download. |
| YouTube | `watch`, `shorts`, `live`, `embed`, `youtu.be` and `music.` links. Resolution steps down from 1080p to 144p until the video fits `MAX_MB`. |
| Long videos | Videos over `YT_MAX_MIN` get a prompt: ✅ send as a file, 🎵 send as audio (MP3, 192 kbps), ❌ cancel. Only the person who posted the link can answer, and the prompt expires after 10 minutes. |
| iOS-safe video | VP9 and AV1 are transcoded to H.264 with `+faststart`, because Element X on iOS otherwise plays them as audio only. H.264 sources are remuxed without re-encoding. |
| Status reactions | ⏳ while working, ❌ on failure. |
| Thanks | Reacts ❤️ to messages like "thanks linkbot" or "ty link bot". |
| Archive | Optional copy of every delivered file to `ARCHIVE_DIR`, such as an NFS mount. |
| Invites | Auto-joins rooms when invited by a user on its own homeserver. |

Edits, quoted reply fallbacks and messages sent while the bot was offline are ignored. Live streams are rejected.

## Requirements

- Debian 13 or similar, with Python 3.11 or newer
- `ffmpeg` and `ffprobe`
- [Deno](https://deno.com/), which yt-dlp needs to solve YouTube's JS challenges
- A Matrix account for the bot
- Synapse `max_upload_size` at least `FILE_MAX_MB`, for example `max_upload_size: 500M`

## Install

```bash
sudo apt install -y python3-venv ffmpeg git
```

```bash
sudo useradd --system --home /opt/matrix-link-bot --shell /usr/sbin/nologin linkbot
```

```bash
sudo git clone https://github.com/zuexs-dev/matrix-linkbot /opt/matrix-link-bot && sudo chown -R linkbot: /opt/matrix-link-bot
```

```bash
sudo -u linkbot bash -c 'cd /opt/matrix-link-bot && python3 -m venv venv && venv/bin/pip install -r requirements.txt && mkdir -p store downloads'
```

Install Deno system-wide so the `linkbot` user can find it on `PATH`:

```bash
curl -fsSL https://deno.land/install.sh | sudo DENO_INSTALL=/usr/local sh
```

## Configure

```bash
sudo install -m 600 /opt/matrix-link-bot/env.example /etc/matrix-link-bot.env && sudo nano /etc/matrix-link-bot.env
```

| Variable | Default | Description |
|---|---|---|
| `HOMESERVER` | — | Homeserver URL |
| `BOT_USER` | — | Full MXID, e.g. `@linkbot:example.org` |
| `BOT_PASSWORD` | — | Used only for the first login. The session is then saved to `store/session.json`. |
| `MAX_MB` | `100` | Inline video cap |
| `FILE_MAX_MB` | `500` | Cap for file and audio sends |
| `YT_MAX_MIN` | `30` | YouTube length that triggers the prompt |
| `ARCHIVE_DIR` | unset | Archive destination. Unset disables archiving. |

Optional: put a Netscape-format `cookies.txt` in `/opt/matrix-link-bot/` and yt-dlp will use it.

## Run

```bash
sudo cp /opt/matrix-link-bot/systemd/matrix-link-bot.service /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl enable --now matrix-link-bot
```

If you use `ARCHIVE_DIR`, also install the drop-in that makes the archive path writable under `ProtectSystem=strict`. Edit the path inside it if yours differs:

```bash
sudo cp -r /opt/matrix-link-bot/systemd/matrix-link-bot.service.d /etc/systemd/system/ && sudo systemctl daemon-reload && sudo systemctl restart matrix-link-bot
```

Logs:

```bash
sudo journalctl -u matrix-link-bot -f
```

Invite the bot to a room and post a link.

## Updating yt-dlp

Sites change often. When downloads start failing, update yt-dlp first:

```bash
sudo -u linkbot /opt/matrix-link-bot/venv/bin/pip install -U "yt-dlp[default]" && sudo systemctl restart matrix-link-bot
```

`requirements.lock.txt` lists the exact versions of a known-working deployment.

## Known limitations

- Instagram photo-only posts aren't supported, because yt-dlp only handles video.
- Encrypted rooms aren't supported, because the bot doesn't use E2EE.
- Pending long-video prompts are held in memory and lost on restart.
- Paths are hardcoded to `/opt/matrix-link-bot` (`BASE` in `bot.py`).

## Built on open source

This bot is a thin layer of glue. The heavy lifting is done by these projects, and they deserve the credit:

| Project | What it does here | License |
|---|---|---|
| [matrix-nio](https://github.com/matrix-nio/matrix-nio) | Matrix client: login, sync, uploads, reactions | ISC |
| [yt-dlp](https://github.com/yt-dlp/yt-dlp) | Extracts and downloads video from Instagram and YouTube | Unlicense |
| [FFmpeg](https://ffmpeg.org) | Probing, transcoding to H.264, thumbnails, MP3 extraction | LGPL-2.1+ / GPL-2.0+ |
| [x264](https://www.videolan.org/developers/x264.html) | The H.264 encoder behind the iOS-safe transcode | GPL-2.0+ |
| [Deno](https://deno.com) | Lets yt-dlp solve YouTube's JS challenges | MIT |
| [Python](https://www.python.org) | Language and asyncio runtime | PSF-2.0 |
| [aiohttp](https://github.com/aio-libs/aiohttp) | Async HTTP underneath matrix-nio | Apache-2.0 |
| [Synapse](https://github.com/element-hq/synapse) | The Matrix homeserver this runs against | AGPL-3.0 |
| [Matrix](https://matrix.org) | The open, decentralized protocol that makes all of it possible | Apache-2.0 (spec) |

If this bot is useful to you, consider supporting or contributing to them.

## License

Licensed under the [Apache License, Version 2.0](LICENSE). See [NOTICE](NOTICE) for attribution.

The projects listed above are separate works under their own licenses and are not included in this repository.
