"""
Telegram bot: YouTube / TikTok / Instagram havolasi -> video + musiqa.
Render (Web Service, webhook rejimi) uchun moslangan.

1. Foydalanuvchi havola yuboradi
2. Bot videoni yuklab, chatga yuboradi
3. Videodagi musiqani Shazam orqali aniqlaydi
4. Topilsa -> nomi + MP3 fayl yuboradi, topilmasa -> "Musiqa topilmadi"
"""
import asyncio
import hashlib
import logging
import os
import re
import shutil
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import CommandStart
from aiogram.types import FSInputFile, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
from aiohttp import web
from shazamio import Shazam
from yt_dlp import YoutubeDL
from yt_dlp.utils import match_filter_func

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
PORT = int(os.getenv("PORT", "10000"))  # Render portni oʻzi beradi
# Render web servislar uchun RENDER_EXTERNAL_URL ni avtomatik beradi
BASE_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()  # alohida sozlash shart emas
MAX_SIZE = 50 * 1024 * 1024  # Telegram Bot API yuklash limiti: 50 MB
MAX_PARALLEL = 2  # Render Free = 512 MB xotira, shuning uchun kam


def prepare_cookies() -> str | None:
    """Render "Secret Files"dagi cookies.txt ni yozish mumkin boʻlgan joyga nusxalaydi."""
    src = Path(os.getenv("COOKIES_FILE", "/etc/secrets/cookies.txt"))
    if not src.exists():
        return None
    dst = Path(tempfile.gettempdir()) / "cookies.txt"
    shutil.copyfile(src, dst)
    return str(dst)


COOKIES_FILE = prepare_cookies()

SUPPORTED_DOMAINS = (
    "youtube.com",
    "youtu.be",
    "tiktok.com",
    "instagram.com",
    "facebook.com",
    "fb.watch",
    "twitter.com",
    "x.com",
)

URL_RE = re.compile(r"https?://\S+")
VIDEO_FORMATS = [
    "bv*[height<=720]+ba/b[height<=720]/b",
    "bv*[height<=360]+ba/b[height<=360]/w",  # katta bo‘lib ketsa, past sifat
]

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("video_bot")

dp = Dispatcher()
shazam = Shazam()
semaphore = asyncio.Semaphore(MAX_PARALLEL)


class VideoTooLarge(Exception):
    pass


# ---------------------------------------------------------------- yordamchilar
def is_supported(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)


def base_opts() -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if COOKIES_FILE and Path(COOKIES_FILE).exists():
        opts["cookiefile"] = COOKIES_FILE
    return opts


def download_video(url: str, out_dir: Path) -> Path:
    """Videoni yuklaydi (bloklovchi funksiya, thread ichida chaqiriladi)."""
    found_any = False
    for fmt in VIDEO_FORMATS:
        for old in out_dir.glob("video.*"):
            old.unlink()
        opts = base_opts() | {
            "outtmpl": str(out_dir / "video.%(ext)s"),
            "format": fmt,
            "merge_output_format": "mp4",
        }
        with YoutubeDL(opts) as ydl:
            ydl.download([url])
        files = list(out_dir.glob("video.*"))
        if not files:
            continue
        found_any = True
        if files[0].stat().st_size <= MAX_SIZE:
            return files[0]
    if found_any:
        raise VideoTooLarge()
    raise RuntimeError("Video fayli topilmadi")


async def extract_audio(video: Path, out_dir: Path) -> Path | None:
    """Videodan dastlabki 30 soniyalik audio ajratadi (aniqlash uchun yetarli)."""
    audio = out_dir / "audio.mp3"
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-i", str(video),
        "-vn", "-t", "30", "-ac", "1", "-ar", "44100", "-b:a", "128k",
        str(audio),
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    if proc.returncode == 0 and audio.exists():
        return audio
    return None  # videoda ovoz yo‘q bo‘lishi mumkin


async def recognize_music(audio: Path) -> tuple[str, str] | None:
    """Shazam orqali musiqani aniqlaydi -> (nomi, ijrochi) yoki None."""
    try:
        result = await shazam.recognize(str(audio))
    except Exception:
        log.exception("Shazam xatosi")
        return None
    track = (result or {}).get("track")
    if not track:
        return None
    return track.get("title", "Nomaʼlum"), track.get("subtitle", "Nomaʼlum")


