"""
Telegram bot: YouTube / TikTok / Instagram havolasi -> video + musiqa.
Render (Web Service, webhook rejimi) uchun moslangan.
"""
import asyncio
import hashlib
import logging
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from urllib.parse import urlparse

from aiogram import Bot, Dispatcher, F
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.filters import Command, CommandObject, CommandStart
from aiogram.types import BufferedInputFile, FSInputFile, Message
from aiogram.webhook.aiohttp_server import SimpleRequestHandler, setup_application
import aiohttp
from aiohttp import web
from shazamio import Shazam
from yt_dlp import YoutubeDL
from yt_dlp.utils import MaxDownloadsReached
from yt_dlp.version import __version__ as YTDLP_VERSION

BOT_TOKEN = os.getenv("BOT_TOKEN", "")
ADMIN_ID = int(os.getenv("ADMIN_ID", "0") or 0)  # ixtiyoriy: xatolar shu ID ga yuboriladi
PORT = int(os.getenv("PORT", "10000"))  # Render portni oʻzi beradi
# Render web servislar uchun RENDER_EXTERNAL_URL ni avtomatik beradi
PROXY_URL = os.getenv("PROXY_URL")  # ixtiyoriy: faqat YouTube uchun ishlat
BASE_URL = (os.getenv("WEBHOOK_URL") or os.getenv("RENDER_EXTERNAL_URL") or "").rstrip("/")
WEBHOOK_PATH = "/webhook"
WEBHOOK_SECRET = hashlib.sha256(BOT_TOKEN.encode()).hexdigest()
MAX_SIZE = 50 * 1024 * 1024  # Telegram Bot API yuklash limiti: 50 MB
MAX_PARALLEL = 2  # Render Free = 512 MB xotira va 0.1 CPU, shuning uchun kam
KEEP_ALIVE = os.getenv("KEEP_ALIVE", "1") != "0"  # uyquga ketishning oldini olish
KEEP_ALIVE_EVERY = 600  # soniya
# 50 MB ga sigʻadigan eng yaxshi sifatni bir urinishdayoq tanlaydi (filesize<? — noma’lum hajmga ruxsat)
VIDEO_FORMAT = (
    "bv*[filesize<?44M][filesize_approx<?44M]+ba/"
    "b[filesize<?48M][filesize_approx<?48M]/"
    "bv*+ba/b"
)
RES_LADDER = [1080, 720, 480, 360]  # avval eng yaxshi sifat, sigʻmasa pastroq
# YouTube uchun usullar (ketma-ket sinab koʻriladi). tv va web_safari akkaunt cookies bilan
# PO token talab qilmaydi (yt-dlp PO Token Guide).
YT_VARIANTS: list[dict] = [
    {},  # yt-dlp ning oʻz tanlovi
    {"youtube": {"player_client": ["tv", "web_safari"]}},
    {"youtube": {"player_client": ["web_safari"]}},
    {"youtube": {"player_client": ["android_vr"]}},
    {"youtube": {"player_client": ["web_embedded"]}},
]


def variant_name(v: dict) -> str:
    return ",".join(v["youtube"]["player_client"]) if v else "default"

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

logging.basicConfig(level=logging.INFO)
log = logging.getLogger("video_bot")


def prepare_cookies() -> str | None:
    """Render "Secret Files"dagi cookies.txt ni yozish mumkin boʻlgan joyga nusxalaydi."""
    src = Path(os.getenv("COOKIES_FILE", "/etc/secrets/cookies.txt"))
    if not src.exists():
        return None
    dst = Path(tempfile.gettempdir()) / "cookies.txt"
    shutil.copyfile(src, dst)
    return str(dst)


COOKIES_FILE = prepare_cookies()
log.info("yt-dlp %s | cookies: %s", YTDLP_VERSION, "bor" if COOKIES_FILE else "yoʻq")

dp = Dispatcher()
shazam = Shazam()
semaphore = asyncio.Semaphore(MAX_PARALLEL)


class VideoTooLarge(Exception):
    pass


