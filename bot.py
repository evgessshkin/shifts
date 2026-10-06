"""
Telegram bot for warehouse shifts.

- Checks the shift page every few minutes (slowly, so the site does not
  block you) and messages you when a weekend shift opens, with a "Take it"
  button.
- /calendar shows a month in Telegram. Tap a day to mark which shift you
  want there (06-14, 14-22, 22-06 or any). When a wanted shift opens it is
  booked straight away if AUTO_SUBSCRIBE=1, otherwise you get an alert.

Settings are read from a .env file next to this script (or the environment):
    SHIFTS_URL      - address of the page with the shift tables
    SHIFTS_COOKIE   - value of the Cookie request header from your browser
    SHIFTS_USER_AGENT - User-Agent header of that same browser (needed for
                      the cf_clearance cookie to be accepted)
    SHIFTS_LOGIN, SHIFTS_PASSWORD - optional; used to log in again by itself
                      when the session expires
    TG_TOKEN        - Telegram bot token from @BotFather
    TG_CHAT_ID      - your Telegram chat id (the bot ignores everyone else)
    AUTO_SUBSCRIBE  - "1" to book wanted shifts automatically (default off);
                      only the starting value, change it later with /settings
    LUNCH           - "yes" or "no" when booking (default "no")
    BOOK_ONLY       - auto-book only shifts whose department contains this
                      text (default "IMPORT"; empty = any department).
                      You still get messages about all departments.

Run:                 python bot.py
Test on saved page:  python bot.py saved_page.html
"""

import calendar
import csv
import json
import os
import random
import sys
import time
from datetime import date, datetime

import requests

from shifts_site import (WEEKDAYS, LoggedOut, LoginFailed, Site, SlowDown,
                         describe, parse_page)

HERE = os.path.dirname(os.path.abspath(__file__))
WISHES_FILE = os.path.join(HERE, "wishes.json")
SETTINGS_FILE = os.path.join(HERE, "settings.json")
LOG_FILE = os.path.join(HERE, "shifts_log.csv")

CHECK_EVERY = (120, 240)      # seconds between checks (random in this range)
FAST_CHECK_EVERY = (60, 120)  # fast mode, switched on/off in /settings
NIGHT_HOURS = range(1, 6)     # check less often at night
NIGHT_CHECK_EVERY = (600, 900)
BACKOFF = 30 * 60             # wait 30 min if the server says "slow down"
MIN_MANUAL_CHECK = 60         # /check is ignored if the last check was sooner
RETRY_LOGIN_AFTER = 60 * 60   # after a failed automatic login, wait this long

# Shifts are grouped by start time, so 06:01-14:00 or 08:00-16:00 still count
# as a morning shift and 13:00-21:00 or 14:01-22:00 as an afternoon one.
SLOTS = {"06": "Morning", "14": "Afternoon", "22": "Night"}
SLOT_HINT = "Morning = starts 04:00-10:59, Afternoon = 11:00-17:59"
MONTHS = ["", "January", "February", "March", "April", "May", "June", "July",
          "August", "September", "October", "November", "December"]


def load_env(path):
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"\''))


def slot_of(shift):
    try:
        hour = int(shift["time_from"].split(":")[0])
    except ValueError:
        return None
    if 4 <= hour < 11:
        return "06"
    if 11 <= hour < 18:
        return "14"
    return "22"


def is_weekend(day):
    return day.weekday() >= 5


