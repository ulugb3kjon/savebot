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
PORT = int(os.environ.get("PORT", "8080"))

URL_REGEX = re.compile(
    r"https?://(?:www\.)?"
    r"(?:instagram\.com/"
    r"|tiktok\.com/"
    r"|(?:twitter|x)\.com/"
    r"|(?:facebook\.com|fb\.watch)/"
    r"|(?:pinterest\.com|pin\.it)/"
    r")\S+",
    re.IGNORECASE,
)

URL_STORE: dict[str, str] = {}

# video_id → song_query (yangi, kichik, turg'un)
QUERY_STORE: dict[str, str] = {}

MAX_FILE_SIZE = 50 * 1024 * 1024

INSTAGRAM_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    ),
    "Accept": "*/*",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "X-IG-App-ID": "936619743392459",
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
                for frame in resampler.resample(in_frame):
                    for packet in out_stream.encode(frame):
                        out.mux(packet)
            for packet in out_stream.encode():
                out.mux(packet)
        finally:
            out.close()
            inp.close()
        return True
    except Exception as e:
        logger.warning("WAV konvertatsiya xatosi: %s", e)
        return False


def detect_platform(url: str) -> str:
    u = url.lower()
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


def _sync_search(query: str, limit: int = 5) -> list:
    fetch = limit * 2
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": True,
        "playlist_items": f"1:{fetch}",
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(f"ytsearch{fetch}:{query}", download=False)
        entries = [e for e in (info.get("entries", []) if info else []) if e]
        return entries[:limit]


def _sync_resolve_pin_url(url: str) -> str:
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
    return url


async def resolve_pinterest_short_url(url: str) -> str:
    loop = asyncio.get_event_loop()
    return await loop.run_in_executor(None, _sync_resolve_pin_url, url)


# ─── cobalt.tools download (YouTube IP blokidan chetlab) ─────────────────────

async def _cobalt_download(url: str, tmpdir: str) -> str | None:
    from aiohttp import ClientSession, ClientTimeout

    ext = ".mp3"
    filepath = os.path.join(tmpdir, f"cobalt{ext}")

    payload = {
        "url": url,
        "downloadMode": "audio",
        "audioFormat": "mp3",
        "audioBitrate": "192",
    }
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (compatible; bot)",
    }

    endpoints = [
        "https://api.cobalt.tools/",
        "https://cobalt.tools/api/",
    ]

    for endpoint in endpoints:
        try:
            async with ClientSession() as session:
                async with session.post(
                    endpoint,
                    json=payload,
                    headers=headers,
                    timeout=ClientTimeout(total=30),
                ) as resp:
                    if resp.status not in (200, 201):
                        logger.warning("cobalt %s → %d", endpoint, resp.status)
                        continue
                    data = await resp.json(content_type=None)

                status_c = data.get("status")
                dl_url = data.get("url")
                if status_c not in ("redirect", "tunnel") or not dl_url:
                    logger.warning("cobalt: status=%s", status_c)
                    continue

                async with session.get(dl_url, timeout=ClientTimeout(total=300)) as dl_resp:
                    if dl_resp.status != 200:
                        continue
                    with open(filepath, "wb") as f:
                        async for chunk in dl_resp.content.iter_chunked(65536):
                            f.write(chunk)

            if os.path.exists(filepath) and os.path.getsize(filepath) > 1000:
                logger.info("cobalt OK: %d bayt", os.path.getsize(filepath))
                return filepath
        except Exception as exc:
            logger.warning("cobalt xatosi [%s]: %s", endpoint, str(exc)[:100])

    if os.path.exists(filepath):
        try:
            os.unlink(filepath)
        except OSError:
            pass
    return None


def build_ydl_opts(tmpdir: str, platform: str = "other") -> dict:
    base = {
        "outtmpl": os.path.join(tmpdir, "%(title).80s.%(ext)s"),
        "quiet": True,
        "no_warnings": True,
        "noplaylist": True,
    }

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


# ─── health check ─────────────────────────────────────────────────────────────

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


# ─── command handlers ─────────────────────────────────────────────────────────

