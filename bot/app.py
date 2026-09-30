import asyncio
import logging
import os
import shutil
import tempfile
import time
import uuid
from pathlib import Path

from dotenv import load_dotenv
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.constants import ParseMode
from telegram.ext import Application, CallbackQueryHandler, CommandHandler, ContextTypes, MessageHandler, filters

from bot.detect import classify, find_urls
from bot.services import UPLOAD_LIMIT, ProviderError, card_text, download_item, human_size, inspect

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("video-download")

STORE: dict[str, tuple[float, dict]] = {}
BUSY: set[int] = set()
ROOT = Path(tempfile.gettempdir()) / "video-download"
STORE_TTL_SECONDS = 1800


def _purge_store() -> None:
    now = time.monotonic()
    expired = [token for token, (created, _) in STORE.items() if now - created > STORE_TTL_SECONDS]
    for token in expired:
        STORE.pop(token, None)


def _bar(percent: float | None, detail: str) -> str:
    if percent is None:
        return f"Downloading\n{detail}"
    filled = int(percent // 10)
    return f"<b>{detail}</b>\n{'▓' * filled}{'░' * (10 - filled)} {percent:.0f}%"


def _keyboard(token: str, probe: dict) -> InlineKeyboardMarkup:
    rows = []
    items = probe["items"]
    if len(items) == 1:
        rows.append(
            [
                InlineKeyboardButton("Play in chat", callback_data=f"p:{token}:0"),
                InlineKeyboardButton("Save file", callback_data=f"d:{token}:0"),
            ]
        )
        if items[0]["kind"] == "video":
            rows.append(
                [
                    InlineKeyboardButton("720", callback_data=f"q:{token}:0:720"),
                    InlineKeyboardButton("480", callback_data=f"q:{token}:0:480"),
                    InlineKeyboardButton("360", callback_data=f"q:{token}:0:360"),
                    InlineKeyboardButton("Audio", callback_data=f"a:{token}:0"),
                ]
            )
    else:
        for index, item in enumerate(items[:12]):
            label = item["label"][:28]
            rows.append(
                [
                    InlineKeyboardButton(f"Play {index + 1}. {label}", callback_data=f"p:{token}:{index}"),
                    InlineKeyboardButton("Save", callback_data=f"d:{token}:{index}"),
                ]
            )
    return InlineKeyboardMarkup(rows)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await update.message.reply_text(
        "<b>Video desk</b>\n\n"
        "Paste a public Instagram or TeraBox link.\n"
        "I detect the site and send a video you can play in this chat.\n\n"
        "Instagram posts, reels, and IGTV.\n"
        "TeraBox, 1024terabox, nephobox, 4funbox, mirrobox, and the other share domains.\n\n"
        "No Instagram or TeraBox account is required.\n"
        "Private posts and password shares stay locked.\n"
        "Telegram can play files up to 50 MB. Larger videos are sent in a smaller stream.",
        parse_mode=ParseMode.HTML,
    )


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    _purge_store()

    message = update.message
    urls = [url for url in find_urls(message.text or "") if classify(url)]
    if not urls:
        await message.reply_text("Paste an Instagram or TeraBox link.")
        return
    for url in urls[:3]:
        status = await message.reply_text("Looking up the link…")
        try:
            probe = await inspect(url)
        except ProviderError as error:
            await status.edit_text(str(error))
            continue
        except Exception:
            log.exception("inspect failed")
            await status.edit_text("That link could not be opened. Try again in a moment.")
            continue
        token = uuid.uuid4().hex[:8]
        STORE[token] = (time.monotonic(), probe)
        text = card_text(probe)
        markup = _keyboard(token, probe)
        thumb = probe["items"][0].get("thumb")
        if thumb:
            try:
                await status.delete()
                await message.reply_photo(
                    photo=thumb,
                    caption=text[:1024],
                    parse_mode=ParseMode.HTML,
                    reply_markup=markup,
                )
                continue
            except Exception:
                log.info("thumbnail skipped")
        await status.edit_text(text, parse_mode=ParseMode.HTML, reply_markup=markup)


async def _send(target, path: Path, item: dict, mode: str) -> None:
    size = path.stat().st_size
    if size > UPLOAD_LIMIT:
        raise ProviderError(
            f"This file is {human_size(size)}. Telegram bots can upload 50 MB. Pick 480 or 360."
        )
    caption = f"<b>{path.name}</b>"
    if item.get("quality"):
        caption += f"\n{item['quality']}p"
    kwargs = {
        "caption": caption[:1024],
        "parse_mode": ParseMode.HTML,
        "read_timeout": 600,
        "write_timeout": 600,
        "connect_timeout": 30,
    }
    with path.open("rb") as handle:
        if mode == "audio":
            await target.reply_audio(audio=handle, **kwargs)
        elif item["kind"] == "image" or path.suffix.lower() in {".jpg", ".jpeg", ".png", ".webp"}:
            await target.reply_photo(photo=handle, **kwargs)
        elif mode == "file":
            await target.reply_document(document=handle, **kwargs)
        else:
            await target.reply_video(
                video=handle,
                supports_streaming=True,
                width=item.get("width") or None,
                height=item.get("height") or None,
                duration=item.get("duration") or None,
                **kwargs,
            )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    await query.answer()
    parts = (query.data or "").split(":")
    if len(parts) < 3 or parts[1] not in STORE:
        await query.message.reply_text("This card expired. Paste the link again.")
        return
    mode_key, token, index_text, *rest = parts
    entry = STORE.get(token)
    if entry is None:
        await query.message.reply_text("This card expired. Paste the link again.")
        return
    _, probe = entry
    try:
        item = probe["items"][int(index_text)]
    except (ValueError, IndexError):
        await query.message.reply_text("That file is not on the card anymore.")
        return
    chat_id = query.message.chat_id
    if chat_id in BUSY:
        await query.message.reply_text("Still finishing the previous download.")
        return
    mode = {"p": "play", "d": "file", "a": "audio", "q": "play"}[mode_key]
    quality = rest[0] if rest else "auto"
    BUSY.add(chat_id)
    folder = ROOT / uuid.uuid4().hex
    status = await query.message.reply_text("Starting…")

    async def progress(percent: float | None, detail: str) -> None:
        try:
            await status.edit_text(_bar(percent, detail), parse_mode=ParseMode.HTML)
        except Exception:
            pass

    try:
        await progress(None, "Starting")
        path = await download_item(item, folder, mode, quality, progress)
        await progress(100, "Sending to Telegram")
        await _send(query.message, path, item, mode)
        try:
            await status.edit_text("Ready. The file is in the chat above.", reply_markup=_keyboard(token, probe))
        except Exception:
            pass
        STORE.pop(token, None)
    except ProviderError as error:
        await query.message.reply_text(str(error)[:500])
    except Exception:
        log.exception("download failed")
        await query.message.reply_text("Download failed. Paste the link again and retry.")
    finally:
        BUSY.discard(chat_id)
        shutil.rmtree(folder, ignore_errors=True)


def main() -> None:
    load_dotenv()
    token = os.getenv("BOT_TOKEN", "").strip()
    if not token or token.startswith("123456:"):
        raise SystemExit("Put your BotFather token in .env as BOT_TOKEN=...")
    if shutil.which("ffmpeg") is None:
        raise SystemExit("ffmpeg is not on PATH. Install it, then start the bot again.")
    ROOT.mkdir(parents=True, exist_ok=True)
    app = (
        Application.builder()
        .token(token)
        .connect_timeout(30)
        .read_timeout(600)
        .write_timeout(600)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(CallbackQueryHandler(on_button))
    log.info("bot is running")
    app.run_polling(allowed_updates=Update.ALL_TYPES)

