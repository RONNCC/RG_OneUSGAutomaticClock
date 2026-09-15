"""Week-to-date hours summary parsed from the clock page text/HTML."""

import re
from datetime import datetime

FALLBACK = "Weekly hours unavailable."
WEEKLY_CAP_HOURS = 20.0

def _page_text(ctx):
    """Raw page text/HTML from live browser, else None. Never raises."""
    try:
        driver = getattr(ctx, "driver", None)
        if driver is None:
            return None
        parts = []
        try:
            body = driver.execute_script("return document.body.innerText;")
            if body:
                parts.append(str(body))
        except Exception:
            pass
        try:
            gp = getattr(driver, "get_page_source", None)
            if callable(gp):
                src = gp()
            else:
                src = getattr(driver, "page_source", "")
                if callable(src):
                    src = src()
            if src:
                parts.append(str(src))
        except Exception:
            pass
        text = "\n".join(parts)
        return text if text.strip() else None
    except Exception:
        return None

def total_hours_from_text(text):
    """Numeric week total from raw page text/HTML. Single computation via _parse_week. None only when unreadable."""
    try:
        if not text or not str(text).strip():
            return None
        _, total = _parse_week(text)
        return total
    except Exception:
        return None

def get_weekly_total_hours(ctx):
    """Numeric week-to-date total from live page, else None. Never raises."""
    try:
        return total_hours_from_text(_page_text(ctx))
    except Exception:
        return None

_DAY_ALIASES = {
    "mon": ("mon", 0, "Mon"),
    "monday": ("mon", 0, "Mon"),
    "tue": ("tue", 1, "Tue"),
    "tues": ("tue", 1, "Tue"),
    "tuesday": ("tue", 1, "Tue"),
    "wed": ("wed", 2, "Wed"),
    "wednesday": ("wed", 2, "Wed"),
    "thu": ("thu", 3, "Thu"),
    "thur": ("thu", 3, "Thu"),
    "thurs": ("thu", 3, "Thu"),
    "thursday": ("thu", 3, "Thu"),
    "fri": ("fri", 4, "Fri"),
    "friday": ("fri", 4, "Fri"),
    "sat": ("sat", 5, "Sat"),
    "saturday": ("sat", 5, "Sat"),
    "sun": ("sun", 6, "Sun"),
    "sunday": ("sun", 6, "Sun"),
}

_DAY_PAT = re.compile(
    r"\b(mon(?:day)?|tue(?:s(?:day)?)?|wed(?:nesday)?|"
    r"thu(?:r(?:s(?:day)?)?)?|fri(?:day)?|sat(?:urday)?|sun(?:day)?)\b",
    re.IGNORECASE,
)
_TIME_PAT = re.compile(r"\b(\d{1,2}:\d{2}\s*(?:[AaPp]\s*\.?\s*[Mm]\s*\.?)?)")
_DATE_PAT = re.compile(r"\b(\d{1,2}/\d{1,2}(?:/\d{2,4})?)\b")
_HOURS_PAT = re.compile(r"\b(\d{1,3}\.\d{1,2})\s*(?:h(?:rs?|ours?)?)?\b", re.IGNORECASE)

_REPORTED_PAT = re.compile(r"Reported\s+(\d+\.\d+)", re.IGNORECASE)
_SUBMITTED_PAT = re.compile(r"Submitted\s+(\d+\.\d+)\s*Hours?", re.IGNORECASE)
_HEADER_DATE_PAT = re.compile(
    r"\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b"
    r"\s*,?\s*(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*"
    r"\s+(\d{1,2})\s*,?\s*(\d{4})",
    re.IGNORECASE,
)
_PUNCH_PAT = re.compile(
    r"\b(In|Out)\s*,?\s*(\d{1,2}:\d{2}(?::\d{2})?\s*[AaPp]\s*\.?\s*[Mm]\.?)"
    r"(?:\s+(\d{1,2}/\d{1,2}/\d{2,4}))?",
    re.IGNORECASE,
)
_MONTHS = {"jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
           "jul": 7, "aug": 8, "sep": 9, "sept": 9, "oct": 10, "nov": 11, "dec": 12}


def _to_minutes(raw):
    """'8:00 AM'/'5:00PM'/'17:00' -> minutes past midnight, else None."""
    up = re.sub(r"[\s.]+", "", raw.strip()).upper()
    for fmt in ("%I:%M:%S%p", "%I:%M%p", "%H:%M"):
        try:
            dt = datetime.strptime(up, fmt)
            return dt.hour * 60 + dt.minute
        except Exception:
            pass
    return None

def _fmt_time(minutes):
    h, m = divmod(minutes % (24 * 60), 60)
    ap = "AM" if h < 12 else "PM"
    h12 = h % 12 or 12
    return "%d:%02d %s" % (h12, m, ap)


def _header_label(text):
    m = _HEADER_DATE_PAT.search(text)
    if not m:
        return None
    day, mon, dd, yyyy = m.group(1), m.group(2), m.group(3), m.group(4)
    label = _DAY_ALIASES[day.lower()][2]
    num = _MONTHS[mon.lower()[:4] if mon.lower() != "sept" else "sept"]
    return "%s %02d/%02d" % (label, num, int(dd))


def _label_for_punch_date(mdY):
    try:
        p = mdY.split("/")
        mm, dd = int(p[0]), int(p[1])
        yy = int(p[2]) if len(p) > 2 else None
        if yy is not None:
            if yy < 100:
                yy += 2000
            wd = datetime(yy, mm, dd).strftime("%a")
            return "%s %02d/%02d" % (wd, mm, dd)
        return "%02d/%02d" % (mm, dd)
    except Exception:
        return None


