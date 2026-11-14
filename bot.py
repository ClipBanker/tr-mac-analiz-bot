#!/usr/bin/env python3
"""
Бот-контентщик для Telegram-канала с футбольной аналитикой (турецкая аудитория).

Один запуск = один "тик" (GitHub Actions запускает раз в час). За тик бот:
  1. публикует результаты прошедших прогнозов и экспресса;
  2. утром — итог вчерашнего дня и анонс (с фото из папки photos/);
  3. днём — экспресс дня (2–3 события);
  4. раз в N часов — разбор и прогноз на ближайший интересный матч с картинкой-карточкой.
Состояние хранится в state.json (коммитится обратно в репозиторий).

Переменные окружения:
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHANNEL, API_FOOTBALL_KEY, LLM_API_KEY (необязательно)

Запуск:
  python bot.py                 боевой режим
  python bot.py --force         опубликовать прогноз сразу
  python bot.py --dry-run       ничего не отправлять, печатать посты и сохранять картинки в /tmp
  python bot.py --mock --dry-run --now "2026-11-15 17:10"   тест на мок-данных
"""
import argparse
import html
import io
import json
import os
import random
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests
import yaml

ROOT = Path(__file__).resolve().parent
STATE_FILE = Path(os.environ.get("BOT_STATE_FILE", ROOT / "state.json"))
MOCK_DIR = ROOT / "mock"
PHOTOS_DIR = ROOT / "photos"
API_BASE = "https://v3.football.api-sports.io"

MARKETS = {
    "1X2": {"1": "MS 1", "X": "MS X", "2": "MS 2"},
    "DC": {"1X": "ÇŞ 1-X", "12": "ÇŞ 1-2", "X2": "ÇŞ X-2"},
    "OU25": {"Over": "2.5 Üst", "Under": "2.5 Alt"},
    "BTTS": {"Yes": "KG Var", "No": "KG Yok"},
}
FORBIDDEN_WORDS = ["garanti", "kesin kazanç", "%100", "100%", "şike", "bedava para", "risksiz"]
DEAD_PHRASES = ["sonuç olarak", "genel olarak", "dikkat çekiyor", "göz önünde bulundurulduğunda",
                "önemli bir faktör", "değerlendirildiğinde", "unutulmamalıdır"]


def log(*a):
    print("[bot]", *a, file=sys.stderr)


# ================================================================ утилиты
def load_config(path="config.yaml"):
    with open(ROOT / path, encoding="utf-8") as f:
        return yaml.safe_load(f)


def persona(cfg, base):
    """Добавляет к системному промпту стиль конкретного канала из config (persona)."""
    extra = (cfg.get("persona") or "").strip()
    return base + ("\n\nKanalın tarzı (buna mutlaka uy):\n" + extra if extra else "")


def rgb(cfg, key, default):
    v = cfg.get(key)
    return tuple(v) if isinstance(v, (list, tuple)) and len(v) == 3 else default


def load_state():
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8") or "{}")
        except json.JSONDecodeError:
            log("state.json повреждён, начинаю заново")
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def esc(s):
    return html.escape(str(s), quote=False)


def parse_dt(s, tz):
    return datetime.fromisoformat(s.replace("Z", "+00:00")).astimezone(tz)


# ================================================================ данные
class Football:
    """Клиент API-Football. В режиме mock читает файлы из папки mock/."""

    def __init__(self, key, mock=False):
        self.key = key
        self.mock = mock
        self.calls = 0

    def _get(self, path, params, mock_name):
        if self.mock:
            p = MOCK_DIR / mock_name
            if not p.exists():
                return []
            return json.loads(p.read_text(encoding="utf-8")).get("response", [])
        self.calls += 1
        r = requests.get(f"{API_BASE}/{path}", params=params,
                         headers={"x-apisports-key": self.key}, timeout=30)
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"API-Football error: {data['errors']}")
        return data.get("response", [])

    def fixtures_by_date(self, date_str, tz_name):
        return self._get("fixtures", {"date": date_str, "timezone": tz_name}, f"fixtures_{date_str}.json")

    def fixtures_by_ids(self, ids):
        ids = [str(i) for i in ids][:20]
        return self._get("fixtures", {"ids": "-".join(ids)}, "fixtures_ids.json")

    def prediction(self, fid):
        res = self._get("predictions", {"fixture": fid}, f"predictions_{fid}.json")
        return res[0] if res else None

    def odds(self, fid):
        res = self._get("odds", {"fixture": fid}, f"odds_{fid}.json")
        return res[0] if res else None

    def injuries(self, fid):
        try:
            return self._get("injuries", {"fixture": fid}, f"injuries_{fid}.json")
        except Exception as e:  # noqa: BLE001
            log("injuries:", e)
            return []

    def standings(self, league, season):
        try:
            res = self._get("standings", {"league": league, "season": season}, f"standings_{league}.json")
        except Exception as e:  # noqa: BLE001
            log("standings:", e)
            return {}
        table = {}
        for item in res:
            for group in item.get("league", {}).get("standings", []):
                for row in group:
                    table[row["team"]["name"]] = {"rank": row.get("rank"), "points": row.get("points"),
                                                  "group": row.get("group", "")}
        return table


# ================================================================ коэффициенты
def extract_odds(odds_resp):
    out = {}
    if not odds_resp:
        return out
    name_map = {
        "Match Winner": ("1X2", {"Home": "1", "Draw": "X", "Away": "2"}),
        "Double Chance": ("DC", {"Home/Draw": "1X", "Home/Away": "12", "Draw/Away": "X2"}),
        "Goals Over/Under": ("OU25", {"Over 2.5": "Over", "Under 2.5": "Under"}),
        "Both Teams Score": ("BTTS", {"Yes": "Yes", "No": "No"}),
    }
    for bm in odds_resp.get("bookmakers", []):
        for bet in bm.get("bets", []):
            if bet.get("name") not in name_map:
                continue
            market, vmap = name_map[bet["name"]]
            for v in bet.get("values", []):
                sel = vmap.get(str(v.get("value")))
                if sel and (market, sel) not in out:
                    try:
                        out[(market, sel)] = float(v["odd"])
                    except (TypeError, ValueError):
                        pass
    return out


