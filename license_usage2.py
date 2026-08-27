"""
License utilization report.

Tracks concurrent OUT/IN usage against owned seat counts from ptcstatus,
reports DENIED events where all seats were already in use, prints a text
summary, and writes an HTML dashboard that opens in the browser.
"""

import argparse
import datetime
import html
import json
import os
import re
import textwrap
import webbrowser
from collections import defaultdict
from pathlib import Path

# ---------------------------------------------------------------------------
# Defaults — edit these so you can run the script with no prompts
# ---------------------------------------------------------------------------
# DEFAULT_LOG_FILE = r"C:\dev\License usage 2\ptc_d.log.big"

# DEFAULT_LOG_FILE = r"C:\dev\License usage 2\rheinmetall\ptc_d.log"

DEFAULT_LOG_FILE = r"C:\dev\License usage 2\lanteris\ptclmgrd.log"

DEFAULT_STATUS_FILE = ""  # leave blank to ignore owned-seat file; usage-only report
# DEFAULT_LICENSES = "PROE_DesignEss"  # blank = report every feature found in the log
DEFAULT_LICENSES = ""  # blank = report every feature found in the log
DEFAULT_LOOKBACK_DAYS = 180   # 0 = entire log
DEFAULT_REPORTING_DAYS = 0  # 0 = entire log
# user@computer filter; set to "" for all users (recommended for company-wide utilization)
DEFAULT_USERS = ""
# Optional: "PROE_DesignEss=10|10113=5" when you know real owned counts
DEFAULT_CAPACITY = ""
DEFAULT_LOOKUP_FILE = str(Path(__file__).resolve().parent / "license_lookup.txt")
DEFAULT_HTML_FILE = str(Path(__file__).resolve().parent / "license_report.html")
# ---------------------------------------------------------------------------

TIMESTAMP_PATTERN = re.compile(r"TIMESTAMP\s+(\d{1,2}/\d{1,2}/\d{4})")
START_DATE_PATTERN = re.compile(
    r"Start-Date:\s+\w+\s+(\w+)\s+(\d{1,2})\s+(\d{4})\s+"
)
TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})")
OUT_PATTERN = re.compile(r'OUT:\s+"([^"]+)"\s+(\S+)')
IN_PATTERN = re.compile(r'IN:\s+"([^"]+)"\s+(\S+)')
DENIED_PATTERN = re.compile(r'DENIED:\s+"([^"]+)"\s+(\S+)\s+\(([^)]*)\)')
STATUS_LICENSE_PATTERN = re.compile(r"^\s*(\S+)\s+(\d+)\s+(\d+)\s*$")
SERVER_STARTED_PATTERN = re.compile(r"Server started on")
EXPIRED_PATTERN = re.compile(r"EXPIRED:\s+(\S+)")
EXPIRES_WARNING_PATTERN = re.compile(
    r"Warning:\s+(\S+)\s+expires\s+(\d{1,2}-\w{3}-\d{4})", re.IGNORECASE
)
SATURATION_DENIAL = "Licensed number of users already reached"
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
REPORT_WIDTH = 72


def get_input(prompt, default):
    user_input = input(f"{prompt} [{default}]: ").strip()
    return user_input if user_input else default


