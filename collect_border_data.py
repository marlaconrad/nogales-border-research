#!/usr/bin/env python3
"""Append timestamped CBP northbound border-wait observations for Nogales crossings."""
import csv
import http.client
import json
import time
import urllib.error
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
REQUIRED_CROSSINGS = ("deconcini", "mariposa")
OPTIONAL_PEDESTRIAN_CROSSINGS = ("morley",)


def extract(payload, collected_at=None):
    if not isinstance(payload, list):
        raise ValueError("Unexpected CBP response: expected a list of ports")
    now = collected_at or datetime.now(timezone.utc)
    utc = now.astimezone(timezone.utc).isoformat(timespec="seconds")
    az = now.astimezone(ZoneInfo("America/Phoenix")).isoformat(timespec="seconds")

    selected = []
    for port in payload:
        if not isinstance(port, dict) or port.get("border") != "Mexican Border":
            continue
        port_name = str(port.get("port_name", ""))
        crossing_name = str(port.get("crossing_name", ""))
        combined_name = (port_name + " " + crossing_name).lower()
        if "nogales" not in combined_name:
            continue
        if any(name in combined_name for name in REQUIRED_CROSSINGS + OPTIONAL_PEDESTRIAN_CROSSINGS):
            selected.append(port)

    selected_names = " ".join(str(p.get("crossing_name", "")) for p in selected).lower()
    if not all(name in selected_names for name in REQUIRED_CROSSINGS):
        raise ValueError("Expected both DeConcini and Mariposa in the CBP response; no data written")

    rows = []
    for port in selected:
        combined_name = (str(port.get("port_name", "")) + " " + str(port.get("crossing_name", ""))).lower()
        is_morley = "morley" in combined_name
        for section, (traffic_type, auto_key) in SECTIONS.items():
            if is_morley and traffic_type != "pedestrian":
                continue
            group = port.get(section) or {}
            if not isinstance(group, dict):
                continue
            for lane_key in sorted(LANE_KEYS):
                lane = group.get(lane_key)
                if not isinstance(lane, dict):
                    continue
                rows.append({
                    "collected_at_utc": utc,
                    "collected_at_arizona": az,
                    "port_number": port.get("port_number", ""),
                    "port_name": port.get("port_name", ""),
                    "crossing_name": port.get("crossing_name", ""),
                    "border": port.get("border", ""),
                    "port_status": port.get("port_status", ""),
                    "cbp_date": port.get("date", ""),
                    "cbp_time": port.get("time", ""),
                    "traffic_type": traffic_type,
                    "lane_category": lane_key,
                    "maximum_lanes": group.get("maximum_lanes", ""),
                    "automation_type": group.get(auto_key, ""),
                    "lanes_open": lane.get("lanes_open", ""),
                    "delay_minutes": lane.get("delay_minutes", ""),
                    "operational_status": lane.get("operational_status", ""),
                    "cbp_update_time": lane.get("update_time", ""),
                    "construction_notice": port.get("construction_notice", ""),
                })
    if not rows:
        raise ValueError("No lane-category observations found; no data written")
    return rows



def main():
    request = urllib.request.Request(
        URL,
        headers={
            "User-Agent": "NogalesAcademicResearch/1.0 (public CBP data)",
            "Accept": "application/json",
        },
    )

    retry_delays = [15, 30, 60]
    max_attempts = len(retry_delays) + 1

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"CBP request attempt {attempt}/{max_attempts}", flush=True)

            with urllib.request.urlopen(request, timeout=35) as response:
                payload = json.load(response)

            rows = extract(payload)
            print("CBP data retrieved and validated successfully.")
            break

        except urllib.error.HTTPError as error:
            if error.code not in (429, 500, 502, 503, 504):
                raise

            print(f"Temporary HTTP error: {error.code}", flush=True)

        except (urllib.error.URLError, TimeoutError) as error:
            print(f"Connection error: {error}", flush=True)

        except (http.client.HTTPException, ConnectionError) as error:
            # Connection dropped or response cut off while downloading.
            print(f"Connection interrupted during download: {error!r}", flush=True)

        except json.JSONDecodeError as error:
            # Usually a truncated or temporary non-JSON response; retry.
            # Structural problems found by extract() are NOT retried.
            print(f"Response was not valid JSON: {error}", flush=True)

        if attempt == max_attempts:
            raise RuntimeError(
                f"CBP collection failed after {max_attempts} attempts. "
                "No new observations were written."
            )

        delay = retry_delays[attempt - 1]
        print(f"Waiting {delay} seconds before retrying...", flush=True)
        time.sleep(delay)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    exists = OUT.exists() and OUT.stat().st_size > 0

    if exists:
        with OUT.open(newline="", encoding="utf-8") as handle:
            header = next(csv.reader(handle))

        if header != COLUMNS:
            raise ValueError(
                "Existing CSV columns differ from script; refusing to append"
            )

    with OUT.open("a", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=COLUMNS)

        if not exists:
            writer.writeheader()

        writer.writerows(rows)

    captured = sorted({str(row["crossing_name"]) for row in rows})

    print(
        f"Saved {len(rows)} observations for "
        f"{', '.join(captured)} at {rows[0]['collected_at_utc']}"
    )

    if not any(
        "morley" in str(row["crossing_name"]).lower()
        for row in rows
    ):
        print(
            "Note: CBP did not provide Morley Gate "
            "pedestrian lane observations in this response"
        )

if __name__ == "__main__":
    main()
