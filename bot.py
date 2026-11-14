#!/usr/bin/env python3
"""
Бот-контентщик для Telegram-канала с футбольной аналитикой (турецкая аудитория).

Один запуск = один "тик". Бот рассчитан на запуск раз в час по расписанию
(GitHub Actions cron). За тик он:
  1. раз в день загружает матчи на сегодня и выбирает лучшие;
  2. утром публикует анонс дня;
  3. за ~3 часа до матча публикует разбор и прогноз;
  4. после матча публикует результат прогноза;
  5. вечером публикует итог дня.
Состояние хранится в state.json (коммитится обратно в репозиторий).

Переменные окружения:
  TELEGRAM_BOT_TOKEN   токен бота от @BotFather (бот — админ канала)
  TELEGRAM_CHANNEL     @username канала или числовой id (-100...)
  API_FOOTBALL_KEY     ключ с dashboard.api-football.com
  LLM_API_KEY          ключ нейросети (необязательно; без него — шаблоны)

Запуск:
  python bot.py                 боевой режим
  python bot.py --dry-run       ничего не отправляет, печатает посты
  python bot.py --mock --dry-run --now "2026-11-15 17:10"   тест на мок-данных
"""
import argparse
import html
import json
import os
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

API_BASE = "https://v3.football.api-sports.io"

# ---------------------------------------------------------------- рынки
# Внутренние коды рынков → турецкие подписи
MARKETS = {
    "1X2": {"1": "MS 1", "X": "MS X", "2": "MS 2"},
    "DC": {"1X": "ÇŞ 1-X", "12": "ÇŞ 1-2", "X2": "ÇŞ X-2"},
    "OU25": {"Over": "2.5 Üst", "Under": "2.5 Alt"},
    "BTTS": {"Yes": "KG Var", "No": "KG Yok"},
}

FORBIDDEN_WORDS = ["garanti", "kesin kazanç", "%100", "100%", "şike", "bedava para", "risksiz"]


def log(*a):
    print("[bot]", *a, file=sys.stderr)


# ================================================================ утилиты
def load_config():
    with open(ROOT / "config.yaml", encoding="utf-8") as f:
        return yaml.safe_load(f)


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
        r = requests.get(
            f"{API_BASE}/{path}",
            params=params,
            headers={"x-apisports-key": self.key},
            timeout=30,
        )
        r.raise_for_status()
        data = r.json()
        if data.get("errors"):
            raise RuntimeError(f"API-Football error: {data['errors']}")
        return data.get("response", [])

    def fixtures_by_date(self, date_str, tz_name):
        return self._get("fixtures", {"date": date_str, "timezone": tz_name}, f"fixtures_{date_str}.json")

    def fixtures_by_ids(self, ids):
        ids = [str(i) for i in ids][:20]  # лимит API — 20 id за запрос
        return self._get("fixtures", {"ids": "-".join(ids)}, "fixtures_ids.json")

    def prediction(self, fid):
        res = self._get("predictions", {"fixture": fid}, f"predictions_{fid}.json")
        return res[0] if res else None

    def odds(self, fid):
        res = self._get("odds", {"fixture": fid}, f"odds_{fid}.json")
        return res[0] if res else None


# ================================================================ коэффициенты
def extract_odds(odds_resp):
    """Возвращает словарь {(market, selection): odd} по первому букмекеру, где есть рынок."""
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
def build_summary(fx, pred):
    home = fx["teams"]["home"]["name"]
    away = fx["teams"]["away"]["name"]
    lines = [f"Maç: {home} - {away}", f"Lig: {fx['league']['name']} ({fx['league'].get('round','')})"]
    if not pred:
        return "\n".join(lines)
    p = pred.get("predictions", {})
    lines.append(f"Kazanma olasılıkları: ev {p.get('percent',{}).get('home')}, beraberlik {p.get('percent',{}).get('draw')}, deplasman {p.get('percent',{}).get('away')}")
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
            f"son 5 maç gol ort. attığı {gf} / yediği {ga}; lig formu {lg.get('form','')[-6:]}"
        )
    comp = pred.get("comparison", {})
    if comp:
        lines.append("Karşılaştırma (ev/dep): " + ", ".join(
            f"{k} {v.get('home')}/{v.get('away')}" for k, v in comp.items() if isinstance(v, dict)))
    h2h = pred.get("h2h", [])[:5]
    if h2h:
        res = []
        for m in h2h:
            g = m.get("goals", {})
            res.append(f"{m['teams']['home']['name']} {g.get('home')}-{g.get('away')} {m['teams']['away']['name']}")
        lines.append("Son karşılaşmalar: " + "; ".join(res))
    return "\n".join(lines)


