"""Короткие вертикальные ролики (YouTube Shorts / Instagram Reels) из игровых новостей.

Запускается после основного бота. В заданное время (по Москве) выбирает самую
интересную свежую новость, пишет сценарий, озвучивает мужским голосом, собирает
видео 1080x1920 и присылает его владельцу в Telegram вместе с текстами для загрузки.
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from io import BytesIO

import feedparser
import requests
from PIL import Image, ImageDraw, ImageFilter, ImageFont

import bot  # берём источники, поиск картинок и ИИ из основного бота

# ====== НАСТРОЙКИ ======
VIDEO_TIMES = os.getenv("VIDEO_TIMES", "12:00,19:00")  # когда делать ролики, по Москве
VOICE = os.getenv("VIDEO_VOICE", "ru-RU-DmitryNeural")  # мужской голос
VOICE_RATE = os.getenv("VIDEO_VOICE_RATE", "+8%")       # скорость речи
CHANNEL_NAME = os.getenv("VIDEO_CHANNEL_NAME", "Первый игровой")
FORCE = os.getenv("VIDEO_FORCE", "").lower() in ("1", "true", "yes")
OWNER_ID = os.getenv("TELEGRAM_OWNER_ID", "")
STATE_FILE = "video_state.json"
LOGO_FILE = "logo.png"  # необязательно: загрузите логотип канала в репозиторий

MSK = timezone(timedelta(hours=3))
W, H, FPS = 1080, 1920, 30
IMG_W, IMG_H, IMG_Y = 1000, 562, 270
SUB_Y, SUB_H = 1150, 260

BG = (24, 25, 64)
ACCENT = (46, 211, 198)
WHITE = (255, 255, 255)


# ====== СОСТОЯНИЕ ======
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            s = json.load(f)
    else:
        s = {}
    s.setdefault("done", [])
    s.setdefault("used", [])
    return s


def save_state(s):
    s["done"] = s["done"][-60:]
    s["used"] = s["used"][-300:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(s, f, ensure_ascii=False, indent=1)


def due_slot(state, now):
    """Возвращает метку слота, если пора делать ролик (в течение 3 часов после времени слота)."""
    local = now.astimezone(MSK)
    for t in [x.strip() for x in VIDEO_TIMES.split(",") if x.strip()]:
        h, m = map(int, t.split(":"))
        start = local.replace(hour=h, minute=m, second=0, microsecond=0)
        label = start.strftime("%Y-%m-%d %H:%M")
        if start <= local < start + timedelta(hours=3) and label not in state["done"]:
            return label
    return None


# ====== ШРИФТЫ ======
def find_font(bold=True):
    names = ["DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"]
    for d in ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF"):
        for n in names:
            p = os.path.join(d, n)
            if os.path.exists(p):
                return p
    try:
        q = "DejaVu Sans:bold" if bold else "DejaVu Sans"
        p = subprocess.run(["fc-match", q, "-f", "%{file}"], capture_output=True, text=True).stdout.strip()
        if p and os.path.exists(p):
            return p
    except Exception:
        pass
    subprocess.run(["sudo", "apt-get", "install", "-y", "-q", "fonts-dejavu-core"], check=False)
    return f"/usr/share/fonts/truetype/dejavu/{names[0]}"


FONT_BOLD = None
FONT_REG = None


def font(size, bold=True):
    return ImageFont.truetype(FONT_BOLD if bold else FONT_REG, size)


def wrap(draw, text, fnt, max_w):
    words, lines, cur = text.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if draw.textlength(test, font=fnt) <= max_w:
            cur = test
        else:
            if cur:
                lines.append(cur)
            cur = w
    if cur:
        lines.append(cur)
    return lines


def fit_text(draw, text, max_w, max_lines, start, minimum):
    size = start
    while size > minimum:
        lines = wrap(draw, text, font(size), max_w)
        if len(lines) <= max_lines:
            return font(size), lines
        size -= 4
    fnt = font(minimum)
    lines = wrap(draw, text, fnt, max_w)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        lines[-1] = lines[-1].rstrip(".,") + "…"
    return fnt, lines


# ====== КАРТИНКИ ======
def cover(img, w, h):
    r = max(w / img.width, h / img.height)
    img = img.resize((max(w, int(img.width * r + 0.5)), max(h, int(img.height * r + 0.5))), Image.LANCZOS)
    x, y = (img.width - w) // 2, (img.height - h) // 2
    return img.crop((x, y, x + w, y + h))


def load_image(url):
    if not url:
        return None
    try:
        r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
        r.raise_for_status()
        img = Image.open(BytesIO(r.content)).convert("RGB")
        return img if img.width >= 300 else None
    except Exception as ex:
        print("Не удалось скачать картинку:", ex)
        return None


def placeholder():
    img = Image.new("RGB", (1600, 900), BG)
    d = ImageDraw.Draw(img)
    for i in range(0, 1600, 80):
        d.rectangle((i, 0, i + 40, 900), fill=(30, 32, 80))
    if os.path.exists(LOGO_FILE):
        logo = Image.open(LOGO_FILE).convert("RGBA").resize((600, 600))
        img.paste(logo, (500, 150), logo)
    return img


def rounded_mask(size, radius):
    m = Image.new("L", size, 0)
    ImageDraw.Draw(m).rounded_rectangle((0, 0, size[0] - 1, size[1] - 1), radius, fill=255)
    return m


def make_background(img, title, channel_handle, path):
    """Статичный фон: размытая картинка, шапка, заголовок, подвал."""
    bg = cover(img, W, H).filter(ImageFilter.GaussianBlur(40))
    bg = Image.blend(bg, Image.new("RGB", (W, H), BG), 0.72)
    d = ImageDraw.Draw(bg)

    # шапка: логотип + плашка с названием
    x = 40
    if os.path.exists(LOGO_FILE):
        logo = Image.open(LOGO_FILE).convert("RGBA").resize((110, 110))
        bg.paste(logo, (40, 100), rounded_mask((110, 110), 55))
        x = 170
    f = font(46)
    label = CHANNEL_NAME.upper()
    tw = d.textlength(label, font=f)
    d.rounded_rectangle((x, 118, x + tw + 56, 192), 37, fill=ACCENT)
    d.text((x + 28, 155), label, font=f, fill=BG, anchor="lm")
    date = datetime.now(MSK).strftime("%d.%m.%Y")
    d.text((W - 40, 155), date, font=font(36, False), fill=(200, 205, 255), anchor="rm")

    # рамка под картинку новости
    d.rounded_rectangle((IMG_X - 6, IMG_Y - 6, IMG_X + IMG_W + 5, IMG_Y + IMG_H + 5), 28, fill=ACCENT)

    # заголовок
    fnt, lines = fit_text(d, title, 980, 3, 66, 46)
    y = IMG_Y + IMG_H + 50
    lh = int(fnt.size * 1.18)
    for line in lines:
        d.text((W // 2, y), line, font=fnt, fill=WHITE, anchor="mt")
        y += lh

    # подвал с призывом
    d.text((W // 2, 1440), "Больше новостей — в Telegram", font=font(40, False), fill=(210, 214, 255), anchor="mm")
    d.text((W // 2, 1505), channel_handle or CHANNEL_NAME, font=font(54), fill=ACCENT, anchor="mm")
    bg.save(path)


def make_foreground(img, path):
    """Картинка новости в 2x размере, чтобы плавное приближение было чётким."""
    fg = cover(img, IMG_W * 2, IMG_H * 2)
    fg.save(path)


def make_sub(text, path):
    im = Image.new("RGBA", (W, SUB_H), (0, 0, 0, 0))
    if text:
        d = ImageDraw.Draw(im)
        fnt, lines = fit_text(d, text, 900, 2, 72, 52)
        lh = int(fnt.size * 1.2)
        total = lh * len(lines)
        y0 = (SUB_H - total) // 2
        widest = max(d.textlength(l, font=fnt) for l in lines)
        d.rounded_rectangle(
            (W / 2 - widest / 2 - 30, y0 - 18, W / 2 + widest / 2 + 30, y0 + total + 10),
            26,
            fill=(10, 10, 30, 190),
        )
        for i, line in enumerate(lines):
            d.text((W // 2, y0 + i * lh), line, font=fnt, fill=WHITE, anchor="mt",
                   stroke_width=3, stroke_fill=(0, 0, 0))
    im.save(path)


IMG_X = (W - IMG_W) // 2


# ====== НОВОСТЬ И СЦЕНАРИЙ ======
def gather(state, now):
    used = set(state["used"])
    cutoff = now - timedelta(hours=18)
    items = []
    for name, url in bot.FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as ex:
            print("Feed error", name, ex)
            continue
        for e in feed.entries[:20]:
            link = e.get("link", "")
            eid = e.get("id") or link
            if not eid or eid in used or bot.is_video(link):
                continue
            if bot.entry_time(e) < cutoff:
                continue
            items.append({"id": eid, "source": name, "entry": e, "link": link,
                          "title": bot.clean(e.get("title")), "text": bot.clean(e.get("summary") or "")[:300]})
    items.sort(key=lambda i: bot.entry_time(i["entry"]), reverse=True)
    return items[:25]


def write_script(items):
    listing = "\n".join(f"{i}. [{it['source']}] {it['title']} — {it['text']}" for i, it in enumerate(items))
    prompt = f"""Ты сценарист коротких вертикальных видео (YouTube Shorts, Instagram Reels) для игрового канала «{CHANNEL_NAME}».