def _parse_week(text):
    """Single computation: (per-day lines, week total). ([], 0.0) when no punches found."""
    if not text or not str(text).strip():
        return ([], 0.0)
    cleaned = re.sub(r"<[^>]+>", " ", str(text))
    cleaned = re.sub(r"&nbsp;?|&#160;|\xa0", " ", cleaned)
    total = None
    m = _REPORTED_PAT.search(cleaned)
    if m:
        total = float(m.group(1))
    else:
        m = _SUBMITTED_PAT.search(cleaned)
        if m:
            total = float(m.group(1))
    default_label = _header_label(cleaned)
    punches = []
    prev = None
    for m in _PUNCH_PAT.finditer(cleaned):
        kind = m.group(1).capitalize()
        mins = _to_minutes(m.group(2))
        if mins is None:
            continue
        if re.search(r"last\s+action", cleaned[max(0, m.start() - 20):m.start()], re.IGNORECASE):
            continue  # 'Last action' line repeats the glued punch
        if (kind, mins) == prev:
            continue  # glued repeat of the previous punch
        prev = (kind, mins)
        punches.append((kind, mins, m.group(3)))
    lines = []
    if punches:
        pairs = []
        pending = None
        for kind, mins, pdate in punches:
            if kind == "In":
                if pending is not None:
                    pairs.append((pending, None))
                pending = (mins, pdate)
            else:
                if pending is not None:
                    pairs.append((pending, (mins, pdate)))
                    pending = None
        if pending is not None:
            pairs.append((pending, None))
        pairs.sort(key=lambda p: p[0][0])
        for (in_mins, in_date), out in pairs:
            label = default_label
            if not label:
                label = _label_for_punch_date(in_date) if in_date else None
                if not label and out and out[1]:
                    label = _label_for_punch_date(out[1])
                label = label or "Day"
            if out is None:
                lines.append("%s: In %s, Out -- (still clocked in)"
                             % (label, _fmt_time(in_mins)))
            else:
                hrs = ((out[0] - in_mins) % (24 * 60)) / 60.0
                lines.append("%s: In %s, Out %s (%.2fh)"
                             % (label, _fmt_time(in_mins), _fmt_time(out[0]), hrs))
        if total is None:
            total = sum(((o[0] - i[0]) % (24 * 60)) / 60.0
                        for i, o in pairs if o is not None)
        return (lines, total if total is not None else 0.0)
    matches = list(_DAY_PAT.finditer(cleaned))
    rows = []
    for i, m in enumerate(matches):
        chunk = cleaned[m.start(): matches[i + 1].start()
                        if i + 1 < len(matches) else len(cleaned)]
        key, order, label = _DAY_ALIASES[m.group(0).lower()]
        date_m = _DATE_PAT.search(chunk)
        date = None
        if date_m:
            date = "/".join(date_m.group(1).split("/")[:2])
        times = []
        for t in _TIME_PAT.findall(chunk):
            mins = _to_minutes(t)
            if mins is not None:
                times.append(mins)
        day_hours = None
        in_s = out_s = None
        if len(times) >= 2:
            n = len(times) // 2
            day_hours = sum(
                (times[2 * k + 1] - times[2 * k]) % (24 * 60)
                for k in range(n)
            ) / 60.0
            in_s = _fmt_time(times[0])
            out_s = _fmt_time(times[-1])
        elif len(times) == 1:
            reported = _HOURS_PAT.findall(chunk)
            if reported:
                day_hours = float(reported[-1])
            else:
                day_hours = 0.0
            in_s = _fmt_time(times[0])
        else:
            reported = _HOURS_PAT.findall(chunk)
            if reported:
                day_hours = float(reported[-1])
        if day_hours is None:
            continue
        head = label + (" " + date if date else "")
        if in_s is not None and out_s is not None:
            rows.append((order, i, "%s: In %s, Out %s (%.2fh)"
                               % (head, in_s, out_s, day_hours), day_hours))
        elif in_s is not None:
            rows.append((order, i, "%s: In %s, Out -- (%.2fh)"
                               % (head, in_s, day_hours), day_hours))
        else:
            rows.append((order, i, "%s: %.2fh" % (head, day_hours), day_hours))
    if not rows:
        return ([], total if total is not None else 0.0)
    rows.sort(key=lambda r: (r[0], r[1]))
    if total is None:
        total = sum(r[3] for r in rows)
    return ([r[2] for r in rows], total)
def summarize_punches(text):
    """Parse page text/HTML into per-day in/out lines plus week total."""
    try:
        lines, total = _parse_week(text)
        if not lines:
            return "No punches found.\nWeek total so far: %.2fh" % (total if total is not None else 0.0)
        return "\n".join(lines + ["Week total so far: %.2fh" % total])
    except Exception:
        return FALLBACK


def format_weekly_summary(ctx):
    """Read week-to-date hours from a live browser context. Never raises."""
    try:
        text = _page_text(ctx)
        if text is None:
            return FALLBACK
        return summarize_punches(text)
    except Exception:
        return FALLBACK


def format_weekly_total(ctx):
    """Single-line total with cap remaining, e.g. 'Week total so far: 1.71h (18.29h left of 20h cap)'. Never raises."""
    try:
        text = _page_text(ctx)
        if text is None:
            return FALLBACK
        total = total_hours_from_text(text)
        if total is None:
            return FALLBACK
        line = "Week total so far: %.2fh" % total
        left = WEEKLY_CAP_HOURS - total
        if left < 0:
            return "%s (over 20h cap by %.2fh, do not clock in)" % (line, -left)
        if left == 0:
            return "%s (at 20h cap, do not clock in)" % line
        return "%s (%.2fh left of 20h cap)" % (line, left)
    except Exception:
        return FALLBACK

