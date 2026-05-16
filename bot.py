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
SEARCH_STORE: dict[str, list] = {}
MAX_FILE_SIZE = 50 * 1024 * 1024  # 50 MB

INSTAGRAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 16_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/16.0 Mobile/15E148 Safari/604.1"
    )
}


# ─── helpers ────────────────────────────────────────────────────────────────

def fmt_duration(seconds) -> str:
    if not seconds:
        return ""
    m, s = divmod(int(seconds), 60)
    return f"{m}:{s:02d}"


def convert_to_wav(src: str, dst: str) -> bool:
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
        return ok
    except Exception as e:
        logger.error("convert_to_wav xatosi: %s", e)
        return False


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


def _sync_search(query: str, limit: int = 5) -> list:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": f"1:{limit}",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{limit}:{query}", download=False)
        return info.get("entries", []) if info else []


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
        base["format"] = (
            f"best[height<={height}][ext=mp4]"
            f"/bestvideo[height<={height}][ext=mp4]+bestaudio[ext=m4a]"
            f"/bestvideo[height<={height}]+bestaudio"
            f"/best[height<={height}]/best"
        )
        base["merge_output_format"] = "mp4"
        return base

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
        "Ovozli xabar yoki audio fayl yuboring → qo'shiq aniqlanib, yuklab olish variantlari chiqadi!\n\n"
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
        "• Bot qo'shiqni topib, YouTube'dan variantlar ko'rsatadi\n"
        "• Raqamga bosing → audio yuklanadi\n"
        "• Video tugmasiga bosing → video sifatini tanlang\n\n"
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
            reply_to=update.message,
        )


# ─── callback handler ─────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    parts = query.data.split(":")

    # dl:quality:url_key — oddiy download
    if parts[0] == "dl" and len(parts) == 3:
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
            reply_to=query.message,
        )
        return

    # yt:action:search_key:index — shazam natijasidan yuklab olish
    if parts[0] == "yt" and len(parts) == 4:
        _, action, search_key, idx_str = parts
        index = int(idx_str)

        entries = SEARCH_STORE.get(search_key)
        if not entries or index >= len(entries):
            await query.message.reply_text("❌ Ma'lumot eskirgan. Qaytadan yuboring.")
            return

        entry = entries[index]
        url = entry["url"]
        if not url.startswith("http"):
            url = f"https://www.youtube.com/watch?v={url}"

        url_key = uuid.uuid4().hex[:10]
        URL_STORE[url_key] = url

        if action == "video":
            keyboard = [
                [
                    InlineKeyboardButton("📹 360p", callback_data=f"dl:360:{url_key}"),
                    InlineKeyboardButton("📹 720p", callback_data=f"dl:720:{url_key}"),
                    InlineKeyboardButton("📹 1080p", callback_data=f"dl:1080:{url_key}"),
                ],
                [InlineKeyboardButton("🎵 Faqat audio (MP3)", callback_data=f"dl:audio:{url_key}")],
            ]
            await query.message.reply_text(
                "🎬 Video sifatini tanlang:",
                reply_markup=InlineKeyboardMarkup(keyboard),
            )
        else:
            # audio — yangi status xabari yuborib, audio yuklaymiz
            status = await query.message.reply_text("⏬ Audio yuklanmoqda... iltimos kuting")
            await download_and_send(
                update, context, url,
                quality="audio",
                platform="youtube",
                url_key=url_key,
                status_msg=status,
                reply_to=query.message,
            )
        return


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
    reply_to=None,
):
    if callback_query:
        status = await callback_query.edit_message_text("⏬ Yuklanmoqda... iltimos kuting")
        reply_target = reply_to or callback_query.message
    elif status_msg:
        status = status_msg
        reply_target = reply_to or (update.message if update.message else status_msg.chat)
    else:
        status = await update.message.reply_text("⏬ Yuklanmoqda... iltimos kuting")
        reply_target = reply_to or update.message

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
            else:
                await status.edit_text(f"❌ Yuklashda xatolik:\n`{str(e)[:200]}`", parse_mode="Markdown")
        except Exception as e:
            logger.exception("download_and_send [%s]", platform)
            await status.edit_text(f"❌ Kutilmagan xatolik: {str(e)[:200]}")