# ------------------------------------------------------------------ yordamchilar
def is_supported(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return any(host == d or host.endswith("." + d) for d in SUPPORTED_DOMAINS)


def is_youtube(url: str) -> bool:
    host = (urlparse(url).hostname or "").lower()
    return host.endswith("youtube.com") or host.endswith("youtu.be")


def base_opts(use_proxy: bool = False) -> dict:
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if COOKIES_FILE:
        opts["cookiefile"] = COOKIES_FILE
    if use_proxy and PROXY_URL:
        opts["proxy"] = PROXY_URL
    return opts


def safe_name(text: str) -> str:
    return re.sub(r'[\\/:*?"<>|]+', " ", text).strip()[:80] or "audio"


def friendly_error(err: Exception) -> str:
    """Foydalanuvchiga koʻrsatiladigan tushunarli xabar."""
    t = str(err).lower()
    if "not a bot" in t or "confirm you" in t:
        return "❌ YouTube hozir bu serverdan videoni bermayapti. Keyinroq urinib koʻring."
    if "login" in t or "log in" in t or "cookie" in t or "rate-limit" in t:
        return "❌ Bu videoni olish uchun tizimga kirish talab qilinmoqda (platforma cheklovi)."
    if "private" in t:
        return "❌ Bu video yopiq (private)."
    if "unavailable" in t or "not available" in t or "removed" in t or "404" in t:
        return "❌ Video topilmadi yoki oʻchirilgan."
    return "❌ Videoni yuklab boʻlmadi. Havolani tekshirib, qayta urinib koʻring."


def admin_hint(err: Exception) -> str:
    t = str(err).lower()
    if "not a bot" in t or "confirm you" in t:
        return "👉 YouTube serverni blokladi. Render > Secret Files ga cookies.txt qoʻshing."
    if "login" in t or "cookie" in t or "rate-limit" in t:
        return "👉 Cookies kerak (Instagram/YouTube). cookies.txt ni yangilang."
    return ""


async def notify_admin(bot: Bot, text: str) -> None:
    if not ADMIN_ID:
        return
    try:
        await bot.send_message(ADMIN_ID, text[:3500])
    except Exception:
        log.exception("Adminga xabar yuborib boʻlmadi")


# ------------------------------------------------------------------ diagnostika
class ListLogger:
    """yt-dlp xabarlarini roʻyxatga yigʻadi."""

    def __init__(self) -> None:
        self.lines: list[str] = []

    def debug(self, msg) -> None:
        self.lines.append(str(msg))

    def info(self, msg) -> None:
        self.lines.append(str(msg))

    def warning(self, msg) -> None:
        self.lines.append("WARNING: " + str(msg))

    def error(self, msg) -> None:
        self.lines.append("ERROR: " + str(msg))


def tool_version(name: str) -> str:
    path = shutil.which(name)
    if not path:
        return "yoq"
    try:
        out = subprocess.run([path, "--version"], capture_output=True, text=True, timeout=10)
        return ((out.stdout or out.stderr).splitlines() or ["?"])[0]
    except Exception as e:
        return f"xato: {e}"


def try_variant(url: str, extractor_args: dict, verbose: bool) -> tuple[str, list[str]]:
    """Bitta usul bilan eng past sifatni haqiqatan yuklab koʻradi (403 xatolarini ham aniqlaydi)."""
    logger = ListLogger()
    with tempfile.TemporaryDirectory(prefix="dbg_") as d:
        opts = base_opts(use_proxy=is_youtube(url)) | {
            "verbose": verbose,
            "logger": logger,
            "quiet": False,
            "no_warnings": False,
            "format": "worst",
            "outtmpl": f"{d}/t.%(ext)s",
            "max_filesize": 20 * 1024 * 1024,
        }
        if extractor_args:
            opts["extractor_args"] = extractor_args
        try:
            with YoutubeDL(opts) as ydl:
                ydl.extract_info(url, download=True)
            size = sum(f.stat().st_size for f in Path(d).glob("t.*"))
            return f"OK ({size // 1024} KB yuklandi)", logger.lines
        except Exception as e:
            return f"XATO — {str(e)[:300]}", logger.lines


def run_debug(url: str) -> str:
    """Havolani barcha usullar bilan tekshirib, hisobot qaytaradi (parol/cookie qiymatlarisiz)."""
    head = [
        f"yt-dlp: {YTDLP_VERSION}",
        f"ffmpeg: {tool_version('ffmpeg')[:60]}",
        f"deno: {tool_version('deno')}",
        f"proxy: {'bor' if PROXY_URL else 'yoq'}",
    ]
    if COOKIES_FILE:
        raw = Path(COOKIES_FILE).read_text(errors="ignore")
        head.append(
            f"cookies: bor, {len(raw.splitlines())} qator, "
            f"LOGIN_INFO={'LOGIN_INFO' in raw}, SAPISID={'SAPISID' in raw}"
        )
    else:
        head.append("cookies: yoq")

    variants = YT_VARIANTS if is_youtube(url) else [{}]
    summary: list[str] = []
    first_log: list[str] = []
    for i, v in enumerate(variants):
        result, lines = try_variant(url, v, verbose=(i == 0))
        summary.append(f"[{variant_name(v)}] {result}")
        if i == 0:
            first_log = lines

    text = "\n".join(
        head + ["", "NATIJALAR:"] + summary + ["", "--- yt-dlp log (birinchi usul) ---"] + first_log
    )
    return text[:40000]


# ------------------------------------------------------------------ video
def _download_once(url: str, out_dir: Path, extractor_args: dict) -> tuple[Path, dict]:
    """
    Bitta usul bilan yuklaydi. "res" — eng kichik oʻlcham boʻyicha (vertikal videolar ham toʻgʻri).
    Avval 1080p, fayl 50 MB dan oshsa 720p, 480p, 360p.
    """
    too_big = False
    for res in RES_LADDER:
        for old in out_dir.glob("video.*"):
            old.unlink()
        opts = base_opts(use_proxy=is_youtube(url)) | {
            "outtmpl": str(out_dir / "video.%(ext)s"),
            "format": VIDEO_FORMAT,
            "format_sort": [f"res:{res}", "vcodec:h264", "acodec:aac"],
            "merge_output_format": "mp4",
            "playlist_items": "1",
            "max_filesize": MAX_SIZE,
            "concurrent_fragment_downloads": 4,
            "postprocessor_args": {"merger": ["-movflags", "+faststart"]},
        }
        if extractor_args:
            opts["extractor_args"] = extractor_args
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=True)
        if info and info.get("_type") == "playlist" and info.get("entries"):
            info = info["entries"][0]

        files = [f for f in out_dir.glob("video.*") if f.suffix != ".part"]
        if not files:
            too_big = True  # max_filesize sababli yuklanmadi
            continue
        if files[0].stat().st_size <= MAX_SIZE:
            return files[0], info or {}
        too_big = True
    if too_big:
        raise VideoTooLarge()
    raise RuntimeError("Video fayli topilmadi")


