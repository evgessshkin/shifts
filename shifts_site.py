"""
Talking to the shift website: download the page, read the tables, book a shift.
"""

from datetime import datetime
from urllib.parse import urljoin, urlparse

import random
import time

import requests
from bs4 import BeautifulSoup

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/129.0 Safari/537.36"
)
WEEKDAYS = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]


class LoggedOut(Exception):
    pass


class SlowDown(Exception):
    """Server answered 403/429: stop asking for a while."""


class LoginFailed(Exception):
    pass


def cell_text(row, name):
    td = row.find("td", attrs={"name": name})
    return td.get_text(strip=True) if td else "?"


def table_rows(soup, table_id):
    table = soup.find("table", id=table_id)
    if not table:
        return []
    rows = []
    for row in table.find_all("tr"):
        try:
            day = datetime.strptime(cell_text(row, "date"), "%d.%m.%Y").date()
        except ValueError:
            continue
        rows.append((row, {
            "day": day,
            "time_from": cell_text(row, "time_from"),
            "time_to": cell_text(row, "time_to"),
            "responsible": cell_text(row, "user_id"),
        }))
    return rows


def parse_page(html):
    """Return (invitations, scheduled, csrf_token) from the shift page."""
    soup = BeautifulSoup(html, "html.parser")

    invitations = {}
    for row, s in table_rows(soup, "invitations_table"):
        button = row.find("button", class_="subscribe_shift")
        if button:
            s["status"] = "available"
            s["id"] = button.get("data-id")
            s["can_lunch"] = button.get("data-can_lunch") == "True"
            # data-name is "date / department, address / responsible - you"
            parts = button.get("data-name", "").split(" / ")
            s["place"] = parts[1].strip() if len(parts) > 2 else None
        elif row.find("i", class_="fa-ban"):
            s["status"] = "blocked (rest time)"
            s["id"] = None
            s["place"] = None     # only shown on the Subscribe button
        else:
            s["status"] = "unknown"
            s["id"] = None
            s["place"] = None
        # A blocked shift and the same shift once it becomes available get
        # different keys, so you are notified again when it opens for you.
        key = s["id"] or f"{s['day']} {s['time_from']}-{s['time_to']} {s['status']}"
        invitations[key] = s

    scheduled = [s for _, s in table_rows(soup, "scheduled_shifts_table")]

    token_input = soup.find("input", attrs={"name": "csrf_token"})
    csrf = token_input.get("value") if token_input else None
    return invitations, scheduled, csrf


def describe(s):
    return (f"{WEEKDAYS[s['day'].weekday()]} {s['day']:%d.%m.%Y} "
            f"{s['time_from']}-{s['time_to']}")


def same_shift(a, b):
    return (a["day"], a["time_from"], a["time_to"]) == \
           (b["day"], b["time_from"], b["time_to"])


def looks_logged_out(response):
    if response.is_redirect:
        return True
    soup = BeautifulSoup(response.text, "html.parser")
    return soup.find("input", attrs={"type": "password"}) is not None


class Site:
    def __init__(self, url, cookie, user_agent=None):
        self.url = url
        self.host = urlparse(url).hostname
        self.session = requests.Session()
        self.session.headers["User-Agent"] = user_agent or USER_AGENT
        # Keep cookies in a jar (not a fixed header) so a new session_id from
        # the server, e.g. after logging in again, replaces the old one.
        parent = "." + ".".join(self.host.split(".")[-2:])
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name:
                cf = name.startswith(("cf_", "__cf"))
                self.session.cookies.set(name, value, path="/",
                                         domain=parent if cf else self.host)

    def login(self, username, password):
        """Log in with the website form. Raises LoginFailed / SlowDown."""
        login_url = urljoin(self.url, "/web/login")
        try:
            self.session.cookies.clear(self.host, "/", "session_id")
        except KeyError:
            pass

        r = self.session.get(login_url, timeout=30, allow_redirects=False)
        if r.status_code in (403, 429):
            raise SlowDown(r.status_code)
        token = BeautifulSoup(r.text, "html.parser").find(
            "input", attrs={"name": "csrf_token"})
        if not token:
            raise LoginFailed("login form not found")

        r = self.session.post(login_url, timeout=30, allow_redirects=False,
                              data={"csrf_token": token.get("value"),
                                    "login": username, "password": password,
                                    "redirect": ""})
        if r.status_code in (403, 429):
            raise SlowDown(r.status_code)
        # The site redirects after a good login and shows the form again,
        # with an error message, after a bad one.
        if not r.is_redirect:
            error = BeautifulSoup(r.text, "html.parser").find(
                class_="alert-danger")
            raise LoginFailed(error.get_text(" ", strip=True) if error
                              else f"HTTP {r.status_code}")
        try:
            self.fetch()
        except LoggedOut:
            raise LoginFailed("still logged out after login")

    def fetch(self):
        """Download and parse the page. Raises LoggedOut / SlowDown."""
        r = self.session.get(self.url, timeout=30, allow_redirects=False)
        if r.status_code in (403, 429):
            raise SlowDown(r.status_code)
        if looks_logged_out(r):
            raise LoggedOut()
        r.raise_for_status()
        return parse_page(r.text)

    def subscribe(self, shift, csrf, lunch="no"):
        """Book one shift. Returns True if it now shows in My Scheduled Shifts."""
        if not shift.get("can_lunch"):
            lunch = "no"
        r = self.session.post(
            urljoin(self.url, "/invitation/subscribe"),
            data={"csrf_token": csrf, "record_id": shift["id"], "lunch": lunch},
            timeout=30,
        )
        print(f"Subscribe {shift['id']} -> HTTP {r.status_code}")

        # Check the result the same way you would: reload and look at the table.
        time.sleep(random.uniform(2, 4))
        _, scheduled, _ = self.fetch()
        return any(same_shift(shift, s) for s in scheduled)
