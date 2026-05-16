#!/usr/bin/env python3
import os
import asyncio
import logging
import re
import tempfile
import uuid
from pathlib import Path

import av
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

URL_STORE: dict[str, str] = {}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

INSTAGRAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    )
}


# ─── audio conversion (PyAV — tizimda ffmpeg kerak emas) ───────────────────

def convert_to_wav(src: str, dst: str) -> bool:
    """OGG OPUS → 16kHz mono WAV (PyAV o'zining FFmpeg bilan ishlaydi)."""
    try:
        inp = av.open(src)
        out = av.open(dst, "w", format="wav")
        try:
            out_stream = out.add_stream("pcm_s16le", rate=16000, layout="mono")
            resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)

            for in_frame in inp.decode(audio=0):
                for rf in resampler.resample(in_frame):
                    rf.pts = None
                    for pkt in out_stream.encode(rf):
                        out.mux(pkt)

            for rf in resampler.resample(None):
                rf.pts = None
                for pkt in out_stream.encode(rf):
                    out.mux(pkt)

            for pkt in out_stream.encode(None):
                out.mux(pkt)
        finally:
            inp.close()
            out.close()

        ok = os.path.exists(dst) and os.path.getsize(dst) > 200
        if ok:
            logger.info("WAV tayyor: %s (%d bytes)", dst, os.path.getsize(dst))
        else:
            logger.warning("WAV juda kichik yoki yo'q: %s", dst)
        return ok
    except Exception as e:
        logger.error("convert_to_wav xatosi: %s", e)
        return False


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


def build_ydl_opts(tmpdir: str, quality: str, platform: str = "other") -> dict:
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
        return base

    if platform == "youtube":
        height = {"360": 360, "720": 720, "1080": 1080}.get(quality, 720)
        # Pre-merged formatni birinchi urinib ko'ramiz (ffmpeg shart emas)
        base["format"] = (
            f"best[height<={height}][ext=mp4]"
            f"/bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio"
            f"/best[height<={height}]/best"
        )
        base["merge_output_format"] = "mp4"
        return base

    # Instagram, TikTok, Twitter, Facebook — ffmpeg merging kerak emas
    base["format"] = "best[ext=mp4]/best[ext=webm]/best"

    if platform == "instagram":
        base["http_headers"] = INSTAGRAM_HEADERS
        base["extractor_args"] = {"instagram": {"api": ["1"]}}

    if platform == "tiktok":
        base["format"] = "download_addr-0/best[ext=mp4]/best"

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
        "Ovozli xabar yoki audio fayl yuboring → qo'shiq aniqlanadi!\n\n"
        "💡 *Foydalanish:* Shunchaki link yoki ovozli xabar yuboring."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Yordam*\n\n"
        "*1. Video / Audio yuklash:*\n"
        "• Linkni yuboring — bot platformani o'zi aniqlaydi\n"
        "• YouTube uchun sifat tanlash oynasi chiqadi\n"
        "• Video yuklanganida qo'shiqni alohida yuklab olish tugmasi chiqadi\n\n"
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

    # Har qanday URL ni saqlaymiz (audio tugma uchun kerak)
    url_key = uuid.uuid4().hex[:10]
    URL_STORE[url_key] = url

    if platform == "youtube":
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
        icon = {"instagram": "📷", "tiktok": "🎵", "twitter": "🐦", "facebook": "👥"}.get(platform, "🌐")
        status = await update.message.reply_text(f"{icon} Yuklanmoqda... iltimos kuting ⏳")
        await download_and_send(
            update, context, url,
            quality="best",
            platform=platform,
            url_key=url_key,
            status_msg=status,
        )


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

    platform = detect_platform(url)
    await download_and_send(
        update, context, url,
        quality=quality,
        platform=platform,
        url_key=url_key,
        callback_query=query,
    )


# ─── download & send ─────────────────────────────────────────────────────────