async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "👋 *Salom! Media yuklovchi botga xush kelibsiz!*\n\n"
        "📹 *Qo'llab-quvvatlanadigan platformalar:*\n"
        "• 📷 Instagram — post / reel / story\n"
        "• 🎵 TikTok — watermarksiz\n"
        "• 🐦 Twitter / X\n"
        "• 👥 Facebook\n"
        "• 📌 Pinterest — rasm / video\n\n"
        "🎵 *Musiqa aniqlash (Shazam):*\n"
        "Ovozli xabar, audio yoki video yuboring → qo'shiq aniqlanadi!\n\n"
        "🔍 *Qo'shiq qidirish:*\n"
        "Biror matn yuboring → YouTube/SoundCloud dan topib yuklaydi!\n"
        "Masalan: `Jasur Umirov Qizaloq` yoki `Phonk drift`\n\n"
        "💡 *Foydalanish:* Link, ovozli xabar yoki qo'shiq nomi yuboring."
    )
    await update.message.reply_text(text, parse_mode="Markdown")


async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "📖 *Yordam*\n\n"
        "*1. Video yuklash:*\n"
        "• Linkni yuboring — bot platformani o'zi aniqlaydi\n\n"
        "*2. Musiqa aniqlash 🎵 (Shazam):*\n"
        "• Ovozli xabar yoki video yuboring\n"
        "• Bot qo'shiqni topib, 5 ta variant ko'rsatadi\n"
        "• Raqamga bosing → MP3 yuklanadi\n\n"
        "*3. Qo'shiq qidirish 🔍:*\n"
        "• Matn yuboring (masalan: `Bad Bunny` yoki `Ozodbek Nazarbekov`)\n"
        "• Bot YouTube dan 5 ta natija topadi\n"
        "• Raqamga bosing → MP3 yuklanadi\n\n"
        "*⚠️ Cheklovlar:*\n"
        "• Fayl hajmi ≤ 50 MB\n"
        "• Xususiy / yopiq postlar yuklanmaydi"
    )
    await update.message.reply_text(text, parse_mode="Markdown")


# ─── URL handler ──────────────────────────────────────────────────────────────

