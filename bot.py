#!/usr/bin/env python3
import os
import asyncio
import logging
import re
import tempfile
import uuid
from pathlib import Path

import yt_dlp
from shazamio import Shazam
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
    ContextTypes,
)

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO,
)
logger = logging.getLogger(__name__)

BOT_TOKEN = os.environ.get("BOT_TOKEN", "")

URL_REGEX = re.compile(
    r"https?://(?:www\.)?"
    r"(?:youtube\.com/(?:watch|shorts|embed|live)|youtu\.be/"
    r"|instagram\.com/"
    r"|tiktok\.com/"
    r"|(?:twitter|x)\.com/"
    r"|(?:facebook\.com|fb\.watch)/"
    r")\S+",
    re.IGNORECASE,
)

# In-memory URL store (url_key → url) to keep callback_data short
URL_STORE: dict[str, str] = {}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB Telegram limit


# ─── helpers ────────────────────────────────────────────────────────────────

def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "instagram.com" in u:
        return "instagram"
    if "tiktok.com" in u:
        return "tiktok"
    if "twitter.com" in u or "x.com" in u:
        return "twitter"
    if "facebook.com" in u or "fb.watch" in u:
        return "facebook"
    return "other"


def _sync_download(ydl_opts: dict, url: str):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)


def build_ydl_opts(tmpdir: str, quality: str) -> dict:
    base = {
        "outtmpl": os.path.join(tmpdir, "%(title).80s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }
    if quality == "audio":
        base["format"] = "bestaudio/best"
        base["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
    elif quality == "360":
        base["format"] = (
            "bestvideo[height<=360][ext=mp4]+bestaudio[ext=m4a]"
            "/best[height<=360][ext=mp4]/best[height<=360]/best"
        )
        base["merge_output_format"] = "mp4"
    elif quality == "1080":
        base["format"] = (
            "bestvideo[height<=1080][ext=mp4]+bestaudio[ext=m4a]"
            "/best[height<=1080][ext=mp4]/best[height<=1080]/best"
        )
        base["merge_output_format"] = "mp4"
    else:  # default 720
        base["format"] = (
            "bestvideo[height<=720][ext=mp4]+bestaudio[ext=m4a]"
            "/best[height<=720][ext=mp4]/best[height<=720]/best"
        )
        base["merge_output_format"] = "mp4"
    return base


# ─── command handlers ────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Salom! Media yuklovchi botga xush kelibsiz!*\n\n"
        "📹 *Qo'llab-quvvatlanadigan platformalar:*\n"
        "• 🔴 YouTube — video yoki audio\n"
        "• 📷 Instagram — post / reel / story\n"
        "• 🎵 TikTok — watermarksiz\n"
        "• 🐦 Twitter / X\n"
        "• 👥 Facebook\n\n"
        "🎵 *Shazam funksiyasi:*\n"
        "Ovozli xabar yoki audio yuboring → qo'shiq aniqlanadi!\n\n"
        "💡 *Foydalanish:* Shunchaki link yoki ovozli xabar yuboring."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Yordam*\n\n"
        "*1. Video / Audio yuklash:*\n"
        "• Linkni yuboring — bot platformani o'zi aniqlaydi\n"
        "• YouTube uchun sifat tanlash oynasi chiqadi\n\n"
        "*2. Musiqa aniqlash 🎵 (Shazam):*\n"
        "• Ovozli xabar yuboring\n"
        "• Bot qo'shiq va ijrochi nomini topadi\n\n"
        "*3. Buyruqlar:*\n"
        "/start — boshlash\n"
        "/help — yordam\n\n"
        "*⚠️ Cheklovlar:*\n"
        "• Fayl hajmi ≤ 50 MB\n"
        "• Xususiy / yopiq postlar yuklanmaydi"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ─── URL handler ─────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    matches = URL_REGEX.findall(update.message.text)
    if not matches:
        return

    url = matches[0]
    platform = detect_platform(url)

    if platform == "youtube":
        url_key = uuid.uuid4().hex[:10]
        URL_STORE[url_key] = url

        keyboard = [
            [
                InlineKeyboardButton("📹 360p", callback_data=f"dl:360:{url_key}"),
                InlineKeyboardButton("📹 720p", callback_data=f"dl:720:{url_key}"),
                InlineKeyboardButton("📹 1080p", callback_data=f"dl:1080:{url_key}"),
            ],
            [
                InlineKeyboardButton("🎵 Faqat audio (MP3)", callback_data=f"dl:audio:{url_key}"),
            ],
        ]
        await update.message.reply_text(
            "🎬 *YouTube* — sifatni tanlang:",
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="Markdown",
        )
    else:
        icon = {
            "instagram": "📷",
            "tiktok": "🎵",
            "twitter": "🐦",
            "facebook": "👥",
        }.get(platform, "🌐")
        await update.message.reply_text(f"{icon} Yuklanmoqda... iltimos kuting ⏳")
        await download_and_send(update, context, url, quality="720")


async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":", 2)
    if len(parts) != 3 or parts[0] != "dl":
        return

    _, quality, url_key = parts
    url = URL_STORE.get(url_key)
    if not url:
        await query.edit_message_text("❌ URL eskirgan. Linkni qaytadan yuboring.")
        return

    await download_and_send(update, context, url, quality=quality, callback_query=query)


# ─── download & send ─────────────────────────────────────────────────────────

async def download_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    quality: str = "720",
    callback_query=None,
):
    if callback_query:
        status_msg = await callback_query.edit_message_text("⏬ Yuklanmoqda... iltimos kuting")
        reply_target = callback_query.message
    else:
        status_msg = await update.message.reply_text("⏬ Yuklanmoqda... iltimos kuting")
        reply_target = update.message

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = build_ydl_opts(tmpdir, quality)

        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: _sync_download(ydl_opts, url))

            if not info:
                await status_msg.edit_text("❌ Yuklab bo'lmadi. Link noto'g'ri bo'lishi mumkin.")
                return

            title = (info.get("title") or "media")[:100]

            files = [f for f in Path(tmpdir).iterdir() if f.is_file()]
            if not files:
                await status_msg.edit_text("❌ Fayl topilmadi.")
                return

            filepath = files[0]
            size = filepath.stat().st_size

            if size > MAX_FILE_SIZE:
                mb = size // (1024 * 1024)
                await status_msg.edit_text(
                    f"❌ Fayl hajmi {mb} MB — Telegram limiti 50 MB.\n"
                    "Iltimos, past sifat tanlang."
                )
                return

            await status_msg.edit_text("📤 Yuborilmoqda...")

            with open(filepath, "rb") as fh:
                ext = filepath.suffix.lower()
                if quality == "audio" or ext in (".mp3", ".m4a", ".ogg", ".opus"):
                    await reply_target.reply_audio(fh, title=title)
                else:
                    await reply_target.reply_video(
                        fh,
                        caption=f"🎬 {title}",
                        supports_streaming=True,
                    )

            await status_msg.delete()

        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            logger.warning("yt-dlp error for %s: %s", url, msg)
            if "private" in msg.lower():
                await status_msg.edit_text("❌ Bu xususiy post, yuklab bo'lmadi.")
            elif "not available" in msg.lower() or "unavailable" in msg.lower():
                await status_msg.edit_text("❌ Video mavjud emas yoki cheklov qo'yilgan.")
            else:
                await status_msg.edit_text(f"❌ Yuklashda xatolik:\n{msg[:200]}")
        except Exception as e:
            logger.exception("Unexpected error for %s", url)
            await status_msg.edit_text(f"❌ Kutilmagan xatolik: {str(e)[:200]}")


