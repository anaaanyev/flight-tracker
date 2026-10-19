#!/usr/bin/env python3
"""Трекер дешёвых авиабилетов (один или несколько маршрутов).

Запускается GitHub Actions каждые 15 минут:
  * каждый запуск разбирает команды из Telegram (/add, /set, /status, ...);
  * сами цены проверяются 1 раз в сутки (около 09:00 по Москве) или по команде /check.
Уведомление приходит только если цена по маршруту упала ниже сохранённого минимума
больше чем на порог (по умолчанию 1000 руб).
Раз в месяц приходит короткий отчёт, чтобы было видно, что бот жив.
state.json меняется (и коммитится) только при реальных событиях: новая цена, смена
маршрута/порога, ежемесячный отчёт, редкий служебный «пульс».
"""
import copy
import json
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ----------------------------- настройки ---------------------------------
WINDOW_DAYS = (15, 45)        # окно вылета: от сегодня+15 до сегодня+45
MAX_TRANSFERS = 1             # прямой рейс или 1 пересадка
HTTP_RETRIES = 3              # повторы запроса к Aviasales при сбое сети/сервера
MAX_ROUTES = 8               # максимум маршрутов одновременно
HEARTBEAT_DAYS = 25           # раз в ~25 дней коммит, чтобы GitHub не отключил cron
DEFAULT_ROUTE = {
    "origin": "MOW", "dest": "BKK",
    "min_price": None, "min_info": None,              # самый дешёвый (до MAX_TRANSFERS пересадок)
    "direct_min_price": None, "direct_min_info": None,  # самый дешёвый прямой рейс
}
DEFAULTS = {
    "routes": [DEFAULT_ROUTE],
    "threshold": 1000,        # на сколько рублей цена должна упасть (/threshold)
    "last_heartbeat": None,
    "last_report_month": None,
}
OBSOLETE_KEYS = ("last_slot", "last_check", "last_error_date", "tg_offset")  # старые поля
# -------------------------------------------------------------------------

MSK = timezone(timedelta(hours=3))
STATE_FILE = Path(__file__).with_name("state.json")

TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT = str(os.environ["TG_CHAT_ID"])
TP_TOKEN = os.environ["TP_TOKEN"]
FORCE_ENV = os.environ.get("FORCE_CHECK") == "1"   # ручной запуск из GitHub: ответить подробно
DAILY_ENV = os.environ.get("DAILY_CHECK") == "1"   # ежедневный запуск по расписанию

IATA = re.compile(r"[A-Za-z]{3}")

HELP = (
    "Команды:\n"
    "/set MOW BKK — следить за одним маршрутом (все остальные удаляются)\n"
    "/add LED HKT — добавить ещё один маршрут (откуда, куда)\n"
    "/remove LED HKT — убрать маршрут\n"
    "/routes — список маршрутов\n"
    "/threshold 1500 — порог падения цены в рублях\n"
    "/status — настройки и минимальные цены\n"
    "/check — проверить цены прямо сейчас\n"
    "Коды — IATA города или аэропорта (MOW, LED, KZN, BKK, HKT, DXB…).\n"
    "Команды обрабатываются раз в ~15 минут."
)


# ----------------------------- состояние ---------------------------------
def load_state():
    state = copy.deepcopy(DEFAULTS)
    if STATE_FILE.exists():
        loaded = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        # миграция со старого формата (один маршрут: origin/min_price в корне)
        if "routes" not in loaded and "origin" in loaded:
            loaded["routes"] = [{
                "origin": loaded.pop("origin"),
                "dest": "BKK",
                "min_price": loaded.pop("min_price", None),
                "min_info": loaded.pop("min_info", None),
            }]
        loaded.pop("origin", None)
        loaded.pop("min_price", None)
        loaded.pop("min_info", None)
        state.update(loaded)
    for k in OBSOLETE_KEYS:  # эти поля больше не хранятся: так меньше коммитов
        state.pop(k, None)
    for r in state["routes"]:  # миграция: поля для прямых рейсов
        r.setdefault("direct_min_price", None)
        r.setdefault("direct_min_info", None)
    return state


def save_state(state):
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def find_route(state, origin, dest):
    for r in state["routes"]:
        if r["origin"] == origin and r["dest"] == dest:
            return r
    return None


def new_route(origin, dest):
    return {
        "origin": origin, "dest": dest,
        "min_price": None, "min_info": None,
        "direct_min_price": None, "direct_min_info": None,
    }