async def handle_message(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not update.message or not update.message.text:
        return

    text = update.message.text.strip()
    matches = URL_REGEX.findall(text)

    if matches:
        url = matches[0]
        if "pin.it" in url:
            resolved = await resolve_pinterest_short_url(url)
            if "pin.it" in resolved:
                await update.message.reply_text(
                    "📌 *pin.it qisqa linki ishlamadi*\n\n"
                    "To'liq pinterest.com URL ni yuboring.",
                    parse_mode="Markdown",
                )
                return
            url = resolved

        platform = detect_platform(url)
        url_key = uuid.uuid4().hex[:10]
        URL_STORE[url_key] = url

        icon = {
            "instagram": "📷",
            "tiktok": "🎵",
            "twitter": "🐦",
            "facebook": "👥",
            "pinterest": "📌",
        }.get(platform, "🌐")
        status = await update.message.reply_text(f"{icon} Yuklanmoqda... iltimos kuting ⏳")
        await download_and_send(
            update, context, url,
            platform=platform,
            url_key=url_key,
            status_msg=status,
            reply_to=update.message,
        )
    else:
        # URL yo'q → matn bo'yicha qo'shiq qidirish
        await handle_text_search(update, text)


# ─── matn orqali qo'shiq qidirish ─────────────────────────────────────────────

async def handle_text_search(update: Update, query: str):
    if len(query) < 2 or len(query) > 150:
        return

    status = await update.message.reply_text(f"🔍 *{query}* qidirilmoqda...", parse_mode="Markdown")

    loop = asyncio.get_event_loop()
    try:
        entries = await loop.run_in_executor(None, _sync_search, query, 5)
    except Exception as e:
        logger.warning("Text search xatosi: %s", e)
        await status.edit_text("❌ Qidiruvda xatolik. Qaytadan urinib ko'ring.")
        return

    if not entries:
        await status.edit_text(
            f"😔 *{query}* bo'yicha hech narsa topilmadi.\n\n"
            "Boshqa so'z bilan urinib ko'ring.",
            parse_mode="Markdown",
        )
        return

    await _show_search_results(update.message, entries, query, status)


async def _show_search_results(msg, entries: list, query: str, status_msg):
    entries_data = []
    for e in entries:
        vid_id = e.get("id", "")
        if not vid_id:
            raw = e.get("url", "")
            m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", raw)
            vid_id = m.group(1) if m else raw[:11]
        full_url = e.get("webpage_url") or (
            f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""
        )
        entries_data.append({
            "vid_id": vid_id,
            "url": full_url,
            "title": e.get("title", "Noma'lum"),
            "duration": e.get("duration"),
        })

    # QUERY_STORE ga saqlash (video_id → query)
    for e in entries_data:
        if e["vid_id"]:
            QUERY_STORE[e["vid_id"]] = query

    lines = []
    for i, e in enumerate(entries_data, 1):
        dur = fmt_duration(e.get("duration"))
        t = e["title"][:52]
        lines.append(f"*{i}.* {t}  `{dur}`" if dur else f"*{i}.* {t}")

    caption = f"🎵 *{query}* bo'yicha natijalar:\n\n" + "\n".join(lines)

    # callback_data: "ytdl:{vid_id}" — SEARCH_STORE siz, video_id to'g'ridan ishlatiladi
    keyboard = []
    row = []
    for i, e in enumerate(entries_data):
        vid_id = e["vid_id"] or str(i)
        row.append(InlineKeyboardButton(str(i + 1), callback_data=f"ytdl:{vid_id}"))
    keyboard.append(row)

    await status_msg.delete()
    await msg.reply_text(
        caption,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="Markdown",
    )


# ─── Shazam audio yuklovchi: SoundCloud → yt-dlp (bir necha usul) ────────────

async def download_shazam_audio(query: str, yt_url: str, status_msg, reply_target):
    loop = asyncio.get_event_loop()

    audio_pp = [{"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}]

    def _make_opts(tmpdir, extra=None):
        opts = {
            "format": "bestaudio/best",
            "outtmpl": os.path.join(tmpdir, "%(title).80s.%(ext)s"),
            "quiet": True,
            "no_warnings": True,
            "noplaylist": True,
            "postprocessors": audio_pp,
        }
        if extra:
            opts.update(extra)
        return opts

    with tempfile.TemporaryDirectory() as tmpdir:
        filepath = None

        # 1) SoundCloud — o'zbek va phonk musiqalari uchun yaxshi
        try:
            info = await loop.run_in_executor(
                None, lambda: _sync_download(_make_opts(tmpdir), f"scsearch1:{query}")
            )
            if info:
                files = [f for f in Path(tmpdir).iterdir() if f.is_file()]
                if files:
                    filepath = max(files, key=lambda f: f.stat().st_size)
                    logger.info("SoundCloud: topildi — %s", query)
        except Exception as e:
            logger.warning("SoundCloud topilmadi: %s", str(e)[:100])

        # 2) yt-dlp — TV embedded player (IP blokidan chetlaydi)
        if not filepath and yt_url and yt_url.startswith("http"):
            try:
                opts = _make_opts(tmpdir, {
                    "extractor_args": {
                        "youtube": {"player_client": ["tv_embedded", "web_creator", "android"]}
                    }
                })
                info = await loop.run_in_executor(
                    None, lambda: _sync_download(opts, yt_url)
                )
                if info:
                    files = [f for f in Path(tmpdir).iterdir() if f.is_file()]
                    if files:
                        filepath = max(files, key=lambda f: f.stat().st_size)
                        logger.info("yt-dlp TV embedded: topildi")
            except Exception as e:
                logger.warning("yt-dlp TV embedded xato: %s", str(e)[:100])

        # 3) cobalt.tools (yt_url orqali)
        if not filepath and yt_url and yt_url.startswith("http"):
            cobalt_path = await _cobalt_download(yt_url, tmpdir)
            if cobalt_path:
                filepath = Path(cobalt_path)
                logger.info("cobalt audio: topildi")

        # 4) yt-dlp — format 140 (YouTube m4a, eng tez)
        if not filepath and yt_url and yt_url.startswith("http"):
            try:
                opts = _make_opts(tmpdir, {"format": "140/bestaudio/best"})
                info = await loop.run_in_executor(
                    None, lambda: _sync_download(opts, yt_url)
                )
                if info:
                    files = [f for f in Path(tmpdir).iterdir() if f.is_file()]
                    if files:
                        filepath = max(files, key=lambda f: f.stat().st_size)
                        logger.info("yt-dlp format 140: topildi")
            except Exception as e:
                logger.warning("yt-dlp format 140 xato: %s", str(e)[:100])

        # 5) ytsearch1 fallback (YouTube qidiruvidan)
        if not filepath:
            try:
                opts = _make_opts(tmpdir, {
                    "extractor_args": {
                        "youtube": {"player_client": ["tv_embedded", "android"]}
                    }
                })
                info = await loop.run_in_executor(
                    None, lambda: _sync_download(opts, f"ytsearch1:{query}")
                )
                if info:
                    files = [f for f in Path(tmpdir).iterdir() if f.is_file()]
                    if files:
                        filepath = max(files, key=lambda f: f.stat().st_size)
                        logger.info("ytsearch1 fallback: topildi")
            except Exception as e:
                logger.warning("ytsearch1 xato: %s", str(e)[:100])

        if not filepath or not Path(filepath).exists():
            await status_msg.edit_text(
                "❌ Bu qo'shiq yuklab bo'lmadi.\n"
                "Boshqa variant tanlang yoki qo'shiq nomini matn sifatida yuboring."
            )
            return

        filepath = Path(filepath)
        if filepath.stat().st_size > MAX_FILE_SIZE:
            mb = filepath.stat().st_size // (1024 * 1024)
            await status_msg.edit_text(f"❌ Fayl {mb} MB — Telegram limiti 50 MB.")
            return

        title = query[:100]
        await status_msg.edit_text("📤 Yuborilmoqda...")
        with open(filepath, "rb") as fh:
            await reply_target.reply_audio(fh, title=title, write_timeout=120)
        await status_msg.delete()


# ─── callback handler ─────────────────────────────────────────────────────────

async def handle_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()

    data = query.data

    # Eski format: dl:{quality}:{url_key}
    if data.startswith("dl:"):
        parts = data.split(":")
        if len(parts) == 3:
            _, quality, url_key = parts
            url = URL_STORE.get(url_key)
            if not url:
                await query.edit_message_text("❌ URL eskirgan. Linkni qaytadan yuboring.")
                return
            platform = detect_platform(url)
            await download_and_send(
                update, context, url,
                platform=platform,
                url_key=url_key,
                callback_query=query,
                reply_to=query.message,
            )
        return

    # Yangi format: ytdl:{video_id}
    if data.startswith("ytdl:"):
        vid_id = data[5:]  # "ytdl:" dan keyin
        if not vid_id:
            await query.message.reply_text("❌ Video ID topilmadi.")
            return

        yt_url = f"https://www.youtube.com/watch?v={vid_id}"
        # QUERY_STORE dan qo'shiq nomini olish
        song_query = QUERY_STORE.get(vid_id, vid_id)

        status = await query.message.reply_text("⏬ Yuklanmoqda... iltimos kuting")
        await download_shazam_audio(song_query, yt_url, status, query.message)
        return

    await query.message.reply_text("❌ Noma'lum tugma. Qaytadan urinib ko'ring.")


# ─── download & send (URL yuklash) ───────────────────────────────────────────

async def download_and_send(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    url: str,
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
        ydl_opts = build_ydl_opts(tmpdir, platform)

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
                await status.edit_text(f"❌ Fayl hajmi {mb} MB — Telegram limiti 50 MB.")
                return

            await status.edit_text("📤 Yuborilmoqda...")

            ext = filepath.suffix.lower()
            is_audio = ext in (".mp3", ".m4a", ".opus")
            is_image = ext in (".jpg", ".jpeg", ".png", ".webp")

            with open(filepath, "rb") as fh:
                if is_audio:
                    await reply_target.reply_audio(fh, title=title, write_timeout=120)
                elif is_image:
                    await reply_target.reply_photo(fh, caption=title, write_timeout=60)
                else:
                    await reply_target.reply_video(
                        fh,
                        caption=f"🎬 {title}",
                        supports_streaming=True,
                        write_timeout=120,
                    )

            await status.delete()

        except yt_dlp.utils.DownloadError as e:
            err = str(e).lower()
            logger.warning("yt-dlp [%s]: %s", platform, err[:200])

            if platform == "instagram" and ("403" in err or "404" in err or "unable" in err):
                try:
                    retry_opts = {
                        "outtmpl": os.path.join(tmpdir, "retry.%(ext)s"),
                        "quiet": True,
                        "no_warnings": True,
                        "noplaylist": True,
                        "format": "best[ext=mp4]/best",
                        "http_headers": INSTAGRAM_HEADERS,
                    }
                    info2 = await asyncio.get_event_loop().run_in_executor(
                        None, lambda: _sync_download(retry_opts, url)
                    )
                    if info2:
                        files2 = [f for f in Path(tmpdir).iterdir() if f.is_file()]
                        if files2:
                            fp2 = max(files2, key=lambda f: f.stat().st_size)
                            if fp2.stat().st_size <= MAX_FILE_SIZE and fp2.stat().st_size > 1000:
                                title2 = (info2.get("title") or "media")[:100]
                                await status.edit_text("📤 Yuborilmoqda...")
                                with open(fp2, "rb") as fh2:
                                    ext2 = fp2.suffix.lower()
                                    if ext2 in (".jpg", ".jpeg", ".png", ".webp"):
                                        await reply_target.reply_photo(fh2, caption=title2, write_timeout=60)
                                    else:
                                        await reply_target.reply_video(fh2, caption=f"🎬 {title2}", supports_streaming=True, write_timeout=120)
                                await status.delete()
                                return
                except Exception:
                    pass

            if "private" in err or "login" in err:
                await status.edit_text("❌ Bu xususiy post — yuklab bo'lmadi.")
            elif "not available" in err or "unavailable" in err:
                await status.edit_text("❌ Video mavjud emas yoki cheklov qo'yilgan.")
            elif "404" in err or "not found" in err:
                await status.edit_text("❌ Kontent topilmadi (404).")
            else:
                await status.edit_text(f"❌ Yuklashda xatolik:\n`{str(e)[:200]}`", parse_mode="Markdown")

        except Exception as e:
            logger.exception("download_and_send [%s]", platform)
            await status.edit_text(f"❌ Kutilmagan xatolik: {str(e)[:200]}")


# ─── Shazam: ovozli xabar va video ───────────────────────────────────────────

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
                    "❓ *Qo'shiq aniqlanmadi.*\n\n"
                    "Sabab: Shazam bu qo'shiqni bilmaydi (o'zbek yoki phonk bo'lishi mumkin).\n\n"
                    "*Nima qilish kerak:*\n"
                    "• Qo'shiq nomini matn sifatida yuboring\n"
                    "  Masalan: `Jasur Umirov Qizaloq`\n"
                    "• Yoki YouTube linkini yuboring",
                    parse_mode="Markdown",
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
                caption = f"🎵 *{song_title}*\n👤 {artist}\n\n❌ YouTube da yuklanadigan variant topilmadi."
                await status_msg.delete()
                if coverart:
                    await msg.reply_photo(coverart, caption=caption, parse_mode="Markdown")
                else:
                    await msg.reply_text(caption, parse_mode="Markdown")
                return

            entries_data = []
            for e in entries:
                vid_id = e.get("id", "")
                if not vid_id:
                    raw = e.get("url", "")
                    m = re.search(r"(?:v=|youtu\.be/|/shorts/)([A-Za-z0-9_-]{11})", raw)
                    vid_id = m.group(1) if m else raw[:11]
                full_url = e.get("webpage_url") or (
                    f"https://www.youtube.com/watch?v={vid_id}" if vid_id else ""
                )
                entries_data.append({
                    "vid_id": vid_id,
                    "url": full_url,
                    "title": e.get("title", "Noma'lum"),
                    "duration": e.get("duration"),
                })

            # QUERY_STORE — video_id → qo'shiq nomi
            for e in entries_data:
                if e["vid_id"]:
                    QUERY_STORE[e["vid_id"]] = query

            lines = []
            for i, e in enumerate(entries_data, 1):
                dur = fmt_duration(e.get("duration"))
                t = e["title"][:52]
                lines.append(f"*{i}.* {t}  `{dur}`" if dur else f"*{i}.* {t}")

            caption = (
                f"🎵 *{song_title}*\n"
                f"👤 {artist}\n\n"
                + "\n".join(lines)
                + "\n\n_Raqamga bosib yuklab oling 👇_"
            )

            # Yangi format: ytdl:{vid_id}
            keyboard = [[
                InlineKeyboardButton(str(i + 1), callback_data=f"ytdl:{e['vid_id']}")
                for i, e in enumerate(entries_data)
                if e["vid_id"]
            ]]

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
            logger.exception("Shazam xatosi")
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


# ─── main ─────────────────────────────────────────────────────────────────────

async def error_handler(update, context):
    logger.error("Xato: %s", context.error)


async def async_main():
    if not BOT_TOKEN:
        raise ValueError("BOT_TOKEN muhit o'zgaruvchisi o'rnatilmagan!")

    RAILWAY_DOMAIN = os.environ.get("RAILWAY_PUBLIC_DOMAIN", "")

    builder = (
        Application.builder()
        .token(BOT_TOKEN)
        .read_timeout(30)
        .write_timeout(120)
        .connect_timeout(30)
        .pool_timeout(10)
    )
    if RAILWAY_DOMAIN:
        builder = builder.updater(None)
    app = builder.build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_message))
    app.add_handler(MessageHandler(filters.VOICE, handle_voice))
    app.add_handler(MessageHandler(filters.AUDIO, handle_voice))
    app.add_handler(MessageHandler(filters.VIDEO, handle_video_shazam))
    app.add_handler(MessageHandler(filters.VIDEO_NOTE, handle_video_shazam))
    app.add_handler(CallbackQueryHandler(handle_callback))
    app.add_error_handler(error_handler)

    if RAILWAY_DOMAIN:
        webhook_url = f"https://{RAILWAY_DOMAIN}/{BOT_TOKEN}"

        async def webhook_route(request):
            try:
                data = await request.json()
                update = Update.de_json(data, app.bot)
                await app.update_queue.put(update)
            except Exception as e:
                logger.error("Webhook parse xatosi: %s", e)
            return web.Response(text="OK")

        web_server = web.Application()
        web_server.router.add_get("/", _health_handler)
        web_server.router.add_get("/health", _health_handler)
        web_server.router.add_post(f"/{BOT_TOKEN}", webhook_route)

        runner = web.AppRunner(web_server)
        await runner.setup()
        site = web.TCPSite(runner, "0.0.0.0", PORT)
        await site.start()
        logger.info("Server port %d da ishga tushdi", PORT)

        async with app:
            await app.start()
            await app.bot.set_webhook(
                url=webhook_url,
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            logger.info("=== BOT WEBHOOK MODE DA ISHGA TUSHDI: %s ===", webhook_url)
            await asyncio.Event().wait()
            await app.bot.delete_webhook()
            await app.stop()

        await runner.cleanup()
    else:
        health_runner = await start_health_server(PORT)
        async with app:
            await app.start()
            await app.updater.start_polling(
                allowed_updates=Update.ALL_TYPES,
                drop_pending_updates=True,
            )
            logger.info("Bot ishga tushdi (polling mode)...")
            await asyncio.Event().wait()
            await app.updater.stop()
            await app.stop()
        await health_runner.cleanup()


def main():
    asyncio.run(async_main())


if __name__ == "__main__":
    main()
