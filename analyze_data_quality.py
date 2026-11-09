#!/usr/bin/env python3
"""Audit CBP Nogales observations without modifying the original CSV.

Usage: python analyze_data_quality.py [input.csv] [output_directory]
Requires Python 3.9+; no third-party packages.

Methodology version: 2.2 (2026-11-09)
  - Researcher-supplied schedules (see SCHEDULE NOTES below)
  - Freshness flag from the age of CBP's per-lane update time
  - Lane categories not offered at a crossing are labeled, not treated as missing
  - v2.2: CBP update times up to 60 min ahead of collection count as current
  - Average waits use only scheduled-open, fresh, numeric, not-"Lanes Closed" rows
"""
import csv
import re
import sys
from collections import Counter, defaultdict
from datetime import datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

INPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('data/nogales_border_observations.csv')
OUTPUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path('reports')
AZ = ZoneInfo('America/Phoenix')
METHODOLOGY_VERSION = '2.2'

# Minutes either side of a scheduled opening/closing in which CBP's status may
# legitimately lag the official time. Rows here are flagged, not treated as errors.
BOUNDARY_MINUTES = 30

# Freshness thresholds, in minutes since CBP's per-lane update time.
# CBP normally refreshes about hourly, so readings up to 90 minutes old are normal.
FRESH_MAX = 90
STALE_MIN = 120

# SCHEDULE NOTES (researcher-supplied, November 2026; verify against official
# CBP sources for each analysis period before publication):
#   DeConcini main crossing: passenger vehicles and pedestrians, 24/7.
#   DeConcini SENTRI (Mexican-side entrance, Av. Adolfo Lopez Mateos):
#       closes 10 PM Mon-Thu, midnight Fri-Sun. Source: Google listing, not CBP.
#       OPENING TIME UNVERIFIED: 6 AM is a placeholder; data suggests ~6:45-7:30 AM.
#       The schedule is for the approach gate, not necessarily the CBP booths.
#   Mariposa passenger vehicles and pedestrians: 6 AM - 10 PM daily.
#   Mariposa commercial: Mon-Fri 6 AM - 10 PM; Sat 8 AM - 6 PM; closed Sunday.
#       Sources cited by researcher: ADOT port-of-entry page; CBP BWT.
#   Morley Gate: pedestrians only, 10 AM - 6 PM daily.
#   Ready Lanes: not offered at these crossings (researcher-confirmed).
#   Mariposa SENTRI: not offered (researcher-confirmed).
#   DeConcini commercial: not offered (commercial traffic uses Mariposa).
SENTRI_OPEN = time(6)  # placeholder until verified


def _window(start, end):
    return (start, end)


def schedule_windows(crossing, traffic, category, day):
    """Return a list of (start, end) open windows for this weekday, or a label."""
    name = crossing.strip().lower()
    wd = day.weekday()  # Monday = 0; Sunday = 6
    if category == 'ready_lanes':
        return 'not_offered'
    if 'morley' in name:
        if traffic != 'pedestrian':
            return 'not_offered'
        return [_window(time(10), time(18))]
    if 'mariposa' in name:
        if traffic == 'commercial_vehicle':
            if wd <= 4:
                return [_window(time(6), time(22))]
            if wd == 5:
                return [_window(time(8), time(18))]
            return []  # closed Sunday
        if category == 'NEXUS_SENTRI_lanes':
            return 'not_offered'  # no SENTRI lane at Mariposa (researcher-confirmed)
        return [_window(time(6), time(22))]
    if 'deconcini' in name or 'de concini' in name:
        if traffic == 'commercial_vehicle':
            return 'not_offered'
        if traffic == 'passenger_vehicle' and category == 'NEXUS_SENTRI_lanes':
            # Mon-Thu close 10 PM; Fri-Sun close at midnight (end of day).
            return [_window(SENTRI_OPEN, time(22) if wd <= 3 else None)]
        return 'always_open'
    return 'schedule_unknown'