# ================================================================ нейросеть
LLM_SYSTEM = """Sen Türk futbol analiz kanalının yazarısın. Sadece verilen verilere dayanarak kısa, akıcı ve samimi bir maç analizi yazıyorsun.
Kurallar:
- Yalnızca Türkçe yaz.
- Verilerde olmayan sakatlık, kadro, haber veya istatistik UYDURMA.
- "garanti", "kesin", "%100", "risksiz", "şike" gibi ifadeler KULLANMA. Tahmin bir görüştür.
- Analiz 3-5 cümle olsun, emoji en fazla 2 tane.
- Tek bir tahmin seç. İzinli pazarlar ve seçimler:
  1X2: "1", "X", "2"
  DC: "1X", "12", "X2"
  OU25: "Over", "Under"
  BTTS: "Yes", "No"
- Oranlar verilmişse min_odd ile max_odd arasındaki seçimleri tercih et.
- Güven 1-5 arası bir tam sayı.
Cevabın SADECE şu JSON olsun, başka hiçbir şey yazma:
{"market": "...", "selection": "...", "confidence": 3, "analysis": "..."}"""


def llm_call(provider, model, key, system, user):
    if provider == "gemini":
        model = model or "gemini-flash-latest"
        r = requests.post(
            f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent",
            params={"key": key},
            json={
                "systemInstruction": {"parts": [{"text": system}]},
                "contents": [{"role": "user", "parts": [{"text": user}]}],
                "generationConfig": {"temperature": 0.7, "responseMimeType": "application/json"},
            },
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["candidates"][0]["content"]["parts"][0]["text"]
    if provider == "anthropic":
        model = model or "claude-haiku-5-5"
        r = requests.post(
            "https://api.anthropic.com/v1/messages",
            headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": model, "max_tokens": 800, "system": system,
                  "messages": [{"role": "user", "content": user}]},
            timeout=60,
        )
        r.raise_for_status()
        return "".join(b.get("text", "") for b in r.json()["content"])
    if provider == "openai":
        model = model or "gpt-4o-mini"
        r = requests.post(
            "https://api.openai.com/v1/chat/completions",
            headers={"Authorization": f"Bearer {key}"},
            json={"model": model, "temperature": 0.7, "response_format": {"type": "json_object"},
                  "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()["choices"][0]["message"]["content"]
    raise ValueError(f"Неизвестный провайдер: {provider}")


def parse_llm_json(text):
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        raise ValueError("нет JSON в ответе")
    d = json.loads(m.group(0))
    market, sel = d.get("market"), d.get("selection")
    if market not in MARKETS or sel not in MARKETS[market]:
        raise ValueError(f"недопустимый выбор {market}/{sel}")
    analysis = str(d.get("analysis", "")).strip()
    low = analysis.lower()
    for w in FORBIDDEN_WORDS:
        if w in low:
            raise ValueError(f"запрещённое слово в анализе: {w}")
    conf = int(d.get("confidence", 3))
    return {"market": market, "selection": sel, "confidence": max(1, min(5, conf)), "analysis": analysis}


# ================================================================ шаблонный прогноз (без нейросети)
def template_pick(fx, pred, odds, cfg):
    home = fx["teams"]["home"]["name"]
    away = fx["teams"]["away"]["name"]
    p = (pred or {}).get("predictions", {})
    pct = {k: int(str(v).rstrip("%") or 0) for k, v in (p.get("percent") or {}).items()} if p.get("percent") else {}
    h, d, a = pct.get("home", 0), pct.get("draw", 0), pct.get("away", 0)

    candidates = []
    if h >= 50:
        candidates.append(("1X2", "1"))
    if a >= 50:
        candidates.append(("1X2", "2"))
    candidates.append(("DC", "1X" if h >= a else "X2"))
    uo = str(p.get("under_over") or "")
    if uo.startswith("+") and float(uo[1:] or 0) >= 2.5:
        candidates.append(("OU25", "Over"))
    elif uo.startswith("-") and float(uo[1:] or 0) <= 2.5:
        candidates.append(("OU25", "Under"))

    chosen = None
    for c in candidates:
        o = odds.get(c)
        if o is None or cfg["min_odd"] <= o <= cfg["max_odd"]:
            chosen = c
            break
    chosen = chosen or candidates[0]

    t = (pred or {}).get("teams", {})
    th, ta = t.get("home", {}), t.get("away", {})
    hf = th.get("last_5", {}).get("form", "-")
    af = ta.get("last_5", {}).get("form", "-")
    hg = th.get("last_5", {}).get("goals", {})
    ag = ta.get("last_5", {}).get("goals", {})
    fav = home if h >= a else away
    import random
    rnd = random.Random(fx["fixture"]["id"])
    intro = rnd.choice([
        f"{home} son 5 maçta %{str(hf).rstrip('%')} form yakaladı, {away} tarafında bu oran %{str(af).rstrip('%')}.",
        f"Son haftalarda {home} %{str(hf).rstrip('%')}, {away} ise %{str(af).rstrip('%')} form performansı gösterdi.",
    ])
    goals = (f" {home} son 5 maçta maç başı {hg.get('for',{}).get('average','-')} gol atıp "
             f"{hg.get('against',{}).get('average','-')} gol yedi; {away} için bu sayılar "
             f"{ag.get('for',{}).get('average','-')} / {ag.get('against',{}).get('average','-')}.")
    outro = rnd.choice([
        f" İstatistik modeli ev %{h}, beraberlik %{d}, deplasman %{a} olasılık veriyor; terazi {fav} tarafına eğiliyor.",
        f" Olasılık dağılımı %{h} / %{d} / %{a} şeklinde ve veriler {fav} tarafını bir adım önde gösteriyor.",
    ])
    analysis = intro + goals + outro
    conf = 4 if max(h, a) >= 55 else 3
    return {"market": chosen[0], "selection": chosen[1], "confidence": conf, "analysis": analysis}


def make_pick(fx, pred, odds, cfg, dry=False):
    provider = (cfg.get("llm_provider") or "none").lower()
    key = os.environ.get("LLM_API_KEY", "")
    if provider != "none" and key:
        summary = build_summary(fx, pred)
        odds_txt = ", ".join(f"{m}:{s}={o}" for (m, s), o in sorted(odds.items())) or "yok"
        user = f"{summary}\nOranlar: {odds_txt}\nmin_odd={cfg['min_odd']} max_odd={cfg['max_odd']}"
        for attempt in range(2):
            try:
                raw = llm_call(provider, cfg.get("llm_model") or "", key, LLM_SYSTEM, user)
                pick = parse_llm_json(raw)
                pick["source"] = provider
                return pick
            except Exception as e:  # noqa: BLE001
                log(f"LLM попытка {attempt+1} не удалась: {e}")
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


# ================================================================ тексты постов
def stars(n):
    return "⭐" * n + "☆" * (5 - n)


def post_prediction(fx, pick, odd, cfg, tz):
    home, away = fx["teams"]["home"]["name"], fx["teams"]["away"]["name"]
    kt = parse_dt(fx["fixture"]["date"], tz).strftime("%H:%M")
    label = MARKETS[pick["market"]][pick["selection"]]
    odd_line = f"\n💰 Oran: <b>{odd:.2f}</b>" if odd else ""
    return (
        f"⚽ <b>{esc(home)} – {esc(away)}</b>\n"
        f"🏆 {esc(fx['league']['name'])} | 🕗 {kt}\n\n"
        f"📊 <b>Analiz</b>\n{esc(pick['analysis'])}\n\n"
        f"🎯 Tahmin: <b>{esc(label)}</b>{odd_line}\n"
        f"📈 Güven: {stars(pick['confidence'])}\n\n"
        f"<i>{esc(cfg['disclaimer'])}</i>"
    )


def post_digest(selected, cfg, tz, date_label):
    lines = [f"📅 <b>Günün maçları — {date_label}</b>", ""]
    for fx in selected:
        kt = parse_dt(fx["fixture"]["date"], tz).strftime("%H:%M")
        lines.append(f"🕗 {kt} | {esc(fx['teams']['home']['name'])} – {esc(fx['teams']['away']['name'])} "
                     f"<i>({esc(fx['league']['name'])})</i>")
    lines += ["", "🔎 Her maçın analizi başlamadan yaklaşık 3 saat önce kanalda! 🔔",
              "", f"<i>{esc(cfg['disclaimer'])}</i>"]
    return "\n".join(lines)


def post_result(p, gh, ga, won):
    mark = "✅" if won else "❌"
    label = MARKETS[p["market"]][p["selection"]]
    return (f"{mark} <b>{esc(p['home'])} {gh}-{ga} {esc(p['away'])}</b>\n"
            f"Tahmin: {esc(label)} — {'TUTTU' if won else 'tutmadı'}")


def post_recap(day, cfg):
    w, l = day.get("won", 0), day.get("lost", 0)
    if w + l == 0:
        return None
    tot = cfg.get("_totals", {})
    tw, tl = tot.get("won", 0), tot.get("lost", 0)
    rate = round(100 * tw / (tw + tl)) if tw + tl else 0
    return (f"📊 <b>Dünün özeti</b>\n\n✅ {w}  ❌ {l}\n\n"
            f"Genel: {tw} doğru / {tl} yanlış (%{rate})\n\nBugünün analizleri birazdan kanalda! 👋")


# ================================================================ Telegram
def tg_send(text, dry):
    if dry:
        print("\n" + "=" * 60 + "\n" + text + "\n" + "=" * 60)
        return True
    token = os.environ["TELEGRAM_BOT_TOKEN"]
    chat = os.environ["TELEGRAM_CHANNEL"]
    r = requests.post(
        f"https://api.telegram.org/bot{token}/sendMessage",
        json={"chat_id": chat, "text": text, "parse_mode": "HTML", "disable_web_page_preview": True},
        timeout=30,
    )
    if not r.ok:
        log("Telegram error:", r.status_code, r.text[:300])
        return False
    return True


# ================================================================ выбор матчей
def select_fixtures(fixtures, cfg):
    order = {l["id"]: i for i, l in enumerate(cfg["leagues"])}
    prio_teams = [t.lower() for t in cfg.get("priority_teams", [])]
    pool = []
    for fx in fixtures:
        lid = fx["league"]["id"]
        if lid not in order or fx["fixture"]["status"]["short"] != "NS":
            continue
        names = (fx["teams"]["home"]["name"] + " " + fx["teams"]["away"]["name"]).lower()
        bonus = -100 if any(t in names for t in prio_teams) else 0
        pool.append((order[lid] + bonus, fx["fixture"]["timestamp"], fx))
    pool.sort(key=lambda x: (x[0], x[1]))
    return [p[2] for p in pool]


# ================================================================ основной тик
def run(args):
    cfg = load_config()
    tz = ZoneInfo(cfg["timezone"])
    now = datetime.strptime(args.now, "%Y-%m-%d %H:%M").replace(tzinfo=tz) if args.now else datetime.now(tz)
    today = now.strftime("%Y-%m-%d")
    dry = args.dry_run

    api = Football(os.environ.get("API_FOOTBALL_KEY", ""), mock=args.mock)
    if not args.mock and not api.key:
        sys.exit("Не задан API_FOOTBALL_KEY")
    if not dry and not (os.environ.get("TELEGRAM_BOT_TOKEN") and os.environ.get("TELEGRAM_CHANNEL")):
        sys.exit("Не заданы TELEGRAM_BOT_TOKEN / TELEGRAM_CHANNEL")

    state = load_state()
    state.setdefault("totals", {"won": 0, "lost": 0})
    state.setdefault("pending", {})          # fixture_id -> pick, ждут результата
    days = state.setdefault("days", {})
    day = days.setdefault(today, {"selected": [], "posted": [], "digest": False, "recap": False, "won": 0, "lost": 0})
    # чистим старые дни (храним 7)
    for d in sorted(days)[:-7]:
        days.pop(d, None)

    # 1. Матчи дня (один запрос в день)
    if not day["selected"] and not day.get("loaded"):
        fixtures = api.fixtures_by_date(today, cfg["timezone"])
        chosen = []
        for fx in select_fixtures(fixtures, cfg):
            if len(chosen) >= cfg["max_predictions_per_day"]:
                break
            pred = api.prediction(fx["fixture"]["id"])
            if not pred or not (pred.get("predictions") or {}).get("percent"):
                continue  # нет статистики — такой матч не берём
            chosen.append({"id": fx["fixture"]["id"], "date": fx["fixture"]["date"], "fx": fx, "pred": pred})
        chosen.sort(key=lambda c: c["fx"]["fixture"]["timestamp"])
        day["selected"] = chosen
        log(f"Выбрано матчей: {len(chosen)}")
        day["loaded"] = True

    # 3. Прогнозы перед матчами
    for s in day["selected"]:
        fid = s["id"]
        if fid in day["posted"]:
            continue
        ko = parse_dt(s["date"], tz)
        mins = (ko - now).total_seconds() / 60
        if mins < cfg["min_before_kickoff_min"]:
            day["posted"].append(fid)  # опоздали — пропускаем
            log(f"Пропущен {fid}: до старта {mins:.0f} мин")
            continue
        if mins > cfg["post_before_kickoff_min"]:
            continue
        fx = s["fx"]
        pred = s.get("pred") or api.prediction(fid)
        if not pred or not (pred.get("predictions") or {}).get("percent"):
            day["posted"].append(fid)  # без статистики не постим, чтобы не выдумывать
            log(f"Пропущен {fid}: нет данных для прогноза")
            continue
        odds = extract_odds(api.odds(fid))
        pick = make_pick(fx, pred, odds, cfg)
        odd = odds.get((pick["market"], pick["selection"]))
        if tg_send(post_prediction(fx, pick, odd, cfg, tz), dry):
            day["posted"].append(fid)
            state["pending"][str(fid)] = {
                "day": today, "kickoff": s["date"], "market": pick["market"], "selection": pick["selection"],
                "home": fx["teams"]["home"]["name"], "away": fx["teams"]["away"]["name"], "odd": odd,
                "source": pick["source"],
            }

    # 4. Результаты
    due = [fid for fid, p in state["pending"].items()
           if (now - parse_dt(p["kickoff"], tz)).total_seconds() / 60 >= cfg["results_check_after_min"]]
    if due:
        for fx in api.fixtures_by_ids(due):
            fid = str(fx["fixture"]["id"])
            st = fx["fixture"]["status"]["short"]
            p = state["pending"].get(fid)
            if not p or fid not in due:
                continue
            if st in ("PST", "CANC", "ABD", "AWD", "WO"):
                state["pending"].pop(fid)
                continue
            if st not in ("FT", "AET", "PEN"):
                continue
            # для оценки берём счёт основного времени
            ft = fx.get("score", {}).get("fulltime", {})
            gh, ga = ft.get("home"), ft.get("away")
            if gh is None:
                gh, ga = fx["goals"]["home"], fx["goals"]["away"]
            won = evaluate(p["market"], p["selection"], gh, ga)
            if won is None:
                continue
            if tg_send(post_result(p, gh, ga, won), dry):
                key = "won" if won else "lost"
                state["totals"][key] += 1
                days.setdefault(p["day"], {"won": 0, "lost": 0}).setdefault(key, 0)
                days[p["day"]][key] += 1
                state["pending"].pop(fid)

    # 5. Итог вчерашнего дня — утром, перед анонсом
    yday = (now - timedelta(days=1)).strftime("%Y-%m-%d")
    yd = days.get(yday)
    if yd and not yd.get("recap") and now.hour >= cfg["digest_hour"] - 1:
        if not [p for p in state["pending"].values() if p["day"] == yday]:
            cfg["_totals"] = state["totals"]
            text = post_recap(yd, cfg)
            if text is None or tg_send(text, dry):
                yd["recap"] = True

    # 6. Утренний анонс
    if not day["digest"] and now.hour >= cfg["digest_hour"] and day["selected"]:
        upcoming = [s["fx"] for s in day["selected"] if parse_dt(s["date"], tz) > now]
        if upcoming and tg_send(post_digest(upcoming, cfg, tz, now.strftime("%d.%m")), dry):
            day["digest"] = True

    save_state(state)
    log(f"Готово. Запросов к API-Football: {api.calls}")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="печатать посты вместо отправки")
    ap.add_argument("--mock", action="store_true", help="брать данные из папки mock/")
    ap.add_argument("--now", help="подменить текущее время, формат 'YYYY-MM-DD HH:MM' (Стамбул)")
    run(ap.parse_args())
