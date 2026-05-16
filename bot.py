#!/usr/bin/env python3
import os
import signal
import asyncio
import logging
import re
import tempfile
import uuid
from pathlib import Path

signal.signal(signal.SIGTERM, lambda s, f: os._exit(0))

import av
import yt_dlp
from aiohttp import web
from shazamio import Shazam
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.error import Conflict
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
YOUTUBE_COOKIES = os.environ.get("YOUTUBE_COOKIES", "")
PORT = int(os.environ.get("PORT", "8080"))

COOKIES_FILE = "/tmp/yt_cookies.txt"
if YOUTUBE_COOKIES:
    cookie_content = YOUTUBE_COOKIES.replace('\\n', '\n')
    with open(COOKIES_FILE, "w") as _f:
        _f.write(cookie_content)
    logging.getLogger(__name__).info("Cookies yuklandi: %d qator", cookie_content.count('\n'))
else:
    COOKIES_FILE = None

URL_REGEX = re.compile(
    r"https?://(?:www\.)?"
    r"(?:youtube\.com/(?:watch|shorts|embed|live)|youtu\.be/"
    r"|instagram\.com/"
    r"|tiktok\.com/"
    r"|(?:twitter|x)\.com/"
    r"|(?:facebook\.com|fb\.watch)/"
    r"|(?:pinterest\.com|pin\.it)/"
    r")\S+",
    re.IGNORECASE,
)

URL_STORE: dict[str, str] = {}
SEARCH_STORE: dict[str, list] = {}
MAX_FILE_SIZE = 50 * 1024 * 1024

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
        return os.path.exists(dst) and os.path.getsize(dst) > 200
    except Exception as e:
        logger.error("convert_to_wav xatosi: %s", e)
        return False


def detect_platform(url: str) -> str:
    u = url.lower()
    if "youtube.com" in u or "youtu.be" in u:
        return "youtube"
    if "soundcloud.com" in u:
        return "soundcloud"
    if "instagram.com" in u:
        return "instagram"
    if "tiktok.com" in u:
        return "tiktok"
    if "twitter.com" in u or "x.com" in u:
        return "twitter"
    if "facebook.com" in u or "fb.watch" in u:
        return "facebook"
    if "pinterest.com" in u or "pin.it" in u:
        return "pinterest"
    return "other"


def _sync_download(ydl_opts: dict, url: str):
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        return ydl.extract_info(url, download=True)


def _sync_resolve_pin_url(url: str) -> str:
    """pin.it → pinterest.com/pin/ID/ URL. httpx + aiohttp fallback."""
    import httpx

    _headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.9",
    }

    # httpx bilan sinab ko'r (urllib3 stack, aiohttp'dan farq qiladi)
    try:
        with httpx.Client(follow_redirects=True, timeout=30, headers=_headers) as client:
            resp = client.get(url)
            final = str(resp.url)
            m = re.search(r"/pin/(\d+)", final)
            if m:
                return f"https://www.pinterest.com/pin/{m.group(1)}/"
            if "pinterest.com" in final:
                return final
    except Exception as e:
        logger.warning("httpx pin.it resolve xatosi: %s", e)

    return url  # resolve muvaffaqiyatsiz — original qaytaramiz


async def resolve_pinterest_short_url(url: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_resolve_pin_url, url)


def _sync_search(query: str, limit: int = 5) -> list:
    # Request 2x to compensate for any filtered-out entries
    fetch = limit * 2
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": f"1:{fetch}",
    }
    if COOKIES_FILE:
        ydl_opts["cookiefile"] = COOKIES_FILE
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{fetch}:{query}", download=False)
        entries = [e for e in (info.get("entries", []) if info else []) if e]
        return entries[:limit]


