# Video desk

Telegram bot that plays public Instagram and TeraBox videos in chat. No Instagram or TeraBox account is required.

## Run

1. Open Telegram, message [@BotFather](https://t.me/BotFather), send `/newbot`, and copy the token.
2. Copy `.env.example` to `.env` and set `BOT_TOKEN`.
3. Install [ffmpeg](https://ffmpeg.org) and make sure `ffmpeg` works in a terminal.
4. Double-click `run.bat` or run:

```bat
python -m venv .venv
.venv\Scripts\activate
pip install -r requirements.txt
python -m bot
```

5. Open your bot and paste a link.

## What it accepts

Instagram posts, reels, and IGTV (`instagram.com`, `instagr.am`, `ddinstagram.com`).

TeraBox share links, including `terabox.com`, `1024terabox.com`, `1024tera.com`, `nephobox.com`, `4funbox.com`, `mirrobox.com`, `freeterabox.com`, and the other public share domains. A `/s/` path or a `surl` query both work.

The bot detects the site from the link. Tap **Play in chat** and the video is sent so Telegram can play it. Files over 50 MB are fetched from a smaller TeraBox stream, or compressed, because that is Telegram's bot upload limit.

Private Instagram posts, expired stories, and password-protected TeraBox shares cannot be opened without an account. Only save media you are allowed to keep.