# ─── Shazam handler ───────────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    status_msg = await update.message.reply_text("🎵 Musiqa aniqlanmoqda... iltimos kuting")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            voice_file = await context.bot.get_file(voice.file_id)
            local_path = os.path.join(tmpdir, "audio.ogg")
            await voice_file.download_to_drive(local_path)

            shazam = Shazam()
            loop = asyncio.get_event_loop()
            result = await loop.run_in_executor(
                None, lambda: asyncio.run(shazam.recognize(local_path))
            )

            if result and result.get("matches"):
                track = result.get("track", {})
                song_title = track.get("title", "Noma'lum")
                artist = track.get("subtitle", "Noma'lum ijrochi")

                # Genre
                genre = ""
                for section in track.get("sections", []):
                    for meta in section.get("metadata", []):
                        if meta.get("title") == "Genre":
                            genre = meta.get("text", "")

                response = (
                    f"🎵 *Qo'shiq topildi!*\n\n"
                    f"🎤 *Ijrochi:* {artist}\n"
                    f"🎶 *Qo'shiq:* {song_title}\n"
                )
                if genre:
                    response += f"🎼 *Janr:* {genre}\n"

                # Streaming links
                for action in track.get("hub", {}).get("actions", []):
                    if action.get("type") == "uri":
                        uri = action.get("uri", "")
                        if "spotify" in uri.lower():
                            response += f"\n🟢 [Spotify'da tinglash]({uri})"
                        elif "apple" in uri.lower():
                            response += f"\n🍎 [Apple Music'da tinglash]({uri})"

                coverart = track.get("images", {}).get("coverarthq") or track.get("images", {}).get("coverart")
                if coverart:
                    await status_msg.delete()
                    await update.message.reply_photo(
                        coverart, caption=response, parse_mode="Markdown"
                    )
                else:
                    await status_msg.edit_text(response, parse_mode="Markdown")
            else:
                await status_msg.edit_text(
                    "❓ Qo'shiq aniqlanmadi.\n"
                    "Ovoz aniqroq yoki uzunroq bo'lishi kerak."
                )

        except Exception as e:
            logger.exception("Shazam error")
            await status_msg.edit_text(f"❌ Musiqa aniqlashda xatolik: {str(e)[:150]}")