Свежие новости:
{listing}

1) Выбери ОДНУ новость, которая больше всего зацепит русскоязычных геймеров (громкий анонс, дата релиза, скандал, неожиданный поворот). Не выбирай скидки, гайды, подборки, рекламу.
2) Напиши закадровый текст на русском своими словами: 45-65 слов, 3-5 коротких предложений, разговорный стиль.
   Первое предложение — цепляющий крючок. Последнее предложение ровно такое: «Больше новостей — в телеграм-канале «{CHANNEL_NAME}».»
   Названия игр и компаний пиши так, как их произносят по-русски (например, «Гта шесть», «Сони», «Нинтендо»), без латиницы, цифры — словами.
   Не выдумывай факты, которых нет в новости.
3) Короткий заголовок для экрана (до 60 символов, можно латиницей для названий игр).
4) Название для YouTube (до 90 символов, в конце #Shorts), описание (1-2 предложения) и 5-7 хэштегов.

Ответь ТОЛЬКО JSON без пояснений:
{{"index": 0, "screen_title": "...", "script": "...", "yt_title": "...", "description": "...", "hashtags": ["#игры", "..."]}}"""
    out = bot.ask_llm(prompt)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise RuntimeError("ИИ вернул не JSON: " + out[:200])
    data = json.loads(m.group(0))
    idx = int(data.get("index", 0))
    if not 0 <= idx < len(items):
        idx = 0
    for k in ("screen_title", "script", "yt_title"):
        if not str(data.get(k, "")).strip():
            raise RuntimeError(f"В ответе ИИ нет поля {k}")
    return items[idx], data


# ====== ОЗВУЧКА ======
def sentences(text):
    parts = re.split(r"(?<=[.!?…])\s+", text.strip())
    return [p.strip() for p in parts if p.strip()]


async def tts_one(text, path):
    import edge_tts

    await edge_tts.Communicate(text, VOICE, rate=VOICE_RATE).save(path)


def run(cmd):
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"Команда не выполнилась: {' '.join(cmd[:3])}...\n{r.stderr[-800:]}")
    return r


def duration(path):
    r = run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "default=nw=1:nk=1", path])
    return float(r.stdout.strip())


def synthesize(script, work):
    """Озвучивает по предложениям, чтобы точно знать, когда показывать субтитры."""
    parts, wavs = [], []
    for i, s in enumerate(sentences(script)):
        mp3 = os.path.join(work, f"s{i}.mp3")
        wav = os.path.join(work, f"s{i}.wav")
        for attempt in range(3):
            try:
                asyncio.run(tts_one(s, mp3))
                break
            except Exception as ex:
                if attempt == 2:
                    raise RuntimeError(f"Озвучка не удалась: {ex}")
        run(["ffmpeg", "-y", "-v", "error", "-i", mp3, "-af", "apad=pad_dur=0.18", "-ar", "44100", "-ac", "1", wav])
        parts.append((s, duration(wav)))
        wavs.append(wav)
    lst = os.path.join(work, "audio.txt")
    with open(lst, "w") as f:
        f.writelines(f"file '{os.path.abspath(w)}'\n" for w in wavs)
    audio = os.path.join(work, "audio.wav")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", audio])
    return audio, parts


def chunks_for(sentence, max_chars=18):
    words, out, cur = sentence.split(), [], ""
    for w in words:
        test = f"{cur} {w}".strip()
        if len(test) <= max_chars or not cur:
            cur = test
        else:
            out.append(cur)
            cur = w
    if cur:
        out.append(cur)
    return out


def build_subs(parts, work):
    """Готовит картинки субтитров и список для ffmpeg (по куску фразы за раз)."""
    blank = os.path.abspath(os.path.join(work, "sub_blank.png"))
    make_sub("", blank)
    lines, n = [], 0
    for sentence, dur in parts:
        speech = max(dur - 0.18, 0.3)
        pieces = chunks_for(sentence)
        total = sum(len(p) for p in pieces) or 1
        for p in pieces:
            png = os.path.join(work, f"sub{n}.png")
            make_sub(p, png)
            lines.append(f"file '{os.path.abspath(png)}'\nduration {speech * len(p) / total:.3f}\n")
            n += 1
        lines.append(f"file '{blank}'\nduration {dur - speech:.3f}\n")
    lines.append(f"file '{blank}'\nduration 1.0\nfile '{blank}'\n")
    lst = os.path.join(work, "subs.txt")
    with open(lst, "w") as f:
        f.writelines(lines)
    return lst

async def tts_one(text, path, rate):
    import edge_tts

    kwargs = {"rate": rate} if rate else {}
    await edge_tts.Communicate(text, VOICE, **kwargs).save(path)


def edge_say(text, path):
    """Озвучка Microsoft (лучшее качество). Пробует со скоростью и без неё."""
    import time

    last = None
    for rate in (VOICE_RATE, None, None):
        try:
            asyncio.run(tts_one(text, path, rate))
            if os.path.exists(path) and os.path.getsize(path) > 1000:
                return
        except Exception as ex:
            last = ex
        time.sleep(2)
    raise RuntimeError(f"edge-tts: {last}")


PIPER_VOICE = os.getenv("PIPER_VOICE", "ru_RU-denis-medium")  # мужской голос, запасной
_piper = None


def piper_say(text, path):
    """Запасная озвучка Piper: работает без интернета после загрузки голоса."""
    global _piper
    import wave

    if _piper is None:
        subprocess.run([sys.executable, "-m", "pip", "install", "-q", "piper-tts"], check=True)
        from piper import PiperVoice

        lang, name, quality = PIPER_VOICE.split("-")
        base = (f"https://huggingface.co/rhasspy/piper-voices/resolve/main/"
                f"{lang.split('_')[0]}/{lang}/{name}/{quality}/{PIPER_VOICE}")
        os.makedirs("piper_voice", exist_ok=True)
        model = os.path.join("piper_voice", PIPER_VOICE + ".onnx")
        for suffix in (".onnx", ".onnx.json"):
            dst = model if suffix == ".onnx" else model + ".json"
            if not os.path.exists(dst):
                r = requests.get(base + suffix, timeout=120)
                r.raise_for_status()
                with open(dst, "wb") as f:
                    f.write(r.content)
        _piper = PiperVoice.load(model)
    with wave.open(path, "wb") as wf:
        if hasattr(_piper, "synthesize_wav"):  # новые версии piper
            _piper.synthesize_wav(text, wf)
        else:  # старые версии
            _piper.synthesize(text, wf)


def speak_all(script, work, engine):
    parts, wavs = [], []
    for i, s in enumerate(sentences(script)):
        raw = os.path.join(work, f"s{i}.{'mp3' if engine == 'edge' else 'raw.wav'}")
        wav = os.path.join(work, f"s{i}.wav")
        clean_s = s.replace("«", "").replace("»", "").replace("—", ",")
        (edge_say if engine == "edge" else piper_say)(clean_s, raw)
        run(["ffmpeg", "-y", "-v", "error", "-i", raw, "-af", "apad=pad_dur=0.18", "-ar", "44100", "-ac", "1", wav])
        parts.append((s, duration(wav)))
        wavs.append(wav)
    return parts, wavs


def synthesize(script, work):
    """Озвучивает по предложениям, чтобы точно знать, когда показывать субтитры."""
    try:
        parts, wavs = speak_all(script, work, "edge")
    except Exception as ex:
        print("Голос Microsoft не сработал, включаю запасной голос Piper:", ex)
        try:
            parts, wavs = speak_all(script, work, "piper")
        except Exception as ex2:
            raise RuntimeError(f"Озвучка не удалась. Microsoft: {ex}. Piper: {ex2}")
    lst = os.path.join(work, "audio.txt")
    with open(lst, "w") as f:
        f.writelines(f"file '{os.path.abspath(w)}'\n" for w in wavs)
    audio = os.path.join(work, "audio.wav")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", audio])
    return audio, parts

GEMINI_VOICE = os.getenv("GEMINI_VOICE", "Charon")  # мужские голоса: Charon, Fenrir, Orus, Puck
_gemini_tts = None


def gemini_tts_model():
    """Находит модель Gemini, которая умеет озвучивать текст."""
    global _gemini_tts
    if _gemini_tts:
        return _gemini_tts
    if os.getenv("GEMINI_TTS_MODEL"):
        _gemini_tts = os.environ["GEMINI_TTS_MODEL"]
        return _gemini_tts
    r = requests.get("https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
                     headers={"x-goog-api-key": bot.GEMINI_KEY}, timeout=30)
    r.raise_for_status()
    names = [m["name"].replace("models/", "") for m in r.json().get("models", [])
             if "tts" in m["name"] and "generateContent" in m.get("supportedGenerationMethods", [])]
    print("Модели озвучки Gemini:", ", ".join(names) or "нет")
    if not names:
        raise RuntimeError("для вашего ключа нет моделей озвучки Gemini")
    flash = [n for n in names if "flash" in n] or names
    _gemini_tts = sorted(flash)[-1]
    print("Голос Gemini через модель:", _gemini_tts)
    return _gemini_tts


def gemini_say(text, path):
    """Озвучка Gemini: живой голос, весь текст одним запросом (бережёт лимиты)."""
    import base64
    import wave

    prompt = text  # только сам текст: иначе модель иногда зачитывает и инструкцию
    r = requests.post(
        f"https://generativelanguage.googleapis.com/v1beta/models/{gemini_tts_model()}:generateContent",
        headers={"x-goog-api-key": bot.GEMINI_KEY},
        json={
            "contents": [{"parts": [{"text": prompt}]}],
            "generationConfig": {
                "responseModalities": ["AUDIO"],
                "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE}}},
            },
        },
        timeout=180,
    )
    if not r.ok:
        raise RuntimeError(f"Gemini TTS {r.status_code}: {r.text[:300]}")
    data = None
    for part in r.json()["candidates"][0]["content"]["parts"]:
        if "inlineData" in part:
            data = base64.b64decode(part["inlineData"]["data"])
            break
    if not data:
        raise RuntimeError("Gemini не вернул звук")
    with wave.open(path, "wb") as wf:  # Gemini отдаёт 16-бит PCM, 24 кГц, моно
        wf.setnchannels(1)
        wf.setsampwidth(2)
        wf.setframerate(24000)
        wf.writeframes(data)


def synthesize(script, work):
    """Сначала живой голос Gemini, если не вышло — Microsoft, в крайнем случае Piper."""
    if bot.GEMINI_KEY and os.getenv("VIDEO_TTS", "gemini") == "gemini":
        try:
            raw = os.path.join(work, "gemini_raw.wav")
            gemini_say(script.replace("«", "").replace("»", ""), raw)
            audio = os.path.join(work, "audio.wav")
            run(["ffmpeg", "-y", "-v", "error", "-i", raw,
                 "-af", "silenceremove=start_periods=1:start_threshold=-50dB,apad=pad_dur=0.3",
                 "-ar", "44100", "-ac", "1", audio])
            total = duration(audio)
            sents = sentences(script)
            chars = sum(len(s) for s in sents) or 1
            parts = [(s, total * len(s) / chars) for s in sents]  # субтитры по длине фраз
            print("Озвучено голосом Gemini")
            return audio, parts
        except Exception as ex:
            print("Голос Gemini не сработал, пробую Microsoft:", ex)
    try:
        parts, wavs = speak_all(script, work, "edge")
        print("Озвучено голосом Microsoft")
    except Exception as ex:
        print("Голос Microsoft не сработал, включаю запасной голос Piper:", ex)
        try:
            parts, wavs = speak_all(script, work, "piper")
            print("Озвучено запасным голосом Piper")
        except Exception as ex2:
            raise RuntimeError(f"Озвучка не удалась. Microsoft: {ex}. Piper: {ex2}")
    lst = os.path.join(work, "audio.txt")
    with open(lst, "w") as f:
        f.writelines(f"file '{os.path.abspath(w)}'\n" for w in wavs)
    audio = os.path.join(work, "audio.wav")
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", audio])
    return audio, parts
    
# ====== СБОРКА ======
def ensure_ffmpeg():
    if shutil.which("ffmpeg") and shutil.which("ffprobe"):
        return
    subprocess.run(["sudo", "apt-get", "update", "-q"], check=False)
    subprocess.run(["sudo", "apt-get", "install", "-y", "-q", "ffmpeg"], check=False)


def render(bg, fg, subs, audio, total, out):
    frames = int(total * FPS) + 1
    zoom = f"min(1+0.10*on/{frames},1.10)"
    graph = (
        f"[1:v]zoompan=z='{zoom}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)'"
        f":d={frames}:s={IMG_W}x{IMG_H}:fps={FPS}[fg];"
        f"[0:v][fg]overlay={IMG_X}:{IMG_Y}[v1];"
        f"[2:v]format=rgba[sub];"
        f"[v1][sub]overlay=0:{SUB_Y}:eof_action=pass,format=yuv420p[v]"
    )
    run([
        "ffmpeg", "-y", "-v", "error",
        "-loop", "1", "-framerate", str(FPS), "-t", f"{total:.2f}", "-i", bg,
        "-i", fg,
        "-f", "concat", "-safe", "0", "-i", subs,
        "-i", audio,
        "-filter_complex", graph,
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k",
        "-t", f"{total:.2f}", "-movflags", "+faststart", out,
    ])


# ====== ОТПРАВКА ======
def send_to_owner(video, data):
    if not OWNER_ID:
        print("Не задан TELEGRAM_OWNER_ID, ролик некому отправить")
        return False
    base = f"https://api.telegram.org/bot{bot.TG_TOKEN}"
    tags = " ".join(data.get("hashtags", [])[:7])
    with open(video, "rb") as f:
        r = requests.post(f"{base}/sendVideo", timeout=120, data={
            "chat_id": OWNER_ID, "supports_streaming": "true", "width": W, "height": H,
            "caption": f"🎬 Ролик готов: {data['screen_title']}",
        }, files={"video": ("short.mp4", f, "video/mp4")})
    if not r.ok:
        print("Telegram error:", r.status_code, r.text)
        return False
    desc = data.get("description", "").strip()
    link = "https://t.me/" + str(bot.TG_CHAT).lstrip("@")
    text = (
        f"YouTube — название:\n{data['yt_title']}\n\n"
        f"YouTube — описание:\n{desc}\nБольше новостей — в Telegram: {link}\n{tags}\n\n"
        f"Instagram — подпись:\n{desc}\nБольше новостей — в Telegram: {bot.TG_CHAT} (ссылка в профиле)\n{tags}"
    )
    requests.post(f"{base}/sendMessage", timeout=30, data={"chat_id": OWNER_ID, "text": text})
    return True


def notify(text):
    if OWNER_ID:
        requests.post(f"https://api.telegram.org/bot{bot.TG_TOKEN}/sendMessage", timeout=30,
                      data={"chat_id": OWNER_ID, "text": text})


def main():
    global FONT_BOLD, FONT_REG
    state = load_state()
    now = datetime.now(timezone.utc)
    slot = "вручную " + now.astimezone(MSK).strftime("%Y-%m-%d %H:%M") if FORCE else due_slot(state, now)
    if not slot:
        print("Ролик сейчас не нужен")
        save_state(state)
        return

    items = gather(state, now)
    if not items:
        print("Нет свежих новостей для ролика")
        state["done"].append(slot)
        save_state(state)
        return

    # одна попытка на слот: при ошибке не повторяем каждые 15 минут
    state["done"].append(slot)
    save_state(state)

    ensure_ffmpeg()
    FONT_BOLD, FONT_REG = find_font(True), find_font(False)
    item, data = write_script(items)
    print("Новость для ролика:", item["title"])

    img = load_image(bot.find_image(item["entry"], item["link"])) or placeholder()
    channel = bot.TG_CHAT if str(bot.TG_CHAT).startswith("@") else ""

    with tempfile.TemporaryDirectory() as work:
        bg, fg = os.path.join(work, "bg.png"), os.path.join(work, "fg.png")
        make_background(img, data["screen_title"], channel, bg)
        make_foreground(img, fg)
        audio, parts = synthesize(data["script"], work)
        subs = build_subs(parts, work)
        total = duration(audio) + 1.0
        out = os.path.join(work, "short.mp4")
        render(bg, fg, subs, audio, total, out)
        print(f"Ролик собран: {total:.1f} с, {os.path.getsize(out) / 1e6:.1f} МБ")
        sent = send_to_owner(out, data)

    state["used"].append(item["id"])
    save_state(state)
    if not sent:
        raise RuntimeError("ролик собран, но не отправлен в Telegram (нажмите Start у бота канала)")

# ====== ВИДЕО ИГРЫ (дополнение) ======
USE_STEAM_VIDEO = os.getenv("VIDEO_STEAM", "1") == "1"  # брать видео игры из Steam


STEAM_HEADERS = {"User-Agent": "Mozilla/5.0", "Accept-Language": "en"}


def steam_find(game):
    """Ищет игру в Steam и проверяет, что название действительно совпадает."""
    import difflib

    if not game:
        return None
    r = requests.get("https://store.steampowered.com/api/storesearch/",
                     params={"term": game, "l": "english", "cc": "US"}, headers=STEAM_HEADERS, timeout=20)
    r.raise_for_status()
    best, score = None, 0.0
    norm = lambda t: re.sub(r"[^a-z0-9 ]", "", t.lower())
    for it in r.json().get("items", [])[:10]:
        sc = difflib.SequenceMatcher(None, norm(game), norm(it.get("name", ""))).ratio()
        if norm(game) and norm(game) in norm(it.get("name", "")):
            sc = max(sc, 0.8)
        if sc > score:
            best, score = it, sc
    if best and score >= 0.7:
        print(f"Steam: {best['name']} (совпадение {score:.2f})")
        return best["id"]
    print("Steam: игра не найдена для", game)
    return None


def steam_media(appid):
    r = requests.get("https://store.steampowered.com/api/appdetails",
                     params={"appids": appid, "cc": "US", "l": "english"}, headers=STEAM_HEADERS, timeout=20)
    r.raise_for_status()
    data = (r.json().get(str(appid)) or {}).get("data") or {}
    trailers = []
    for m in data.get("movies", []):
        url = m.get("hls_h264") or (m.get("mp4") or {}).get("max") or (m.get("mp4") or {}).get("480")
        if url:
            trailers.append(url)
    shots = [s.get("path_full") for s in data.get("screenshots", []) if s.get("path_full")]
    return trailers, shots


def clip_from_trailer(url, need, out):
    """Берёт кусок трейлера без звука, пропуская логотипы в начале."""
    for start in (8, 0):
        cmd = ["ffmpeg", "-y", "-v", "error", "-ss", str(start), "-i", url, "-t", f"{need:.1f}",
               "-map", "0:v:0", "-vf", "scale=1280:720:force_original_aspect_ratio=increase,crop=1280:720,fps=30,setsar=1",
               "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-an", out]
        try:
            run(cmd)
            if duration(out) >= min(need, 8):
                return True
        except Exception as ex:
            print("Трейлер не скачался:", str(ex)[:200])
    return False


def clip_from_screens(shots, need, work, out):
    """Слайд-шоу из скриншотов игры с плавным приближением."""
    shots = shots[:6]
    if len(shots) < 2:
        return False
    per = max(need / len(shots), 3.0)
    frames = int(per * FPS)
    parts = []
    for i, url in enumerate(shots):
        img = load_image(url)
        if not img:
            continue
        src = os.path.join(work, f"shot{i}.jpg")
        cover(img, 2560, 1440).save(src, quality=92)
        seg = os.path.join(work, f"shot{i}.mp4")
        z = f"min(1+0.12*on/{frames},1.12)" if i % 2 == 0 else f"max(1.12-0.12*on/{frames},1)"
        run(["ffmpeg", "-y", "-v", "error", "-i", src, "-vf",
             f"zoompan=z='{z}':x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':d={frames}:s=1280x720:fps={FPS},setsar=1",
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", seg])
        parts.append(seg)
    if len(parts) < 2:
        return False
    lst = os.path.join(work, "shots.txt")
    with open(lst, "w") as f:
        f.writelines(f"file '{os.path.abspath(p)}'\n" for p in parts)
    run(["ffmpeg", "-y", "-v", "error", "-f", "concat", "-safe", "0", "-i", lst, "-c", "copy", out])
    return True


def get_game_clip(game, need, work):
    """Возвращает (путь к видео, False) или (None, False), если видео игры не нашлось."""
    if not USE_STEAM_VIDEO or not game:
        return None, False
    try:
        appid = steam_find(game)
        if not appid:
            return None, False
        trailers, shots = steam_media(appid)
        out = os.path.join(work, "clip.mp4")
        for url in trailers[:2]:
            if clip_from_trailer(url, need, out):
                print("Фон: трейлер из Steam")
                return out, False
        if clip_from_screens(shots, need, work, out):
            print("Фон: скриншоты из Steam")
            return out, False
    except Exception as ex:
        print("Steam недоступен:", str(ex)[:200])
    return None, False


def make_overlay(title, channel_handle, path):
    """Прозрачный слой поверх видео: шапка, рамка, заголовок, подвал."""
    ov = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(ov)
    x = 40
    if os.path.exists(LOGO_FILE):
        logo = Image.open(LOGO_FILE).convert("RGBA").resize((110, 110))
        ov.paste(logo, (40, 100), rounded_mask((110, 110), 55))
        x = 170
    f = font(46)
    label = CHANNEL_NAME.upper()
    tw = d.textlength(label, font=f)
    d.rounded_rectangle((x, 118, x + tw + 56, 192), 37, fill=ACCENT)
    d.text((x + 28, 155), label, font=f, fill=BG, anchor="lm")
    date = datetime.now(MSK).strftime("%d.%m.%Y")
    d.text((W - 40, 155), date, font=font(36, False), fill=(200, 205, 255), anchor="rm")
    d.rounded_rectangle((IMG_X - 6, IMG_Y - 6, IMG_X + IMG_W + 5, IMG_Y + IMG_H + 5), 12, outline=ACCENT, width=8)
    fnt, lines = fit_text(d, title, 980, 3, 66, 46)
    y = IMG_Y + IMG_H + 50
    lh = int(fnt.size * 1.18)
    for line in lines:
        d.text((W // 2, y), line, font=fnt, fill=WHITE, anchor="mt", stroke_width=3, stroke_fill=(10, 10, 30))
        y += lh
    d.text((W // 2, 1440), "Больше новостей — в Telegram", font=font(40, False), fill=(225, 228, 255), anchor="mm",
           stroke_width=2, stroke_fill=(10, 10, 30))
    d.text((W // 2, 1505), channel_handle or CHANNEL_NAME, font=font(54), fill=ACCENT, anchor="mm",
           stroke_width=3, stroke_fill=(10, 10, 30))
    ov.save(path)


def render_clip(clip, overlay, subs, audio, total, out):
    """Видео игры: размытое на весь экран + чёткое в рамке, сверху надписи и субтитры."""
    graph = (
        "[0:v]split=2[a][b];"
        f"[a]scale={W}:{H}:force_original_aspect_ratio=increase,crop={W}:{H},boxblur=25:2,"
        f"drawbox=x=0:y=0:w={W}:h={H}:color=0x181940@0.62:t=fill[bgv];"
        f"[b]scale={IMG_W}:{IMG_H}:force_original_aspect_ratio=increase,crop={IMG_W}:{IMG_H},setsar=1[fgv];"
        f"[bgv][fgv]overlay={IMG_X}:{IMG_Y}[v1];"
        "[v1][1:v]overlay=0:0[v2];"
        "[2:v]format=rgba[sub];"
        f"[v2][sub]overlay=0:{SUB_Y}:eof_action=pass,fps={FPS},format=yuv420p[v]"
    )
    run([
        "ffmpeg", "-y", "-v", "error",
        "-stream_loop", "-1", "-i", clip,
        "-loop", "1", "-framerate", str(FPS), "-i", overlay,
        "-f", "concat", "-safe", "0", "-i", subs,
        "-i", audio,
        "-filter_complex", graph,
        "-map", "[v]", "-map", "3:a",
        "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "160k",
        "-t", f"{total:.2f}", "-movflags", "+faststart", out,
    ])


def write_script(items):
    listing = "\n".join(f"{i}. [{it['source']}] {it['title']} — {it['text']}" for i, it in enumerate(items))
    prompt = f"""Ты сценарист коротких вертикальных видео (YouTube Shorts, Instagram Reels) для игрового канала «{CHANNEL_NAME}».
Свежие новости:
{listing}

1) Выбери ОДНУ новость, которая больше всего зацепит русскоязычных геймеров (громкий анонс, дата релиза, скандал, неожиданный поворот). Не выбирай скидки, гайды, подборки, рекламу.
2) Напиши закадровый текст на русском своими словами: 45-65 слов, 3-5 коротких предложений, разговорный стиль.
   Первое предложение — цепляющий крючок. Последнее предложение ровно такое: «Больше новостей — в телеграм-канале «{CHANNEL_NAME}».»
   Названия игр и компаний пиши так, как их произносят по-русски (например, «Гта шесть», «Сони», «Нинтендо»), без латиницы, цифры — словами.
   Не выдумывай факты, которых нет в новости.