def download_song(query: str, out_dir: Path) -> Path | None:
    """Aniqlangan musiqaning MP3 faylini YouTube’dan qidirib yuklaydi."""
    opts = base_opts() | {
        "outtmpl": str(out_dir / "song.%(ext)s"),
        "format": "bestaudio/best",
        "match_filter": match_filter_func("duration < 600"),  # 10 daqiqadan uzun emas
        "postprocessors": [
            {"key": "FFmpegExtractAudio", "preferredcodec": "mp3", "preferredquality": "192"}
        ],
    }
    with YoutubeDL(opts) as ydl:
        ydl.download([f"ytsearch1:{query}"])
    files = list(out_dir.glob("song.mp3"))
    if files and files[0].stat().st_size <= MAX_SIZE:
        return files[0]
    return None


# ---------------------------------------------------------------- handlerlar
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Salom! 👋\n\n"
        "Menga YouTube, TikTok yoki Instagram video havolasini yuboring — "
        "men videoni yuklab beraman va undagi musiqani ham topishga harakat qilaman 🎵"
    )


@dp.message(F.text.regexp(URL_RE))
async def handle_link(message: Message):
    url = URL_RE.search(message.text).group(0)
    if not is_supported(url):
        await message.answer("❌ Bu havola qoʻllab-quvvatlanmaydi. YouTube, TikTok yoki Instagram havolasini yuboring.")
        return

    status = await message.answer("⏳ Video yuklanmoqda...")
    tmp = Path(tempfile.mkdtemp(prefix="vbot_"))
    try:
        async with semaphore:
            # 1) Videoni yuklash
            try:
                video = await asyncio.to_thread(download_video, url, tmp)
            except VideoTooLarge:
                await status.edit_text("❌ Video 50 MB dan katta, Telegram orqali yuborib boʻlmaydi.")
                return
            except Exception:
                log.exception("Video yuklashda xato: %s", url)
                await status.edit_text(
                    "❌ Videoni yuklab boʻlmadi. Havola toʻgʻri va video ochiq ekanini tekshiring."
                )
                return

            # 2) Videoni yuborish
            await status.edit_text("📤 Video yuborilmoqda...")
            await message.answer_video(FSInputFile(video), supports_streaming=True, caption="🎬 Video tayyor")

            # 3) Musiqani aniqlash
            await status.edit_text("🎵 Musiqa qidirilmoqda...")
            audio = await extract_audio(video, tmp)
            music = await recognize_music(audio) if audio else None

            if not music:
                await status.edit_text("🎵 Musiqa topilmadi.")
                return

            title, artist = music
            await status.edit_text(f"🎵 Musiqa topildi:\n{artist} — {title}\n\n⏳ Audio yuklanmoqda...")

            # 4) Musiqaning MP3 faylini yuborish
            try:
                song = await asyncio.to_thread(download_song, f"{artist} {title}", tmp)
            except Exception:
                log.exception("Musiqani yuklashda xato")
                song = None

            if song:
                await message.answer_audio(FSInputFile(song), title=title, performer=artist)
                await status.edit_text(f"🎵 Musiqa topildi:\n{artist} — {title}")
            else:
                await status.edit_text(
                    f"🎵 Musiqa topildi:\n{artist} — {title}\n\n(Audio faylini yuklab boʻlmadi)"
                )
    except Exception:
        log.exception("Kutilmagan xato")
        await status.edit_text("❌ Xatolik yuz berdi. Keyinroq qayta urinib koʻring.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dp.message()
async def fallback(message: Message):
    await message.answer("Iltimos, video havolasini yuboring (YouTube, TikTok yoki Instagram).")


async def on_startup(bot: Bot) -> None:
    await bot.set_webhook(
        f"{BASE_URL}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message"],
    )
    log.info("Webhook oʻrnatildi: %s%s", BASE_URL, WEBHOOK_PATH)


async def health(_request: web.Request) -> web.Response:
    return web.Response(text="OK")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. Render > Environment boʻlimiga qoʻshing.")
    if not BASE_URL:
        raise SystemExit("RENDER_EXTERNAL_URL yoki WEBHOOK_URL topilmadi.")

    bot = Bot(BOT_TOKEN, session=AiohttpSession(timeout=300))
    dp.startup.register(on_startup)

    app = web.Application()
    app.router.add_get("/", health)  # Render health check / uyg‘otish uchun
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(
        app, path=WEBHOOK_PATH
    )
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
