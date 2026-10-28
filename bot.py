import html
import json
import os
import re
import time
from datetime import datetime, timedelta, timezone

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
    ("Push Square", "https://www.pushsquare.com/feeds/latest"),
    ("Nintendo Life", "https://www.nintendolife.com/feeds/latest"),
    ("Xbox Wire", "https://news.xbox.com/en-us/feed/"),
    # Трейлеры с YouTube: подставьте ID канала (UC...) и уберите #
    # ("IGN Video", "https://www.youtube.com/feeds/videos.xml?channel_id=ID_КАНАЛА"),
]

POST_INTERVAL_MIN = int(os.getenv("POST_INTERVAL_MIN", "45"))  # пауза между постами
MAX_QUEUE = int(os.getenv("MAX_QUEUE", "3"))                   # сколько постов держим в очереди
MAX_LLM_CALLS = int(os.getenv("MAX_LLM_CALLS", "8"))           # лимит обращений к ИИ за запуск
MAX_AGE_HOURS = int(os.getenv("MAX_AGE_HOURS", "24"))          # новости старше не берём
QUEUE_MAX_AGE_HOURS = int(os.getenv("QUEUE_MAX_AGE_HOURS", "12"))  # пост в очереди старше не публикуем
STATE_FILE = "state.json"
SIGNATURE = os.getenv("POST_SIGNATURE", "🎮 Первый игровой")  # подпись в конце каждого поста

# Еженедельный дайджест «Итоги недели»
DIGEST_WEEKDAY = int(os.getenv("DIGEST_WEEKDAY", "6"))  # 0=понедельник ... 6=воскресенье
DIGEST_HOUR = int(os.getenv("DIGEST_HOUR", "18"))       # час по Москве, раньше не публикуем
DIGEST_MIN_POSTS = int(os.getenv("DIGEST_MIN_POSTS", "5"))  # меньше постов за неделю: дайджест пропускаем
MSK = timezone(timedelta(hours=3))

# Если задан GEMINI_API_KEY, используется бесплатный Gemini, иначе Claude.
GEMINI_KEY = os.getenv("GEMINI_API_KEY")
CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-5-5")

TG_TOKEN = os.environ["TELEGRAM_TOKEN"]
TG_CHAT = os.environ["TELEGRAM_CHANNEL"]  # @username канала или -100...

feedparser.USER_AGENT = "Mozilla/5.0 (compatible; GameNewsBot/1.0)"


# ====== СОСТОЯНИЕ ======
def load_state():
    state = {}
    if os.path.exists(STATE_FILE):
        with open(STATE_FILE, encoding="utf-8") as f:
            state = json.load(f)
    state.setdefault("ids", [])
    state.setdefault("titles", [])
    state.setdefault("queue", [])
    state.setdefault("last_post", None)
    state.setdefault("published", [])
    state.setdefault("last_digest", None)
    return state


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

def meta_image(page):
    """Достаёт большую обложку статьи (og:image / twitter:image) из кода страницы."""
    for prop in ("og:image", "twitter:image"):
        key = re.escape(prop)
        m = re.search(r'<meta[^>]+(?:property|name)=["\']' + key + r'["\'][^>]*content=["\']([^"\']+)', page, re.I)
        if not m:
            m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]*(?:property|name)=["\']' + key + r'["\']', page, re.I)
        if m:
            return html.unescape(m.group(1))
    return None

def find_image(e, link):
    """Ищет самую крупную картинку: сначала обложка статьи, потом картинки из ленты."""
    from urllib.parse import urljoin

    # 1. Обложка статьи: сайты делают её большой (для соцсетей)
    try:
        page = requests.get(link, timeout=10, headers={"User-Agent": "Mozilla/5.0"}).text
        img = meta_image(page)
        if img:
            img = urljoin(link, img)
            if img.startswith("http"):
                return img
    except Exception:
        pass
    # 2. Картинки из ленты: берём самую широкую
    best, best_w = None, -1
    for key in ("media_content", "media_thumbnail"):
        for item in e.get(key, []) or []:
            url = item.get("url")
            if not url:
                continue
            try:
                w = int(item.get("width") or 0)
            except ValueError:
                w = 0
            if w > best_w:
                best, best_w = url, w
    if best:
        return best
    for enc in e.get("enclosures", []):
        if str(enc.get("type", "")).startswith("image"):
            return enc.get("href")
    blob = (e.get("summary") or "") + " ".join(c.get("value", "") for c in e.get("content", []))
    m = re.search(r'<img[^>]+src="([^"]+)"', blob)
    return m.group(1) if m else None
    
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
Разрешён только тег <b>. Без markdown. Названия игр и компаний оставляй на латинице.
В конце, после пустой строки, добавь одну строку с 1-2 хэштегами только из списка: #новости #трейлер #релиз #анонс #индустрия #PlayStation #Xbox #Nintendo (для видео обязательно #трейлер). Хэштег платформы (#PlayStation, #Xbox, #Nintendo) ставь только если новость именно про эту платформу; если игра выходит сразу на нескольких платформах или платформа не главная в новости, платформенный хэштег не ставь.
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


# ====== ОЧЕРЕДЬ ======
def collect_candidates(state, seen, cutoff):
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
    return candidates