# ------------------------------ Telegram ---------------------------------
def tg(method, **params):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params, timeout=20
    )
    if not r.ok:  # показываем причину от Telegram (и не выводим токен из адреса)
        try:
            desc = r.json().get("description", "")
        except ValueError:
            desc = ""
        raise RuntimeError(f"Telegram {method}: HTTP {r.status_code} {desc}")
    return r.json()["result"]


def say(text):
    tg("sendMessage", chat_id=TG_CHAT, text=text, disable_web_page_preview=True)


def rub(n):
    return f"{int(n):,}".replace(",", " ") + " ₽"


def parse_route_args(args):
    """['mow', 'bkk'] -> ('MOW', 'BKK') или None, если формат неверный."""
    if len(args) == 2 and all(IATA.fullmatch(a) for a in args):
        o, d = args[0].upper(), args[1].upper()
        if o != d:
            return o, d
    return None


def routes_text(state):
    lines = []
    for r in state["routes"]:
        mp, dp = r["min_price"], r["direct_min_price"]
        if mp:
            text = (f"минимум {rub(mp)}, прямой "
                    + (rub(dp) if dp else "—"))
        else:
            text = "минимум ещё не зафиксирован"
        lines.append(f"• {r['origin']} → {r['dest']}: {text}")
    return "\n".join(lines) if lines else "маршрутов нет, добавьте: /add MOW BKK"


def process_commands(state):
    """Читает команды из Telegram. Возвращает True, если нужна внеплановая проверка."""
    force = False
    updates = tg("getUpdates", timeout=0, allowed_updates=["message"])
    if updates:
        # подтверждаем получение сразу: Telegram удалит эти сообщения и не отдаст их снова,
        # поэтому номер последнего сообщения хранить в state.json не нужно
        tg("getUpdates", offset=updates[-1]["update_id"] + 1, limit=1, timeout=0)
    for u in updates:
        msg = u.get("message") or {}
        if str(msg.get("chat", {}).get("id")) != TG_CHAT:
            continue  # игнорируем чужих
        text = (msg.get("text") or "").strip()
        if not text.startswith("/"):
            continue
        cmd, *args = text.split()
        cmd = cmd.split("@")[0].lower()

        if cmd in ("/start", "/help"):
            say(HELP)

        elif cmd in ("/set", "/add", "/remove"):
            pair = parse_route_args(args)
            if not pair:
                say(f"Формат: {cmd} MOW BKK (код «откуда» и код «куда», три латинские буквы)")
                continue
            origin, dest = pair
            if cmd == "/set":
                state["routes"] = [new_route(origin, dest)]
                force = True
                say(f"Теперь слежу только за {origin} → {dest}. Делаю первую проверку.")
            elif cmd == "/add":
                if find_route(state, origin, dest):
                    say(f"{origin} → {dest} уже отслеживается.")
                elif len(state["routes"]) >= MAX_ROUTES:
                    say(f"Достигнут лимит: {MAX_ROUTES} маршрутов. Уберите лишний: /remove")
                else:
                    state["routes"].append(new_route(origin, dest))
                    force = True
                    say(f"Добавлен маршрут {origin} → {dest}. Делаю первую проверку.")
            else:  # /remove
                r = find_route(state, origin, dest)
                if r:
                    state["routes"].remove(r)
                    say(f"Маршрут {origin} → {dest} удалён.\n{routes_text(state)}")
                else:
                    say(f"Маршрута {origin} → {dest} нет в списке.\n{routes_text(state)}")

        elif cmd == "/routes":
            say("Маршруты:\n" + routes_text(state))

        elif cmd == "/threshold":
            if len(args) == 1 and args[0].isdigit():
                state["threshold"] = int(args[0])
                say(f"Порог падения цены: {rub(state['threshold'])}")
            else:
                say("Формат: /threshold 1000")

        elif cmd == "/status":
            say(
                "В одну сторону, 1 взр., эконом\n"
                f"Окно вылета: +{WINDOW_DAYS[0]}…+{WINDOW_DAYS[1]} дней, "
                f"пересадок не более {MAX_TRANSFERS}\n"
                f"Порог: {rub(state['threshold'])}\n"
                f"Маршруты:\n{routes_text(state)}\n"
                "Цены проверяются раз в сутки около 09:00 МСК или по /check."
            )

        elif cmd == "/check":
            force = True
            say("Проверяю цены…")

        else:
            say(HELP)
    return force