3) Короткий заголовок для экрана (до 60 символов, можно латиницей для названий игр).
4) Название для YouTube (до 90 символов, в конце #Shorts), описание (1-2 предложения) и 5-7 хэштегов.
5) Официальное английское название ОДНОЙ игры, о которой новость (как в Steam). Если новость не про конкретную игру — пустая строка.

Ответь ТОЛЬКО JSON без пояснений:
{{"index": 0, "screen_title": "...", "script": "...", "yt_title": "...", "description": "...", "hashtags": ["#игры", "..."], "game_en": "..."}}"""
    out = bot.ask_llm(prompt)
    m = re.search(r"\{.*\}", out, re.S)
    if not m:
        raise RuntimeError("ИИ вернул не JSON: " + out[:200])
    data = json.loads(m.group(0))
    idx = int(data.get("index", 0))
    if not 0 <= idx < len(items):
        idx = 0
    for k in ("screen_title", "script", "yt_title"):
        if not str(data.get(k, "")).strip():
            raise RuntimeError(f"В ответе ИИ нет поля {k}")
    return items[idx], data


def main():
    global FONT_BOLD, FONT_REG
    state = load_state()
    now = datetime.now(timezone.utc)
    slot = "вручную " + now.astimezone(MSK).strftime("%Y-%m-%d %H:%M") if FORCE else due_slot(state, now)
    if not slot:
        print("Ролик сейчас не нужен")
        save_state(state)
        return

    items = gather(state, now)
    if not items:
        print("Нет свежих новостей для ролика")
        state["done"].append(slot)
        save_state(state)
        return

    # одна попытка на слот: при ошибке не повторяем каждые 15 минут
    state["done"].append(slot)
    save_state(state)

    ensure_ffmpeg()
    FONT_BOLD, FONT_REG = find_font(True), find_font(False)
    item, data = write_script(items)
    print("Новость для ролика:", item["title"])

    img = load_image(bot.find_image(item["entry"], item["link"])) or placeholder()
    channel = bot.TG_CHAT if str(bot.TG_CHAT).startswith("@") else ""

    with tempfile.TemporaryDirectory() as work:
        bg, fg = os.path.join(work, "bg.png"), os.path.join(work, "fg.png")
        make_background(img, data["screen_title"], channel, bg)
        make_foreground(img, fg)
        audio, parts = synthesize(data["script"], work)
        subs = build_subs(parts, work)
        total = duration(audio) + 1.0
        out = os.path.join(work, "short.mp4")
        clip, _ = get_game_clip(str(data.get("game_en", "")).strip(), total, work)
        if clip:
            overlay = os.path.join(work, "overlay.png")
            make_overlay(data["screen_title"], channel, overlay)
            render_clip(clip, overlay, subs, audio, total, out)
        else:
            print("Фон: картинка новости")
            render(bg, fg, subs, audio, total, out)
        print(f"Ролик собран: {total:.1f} с, {os.path.getsize(out) / 1e6:.1f} МБ")
        sent = send_to_owner(out, data)

    state["used"].append(item["id"])
    save_state(state)
    if not sent:
        raise RuntimeError("ролик собран, но не отправлен в Telegram (нажмите Start у бота канала)")

if __name__ == "__main__":
    try:
        main()
    except Exception as ex:
        print("Ошибка при создании ролика:", ex)
        notify(f"⚠️ Не получилось сделать ролик: {str(ex)[:300]}")
        sys.exit(1)
