FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1 \
    PIP_ROOT_USER_ACTION=ignore

# ffmpeg: videodan audio ajratish va birlashtirish uchun
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# YouTube uchun yt-dlp ga JavaScript runtime (Deno) kerak
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

# Tez ishga tushishi uchun yt-dlp ni ishga tushganda yangilamaymiz:
# Render'da "Clear build cache & deploy" qilsangiz, eng yangi versiya oʻrnatiladi.
CMD ["python", "bot.py"]
