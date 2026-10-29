#!/usr/bin/env python3
"""Append timestamped CBP northbound border-wait observations for Nogales crossings."""
import csv
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

URL = "https://bwt.cbp.gov/api/bwtnew"
OUT = Path("data/nogales_border_observations.csv")
COLUMNS = [
    "collected_at_utc", "collected_at_arizona", "port_number", "port_name",
    "crossing_name", "border", "port_status", "cbp_date", "cbp_time",
    "traffic_type", "lane_category", "maximum_lanes", "automation_type",
    "lanes_open", "delay_minutes", "operational_status", "cbp_update_time",
    "construction_notice"
]
SECTIONS = {
    "passenger_vehicle_lanes": ("passenger_vehicle", "pv_automation_type"),
    "pedestrian_lanes": ("pedestrian", "ped_automation_type"),
    "commercial_vehicle_lanes": ("commercial_vehicle", "cv_automation_type"),
}
LANE_KEYS = {"standard_lanes", "ready_lanes", "NEXUS_SENTRI_lanes", "FAST_lanes"}

def extract(payload, collected_at=None):
    if not isinstance(payload, list):
        raise ValueError("Unexpected CBP response: expected a list of ports")
    now = collected_at or datetime.now(timezone.utc)
    utc = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    az = now.astimezone(ZoneInfo("America/Phoenix")).isoformat(timespec="seconds")
    selected = [
        p for p in payload if isinstance(p, dict)
        and p.get("border") == "Mexican Border"
        and (
            "nogales" in str(p.get("port_name", "")).lower()
            or "nogales" in str(p.get("crossing_name", "")).lower()
        )
        and any(s in (str(p.get("crossing_name", ""))+" "+str(p.get("port_name", ""))).lower()
                for s in ("deconcini", "mariposa"))
    ]
    names = " ".join(str(p.get("crossing_name", "")) for p in selected).lower()
    if not ("deconcini" in names and "mariposa" in names):
        raise ValueError("Expected both DeConcini and Mariposa in the CBP response; no data written")
    rows = []
    for p in selected:
        for section, (traffic_type, auto_key) in SECTIONS.items():
            group = p.get(section) or {}
            if not isinstance(group, dict):
                continue
            for lane_key in sorted(LANE_KEYS):
                lane = group.get(lane_key)
                if not isinstance(lane, dict):
                    continue
                rows.append({
                    "collected_at_utc": utc,
                    "collected_at_arizona": az,
                    "port_number": p.get("port_number", ""),
                    "port_name": p.get("port_name", ""),
                    "crossing_name": p.get("crossing_name", ""),
                    "border": p.get("border", ""),
                    "port_status": p.get("port_status", ""),
                    "cbp_date": p.get("date", ""),
                    "cbp_time": p.get("time", ""),
                    "traffic_type": traffic_type,
                    "lane_category": lane_key,
                    "maximum_lanes": group.get("maximum_lanes", ""),
                    "automation_type": group.get(auto_key, ""),
                    "lanes_open": lane.get("lanes_open", ""),
                    "delay_minutes": lane.get("delay_minutes", ""),
                    "operational_status": lane.get("operational_status", ""),
                    "cbp_update_time": lane.get("update_time", ""),
                    "construction_notice": p.get("construction_notice", ""),
                })
    if not rows:
        raise ValueError("No lane-category observations found; no data written")
    return rows

def main():
    request = urllib.request.Request(
        URL, headers={"User-Agent": "NogalesAcademicResearch/1.0 (public CBP data)", "Accept": "application/json"}
    )
    with urllib.request.urlopen(request, timeout=35) as response:
        payload = json.load(response)
    rows = extract(payload)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    exists = OUT.exists() and OUT.stat().st_size > 0
    if exists:
        with OUT.open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle))
        if header != COLUMNS:
            raise ValueError("Existing CSV columns differ from script; refusing to append")
    with OUT.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)
        if not exists:
            writer.writeheader()
        writer.writerows(rows)
    print(f"Saved {len(rows)} observations for DeConcini and Mariposa at {rows[0]['collected_at_utc']}")

if __name__ == "__main__":
    main()
