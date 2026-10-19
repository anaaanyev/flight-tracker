#!/usr/bin/env python3
"""Трекер дешёвых авиабилетов (один или несколько маршрутов).

Запускается GitHub Actions каждые 15 минут:
  * каждый запуск разбирает команды из Telegram (/add, /set, /status, ...);
  * сами цены проверяются 2 раза в день (09:00 и 19:00 по Москве) или по команде /check.
Уведомление приходит только если цена по маршруту упала ниже сохранённого минимума
больше чем на порог (по умолчанию 1000 руб).
Раз в месяц приходит короткий отчёт, чтобы было видно, что бот жив.
"""
import copy
import json
import os
import re
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

# ----------------------------- настройки ---------------------------------
WINDOW_DAYS = (15, 45)        # окно вылета: от сегодня+15 до сегодня+45
MAX_TRANSFERS = 1             # прямой рейс или 1 пересадка
SLOT_HOURS = (9, 19)          # время плановых проверок по Москве
MAX_ROUTES = 8                # максимум маршрутов одновременно
HEARTBEAT_DAYS = 25           # раз в ~25 дней коммит, чтобы GitHub не отключил cron
DEFAULT_ROUTE = {"origin": "MOW", "dest": "BKK", "min_price": None, "min_info": None}
DEFAULTS = {
    "routes": [DEFAULT_ROUTE],
    "threshold": 1000,        # на сколько рублей цена должна упасть (/threshold)
    "last_slot": None,
    "last_check": None,
    "last_error_date": None,
    "last_heartbeat": None,
    "last_report_month": None,
    "tg_offset": 0,
}
# -------------------------------------------------------------------------

MSK = timezone(timedelta(hours=3))
STATE_FILE = Path(__file__).with_name("state.json")

TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT = str(os.environ["TG_CHAT_ID"])
TP_TOKEN = os.environ["TP_TOKEN"]
FORCE_ENV = os.environ.get("FORCE_CHECK") == "1"

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
    return {"origin": origin, "dest": dest, "min_price": None, "min_info": None}


# ------------------------------ Telegram ---------------------------------
def tg(method, **params):
    r = requests.post(
        f"https://api.telegram.org/bot{TG_TOKEN}/{method}", json=params, timeout=20
    )
    r.raise_for_status()
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
        mp = r["min_price"]
        info = r["min_info"] or {}
        lines.append(
            f"• {r['origin']} → {r['dest']}: "
            + (f"минимум {rub(mp)}, вылет {info.get('date')}" if mp else "минимум ещё не зафиксирован")
        )
    return "\n".join(lines) if lines else "маршрутов нет, добавьте: /add MOW BKK"


def process_commands(state):
    """Читает команды из Telegram. Возвращает True, если нужна внеплановая проверка."""
    force = False
    updates = tg(
        "getUpdates",
        offset=state["tg_offset"],
        timeout=0,
        allowed_updates=["message"],
    )
    for u in updates:
        state["tg_offset"] = u["update_id"] + 1
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
                f"Последняя проверка: {state['last_check'] or '—'}"
            )

        elif cmd == "/check":
            force = True
            say("Проверяю цены…")

        else:
            say(HELP)
    return force


# ------------------------------ Aviasales --------------------------------
def months_in_window(start, end):
    m = date(start.year, start.month, 1)
    out = []
    while m <= end:
        out.append(m.strftime("%Y-%m"))
        m = date(m.year + (m.month == 12), m.month % 12 + 1, 1)
    return out