def download_video(url: str, out_dir: Path) -> tuple[Path, dict]:
    """YouTube uchun bir necha usulni ketma-ket sinaydi, boshqa saytlar uchun bittasini."""
    variants = YT_VARIANTS if is_youtube(url) else [{}]
    errors: list[str] = []
    first_err: Exception | None = None
    for v in variants:
        try:
            return _download_once(url, out_dir, v)
        except VideoTooLarge:
            raise
        except Exception as e:
            log.warning("[%s] usuli muvaffaqiyatsiz: %s", variant_name(v), str(e)[:300])
            errors.append(f"[{variant_name(v)}] {str(e)[:300]}")
            first_err = first_err or e
    assert first_err is not None
    first_err.all_errors = errors  # type: ignore[attr-defined]
    raise first_err


# ------------------------------------------------------------------ ffmpeg
async def run_ffmpeg(*args: str) -> bool:
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg", "-y", "-loglevel", "error", *args,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.DEVNULL,
    )
    await proc.wait()
    return proc.returncode == 0


async def get_duration(path: Path) -> float:
    proc = await asyncio.create_subprocess_exec(
        "ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=nw=1:nk=1", str(path),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    try:
        return float(out.decode().strip())
    except ValueError:
        return 0.0


async def cut_clip(video: Path, out: Path, start: float, length: int = 20) -> bool:
    """Shazam uchun qisqa WAV parcha (kodlashsiz — 0.1 CPU da tez ishlaydi)."""
    return await run_ffmpeg(
        "-ss", str(int(start)), "-i", str(video),
        "-t", str(length), "-vn", "-ac", "1", "-ar", "16000", "-c:a", "pcm_s16le", str(out),
    )


async def audio_from_video(video: Path, out_dir: Path) -> Path | None:
    """Videodagi ovozni qayta kodlamasdan (copy) M4A qiladi; ilojsiz boʻlsa MP3."""
    m4a = out_dir / "voice.m4a"
    if await run_ffmpeg("-i", str(video), "-vn", "-c:a", "copy", str(m4a)):
        if m4a.exists() and 0 < m4a.stat().st_size <= MAX_SIZE:
            return m4a
    mp3 = out_dir / "voice.mp3"
    if await run_ffmpeg("-i", str(video), "-vn", "-ac", "2", "-b:a", "128k", str(mp3)):
        if mp3.exists() and 0 < mp3.stat().st_size <= MAX_SIZE:
            return mp3
    return None


async def to_telegram_audio(path: Path) -> Path | None:
    """Telegram audio sifatida faqat MP3/M4A ni qabul qiladi; boshqa formatni M4A ga oʻtkazadi."""
    if path.suffix.lower() in (".m4a", ".mp3"):
        return path
    out = path.with_name("converted.m4a")
    if await run_ffmpeg("-i", str(path), "-vn", "-c:a", "aac", "-b:a", "128k", str(out)):
        return out
    return None


# ------------------------------------------------------------------ musiqa
async def recognize(clip: Path) -> tuple[str, str] | None:
    try:
        func = getattr(shazam, "recognize", None) or shazam.recognize_song
        result = await func(str(clip))
    except Exception:
        log.exception("Shazam xatosi")
        return None
    track = (result or {}).get("track")
    if not track:
        return None
    return track.get("title", "Nomaʼlum"), track.get("subtitle", "Nomaʼlum")


async def find_music(
    video: Path, out_dir: Path, duration: float | None = None
) -> tuple[str, str] | None:
    """Videoning bir necha joyidan 20 soniyalik parcha olib, Shazam orqali bir vaqtda aniqlaydi."""
    dur = float(duration or 0) or await get_duration(video)
    starts = [0.0]
    if dur > 45:
        starts += [dur * 0.35, dur * 0.7]

    async def probe(i: int, start: float):
        clip = out_dir / f"clip{i}.wav"
        if not await cut_clip(video, clip, start):
            return None
        return await recognize(clip)

    tasks = [asyncio.create_task(probe(i, st)) for i, st in enumerate(starts)]
    found = None
    try:
        for fut in asyncio.as_completed(tasks):
            result = await fut
            if result:
                found = result
                break
    finally:
        for t in tasks:
            t.cancel()
    return found


def _norm(text: str) -> str:
    text = re.sub(r"[\(\[].*?[\)\]]", " ", text.lower())
    text = re.sub(r"[^\w\s]", " ", text)
    return " ".join(text.split())


def make_song_filter(title: str):
    """Davomiyligi 45–600 soniya va nomida qoʻshiq nomi bor natijalarnigina qabul qiladi."""
    want = _norm(title)

    def _filter(info, incomplete=False):
        if incomplete:
            return None
        dur = info.get("duration")
        if dur and not (45 < dur < 600):
            return "davomiyligi mos emas"
        if want and want not in _norm(info.get("title") or ""):
            return "nomi mos emas"
        return None

    return _filter


def download_song_from(prefix: str, query: str, title: str, work_dir: Path) -> Path | None:
    """Bitta manbadan (SoundCloud yoki YouTube) qoʻshiqni qayta kodlamasdan yuklaydi."""
    work_dir.mkdir(parents=True, exist_ok=True)
    opts = base_opts(use_proxy=prefix.startswith("yt")) | {
        "outtmpl": str(work_dir / "song.%(ext)s"),
        "format": "bestaudio[ext=m4a]/bestaudio[ext=mp3]/bestaudio/best",
        "match_filter": make_song_filter(title),
        "max_downloads": 1,
        "concurrent_fragment_downloads": 4,
    }
    try:
        with YoutubeDL(opts) as ydl:
            ydl.download([f"{prefix}{query}"])
    except MaxDownloadsReached:
        pass  # 1 ta fayl yuklandi — hammasi joyida
    files = [f for f in work_dir.glob("song.*") if f.suffix != ".part"]
    return files[0] if files else None


async def find_song(artist: str, title: str, out_dir: Path) -> Path | None:
    """Toʻliq qoʻshiqni qidiradi. SoundCloud yengil va tez, shuning uchun birinchi; keyin YouTube."""
    query = f"{artist} - {title}"
    for i, prefix in enumerate(("scsearch3:", "ytsearch3:")):
        try:
            path = await asyncio.to_thread(download_song_from, prefix, query, title, out_dir / f"song{i}")
        except Exception as e:
            log.warning("%s qidiruvi muvaffaqiyatsiz: %s", prefix, str(e)[:300])
            continue
        if not path:
            continue
        path = await to_telegram_audio(path)
        if path and path.stat().st_size <= MAX_SIZE:
            return path
    return None


# ------------------------------------------------------------------ handlerlar
@dp.message(CommandStart())
async def start(message: Message):
    await message.answer(
        "Salom! 👋\n\n"
        "Menga YouTube, TikTok yoki Instagram video havolasini yuboring — "
        "men videoni yuklab beraman va undagi musiqani ham topishga harakat qilaman 🎵"
    )


@dp.message(Command("debug"))
async def debug_cmd(message: Message, command: CommandObject):
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        return
    url = (command.args or "").strip() or "https://www.youtube.com/watch?v=jNQXAC9IVRw"
    status = await message.answer("🔍 Tekshirilmoqda, 20–40 soniya kuting...")
    text = await asyncio.to_thread(run_debug, url)
    await message.answer_document(BufferedInputFile(text.encode("utf-8"), filename="debug.txt"))
    await status.delete()


@dp.message(F.text.regexp(URL_RE))
async def handle_link(message: Message):
    url = URL_RE.search(message.text).group(0)
    if not is_supported(url):
        await message.answer(
            "❌ Bu havola qoʻllab-quvvatlanmaydi. YouTube, TikTok yoki Instagram havolasini yuboring."
        )
        return

    bot = message.bot
    status = await message.answer("⏳ Video yuklanmoqda...")
    tmp = Path(tempfile.mkdtemp(prefix="vbot_"))
    try:
        async with semaphore:
            # 1) Videoni yuklash
            try:
                video, info = await asyncio.to_thread(download_video, url, tmp)
            except VideoTooLarge:
                await status.edit_text("❌ Video 50 MB dan katta, Telegram orqali yuborib boʻlmaydi.")
                return
            except Exception as e:
                log.exception("Video yuklashda xato: %s", url)
                await status.edit_text(friendly_error(e))
                details = "\n".join(getattr(e, "all_errors", None) or [str(e)[:1500]])
                await notify_admin(bot, f"❗ Yuklash xatosi\n{url}\n\n{details[:2500]}\n\n{admin_hint(e)}")
                return

            # 2) Videoni yuborish va musiqani aniqlash BIR VAQTDA
            await status.edit_text("📤 Video yuborilmoqda...\n🎵 Musiqa ham qidirilmoqda...")
            music_task = asyncio.create_task(find_music(video, tmp, info.get("duration")))
            try:
                await message.answer_video(
                    FSInputFile(video),
                    supports_streaming=True,
                    width=info.get("width"),
                    height=info.get("height"),
                    duration=int(info["duration"]) if info.get("duration") else None,
                    caption="🎬 Video tayyor",
                )
            except BaseException:
                music_task.cancel()
                raise

            music = await music_task
            if not music:
                await status.edit_text("🎵 Musiqa topilmadi.")
                return

            title, artist = music
            found_text = f"🎵 Musiqa topildi:\n{artist} — {title}"
            await status.edit_text(f"{found_text}\n\n⏳ Audio fayl qidirilmoqda...")

            # 3) Toʻliq qoʻshiq (SoundCloud -> YouTube), topilmasa videodagi ovoz
            song = await find_song(artist, title, tmp)
            base = f"{safe_name(artist)} - {safe_name(title)}"
            if song:
                await message.answer_audio(
                    FSInputFile(song, filename=f"{base}{song.suffix}"), title=title, performer=artist
                )
                await status.edit_text(found_text)
                return

            voice = await audio_from_video(video, tmp)
            if voice:
                await message.answer_audio(
                    FSInputFile(voice, filename=f"{base}{voice.suffix}"),
                    title=title,
                    performer=artist,
                    caption="ℹ️ Toʻliq qoʻshiq topilmadi, videodagi ovoz yuborildi.",
                )
                await status.edit_text(found_text)
            else:
                await status.edit_text(f"{found_text}\n\n(Audio faylini yuklab boʻlmadi)")
    except Exception as e:
        log.exception("Kutilmagan xato")
        await status.edit_text("❌ Xatolik yuz berdi. Keyinroq qayta urinib koʻring.")
        await notify_admin(bot, f"❗ Kutilmagan xato\n{url}\n\n{str(e)[:1500]}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


@dp.message()
async def fallback(message: Message):
    await message.answer("Iltimos, video havolasini yuboring (YouTube, TikTok yoki Instagram).")


# ------------------------------------------------------------------ server
async def on_startup(bot: Bot) -> None:
    await bot.set_webhook(
        f"{BASE_URL}{WEBHOOK_PATH}",
        secret_token=WEBHOOK_SECRET,
        allowed_updates=["message"],
    )
    log.info("Webhook oʻrnatildi: %s%s", BASE_URL, WEBHOOK_PATH)


async def keep_alive_loop() -> None:
    """Oʻz manziliga har 10 daqiqada soʻrov yuborib, Render’ning 15 daqiqalik uyqusini oldini oladi."""
    await asyncio.sleep(120)
    timeout = aiohttp.ClientTimeout(total=30)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        while True:
            try:
                async with session.get(f"{BASE_URL}/") as resp:
                    await resp.read()
            except Exception as e:
                log.warning("Keep-alive xatosi: %s", e)
            await asyncio.sleep(KEEP_ALIVE_EVERY)


async def start_background(app: web.Application) -> None:
    if KEEP_ALIVE:
        app["keep_alive"] = asyncio.create_task(keep_alive_loop())


async def stop_background(app: web.Application) -> None:
    task = app.get("keep_alive")
    if task:
        task.cancel()


async def health(_request: web.Request) -> web.Response:
    cookies = "bor" if COOKIES_FILE else "yoq"
    return web.Response(text=f"OK | yt-dlp {YTDLP_VERSION} | cookies: {cookies}")


def main() -> None:
    if not BOT_TOKEN:
        raise SystemExit("BOT_TOKEN topilmadi. Render > Environment boʻlimiga qoʻshing.")
    if not BASE_URL:
        raise SystemExit("RENDER_EXTERNAL_URL yoki WEBHOOK_URL topilmadi.")

    bot = Bot(BOT_TOKEN, session=AiohttpSession(timeout=300))
    dp.startup.register(on_startup)

    app = web.Application()
    app.on_startup.append(start_background)
    app.on_cleanup.append(stop_background)
    app.router.add_get("/", health)  # Render health check / uygʻotish / holatni koʻrish
    SimpleRequestHandler(dispatcher=dp, bot=bot, secret_token=WEBHOOK_SECRET).register(
        app, path=WEBHOOK_PATH
    )
    setup_application(app, dp, bot=bot)
    web.run_app(app, host="0.0.0.0", port=PORT)


if __name__ == "__main__":
    main()