def fill_queue(state, now):
    """Готовит посты (перевод и переписывание) и кладёт их в очередь."""
    if len(state["queue"]) >= MAX_QUEUE:
        return
    seen = set(state["ids"])
    candidates = collect_candidates(state, seen, now - timedelta(hours=MAX_AGE_HOURS))
    calls = 0
    for t, name, e, eid in candidates:
        if len(state["queue"]) >= MAX_QUEUE or calls >= MAX_LLM_CALLS:
            break
        link = e.get("link", "")
        video = is_video(link)
        recent = state["titles"]
        try:
            calls += 1
            post = rewrite(clean(e.get("title")), clean(e.get("summary") or ""), name, video, recent)
        except Exception as ex:
            print("LLM error:", ex)
            break  # скорее всего лимит или сбой: попробуем в следующий запуск
        time.sleep(4)  # бесплатный тариф ограничивает запросы в минуту
        state["ids"].append(eid)
        if not post:
            continue
        if video:
            post += f'\n\n<a href="{html.escape(link, quote=True)}">Смотреть трейлер</a>'
        post += f"\n\n{SIGNATURE}"
        image = None if video else find_image(e, link)
        state["queue"].append({"post": post, "image": image, "ts": now.isoformat(), "tries": 0})
        state["titles"].append(re.sub(r"<[^>]+>", "", post.split("\n")[0]))


def post_parts(post):
    """Достаёт из готового поста заголовок и краткое описание без хэштегов, ссылок и подписи."""
    lines = [re.sub(r"<[^>]+>", "", l).strip() for l in post.split("\n")]
    lines = [l for l in lines if l and not l.startswith("#") and l != SIGNATURE and l != "Смотреть трейлер"]
    if not lines:
        return "", ""
    return lines[0], " ".join(lines[1:])[:220]


def maybe_queue_digest(state, now):
    """В воскресенье вечером готовит пост «Итоги недели» и ставит его первым в очередь."""
    local = now.astimezone(MSK)
    if local.weekday() != DIGEST_WEEKDAY or local.hour < DIGEST_HOUR:
        return
    iso = local.isocalendar()
    week_id = f"{iso[0]}-W{iso[1]}"
    if state.get("last_digest") == week_id:
        return
    week_ago = now - timedelta(days=7)
    posts = [p for p in state["published"] if datetime.fromisoformat(p["ts"]) >= week_ago]
    if len(posts) < DIGEST_MIN_POSTS:
        state["last_digest"] = week_id
        return
    lines = "\n".join(f"- {p['title']}: {p['body']}" for p in posts[-60:])
    prompt = f"""Ты редактор русскоязычного Telegram-канала про игровую индустрию.
Ниже посты канала за неделю (заголовок: описание).

{lines}

Задача: составь пост «Итоги недели». Выбери 5-7 самых важных событий (не повторяй одно и то же), по каждому одно короткое предложение своими словами.
Формат: первая строка <b>Итоги недели</b>, пустая строка, затем нумерованный список (1., 2., ...), одно событие на строку.
Разрешён только тег <b>. Без markdown. Весь текст до 1200 символов.
В конце, после пустой строки, одна строка: #итогинедели
Отвечай только текстом поста."""
    try:
        text = ask_llm(prompt).strip()
    except Exception as ex:
        print("LLM error (digest):", ex)
        return  # попробуем в следующий запуск, пока не кончилось воскресенье
    if not text:
        return
    text += f"\n\n{SIGNATURE}"
    state["queue"].insert(0, {"post": text, "image": None, "ts": now.isoformat(), "tries": 0, "digest": True})
    state["last_digest"] = week_id
    print("Дайджест поставлен в очередь")


def publish_due(state, now):
    """Публикует один пост из очереди, если с прошлой публикации прошла пауза."""
    if not state["queue"]:
        print("Очередь пуста")
        return
    last = state.get("last_post")
    # допуск 5 минут: расписание GitHub срабатывает не секунда в секунду
    need = timedelta(minutes=max(POST_INTERVAL_MIN - 5, 0))
    if last and now - datetime.fromisoformat(last) < need:
        print("Ещё рано публиковать, в очереди:", len(state["queue"]))
        return
    item = state["queue"][0]
    if send_post(item["post"], item["image"]):
        state["queue"].pop(0)
        state["last_post"] = now.isoformat()
        if not item.get("digest"):
            title, body = post_parts(item["post"])
            state["published"].append({"ts": now.isoformat(), "title": title, "body": body})
        print("Опубликован пост, осталось в очереди:", len(state["queue"]))
    else:
        item["tries"] = item.get("tries", 0) + 1
        if item["tries"] >= 3:
            state["queue"].pop(0)  # не блокируем очередь из-за проблемного поста


def main():
    state = load_state()
    now = datetime.now(timezone.utc)
    fresh = timedelta(hours=QUEUE_MAX_AGE_HOURS)
    state["queue"] = [q for q in state["queue"] if now - datetime.fromisoformat(q["ts"]) < fresh]
    state["published"] = [
        p for p in state["published"] if now - datetime.fromisoformat(p["ts"]) < timedelta(days=8)
    ]
    fill_queue(state, now)
    maybe_queue_digest(state, now)
    publish_due(state, now)
    save_state(state)


if __name__ == "__main__":
    main()