# ================================================================ сводка для нейросети
def build_summary(fx, pred, table=None, injuries=None):
    home = fx["teams"]["home"]["name"]
    away = fx["teams"]["away"]["name"]
    lines = [f"Maç: {home} - {away}", f"Lig: {fx['league']['name']} ({fx['league'].get('round', '')})"]
    if table:
        for t in (home, away):
            r = table.get(t)
            if r:
                g = f" (grup {r['group']})" if r.get("group") and "Group" in str(r["group"]) else ""
                lines.append(f"Puan durumu: {t} {r['rank']}. sırada, {r['points']} puan{g}")
    if injuries:
        by_team = {}
        for i in injuries:
            by_team.setdefault(i["team"]["name"], []).append(
                f"{i['player']['name']} ({i['player'].get('reason') or i['player'].get('type') or 'eksik'})")
        for t, pl in by_team.items():
            lines.append(f"Eksikler {t}: " + ", ".join(pl[:5]))
    if not pred:
        return "\n".join(lines)
    p = pred.get("predictions", {})
    pc = p.get("percent", {})
    lines.append(f"Kazanma olasılıkları: ev {pc.get('home')}, beraberlik {pc.get('draw')}, deplasman {pc.get('away')}")
    if p.get("advice"):
        lines.append(f"Model tavsiyesi (İngilizce): {p['advice']}")
    if p.get("under_over"):
        lines.append(f"Gol çizgisi önerisi: {p['under_over']}")
    teams = pred.get("teams", {})
    for side, label in (("home", "Ev sahibi"), ("away", "Deplasman")):
        t = teams.get(side, {})
        l5 = t.get("last_5", {})
        lg = t.get("league", {})
        gf = l5.get("goals", {}).get("for", {}).get("average")
        ga = l5.get("goals", {}).get("against", {}).get("average")
        lines.append(
            f"{label} {t.get('name')}: son 5 maç form {l5.get('form')}, atak {l5.get('att')}, defans {l5.get('def')}, "
            f"son 5 maç gol ort. attığı {gf} / yediği {ga}; son maçlar {str(lg.get('form', ''))[-6:]}")
    comp = pred.get("comparison", {})
    if comp:
        lines.append("Karşılaştırma (ev/dep): " + ", ".join(
            f"{k} {v.get('home')}/{v.get('away')}" for k, v in comp.items() if isinstance(v, dict)))
    h2h = pred.get("h2h", [])[:5]
    if h2h:
        res = [f"{m['teams']['home']['name']} {m.get('goals', {}).get('home')}-{m.get('goals', {}).get('away')} "
               f"{m['teams']['away']['name']}" for m in h2h]
        lines.append("Son karşılaşmalar: " + "; ".join(res))
    return "\n".join(lines)


# ================================================================ нейросеть
LLM_SYSTEM = """Sen Türkiye'de futbol üzerine yazan, takipçisi bol bir Telegram blogger'ısın. Kendi kanalında maç yorumu paylaşıyorsun.
Üslup:
- Birinci tekil şahıs, samimi, sohbet eder gibi: "bence", "açıkçası", "bu maçta gözüm ... üzerinde", "ben burada ... diyorum".
- Kısa, canlı cümleler. Gerçek bir insanın telefondan yazdığı gibi. Arada küçük bir espri ya da futbol argosu olabilir.
- Her seferinde farklı bir girişle başla, kalıp cümle tekrarlama.
- ASLA şu yapay zeka kalıplarını kullanma: "Sonuç olarak", "Genel olarak", "dikkat çekiyor", "göz önünde bulundurulduğunda", "önemli bir faktör", "değerlendirildiğinde", "unutulmamalıdır".
- Emoji en fazla 2 tane, metnin içinde doğal yerlerde.
Kurallar:
- Yalnızca Türkçe yaz.
- Sadece verilen verilere dayan. Verilerde olmayan sakatlık, transfer, haber, istatistik UYDURMA.
- "garanti", "kesin", "%100", "risksiz", "şike" gibi ifadeler KULLANMA. Bu senin görüşün.
- Tek bir tahmin seç. İzinli pazarlar ve seçimler:
  1X2: "1", "X", "2" | DC: "1X", "12", "X2" | OU25: "Over", "Under" | BTTS: "Yes", "No"
- Oranlar verilmişse min_odd ile max_odd arasındaki seçimleri tercih et.
- "hook": dikkat çeken tek bir kısa açılış cümlesi (en fazla 70 karakter).
- "analysis": 2-4 cümle, en fazla 420 karakter.
- "confidence": 1-5 arası tam sayı.
Cevabın SADECE şu JSON olsun:
{"hook": "...", "market": "...", "selection": "...", "confidence": 3, "analysis": "..."}"""


