"""
License utilization report.

Tracks concurrent OUT/IN usage against owned seat counts from license.dat,
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
# Defaults — used when you press Enter at prompts (edit if you like)
# ---------------------------------------------------------------------------
# DEFAULT_LICENSES = "PROE_DesignEss"  # blank = report every feature found in the log
DEFAULT_LICENSES = ""  # blank = report every feature found in the log
DEFAULT_LOOKBACK_DAYS = 180   # 0 = entire log
DEFAULT_REPORTING_DAYS = 0  # 0 = entire log
# user@computer filter; set to "" for all users (recommended for company-wide utilization)
DEFAULT_USERS = ""
# Optional: "PROE_DesignEss=10|10113=5" when you know real owned counts
DEFAULT_CAPACITY = ""
DEFAULT_HTML_FILE = str(Path(__file__).resolve().parent / "license_report.html")
LAST_DATA_FOLDER_FILE = Path(__file__).resolve().parent / ".last_data_folder"
# FlexLM vendor daemon logs — either name may appear in a customer folder.
LOG_FILE_NAMES = ("ptc_d.log", "ptclmgrd.log")
# ---------------------------------------------------------------------------

TIMESTAMP_PATTERN = re.compile(r"TIMESTAMP\s+(\d{1,2}/\d{1,2}/\d{4})")
START_DATE_PATTERN = re.compile(
    r"Start-Date:\s+\w+\s+(\w+)\s+(\d{1,2})\s+(\d{4})\s+"
)
TIME_PATTERN = re.compile(r"^(\d{1,2}):(\d{2}):(\d{2})")
OUT_PATTERN = re.compile(r'OUT:\s+"([^"]+)"\s+(\S+)')
IN_PATTERN = re.compile(r'IN:\s+"([^"]+)"\s+(\S+)')
DENIED_PATTERN = re.compile(r'DENIED:\s+"([^"]+)"\s+(\S+)\s+\(([^)]*)\)')
UNSUPPORTED_PATTERN = re.compile(
    r'UNSUPPORTED:\s+"([^"]+)"\s+(?:\([^)]+\)\s+)?(\S+)\s+\(([^)]*)\)'
)
# INCREMENT feature daemon version expiry count ...
INCREMENT_OWNED_PATTERN = re.compile(
    r"^INCREMENT\s+(\S+)\s+\S+\s+\S+\s+\S+\s+(\d+)\b"
)
SERVICEABLE_PATTERN = re.compile(r"^#\s*Serviceable\s*=\s*(.+)$", re.IGNORECASE)
FEATURE_NAME_PATTERN = re.compile(r"^#\s*Feature Name\s*=\s*(\S+)", re.IGNORECASE)
DETAIL_TABLE_PATTERN = re.compile(
    r"^#\S+\s+(.+?)\s+SPN-\S+-([FL])-?\s+.+\s+\d+\s+\d{4}-\d{2}-\d{2}\s+([\w,]+)\b"
)
SUMMARY_TABLE_ROW = re.compile(
    r"^#(\S+)\s+\d+\s+(.+?)\s+(?:Creo|Prime)\s+\d+\.\d+\s+(?:Flt|Ext)\s+(?:Lic|Opt)\s+\d{1,2}-\w{3}-\d{4}",
    re.IGNORECASE,
)
# Summary table uses internal FlexLM labels for some numeric features — keep #Serviceable instead.
SUMMARY_SKIP_NAMES = frozenset({
    "NOTEBOOK",
    "VERIFY",
    "TOOLKIT",
    "TOOLKIT (> OR = 18.0)",
    "PROCESS FOR MFG",
})
SERVER_STARTED_PATTERN = re.compile(r"Server started on")
EXPIRED_PATTERN = re.compile(r"EXPIRED:\s+(\S+)")
EXPIRES_WARNING_PATTERN = re.compile(
    r"Warning:\s+(\S+)\s+expires\s+(\d{1,2}-\w{3}-\d{4})", re.IGNORECASE
)
SATURATION_DENIAL = "Licensed number of users already reached"
BUNDLE_PLACEHOLDER_COUNT = 99999
MONTHS = {
    "Jan": 1, "Feb": 2, "Mar": 3, "Apr": 4, "May": 5, "Jun": 6,
    "Jul": 7, "Aug": 8, "Sep": 9, "Oct": 10, "Nov": 11, "Dec": 12,
}
REPORT_WIDTH = 72


def get_input(prompt, default):
    user_input = input(f"{prompt} [{default}]: ").strip()
    return user_input if user_input else default


def default_data_folder():
    """Folder suggested when you press Enter (last run, else this script's directory)."""
    last = load_last_data_folder()
    if last:
        return last
    return str(Path(__file__).resolve().parent)


def customer_report_title(folder_path):
    """Report heading from customer folder name, e.g. rheinmetall -> Rheinmetall."""
    name = Path(folder_path).name.title()
    return f"License Usage Report for {name}"


def load_last_data_folder():
    """Return the last successfully used customer folder, or None."""
    try:
        path = LAST_DATA_FOLDER_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not path:
        return None
    root = Path(path)
    if root.is_dir():
        return str(root.resolve())
    return None


def save_last_data_folder(folder):
    """Remember customer folder for the next run."""
    try:
        root = Path(folder).expanduser()
        if not root.is_absolute():
            root = (Path.cwd() / root).resolve()
        else:
            root = root.resolve()
        LAST_DATA_FOLDER_FILE.write_text(str(root) + "\n", encoding="utf-8")
    except OSError:
        pass


def discover_ptc_files(folder):
    """
    Find license.dat and a FlexLM vendor log in a customer folder.

    Looks for ptc_d.log and ptclmgrd.log; if both exist, uses the newest file.
    """
    root = Path(folder).expanduser()
    if not root.is_absolute():
        root = (Path.cwd() / root).resolve()
    else:
        root = root.resolve()

    if not root.is_dir():
        raise FileNotFoundError(f"Folder not found: {root}")

    license_path = root / "license.dat"
    license_file = str(license_path) if license_path.is_file() else None

    log_paths = [root / name for name in LOG_FILE_NAMES if (root / name).is_file()]
    if not log_paths:
        raise FileNotFoundError(
            f"No log in {root} (expected one of: {', '.join(LOG_FILE_NAMES)})"
        )

    log_path = max(log_paths, key=lambda p: p.stat().st_mtime)
    if len(log_paths) > 1:
        print(f"Note: multiple logs found; using newest: {log_path.name}")

    return str(log_path), license_file


def prompt_for_data_folder(default_folder=None):
    """Ask for a customer folder and resolve log + license.dat inside it."""
    default_folder = default_folder or default_data_folder()
    while True:
        folder = get_input(
            "Customer folder (license.dat + ptc_d.log or ptclmgrd.log):",
            default_folder,
        )
        try:
            log_file, license_file = discover_ptc_files(folder)
            save_last_data_folder(folder)
            print(f"  Log:     {log_file}")
            if license_file:
                print(f"  License: {license_file}")
            else:
                print("  License: (not found — owned seats will be inferred only)")
            return log_file, license_file
        except FileNotFoundError as exc:
            print(f"  {exc}")
            print("  Try another folder.\n")


def clean_product_name(name):
    """Strip '(formerly ...)' and truncated summary-table parens like '(forme'."""
    name = re.sub(r"\s*\(formerly[^)]*\)", "", name, flags=re.IGNORECASE)
    # Truncated "(formerly ..." with no closing paren (fixed-width summary table)
    name = re.sub(r"\s*\(formerly\b.*$", "", name, flags=re.IGNORECASE)
    # Other truncated parens: "(forme", "(no PLM", "(AAX" cut off by column width
    name = re.sub(r"\s*\([^)]*$", "", name)
    return re.sub(r"\s+", " ", name).strip()


def summary_name_usable(name):
    """True when the summary-table Product column is a real name, not an internal alias."""
    cleaned = clean_product_name(name)
    if not cleaned:
        return False
    if cleaned.upper() in SUMMARY_SKIP_NAMES:
        return False
    if cleaned.upper().startswith("PROCESS FOR MFG"):
        return False
    return True


def merge_display_names(block_names, summary_names):
    """Prefer summary-table names except for cryptic internal aliases (NOTEBOOK, etc.)."""
    merged = dict(block_names)
    for feature, raw_name in summary_names.items():
        if not summary_name_usable(raw_name):
            continue
        merged[feature] = clean_product_name(raw_name)
    return merged


def set_license_type(license_types, feature, fl_char):
    """Map feature -> F (floating) or L (locked); prefer L when detail rows disagree."""
    if not feature:
        return
    if license_types.get(feature) == "L":
        return
    license_types[feature] = fl_char


def license_type_label(feature, license_types):
    """Human label from detail-table Product Package Id suffix (-F / -L)."""
    fl = license_types.get(feature)
    if fl == "F":
        return "Floating"
    if fl == "L":
        return "Locked"
    return None


def parse_license_dat(license_file):
    """
    Parse PTC license.dat for owned seat counts and feature display names.

    Names: license-pack summary table (Product column) when readable, else
    #Serviceable / #Feature Name blocks, with the detail table as fallback
    (e.g. 10113,PROBUNDLE_10113). Product Package Id suffix in the detail
    table (-F / -L) records floating vs locked. Duplicate INCREMENT rows are
    summed; bundle defs with count 99999 are skipped.
    """
    owned = defaultdict(int)
    names = {}
    license_types = {}
    summary_names = {}
    current_serviceable = None
    in_summary_table = False

    with open(license_file, "r", encoding="utf-8", errors="replace") as f:
        for line in f:
            stripped = line.lstrip()

            if stripped.startswith("#") and "Summary Table" in stripped:
                in_summary_table = True
                continue
            if stripped.startswith("#") and "Detail Table" in stripped:
                in_summary_table = False
                continue

            if in_summary_table:
                summary_match = SUMMARY_TABLE_ROW.match(stripped)
                if summary_match:
                    summary_names[summary_match.group(1)] = summary_match.group(2)
                continue

            serviceable_match = SERVICEABLE_PATTERN.match(stripped)
            if serviceable_match:
                current_serviceable = clean_product_name(serviceable_match.group(1))
                continue

            feature_match = FEATURE_NAME_PATTERN.match(stripped)
            if feature_match and current_serviceable:
                names[feature_match.group(1)] = current_serviceable
                continue

            detail_match = DETAIL_TABLE_PATTERN.match(stripped)
            if detail_match:
                product_name = clean_product_name(detail_match.group(1))
                fl_char = detail_match.group(2)
                for feature in detail_match.group(3).split(","):
                    feature = feature.strip()
                    if feature and feature not in names:
                        names[feature] = product_name
                    set_license_type(license_types, feature, fl_char)
                continue

            increment_match = INCREMENT_OWNED_PATTERN.match(stripped)
            if not increment_match:
                continue
            name, count_s = increment_match.groups()
            count = int(count_s)
            if count >= BUNDLE_PLACEHOLDER_COUNT:
                continue
            owned[name] += count

    return dict(owned), merge_display_names(names, summary_names), dict(license_types)


def display_name(feature, lookup):
    friendly = lookup.get(feature)
    if friendly:
        return friendly
    return feature


def normalize_user(user):
    """
    Case-fold user@host so the same Windows account counts as one seat.

    Creo/FlexLM logs sometimes vary casing (rbrock@HOST vs RBROCK@HOST);
    FlexLM treats those as one holder.
    """
    return user.casefold()


def normalize_username(user):
    """Username before @, case-insensitive — same person on two PCs counts once."""
    return normalize_user(user).split("@", 1)[0]


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
        self.owned = owned  # explicit override from license.dat / capacity, if any
        self.concurrent = 0  # unique user@host currently holding a seat (case-insensitive)
        self.holders = defaultdict(int)  # normalized user@host -> open checkout count
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
        """Record OUT. Same user@host (ignoring case) already holding does not add another."""
        user = normalize_user(user)
        prev = self.holders[user]
        self.holders[user] = prev + 1
        if prev == 0:
            self.concurrent += 1
            return True  # new seat taken
        return False  # same user relaunch; still one seat

    def checkin(self, user):
        """Record IN. Seat freed only when this user@host has no remaining checkouts."""
        user = normalize_user(user)
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
        Resolve owned seat count.

        Prefer explicit owned from license.dat / capacity. Otherwise
        infer from denials and peak: successful concurrent checkouts cannot
        exceed owned seats, so owned >= historical peak. A saturation DENIED
        means the pool was full at that moment.
        """
        if self.owned is not None:
            return {
                "count": self.owned,
                "confidence": "owned",
                "detail": "",
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
                    f"Using {predicted} as the inferred owned count "
                    f"(cannot be below peak)."
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
            "detail": "no usage and no denials — cannot determine owned count",
            "lower_bound": 0,
            "at_denial": None,
            "note": None,
        }

    def effective_capacity(self):
        """Capacity used for UNDER/OVER judgment."""
        pred = self.predict_owned_seats()
        if pred["count"] is not None:
            if pred["confidence"] == "owned":
                return pred["count"], "owned seats"
            return pred["count"], f"inferred owned ({pred['confidence']} confidence)"
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
    unsupported_count = 0
    last_unsupported_key = None

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
                user_key = normalize_user(user)
                if feature_set is not None and feature not in feature_set:
                    continue
                if user_set is not None and user_key not in user_set:
                    continue
                s = get_stats(feature)
                took_new_seat = s.checkout(user_key)

                if in_report_window(current_date, now, lookback_days, reporting_days):
                    s.out_count += 1
                    s.users_seen.add(user_key)
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
                user_key = normalize_user(user)
                if feature_set is not None and feature not in feature_set:
                    continue
                if user_set is not None and user_key not in user_set:
                    continue
                s = get_stats(feature)
                s.checkin(user_key)
                if in_report_window(current_date, now, lookback_days, reporting_days):
                    s.in_count += 1
                continue

            denied_match = DENIED_PATTERN.search(line)
            if denied_match:
                feature, user, reason = denied_match.groups()
                user_key = normalize_user(user)
                if feature_set is not None and feature not in feature_set:
                    continue
                if user_set is not None and user_key not in user_set:
                    continue
                if SATURATION_DENIAL not in reason:
                    continue
                if not in_report_window(current_date, now, lookback_days, reporting_days):
                    continue

                s = get_stats(feature)
                s.denial_raw += 1
                # Collapse Creo retry bursts: same feature/user/second = one event
                denial_key = (current_date, current_time_str, feature, user_key)
                if denial_key != s._last_denial_key:
                    s._last_denial_key = denial_key
                    s.denial_events += 1
                    s.denial_by_day[current_date] += 1
                    s.denial_users[user_key] += 1
                    # DENIED => pool full => holders in use = owned seats
                    s.denial_holder_samples.append(s.concurrent)
                continue

            unsupported_match = UNSUPPORTED_PATTERN.search(line)
            if unsupported_match:
                feature, user, _reason = unsupported_match.groups()
                user_key = normalize_user(user)
                if user_set is not None and user_key not in user_set:
                    continue
                if not in_report_window(
                    current_date, now, lookback_days, reporting_days
                ):
                    continue
                unsupported_key = (
                    current_date, current_time_str, feature, user_key
                )
                if unsupported_key != last_unsupported_key:
                    last_unsupported_key = unsupported_key
                    unsupported_count += 1

    return stats, line_no, unsupported_count


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
            if pred["confidence"] == "owned":
                parts.append(f"Owned seats: {pred['count']}.")
            else:
                parts.append(
                    f"Inferred owned seats: {pred['count']} "
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


def had_window_usage(s):
    """True if the feature had checkout / denial activity in the report window."""
    return bool(
        s.out_count
        or s.in_count
        or s.denial_events
        or s.denial_raw
        or s.peak_overall
        or s.users_seen
        or s.daily_peak
    )


def include_in_report(s, lookup=None):
    """
    Keep features that were used in the window, or that have a known owned
    count from license.dat / capacity. Drop noise like legacy
    features seen only outside the window with no owned seats, and pure-numeric
    optional-module IDs (308, 301, …) that are not in license.dat.
    """
    lookup = lookup or {}
    if s.name.isdigit() and s.name not in lookup:
        return False
    if had_window_usage(s):
        return True
    if s.owned is not None:
        return True
    return False


def plain_meaning(s, label, lookup):
    """One short paragraph a non-expert can act on (no stats duplicated from metrics)."""
    name = display_name(s.name, lookup)
    pred = s.predict_owned_seats()
    bucket = status_bucket(label)

    if bucket == "unused":
        return (
            f"{name} had no checkouts in this window. If you pay for these seats, "
            f"they look unused here — or this feature simply was not needed."
        )
    if bucket == "over":
        denial_days = len(s.denial_by_day)
        if denial_days <= 2:
            rarity = "rare"
        elif denial_days <= 10:
            rarity = "occasional"
        else:
            rarity = "repeated"
        extra = f" {pred['note']}" if pred.get("note") else ""
        return (
            f"{name} ran out of seats ({rarity} shortage). "
            f"Demand exceeded supply — consider more seats or "
            f"staggering who uses this option.{extra}"
        )
    if bucket == "under":
        return (
            f"{name} never ran out. Peak stayed below owned capacity with no denials. "
            f"Capacity looks comfortable for this window."
        )
    if bucket == "full":
        return (
            f"{name} hit peak equal to owned capacity, but nobody was denied. "
            f"Fully used at times, yet the pool still covered demand — watch if usage grows."
        )
    return (
        f"{name} peaked with no denials. No proof of shortage; exact owned count "
        f"unknown without denials or a configured capacity."
    )


def ordered_features(stats):
    # After PROE_ base licenses: FULLY → other used (OVER/UNDER/…) → UNUSED
    bucket_rank = {
        "full": 0,
        "over": 1,
        "under": 2,
        "unknown": 3,
        "unused": 4,
    }

    def sort_key(s):
        label, _ = classify_feature(s)
        base = 0 if s.name.startswith("PROE_") else 1
        rank = bucket_rank.get(status_bucket(label), 3)
        return (base, rank, -s.denial_events, -s.peak_overall, -s.out_count, s.name)

    return sorted(stats.values(), key=sort_key)


def unique_creo_users(stats, lookup=None):
    """People (username) who checked out or were denied any reported feature."""
    users = set()
    for s in stats.values():
        if not include_in_report(s, lookup):
            continue
        for user_host in s.users_seen:
            users.add(normalize_username(user_host))
        for user_host in s.denial_users:
            users.add(normalize_username(user_host))
    return users


def creo_hosts_by_user(stats, lookup=None):
    """Map username → distinct computers (host part of user@host) with Creo activity."""
    hosts_by_user = defaultdict(set)
    for s in stats.values():
        if not include_in_report(s, lookup):
            continue
        for user_host in s.users_seen:
            _record_user_host(hosts_by_user, user_host)
        for user_host in s.denial_users:
            _record_user_host(hosts_by_user, user_host)
    return hosts_by_user


def _record_user_host(hosts_by_user, user_host):
    folded = normalize_user(user_host)
    if "@" not in folded:
        return
    user, host = folded.split("@", 1)
    hosts_by_user[user].add(host)


def multi_computer_creo_users(stats, lookup=None):
    """Usernames that checked out or were denied from more than one computer."""
    return {
        user: hosts
        for user, hosts in creo_hosts_by_user(stats, lookup).items()
        if len(hosts) > 1
    }


def window_phrase(lookback_days=0, reporting_days=0):
    if lookback_days > 0:
        return f"in the last {lookback_days} days"
    if reporting_days > 0:
        return f"in the last {reporting_days} days"
    return "in the log"


def format_window_label(lookback_days=0, reporting_days=0):
    """Report window line; omit reporting when 0."""
    if lookback_days <= 0 and reporting_days <= 0:
        return "entire log"
    parts = []
    if lookback_days > 0:
        parts.append(f"lookback={lookback_days}d")
    if reporting_days > 0:
        parts.append(f"reporting={reporting_days}d")
    return ", ".join(parts)


def build_executive_summary(
    stats, lookup, lookback_days=0, reporting_days=0, unsupported_count=0
):
    """Short bullets for the top of the HTML report."""
    ordered = [s for s in ordered_features(stats) if include_in_report(s, lookup)]
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
    creo_users = unique_creo_users(stats, lookup)
    if creo_users:
        if lookback_days > 0:
            window_txt = f"in the last {lookback_days} days"
        elif reporting_days > 0:
            window_txt = f"in the last {reporting_days} days"
        else:
            window_txt = "in the log"
        bullets.append(
            f"{len(creo_users):,} unique Creo users {window_txt}."
        )
        multi_pc = multi_computer_creo_users(stats, lookup)
        if multi_pc:
            bullets.append(
                f"{len(multi_pc)} user(s) used Creo from more than one computer "
                f"{window_txt}."
            )
    else:
        bullets.append(
            f"No Creo license activity recorded {window_phrase(lookback_days, reporting_days)}."
        )
    if unsupported_count:
        bullets.append(
            f"{unsupported_count:,} UNSUPPORTED license request(s) "
            f"{window_phrase(lookback_days, reporting_days)} "
            f"(feature not served by this license server)."
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
            f"(peak below owned seats, no denials). Possible spare capacity."
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


def print_report(stats, lookback_days, reporting_days, log_file, lines_read,
                 user_computers=None, lookup=None, license_file=None):
    lookup = lookup or {}
    _hr("=")
    print("LICENSE USAGE REPORT")
    print("Under vs over utilization")
    _hr("=")
    print()
    _kv("Log", log_file)
    _kv("License.dat", license_file or "(not used)")
    _kv("Lines", f"{lines_read:,}")
    ordered = [s for s in ordered_features(stats) if include_in_report(s, lookup)]
    _kv("Features", len(ordered))
    _kv("Window", format_window_label(lookback_days, reporting_days))
    if user_computers:
        print(_wrap("Users: " + ", ".join(user_computers), indent=2))
    else:
        _kv("Users", "all")
    print()
    print(_wrap(
        "OVER = denials prove seats ran out. "
        "UNDER = peak below owned seats (from license.dat when provided), no denials. "
        "Owned >= peak concurrent (successful checkouts cannot exceed seats). "
        "DENIED means pool full at that moment.",
        indent=2,
    ))
    print()

    for s in ordered:
        avg_daily_peak = (
            sum(s.daily_peak.values()) / len(s.daily_peak) if s.daily_peak else 0
        )
        label, _ = classify_feature(s)
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
        print(_wrap(plain_meaning(s, label, lookup), indent=2))
        print()

        print("  Owned seats")
        if pred["count"] is not None:
            if pred["confidence"] == "owned":
                _kv("Owned", str(pred["count"]))
            else:
                _kv(
                    "Inferred",
                    f"{pred['count']}  [{pred['confidence']} confidence]",
                )
        else:
            _kv("Exact count", f"unknown (at least {pred['lower_bound']})")
        if pred["detail"]:
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
                "Peak vs owned",
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
                "not the only user. Same user@host (ignoring case) = 1 seat.",
                indent=4,
            ))
        _kv("Avg daily peak", f"{avg_daily_peak:.1f}")
        _kv("Active days", len(s.daily_peak))
        _kv("Users checked out", len(s.users_seen))
        _kv("OUT / IN", f"{s.out_count:,} / {s.in_count:,}")
        print()

        print("  Denials (ran out?)")
        if s.denial_events > 0:
            _kv("Answer", "YES — FlexLM refused checkouts")
        else:
            _kv("Answer", "NO — no saturation denials in this window")
        _kv("Events", f"{s.denial_events}  ({s.denial_raw} raw log lines)")
        _kv("Users denied", len(s.denial_users))
        _kv("Days with denials", len(s.denial_by_day))
        print()

        if s.daily_peak:
            print("  Top peak days")
            top_days = sorted(s.daily_peak.items(), key=lambda x: (-x[1], x[0]))[:5]
            for day, peak in top_days:
                mark = ""
                if pred["count"] is not None:
                    if peak == pred["count"]:
                        mark = "  (at owned)"
                    elif peak > pred["count"]:
                        mark = "  (above owned — unexpected)"
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
        "Owned seats come from license.dat when present. "
        "Otherwise they may be inferred from denials: owned is at least the "
        "peak concurrent checkouts that succeeded. DENIED means the pool was "
        "full then; if in-use at DENIED is lower than peak, capacity changed "
        "(e.g. expiry). Same user@host (ignoring case) = 1 seat.",
        indent=2,
    ))
    _hr("=")


def write_html_report(
    stats,
    lookback_days,
    reporting_days,
    log_file,
    lines_read,
    html_path,
    user_computers=None,
    lookup=None,
    license_file=None,
    unsupported_count=0,
    license_types=None,
):
    """Write a self-contained HTML dashboard and return its path."""
    lookup = lookup or {}
    license_types = license_types or {}
    ordered = [s for s in ordered_features(stats) if include_in_report(s, lookup)]
    bullets, status_counts, total_denials = build_executive_summary(
        stats, lookup, lookback_days, reporting_days, unsupported_count
    )
    generated = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    window = format_window_label(lookback_days, reporting_days)
    data_folder = str(Path(log_file).resolve().parent)
    report_title = customer_report_title(data_folder)

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

    # Utilization % where owned known — bar color matches card status (OVER = denials)
    util_labels = []
    util_values = []
    util_colors = []
    bucket_bar_colors = {
        "over": "#c0392b",
        "under": "#1f6f8b",
        "full": "#2f7d4a",
        "unused": "#7a756c",
        "unknown": "#b08900",
    }
    for s in ordered:
        pred = s.predict_owned_seats()
        if pred["count"] and pred["count"] > 0 and s.peak_overall > 0:
            label, _ = classify_feature(s)
            bucket = status_bucket(label)
            util_labels.append(display_name(s.name, lookup))
            util_values.append(round(100.0 * s.peak_overall / pred["count"], 1))
            util_colors.append(bucket_bar_colors.get(bucket, "#1f6f8b"))
    util_labels = util_labels[:15]
    util_values = util_values[:15]
    util_colors = util_colors[:15]

    feature_cards = []
    for s in ordered:
        label, _ = classify_feature(s)
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
            "license_type": license_type_label(s.name, license_types),
            "bucket": bucket,
            "label": label,
            "meaning": plain_meaning(s, label, lookup),
            "peak": s.peak_overall,
            "peak_when": peak_when,
            "avg_daily_peak": round(avg_daily_peak, 1),
            "users": len(s.users_seen),
            "out": s.out_count,
            "inn": s.in_count,
            "denials": s.denial_events,
            "denial_days": len(s.denial_by_day),
            "unique_denied": len(s.denial_users),
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
        "utilLabels": util_labels,
        "utilValues": util_values,
        "utilColors": util_colors,
    }

    def esc(text):
        return html.escape(str(text))

    card_html_parts = []
    for card in feature_cards:
        owned_txt = (
            str(card["owned"])
            if card["owned"] is not None and card["owned_confidence"] == "owned"
            else (
                f"{card['owned']} (inferred, {card['owned_confidence']})"
                if card["owned"] is not None
                else f"unknown (at least {card['owned_lower']})"
            )
        )
        peak_pct = ""
        if card["owned"] and card["owned"] > 0:
            peak_pct = f" — {100.0 * card['peak'] / card['owned']:.0f}% of owned"

        days_rows = "".join(
            f"<tr><td>{esc(d)}</td><td class=\"num\">{p}</td></tr>"
            for d, p in card["top_days"]
        )
        denial_rows = "".join(
            f"<tr><td>{esc(d)}</td><td class=\"num\">{c}</td></tr>"
            for d, c in card["top_denial_days"]
        )
        user_rows = "".join(
            f"<tr><td>{esc(u)}</td><td class=\"num\">{c}</td></tr>"
            for u, c in card["top_users"]
        )

        side_blocks = []
        if days_rows:
            side_blocks.append(
                f'<div class="mini"><h4>Busiest days</h4>'
                f'<table class="mini-table"><thead><tr>'
                f'<th>Day</th><th class="num">Peak</th></tr></thead>'
                f'<tbody>{days_rows}</tbody></table></div>'
            )
        if denial_rows:
            side_blocks.append(
                f'<div class="mini"><h4>Days blocked</h4>'
                f'<table class="mini-table"><thead><tr>'
                f'<th>Day</th><th class="num">Blocks</th></tr></thead>'
                f'<tbody>{denial_rows}</tbody></table></div>'
            )
        if user_rows:
            side_blocks.append(
                f'<div class="mini"><h4>Most denied users</h4>'
                f'<table class="mini-table"><thead><tr>'
                f'<th>User</th><th class="num">Times</th></tr></thead>'
                f'<tbody>{user_rows}</tbody></table></div>'
            )
        side = "".join(side_blocks)

        type_suffix = (
            f' <span class="lic-type">({esc(card["license_type"])})</span>'
            if card.get("license_type")
            else ""
        )

        card_html_parts.append(f"""
<article class="feature bucket-{esc(card['bucket'])}" id="f-{esc(card['id'])}">
  <header>
    <div>
      <h3>{esc(card['title'])}{type_suffix}</h3>
      <p class="fid">{esc(card['id'])}</p>
    </div>
    <span class="badge badge-{esc(card['bucket'])}">{esc(card['label'])}</span>
  </header>
  <p class="meaning"><strong>What this means:</strong> {esc(card['meaning'])}</p>
  <div class="metrics">
    <div><span>Peak concurrent</span><strong>{card['peak']}{esc(peak_pct)}</strong></div>
    <div><span>Owned seats</span><strong>{esc(owned_txt)}</strong></div>
    <div><span>Denial events</span><strong>{card['denials']}</strong></div>
    <div><span>Users denied</span><strong>{card['unique_denied']}</strong></div>
    <div><span>Users checked out</span><strong>{card['users']}</strong></div>
    <div><span>Avg daily peak</span><strong>{card['avg_daily_peak']}</strong></div>
  </div>
  {"<p class='muted'>Peak when: " + esc(card['peak_when']) + "</p>" if card['peak_when'] else ""}
  {"<p class='muted'>" + esc(card['owned_detail']) + "</p>" if card['owned_detail'] else ""}
  <div class="side-grid">{side}</div>
</article>
""")

    bullets_html = "".join(f"<li>{esc(b)}</li>" for b in bullets)
    chart_json = json.dumps(chart_payload)

    show_util_chart = "true" if util_values else "false"
    show_pie = "true" if pie_values else "false"

    page = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{esc(report_title)}</title>
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
  margin: 0 0 14px;
  font-size: 1.85rem;
  letter-spacing: -0.02em;
  color: var(--accent);
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
  min-width: 0;
}}
@media (max-width: 860px) {{
  .charts {{ grid-template-columns: 1fr; }}
}}
.chart-card {{
  background: var(--card);
  border: 1px solid var(--line);
  border-radius: var(--radius);
  padding: 16px 18px;
  min-width: 0;
}}
.chart-card h2 {{ margin: 0 0 4px; font-size: 1.05rem; }}
.chart-card .caption {{ margin: 0 0 12px; font-size: 0.85rem; color: var(--muted); }}
.chart-wrap {{
  position: relative;
  height: 280px;
  width: 100%;
  min-width: 0;
}}
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
.lic-type {{ font-weight: 500; font-size: 0.92rem; color: var(--muted); }}
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
  margin: 12px 0 12px;
  padding: 10px 12px;
  background: #f3f7f5;
  border-radius: 8px;
  border: 1px solid #d5e4dc;
}}
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
  display: flex;
  flex-wrap: wrap;
  gap: 16px 24px;
  margin-top: 10px;
}}
.mini {{
  flex: 0 1 auto;
  min-width: 11rem;
  max-width: 20rem;
}}
.mini h4 {{ margin: 0 0 6px; font-size: 0.82rem; color: var(--muted); text-transform: uppercase; letter-spacing: 0.03em; }}
.mini-table {{
  width: 100%;
  border-collapse: collapse;
  font-size: 0.88rem;
}}
.mini-table th {{
  text-align: left;
  font-size: 0.72rem;
  color: var(--muted);
  font-weight: 600;
  text-transform: uppercase;
  letter-spacing: 0.02em;
  padding: 0 0 4px;
  border-bottom: 1px solid var(--line);
}}
.mini-table th.num {{
  text-align: right;
  width: 3.5rem;
  padding-left: 12px;
}}
.mini-table td {{
  padding: 5px 0;
  border-bottom: 1px solid #edf1f4;
  vertical-align: top;
}}
.mini-table td.num {{
  text-align: right;
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  width: 3.5rem;
  padding-left: 12px;
  white-space: nowrap;
}}
.mini-table tbody tr:last-child td {{ border-bottom: none; }}
.hidden {{ display: none !important; }}
</style>
</head>
<body>
<div class="wrap">
  <header class="hero">
    <h1>{esc(report_title)}</h1>
    <div class="meta">
      <div><strong>Generated</strong> {esc(generated)}</div>
      <div><strong>Folder</strong> {esc(data_folder)}</div>
      <div><strong>Window</strong> {esc(window)}</div>
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
      <h2>Peak use vs owned seats</h2>
      <p class="caption">Percent of owned seats used at the busiest moment. Bar color matches the feature cards: red = OVER-UTILIZED (denials / ran out), not merely 100% peak.</p>
      <div class="chart-wrap"><canvas id="barUtil"></canvas></div>
    </div>
  </div>

  <h2 style="margin: 8px 0 12px; font-size: 1.2rem;">Usage details</h2>
  {"".join(card_html_parts)}

</div>
<script>
const DATA = {chart_json};
const SHOW = {{
  pie: {show_pie},
  util: {show_util_chart}
}};

Chart.defaults.font.family = '"Segoe UI", "Helvetica Neue", sans-serif';
Chart.defaults.color = '#5a6a78';
Chart.defaults.animation = false;

function yAxisLabelWidth(scale, labels) {{
  const ctx = scale.chart.ctx;
  const font = Chart.defaults.font;
  ctx.save();
  ctx.font = `${{font.size}}px ${{font.family}}`;
  let max = 0;
  for (const label of labels) {{
    max = Math.max(max, ctx.measureText(String(label)).width);
  }}
  ctx.restore();
  const needed = Math.ceil(max) + 16;
  const budget = Math.floor(scale.chart.width * 0.48);
  return Math.min(needed, Math.max(120, budget));
}}

function watchChartResize(chart, container) {{
  if (!window.ResizeObserver) {{
    return;
  }}
  const ro = new ResizeObserver(() => {{
    chart.resize();
  }});
  ro.observe(container);
}}

if (SHOW.pie && document.getElementById('pieStatus')) {{
  const pieCanvas = document.getElementById('pieStatus');
  const pieChart = new Chart(pieCanvas, {{
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
  watchChartResize(pieChart, pieCanvas.parentElement);
}}

if (SHOW.util && document.getElementById('barUtil')) {{
  const utilCanvas = document.getElementById('barUtil');
  const utilWrap = utilCanvas.parentElement;
  const utilCount = DATA.utilLabels.length;
  utilWrap.style.height = Math.max(280, utilCount * 36) + 'px';
  const utilChart = new Chart(utilCanvas, {{
    type: 'bar',
    data: {{
      labels: DATA.utilLabels,
      datasets: [{{
        label: 'Peak as % of owned seats',
        data: DATA.utilValues,
        backgroundColor: DATA.utilColors,
        borderRadius: 4
      }}]
    }},
    options: {{
      indexAxis: 'y',
      responsive: true,
      maintainAspectRatio: false,
      scales: {{
        y: {{
          ticks: {{
            autoSkip: false,
            crossAlign: 'near',
            padding: 6
          }},
          afterFit(scale) {{
            scale.width = yAxisLabelWidth(scale, DATA.utilLabels);
          }}
        }},
        x: {{
          min: 0,
          max: 100,
          title: {{ display: true, text: 'Utilization at peak (%)' }}
        }}
      }},
      plugins: {{ legend: {{ display: false }} }}
    }}
  }});
  watchChartResize(utilChart, utilWrap);
}}
</script>
</body>
</html>
"""

    Path(html_path).write_text(page, encoding="utf-8")
    return html_path


def main():
    parser = argparse.ArgumentParser(
        description="License usage report: peak concurrent use + denials from FlexLM logs"
    )
    parser.add_argument("--no_browser", action="store_true",
                        help="Write HTML but do not open a browser")
    args = parser.parse_args()

    log_file, license_file = prompt_for_data_folder()
    lookback_days = int(
        get_input("Lookback days (0 = entire log):", str(DEFAULT_LOOKBACK_DAYS))
    )

    feature_input = DEFAULT_LICENSES
    users_input = DEFAULT_USERS
    reporting_days = DEFAULT_REPORTING_DAYS
    capacity_text = DEFAULT_CAPACITY
    html_file = DEFAULT_HTML_FILE

    features = [f.strip() for f in feature_input.split("|") if f.strip()]
    user_computers = [
        normalize_user(u.strip()) for u in users_input.split("|") if u.strip()
    ] or None
    lookup = {}
    owned = {}
    license_types = {}
    if license_file:
        try:
            owned, lookup, license_types = parse_license_dat(license_file)
            print(f"Loaded owned seats from {license_file}: {owned}")
            print(f"Loaded {len(lookup)} feature names from {license_file}")
        except FileNotFoundError:
            print(f"Warning: license.dat not found: {license_file}")
            license_file = None

    if capacity_text:
        owned.update(parse_capacity_override(capacity_text))

    if features:
        print(f"\nParsing {log_file} for: {', '.join(features)}")
    else:
        print(f"\nParsing {log_file} for: ALL features found in log")
    if user_computers:
        print(f"User filter: {', '.join(user_computers)}")
    print("This may take a moment on large logs...\n")

    stats, lines_read, unsupported_count = parse_log(
        log_file,
        features,
        owned,
        lookback_days=lookback_days,
        reporting_days=reporting_days,
        user_computers=user_computers,
    )
    print_report(
        stats, lookback_days, reporting_days, log_file, lines_read,
        user_computers=user_computers,
        lookup=lookup,
        license_file=license_file,
    )

    html_path = write_html_report(
        stats,
        lookback_days,
        reporting_days,
        log_file,
        lines_read,
        html_file,
        user_computers=user_computers,
        lookup=lookup,
        license_file=license_file,
        unsupported_count=unsupported_count,
        license_types=license_types,
    )
    abs_html = os.path.abspath(html_path)
    print(f"\nHTML dashboard written to: {abs_html}")
    if not args.no_browser:
        webbrowser.open(Path(abs_html).as_uri())
        print("Opened report in your default browser.")


if __name__ == "__main__":
    print("License Usage Report\n")
    main()
