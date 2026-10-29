FROM python:3.12-slim

ENV PYTHONUNBUFFERED=1

# ffmpeg: videodan audio ajratish va MP3 ga aylantirish uchun
RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg \
    && rm -rf /var/lib/apt/lists/*

# YouTube uchun yt-dlp ga JavaScript runtime (Deno) kerak
COPY --from=denoland/deno:bin /deno /usr/local/bin/deno

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY bot.py .

CMD ["python", "bot.py"]
