"""
Salary estimate for a student work agreement ("dohoda o brigádnickej práci
študentov") in Slovakia.

Checked against the payslips for 08.2026 and 10.2026 (given the real hours):
    brutto  = paid hours x hourly rate + supplements
              (Saturday 50 %, Sunday 100 %, holiday 100 %, night 40 %
               of the minimum hourly wage = minimum monthly wage / 174)
    insurance = 4 % old-age + 3 % disability, each rounded down to a cent
              (no health or unemployment insurance on this agreement)
    tax     = 19 % of (brutto - insurance - monthly tax-free allowance)
    netto   = brutto - insurance - tax          ("Čistý príjem")
    payout  = netto - 1 EUR for every lunch      ("Výplata na účet")

The portal only shows planned shift times, not clocked hours. Each shift
loses a 30 min unpaid break, and toilet breaks or clocking minutes cost a
little more, so planned hours are multiplied by PAID_FACTOR. Real payslips
were 1-4 % below the planned hours.
"""

import os
from datetime import date, datetime, timedelta
from decimal import ROUND_DOWN, ROUND_HALF_UP, Decimal

PAUSE_HOURS = 0.5
PAID_FACTOR = 0.98
LUNCH_PRICE = 1.0
NIGHT = (22, 6)               # night work is 22:00-06:00

# Minimum monthly wage and monthly tax-free allowance (nezdaniteľná časť
# základu dane) per year. Years not listed use the latest one.
MIN_WAGE = {2023: 700, 2024: 750, 2025: 816, 2026: 915}
TAX_FREE = {2023: 410.24, 2024: 470.54, 2025: 479.48, 2026: 497.23}
SUPPLEMENT = {"sat": 0.5, "sun": 1.0, "holiday": 1.0, "night": 0.4}
OLD_AGE, DISABILITY, TAX = 0.04, 0.03, 0.19


def hourly_rate():
    return float(os.environ.get("HOURLY_RATE", "6.5").replace(",", "."))


def for_year(table, year):
    known = [k for k in table if k <= year]
    return table[max(known)] if known else table[min(table)]


def min_hourly(year):
    return for_year(MIN_WAGE, year) / 174


def easter(year):
    """Easter Sunday (Gregorian calendar)."""
    a, b, c = year % 19, year // 100, year % 100
    d, e = b // 4, b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i, k = c // 4, c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    return date(year, month, (h + l - 7 * m + 114) % 31 + 1)


def holidays(year):
    """Days of rest (dni pracovného pokoja) in Slovakia that year."""
    days = {(1, 1), (1, 6), (5, 1), (5, 8), (7, 5), (8, 29), (9, 1), (9, 15),
            (11, 1), (11, 17), (12, 24), (12, 25), (12, 26)}
    # Consolidation: these stopped being days off.
    if year >= 2024:
        days.discard((9, 1))
    if year >= 2025:
        days.discard((11, 17))
    if year == 2026:
        days -= {(5, 8), (9, 15)}
    result = {date(year, m, d) for m, d in days}
    e = easter(year)
    return result | {e - timedelta(days=2), e + timedelta(days=1)}


def is_holiday(day):
    return day in holidays(day.year)


def shift_times(shift):
    """Start and end datetime; a shift ending before it starts ends next day."""
    start = datetime.combine(
        shift["day"], datetime.strptime(shift["time_from"], "%H:%M").time())
    end = datetime.combine(
        shift["day"], datetime.strptime(shift["time_to"], "%H:%M").time())
    if end <= start:
        end += timedelta(days=1)
    return start, end


def shift_hours(shift):
    """Estimated paid hours, split by what supplement they get."""
    start, end = shift_times(shift)
    total = (end - start).total_seconds() / 3600
    ratio = max(0.0, total - PAUSE_HOURS) / total * PAID_FACTOR if total else 0
    hours = {"all": 0.0, "sat": 0.0, "sun": 0.0, "holiday": 0.0, "night": 0.0}
    t, step = start, timedelta(minutes=1)
    while t < end:
        h = ratio / 60
        hours["all"] += h
        # Holiday supplement replaces the weekend one, they don't add up.
        if is_holiday(t.date()):
            hours["holiday"] += h
        elif t.weekday() == 5:
            hours["sat"] += h
        elif t.weekday() == 6:
            hours["sun"] += h
        if t.hour >= NIGHT[0] or t.hour < NIGHT[1]:
            hours["night"] += h
        t += step
    return hours


def shift_value(shift):
    """Estimated brutto of one shift, used to compare shifts."""
    h = shift_hours(shift)
    mh = min_hourly(shift["day"].year)
    return h["all"] * hourly_rate() + sum(
        h[k] * SUPPLEMENT[k] * mh for k in SUPPLEMENT)


def money(x, rounding=ROUND_HALF_UP):
    """Round to cents like payroll does (half up, not Python's float round)."""
    return float(Decimal(repr(x)).quantize(Decimal("0.01"), rounding))


def amount(hours, rate):
    """hours (rounded to 0.01 h, as on the payslip) x rate, in exact decimals."""
    exact = Decimal(repr(money(hours))) * Decimal(repr(rate))
    return float(exact.quantize(Decimal("0.01"), ROUND_HALF_UP))


DAY_TYPES = ("weekday", "sat", "sun", "holiday")


def day_type(shift):
    """Which kind of day a shift is paid as. A holiday on a weekend counts
    as a holiday, since that supplement is the one paid."""
    day = shift["day"]
    if is_holiday(day):
        return "holiday"
    return {5: "sat", 6: "sun"}.get(day.weekday(), "weekday")


def month_pay(shifts, year):
    """Brutto per kind of day (supplements included), then netto."""
    mh = min_hourly(year)
    days = {t: {"shifts": 0, "hours": 0.0, "gross": 0.0} for t in DAY_TYPES}
    hours = {t: {k: 0.0 for k in ("all", *SUPPLEMENT)} for t in DAY_TYPES}
    for s in shifts:
        t = day_type(s)
        days[t]["shifts"] += 1
        for k, v in shift_hours(s).items():
            hours[t][k] += v
    for t in DAY_TYPES:
        h = hours[t]
        days[t]["hours"] = h["all"]
        days[t]["gross"] = money(amount(h["all"], hourly_rate()) + sum(
            amount(h[k], SUPPLEMENT[k] * mh) for k in SUPPLEMENT))
    gross = money(sum(d["gross"] for d in days.values()))
    insurance = money(money(gross * OLD_AGE, ROUND_DOWN)
                      + money(gross * DISABILITY, ROUND_DOWN))
    taxable = gross - insurance - for_year(TAX_FREE, year)
    tax = money(max(0.0, taxable) * TAX, ROUND_DOWN)
    net = money(gross - insurance - tax)
    lunches = sum(1 for s in shifts if s.get("lunch"))
    return {
        "shifts": len(shifts), "days": days,
        "gross": gross, "insurance": insurance, "tax": tax,
        "net": net, "lunches": lunches,
        "payout": money(net - lunches * LUNCH_PRICE),
    }