# ─── Shazam + YouTube search ──────────────────────────────────────────────────

async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice or update.message.audio
    if not voice:
        return

    status = await update.message.reply_text("🎵 Musiqa aniqlanmoqda... iltimos kuting")

    with tempfile.TemporaryDirectory() as tmpdir:
        oga_path = os.path.join(tmpdir, "voice.oga")
        wav_path = os.path.join(tmpdir, "voice.wav")

        try:
            tg_file = await context.bot.get_file(voice.file_id)
            await tg_file.download_to_drive(oga_path)

            loop = asyncio.get_event_loop()
            converted = await loop.run_in_executor(None, convert_to_wav, oga_path, wav_path)
            recognize_path = wav_path if converted else oga_path

            shazam = Shazam()
            result = await shazam.recognize(recognize_path)

            if not result or not result.get("matches"):
                await status.edit_text(
                    "❓ Qo'shiq aniqlanmadi.\n\n"
                    "• Kamida 5-10 soniya yuboring\n"
                    "• Shovqinsiz joyda yozing\n"
                    "• Musiqa qismi bo'lsin (so'z emas)"
                )
                return

            track = result.get("track", {})
            song_title = track.get("title", "Noma'lum")
            artist = track.get("subtitle", "Noma'lum ijrochi")

            # YouTube'dan qidirish
            await status.edit_text("🔍 YouTube'dan qidirilmoqda...")
            query = f"{artist} {song_title}"
            entries = await loop.run_in_executor(None, _sync_search, query, 5)
            entries = [e for e in entries if e]

            if not entries:
                await status.edit_text(
                    f"🎵 *Qo'shiq topildi!*\n\n🎤 *Ijrochi:* {artist}\n🎶 *Qo'shiq:* {song_title}\n\n"
                    "❌ YouTube'dan yuklab bo'lmadi.",
                    parse_mode="Markdown",
                )
                return

            # Natijalarni saqlash
            search_key = uuid.uuid4().hex[:10]
            SEARCH_STORE[search_key] = [
                {
                    "url": e.get("url") or e.get("id", ""),
                    "title": e.get("title", "Noma'lum"),
                    "duration": e.get("duration"),
                }
                for e in entries
            ]

            # Ro'yxat matni
            lines = []
            for i, e in enumerate(SEARCH_STORE[search_key], 1):
                dur = fmt_duration(e.get("duration"))
                t = e["title"][:55]
                lines.append(f"*{i}.* {t}  *{dur}*" if dur else f"*{i}.* {t}")

            caption = (
                f"Ijrochi: *{artist}*\n"
                f"Qo'shiq nomi: *{song_title}*\n\n"
                + "\n".join(lines)
            )

            # Tugmalar: Video + 1 2 3 4 5
            n = len(SEARCH_STORE[search_key])
            keyboard = [
                [InlineKeyboardButton("📼 Video", callback_data=f"yt:video:{search_key}:0")],
                [
                    InlineKeyboardButton(str(i + 1), callback_data=f"yt:audio:{search_key}:{i}")
                    for i in range(n)
                ],
            ]

            images = track.get("images", {})
            coverart = images.get("coverarthq") or images.get("coverart")

            await status.delete()

            if coverart:
                await update.message.reply_photo(
                    coverart,
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )
            else:
                await update.message.reply_text(
                    caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )

        except Exception as e:
            logger.exception("Shazam/search xatosi")
            await status.edit_text(f"❌ Xatolik:\n`{str(e)[:200]}`", parse_mode="Markdown")


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