def llm_call(provider, model, key, system, user, json_mode=True):
    if provider == "gemini":
        models = [model or "gemini-pro-latest", "gemini-flash-latest"]
        last_err = None
        for m in dict.fromkeys(models):
            try:
                cfg = {"temperature": 0.9}
                if json_mode:
                    cfg["responseMimeType"] = "application/json"
                r = requests.post(
                    f"https://generativelanguage.googleapis.com/v1beta/models/{m}:generateContent",
                    params={"key": key},
                    json={"systemInstruction": {"parts": [{"text": system}]},
                          "contents": [{"role": "user", "parts": [{"text": user}]}],
                          "generationConfig": cfg},
                    timeout=90)
                r.raise_for_status()
                return r.json()["candidates"][0]["content"]["parts"][0]["text"]
            except Exception as e:  # noqa: BLE001
                last_err = e
                log(f"Gemini {m}: {e}")
        raise RuntimeError(last_err)
    if provider == "anthropic":
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model or "claude-sonnet-5-5", "max_tokens": 900, "system": system,
                  "messages": [{"role": "user", "content": user}]}, timeout=90)
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])
    if provider == "openai":
        body = {"model": model or "gpt-4o", "temperature": 0.9,
                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        r = requests.post("https://api.openai.com/v1/chat/completions",
                          headers={"Authorization": f"Bearer {key}"}, json=body, timeout=90)
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    raise ValueError(f"Неизвестный провайдер: {provider}")


def text_ok(t):
    low = t.lower()
    for w in FORBIDDEN_WORDS:
        if w in low:
            raise ValueError(f"запрещённое слово: {w}")
    for w in DEAD_PHRASES:
        if w in low:
            raise ValueError(f"шаблонная фраза: {w}")
    return t


def parse_llm_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("нет JSON в ответе")
    d = json.loads(m.group(0))
    market, sel = d.get("market"), d.get("selection")
    if market not in MARKETS or sel not in MARKETS[market]:
        raise ValueError(f"недопустимый выбор {market}/{sel}")
    analysis = text_ok(str(d.get("analysis", "")).strip())[:600]
    hook = text_ok(str(d.get("hook", "")).strip())[:90]
    conf = int(d.get("confidence", 3))
    return {"market": market, "selection": sel, "confidence": max(1, min(5, conf)),
            "analysis": analysis, "hook": hook}


def llm_text(cfg, system, user, fallback):
    """Свободный текст от нейросети (анонсы, экспресс). При ошибке — fallback."""
    provider = (cfg.get("llm_provider") or "none").lower()
    key = os.environ.get("LLM_API_KEY", "")
    if provider == "none" or not key:
        return fallback
    for _ in range(2):
        try:
            t = llm_call(provider, cfg.get("llm_model") or "", key, system, user, json_mode=False).strip()
            t = t.strip("`\"' \n")
            return text_ok(t)[:500]
        except Exception as e:  # noqa: BLE001
            log("LLM текст:", e)
    return fallback


BLOGGER_SHORT = """Sen samimi bir Türk futbol Telegram blogger'ısın. Birinci tekil şahıs, kısa ve canlı yazıyorsun.
Yapay zeka kalıpları ("Sonuç olarak", "Genel olarak", "dikkat çekiyor") kullanma. "garanti", "%100", "kesin" deme.
Emoji en fazla 2. Sadece istenen metni yaz, başka açıklama ekleme."""


# ================================================================ шаблонный прогноз (без нейросети)
def template_pick(fx, pred, odds, cfg, safe=False):
    home = fx["teams"]["home"]["name"]
    away = fx["teams"]["away"]["name"]
    p = (pred or {}).get("predictions", {})
    pct = {k: int(str(v).rstrip("%") or 0) for k, v in (p.get("percent") or {}).items()}
    h, d, a = pct.get("home", 0), pct.get("draw", 0), pct.get("away", 0)
    candidates = []
    if not safe and h >= 50:
        candidates.append(("1X2", "1"))
    if not safe and a >= 50:
        candidates.append(("1X2", "2"))
    candidates.append(("DC", "1X" if h >= a else "X2"))
    uo = str(p.get("under_over") or "")
    try:
        line = float(uo[1:] or 0)
    except ValueError:
        line = 0
    if uo.startswith("+") and line >= 2.5:
        candidates.append(("OU25", "Over"))
    elif uo.startswith("-") and 0 < line <= 2.5:
        candidates.append(("OU25", "Under"))
    lo, hi = (1.20, 1.75) if safe else (cfg["min_odd"], cfg["max_odd"])
    chosen = next((c for c in candidates if odds.get(c) is None or lo <= odds[c] <= hi), candidates[0])

    t = (pred or {}).get("teams", {})
    hf = str(t.get("home", {}).get("last_5", {}).get("form", "-")).rstrip("%")
    af = str(t.get("away", {}).get("last_5", {}).get("form", "-")).rstrip("%")
    fav = home if h >= a else away
    rnd = random.Random(fx["fixture"]["id"])
    hook = rnd.choice([f"Bu akşam gözüm {home} – {away} maçında 👀", f"{home} – {away}: benim notlarım",
                       f"Bu maçı kaçırmayın, anlatayım 👇", f"{fav} için güzel bir fırsat görüyorum"])
    analysis = rnd.choice([
        f"Son 5 maçta {home} %{hf}, {away} %{af} formda. Rakamlar açıkçası {fav} tarafını gösteriyor, "
        f"olasılıklar %{h} / %{d} / %{a}. Ben burada risk almadan ilerliyorum.",
        f"{home} formu %{hf}, {away} ise %{af}. Bence {fav} bu maçta bir adım önde, "
        f"model de %{h} / %{d} / %{a} diyor. Benim tercihim aşağıda.",
    ])
    return {"market": chosen[0], "selection": chosen[1], "confidence": 4 if max(h, a) >= 55 else 3,
            "analysis": analysis, "hook": hook}


def make_pick(fx, pred, odds, cfg, table=None, injuries=None):
    provider = (cfg.get("llm_provider") or "none").lower()
    key = os.environ.get("LLM_API_KEY", "")
    if provider != "none" and key:
        summary = build_summary(fx, pred, table, injuries)
        odds_txt = ", ".join(f"{m}:{s}={o}" for (m, s), o in sorted(odds.items())) or "yok"
        user = f"{summary}\nOranlar: {odds_txt}\nmin_odd={cfg['min_odd']} max_odd={cfg['max_odd']}"
        for attempt in range(3):
            try:
                pick = parse_llm_json(llm_call(provider, cfg.get("llm_model") or "", key, persona(cfg, LLM_SYSTEM), user))
                pick["source"] = provider
                return pick
            except Exception as e:  # noqa: BLE001
                log(f"LLM попытка {attempt + 1}: {e}")
    pick = template_pick(fx, pred, odds, cfg)
    pick["source"] = "template"
    return pick


# ================================================================ оценка результата
def evaluate(market, sel, gh, ga):
    if gh is None or ga is None:
        return None
    res = "1" if gh > ga else "2" if ga > gh else "X"
    if market == "1X2":
        return sel == res
    if market == "DC":
        return res in sel
    if market == "OU25":
        return (gh + ga > 2.5) if sel == "Over" else (gh + ga < 2.5)
    if market == "BTTS":
        both = gh > 0 and ga > 0
        return both if sel == "Yes" else not both
    return None


# ================================================================ картинки
FONT_PATHS = {
    "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf"],
    "regular": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf"],
}


def _font(kind, size):
    from PIL import ImageFont
    for p in FONT_PATHS[kind]:
        if os.path.exists(p):
            return ImageFont.truetype(p, size)
    return ImageFont.load_default()


def _logo(url, size):
    from PIL import Image
    if not url:
        return None
    try:
        r = requests.get(url, timeout=15)
        r.raise_for_status()
        im = Image.open(io.BytesIO(r.content)).convert("RGBA")
        im.thumbnail((size, size))
        return im
    except Exception as e:  # noqa: BLE001
        log("logo:", e)
        return None


def _background(W, H, accent):
    from PIL import Image, ImageDraw
    img = Image.new("RGB", (W, H), (10, 14, 26))
    d = ImageDraw.Draw(img)
    for y in range(H):  # вертикальный градиент
        k = y / H
        d.line([(0, y), (W, y)], fill=(int(10 + 18 * k), int(14 + 30 * k), int(26 + 40 * k)))
    for i in range(0, W + H, 60):  # диагональные полосы
        d.line([(i, 0), (i - H, H)], fill=(18, 26, 44), width=2)
    d.rectangle([0, 0, W, 14], fill=accent)
    d.rectangle([0, H - 14, W, H], fill=accent)
    return img, d