def fetch_best(origin, dest, start, end):
    """Самый дешёвый вариант в окне [start, end] с учётом лимита пересадок."""
    best = None
    for month in months_in_window(start, end):
        r = requests.get(
            "https://api.travelpayouts.com/aviasales/v3/prices_for_dates",
            params={
                "origin": origin,
                "destination": dest,
                "departure_at": month,
                "one_way": "true",
                "direct": "false",
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
            if best is None or o["price"] < best["price"]:
                best = {
                    "price": int(o["price"]),
                    "date": dep.isoformat(),
                    "airline": o.get("airline", "?"),
                    "transfers": o.get("transfers", 0),
                    "link": "https://www.aviasales.ru" + o.get("link", ""),
                }
    return best


def check_route(state, route, today, verbose):
    """Проверяет один маршрут. Возвращает лучшую найденную цену (или None)."""
    start = today + timedelta(days=WINDOW_DAYS[0])
    end = today + timedelta(days=WINDOW_DAYS[1])
    name = f"{route['origin']} → {route['dest']}"
    best = fetch_best(route["origin"], route["dest"], start, end)

    if best is None:
        print(f"{name}: нет предложений в окне")
        if verbose:
            say(f"{name}: предложений в окне вылета не найдено. Проверьте коды городов.")
        return None

    old = route["min_price"]
    details = (
        f"{name}\n"
        f"📅 вылет {best['date']}\n"
        f"✈️ {best['airline']}, пересадок: {best['transfers']}\n"
        f"{best['link']}"
    )
    if old is None:
        route["min_price"] = best["price"]
        route["min_info"] = best
        say(f"👀 Слежение запущено. Базовая цена: {rub(best['price'])}\n{details}")
    elif old - best["price"] > state["threshold"]:
        route["min_price"] = best["price"]
        route["min_info"] = best
        say(
            f"🔥 Цена упала: {rub(best['price'])} "
            f"(было {rub(old)}, −{rub(old - best['price'])})\n{details}\n"
            "Цена из кэша Aviasales — проверьте на сайте перед покупкой."
        )
    else:
        print(f"{name}: лучшая цена {best['price']}, минимум {old}, молчим")
        if verbose:
            say(f"{name}: снижения нет. Лучшая цена сейчас {rub(best['price'])} "
                f"(минимум {rub(old)}).")
    return best


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
        mp = r["min_price"]
        lines.append(
            f"• {r['origin']} → {r['dest']}: минимум {rub(mp) if mp else '—'}, "
            f"сейчас {rub(cur['price']) if cur else 'нет данных'}"
        )
    say("📊 Ежемесячный отчёт: бот работает.\n" + "\n".join(lines))
    state["last_report_month"] = month


# -------------------------------- main -----------------------------------
def current_slot(now):
    """Последний плановый слот, который уже наступил сегодня (или None)."""
    passed = [h for h in SLOT_HOURS if now.hour >= h]
    return f"{now.date().isoformat()}-{max(passed):02d}" if passed else None


def report_error(state, now, err):
    print("ERROR:", err)
    today = now.date().isoformat()
    if state["last_error_date"] != today:  # не чаще раза в сутки
        state["last_error_date"] = today
        try:
            say(f"⚠️ Ошибка трекера: {err}")
        except Exception as e:  # noqa: BLE001
            print("Не удалось отправить ошибку в Telegram:", e)


def main():
    now = datetime.now(MSK)
    state = load_state()

    force = FORCE_ENV
    try:
        force = process_commands(state) or force
    except Exception as e:  # noqa: BLE001
        report_error(state, now, f"Telegram: {e}")

    slot = current_slot(now)
    if force or (slot and slot != state["last_slot"]):
        try:
            bests, errors = run_checks(state, now, verbose=force)
            if errors:
                report_error(state, now, "; ".join(errors))
            elif slot:
                state["last_slot"] = slot  # при ошибке слот не отмечается: повтор через 15 минут
            state["last_check"] = now.strftime("%Y-%m-%d %H:%M МСК")
            if not errors:
                monthly_report(state, now, bests)
        except Exception as e:  # noqa: BLE001
            report_error(state, now, e)

    # «пульс», чтобы GitHub не отключил расписание в неактивном репозитории
    hb = state["last_heartbeat"]
    if not hb or (now.date() - date.fromisoformat(hb)).days >= HEARTBEAT_DAYS:
        state["last_heartbeat"] = now.date().isoformat()

    save_state(state)


if __name__ == "__main__":
    main()