# ------------------------------ Aviasales --------------------------------
def http_get(url, **kwargs):
    """GET с повторами: разовый сбой Aviasales не должен стоить пропущенной проверки."""
    for attempt in range(1, HTTP_RETRIES + 1):
        try:
            r = requests.get(url, **kwargs)
            r.raise_for_status()
            return r
        except requests.RequestException:
            if attempt == HTTP_RETRIES:
                raise
            time.sleep(10 * attempt)


def months_in_window(start, end):
    m = date(start.year, start.month, 1)
    out = []
    while m <= end:
        out.append(m.strftime("%Y-%m"))
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return out


def fetch_best(origin, dest, start, end):
    """Два самых дешёвых варианта в окне [start, end]:
    (до MAX_TRANSFERS пересадок включительно, только прямой рейс). Любой может быть None."""
    best, direct = None, None
    for month in months_in_window(start, end):
        # API отдаёт один (самый дешёвый) билет на дату, поэтому прямые рейсы,
        # которые дороже рейса с пересадкой на ту же дату, прячутся.
        # Прямые запрашиваем отдельно (direct=true).
        for only_direct in (False, True):
            r = http_get(
                "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
                params={
                    "origin": origin,
                    "destination": dest,
                    "departure_at": month,
                    "one_way": "true",
                    "direct": "true" if only_direct else "false",
                    "currency": "rub",
                    "market": "ru",
                    "sorting": "price",
                    "limit": 1000,
                },
                headers={"X-Access-Token": TP_TOKEN},
                timeout=30,
            )
            r.raise_for_status()
            payload = r.json()
            if not payload.get("success", True):
                raise RuntimeError(f"Aviasales API: {payload.get('error')}")
            for o in payload.get("data", []):
                dep = date.fromisoformat(o["departure_at"][:10])
                if not (start <= dep <= end):
                    continue
                if o.get("transfers", 0) > MAX_TRANSFERS:
                    continue
                offer = {
                    "price": int(o["price"]),
                    "date": dep.isoformat(),
                    "airline": o.get("airline", "?"),
                    "transfers": o.get("transfers", 0),
                }
                if best is None or offer["price"] < best["price"]:
                    best = offer
                if offer["transfers"] == 0 and (direct is None or offer["price"] < direct["price"]):
                    direct = offer
    return best, direct


def search_link(origin, dest, iso_date):
    """Короткая ссылка на поиск Aviasales: MOW1111BKK1 = Москва, 11.11, Бангкок, 1 пассажир."""
    d = date.fromisoformat(iso_date)
    return f"https://www.aviasales.ru/search/{origin}{d:%d%m}{dest}1"


def offer_text(route, label, offer, was=None):
    d = date.fromisoformat(offer["date"])
    line = f"{label}: {rub(offer['price'])}, вылет {d:%d.%m}, {offer['airline']}"
    if offer["transfers"]:
        line += f", пересадок: {offer['transfers']}"
    if was is not None:
        line += f" (было {rub(was)}, −{rub(was - offer['price'])})"
    return line + "\n" + search_link(route["origin"], route["dest"], offer["date"])


def offers_text(route, any_best, direct_best, was_any=None, was_direct=None):
    """Блок «две цены»: самый дешёвый и самый дешёвый прямой (одним, если это один и тот же рейс)."""
    if direct_best and (direct_best["price"], direct_best["date"]) == (any_best["price"], any_best["date"]):
        return offer_text(route, "Самый дешёвый (прямой рейс)", any_best, was_any or was_direct)
    parts = [offer_text(route, f"Самый дешёвый (до {MAX_TRANSFERS} пересадки)", any_best, was_any)]
    if direct_best:
        parts.append(offer_text(route, "Самый дешёвый прямой", direct_best, was_direct))
    else:
        parts.append("Прямых рейсов в данных Aviasales на эти даты не нашлось")
    return "\n".join(parts)


