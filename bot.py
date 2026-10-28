import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

import feedparser
import requests

# ====== НАСТРОЙКИ ======
# (название источника, адрес RSS). Добавляйте и убирайте строки.
FEEDS = [
    ("IGN", "https://feeds.feedburner.com/ign/all"),
    ("PC Gamer", "https://www.pcgamer.com/rss/"),
    ("Eurogamer", "https://www.eurogamer.net/feed"),
    ("Rock Paper Shotgun", "https://www.rockpapershotgun.com/feed"),
    ("Gematsu", "https://www.gematsu.com/feed"),
    # Трейлеры с YouTube: подставьте ID канала (UC...) и уберите #
    # ("IGN Video", "https://www.youtube.com/feeds/videos.xml?channel_id=ID_КАНАЛА"),
]

MAX_POSTS_PER_RUN = int(os.getenv("MAX_POSTS_PER_RUN", "3"))
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "24"))
STATE_FILE = "state.json"

# Если задан GEMINI_API_KEY, используется бесплатный Gemini, иначе Claude.
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")

TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHANNEL"]  # @username канала или -100...

feedparser.USER_AGENT = "Mozilla/5.0 (compatible; GameNewsBot/1.0)"


# ====== СОСТОЯНИЕ ======
def load_state():
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"ids": [], "titles": []}


def save_state(state):
    state["ids"] = state["ids"][-3000:]
    state["titles"] = state["titles"][-20:]
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False, indent=1)


# ====== ВСПОМОГАТЕЛЬНОЕ ======
def clean(text):
    text = re.sub(r"<[^>]+>", " ", text or "")
    return re.sub(r"\s+", " ", html.unescape(text)).strip()


def entry_time(e):
    t = e.get("published_parsed") or e.get("updated_parsed")
    if t:
        return datetime(*t[:6], tzinfo=timezone.utc)
    return datetime.now(timezone.utc)


def is_video(link):
    return "youtube.com" in link or "youtu.be" in link


def find_image(e, link):
    for key in ("media_thumbnail", "media_content"):
        items = e.get(key)
        if items and items[0].get("url"):
            return items[0]["url"]
    for enc in e.get("enclosures", []):
        if str(enc.get("type", "")).startswith("image"):
            return enc.get("href")
    blob = (e.get("summary") or "") + " ".join(
        c.get("value", "") for c in e.get("content", [])
    )
    m = re.search(r'<img[^>]+src="([^"]+)"', blob)
    if m:
        return m.group(1)
    try:
        page = requests.get(
            link, timeout=10, headers={"User-Agent": "Mozilla/5.0"}
        ).text
        m = re.search(
            r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)', page
        )
        if m:
            return html.unescape(m.group(1))
    except Exception:
        pass
    return None


# ====== ПЕРЕПИСЫВАНИЕ ЧЕРЕЗ ИИ ======
def rewrite(title, text, source, video, recent_titles):
    recent = "\n".join(f"- {t}" for t in recent_titles) or "—"
    prompt = f"""Ты редактор русскоязычного Telegram-канала про игровую индустрию.
Источник: {source}. Тип материала: {"видео (трейлер)" if video else "новость"}.
Заголовок: {title}
Текст: {text[:1500]}

Недавно уже опубликованные заголовки:
{recent}

Задача: напиши пост на русском СВОИМИ СЛОВАМИ, не дословным переводом.
Формат: первая строка <b>цепкий заголовок</b>, затем пустая строка, затем {"1 предложение" if video else "2-3 предложения"} сути. Весь текст до 450 символов.
Разрешён только тег <b>. Без markdown, без хэштегов. Названия игр и компаний оставляй на латинице.
Если это не игровая новость (скидки, промокоды, подборки «лучшие X», гайды, реклама) или то же событие уже публиковалось выше, ответь одним словом SKIP.
Отвечай только текстом поста."""
    out = ask_llm(prompt).strip()
    return None if out.upper().startswith("SKIP") else out


_gemini_model = None