def build_ydl_opts(tmpdir: str, quality: str, platform: str = "other") -> dict:
    base = {
        "outtmpl": os.path.join(tmpdir, "%(title).80s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

    if COOKIES_FILE:
        base["cookiefile"] = COOKIES_FILE

    if quality == "audio":
        base["format"] = "bestaudio/best"
        base["postprocessors"] = [
            {
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }
        ]
        if platform == "youtube":
            base["extractor_args"] = {"youtube": {"player_client": ["web", "android", "ios"]}}
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
        base["extractor_args"] = {"youtube": {"player_client": ["web", "android", "ios"]}}
        return base

    if platform == "instagram":
        base["format"] = "best[ext=mp4]/best[ext=webm]/best"
        base["http_headers"] = INSTAGRAM_HEADERS
        base["extractor_args"] = {"instagram": {"api": ["1"]}}
        return base

    if platform == "tiktok":
        base["format"] = "download_addr-0/best[ext=mp4]/best"
        return base

    if platform == "pinterest":
        base["format"] = "best"
        return base

    base["format"] = "best[ext=mp4]/best[ext=webm]/best"
    return base


# ─── health check server ─────────────────────────────────────────────────────

async def _health_handler(request):
    return web.Response(text="OK")


async def start_health_server(port: int):
    web_app = web.Application()
    web_app.router.add_get("/", _health_handler)
    web_app.router.add_get("/health", _health_handler)
    runner = web.AppRunner(web_app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    logger.info("Health server port %d da ishga tushdi", port)
    return runner


# ─── command handlers ────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Salom! Media yuklovchi botga xush kelibsiz!*\n\n"
        "📹 *Qo'llab-quvvatlanadigan platformalar:*\n"
        "• 🔴 YouTube — video yoki audio\n"
        "• 📷 Instagram — post / reel / story\n"
        "• 🎵 TikTok — watermarksiz\n"
        "• 🐦 Twitter / X\n"
        "• 👥 Facebook\n"
        "• 📌 Pinterest — rasm / video\n\n"
        "🎵 *Shazam funksiyasi:*\n"
        "Ovozli xabar, audio yoki video yuboring → qo'shiq aniqlanib, yuklab olish variantlari chiqadi!\n\n"
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
        "• Ovozli xabar yoki video yuboring\n"
        "• Bot qo'shiqni topib, YouTube'dan 5 ta variant ko'rsatadi\n"
        "• Raqamga bosing → audio yuklanadi\n\n"
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

    # pin.it → clean pinterest.com/pin/ID/ URLga aylantir (yt-dlp pin.it taniy olmaydi)
    if "pin.it" in url:
        resolved = await resolve_pinterest_short_url(url)
        if "pin.it" in resolved:
            # Server pin.it domeniga yeta olmadi — foydalanuvchiga tushuntir
            await update.message.reply_text(
                "📌 *pin.it qisqa linki ishlamadi*\n\n"
                "Server `pin.it` domeniga ulanolmayapti.\n\n"
                "*Shunday qiling:*\n"
                "1. Brauzerda Pinterest ni oching\n"
                "2. Pin sahifasini oching\n"
                "3. Yuqoridagi to'liq URL ni nusxalang\n"
                "   (masalan: `pinterest.com/pin/123456789/`)\n"
                "4. Shu URLni yuboring",
                parse_mode="Markdown"
            )
            return
        url = resolved

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
        icon = {"instagram": "📷", "tiktok": "🎵", "twitter": "🐦", "facebook": "👥", "pinterest": "📌"}.get(platform, "🌐")
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

        status = await query.message.reply_text("⏬ Yuklanmoqda... iltimos kuting")
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
            is_image = ext in (".jpg", ".jpeg", ".png", ".webp")

            with open(filepath, "rb") as fh:
                if is_audio:
                    await reply_target.reply_audio(fh, title=title)
                elif is_image:
                    await reply_target.reply_photo(fh, caption=title)
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

async def shazam_and_reply(update: Update, file_id: str, status_msg):
    msg = update.message
    loop = asyncio.get_event_loop()

    with tempfile.TemporaryDirectory() as tmpdir:
        src_path = os.path.join(tmpdir, "media.bin")
        wav_path = os.path.join(tmpdir, "audio.wav")

        try:
            tg_file = await update.get_bot().get_file(file_id)
            await tg_file.download_to_drive(src_path)

            converted = await loop.run_in_executor(None, convert_to_wav, src_path, wav_path)
            recognize_path = wav_path if converted else src_path

            shazam = Shazam()
            result = await shazam.recognize(recognize_path)

            if not result or not result.get("matches"):
                await status_msg.edit_text(
                    "❓ Qo'shiq aniqlanmadi.\n\n"
                    "• Kamida 5-10 soniya yuboring\n"
                    "• Shovqinsiz joyda yozing\n"
                    "• Musiqa qismi bo'lsin (so'z emas)"
                )
                return

            track = result.get("track", {})
            song_title = track.get("title", "Noma'lum")
            artist = track.get("subtitle", "Noma'lum ijrochi")

            query = f"{artist} {song_title}"
            entries = await loop.run_in_executor(None, _sync_search, query, 5)

            images = track.get("images", {})
            coverart = images.get("coverarthq") or images.get("coverart")

            if not entries:
                text = f"Ijrochi: *{artist}*\nQo'shiq nomi: *{song_title}*"
                await status_msg.delete()
                if coverart:
                    await msg.reply_photo(coverart, caption=text, parse_mode="Markdown")
                else:
                    await msg.reply_text(text, parse_mode="Markdown")
                return

            search_key = uuid.uuid4().hex[:10]
            SEARCH_STORE[search_key] = []
            for e in entries:
                vid_id = e.get("id") or e.get("url", "")
                full_url = e.get("webpage_url") or (
                    f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""
                )
                SEARCH_STORE[search_key].append({
                    "url": full_url,
                    "title": e.get("title", "Noma'lum"),
                    "duration": e.get("duration"),
                })

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

            n = len(SEARCH_STORE[search_key])
            keyboard = [
                [
                    InlineKeyboardButton(str(i + 1), callback_data=f"yt:audio:{search_key}:{i}")
                    for i in range(n)
                ],
            ]

            await status_msg.delete()

            if coverart:
                await msg.reply_photo(
                    coverart,
                    caption=caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )
            else:
                await msg.reply_text(
                    caption,
                    reply_markup=InlineKeyboardMarkup(keyboard),
                    parse_mode="Markdown",
                )

        except Exception as e:
            logger.exception("Shazam/search xatosi")
            await status_msg.edit_text(f"❌ Xatolik:\n`{str(e)[:200]}`", parse_mode="Markdown")


async def handle_voice(update: Update, context: ContextTypes.DEFAULT_TYPE):
    voice = update.message.voice or update.message.audio
    if not voice:
        return
    status = await update.message.reply_text("🎵 Musiqa aniqlanmoqda... iltimos kuting")
    await shazam_and_reply(update, voice.file_id, status)


async def handle_video_shazam(update: Update, context: ContextTypes.DEFAULT_TYPE):
    video = update.message.video or update.message.video_note
    if not video:
        return
    status = await update.message.reply_text("🎵 Videodagi musiqa aniqlanmoqda...")
    await shazam_and_reply(update, video.file_id, status)


# ─── main ────────────────────────────────────────────────────────────────────

async def conflict_error_handler(update, context):
    if isinstance(context.error, Conflict):
        # Yangi container: sabr qil, eski container Railway SIGTERM orqali o'ladi
        # os._exit qilsak YANGI container o'ladi va deploy hech qachon bo'lmaydi
        logger.warning("Conflict: eski instance hali ishlayapti. 30s kutilmoqda...")
        await asyncio.sleep(30)
    else:
        logger.error("Xato: %s", context.error)


async def async_main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN muhit o'zgaruvchisi o'rnatilmagan!")

    health_runner = await start_health_server(PORT)

    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video_shazam))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_shazam))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(conflict_error_handler)

    async with app:
        await app.start()
        await app.updater.start_polling(
            allowed_updates=Update.ALL_TYPES,
            drop_pending_updates=True,
        )
        logger.info("Bot ishga tushdi (polling mode)...")
        await asyncio.Event().wait()  # SIGTERM → os._exit(0) kills this
        await app.updater.stop()
        await app.stop()

    await health_runner.cleanup()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
