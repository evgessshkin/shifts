"""
Telegram bot for warehouse shifts.

- Checks the shift page every few minutes (slowly, so the site does not
  block you) and messages you when a weekend shift opens, with a "Take it"
  button.
- /calendar shows a month in Telegram. Tap a day to mark which shift you
  want there (06-14, 14-22, 22-06 or any). When a wanted shift opens it is
  booked straight away if AUTO_SUBSCRIBE=1, otherwise you get an alert.
  Days you worked are marked too.
- Weekend reselect (/settings): swaps a booked shift for a better-paid
  weekend or holiday IMPORT shift when the rest-time rule allows only one.
- /salary estimates brutto, netto and payout for every month you worked.

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
    HOURLY_RATE     - brutto per hour for /salary (default 6.5)

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
from bisect import bisect_right
from collections import defaultdict
from datetime import date, datetime, timedelta

import requests

import salary as sal
from shifts_site import (WEEKDAYS, LoggedOut, LoginFailed, Site, SlowDown,
                         describe, parse_page, same_shift)

HERE = os.path.dirname(os.path.abspath(__file__))
WISHES_FILE = os.path.join(HERE, "wishes.json")
SETTINGS_FILE = os.path.join(HERE, "settings.json")
LOG_FILE = os.path.join(HERE, "shifts_log.csv")
# Real payslip figures, shown next to the estimates in /salary:
# {"2026-10": {"gross": 500.00, "net": 465.00, "payout": 455.00}}
PAYSLIPS_FILE = os.path.join(HERE, "payslips.json")

CHECK_EVERY = (120, 240)      # seconds between checks (random in this range)
FAST_CHECK_EVERY = (60, 120)  # fast mode, switched on/off in /settings
NIGHT_HOURS = range(1, 6)     # check less often at night
NIGHT_CHECK_EVERY = (600, 900)
BACKOFF = 30 * 60             # wait 30 min if the server says "slow down"
MIN_MANUAL_CHECK = 60         # /check is ignored if the last check was sooner
RETRY_LOGIN_AFTER = 60 * 60   # after a failed automatic login, wait this long
REST_HOURS = 10               # the portal's minimum rest between two shifts
KEEP_BONUS = 1.0              # EUR: reselect only swaps for a real gain
FIXED = 1e6                   # value of a shift reselect must not cancel

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


def premium_day(day):
    """Days with a pay supplement: weekends and holidays."""
    return is_weekend(day) or sal.is_holiday(day)


def conflict(a, b):
    """True if two shifts overlap or leave less than REST_HOURS between."""
    sa, ea = sal.shift_times(a)
    sb, eb = sal.shift_times(b)
    rest = timedelta(hours=REST_HOURS)
    return sb < ea + rest and sa < eb + rest


def best_schedule(items):
    """Pick shifts that don't conflict with the largest total value.

    items: [(shift, value)]. Every shift blocks the time from its start until
    REST_HOURS after its end, so this is weighted interval scheduling.
    Returns the ids of the chosen shifts.
    """
    spans = []
    for shift, value in items:
        start, end = sal.shift_times(shift)
        spans.append((start, end + timedelta(hours=REST_HOURS), shift, value))
    spans.sort(key=lambda x: x[1])
    ends = [x[1] for x in spans]
    best = [(0.0, [])]          # best[j]: best plan using the first j spans
    for j, (start, _, shift, value) in enumerate(spans):
        i = bisect_right(ends, start, 0, j)
        take = (best[i][0] + value, best[i][1] + [shift])
        best.append(take if take[0] > best[j][0] else best[j])
    return {id(s) for s in best[-1][1]}


def money(x):
    return f"{x:,.2f}".replace(",", " ")


def load_payslips():
    try:
        with open(PAYSLIPS_FILE, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return {}


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
        self.past = []
        self.csrf = None
        self.last_check = None
        self.seen = set()                  # invitation keys already logged
        self.own_cancels = []              # shifts reselect gave up
        self.failed_targets = []           # reselect won't retry these
        self.alerted = set()               # keys alerted while still visible
        self.warned_logout = False
        self.next_check = 0.0

    # ---------- settings (changed with /settings) ----------

    def load_settings(self):
        settings = {"auto": os.environ.get("AUTO_SUBSCRIBE") == "1",
                    "night": False,
                    "fast": False,
                    "reselect": False}
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
                + f"\n\nWeekend reselect: "
                f"{'ON' if self.settings['reselect'] else 'OFF'}\n"
                + ("Swaps a booked shift for a better-paid weekend or holiday "
                   f"{self.book_only or ''} shift when you can't have both, "
                   "and books free ones. Never cancels shifts of other "
                   "departments or ones too late to cancel."
                   if self.settings["reselect"] else
                   "Turn on to let the bot cancel a weekday shift for a "
                   "better-paid weekend shift.")
                + f"\n\nChecking: {self.mode_text()}\n"
                + ("Stays on until you switch it off. Goes back to normal "
                   "only if the site starts refusing requests."
                   if self.fast else
                   "Fast mode checks about twice as often."))
        rows = [[("Turn auto-booking " + ("OFF" if auto else "ON"),
                  "set:auto")],
                [("Show night shifts" if not night else "Ignore night shifts",
                  "set:night")],
                [("Turn weekend reselect "
                  + ("OFF" if self.settings["reselect"] else "ON"),
                  "set:reselect")],
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

    def apply(self, page):
        self.invitations, self.scheduled = page.invitations, page.scheduled
        self.past, self.csrf = page.past, page.csrf

    def check(self):
        """Check the page once. Returns seconds to wait until the next check."""
        try:
            page = self.fetch()
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
        self.apply(page)
        self.last_check = datetime.now()

        fresh = [s for k, s in page.invitations.items() if k not in self.seen]
        if fresh:
            log_new(fresh)
        self.seen.update(page.invitations)

        if self.settings["reselect"]:
            self.reselect()       # may book or swap, then reloads the page
        invitations = self.invitations

        today = date.today()
        # Weekend shifts are reported even when blocked by the rest-time rule,
        # because you may want to drop a neighbouring shift to get them.
        self.own_cancels = [c for c in self.own_cancels if c["day"] >= today]
        self.failed_targets = [f for f in self.failed_targets
                               if f["day"] >= today]
        interesting = {k: s for k, s in invitations.items()
                       if s["day"] >= today and s["status"] != "unknown"
                       and not self.skip(s)
                       and not any(same_shift(s, c) for c in self.own_cancels)
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
                 f"📍 {s.get('place') or 'department unknown'}"]
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
            self.apply(self.fetch())
        except (requests.RequestException, LoggedOut, SlowDown) as e:
            return f"Could not load the page: {e or type(e).__name__}"
        s = self.invitations.get(shift_id)
        if not s:
            return "Too late, this shift is gone."
        self.book(s)
        return ""

    # ---------- weekend reselect ----------

    def reselect_plan(self):
        """The best single move now: (gain, shift to get, shifts to cancel).

        Looks at every open or rest-time-blocked weekend/holiday shift of the
        BOOK_ONLY department and finds the set of shifts with the highest
        total pay that keeps the rest-time rule. A booked shift is dropped
        only if that set doesn't contain it. Returns None if nothing to do.
        """
        today = date.today()
        targets = [s for s in self.invitations.values()
                   if s["day"] >= today and not self.skip(s)
                   and s["status"] in ("available", "blocked (rest time)")
                   and self.bookable_place(s) and premium_day(s["day"])
                   # A day with wishes only takes the wished times.
                   and (s["day"].isoformat() not in self.wishes
                        or self.is_wanted(s))
                   and not any(same_shift(s, f) for f in self.failed_targets)]
        movable = [b for b in self.scheduled
                   if b.get("cancel_id") and self.bookable_place(b)]
        # Shifts that can't move: other departments, too late to cancel,
        # and ones worked in the last two days (rest time still counts).
        fixed = [b for b in self.scheduled if b not in movable] + \
                [p for p in self.past if p["day"] >= today - timedelta(days=2)]
        # Blocked by something we can't see (not by one of our shifts).
        targets = [t for t in targets if t["status"] == "available"
                   or any(conflict(t, b) for b in movable)]
        if not targets:
            return None

        chosen = best_schedule(
            [(b, FIXED) for b in fixed]
            + [(b, sal.shift_value(b) + KEEP_BONUS) for b in movable]
            + [(t, sal.shift_value(t)) for t in targets])
        if any(id(b) not in chosen for b in fixed):
            return None
        moves = []
        for t in targets:
            if id(t) not in chosen:
                continue
            drop = [b for b in movable if id(b) not in chosen and conflict(t, b)]
            gain = sal.shift_value(t) - sum(sal.shift_value(b) for b in drop)
            moves.append((gain, t, drop))
        return max(moves, key=lambda m: m[0]) if moves else None

    def reselect(self, max_moves=3):
        """Do the best moves from reselect_plan, a few per check."""
        for _ in range(max_moves):
            plan = self.reselect_plan()
            if not plan or not self.reselect_move(*plan):
                return

    def reselect_move(self, gain, target, drop):
        """Book target, cancelling drop first. True if it worked."""
        if not drop:
            if target["status"] == "available" and self.book(target):
                try:
                    self.apply(self.fetch())   # plan the next move on fresh data
                except (requests.RequestException, LoggedOut, SlowDown):
                    return False
                return True
            self.failed_targets.append(target)
            return False

        cancelled, problem = [], None
        try:
            for b in drop:
                if not self.site.cancel(b, self.csrf):
                    problem = f"could not cancel {describe(b)}"
                    break
                cancelled.append(b)
            if not problem:
                self.apply(self.fetch())
                fresh = next((s for s in self.invitations.values()
                              if same_shift(s, target)
                              and s["status"] == "available"), None)
                if not fresh:
                    problem = "the weekend shift did not open up"
                elif not self.site.subscribe(fresh, self.csrf, self.lunch):
                    problem = "booking the weekend shift failed"
        except (requests.RequestException, LoggedOut, SlowDown) as e:
            problem = f"connection problem ({e or type(e).__name__})"

        if not problem:
            self.apply(self.fetch())
            self.own_cancels += cancelled
            self.tg.send("🔁 Swapped for a better-paid shift\n"
                         + "".join(f"➖ Cancelled: {describe(b)}\n"
                                   for b in cancelled)
                         + f"➕ Booked: {describe(target)}\n"
                         f"📍 {target.get('place') or '?'}\n"
                         f"≈ +{money(gain)} € brutto")
            return True

        # Something went wrong after cancelling: try to get those back.
        restored, lost = [], []
        for b in cancelled:
            try:
                self.apply(self.fetch())
                again = next((s for s in self.invitations.values()
                              if same_shift(s, b)
                              and s["status"] == "available"), None)
                ok = bool(again) and self.site.subscribe(again, self.csrf,
                                                         self.lunch)
            except (requests.RequestException, LoggedOut, SlowDown):
                ok = False
            (restored if ok else lost).append(b)
        self.tg.send("⚠️ Weekend swap failed: " + problem + "\n"
                     f"Wanted: {describe(target)}\n"
                     + "".join(f"↩️ Booked again: {describe(b)}\n"
                               for b in restored)
                     + "".join(f"❌ Lost, book it yourself: {describe(b)}\n"
                               for b in lost)
                     + self.site.url)
        self.failed_targets.append(target)
        return False

    # ---------- calendar ----------

    def month_view(self, year, month):
        prev_m = (year, month - 1) if month > 1 else (year - 1, 12)
        next_m = (year, month + 1) if month < 12 else (year + 1, 1)
        booked = {s["day"] for s in self.scheduled}
        worked = {s["day"] for s in self.past}
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
                        "☑️" if d in worked else
                        "⭐" if self.wishes.get(d.isoformat()) else
                        "🟢" if d in open_days else "")
                row.append((f"{n}{mark}", f"d:{d.isoformat()}"))
            rows.append(row)
        rows.append([("💶 Salary this month", f"pay:{year}-{month:02d}")])

        text = ("Tap a day to choose shifts you want.\n"
                "✅ booked  ☑️ worked  ⭐ wanted  🟢 open shift")
        return text, rows

    def day_view(self, iso):
        d = date.fromisoformat(iso)
        lines = [f"{WEEKDAYS[d.weekday()]} {d:%d.%m.%Y}"
                 + (" (holiday)" if sal.is_holiday(d) else "")]
        worked = [s for s in self.past if s["day"] == d]
        booked = [s for s in self.scheduled if s["day"] == d]
        opened = [s for s in self.invitations.values() if s["day"] == d]

        def info(s):
            place = f", {s['place']}" if s.get("place") else ""
            return (f"{s['time_from']}-{s['time_to']}{place} · "
                    f"≈{money(sal.shift_value(s))} € brutto")
        for s in worked:
            lines.append(f"☑️ Worked {info(s)}"
                         + (" · lunch" if s.get("lunch") else ""))
        for s in booked:
            lines.append(f"✅ Booked {info(s)}")
        for s in opened:
            lines.append(f"🟢 Open {info(s)} ({s['status']})")
        if not worked and not booked and not opened:
            lines.append("No shifts on this day.")
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

    # ---------- salary ----------

    def shifts_by_month(self):
        """Worked shifts plus booked ones that are still coming."""
        months = defaultdict(lambda: {"worked": [], "booked": []})
        for s in self.past:
            months[(s["day"].year, s["day"].month)]["worked"].append(s)
        for s in self.scheduled:
            if not any(same_shift(s, p) for p in self.past):
                months[(s["day"].year, s["day"].month)]["booked"].append(s)
        return months

    @staticmethod
    def with_payslip(est, real):
        """Real payslip numbers where known, estimates otherwise."""
        gross = real.get("gross", est["gross"])
        net = real.get("net", est["net"])
        payout = real.get("payout", net - est["lunches"] * sal.LUNCH_PRICE)
        return {"gross": (gross, "gross" not in real),
                "net": (net, "net" not in real),
                "payout": (payout, "payout" not in real and "net" not in real)}

    def salary_overview(self):
        months = self.shifts_by_month()
        if not months:
            return "No shifts yet. Data comes after the first check.", None
        payslips = load_payslips()
        lines = ["💶 Salary per month",
                 "brutto / netto / to your account (after lunch)", ""]
        year_total = defaultdict(float)
        keys = sorted(months, reverse=True)
        for y, m in keys:
            data = months[(y, m)]
            est = sal.month_pay(data["worked"] + data["booked"], y)
            real = payslips.get(f"{y}-{m:02d}", {})
            vals = self.with_payslip(est, real)
            year_total[y] += vals["gross"][0]
            shown = " / ".join(("≈" if approx else "") + money(v)
                               for v, approx in vals.values())
            note = ("  📄" if real else "") + (
                f"  ⏳ {len(data['booked'])} booked" if data["booked"] else "")
            lines.append(f"{m:02d}.{y} ({est['shifts']}): {shown}{note}")
        lines.append("")
        lines += [f"{y} brutto total ≈ {money(v)} €"
                  for y, v in sorted(year_total.items(), reverse=True)]
        lines.append("\n📄 = from your payslip, ≈ = estimate.\n"
                     "Tap a month for details.")
        text = "\n".join(lines)
        if len(text) > 4000:                 # Telegram message limit
            text = text[:3990] + "\n…"
        buttons = [(f"{m:02d}.{y}", f"pay:{y}-{m:02d}") for y, m in keys]
        rows = [buttons[i:i + 4] for i in range(0, len(buttons), 4)]
        return text, rows

    def salary_month(self, key):
        y, m = map(int, key.split("-"))
        data = self.shifts_by_month().get((y, m), {"worked": [], "booked": []})
        shifts = data["worked"] + data["booked"]
        est = sal.month_pay(shifts, y)
        mh = sal.min_hourly(y)
        h = est["hours"]
        planned = sum((lambda a, b: (b - a).total_seconds() / 3600)(
            *sal.shift_times(s)) for s in shifts)
        lines = [f"💶 {MONTHS[m]} {y}",
                 f"Worked: {len(data['worked'])} shifts"
                 + (f", booked: {len(data['booked'])} more"
                    if data["booked"] else ""),
                 f"Planned {planned:.2f} h → paid ≈{h['all']:.2f} h "
                 f"(minus 30 min break per shift, ×{sal.PAID_FACTOR} for "
                 f"toilet breaks and clocking)", "",
                 f"Base {h['all']:.2f} h × {sal.hourly_rate():.2f} = "
                 f"{money(est['lines']['base'])}"]
        names = {"sat": "Saturday", "sun": "Sunday", "holiday": "Holiday",
                 "night": "Night"}
        for k, name in names.items():
            if est["lines"][k]:
                lines.append(f"{name} {h[k]:.2f} h × "
                             f"{sal.SUPPLEMENT[k] * mh:.2f} = "
                             f"{money(est['lines'][k])}")
        lines += [f"Brutto ≈ {money(est['gross'])}",
                  f"Insurance 4 % + 3 %: −{money(est['insurance'])}",
                  f"Tax 19 % (first {money(sal.for_year(sal.TAX_FREE, y))} "
                  f"tax-free): −{money(est['tax'])}",
                  f"Netto ≈ {money(est['net'])}",
                  f"Lunch {est['lunches']} × {sal.LUNCH_PRICE:.0f} €: "
                  f"−{money(est['lunches'] * sal.LUNCH_PRICE)}",
                  f"To your account ≈ {money(est['payout'])}"]
        real = load_payslips().get(key)
        if real:
            lines.append("\n📄 Payslip: " + ", ".join(
                f"{label} {money(real[k])}" for k, label in
                (("gross", "brutto"), ("net", "netto"), ("payout", "to account"))
                if k in real))
        if y < 2026:
            lines.append("\nOlder month: uses your current hourly rate, "
                         "which may have been different then.")
        rows = [[("« All months", "pay:all")]]
        return "\n".join(lines), rows

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
                f"{'shown' if self.settings['night'] else 'ignored'}\n"
                f"Weekend reselect: "
                f"{'ON' if self.settings['reselect'] else 'off'}")

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
        elif text == "/salary":
            self.tg.send(*self.salary_overview())
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
                         "/salary – brutto and netto per month\n"
                         "/status – last check and counts\n"
                         "/settings – auto-booking, reselect, night, fast\n"
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
        elif kind == "set" and arg in ("auto", "night", "reselect"):
            self.toggle_setting(arg)
            note = f"{arg}: {'on' if self.settings[arg] else 'off'}"
            self.tg.edit(msg_id, *self.settings_view())
        elif kind == "pay":
            view = self.salary_overview() if arg == "all" \
                else self.salary_month(arg)
            self.tg.edit(msg_id, *view)
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
            {"command": "salary", "description": "Brutto and netto per month"},
            {"command": "status", "description": "Last check and counts"},
            {"command": "settings",
             "description": "Auto-booking, reselect, night, fast checking"},
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
        page = parse_page(f.read())
    print(f"Invitations ({len(page.invitations)}):")
    for s in sorted(page.invitations.values(), key=lambda s: s["day"]):
        mark = "  <- weekend" if is_weekend(s["day"]) else ""
        print(f"  {describe(s)}  [{s['status']}]  id={s['id']}{mark}")
    print(f"Scheduled ({len(page.scheduled)}):")
    for s in sorted(page.scheduled, key=lambda s: s["day"]):
        print("  " + describe(s) + ("" if s["cancel_id"] else "  (can't cancel)"))
    print(f"Past shifts: {len(page.past)}")
    print("csrf token found:", bool(page.csrf))


if __name__ == "__main__":
    load_env(os.path.join(HERE, ".env"))
    if len(sys.argv) > 1:
        test_file(sys.argv[1])
    else:
        Bot().run()