class Telegram:
    def __init__(self, token, chat_id):
        self.base = f"https://api.telegram.org/bot{token}/"
        self.chat_id = str(chat_id)

    def call(self, method, http_timeout=15, **params):
        try:
            r = requests.post(self.base + method, json=params,
                              timeout=http_timeout)
            data = r.json()
        except (requests.RequestException, ValueError) as e:
            print(f"Telegram {method} error:", e)
            return None
        if not data.get("ok"):
            print(f"Telegram {method}:", data.get("description"))
            return None
        return data["result"]

    @staticmethod
    def markup(buttons):
        if not buttons:
            return None
        return {"inline_keyboard": [[{"text": t, "callback_data": d}
                                     for t, d in row] for row in buttons]}

    def send(self, text, buttons=None):
        params = {"chat_id": self.chat_id, "text": text}
        if buttons:
            params["reply_markup"] = self.markup(buttons)
        self.call("sendMessage", **params)

    def edit(self, message_id, text, buttons=None):
        params = {"chat_id": self.chat_id, "message_id": message_id,
                  "text": text}
        if buttons:
            params["reply_markup"] = self.markup(buttons)
        self.call("editMessageText", **params)

    def answer(self, callback_id, text=""):
        self.call("answerCallbackQuery", callback_query_id=callback_id,
                  text=text)

    def updates(self, offset, timeout):
        return self.call("getUpdates", http_timeout=timeout + 10,
                         offset=offset, timeout=timeout,
                         allowed_updates=["message", "callback_query"]) or []