def load_license_lookup(path):
    """Load feature id/name → product name from license_lookup.txt."""
    lookup = {}
    if not path or not os.path.isfile(path):
        return lookup
    with open(path, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            lookup[key.strip()] = value.strip()
    return lookup


def display_name(feature, lookup):
    friendly = lookup.get(feature)
    if friendly:
        return friendly
    return feature


def parse_owned_seats(status_file):
    """Parse ptcstatus output. Owned seats = In Use + Free."""
    owned = {}
    in_section = False

    with open(status_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            if "License    In Use   Free" in line:
                in_section = True
                continue
            if not in_section:
                continue
            if line.strip().startswith("(") or not line.strip():
                continue
            if line.strip().startswith("*") or line.strip().startswith("^"):
                break

            match = STATUS_LICENSE_PATTERN.match(line)
            if match:
                name, in_use, free = match.groups()
                owned[name] = int(in_use) + int(free)

    return owned


def parse_capacity_override(text):
    """Parse Feature=N|Feature2=M style overrides."""
    if not text:
        return {}
    result = {}
    for part in text.split("|"):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(f"Capacity override must be Feature=N, got: {part}")
        name, value = part.split("=", 1)
        result[name.strip()] = int(value.strip())
    return result


class FeatureStats:
    def __init__(self, name, owned):
        self.name = name
        self.owned = owned  # explicit override from config/status, if any
        self.concurrent = 0  # unique user@host currently holding a seat
        self.holders = defaultdict(int)  # user@host -> open checkout count
        self.peak_overall = 0
        self.peak_overall_when = None
        self.daily_peak = defaultdict(int)
        self.denial_events = 0  # deduped saturation denials
        self.denial_raw = 0
        self.denial_by_day = defaultdict(int)
        self.denial_users = defaultdict(int)
        self.denial_holder_samples = []  # concurrent holders at each denial event
        self.expiry_events = []  # (date, detail) when feature expired / reread
        self.out_count = 0
        self.in_count = 0
        self.users_seen = set()
        self._last_denial_key = None

    def reset_concurrency(self):
        self.concurrent = 0
        self.holders.clear()

    def checkout(self, user):
        """Record OUT. Same user@host already holding a seat does not add another."""
        prev = self.holders[user]
        self.holders[user] = prev + 1
        if prev == 0:
            self.concurrent += 1
            return True  # new seat taken
        return False  # same user relaunch; still one seat

    def checkin(self, user):
        """Record IN. Seat freed only when this user@host has no remaining checkouts."""
        prev = self.holders.get(user, 0)
        if prev <= 0:
            return False
        if prev == 1:
            del self.holders[user]
            self.concurrent = max(0, self.concurrent - 1)
            return True  # seat freed
        self.holders[user] = prev - 1
        return False  # still holding via another Creo session

    def predict_owned_seats(self):
        """
        Predict owned seats.

        Hard rule: successful concurrent checkouts cannot exceed owned seats,
        so owned >= historical peak. A saturation DENIED means the pool was
        full at that moment (holders in use then). If that number is lower
        than peak, capacity changed (e.g. expiry) — report both.
        """
        if self.owned is not None:
            return {
                "count": self.owned,
                "confidence": "configured",
                "detail": "from DEFAULT_CAPACITY / --capacity / status file",
                "lower_bound": self.owned,
                "at_denial": None,
                "note": None,
            }

        samples = [n for n in self.denial_holder_samples if n > 0]
        peak = self.peak_overall

        if samples:
            counts = defaultdict(int)
            for n in samples:
                counts[n] += 1
            at_denial = max(counts.items(), key=lambda kv: (kv[1], kv[0]))[0]

            # Owned cannot be below a successfully observed peak
            predicted = max(at_denial, peak)
            distinct = sorted(counts.keys())

            if at_denial >= peak:
                detail = (
                    f"DENIED when {at_denial} unique holder(s) were in use "
                    f"(pool full). Matches peak {peak}. "
                    f"Owned seats = {predicted}."
                )
                note = None
                confidence = "high"
            else:
                detail = (
                    f"Owned seats at least {peak} (peak concurrent checkouts "
                    f"that succeeded). DENIED later with only {at_denial} "
                    f"holder(s) in use — pool was full then at {at_denial}."
                )
                note = (
                    f"Capacity changed over the log: historical peak {peak}, "
                    f"but denials saw a full pool at {at_denial}. "
                    f"Primary prediction uses {predicted} (cannot be below peak)."
                )
                if self.expiry_events:
                    last = self.expiry_events[-1]
                    when = f" on {last[0]}" if last[0] else ""
                    note += f" Log also shows license expiry/reread ({last[1]}{when})."
                confidence = "high"

            if len(distinct) > 1:
                detail += f" Denial in-use samples varied: {distinct}."

            return {
                "count": predicted,
                "confidence": confidence,
                "detail": detail,
                "lower_bound": peak,
                "at_denial": at_denial,
                "note": note,
            }

        if peak > 0:
            return {
                "count": None,
                "confidence": "low",
                "detail": (
                    f"no saturation denials — exact owned unknown; "
                    f"at least {peak} (peak concurrent that succeeded)"
                ),
                "lower_bound": peak,
                "at_denial": None,
                "note": None,
            }

        return {
            "count": None,
            "confidence": "none",
            "detail": "no usage and no denials — cannot predict",
            "lower_bound": 0,
            "at_denial": None,
            "note": None,
        }

    def effective_capacity(self):
        """Capacity used for UNDER/OVER judgment."""
        pred = self.predict_owned_seats()
        if pred["count"] is not None:
            return pred["count"], f"predicted owned ({pred['confidence']})"
        return None, None


def in_report_window(event_date, now, lookback_days, reporting_days):
    if event_date is None:
        return False
    if lookback_days <= 0 and reporting_days <= 0:
        return True

    days_ago = (now.date() - event_date).days
    if lookback_days > 0 and days_ago > lookback_days:
        return False
    if reporting_days > 0 and lookback_days > 0:
        if days_ago < (lookback_days - reporting_days):
            return False
    elif reporting_days > 0 and days_ago >= reporting_days:
        return False
    return True


def parse_log(log_file, features, owned_seats, lookback_days=0, reporting_days=0,
              user_computers=None):
    # Empty features list => discover and report all features in the log
    feature_set = set(features) if features else None
    user_set = set(user_computers) if user_computers else None
    stats = {
        name: FeatureStats(name, owned_seats.get(name))
        for name in (features or [])
    }

    def get_stats(feature):
        if feature not in stats:
            stats[feature] = FeatureStats(feature, owned_seats.get(feature))
        return stats[feature]

    now = datetime.datetime.now()
    current_date = None
    current_time_str = None
    line_no = 0

    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            line_no += 1

            ts_match = TIMESTAMP_PATTERN.search(line)
            if ts_match:
                try:
                    current_date = datetime.datetime.strptime(
                        ts_match.group(1), "%m/%d/%Y"
                    ).date()
                except ValueError:
                    pass
                continue

            start_match = START_DATE_PATTERN.search(line)
            if start_match:
                mon, day, year = start_match.groups()
                month_num = MONTHS.get(mon)
                if month_num:
                    try:
                        current_date = datetime.date(int(year), month_num, int(day))
                    except ValueError:
                        pass

            if SERVER_STARTED_PATTERN.search(line):
                for s in stats.values():
                    s.reset_concurrency()
                continue

            expired_match = EXPIRED_PATTERN.search(line)
            if expired_match:
                feature = expired_match.group(1)
                if feature_set is None or feature in feature_set:
                    s = get_stats(feature)
                    s.expiry_events.append((current_date, f"EXPIRED: {feature}"))
                    s.reset_concurrency()

            expires_warn = EXPIRES_WARNING_PATTERN.search(line)
            if expires_warn:
                feature, exp_date = expires_warn.groups()
                if feature_set is None or feature in feature_set:
                    s = get_stats(feature)
                    s.expiry_events.append(
                        (current_date, f"expires {exp_date}")
                    )

            time_match = TIME_PATTERN.match(line.lstrip())
            if time_match:
                current_time_str = (
                    f"{int(time_match.group(1)):02d}:"
                    f"{time_match.group(2)}:"
                    f"{time_match.group(3)}"
                )

            out_match = OUT_PATTERN.search(line)
            if out_match:
                feature, user = out_match.groups()
                if feature_set is not None and feature not in feature_set:
                    continue
                if user_set is not None and user not in user_set:
                    continue
                s = get_stats(feature)
                took_new_seat = s.checkout(user)

                if in_report_window(current_date, now, lookback_days, reporting_days):
                    s.out_count += 1
                    s.users_seen.add(user)
                    if took_new_seat:
                        if s.concurrent > s.daily_peak[current_date]:
                            s.daily_peak[current_date] = s.concurrent
                        if s.concurrent > s.peak_overall:
                            s.peak_overall = s.concurrent
                            s.peak_overall_when = (current_date, current_time_str, user)
                continue

            in_match = IN_PATTERN.search(line)
            if in_match:
                feature, user = in_match.groups()
                if feature_set is not None and feature not in feature_set:
                    continue
                if user_set is not None and user not in user_set:
                    continue
                s = get_stats(feature)
                s.checkin(user)
                if in_report_window(current_date, now, lookback_days, reporting_days):
                    s.in_count += 1
                continue

            denied_match = DENIED_PATTERN.search(line)
            if denied_match:
                feature, user, reason = denied_match.groups()
                if feature_set is not None and feature not in feature_set:
                    continue
                if user_set is not None and user not in user_set:
                    continue
                if SATURATION_DENIAL not in reason:
                    continue
                if not in_report_window(current_date, now, lookback_days, reporting_days):
                    continue

                s = get_stats(feature)
                s.denial_raw += 1
                # Collapse Creo retry bursts: same feature/user/second = one event
                denial_key = (current_date, current_time_str, feature, user)
                if denial_key != s._last_denial_key:
                    s._last_denial_key = denial_key
                    s.denial_events += 1
                    s.denial_by_day[current_date] += 1
                    s.denial_users[user] += 1
                    # DENIED => pool full => holders in use = owned seats
                    s.denial_holder_samples.append(s.concurrent)

    return stats, line_no


def classify_feature(s):
    """
    Return (label, explanation) for under vs over utilization.

    OVER  = saturation denials (people could not get a license)
    UNDER = peak concurrent below known/inferred capacity, and no denials
    FULL  = peak reached capacity but nobody was denied
    """
    capacity, capacity_source = s.effective_capacity()
    denial_days = len(s.denial_by_day)

    if s.peak_overall == 0 and s.denial_events == 0:
        return (
            "UNUSED",
            "No checkouts in this window. Seats were idle, or this feature was not used.",
        )

    if s.denial_events > 0:
        if denial_days <= 2:
            rarity = "rare / concentrated"
        elif denial_days <= 10:
            rarity = "occasional"
        else:
            rarity = "repeated"
        pred = s.predict_owned_seats()
        parts = [
            f"FlexLM denied checkouts {s.denial_events} time(s) "
            f"across {denial_days} day(s) ({rarity} shortage).",
            f"Peak holders: {s.peak_overall}.",
        ]
        if pred["count"] is not None:
            parts.append(
                f"Predicted owned: {pred['count']} "
                f"({pred['confidence']} confidence)."
            )
            if pred.get("at_denial") is not None and pred["at_denial"] != pred["count"]:
                parts.append(
                    f"In use at DENIED was {pred['at_denial']} "
                    f"(capacity likely changed since peak)."
                )
        parts.append("People could not get a seat when the pool was full.")
        if pred.get("note"):
            parts.append(pred["note"])
        return ("OVER-UTILIZED — ran out of licenses", " ".join(parts))

    if capacity is not None:
        unused = capacity - s.peak_overall
        pct = (100.0 * s.peak_overall / capacity) if capacity else 0
        if s.peak_overall < capacity:
            return (
                "UNDER-UTILIZED",
                f"Peak used {s.peak_overall} of {capacity} seats "
                f"({pct:.0f}%; {capacity_source}). "
                f"{unused} seat(s) were never needed at the same time. "
                f"No denials — nobody was blocked.",
            )
        return (
            "FULLY UTILIZED — no denials",
            f"Peak reached {s.peak_overall} of {capacity} seats "
            f"({capacity_source}), but nobody was denied a seat.",
        )

    return (
        "NO SHORTAGE EVIDENCE — exact owned count unknown",
        f"Peak holders: {s.peak_overall}. No saturation denials. "
        f"They own at least {s.peak_overall} (lower bound only). "
        f"Without denials the log cannot prove the exact pool size.",
    )


def status_bucket(label):
    """Map classify_feature label to a short status key for charts."""
    if "OVER" in label:
        return "over"
    if "UNDER" in label:
        return "under"
    if "FULLY" in label:
        return "full"
    if "UNUSED" in label:
        return "unused"
    return "unknown"


def plain_meaning(s, label, lookup):
    """One short paragraph a non-expert can act on."""
    name = display_name(s.name, lookup)
    pred = s.predict_owned_seats()
    bucket = status_bucket(label)

    if bucket == "unused":
        return (
            f"{name} had no checkouts in this window. If you pay for these seats, "
            f"they look unused here — or this feature simply was not needed."
        )
    if bucket == "over":
        owned_bit = ""
        if pred["count"] is not None:
            owned_bit = (
                f" The pool looks like about {pred['count']} seat(s) "
                f"(from denials / peak evidence)."
            )
        return (
            f"{name} ran out of seats. FlexLM blocked people "
            f"{s.denial_events} time(s) on {len(s.denial_by_day)} day(s). "
            f"Peak simultaneous users: {s.peak_overall}.{owned_bit} "
            f"Meaning: demand exceeded supply — consider more seats or "
            f"staggering who uses this option."
        )
    if bucket == "under":
        cap = pred["count"]
        spare = (cap - s.peak_overall) if cap else None
        spare_bit = f" About {spare} seat(s) sat unused even at the busiest moment." if spare else ""
        return (
            f"{name} never ran out. Peak was {s.peak_overall} concurrent user(s)"
            f"{f' of {cap} owned' if cap else ''}.{spare_bit} "
            f"Meaning: capacity looks comfortable for this window."
        )
    if bucket == "full":
        return (
            f"{name} hit peak equal to owned capacity ({s.peak_overall}), "
            f"but nobody was denied. Meaning: fully used at times, yet the "
            f"pool still covered demand — watch this one if usage grows."
        )
    return (
        f"{name} peaked at {s.peak_overall} concurrent user(s) with no denials. "
        f"Meaning: no proof of shortage; exact owned count unknown without "
        f"denials or a configured capacity."
    )


def ordered_features(stats):
    def sort_key(s):
        label, _ = classify_feature(s)
        over = 0 if "OVER" in label else 1
        return (over, -s.denial_events, -s.peak_overall, -s.out_count, s.name)

    return sorted(stats.values(), key=sort_key)


def build_executive_summary(stats, lookup):
    """Short bullets for the top of the HTML report."""
    ordered = ordered_features(stats)
    counts = defaultdict(int)
    total_denials = 0
    for s in ordered:
        label, _ = classify_feature(s)
        counts[status_bucket(label)] += 1
        total_denials += s.denial_events

    bullets = []
    n = len(ordered)
    bullets.append(
        f"Analyzed {n} license feature(s) from the FlexLM log."
    )
    if counts["over"]:
        names = [
            display_name(s.name, lookup)
            for s in ordered
            if status_bucket(classify_feature(s)[0]) == "over"
        ][:5]
        extra = "" if counts["over"] <= 5 else f" (+{counts['over'] - 5} more)"
        bullets.append(
            f"{counts['over']} feature(s) ran out of seats "
            f"({total_denials} denial event(s) total): "
            + ", ".join(names) + extra + ". These blocked real users."
        )
    else:
        bullets.append(
            "No feature showed saturation denials — nobody was blocked "
            "because the license pool was full."
        )
    if counts["under"]:
        bullets.append(
            f"{counts['under']} feature(s) look under-utilized "
            f"(peak below predicted owned, no denials). Possible spare capacity."
        )
    if counts["full"]:
        bullets.append(
            f"{counts['full']} feature(s) reached full capacity at peak "
            f"but still had no denials — tight but covered."
        )
    if counts["unused"]:
        bullets.append(
            f"{counts['unused']} feature(s) had no checkouts in this window."
        )
    if counts["unknown"]:
        bullets.append(
            f"{counts['unknown']} feature(s) have usage but no denials and "
            f"no known owned count — only a lower bound (peak) is known."
        )
    return bullets, dict(counts), total_denials


def _wrap(text, indent=0, width=REPORT_WIDTH):
    prefix = " " * indent
    return textwrap.fill(
        text,
        width=width,
        initial_indent=prefix,
        subsequent_indent=prefix,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _hr(char="-", width=REPORT_WIDTH):
    print(char * width)


def _kv(label, value, label_width=18):
    """Print a short label/value pair; wrap value if needed."""
    value = str(value)
    pad = f"  {label:<{label_width}} "
    available = REPORT_WIDTH - len(pad)
    if len(value) <= available:
        print(f"{pad}{value}")
        return
    print(pad.rstrip())
    print(_wrap(value, indent=4))


def print_report(stats, lookback_days, reporting_days, status_file, log_file, lines_read,
                 user_computers=None, lookup=None):
    lookup = lookup or {}
    _hr("=")
    print("LICENSE USAGE REPORT")
    print("Under vs over utilization")
    _hr("=")
    print()
    _kv("Log", log_file)
    _kv("Status", status_file or "(not used)")
    _kv("Lines", f"{lines_read:,}")
    _kv("Features", len(stats))
    if lookback_days or reporting_days:
        _kv("Window", f"lookback={lookback_days}, reporting={reporting_days}")
    else:
        _kv("Window", "entire log")
    if user_computers:
        print(_wrap("Users: " + ", ".join(user_computers), indent=2))
    else:
        _kv("Users", "all")
    print()
    print(_wrap(
        "OVER = denials prove seats ran out. "
        "UNDER = peak below predicted owned, no denials. "
        "Owned >= peak concurrent (successful checkouts cannot exceed seats). "
        "DENIED means pool full at that moment.",
        indent=2,
    ))
    print()

    ordered = ordered_features(stats)

    for s in ordered:
        avg_daily_peak = (
            sum(s.daily_peak.values()) / len(s.daily_peak) if s.daily_peak else 0
        )
        label, explanation = classify_feature(s)
        pred = s.predict_owned_seats()
        title = display_name(s.name, lookup)
        subtitle = s.name if title != s.name else None

        _hr()
        print()
        print(f"  {title}")
        if subtitle:
            print(f"  ({subtitle})")
        print("  " + ("." * (REPORT_WIDTH - 2)))
        print()
        print(f"  >>> {label}")
        print()
        print(_wrap(explanation, indent=2))
        print()
        print(_wrap("Meaning: " + plain_meaning(s, label, lookup), indent=2))
        print()

        print("  Owned seats")
        if pred["count"] is not None:
            _kv("Predicted", f"{pred['count']}  [{pred['confidence']} confidence]")
        else:
            _kv("Predicted", f"unknown exact (at least {pred['lower_bound']})")
        print(_wrap(pred["detail"], indent=4))
        if pred.get("at_denial") is not None and pred["at_denial"] != pred["count"]:
            _kv("In use at DENIED", pred["at_denial"])
            print(_wrap(
                "Full pool at denial time (may be after capacity changed).",
                indent=4,
            ))
        if pred.get("note"):
            print(_wrap("Note: " + pred["note"], indent=4))
        if pred["count"] is not None and s.peak_overall > 0:
            pct = 100.0 * s.peak_overall / pred["count"]
            _kv(
                "Peak vs predicted",
                f"{s.peak_overall} / {pred['count']}  ({pct:.0f}%)",
            )
        print()

        print("  Usage")
        _kv("Peak concurrent", f"{s.peak_overall} unique user@host")
        if s.peak_overall_when:
            d, t, u = s.peak_overall_when
            _kv("Peak when", f"{d} at {t}")
            _kv("Triggered by", u)
            print(_wrap(
                "That user was the checkout that raised holders to the peak, "
                "not the only user. Same user@host = 1 seat.",
                indent=4,
            ))
        _kv("Avg daily peak", f"{avg_daily_peak:.1f}")
        _kv("Active days", len(s.daily_peak))
        _kv("Distinct users", len(s.users_seen))
        _kv("OUT / IN", f"{s.out_count:,} / {s.in_count:,}")
        print()

        print("  Denials (ran out?)")
        if s.denial_events > 0:
            _kv("Answer", "YES — FlexLM refused checkouts")
        else:
            _kv("Answer", "NO — no saturation denials in this window")
        _kv("Events", f"{s.denial_events}  ({s.denial_raw} raw log lines)")
        _kv("Days with denials", len(s.denial_by_day))
        print()

        if s.daily_peak:
            print("  Top peak days")
            top_days = sorted(s.daily_peak.items(), key=lambda x: (-x[1], x[0]))[:5]
            for day, peak in top_days:
                mark = ""
                if pred["count"] is not None:
                    if peak == pred["count"]:
                        mark = "  (at predicted owned)"
                    elif peak > pred["count"]:
                        mark = "  (above predicted — unexpected)"
                print(f"    {day}   {peak} concurrent{mark}")
            print()

        if s.denial_by_day:
            print("  Top denial days (people blocked)")
            top_denial_days = sorted(
                s.denial_by_day.items(), key=lambda x: (-x[1], x[0])
            )[:5]
            for day, count in top_denial_days:
                print(f"    {day}   {count} event(s)")
            print()

        if s.denial_users:
            print("  Users most often denied")
            top_users = sorted(
                s.denial_users.items(), key=lambda x: (-x[1], x[0])
            )[:5]
            for user, count in top_users:
                print(f"    {count:>4}  {user}")
            print()

    _hr("=")
    print()
    print("Notes")
    print(_wrap(
        "Predicted owned is at least the peak concurrent checkouts that "
        "succeeded (cannot license fewer seats than were in use). DENIED "
        "means the pool was full then; if in-use at DENIED is lower than "
        "peak, capacity changed (e.g. expiry). Same user@host = 1 seat.",
        indent=2,
    ))
    _hr("=")


def write_html_report(
    stats,
    lookback_days,
    reporting_days,
    status_file,
    log_file,
    lines_read,
    html_path,
    user_computers=None,
    lookup=None,
):
    """Write a self-contained HTML dashboard and return its path."""
    lookup = lookup or {}
    ordered = ordered_features(stats)
    bullets, status_counts, total_denials = build_executive_summary(stats, lookup)
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")

    if lookback_days or reporting_days:
        window = f"lookback={lookback_days}d, reporting={reporting_days}d"
    else:
        window = "entire log"

    # Chart data
    status_labels = [
        ("over", "Ran out (OVER)"),
        ("under", "Spare capacity (UNDER)"),
        ("full", "Full but no denials"),
        ("unused", "Unused"),
        ("unknown", "Unknown owned"),
    ]
    pie_labels = []
    pie_values = []
    pie_colors = {
        "over": "#c0392b",
        "under": "#1f6f8b",
        "full": "#2f7d4a",
        "unused": "#7a756c",
        "unknown": "#b08900",
    }
    pie_color_list = []
    for key, lab in status_labels:
        n = status_counts.get(key, 0)
        if n:
            pie_labels.append(lab)
            pie_values.append(n)
            pie_color_list.append(pie_colors[key])

    # Peak / denial bars — top features by activity
    chart_features = [
        s for s in ordered
        if s.peak_overall > 0 or s.denial_events > 0 or s.out_count > 0
    ][:20]
    peak_labels = [display_name(s.name, lookup) for s in chart_features]
    peak_values = [s.peak_overall for s in chart_features]
    owned_values = []
    for s in chart_features:
        pred = s.predict_owned_seats()
        owned_values.append(pred["count"] if pred["count"] is not None else None)

    denial_features = [s for s in ordered if s.denial_events > 0][:15]
    denial_labels = [display_name(s.name, lookup) for s in denial_features]
    denial_values = [s.denial_events for s in denial_features]

    # Utilization % where owned known
    util_labels = []
    util_values = []
    for s in ordered:
        pred = s.predict_owned_seats()
        if pred["count"] and pred["count"] > 0 and s.peak_overall > 0:
            util_labels.append(display_name(s.name, lookup))
            util_values.append(round(100.0 * s.peak_overall / pred["count"], 1))
    util_labels = util_labels[:15]
    util_values = util_values[:15]

    feature_cards = []
    for s in ordered:
        label, explanation = classify_feature(s)
        pred = s.predict_owned_seats()
        bucket = status_bucket(label)
        avg_daily_peak = (
            sum(s.daily_peak.values()) / len(s.daily_peak) if s.daily_peak else 0
        )
        top_days = sorted(s.daily_peak.items(), key=lambda x: (-x[1], x[0]))[:5]
        top_denial_days = sorted(
            s.denial_by_day.items(), key=lambda x: (-x[1], x[0])
        )[:5]
        top_users = sorted(s.denial_users.items(), key=lambda x: (-x[1], x[0]))[:5]
        peak_when = None
        if s.peak_overall_when:
            d, t, u = s.peak_overall_when
            peak_when = f"{d} at {t} (triggered by {u})"

        feature_cards.append({
            "id": s.name,
            "title": display_name(s.name, lookup),
            "bucket": bucket,
            "label": label,
            "explanation": explanation,
            "meaning": plain_meaning(s, label, lookup),
            "peak": s.peak_overall,
            "peak_when": peak_when,
            "avg_daily_peak": round(avg_daily_peak, 1),
            "active_days": len(s.daily_peak),
            "users": len(s.users_seen),
            "out": s.out_count,
            "inn": s.in_count,
            "denials": s.denial_events,
            "denial_days": len(s.denial_by_day),
            "owned": pred["count"],
            "owned_lower": pred["lower_bound"],
            "owned_confidence": pred["confidence"],
            "owned_detail": pred["detail"],
            "top_days": [(str(d), p) for d, p in top_days],
            "top_denial_days": [(str(d), c) for d, c in top_denial_days],
            "top_users": top_users,
        })

    chart_payload = {
        "pieLabels": pie_labels,
        "pieValues": pie_values,
        "pieColors": pie_color_list,
        "peakLabels": peak_labels,
        "peakValues": peak_values,
        "ownedValues": owned_values,
        "denialLabels": denial_labels,
        "denialValues": denial_values,
        "utilLabels": util_labels,
        "utilValues": util_values,
    }

    def esc(text):
        return html.escape(str(text))

    card_html_parts = []
    for card in feature_cards:
        owned_txt = (
            f"{card['owned']} ({card['owned_confidence']})"
            if card["owned"] is not None
            else f"unknown (at least {card['owned_lower']})"
        )
        peak_pct = ""
        if card["owned"] and card["owned"] > 0:
            peak_pct = f" — {100.0 * card['peak'] / card['owned']:.0f}% of predicted owned"

        days_rows = "".join(
            f"<li><span>{esc(d)}</span><strong>{p}</strong></li>"
            for d, p in card["top_days"]
        )
        denial_rows = "".join(
            f"<li><span>{esc(d)}</span><strong>{c}</strong></li>"
            for d, c in card["top_denial_days"]
        )
        user_rows = "".join(
            f"<li><span>{esc(u)}</span><strong>{c}</strong></li>"
            for u, c in card["top_users"]
        )

        side_blocks = []
        if days_rows:
            side_blocks.append(
                f'<div class="mini"><h4>Busiest days (peak concurrent)</h4>'
                f'<ul class="kv">{days_rows}</ul></div>'
            )
        if denial_rows:
            side_blocks.append(
                f'<div class="mini"><h4>Days people were blocked</h4>'
                f'<ul class="kv">{denial_rows}</ul></div>'
            )
        if user_rows:
            side_blocks.append(
                f'<div class="mini"><h4>Users denied most often</h4>'
                f'<ul class="kv">{user_rows}</ul></div>'
            )
        side = "".join(side_blocks)

        card_html_parts.append(f"""
<article class="feature bucket-{esc(card['bucket'])}" id="f-{esc(card['id'])}">
  <header>
    <div>
      <h3>{esc(card['title'])}</h3>
      <p class="fid">{esc(card['id'])}</p>
    </div>
    <span class="badge badge-{esc(card['bucket'])}">{esc(card['label'])}</span>
  </header>
  <p class="meaning"><strong>What this means:</strong> {esc(card['meaning'])}</p>
  <p class="detail">{esc(card['explanation'])}</p>
  <div class="metrics">
    <div><span>Peak concurrent</span><strong>{card['peak']}{esc(peak_pct)}</strong></div>
    <div><span>Predicted owned</span><strong>{esc(owned_txt)}</strong></div>
    <div><span>Denial events</span><strong>{card['denials']}</strong></div>
    <div><span>Distinct users</span><strong>{card['users']}</strong></div>
    <div><span>Active days</span><strong>{card['active_days']}</strong></div>
    <div><span>Avg daily peak</span><strong>{card['avg_daily_peak']}</strong></div>
  </div>
  {"<p class='muted'>Peak when: " + esc(card['peak_when']) + "</p>" if card['peak_when'] else ""}
  <p class="muted">{esc(card['owned_detail'])}</p>
  <div class="side-grid">{side}</div>
</article>
""")

    bullets_html = "".join(f"<li>{esc(b)}</li>" for b in bullets)
    users_txt = ", ".join(user_computers) if user_computers else "all users"
    chart_json = json.dumps(chart_payload)

    show_denial_chart = "true" if denial_values else "false"
    show_util_chart = "true" if util_values else "false"
    show_peak_chart = "true" if peak_values else "false"
    show_pie = "true" if pie_values else "false"

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>License Usage Report</title>
<script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.7/dist/chart.umd.min.js"></script>
<style>
:root {{
  --bg: #e8edf2;
  --ink: #15202b;
  --muted: #5a6a78;
  --card: #ffffff;
  --line: #c5d0da;
  --accent: #0b6e4f;
  --over: #c0392b;
  --under: #1f6f8b;
  --full: #2f7d4a;
  --unused: #7a756c;
  --unknown: #b08900;
  --radius: 10px;
  --font: "Segoe UI", "Helvetica Neue", sans-serif;
}}
* {{ box-sizing: border-box; }}
body {{
  margin: 0;
  font-family: var(--font);
  color: var(--ink);
  background:
    radial-gradient(ellipse 80% 50% at 10% -10%, #d5e8e2 0%, transparent 55%),
    radial-gradient(ellipse 60% 40% at 100% 0%, #d7e0ea 0%, transparent 50%),
    var(--bg);
  line-height: 1.45;
}}
.wrap {{ max-width: 1120px; margin: 0 auto; padding: 28px 20px 64px; }}
header.hero {{
  margin-bottom: 28px;
  padding-bottom: 20px;
  border-bottom: 1px solid var(--line);
}}
header.hero h1 {{
  margin: 0 0 6px;
  font-size: 1.85rem;
  letter-spacing: -0.02em;
  color: var(--accent);
}}
header.hero .tagline {{
  margin: 0 0 14px;
  font-size: 1.05rem;
  color: var(--muted);
  max-width: 42rem;
}}
.meta {{
  display: flex; flex-wrap: wrap; gap: 10px 18px;
  font-size: 0.88rem; color: var(--muted);
}}
.meta strong {{ color: var(--ink); font-weight: 600; }}
.kpis {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(140px, 1fr));
  gap: 12px;
  margin: 22px 0;
}}
.kpi {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 14px 16px;
}}
.kpi span {{ display: block; font-size: 0.78rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.04em; }}
.kpi strong {{ display: block; margin-top: 4px; font-size: 1.55rem; font-variant-numeric: tabular-nums; }}
.kpi.over strong {{ color: var(--over); }}
.kpi.under strong {{ color: var(--under); }}
.kpi.denials strong {{ color: var(--over); }}
section.panel {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 18px 20px;
  margin-bottom: 18px;
}}
section.panel h2 {{
  margin: 0 0 10px;
  font-size: 1.15rem;
}}
.summary ul {{ margin: 0; padding-left: 1.15rem; }}
.summary li {{ margin: 0.45rem 0; }}
.charts {{
  display: grid;
  grid-template-columns: 1fr 1.2fr;
  gap: 18px;
  margin-bottom: 18px;
}}
@media (max-width: 860px) {{
  .charts {{ grid-template-columns: 1fr; }}
}}
.chart-card {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px 18px;
}}
.chart-card h2 {{ margin: 0 0 4px; font-size: 1.05rem; }}
.chart-card .caption {{ margin: 0 0 12px; font-size: 0.85rem; color: var(--muted); }}
.chart-wrap {{ position: relative; height: 280px; }}
.chart-wrap.tall {{ height: 340px; }}
.legend-note {{
  font-size: 0.85rem;
  color: var(--muted);
  margin: 8px 0 0;
}}
.feature {{
  background: var(--card);
  border: 1px solid var(--line);
  border-left: 5px solid var(--muted);
  border-radius: var(--radius);
  padding: 16px 18px;
  margin-bottom: 14px;
}}
.feature.bucket-over {{ border-left-color: var(--over); }}
.feature.bucket-under {{ border-left-color: var(--under); }}
.feature.bucket-full {{ border-left-color: var(--full); }}
.feature.bucket-unused {{ border-left-color: var(--unused); }}
.feature.bucket-unknown {{ border-left-color: var(--unknown); }}
.feature header {{
  display: flex; justify-content: space-between; gap: 12px; align-items: flex-start;
  flex-wrap: wrap;
}}
.feature h3 {{ margin: 0; font-size: 1.12rem; }}
.fid {{ margin: 2px 0 0; font-size: 0.8rem; color: var(--muted); font-family: ui-monospace, Consolas, monospace; }}
.badge {{
  display: inline-block;
  padding: 4px 10px;
  border-radius: 999px;
  font-size: 0.75rem;
  font-weight: 600;
  white-space: nowrap;
}}
.badge-over {{ background: #fde8e6; color: var(--over); }}
.badge-under {{ background: #e4f2f7; color: var(--under); }}
.badge-full {{ background: #e6f4ea; color: var(--full); }}
.badge-unused {{ background: #eeebe6; color: var(--unused); }}
.badge-unknown {{ background: #fff3d6; color: #7a5c00; }}
.meaning {{
  margin: 12px 0 8px;
  padding: 10px 12px;
  background: #f3f7f5;
  border-radius: 8px;
  border: 1px solid #d5e4dc;
}}
.detail {{ margin: 0 0 12px; color: var(--muted); font-size: 0.95rem; }}
.metrics {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
  gap: 8px;
  margin-bottom: 10px;
}}
.metrics div {{
  background: #f7f9fb;
  border: 1px solid var(--line);
  border-radius: 8px;
  padding: 8px 10px;
}}
.metrics span {{ display: block; font-size: 0.72rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }}
.metrics strong {{ font-size: 0.95rem; font-variant-numeric: tabular-nums; }}
.muted {{ color: var(--muted); font-size: 0.88rem; margin: 6px 0; }}
.side-grid {{
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
  gap: 12px;
  margin-top: 10px;
}}
.mini h4 {{ margin: 0 0 6px; font-size: 0.82rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }}
.kv {{ list-style: none; margin: 0; padding: 0; }}
.kv li {{
  display: flex; justify-content: space-between; gap: 8px;
  padding: 3px 0; border-bottom: 1px solid #edf1f4;
  font-size: 0.88rem;
}}
.guide {{
  font-size: 0.92rem;
  color: var(--muted);
}}
.guide dt {{ font-weight: 600; color: var(--ink); margin-top: 8px; }}
.guide dd {{ margin: 2px 0 0; }}
footer.note {{
  margin-top: 28px;
  font-size: 0.85rem;
  color: var(--muted);
}}
.hidden {{ display: none !important; }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <h1>License Usage Report</h1>
    <p class="tagline">
      Did we run out of seats, or are we sitting on spare capacity?
      Charts and plain-language findings from your FlexLM / PTC log.
    </p>
    <div class="meta">
      <div><strong>Generated</strong> {esc(generated)}</div>
      <div><strong>Log</strong> {esc(log_file)}</div>
      <div><strong>Lines</strong> {lines_read:,}</div>
      <div><strong>Window</strong> {esc(window)}</div>
      <div><strong>Users</strong> {esc(users_txt)}</div>
      <div><strong>Status file</strong> {esc(status_file or "not used")}</div>
    </div>
  </header>

  <div class="kpis">
    <div class="kpi"><span>Features</span><strong>{len(ordered)}</strong></div>
    <div class="kpi over"><span>Ran out</span><strong>{status_counts.get("over", 0)}</strong></div>
    <div class="kpi under"><span>Under-used</span><strong>{status_counts.get("under", 0)}</strong></div>
    <div class="kpi denials"><span>Denial events</span><strong>{total_denials}</strong></div>
  </div>

  <section class="panel summary">
    <h2>Key findings</h2>
    <ul>
      {bullets_html}
    </ul>
  </section>

  <div class="charts">
    <div class="chart-card {'hidden' if not pie_values else ''}">
      <h2>How features broke down</h2>
      <p class="caption">Share of features by utilization outcome</p>
      <div class="chart-wrap"><canvas id="pieStatus"></canvas></div>
    </div>
    <div class="chart-card {'hidden' if not util_values else ''}">
      <h2>Peak use vs predicted owned</h2>
      <p class="caption">Percent of predicted seats used at the busiest moment (100% = fully used)</p>
      <div class="chart-wrap"><canvas id="barUtil"></canvas></div>
    </div>
  </div>

  <div class="chart-card {'hidden' if not peak_values else ''}" style="margin-bottom:18px">
    <h2>Peak concurrent users by feature</h2>
    <p class="caption">Highest number of unique user@host holders at once. Orange markers = predicted owned seats when known.</p>
    <div class="chart-wrap tall"><canvas id="barPeak"></canvas></div>
  </div>

  <div class="chart-card {'hidden' if not denial_values else ''}" style="margin-bottom:18px">
    <h2>Where people were blocked</h2>
    <p class="caption">Saturation denial events (retries collapsed). Higher = more times someone could not get a seat.</p>
    <div class="chart-wrap"><canvas id="barDenials"></canvas></div>
  </div>

  <section class="panel">
    <h2>How to read this</h2>
    <dl class="guide">
      <dt>Ran out (OVER)</dt>
      <dd>FlexLM logged DENIED because all seats were already in use. Real users were blocked.</dd>
      <dt>Spare capacity (UNDER)</dt>
      <dd>Peak concurrent use stayed below predicted owned seats, and nobody was denied.</dd>
      <dt>Full but no denials</dt>
      <dd>Peak hit the owned count, but the pool still covered demand — watch if usage grows.</dd>
      <dt>Predicted owned</dt>
      <dd>When denials occur, holders already checked out ≈ pool size. Without denials, only a lower bound (peak) is known unless you set capacity.</dd>
      <dt>Same user@host = 1 seat</dt>
      <dd>One person launching Creo twice on the same computer still counts as one concurrent seat.</dd>
    </dl>
  </section>

  <h2 style="margin: 8px 0 12px; font-size: 1.2rem;">Feature details</h2>
  {"".join(card_html_parts)}

  <footer class="note">
    Predicted owned is at least the peak concurrent checkouts that succeeded.
    DENIED means the pool was full at that moment. Source: {esc(os.path.basename(log_file))} · {esc(generated)}
  </footer>
</div>
<script>
const DATA = {chart_json};
const SHOW = {{
  pie: {show_pie},
  peak: {show_peak_chart},
  denial: {show_denial_chart},
  util: {show_util_chart}
}};

Chart.defaults.font.family = '"Segoe UI", "Helvetica Neue", sans-serif';
Chart.defaults.color = '#5a6a78';

if (SHOW.pie && document.getElementById('pieStatus')) {{
  new Chart(document.getElementById('pieStatus'), {{
    type: 'doughnut',
    data: {{
      labels: DATA.pieLabels,
      datasets: [{{
        data: DATA.pieValues,
        backgroundColor: DATA.pieColors,
        borderWidth: 0
      }}]
    }},
    options: {{
      responsive: true,
      maintainAspectRatio: false,
      plugins: {{
        legend: {{ position: 'bottom' }},
        tooltip: {{
          callbacks: {{
            label: (ctx) => {{
              const total = ctx.dataset.data.reduce((a, b) => a + b, 0);
              const pct = total ? Math.round(100 * ctx.raw / total) : 0;
              return ` ${{ctx.label}}: ${{ctx.raw}} (${{pct}}%)`;
            }}
          }}
        }}
      }}
    }}
  }});
}}

if (SHOW.peak && document.getElementById('barPeak')) {{
  new Chart(document.getElementById('barPeak'), {{
    type: 'bar',
    data: {{
      labels: DATA.peakLabels,
      datasets: [
        {{
          label: 'Peak concurrent users',
          data: DATA.peakValues,
          backgroundColor: '#0b6e4f',
          borderRadius: 4
        }},
        {{
          label: 'Predicted owned seats',
          data: DATA.ownedValues,
          backgroundColor: '#e67e22',
          borderRadius: 4
        }}
      ]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {{
        x: {{
          title: {{ display: true, text: 'Concurrent unique user@host' }},
          beginAtZero: true,
          ticks: {{ precision: 0 }}
        }},
        y: {{
          ticks: {{ autoSkip: false, font: {{ size: 11 }} }}
        }}
      }},
      plugins: {{
        legend: {{ position: 'bottom' }}
      }}
    }}
  }});
}}

if (SHOW.denial && document.getElementById('barDenials')) {{
  new Chart(document.getElementById('barDenials'), {{
    type: 'bar',
    data: {{
      labels: DATA.denialLabels,
      datasets: [{{
        label: 'Denial events',
        data: DATA.denialValues,
        backgroundColor: '#c0392b',
        borderRadius: 4
      }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {{
        x: {{
          title: {{ display: true, text: 'Denial events' }},
          beginAtZero: true,
          ticks: {{ precision: 0 }}
        }}
      }},
      plugins: {{ legend: {{ display: false }} }}
    }}
  }});
}}

if (SHOW.util && document.getElementById('barUtil')) {{
  new Chart(document.getElementById('barUtil'), {{
    type: 'bar',
    data: {{
      labels: DATA.utilLabels,
      datasets: [{{
        label: 'Peak as % of predicted owned',
        data: DATA.utilValues,
        backgroundColor: DATA.utilValues.map(v =>
          v >= 100 ? '#c0392b' : (v >= 80 ? '#e67e22' : '#1f6f8b')
        ),
        borderRadius: 4
      }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {{
        x: {{
          min: 0,
          suggestedMax: 100,
          title: {{ display: true, text: 'Utilization at peak (%)' }}
        }}
      }},
      plugins: {{ legend: {{ display: false }} }}
    }}
  }});
}}
</script>
</body>
</html>
"""

    Path(html_path).write_text(page, encoding="utf-8")
    return html_path


def main():
    parser = argparse.ArgumentParser(
        description="License usage report: peak concurrent use + denials from ptc_d.log"
    )
    parser.add_argument("--log_file", type=str, default=DEFAULT_LOG_FILE)
    parser.add_argument("--status_file", type=str, default=DEFAULT_STATUS_FILE,
                        help="Optional ptcstatus file; blank = usage-only")
    parser.add_argument("--licenses", type=str, default=DEFAULT_LICENSES,
                        help="Features to analyze, '|' separated; blank = all features")
    parser.add_argument("--users", type=str, default=DEFAULT_USERS,
                        help="user@computer filter, '|' separated; empty = all users")
    parser.add_argument("--capacity", type=str, default=DEFAULT_CAPACITY or None,
                        help="Optional owned-seat overrides: Feature=N|Feature2=M")
    parser.add_argument("--lookback_days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    parser.add_argument("--reporting_days", type=int, default=DEFAULT_REPORTING_DAYS)
    parser.add_argument("--lookup_file", type=str, default=DEFAULT_LOOKUP_FILE,
                        help="Feature id → product name lookup file")
    parser.add_argument("--html_file", type=str, default=DEFAULT_HTML_FILE,
                        help="Path for the HTML dashboard")
    parser.add_argument("--no_browser", action="store_true",
                        help="Write HTML but do not open a browser")
    parser.add_argument("--prompt", action="store_true",
                        help="Prompt interactively instead of using top-of-script defaults")

    args = parser.parse_args()

    if args.prompt:
        log_file = get_input("Location of ptc_d.log file:", DEFAULT_LOG_FILE)
        status_file = get_input("ptcstatus output file (blank to skip):", DEFAULT_STATUS_FILE)
        if not status_file.strip():
            status_file = None
        feature_input = get_input(
            "Licenses to check (blank = all, or separate by '|'):",
            DEFAULT_LICENSES,
        )
        users_input = get_input("Optional user@computer filter (separate by '|'):", DEFAULT_USERS)
        lookback_days = int(get_input("Lookback days:", str(DEFAULT_LOOKBACK_DAYS)))
        reporting_days = int(get_input("Reporting days:", str(DEFAULT_REPORTING_DAYS)))
        capacity_text = args.capacity or DEFAULT_CAPACITY
    else:
        log_file = args.log_file
        status_file = args.status_file or None
        feature_input = args.licenses
        users_input = args.users
        lookback_days = args.lookback_days
        reporting_days = args.reporting_days
        capacity_text = args.capacity or DEFAULT_CAPACITY

    features = [f.strip() for f in feature_input.split("|") if f.strip()]
    user_computers = [u.strip() for u in users_input.split("|") if u.strip()] or None
    lookup = load_license_lookup(args.lookup_file)

    owned = {}
    if status_file:
        try:
            owned = parse_owned_seats(status_file)
            print(f"Loaded owned seats from {status_file}: {owned}")
        except FileNotFoundError:
            print(f"Warning: status file not found: {status_file}")
            status_file = None

    if capacity_text:
        owned.update(parse_capacity_override(capacity_text))

    if features:
        print(f"\nParsing {log_file} for: {', '.join(features)}")
    else:
        print(f"\nParsing {log_file} for: ALL features found in log")
    if user_computers:
        print(f"User filter: {', '.join(user_computers)}")
    print("This may take a moment on large logs...\n")

    stats, lines_read = parse_log(
        log_file,
        features,
        owned,
        lookback_days=lookback_days,
        reporting_days=reporting_days,
        user_computers=user_computers,
    )
    print_report(
        stats, lookback_days, reporting_days, status_file, log_file, lines_read,
        user_computers=user_computers,
        lookup=lookup,
    )

    html_path = write_html_report(
        stats,
        lookback_days,
        reporting_days,
        status_file,
        log_file,
        lines_read,
        args.html_file,
        user_computers=user_computers,
        lookup=lookup,
    )
    abs_html = os.path.abspath(html_path)
    print(f"\nHTML dashboard written to: {abs_html}")
    if not args.no_browser:
        webbrowser.open(Path(abs_html).as_uri())
        print("Opened report in your default browser.")


if __name__ == "__main__":
    print("License Usage Report\n")
    main()