def _center(d, text, y, font, fill, W):
    text = "".join(ch for ch in str(text) if ord(ch) <= 0xFFFF).strip()  # эмодзи шрифт не рисует
    w = d.textlength(text, font=font)
    d.text(((W - w) / 2, y), text, font=font, fill=fill)


def _fit(d, text, kind, size, max_w):
    f = _font(kind, size)
    while d.textlength(text, font=f) > max_w and size > 18:
        size -= 2
        f = _font(kind, size)
    return f


def match_card(fx, pick, odd, cfg, tz):
    """Карточка 1080x1080: лига, время, логотипы, прогноз, коэффициент."""
    from PIL import Image
    W = H = 1080
    accent = rgb(cfg, "card_accent", (255, 196, 0))
    img, d = _background(W, H, accent)
    league = fx["league"]["name"].upper()
    kt = parse_dt(fx["fixture"]["date"], tz).strftime("%d.%m  •  %H:%M")
    _center(d, league, 70, _fit(d, league, "bold", 40, W - 120), (200, 210, 230), W)
    _center(d, kt, 125, _font("regular", 34), (150, 165, 190), W)
    for side, cx in (("home", W // 4), ("away", 3 * W // 4)):
        t = fx["teams"][side]
        lg = _logo(t.get("logo"), 230)
        if lg:
            img.paste(lg, (cx - lg.width // 2, 230 + (230 - lg.height) // 2), lg)
        else:
            d.ellipse([cx - 110, 235, cx + 110, 455], outline=accent, width=6)
            ini = t["name"][:3].upper()
            f = _font("bold", 70)
            d.text((cx - d.textlength(ini, font=f) / 2, 305), ini, font=f, fill=(255, 255, 255))
        name = t["name"]
        f = _fit(d, name, "bold", 44, W // 2 - 60)
        d.text((cx - d.textlength(name, font=f) / 2, 490), name, font=f, fill=(255, 255, 255))
    _center(d, "VS", 315, _font("bold", 64), accent, W)
    d.rounded_rectangle([110, 640, W - 110, 900], radius=36, fill=(22, 32, 56), outline=accent, width=4)
    _center(d, cfg.get("card_pick_label", "TAHMİNİM"), 670, _font("bold", 34), (170, 185, 210), W)
    label = MARKETS[pick["market"]][pick["selection"]]
    _center(d, label, 725, _fit(d, label, "bold", 96, W - 280), accent, W)
    if odd:
        _center(d, f"ORAN  {odd:.2f}", 840, _font("bold", 40), (255, 255, 255), W)
    _center(d, "★" * pick["confidence"] + "☆" * (5 - pick["confidence"]), 940,
            _font("regular", 48), accent, W)
    _center(d, cfg.get("channel_name", ""), 1010, _font("bold", 32), (120, 135, 160), W)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def express_card(legs, total, cfg, tz, date_label):
    W, H = 1080, 1080
    accent = rgb(cfg, "express_accent", (0, 220, 140))
    img, d = _background(W, H, accent)
    _center(d, "GÜNÜN KUPONU", 70, _font("bold", 72), accent, W)
    _center(d, date_label, 160, _font("regular", 36), (150, 165, 190), W)
    y = 250
    for leg in legs:
        d.rounded_rectangle([80, y, W - 80, y + 170], radius=28, fill=(22, 32, 56))
        teams = f"{leg['home']} – {leg['away']}"
        d.text((120, y + 22), teams, font=_fit(d, teams, "bold", 40, W - 480), fill=(255, 255, 255))
        kt = parse_dt(leg["kickoff"], tz).strftime("%H:%M")
        d.text((120, y + 82), f"{kt}  •  {leg['league']}"[:48], font=_font("regular", 30), fill=(150, 165, 190))
        lab = MARKETS[leg["market"]][leg["selection"]]
        o = f"{leg['odd']:.2f}" if leg.get("odd") else ""
        f = _font("bold", 42)
        d.text((W - 120 - d.textlength(lab, font=f), y + 22), lab, font=f, fill=accent)
        if o:
            f2 = _font("bold", 34)
            d.text((W - 120 - d.textlength(o, font=f2), y + 90), o, font=f2, fill=(255, 255, 255))
        y += 200
    if total:
        d.rounded_rectangle([250, y + 20, W - 250, y + 130], radius=30, outline=accent, width=4)
        _center(d, f"TOPLAM ORAN  {total:.2f}", y + 52, _font("bold", 44), (255, 255, 255), W)
    _center(d, cfg.get("channel_name", ""), 1010, _font("bold", 32), (120, 135, 160), W)
    buf = io.BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def random_photo(state, cfg):
    """Случайное фото из папки photos/ (не повторяя последнее)."""
    d = ROOT / cfg.get("photos_dir", "photos")
    if not d.exists():
        return None
    files = sorted(p for p in d.iterdir() if p.suffix.lower() in (".jpg", ".jpeg", ".png", ".webp"))
    if not files:
        return None
    last = state.get("last_photo")
    pool = [p for p in files if p.name != last] or files
    p = random.choice(pool)
    state["last_photo"] = p.name
    return p.read_bytes()


# ================================================================ тексты постов
def stars(n):
    return "⭐" * n


def post_prediction(fx, pick, odd, cfg, tz):
    home, away = fx["teams"]["home"]["name"], fx["teams"]["away"]["name"]
    kt = parse_dt(fx["fixture"]["date"], tz).strftime("%H:%M")
    label = MARKETS[pick["market"]][pick["selection"]]
    odd_txt = f" @ <b>{odd:.2f}</b>" if odd else ""
    hook = f"<b>{esc(pick['hook'])}</b>\n\n" if pick.get("hook") else ""
    cta = random.choice(cfg.get("cta_lines") or [""])
    return (f"{hook}⚽ {esc(home)} – {esc(away)}\n🏆 {esc(fx['league']['name'])} | 🕗 {kt}\n\n"
            f"{esc(pick['analysis'])}\n\n"
            f"🎯 {esc(cfg.get('pick_label', 'Benim tercihim'))}: <b>{esc(label)}</b>{odd_txt}\n"
            f"📈 Güven: {stars(pick['confidence'])}\n\n"
            + (f"{esc(cta)}\n\n" if cta else "") + f"{esc(cfg['disclaimer'])}")


def post_digest(fixtures, cfg, tz, date_label):
    games = "\n".join(f"🕗 {parse_dt(fx['fixture']['date'], tz).strftime('%H:%M')} | "
                      f"{esc(fx['teams']['home']['name'])} – {esc(fx['teams']['away']['name'])}" for fx in fixtures)
    plain = "; ".join(f"{fx['teams']['home']['name']} - {fx['teams']['away']['name']}" for fx in fixtures)
    intro = llm_text(cfg, persona(cfg, BLOGGER_SHORT),
                     f"Takipçilerine günaydın de ve bugünün maç programını tanıt (2-3 cümle, en fazla 250 karakter). "
                     f"Bugünkü maçlar: {plain}. Gün boyunca analizlerin geleceğini söyle.",
                     random.choice(["Günaydın millet ☀️ Bugün program dolu, kahvemi aldım, analizler geliyor.",
                                    "Günaydın! Bugün bakacağımız maçlar belli, gün boyunca yorumlarım burada 👇"]))
    return (f"{esc(intro)}\n\n📅 <b>Günün maçları — {date_label}</b>\n{games}\n\n{esc(cfg['disclaimer'])}")


def post_result(p, gh, ga, won, cfg=None):
    cfg = cfg or {}
    label = MARKETS[p["market"]][p["selection"]]
    if won:
        line = random.choice(cfg.get("result_win") or ["Tuttu! 🔥", "Bu da geldi ✅", "Dediğim gibi oldu 😎", "Cebimize koyduk ✅"])
    else:
        line = random.choice(cfg.get("result_loss") or ["Bu sefer olmadı ❌", "Bu maç bizi üzdü ❌", "Futbol bu, olmadı ❌ Devam ediyoruz."])
    return f"{line}\n<b>{esc(p['home'])} {gh}-{ga} {esc(p['away'])}</b>\n{esc(cfg.get('pick_label', 'Tercihim'))}: {esc(label)}"


def post_recap(day, totals):
    w, l = day.get("won", 0), day.get("lost", 0)
    if w + l == 0:
        return None
    tw, tl = totals.get("won", 0), totals.get("lost", 0)
    rate = round(100 * tw / (tw + tl)) if tw + tl else 0
    mood = "Dün güzel bir gündü 🔥" if w > l else ("Dün dengeli geçti." if w == l else "Dün istediğimiz gibi olmadı, bugün telafi zamanı 💪")
    return (f"📊 <b>Dünün özeti</b>\n\n{mood}\n✅ {w}  ❌ {l}\n\n"
            f"Genel: {tw} doğru / {tl} yanlış (%{rate})\n\nBugünün analizleri birazdan kanalda 👋")


def post_express(legs, total, intro, cfg, tz):
    lines = []
    for i, leg in enumerate(legs, 1):
        kt = parse_dt(leg["kickoff"], tz).strftime("%H:%M")
        o = f" @ {leg['odd']:.2f}" if leg.get("odd") else ""
        lines.append(f"{i}️⃣ {esc(leg['home'])} – {esc(leg['away'])} ({kt})\n    👉 <b>{esc(MARKETS[leg['market']][leg['selection']])}</b>{o}")
    tot = f"\n\n💰 Toplam oran: <b>{total:.2f}</b>" if total else ""
    return f"🎟 <b>Günün kuponu</b>\n\n{esc(intro)}\n\n" + "\n".join(lines) + tot + f"\n\n{esc(cfg['disclaimer'])}"


def post_express_result(ex, results):
    marks = "\n".join(f"{'✅' if r else '❌'} {esc(l['home'])} – {esc(l['away'])} ({esc(l['score'])})"
                      for l, r in zip(ex["legs"], results))
    head = "🎟 <b>Kupon geçti!</b> 🔥" if all(results) else "🎟 Kupon bu sefer yattı ❌"
    return f"{head}\n\n{marks}"


# ================================================================ Telegram
_dry_n = [0]


def tg_send(text, dry, photo=None):
    if dry:
        print("\n" + "=" * 60 + "\n" + text + "\n" + "=" * 60)
        if photo:
            _dry_n[0] += 1
            p = f"/tmp/bot_post_{_dry_n[0]}.png"
            Path(p).write_bytes(photo)
            print(f"[картинка сохранена: {p}]")
        return True
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHANNEL"]
    base = f"https://api.telegram.org/bot{token}"
    if photo:
        cap_ok = len(re.sub(r"<[^>]+>", "", text)) <= 1024
        r = requests.post(f"{base}/sendPhoto",
                          data={"chat_id": chat, "caption": text if cap_ok else "", "parse_mode": "HTML"},
                          files={"photo": ("post.png", photo)}, timeout=60)
        if not r.ok:
            log("Telegram sendPhoto error:", r.status_code, r.text[:300])
        elif cap_ok:
            return True
    r = requests.post(f"{base}/sendMessage", json={"chat_id": chat, "text": text, "parse_mode": "HTML",
                                                   "disable_web_page_preview": True}, timeout=30)
    if not r.ok:
        log("Telegram error:", r.status_code, r.text[:300])
        return False
    return True


# ================================================================ выбор матчей
EXCLUDE_RE = re.compile(r"women|kadın|\bu1[5-9]\b|\bu2[0-3]\b|youth|reserve|\bii\b|amateur|futsal", re.I)


def compact_fx(fx):
    return {
        "fixture": {"id": fx["fixture"]["id"], "date": fx["fixture"]["date"],
                    "timestamp": fx["fixture"]["timestamp"], "status": {"short": fx["fixture"]["status"]["short"]}},
        "league": {"id": fx["league"]["id"], "name": fx["league"]["name"], "season": fx["league"].get("season"),
                   "country": fx["league"].get("country", ""), "round": fx["league"].get("round", "")},
        "teams": {s: {"name": fx["teams"][s]["name"], "logo": fx["teams"][s].get("logo", "")} for s in ("home", "away")},
    }


def rank_fixture(fx, cfg):
    order = {l["id"]: i for i, l in enumerate(cfg["leagues"])}
    prio_teams = [t.lower() for t in cfg.get("priority_teams", [])]
    names = (fx["teams"]["home"]["name"] + " " + fx["teams"]["away"]["name"]).lower()
    lid = fx["league"]["id"]
    if lid in order:
        base = order[lid]
    elif cfg.get("fallback_any_league"):
        if EXCLUDE_RE.search(fx["league"]["name"] + " " + names):
            return None
        countries = cfg.get("fallback_countries", [])
        c = fx["league"].get("country", "")
        base = 100 + (countries.index(c) if c in countries else 50)
    else:
        return None
    if any(t in names for t in prio_teams):
        base -= 1000
    return base


def filter_day_fixtures(fixtures, cfg, limit=80):
    pool = []
    for fx in fixtures:
        if fx["fixture"]["status"]["short"] != "NS":
            continue
        r = rank_fixture(fx, cfg)
        if r is not None:
            pool.append((r, fx["fixture"]["timestamp"], compact_fx(fx)))
    pool.sort(key=lambda x: (x[0], x[1]))
    return [p[2] for p in pool[:limit]]


# ================================================================ основной тик
def run(args):
    cfg = load_config(args.config)
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M").replace(tzinfo=tz) if args.now else datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    dry = args.dry_run
    interval = float(cfg.get("post_interval_hours", 2))
    q_from, q_to = (cfg.get("quiet_hours") or [None, None])
    quiet = q_from is not None and (q_from <= now.hour < q_to if q_from < q_to else (now.hour >= q_from or now.hour < q_to))
    use_images = cfg.get("images", True)

    api = Football(os.environ.get("API_FOOTBALL_KEY", ""), mock=args.mock)
    if not args.mock and not api.key:
        sys.exit("Не задан API_FOOTBALL_KEY")
    if not dry and not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHANNEL")):
        sys.exit("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL")

    state = load_state()
    state.setdefault("totals", {"won": 0, "lost": 0})
    state.setdefault("pending", {})
    state.setdefault("posted_ids", [])
    state.setdefault("nodata_ids", [])
    state.setdefault("express", {})
    state.setdefault("standings", {})
    fixtures_by_date = state.setdefault("fixtures", {})
    days = state.setdefault("days", {})
    day = days.setdefault(today, {})
    for k, v in {"digest": False, "recap": False, "won": 0, "lost": 0}.items():
        day.setdefault(k, v)
    cutoff = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    for d in sorted(days)[:-7]:
        days.pop(d, None)
    for coll in (fixtures_by_date, state["standings"]):
        for d in list(coll):
            if d < cutoff:
                coll.pop(d)
    for d in list(state["express"]):
        if d < (now - timedelta(days=3)).strftime("%Y-%m-%d"):
            state["express"].pop(d)
    state["posted_ids"] = state["posted_ids"][-300:]
    state["nodata_ids"] = state["nodata_ids"][-300:]

    def fixtures_for(date_str):
        if date_str not in fixtures_by_date:
            fixtures_by_date[date_str] = filter_day_fixtures(api.fixtures_by_date(date_str, cfg["timezone"]), cfg)
            log(f"Загружены матчи на {date_str}: {len(fixtures_by_date[date_str])}")
        return fixtures_by_date[date_str]

    def table_for(fx):
        if not cfg.get("use_standings", True) or not fx["league"].get("season"):
            return {}
        key = f"{fx['league']['id']}-{fx['league']['season']}"
        cache = state["standings"].setdefault(today, {})
        if key not in cache:
            cache[key] = api.standings(fx["league"]["id"], fx["league"]["season"])
        return cache[key]

    def candidates(min_minutes, hours):
        end = now + timedelta(hours=hours)
        out = []
        for d in sorted({today, end.strftime("%Y-%m-%d")}):
            for fx in fixtures_for(d):
                fid = fx["fixture"]["id"]
                mins = (parse_dt(fx["fixture"]["date"], tz) - now).total_seconds() / 60
                if fid in state["nodata_ids"]:
                    continue
                if mins >= min_minutes and parse_dt(fx["fixture"]["date"], tz) <= end:
                    out.append(fx)
        out.sort(key=lambda fx: (rank_fixture(fx, cfg), fx["fixture"]["timestamp"]))
        return out

    # 1. Результаты прогнозов и экспресса
    open_express = [(d, ex) for d, ex in state["express"].items() if not ex.get("done")]
    exp_ids = {str(l["id"]) for _, ex in open_express for l in ex["legs"]}
    due = [fid for fid, p in state["pending"].items()
           if (now - parse_dt(p["kickoff"], tz)).total_seconds() / 60 >= cfg["results_check_after_min"]]
    due_exp = [i for i in exp_ids
               if any(str(l["id"]) == i and (now - parse_dt(l["kickoff"], tz)).total_seconds() / 60 >= cfg["results_check_after_min"]
                      for _, ex in open_express for l in ex["legs"])]
    finished = {}
    if (due or due_exp) and not quiet:
        wanted = set(due) | set(due_exp)
        for fx in api.fixtures_by_ids(sorted(wanted)):
            fid = str(fx["fixture"]["id"])
            if fid not in wanted:
                continue
            st = fx["fixture"]["status"]["short"]
            ft = fx.get("score", {}).get("fulltime", {})
            gh, ga = ft.get("home"), ft.get("away")
            if gh is None:
                gh, ga = fx["goals"]["home"], fx["goals"]["away"]
            finished[fid] = (st, gh, ga)
    for fid in due:
        if fid not in finished:
            continue
        st, gh, ga = finished[fid]
        p = state["pending"][fid]
        if st in ("PST", "CANC", "ABD", "AWD", "WO"):
            state["pending"].pop(fid)
            continue
        if st not in ("FT", "AET", "PEN"):
            continue
        won = evaluate(p["market"], p["selection"], gh, ga)
        if won is None:
            continue
        if tg_send(post_result(p, gh, ga, won, cfg), dry):
            key = "won" if won else "lost"
            state["totals"][key] += 1
            dd = days.setdefault(p["day"], {"won": 0, "lost": 0})
            dd[key] = dd.get(key, 0) + 1
            state["pending"].pop(fid)
    for d, ex in open_express:
        res = []
        for leg in ex["legs"]:
            f = finished.get(str(leg["id"])) or (("FT", leg["gh"], leg["ga"]) if "gh" in leg else None)
            if not f or f[0] not in ("FT", "AET", "PEN"):
                if f and f[0] in ("PST", "CANC", "ABD"):
                    ex["done"] = True  # матч отменён — экспресс не оцениваем
                res = None
                break
            leg["gh"], leg["ga"] = f[1], f[2]
            leg["score"] = f"{f[1]}-{f[2]}"
            res.append(evaluate(leg["market"], leg["selection"], f[1], f[2]))
        if res and not ex.get("done") and tg_send(post_express_result(ex, res), dry):
            ex["done"] = True

    # 2. Итог вчерашнего дня (утром, с фото)
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    yd = days.get(yday)
    if yd and not yd.get("recap") and not quiet and now.hour >= cfg["digest_hour"] - 1:
        if not [p for p in state["pending"].values() if p["day"] == yday]:
            text = post_recap(yd, state["totals"])
            if text is None or tg_send(text, dry):
                yd["recap"] = True

    # 3. Утренний анонс с фото
    if not day["digest"] and cfg["digest_hour"] <= now.hour < cfg["digest_hour"] + 2:
        upcoming = [fx for fx in fixtures_for(today) if parse_dt(fx["fixture"]["date"], tz) > now][: cfg.get("digest_size", 6)]
        upcoming.sort(key=lambda fx: fx["fixture"]["timestamp"])
        if upcoming and tg_send(post_digest(upcoming, cfg, tz, now.strftime("%d.%m")), dry,
                                photo=random_photo(state, cfg) if use_images else None):
            day["digest"] = True

    # 4. Экспресс дня
    ex_hour = cfg.get("express_hour")
    if ex_hour is not None and today not in state["express"] and ex_hour <= now.hour < ex_hour + 3 and not quiet:
        legs = []
        for fx in candidates(60, 14):
            if len(legs) >= cfg.get("express_size", 3):
                break
            fid = fx["fixture"]["id"]
            if fid in state["posted_ids"]:
                continue
            pred = api.prediction(fid)
            if not pred or not (pred.get("predictions") or {}).get("percent"):
                state["nodata_ids"].append(fid)
                continue
            odds = extract_odds(api.odds(fid))
            pk = template_pick(fx, pred, odds, cfg, safe=True)
            legs.append({"id": fid, "home": fx["teams"]["home"]["name"], "away": fx["teams"]["away"]["name"],
                         "league": fx["league"]["name"], "kickoff": fx["fixture"]["date"],
                         "market": pk["market"], "selection": pk["selection"],
                         "odd": odds.get((pk["market"], pk["selection"]))})
        if len(legs) >= 2:
            legs.sort(key=lambda l: l["kickoff"])
            total = 1.0
            for l in legs:
                total = total * l["odd"] if l.get("odd") and total else None
            plain = "; ".join(f"{l['home']} - {l['away']}: {MARKETS[l['market']][l['selection']]}" for l in legs)
            intro = llm_text(cfg, persona(cfg, BLOGGER_SHORT),
                             f"Bugünün kuponunu takipçilerine kısa ve heyecanlı tanıt (1-2 cümle, en fazla 180 karakter). Kupon: {plain}",
                             random.choice(["Bugünün kuponunu hazırladım, bence gayet mantıklı bir kombinasyon 👇",
                                            "Günün kuponu geldi! Ben bu üçlüye güveniyorum 👇"]))
            photo = express_card(legs, total, cfg, tz, now.strftime("%d.%m.%Y")) if use_images else None
            if tg_send(post_express(legs, total, intro, cfg, tz), dry, photo=photo):
                state["express"][today] = {"legs": legs, "total": total}
                log(f"Экспресс дня: {len(legs)} события, кф {total}")
        else:
            state["express"][today] = {"legs": [], "done": True}
            log("Экспресс сегодня не собрался")

    # 5. Прогноз раз в N часов
    last = state.get("last_post_at")
    since_h = (now - datetime.fromisoformat(last)).total_seconds() / 3600 if last else 999
    if args.force or (not quiet and since_h >= interval - 0.25):
        other_ids = set()
        if cfg.get("avoid_state_file"):  # не повторять матчи, которые уже разобрал другой канал
            try:
                other_ids = set(json.loads((ROOT / cfg["avoid_state_file"]).read_text(encoding="utf-8")).get("posted_ids", []))
            except Exception:  # noqa: BLE001
                pass
        prio = [t.lower() for t in cfg.get("priority_teams", [])]

        def is_prio(fx):
            n = (fx["teams"]["home"]["name"] + " " + fx["teams"]["away"]["name"]).lower()
            return any(t in n for t in prio)

        cands = [fx for fx in candidates(cfg["min_before_kickoff_min"], cfg.get("horizon_hours", 12))
                 if fx["fixture"]["id"] not in state["posted_ids"]
                 and (fx["fixture"]["id"] not in other_ids or is_prio(fx))]
        posted = False
        for fx in cands[: cfg.get("max_tries_per_tick", 4)]:
            fid = fx["fixture"]["id"]
            pred = api.prediction(fid)
            if not pred or not (pred.get("predictions") or {}).get("percent"):
                state["nodata_ids"].append(fid)
                log(f"Пропущен {fid}: нет данных для прогноза")
                continue
            odds = extract_odds(api.odds(fid))
            inj = api.injuries(fid) if cfg.get("use_injuries", True) else []
            pick = make_pick(fx, pred, odds, cfg, table_for(fx), inj)
            odd = odds.get((pick["market"], pick["selection"]))
            photo = match_card(fx, pick, odd, cfg, tz) if use_images else None
            if tg_send(post_prediction(fx, pick, odd, cfg, tz), dry, photo=photo):
                state["posted_ids"].append(fid)
                state["last_post_at"] = now.isoformat()
                state["pending"][str(fid)] = {
                    "day": today, "kickoff": fx["fixture"]["date"], "market": pick["market"],
                    "selection": pick["selection"], "home": fx["teams"]["home"]["name"],
                    "away": fx["teams"]["away"]["name"], "odd": odd, "source": pick["source"]}
                log(f"Опубликован прогноз {fid} ({pick['source']})")
                posted = True
            break
        if not posted:
            log("Прогноз в этот запуск не опубликован")

    save_state(state)
    log(f"Готово. Запросов к API-Football: {api.calls}")



# ================================================================ лайв
def pick_status(market, sel, gh, ga):
    """Как сейчас выглядит прогноз при текущем счёте (текст для лайв-поста)."""
    tot = gh + ga
    if market == "OU25":
        if sel == "Over":
            return "✅ Tuttu bile!" if tot >= 3 else f"⏳ {3 - tot} gol daha lazım"
        return "❌ Bu sefer olmadı" if tot >= 3 else "✅ Şimdilik yolunda"
    if market == "BTTS":
        both = gh > 0 and ga > 0
        if sel == "Yes":
            return "✅ Tuttu bile!" if both else "⏳ Diğer taraftan da bir gol lazım"
        return "❌ Bu sefer olmadı" if both else "✅ Şimdilik yolunda"
    res = "1" if gh > ga else "2" if ga > gh else "X"
    ok = (sel == res) if market == "1X2" else (res in sel)
    return "✅ Şimdilik lehimize" if ok else "⏳ Henüz değil, maç uzun"


def run_live(args):
    import subprocess
    import time
    cfg = load_config(args.config)
    tz = ZoneInfo(cfg["timezone"])
    live_file = Path(os.environ.get("LIVE_STATE_FILE", ROOT / "state_live.json"))
    api = Football(os.environ.get("API_FOOTBALL_KEY", ""), mock=args.mock)
    dry = args.dry_run
    lstate = json.loads(live_file.read_text(encoding="utf-8")) if live_file.exists() else {}
    lstate.setdefault("sent", [])
    lstate["sent"] = lstate["sent"][-2000:]
    sent = set(lstate["sent"])
    started = time.time()
    max_sec = float(cfg.get("live_max_minutes", 320)) * 60
    poll = int(cfg.get("live_poll_seconds", 60))
    last_pull = 0.0
    watch = {}  # fid -> pick

    def mark(key):
        sent.add(key)
        lstate["sent"].append(key)
        live_file.write_text(json.dumps(lstate, ensure_ascii=False), encoding="utf-8")

    def refresh_watch():
        st = load_state()
        now = datetime.now(tz)
        items = [(fid, p) for fid, p in st.get("pending", {}).items()]
        for d, ex in st.get("express", {}).items():
            for leg in ex.get("legs", []):
                items.append((str(leg["id"]), {**leg, "day": d}))
        for fid, p in items:
            mins = (parse_dt(p["kickoff"], tz) - now).total_seconds() / 60
            if -150 <= mins <= 75 and f"{fid}:FT" not in sent:
                watch.setdefault(fid, p)

    while time.time() - started < max_sec:
        if not args.mock and time.time() - last_pull > 600:
            subprocess.run(["git", "pull", "--rebase", "--autostash", "-q"], cwd=ROOT, check=False)
            last_pull = time.time()
        refresh_watch()
        now = datetime.now(tz)
        active = [fid for fid, p in watch.items() if parse_dt(p["kickoff"], tz) <= now + timedelta(minutes=2)]
        if not watch:
            log("Лайв: матчей для ведения нет, выхожу")
            break
        if not active:
            nxt = min(parse_dt(p["kickoff"], tz) for p in watch.values())
            wait = max(30, (nxt - now).total_seconds() - 90)
            log(f"Лайв: ближайший матч в {nxt:%H:%M}, жду {int(wait)} сек")
            time.sleep(min(wait, 600))
            continue
        for fx in api.fixtures_by_ids(active):
            fid = str(fx["fixture"]["id"])
            p = watch.get(fid)
            if not p:
                continue
            home, away = fx["teams"]["home"]["name"], fx["teams"]["away"]["name"]
            st = fx["fixture"]["status"]["short"]
            gh, ga = fx["goals"]["home"] or 0, fx["goals"]["away"] or 0
            label = MARKETS[p["market"]][p["selection"]]
            if st in ("1H", "2H", "HT", "ET", "P", "LIVE", "FT", "AET", "PEN") and f"{fid}:KO" not in sent:
                if tg_send(f"🦁 <b>Başladı!</b> {esc(home)} – {esc(away)}\n"
                           f"{esc(cfg.get('pick_label', 'Tercihimiz'))}: <b>{esc(label)}</b>. Takipteyiz 👀", dry):
                    mark(f"{fid}:KO")
            for ev in fx.get("events") or []:
                t = ev.get("type")
                minute = (ev.get("time") or {}).get("elapsed")
                extra = (ev.get("time") or {}).get("extra")
                mtxt = f"{minute}+{extra}'" if extra else f"{minute}'"
                player = (ev.get("player") or {}).get("name") or ""
                team = (ev.get("team") or {}).get("name") or ""
                key = f"{fid}:{t}:{minute}:{extra}:{team}:{player}:{ev.get('detail')}"
                if key in sent:
                    continue
                if t == "Goal" and ev.get("detail") != "Missed Penalty":
                    kind = {"Own Goal": " (kendi kalesine)", "Penalty": " (penaltı)"}.get(ev.get("detail"), "")
                    text = (f"⚽ <b>GOOOL!</b> {esc(team)} {mtxt}\n{esc(player)}{kind}\n\n"
                            f"<b>{esc(home)} {gh}-{ga} {esc(away)}</b>\n"
                            f"{esc(label)}: {pick_status(p['market'], p['selection'], gh, ga)}")
                    if tg_send(text, dry):
                        mark(key)
                elif t == "Card" and ev.get("detail") == "Red Card":
                    if tg_send(f"🟥 <b>Kırmızı kart!</b> {esc(player)} ({esc(team)}) {mtxt}\n"
                               f"{esc(home)} {gh}-{ga} {esc(away)}", dry):
                        mark(key)
                else:
                    mark(key)  # прочие события не публикуем, но запоминаем
            if st == "HT" and f"{fid}:HT" not in sent:
                if tg_send(f"⏸ <b>İlk yarı bitti</b>\n{esc(home)} {gh}-{ga} {esc(away)}\n"
                           f"{esc(label)}: {pick_status(p['market'], p['selection'], gh, ga)}", dry):
                    mark(f"{fid}:HT")
            if st in ("FT", "AET", "PEN", "PST", "CANC", "ABD", "AWD", "WO"):
                mark(f"{fid}:FT")  # итог публикует основной бот
                watch.pop(fid, None)
        if args.mock:
            break
        time.sleep(poll)
    live_file.write_text(json.dumps(lstate, ensure_ascii=False), encoding="utf-8")
    log(f"Лайв завершён. Запросов к API-Football: {api.calls}")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="печатать посты вместо отправки")
    ap.add_argument("--mock", action="store_true", help="брать данные из папки mock/")
    ap.add_argument("--force", action="store_true", help="опубликовать прогноз сейчас, не дожидаясь интервала")
    ap.add_argument("--config", default="config.yaml", help="файл настроек канала")
    ap.add_argument("--live", action="store_true", help="режим лайв: вести матчи с прогнозами канала")
    ap.add_argument("--now", help="подменить текущее время, формат 'YYYY-MM-DD HH:MM' (Стамбул)")
    a = ap.parse_args()
    run_live(a) if a.live else run(a)
