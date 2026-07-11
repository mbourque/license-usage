"""
License utilization report (Stage 1 - text summary).

Tracks concurrent OUT/IN usage against owned seat counts from ptcstatus,
and reports DENIED events where all seats were already in use.
"""

import argparse
import datetime
import re
from collections import defaultdict

# ---------------------------------------------------------------------------
# Defaults — edit these so you can run the script with no prompts
# ---------------------------------------------------------------------------
DEFAULT_LOG_FILE = r"C:\dev\License usage 2\ptc_d.log.big"

#DEFAULT_LOG_FILE = r"C:\dev\License usage 2\ptc_d.log.kevin"

DEFAULT_STATUS_FILE = ""  # leave blank to ignore owned-seat file; usage-only report
DEFAULT_LICENSES = "PROE_DesignEss"  # blank = report every feature found in the log
DEFAULT_LOOKBACK_DAYS = 0   # 0 = entire log
DEFAULT_REPORTING_DAYS = 0  # 0 = entire log
# user@computer filter; set to "" for all users (recommended for company-wide utilization)
DEFAULT_USERS = ""
# Optional: "PROE_DesignEss=10|10113=5" when you know real owned counts
DEFAULT_CAPACITY = ""
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
SATURATION_DENIAL = "Licensed number of users already reached"
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}


def get_input(prompt, default):
    user_input = input(f"{prompt} [{default}]: ").strip()
    return user_input if user_input else default


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
        self.owned = owned
        self.concurrent = 0  # unique user@host currently holding a seat
        self.holders = defaultdict(int)  # user@host -> open checkout count
        self.peak_overall = 0
        self.peak_overall_when = None
        self.daily_peak = defaultdict(int)
        self.denial_events = 0  # deduped saturation denials
        self.denial_raw = 0
        self.denial_by_day = defaultdict(int)
        self.denial_users = defaultdict(int)
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

    return stats, line_no


def classify_feature(s):
    """Return a short summary focused on observed usage."""
    if s.peak_overall == 0 and s.denial_events == 0:
        return "UNUSED — no checkouts in window"
    if s.denial_events > 0:
        return (
            f"USED — peak {s.peak_overall} concurrent; "
            f"{s.denial_events} saturation denial event(s) "
            f"(server ran out of seats at least sometimes)"
        )
    return f"USED — peak {s.peak_overall} concurrent; no saturation denials"


def print_report(stats, lookback_days, reporting_days, status_file, log_file, lines_read,
                 user_computers=None):
    print("=" * 72)
    print("LICENSE USAGE REPORT (Stage 1) — observed use from log")
    print("=" * 72)
    print(f"Log file:      {log_file}")
    print(f"Status file:   {status_file or '(ignored — reporting usage only)'}")
    print(f"Lines read:    {lines_read}")
    print(f"Features:      {len(stats)}")
    if lookback_days or reporting_days:
        print(f"Window:        lookback={lookback_days} days, reporting={reporting_days} days")
    else:
        print("Window:        entire log")
    if user_computers:
        print(f"User filter:   {', '.join(user_computers)}")
    else:
        print("User filter:   all users")
    print()

    # Highest peak first, then most denials, then name
    ordered = sorted(
        stats.values(),
        key=lambda s: (-s.peak_overall, -s.denial_events, -s.out_count, s.name),
    )

    for s in ordered:
        name = s.name
        avg_daily_peak = (
            sum(s.daily_peak.values()) / len(s.daily_peak) if s.daily_peak else 0
        )

        print("-" * 72)
        print(f"Feature: {name}")
        if s.owned is not None:
            print(f"  Owned seats (optional):   {s.owned}")
        print(f"  Peak concurrent in use:   {s.peak_overall}  (unique user@host)")
        if s.peak_overall_when:
            d, t, u = s.peak_overall_when
            print(f"    when:                   {d} at {t}")
            print(f"    triggered by checkout:  {u}")
            print(f"                            (checkout that raised unique holders"
                  " to the peak; same user@host = 1 seat)")
        print(f"  Avg daily peak:           {avg_daily_peak:.1f}")
        print(f"  Days with activity:       {len(s.daily_peak)}")
        print(f"  Distinct users:           {len(s.users_seen)}")
        print(f"  OUT / IN events:          {s.out_count} / {s.in_count}")
        print(f"  Saturation denials:       {s.denial_events} events "
              f"({s.denial_raw} raw log lines)")
        print(f"  Days with denials:        {len(s.denial_by_day)}")
        print(f"  Summary:                  {classify_feature(s)}")

        if s.daily_peak:
            top_days = sorted(s.daily_peak.items(), key=lambda x: (-x[1], x[0]))[:5]
            print("  Top peak days (most seats in use at once):")
            for day, peak in top_days:
                print(f"    {day}: {peak} concurrent")

        if s.denial_by_day:
            top_denial_days = sorted(
                s.denial_by_day.items(), key=lambda x: (-x[1], x[0])
            )[:5]
            print("  Top denial days:")
            for day, count in top_denial_days:
                print(f"    {day}: {count} denial event(s)")

        if s.denial_users:
            top_users = sorted(
                s.denial_users.items(), key=lambda x: (-x[1], x[0])
            )[:5]
            print("  Users most often denied:")
            for user, count in top_users:
                print(f"    {user}: {count}")

    print("-" * 72)
    print()
    print("Notes:")
    print("  - Peak concurrent = highest number of unique user@host holding a seat.")
    print("  - Same user launching Creo again on the same host counts as 1 seat.")
    print("  - 'Triggered by checkout' is the user whose OUT raised the unique count.")
    print("  - Counts reset on 'Server started'; unmatched OUT/IN can still skew peaks.")
    print("  - Saturation denials = 'Licensed number of users already reached'.")
    print("  - Retry bursts in the same second are counted as one denial event.")
    print("=" * 72)


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
    )


if __name__ == "__main__":
    print("License Usage Report | Stage 1 (text)\n")
    main()