def check_route(state, route, today, verbose):
    """Проверяет один маршрут. Возвращает лучшую найденную цену (или None)."""
    start = today + timedelta(days=WINDOW_DAYS[0])
    end = today + timedelta(days=WINDOW_DAYS[1])
    name = f"{route['origin']} → {route['dest']}"
    best, direct = fetch_best(route["origin"], route["dest"], start, end)

    if best is None:
        print(f"{name}: нет предложений в окне")
        if verbose:
            say(f"{name}: предложений в окне вылета не найдено. Проверьте коды городов.")
        return None

    thr = state["threshold"]
    old_any, old_dir = route["min_price"], route["direct_min_price"]
    # событие по каждой из двух цен: первая фиксация или падение больше порога
    any_event = old_any is None or old_any - best["price"] > thr
    dir_event = direct is not None and (old_dir is None or old_dir - direct["price"] > thr)
    any_drop = old_any is not None and any_event
    dir_drop = old_dir is not None and dir_event

    if any_event or dir_event:
        if any_drop or dir_drop:
            title = "🔥 Цена упала"
        elif old_any is None:
            title = "👀 Слежение запущено (базовые цены)"
        else:
            title = "👀 Появился прямой рейс (базовая цена)"
        text = f"{title}\n{name}\n" + offers_text(
            route, best, direct,
            was_any=old_any if any_drop else None,
            was_direct=old_dir if dir_drop else None,
        )
        if any_drop or dir_drop:
            text += "\nЦены из кэша Aviasales — проверьте на сайте перед покупкой."
        # минимумы записываются только после успешной отправки в Telegram:
        # если сообщение не дошло, при следующей проверке оно будет отправлено снова
        say(text)
        if any_event:
            route["min_price"], route["min_info"] = best["price"], best
        if dir_event:
            route["direct_min_price"], route["direct_min_info"] = direct["price"], direct
    else:
        print(f"{name}: {best['price']} (минимум {old_any}), прямой "
              f"{direct['price'] if direct else '—'} (минимум {old_dir}) — молчим")
        if verbose:
            say(f"{name}: снижения нет.\n" + offers_text(route, best, direct))
    return {"any": best, "direct": direct}


def run_checks(state, now, verbose):
    """Проверяет все маршруты. Ошибка одного не мешает остальным.

    Возвращает (лучшие цены по маршрутам, список ошибок)."""
    bests, errors = {}, []
    for route in state["routes"]:
        try:
            bests[(route["origin"], route["dest"])] = check_route(
                state, route, now.date(), verbose
            )
        except Exception as e:  # noqa: BLE001
            errors.append(f"{route['origin']}→{route['dest']}: {e}")
    return bests, errors


# ------------------------------ отчёты -----------------------------------
def monthly_report(state, now, bests):
    month = now.strftime("%Y-%m")
    if state["last_report_month"] is None:
        state["last_report_month"] = month  # первый запуск: отчёт не нужен
        return
    if state["last_report_month"] == month:
        return
    lines = []
    for r in state["routes"]:
        cur = bests.get((r["origin"], r["dest"]))
        mp, dp = r["min_price"], r["direct_min_price"]
        now_any = rub(cur["any"]["price"]) if cur else "нет данных"
        now_dir = rub(cur["direct"]["price"]) if cur and cur["direct"] else "—"
        lines.append(
            f"• {r['origin']} → {r['dest']}: минимум {rub(mp) if mp else '—'}, сейчас {now_any}; "
            f"прямой: минимум {rub(dp) if dp else '—'}, сейчас {now_dir}"
        )
    say("📊 Ежемесячный отчёт: бот работает.\n" + "\n".join(lines))
    state["last_report_month"] = month


# -------------------------------- main -----------------------------------
def report_error(err):
    """Ошибка проверки цен (бывает раз в сутки или по ручному запуску, так что не спамит)."""
    print("ERROR:", err)
    try:
        say(f"⚠️ Ошибка трекера: {err}")
    except Exception as e:  # noqa: BLE001
        print("Не удалось отправить ошибку в Telegram:", e)


def main():
    now = datetime.now(MSK)
    state = load_state()

    cmd_force = False
    try:
        cmd_force = process_commands(state)
    except Exception as e:  # noqa: BLE001
        # запуск идёт каждые 15 минут, поэтому в Telegram об этом не пишем, только в лог
        print("ERROR: Telegram:", e)

    manual = cmd_force or FORCE_ENV  # ручная проверка: отвечаем по каждому маршруту
    if manual or DAILY_ENV:
        try:
            bests, errors = run_checks(state, now, verbose=manual)
            if errors:
                report_error("; ".join(errors))
            else:
                monthly_report(state, now, bests)
        except Exception as e:  # noqa: BLE001
            report_error(e)

    # «пульс», чтобы GitHub не отключил расписание в неактивном репозитории
    hb = state["last_heartbeat"]
    if not hb or (now.date() - date.fromisoformat(hb)).days >= HEARTBEAT_DAYS:
        state["last_heartbeat"] = now.date().isoformat()

    save_state(state)


if __name__ == "__main__":
    main()