def pick_gemini_model():
    """Спрашивает у Google список моделей и выбирает дешёвую текстовую."""
    global _gemini_model
    if _gemini_model:
        return _gemini_model
    if os.getenv("GEMINI_MODEL"):
        _gemini_model = os.environ["GEMINI_MODEL"]
        return _gemini_model
    r = requests.get(
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
        headers={"x-goog-api-key": GEMINI_KEY},
        timeout=30,
    )
    r.raise_for_status()
    bad = ("image", "tts", "live", "audio", "embedding", "robotics", "veo", "imagen", "native")
    names = []
    for m in r.json().get("models", []):
        n = m["name"].replace("models/", "")
        if "generateContent" not in m.get("supportedGenerationMethods", []):
            continue
        if n.startswith("gemini") and not any(b in n for b in bad):
            names.append(n)
    print("Доступные модели Gemini:", ", ".join(names))
    for key in ("flash-lite", "flash"):
        stable = [n for n in names if key in n and "preview" not in n and "exp" not in n]
        anyv = [n for n in names if key in n]
        for group in (stable, anyv):
            if group:
                _gemini_model = sorted(group)[-1]
                print("Выбрана модель:", _gemini_model)
                return _gemini_model
    raise RuntimeError("Не нашёл подходящую модель Gemini")


def ask_llm(prompt):
    if GEMINI_KEY:
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{pick_gemini_model()}:generateContent",
            headers={"x-goog-api-key": GEMINI_KEY},
            json={"contents": [{"parts": [{"text": prompt}]}]},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    import anthropic  # нужен только если используется Claude

    msg = anthropic.Anthropic().messages.create(
        model=CLAUDE_MODEL,
        max_tokens=500,
        messages=[{"role": "user", "content": prompt}],
    )
    return msg.content[0].text


# ====== ОТПРАВКА В TELEGRAM ======
def tg(method, **data):
    return requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}",
        data={"chat_id": TG_CHAT, **data},
        timeout=30,
    )


def send_post(text, image):
    if image and len(text) <= 1024:
        r = tg("sendPhoto", photo=image, caption=text, parse_mode="HTML")
        if r.ok:
            return True
    r = tg("sendMessage", text=text, parse_mode="HTML")
    if r.ok:
        return True
    plain = re.sub(r"<[^>]+>", "", text)  # запасной вариант без разметки
    r = tg("sendMessage", text=plain)
    if not r.ok:
        print("Telegram error:", r.status_code, r.text)
    return r.ok


# ====== ОСНОВНОЙ ПРОЦЕСС ======
def main():
    state = load_state()
    seen = set(state["ids"])
    cutoff = datetime.now(timezone.utc) - timedelta(hours=MAX_AGE_HOURS)

    candidates = []
    for name, url in FEEDS:
        try:
            feed = feedparser.parse(url)
        except Exception as ex:
            print("Feed error", name, ex)
            continue
        for e in feed.entries[:30]:
            eid = e.get("id") or e.get("link")
            if not eid or eid in seen:
                continue
            t = entry_time(e)
            if t < cutoff:
                state["ids"].append(eid)  # старьё просто помечаем прочитанным
                seen.add(eid)
                continue
            candidates.append((t, name, e, eid))

    candidates.sort(key=lambda c: c[0], reverse=True)
    posted = 0
    for t, name, e, eid in candidates:
        if posted >= MAX_POSTS_PER_RUN:
            state["ids"].append(eid)  # лишнее не копим, чтобы не было потока
            continue
        link = e.get("link", "")
        title = clean(e.get("title"))
        text = clean(e.get("summary") or "")
        video = is_video(link)
        try:
            post = rewrite(title, text, name, video, state["titles"])
        except Exception as ex:
            print("LLM error:", ex)
            continue  # не помечаем: попробуем в следующий запуск
        state["ids"].append(eid)
        if not post:
            continue
        host = urlparse(link).netloc.replace("www.", "")
        label = "Смотреть" if video else f"Источник: {host}"
        post += f'\n\n<a href="{html.escape(link, quote=True)}">{label}</a>'
        image = None if video else find_image(e, link)
        if send_post(post, image):
            posted += 1
            first_line = re.sub(r"<[^>]+>", "", post.split("\n")[0])
            state["titles"].append(first_line)
            time.sleep(2)

    save_state(state)
    print(f"Опубликовано: {posted}, кандидатов: {len(candidates)}")


if __name__ == "__main__":
    main()