def scheduled(crossing, traffic, category, when):
    """Return (schedule_flag, near_boundary)."""
    w = schedule_windows(crossing, traffic, category, when)
    if w == 'always_open':
        return 'scheduled_open', False
    if isinstance(w, str):
        return w, False
    t = when.replace(tzinfo=None)
    day = t.replace(hour=0, minute=0, second=0, microsecond=0)
    is_open, near = False, False
    for start, end in w:
        s = day + timedelta(hours=start.hour, minutes=start.minute)
        e = day + timedelta(days=1) if end is None else day + timedelta(hours=end.hour, minutes=end.minute)
        if s <= t < e:
            is_open = True
        edges = [s] + ([] if end is None else [e])
        if any(abs((t - edge).total_seconds()) <= BOUNDARY_MINUTES * 60 for edge in edges):
            near = True
    # Midnight closings (Fri-Sun SENTRI) also need a boundary check just after midnight.
    prev = schedule_windows(crossing, traffic, category, when - timedelta(days=1))
    if isinstance(prev, list) and any(end is None for _, end in prev):
        if (t - day).total_seconds() <= BOUNDARY_MINUTES * 60:
            near = True
    return ('scheduled_open' if is_open else 'scheduled_closed'), near


UPDATE_RE = re.compile(r'(\d{1,2}):(\d{2})\s*([ap])\.?m', re.I)


def update_age_minutes(collected, update_text):
    """Minutes between CBP's per-lane update time and collection; None if unknown.

    CBP gives only a clock time (e.g. 'At 11:00 pm MST'), so the date is inferred:
    an update time later than the collection time is taken to be from the prior day.
    CBP labels these times MST; that label has not been independently verified.
    """
    m = UPDATE_RE.search(update_text or '')
    if not m:
        return None
    hour = int(m.group(1)) % 12 + (12 if m.group(3).lower() == 'p' else 0)
    local = collected.replace(tzinfo=None)
    upd = local.replace(hour=hour, minute=int(m.group(2)), second=0, microsecond=0)
    # CBP sometimes posts an update time up to about an hour AHEAD of the actual
    # time (e.g. 'At 2:00 pm' at 1:22 pm). Treat those as current (age 0), not as
    # yesterday's update. Only times more than 60 minutes ahead are prior-day.
    if upd > local + timedelta(minutes=60):
        upd -= timedelta(days=1)
    return max(0.0, (local - upd).total_seconds() / 60)


def freshness(age):
    if age is None:
        return 'unknown_no_update_time'
    if age <= FRESH_MAX:
        return 'fresh'
    if age < STALE_MIN:
        return 'aging'
    return 'stale'


