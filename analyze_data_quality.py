#!/usr/bin/env python3
"""Audit CBP Nogales observations without modifying the original CSV.

Usage: python analyze_data_quality.py [input.csv] [output_directory]
Requires Python 3.9+; no third-party packages.
"""
import csv
import sys
from collections import Counter, defaultdict
from datetime import datetime, time
from pathlib import Path
from zoneinfo import ZoneInfo

INPUT = Path(sys.argv[1]) if len(sys.argv) > 1 else Path('data/nogales_border_observations.csv')
OUTPUT = Path(sys.argv[2]) if len(sys.argv) > 2 else Path('reports')
AZ = ZoneInfo('America/Phoenix')

# Working schedules supplied by the researcher; independently verify before publication.
# A schedule for a dedicated approach lane is NOT necessarily a schedule for every CBP
# inspection booth assigned to that lane category.
def scheduled(crossing, traffic, category, when):
    name = crossing.strip().lower()
    wd = when.weekday()  # Monday = 0; Sunday = 6
    t = when.time().replace(tzinfo=None)
    if 'morley' in name:
        if traffic != 'pedestrian':
            return 'not_applicable'
        return 'scheduled_open' if time(10) <= t < (time(16) if wd == 6 else time(18)) else 'scheduled_closed'
    if 'mariposa' in name:
        return 'scheduled_open' if time(6) <= t < time(22) else 'scheduled_closed'
    if 'deconcini' in name or 'de concini' in name:
        if traffic == 'passenger_vehicle' and category == 'NEXUS_SENTRI_lanes':
            # Friday-Sunday: 6 AM to midnight (00:00 next day).
            end = time(22) if wd <= 3 else time(23, 59, 59, 999999)
            return 'scheduled_open' if time(6) <= t <= end else 'scheduled_closed'
        if traffic in ('passenger_vehicle', 'pedestrian'):
            return 'scheduled_open'  # Main DeConcini crossing: 24 hours.
    return 'schedule_unknown'


def load_rows(path):
    with path.open(newline='', encoding='utf-8-sig') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError('Input CSV is empty')
    for row in rows:
        row['_dt'] = datetime.fromisoformat(row['collected_at_arizona']).astimezone(AZ)
    return sorted(rows, key=lambda r: (r['_dt'], r['crossing_name'], r['traffic_type'], r['lane_category']))


def audit(rows):
    prior = {}
    detailed = []
    for row in rows:
        key = (row['port_number'], row['crossing_name'], row['traffic_type'], row['lane_category'])
        # CBP update time may be blank, ambiguous, or time-only. A change is evidence
        # of a changed published value; unchanged values do NOT prove a stale report.
        fingerprint = tuple(row.get(k, '') for k in
            ('cbp_update_time', 'lanes_open', 'delay_minutes', 'operational_status'))
        if key not in prior:
            report = 'first_observation'
        elif fingerprint != prior[key]:
            report = 'published_values_changed'
        else:
            report = 'same_values_as_previous_collection'
        prior[key] = fingerprint

        sched = scheduled(row['crossing_name'], row['traffic_type'], row['lane_category'], row['_dt'])
        state = (row['operational_status'] or '').strip().lower()
        if sched == 'scheduled_closed':
            closure = 'outside_scheduled_hours'
        elif sched == 'scheduled_open' and state == 'lanes closed':
            closure = 'possible_unexpected_lane_closure_verify'
        elif sched == 'scheduled_open' and state == 'update pending':
            closure = 'unknown_update_pending'
        elif sched == 'scheduled_open' and state in ('n/a', ''):
            closure = 'unknown_not_reported'
        elif sched == 'scheduled_open':
            closure = 'no_closure_reported'
        else:
            closure = 'unknown_schedule_or_not_applicable'

        # Maximum lanes belongs to the entire traffic section; NEVER divide one
        # category's lanes_open by it and call that category's utilization.
        detail = {k: v for k, v in row.items() if k != '_dt'}
        detail.update(report_change_flag=report, schedule_flag=sched,
                      closure_flag=closure,
                      cbp_update_time_present='yes' if row['cbp_update_time'].strip() else 'no')
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
        valid_waits = [float(r['delay_minutes']) for r in group if r['delay_minutes'].strip().replace('.', '', 1).isdigit() and r['schedule_flag'] == 'scheduled_open']
        out.append(dict(crossing_name=crossing, traffic_type=traffic, lane_category=category,
                        observations=len(group), first_observations=c['first_observation'],
                        changed_published_values=c['published_values_changed'],
                        repeated_published_values=c['same_values_as_previous_collection'],
                        outside_scheduled_hours=d['outside_scheduled_hours'],
                        possible_unexpected_lane_closures=d['possible_unexpected_lane_closure_verify'],
                        update_pending=d['unknown_update_pending'],
                        valid_wait_observations_during_open_hours=len(valid_waits),
                        average_reported_wait_minutes=(f'{sum(valid_waits)/len(valid_waits):.1f}' if valid_waits else ''),
                        note='Repeated values are not proof of stale reporting; maximum_lanes is section-wide, not category-specific'))
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
    print(f'Audited {len(rows)} observations across {len(summary)} crossing/traffic/lane categories.')
    print(f'Wrote {OUTPUT / "data_quality_detail.csv"}')
    print(f'Wrote {OUTPUT / "data_quality_summary.csv"}')
    print('Note: This is an observational audit, NOT a calculation of actual processing capacity.')


if __name__ == '__main__':
    main()