async def download_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
    quality: str = "720",
    platform: str = "other",
    url_key: str = "",
    callback_query=None,
    status_msg=None,
):
    if callback_query:
        status = await callback_query.edit_message_text("⏬ Yuklanmoqda... iltimos kuting")
        reply_target = callback_query.message
    elif status_msg:
        status = status_msg
        reply_target = update.message
    else:
        status = await update.message.reply_text("⏬ Yuklanmoqda... iltimos kuting")
        reply_target = update.message

    with tempfile.TemporaryDirectory() as tmpdir:
        ydl_opts = build_ydl_opts(tmpdir, quality, platform)

        try:
            loop = asyncio.get_event_loop()
            info = await loop.run_in_executor(None, lambda: _sync_download(ydl_opts, url))

            if not info:
                await status.edit_text("❌ Yuklab bo'lmadi. Link noto'g'ri bo'lishi mumkin.")
                return

            title = (info.get("title") or "media")[:100]
            files = [f for f in Path(tmpdir).iterdir() if f.is_file()]

            if not files:
                await status.edit_text("❌ Fayl topilmadi.")
                return

            filepath = files[0]
            size = filepath.stat().st_size

            if size > MAX_FILE_SIZE:
                mb = size // (1024 * 1024)
                await status.edit_text(
                    f"❌ Fayl hajmi {mb} MB — Telegram limiti 50 MB.\n"
                    "Iltimos, past sifat tanlang."
                )
                return

            await status.edit_text("📤 Yuborilmoqda...")

            ext = filepath.suffix.lower()
            is_audio = quality == "audio" or ext in (".mp3", ".m4a", ".opus")

            with open(filepath, "rb") as fh:
                if is_audio:
                    await reply_target.reply_audio(fh, title=title)
                else:
                    audio_markup = None
                    if url_key:
                        audio_markup = InlineKeyboardMarkup([[
                            InlineKeyboardButton(
                                "🎵 Qo'shiqni ham yuklash (MP3)",
                                callback_data=f"dl:audio:{url_key}"
                            )
                        ]])
                    await reply_target.reply_video(
                        fh,
                        caption=f"🎬 {title}",
                        supports_streaming=True,
                        reply_markup=audio_markup,
                    )

            await status.delete()

        except yt_dlp.utils.DownloadError as e:
            err = str(e).lower()
            logger.warning("yt-dlp [%s]: %s", platform, err[:200])
            if "private" in err or "login" in err:
                await status.edit_text("❌ Bu xususiy post yoki login talab qiladi.")
            elif "not available" in err or "unavailable" in err:
                await status.edit_text("❌ Video mavjud emas yoki cheklov qo'yilgan.")
            elif "ffmpeg" in err:
                await status.edit_text("❌ Audio konvertatsiya xatosi (ffmpeg). Server sozlamalarini tekshiring.")
            else:
                await status.edit_text(f"❌ Yuklashda xatolik:\n`{str(e)[:200]}`", parse_mode="Markdown")
        except Exception as e:
            logger.exception("download_and_send [%s]", platform)
            await status.edit_text(f"❌ Kutilmagan xatolik: {str(e)[:200]}")


# ─── Shazam ──────────────────────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    status = await update.message.reply_text("🎵 Musiqa aniqlanmoqda... iltimos kuting")

    with tempfile.TemporaryDirectory() as tmpdir:
        oga_path = os.path.join(tmpdir, "voice.oga")
        wav_path = os.path.join(tmpdir, "voice.wav")

        try:
            # Telegram faylini yuklab olamiz
            tg_file = await context.bot.get_file(voice.file_id)
            await tg_file.download_to_drive(oga_path)
            logger.info("Ovoz fayli yuklandi: %d bytes", os.path.getsize(oga_path))

            # PyAV orqali OGG OPUS → WAV (tizimda ffmpeg kerak emas!)
            loop = asyncio.get_event_loop()
            converted = await loop.run_in_executor(None, convert_to_wav, oga_path, wav_path)
            recognize_path = wav_path if converted else oga_path
            logger.info("Shazam uchun fayl: %s", recognize_path)

            # Shazam API
            shazam = Shazam()
            result = await shazam.recognize(recognize_path)
            logger.info("Shazam natija: matches=%d", len(result.get("matches", [])))

            if not result or not result.get("matches"):
                await status.edit_text(
                    "❓ Qo'shiq aniqlanmadi.\n\n"
                    "Maslahat:\n"
                    "• Kamida 5-10 soniya yuboring\n"
                    "• Shovqinsiz joyda yozing\n"
                    "• Musiqa qismi bo'lsin (so'z emas)"
                )
                return

            track = result.get("track", {})
            song_title = track.get("title", "Noma'lum")
            artist = track.get("subtitle", "Noma'lum ijrochi")

            # Janr
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

            # Streaming havolalar
            for action in track.get("hub", {}).get("actions", []):
                if action.get("type") == "uri":
                    uri = action.get("uri", "")
                    if "spotify" in uri.lower():
                        response += f"\n🟢 [Spotify'da tinglash]({uri})"
                    elif "apple" in uri.lower():
                        response += f"\n🍎 [Apple Music'da tinglash]({uri})"

            # Muqova rasm
            images = track.get("images", {})
            coverart = images.get("coverarthq") or images.get("coverart")

            if coverart:
                await status.delete()
                await update.message.reply_photo(coverart, caption=response, parse_mode="Markdown")
            else:
                await status.edit_text(response, parse_mode="Markdown")

        except Exception as e:
            logger.exception("Shazam xatosi")
            await status.edit_text(
                f"❌ Musiqa aniqlashda xatolik:\n`{str(e)[:200]}`",
                parse_mode="Markdown",
            )


# ─── main ────────────────────────────────────────────────────────────────────

def main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN muhit o'zgaruvchisi o'rnatilmagan!")

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.AUDIO, handle_voice))
    app.add_handler(CallbackQueryHandler(handle_callback))

    logger.info("Bot ishga tushdi (polling mode)...")
    app.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