def load_rows(path):
    with path.open(newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError('Input CSV is empty')
    for row in rows:
        row['_dt'] = datetime.fromisoformat(row['collected_at_arizona']).astimezone(AZ)
    return sorted(rows, key=lambda r: (r['_dt'], r['crossing_name'], r['traffic_type'], r['lane_category']))


def is_number(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False


def section_ages(rows):
    """Freshest known update age per (collection, crossing, traffic type).

    CBP often leaves the update time blank on closed lanes. Those rows inherit
    the freshness of the other lanes in the same section and collection run.
    """
    ages = {}
    for row in rows:
        age = update_age_minutes(row['_dt'], row.get('cbp_update_time', ''))
        if age is None:
            continue
        k = (row['collected_at_utc'], row['crossing_name'], row['traffic_type'])
        ages[k] = age if k not in ages else min(ages[k], age)
    return ages


def audit(rows):
    prior = {}
    detailed = []
    inherited_ages = section_ages(rows)
    for row in rows:
        key = (row['port_number'], row['crossing_name'], row['traffic_type'], row['lane_category'])
        # A change is evidence of a changed published value; unchanged values do NOT
        # prove a stale report (see freshness_flag for that).
        fingerprint = tuple(row.get(k, '') for k in
            ('cbp_update_time', 'lanes_open', 'delay_minutes', 'operational_status'))
        if key not in prior:
            report = 'first_observation'
        elif fingerprint != prior[key]:
            report = 'published_values_changed'
        else:
            report = 'same_values_as_previous_collection'
        prior[key] = fingerprint

        sched, near = scheduled(row['crossing_name'], row['traffic_type'], row['lane_category'], row['_dt'])
        age = update_age_minutes(row['_dt'], row.get('cbp_update_time', ''))
        age_source = 'own_lane'
        if age is None:
            age = inherited_ages.get((row['collected_at_utc'], row['crossing_name'], row['traffic_type']))
            age_source = 'same_section_this_run' if age is not None else 'none'
        fresh = freshness(age)
        if sched == 'not_offered':
            age, age_source, fresh = None, 'none', 'not_applicable'
        state = (row['operational_status'] or '').strip().lower()

        if sched == 'not_offered':
            closure = 'not_offered_at_crossing'
        elif sched == 'scheduled_closed':
            closure = 'outside_scheduled_hours'
        elif sched == 'scheduled_open' and state == 'lanes closed':
            if near:
                closure = 'closed_near_schedule_boundary'
            elif fresh == 'stale':
                closure = 'closed_but_stale_data'
            else:
                closure = 'possible_unexpected_lane_closure_verify'
        elif sched == 'scheduled_open' and state == 'update pending':
            closure = 'unknown_update_pending'
        elif sched == 'scheduled_open' and state in ('n/a', ''):
            closure = 'unknown_not_reported'
        elif sched == 'scheduled_open':
            closure = 'no_closure_reported'
        else:
            closure = 'unknown_schedule'

        usable = (sched == 'scheduled_open' and not near and fresh == 'fresh'
                  and is_number(row['delay_minutes'])
                  and state not in ('lanes closed', 'update pending')
                  and (row.get('port_status') or '').strip().lower() == 'open')

        # Maximum lanes belongs to the entire traffic section; NEVER divide one
        # category's lanes_open by it and call that category's utilization.
        detail = {k: v for k, v in row.items() if k != '_dt'}
        detail.update(report_change_flag=report, schedule_flag=sched,
                      near_schedule_boundary='yes' if near else 'no',
                      update_age_minutes='' if age is None else f'{age:.0f}',
                      update_age_source=age_source,
                      freshness_flag=fresh, closure_flag=closure,
                      usable_for_wait_average='yes' if usable else 'no',
                      cbp_update_time_present='yes' if row['cbp_update_time'].strip() else 'no',
                      methodology_version=METHODOLOGY_VERSION)
        detailed.append(detail)
    return detailed


def summarize(rows):
    groups = defaultdict(list)
    for row in rows:
        groups[(row['crossing_name'], row['traffic_type'], row['lane_category'])].append(row)
    out = []
    for (crossing, traffic, category), group in sorted(groups.items()):
        c = Counter(r['report_change_flag'] for r in group)
        d = Counter(r['closure_flag'] for r in group)
        f = Counter(r['freshness_flag'] for r in group)
        waits = sorted(float(r['delay_minutes']) for r in group if r['usable_for_wait_average'] == 'yes')
        median = ''
        if waits:
            n = len(waits)
            median = f'{(waits[n // 2] if n % 2 else (waits[n // 2 - 1] + waits[n // 2]) / 2):.1f}'
        out.append(dict(crossing_name=crossing, traffic_type=traffic, lane_category=category,
                        observations=len(group),
                        not_offered=d['not_offered_at_crossing'],
                        first_observations=c['first_observation'],
                        changed_published_values=c['published_values_changed'],
                        repeated_published_values=c['same_values_as_previous_collection'],
                        fresh=f['fresh'], aging=f['aging'], stale=f['stale'],
                        no_update_time=f['unknown_no_update_time'],
                        outside_scheduled_hours=d['outside_scheduled_hours'],
                        closed_near_schedule_boundary=d['closed_near_schedule_boundary'],
                        closed_but_stale_data=d['closed_but_stale_data'],
                        possible_unexpected_lane_closures=d['possible_unexpected_lane_closure_verify'],
                        update_pending=d['unknown_update_pending'],
                        usable_wait_observations=len(waits),
                        average_reported_wait_minutes=(f'{sum(waits)/len(waits):.1f}' if waits else ''),
                        median_reported_wait_minutes=median,
                        methodology_version=METHODOLOGY_VERSION,
                        note='Usable = scheduled open, not near open/close time, fresh CBP update, '
                             'numeric delay, not Lanes Closed/Update Pending, port Open. '
                             'Repeated values are not proof of stale reporting; '
                             'maximum_lanes is section-wide, not category-specific'))
    return out


def write_csv(path, records):
    with path.open('w', newline='', encoding='utf-8') as f:
        writer = csv.DictWriter(f, fieldnames=list(records[0].keys()))
        writer.writeheader()
        writer.writerows(records)


def main():
    rows = load_rows(INPUT)
    detailed = audit(rows)
    summary = summarize(detailed)
    OUTPUT.mkdir(parents=True, exist_ok=True)
    write_csv(OUTPUT / 'data_quality_detail.csv', detailed)
    write_csv(OUTPUT / 'data_quality_summary.csv', summary)
    print(f'Audited {len(rows)} observations across {len(summary)} crossing/traffic/lane categories '
          f'(methodology v{METHODOLOGY_VERSION}).')
    print(f'Wrote {OUTPUT / "data_quality_detail.csv"}')
    print(f'Wrote {OUTPUT / "data_quality_summary.csv"}')
    print('Note: This is an observational audit, NOT a calculation of actual processing capacity.')


if __name__ == '__main__':
    main()