class Bot:
    def __init__(self):
        self.site = Site(os.environ["SHIFTS_URL"],
                         os.environ.get("SHIFTS_COOKIE", ""),
                         os.environ.get("SHIFTS_USER_AGENT"))
        self.username = os.environ.get("SHIFTS_LOGIN")
        self.password = os.environ.get("SHIFTS_PASSWORD")
        self.next_login_try = 0.0
        self.tg = Telegram(os.environ["TG_TOKEN"], os.environ["TG_CHAT_ID"])
        self.settings = self.load_settings()
        self.lunch = os.environ.get("LUNCH", "no").lower()
        self.book_only = os.environ.get("BOOK_ONLY", "IMPORT").strip().upper()

        self.wishes = self.load_wishes()   # {"2026-10-17": ["14", "any"]}
        self.invitations = {}
        self.scheduled = []
        self.csrf = None
        self.last_check = None
        self.seen = set()                  # invitation keys already logged
        self.alerted = set()               # keys alerted while still visible
        self.warned_logout = False
        self.next_check = 0.0

    # ---------- settings (changed with /settings) ----------

    def load_settings(self):
        settings = {"auto": os.environ.get("AUTO_SUBSCRIBE") == "1",
                    "night": False,
                    "fast": False}
        try:
            with open(SETTINGS_FILE, encoding="utf-8") as f:
                settings.update(json.load(f))
        except (OSError, ValueError):
            pass
        settings.pop("fast_until", None)      # from the old timed fast mode
        return settings

    def save_settings(self):
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(self.settings, f, indent=1)

    def toggle_setting(self, name):
        self.settings[name] = not self.settings[name]
        self.save_settings()

    @property
    def fast(self):
        return self.settings["fast"]

    def set_fast(self, on):
        self.settings["fast"] = on
        self.save_settings()
        if on:
            # Don't wait out the rest of a slow pause.
            self.next_check = min(self.next_check,
                                  time.time() + random.uniform(*FAST_CHECK_EVERY))

    def next_wait(self):
        if self.fast:
            return random.uniform(*FAST_CHECK_EVERY)
        if datetime.now().hour in NIGHT_HOURS:
            return random.uniform(*NIGHT_CHECK_EVERY)
        return random.uniform(*CHECK_EVERY)

    def mode_text(self):
        if self.fast:
            return (f"⚡ Fast (every {FAST_CHECK_EVERY[0] // 60}-"
                    f"{FAST_CHECK_EVERY[1] // 60} min, day and night)")
        return (f"Normal (every {CHECK_EVERY[0] // 60}-{CHECK_EVERY[1] // 60} "
                f"min, slower at night)")

    @property
    def auto(self):
        return self.settings["auto"]

    def bookable_place(self, shift):
        """Auto-booking is limited to one department (BOOK_ONLY)."""
        return not self.book_only or \
            self.book_only in (shift.get("place") or "").upper()

    def skip(self, shift):
        """Night shifts are ignored completely unless switched on."""
        return slot_of(shift) == "22" and not self.settings["night"]

    def settings_view(self):
        auto, night = self.settings["auto"], self.settings["night"]
        text = ("Settings\n\n"
                f"Auto-booking: {'ON' if auto else 'OFF'}\n"
                + ("Wanted shifts are booked the moment they appear."
                   if auto else
                   "You get a message and book with the Take it button.")
                + f"\n\nNight shifts: {'shown' if night else 'ignored'}\n"
                + ("Night shifts can be alerted and booked." if night else
                   "No alerts and no booking for shifts starting 18:00-03:59.")
                + f"\n\nChecking: {self.mode_text()}\n"
                + ("Stays on until you switch it off. Goes back to normal "
                   "only if the site starts refusing requests."
                   if self.fast else
                   "Fast mode checks about twice as often."))
        rows = [[("Turn auto-booking " + ("OFF" if auto else "ON"),
                  "set:auto")],
                [("Show night shifts" if not night else "Ignore night shifts",
                  "set:night")],
                [("Back to normal checking", "fast:off") if self.fast else
                 ("⚡ Fast checking", "fast:on")]]
        return text, rows

    # ---------- wishes ----------

    def load_wishes(self):
        try:
            with open(WISHES_FILE, encoding="utf-8") as f:
                return json.load(f)
        except (OSError, ValueError):
            return {}

    def save_wishes(self):
        today = date.today().isoformat()
        self.wishes = {d: s for d, s in self.wishes.items() if s and d >= today}
        with open(WISHES_FILE, "w", encoding="utf-8") as f:
            json.dump(self.wishes, f, indent=1, sort_keys=True)

    def is_wanted(self, shift):
        if self.skip(shift):
            return False
        slots = self.wishes.get(shift["day"].isoformat(), [])
        return "any" in slots or slot_of(shift) in slots

    def toggle_wish(self, day, slot):
        slots = self.wishes.setdefault(day, [])
        if slot in slots:
            slots.remove(slot)
        else:
            slots.append(slot)
        self.save_wishes()

    # ---------- checking the site ----------

    def relogin(self):
        """Log in again with SHIFTS_LOGIN/SHIFTS_PASSWORD. True on success."""
        if not (self.username and self.password) \
                or time.time() < self.next_login_try:
            return False
        try:
            self.site.login(self.username, self.password)
        except LoginFailed as e:
            self.next_login_try = time.time() + RETRY_LOGIN_AFTER
            print("Login failed:", e)
            self.tg.send(f"Automatic login failed: {e}\nLog in in the browser "
                         f"and update SHIFTS_COOKIE, or check the password. "
                         f"Next try in {RETRY_LOGIN_AFTER // 60} min.")
            return False
        except requests.RequestException as e:
            self.next_login_try = time.time() + 10 * 60
            print("Login network error:", e)
            return False
        print("Logged in again.")
        self.tg.send("Session expired, logged in again automatically.")
        self.warned_logout = False
        return True

    def fetch(self):
        try:
            return self.site.fetch()
        except LoggedOut:
            if not self.relogin():
                raise
            return self.site.fetch()

    def check(self):
        """Check the page once. Returns seconds to wait until the next check."""
        try:
            invitations, scheduled, csrf = self.fetch()
        except SlowDown as e:
            print(f"Server returned {e}, backing off.")
            was_fast = self.fast
            self.set_fast(False)
            self.tg.send(f"Server returned {e}. Pausing for "
                         f"{BACKOFF // 60} min."
                         + (" Fast mode switched off." if was_fast else ""))
            return BACKOFF
        except LoggedOut:
            if not self.warned_logout:
                self.tg.send("Session expired. Log in again in the browser "
                             "and update SHIFTS_COOKIE.")
                self.warned_logout = True
            print("Logged out.")
            return self.next_wait()
        except requests.RequestException as e:
            print("Network error:", e)
            return self.next_wait()

        self.warned_logout = False
        self.invitations, self.scheduled, self.csrf = invitations, scheduled, csrf
        self.last_check = datetime.now()

        fresh = [s for k, s in invitations.items() if k not in self.seen]
        if fresh:
            log_new(fresh)
        self.seen.update(invitations)

        today = date.today()
        # Weekend shifts are reported even when blocked by the rest-time rule,
        # because you may want to drop a neighbouring shift to get them.
        interesting = {k: s for k, s in invitations.items()
                       if s["day"] >= today and s["status"] != "unknown"
                       and not self.skip(s)
                       and (is_weekend(s["day"]) or
                            (s["status"] == "available" and self.is_wanted(s)))}
        booked_days = set()
        for k, s in sorted(interesting.items(), key=lambda kv: kv[1]["day"]):
            if k in self.alerted:
                continue
            if s["status"] != "available":
                self.alert_blocked(s)
            elif self.auto and self.is_wanted(s) and self.bookable_place(s) \
                    and s["day"] not in booked_days:
                if self.book(s):
                    booked_days.add(s["day"])
            else:
                self.alert(s)
        # Forget shifts that disappeared, so one that is taken and later
        # cancelled triggers a new message.
        self.alerted = set(interesting)

        print(f"{self.last_check:%H:%M:%S} ok, {len(invitations)} "
              f"invitation(s), {len(interesting)} interesting")
        return self.next_wait()

    def alert(self, s):
        star = "⭐ Wanted shift" if self.is_wanted(s) else "Weekend shift"
        lines = [f"{star} available!", describe(s),
                 f"📍 {s.get('place') or 'department unknown'}"]
        if self.auto and self.is_wanted(s) and not self.bookable_place(s):
            lines.append(f"Not booked automatically: not {self.book_only}.")
        lines.append(self.site.url)
        self.tg.send("\n".join(lines), [[("✅ Take it", f"s:{s['id']}")]])

    def alert_blocked(self, s):
        near = [b for b in self.scheduled if abs((b["day"] - s["day"]).days) <= 1]
        lines = [f"Weekend shift appeared, but you can't take it ({s['status']}):",
                 describe(s),
                 "📍 department not shown for blocked shifts"]
        if near:
            lines.append("Your shifts next to it:")
            lines += ["  " + describe(b) for b in near]
        lines.append("If you cancel the conflicting shift it becomes "
                     "available, and you'll get another message.")
        self.tg.send("\n".join(lines))

    def book(self, s):
        try:
            ok = self.site.subscribe(s, self.csrf, self.lunch)
        except (requests.RequestException, LoggedOut, SlowDown) as e:
            print("Booking error:", e)
            ok = False
        if ok:
            self.wishes.pop(s["day"].isoformat(), None)
            self.save_wishes()
            self.tg.send(f"✅ Booked!\n{describe(s)}\n📍 {s.get('place') or '?'}")
        else:
            self.tg.send(f"Tried to book but it did not show up in your "
                         f"schedule. Check now!\n{describe(s)}\n{self.site.url}",
                         [[("Try again", f"s:{s['id']}")]])
        return ok

    def take(self, shift_id):
        """'Take it' button: reload the page, then book if still open."""
        try:
            self.invitations, self.scheduled, self.csrf = self.fetch()
        except (requests.RequestException, LoggedOut, SlowDown) as e:
            return f"Could not load the page: {e or type(e).__name__}"
        s = self.invitations.get(shift_id)
        if not s:
            return "Too late, this shift is gone."
        self.book(s)
        return ""

    # ---------- calendar ----------

    def month_view(self, year, month):
        prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
        next_m = (year, month + 1) if month < 12 else (year + 1, 1)
        booked = {s["day"] for s in self.scheduled}
        open_days = {s["day"] for s in self.invitations.values()
                     if s["status"] == "available" and not self.skip(s)}

        rows = [[("◀", f"m:{prev_m[0]}-{prev_m[1]}"),
                 (f"{MONTHS[month]} {year}", "noop"),
                 ("▶", f"m:{next_m[0]}-{next_m[1]}")],
                [(d, "noop") for d in ["Mo", "Tu", "We", "Th", "Fr", "Sa", "Su"]]]
        for week in calendar.monthcalendar(year, month):
            row = []
            for n in week:
                if n == 0:
                    row.append((" ", "noop"))
                    continue
                d = date(year, month, n)
                mark = ("✅" if d in booked else
                        "⭐" if self.wishes.get(d.isoformat()) else
                        "🟢" if d in open_days else "")
                row.append((f"{n}{mark}", f"d:{d.isoformat()}"))
            rows.append(row)

        text = ("Tap a day to choose shifts you want.\n"
                "✅ booked  ⭐ wanted  🟢 open shift")
        return text, rows

    def day_view(self, iso):
        d = date.fromisoformat(iso)
        lines = [f"{WEEKDAYS[d.weekday()]} {d:%d.%m.%Y}"]
        booked = [s for s in self.scheduled if s["day"] == d]
        opened = [s for s in self.invitations.values() if s["day"] == d]
        for s in booked:
            lines.append(f"✅ Booked {s['time_from']}-{s['time_to']}")
        for s in opened:
            place = f", {s['place']}" if s.get("place") else ""
            lines.append(f"🟢 Open {s['time_from']}-{s['time_to']}{place} "
                         f"({s['status']})")
        if not booked and not opened:
            lines.append("No shifts published for this day yet.")
        if self.last_check:
            lines.append(f"(page checked at {self.last_check:%H:%M})")

        rows = []
        if d >= date.today():
            wanted = self.wishes.get(iso, [])
            night = self.settings["night"]
            rows.append([(("⭐ " if slot in wanted else "") + label,
                          f"w:{iso}:{slot}") for slot, label in SLOTS.items()
                         if night or slot != "22"])
            rows.append([(("⭐ " if "any" in wanted else "") +
                          ("Any shift" if night else "Any day shift"),
                          f"w:{iso}:any")])
            for s in opened:
                if s["status"] == "available" and not self.skip(s):
                    rows.append([(f"✅ Take {s['time_from']}-{s['time_to']}",
                                  f"s:{s['id']}")])
            lines.append("Tap a time to mark it as wanted.\n" + SLOT_HINT)
            if not self.settings["night"]:
                lines.append("Night shifts are ignored (see /settings).")
        rows.append([("« Back", f"m:{d.year}-{d.month}")])
        return "\n".join(lines), rows

    def wishes_text(self):
        if not self.wishes:
            return "No wanted shifts yet. Use /calendar to add some."
        lines = ["Wanted shifts:"]
        for iso, slots in sorted(self.wishes.items()):
            d = date.fromisoformat(iso)
            names = ["any" if s == "any" else SLOTS[s] for s in slots]
            lines.append(f"⭐ {WEEKDAYS[d.weekday()]} {d:%d.%m} – "
                         + ", ".join(names))
        return "\n".join(lines)

    def status_text(self):
        when = f"{self.last_check:%d.%m %H:%M}" if self.last_check else "never"
        avail = sum(s["status"] == "available"
                    for s in self.invitations.values())
        return (f"Last check: {when}\n"
                f"Invitations: {len(self.invitations)} ({avail} available)\n"
                f"Scheduled shifts: {len(self.scheduled)}\n"
                f"Wanted days: {len(self.wishes)}\n"
                f"Checking: {self.mode_text()}\n"
                f"Auto-booking: {'ON' if self.auto else 'off'}\n"
                f"Night shifts: "
                f"{'shown' if self.settings['night'] else 'ignored'}")

    # ---------- Telegram updates ----------

    def handle(self, update):
        if "callback_query" in update:
            q = update["callback_query"]
            if str(q["message"]["chat"]["id"]) != self.tg.chat_id:
                return
            self.on_button(q)
        elif "message" in update:
            m = update["message"]
            if str(m["chat"]["id"]) != self.tg.chat_id:
                return
            self.on_command(m.get("text", "").split("@")[0].strip())

    def on_command(self, text):
        if text == "/calendar":
            self.tg.send(*self.month_view(date.today().year,
                                          date.today().month))
        elif text == "/wishes":
            self.tg.send(self.wishes_text())
        elif text == "/status":
            self.tg.send(self.status_text())
        elif text == "/settings":
            self.tg.send(*self.settings_view())
        elif text == "/check":
            since = (datetime.now() - self.last_check).total_seconds() \
                if self.last_check else MIN_MANUAL_CHECK
            if since < MIN_MANUAL_CHECK:
                self.tg.send("Checked less than a minute ago, try later.")
            else:
                self.check()
                self.tg.send(self.status_text())
        else:
            self.tg.send("Commands:\n"
                         "/calendar – choose the shifts you want\n"
                         "/wishes – list wanted shifts\n"
                         "/status – last check and counts\n"
                         "/settings – auto-booking, night shifts, fast checking\n"
                         "/check – check the page now")

    def on_button(self, q):
        data, msg_id = q.get("data", ""), q["message"]["message_id"]
        kind, _, arg = data.partition(":")
        note = ""
        if kind == "m":
            y, m = map(int, arg.split("-"))
            self.tg.edit(msg_id, *self.month_view(y, m))
        elif kind == "d":
            self.tg.edit(msg_id, *self.day_view(arg))
        elif kind == "w":
            iso, slot = arg.rsplit(":", 1)
            self.toggle_wish(iso, slot)
            self.tg.edit(msg_id, *self.day_view(iso))
        elif kind == "set" and arg in ("auto", "night"):
            self.toggle_setting(arg)
            note = f"{arg}: {'on' if self.settings[arg] else 'off'}"
            self.tg.edit(msg_id, *self.settings_view())
        elif kind == "fast":
            self.set_fast(arg == "on")
            note = "Fast checking on" if self.fast else "Normal checking"
            self.tg.edit(msg_id, *self.settings_view())
        elif kind == "s":
            self.tg.answer(q["id"], "Booking…")
            note = self.take(arg)
            if note:
                self.tg.send(note)
            return
        self.tg.answer(q["id"], note)

    # ---------- main loop ----------

    def run(self):
        self.tg.call("setMyCommands", commands=[
            {"command": "calendar", "description": "Choose the shifts you want"},
            {"command": "wishes", "description": "List wanted shifts"},
            {"command": "status", "description": "Last check and counts"},
            {"command": "settings",
             "description": "Auto-booking, night shifts, fast checking"},
            {"command": "check", "description": "Check the page now"},
        ])
        self.tg.send("Shift bot started."
                     + (" Auto-booking is ON." if self.auto else "")
                     + "\nUse /calendar to choose shifts.")
        offset = None
        while True:
            if time.time() >= self.next_check:
                self.next_check = time.time() + self.check()
            # Waiting for Telegram messages doubles as the pause between
            # checks, so buttons react instantly while the site is checked
            # only every few minutes.
            timeout = int(min(50, max(1, self.next_check - time.time())))
            for u in self.tg.updates(offset, timeout):
                offset = u["update_id"] + 1
                try:
                    self.handle(u)
                except Exception as e:  # one bad update must not stop the bot
                    print("Error handling update:", repr(e))


def log_new(shifts):
    new_file = not os.path.exists(LOG_FILE)
    with open(LOG_FILE, "a", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        if new_file:
            w.writerow(["seen_at", "weekday", "date", "from", "to", "status"])
        now = f"{datetime.now():%Y-%m-%d %H:%M:%S}"
        for s in shifts:
            w.writerow([now, WEEKDAYS[s["day"].weekday()], s["day"],
                        s["time_from"], s["time_to"], s["status"]])


def test_file(path):
    with open(path, encoding="utf-8") as f:
        invitations, scheduled, csrf = parse_page(f.read())
    print(f"Invitations ({len(invitations)}):")
    for s in sorted(invitations.values(), key=lambda s: s["day"]):
        mark = "  <- weekend" if is_weekend(s["day"]) else ""
        print(f"  {describe(s)}  [{s['status']}]  id={s['id']}{mark}")
    print(f"Scheduled ({len(scheduled)}):")
    for s in sorted(scheduled, key=lambda s: s["day"]):
        print("  " + describe(s))
    print("csrf token found:", bool(csrf))


if __name__ == "__main__":
    load_env(os.path.join(HERE, ".env"))
    if len(sys.argv) > 1:
        test_file(sys.argv[1])
    else:
        Bot().run()