async def handle_voice_async(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Wrapper that runs Shazam recognition correctly inside running event loop."""
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    status_msg = await update.message.reply_text("🎵 Musiqa aniqlanmoqda... iltimos kuting")

    with tempfile.TemporaryDirectory() as tmpdir:
        try:
            voice_file = await context.bot.get_file(voice.file_id)
            local_path = os.path.join(tmpdir, "audio.ogg")
            await voice_file.download_to_drive(local_path)

            shazam = Shazam()
            result = await shazam.recognize(local_path)

            if result and result.get("matches"):
                track = result.get("track", {})
                song_title = track.get("title", "Noma'lum")
                artist = track.get("subtitle", "Noma'lum ijrochi")

                genre = ""
                for section in track.get("sections", []):
                    for meta in section.get("metadata", []):
                        if meta.get("title") == "Genre":
                            genre = meta.get("text", "")

                response = (
                    f"🎵 *Qo'shiq topildi!*\n\n"
                    f"🎤 *Ijrochi:* {artist}\n"
                    f"🎶 *Qo'shiq:* {song_title}\n"
                )
                if genre:
                    response += f"🎼 *Janr:* {genre}\n"

                for action in track.get("hub", {}).get("actions", []):
                    if action.get("type") == "uri":
                        uri = action.get("uri", "")
                        if "spotify" in uri.lower():
                            response += f"\n🟢 [Spotify'da tinglash]({uri})"
                        elif "apple" in uri.lower():
                            response += f"\n🍎 [Apple Music'da tinglash]({uri})"

                coverart = (
                    track.get("images", {}).get("coverarthq")
                    or track.get("images", {}).get("coverart")
                )
                if coverart:
                    await status_msg.delete()
                    await update.message.reply_photo(
                        coverart, caption=response, parse_mode="Markdown"
                    )
                else:
                    await status_msg.edit_text(response, parse_mode="Markdown")
            else:
                await status_msg.edit_text(
                    "❓ Qo'shiq aniqlanmadi.\n"
                    "Ovoz aniqroq yoki uzunroq bo'lishi kerak."
                )

        except Exception as e:
            logger.exception("Shazam error")
            await status_msg.edit_text(f"❌ Musiqa aniqlashda xatolik: {str(e)[:150]}")


# ─── main ─────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN muhit o'zgaruvchisi o'rnatilmagan!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice_async))
    app.add_handler(MessageHandler(filters.AUDIO, handle_voice_async))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot ishga tushdi (polling mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
