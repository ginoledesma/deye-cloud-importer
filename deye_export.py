#!/usr/bin/env python3
"""Bulk-export station history from the Deye Cloud OpenAPI to CSV.

Gets the same data as the "PlantsDetails-History" export on deyecloud.com
(5-minute station power frames), plus hourly energy totals built from those
frames and Deye's own daily energy totals.

Endpoints used (see https://developer.deyecloud.com/api):
  POST /v1.0/account/token?appId=...   -> accessToken
  POST /v1.0/station/list              -> your stations and their ids
  POST /v1.0/station/history           -> granularity 1 = frames for one day,
                                          granularity 2 = daily totals (30 days/call)
  Read-only settings (the `config` command) -- see cmd_config.

Usage:
  python deye_export.py stations
  python deye_export.py frames --start 2024-01-01 --end 2026-09-25
  python deye_export.py hourly
  python deye_export.py daily  --start 2024-01-01 --end 2026-09-25
  python deye_export.py all                     # everything since the station started
  python deye_export.py config                  # snapshot of station/inverter settings

Configuration comes from environment variables or a .env file next to this
script (see .env.example).
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import os
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import requests

SUCCESS_CODE = "1000000"
DEFAULT_BASE_URL = "https://eu1-developer.deyecloud.com"

# Station frame fields that hold instantaneous power. The API reports them in
# watts; the website export shows kW, so they are converted.
# Column mapping checked against a website export: all 289 frames of a day match.
POWER_FIELDS = [
    "generationPower",   # website: Production (and PV, when there's no generator)
    "consumptionPower",  # website: Consumption
    "wirePower",         # website: Grid (positive = buying, negative = exporting)
    "batteryPower",      # website: Battery (positive = discharging, negative = charging)
    "purchasePower",     # buying side of wirePower only
    "gridPower",         # exporting side of wirePower only
    "chargePower",
    "dischargePower",
]
# Non-power frame fields worth keeping. The API's other fields (daily totals,
# irradiance, performance ratios, year/month/day) are always empty in frames
# or repeat `time`.
FRAME_EXTRA_FIELDS = ["batterySOC", "generationRatio"]
# Days per granularity=2 request (see DeyeClient.station_daily).
DAILY_CHUNK_DAYS = 30
# Signed fields that get split into positive/negative energy in the hourly roll-up.
SIGNED_FIELDS = ["wirePower", "batteryPower"]


# --------------------------------------------------------------------------- config

def load_dotenv(path: Path) -> None:
    """Minimal .env loader: KEY=VALUE lines, existing env vars win."""
    if not path.is_file():
        return
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def password_hash(env: dict) -> str:
    if env.get("DEYE_PASSWORD_SHA256"):
        return env["DEYE_PASSWORD_SHA256"].lower()
    if env.get("DEYE_PASSWORD"):
        return hashlib.sha256(env["DEYE_PASSWORD"].encode("utf-8")).hexdigest()
    raise SystemExit("Set DEYE_PASSWORD (or DEYE_PASSWORD_SHA256).")


# --------------------------------------------------------------------------- API client

class DeyeError(RuntimeError):
    pass


class DeyeClient:
    def __init__(self, base_url: str, app_id: str, app_secret: str, login: dict,
                 pwd_sha256: str, session: requests.Session | None = None,
                 delay: float = 0.5, retries: int = 4):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.app_secret = app_secret
        self.login = login  # {"email": ...} or {"username": ...} or {"mobile": ..., "countryCode": ...}
        self.pwd_sha256 = pwd_sha256
        self.session = session or requests.Session()
        self.delay = delay
        self.retries = retries
        self.token: str | None = None
        self.token_expires = 0.0

    @classmethod
    def from_env(cls, env: dict, **kwargs) -> "DeyeClient":
        missing = [k for k in ("DEYE_APP_ID", "DEYE_APP_SECRET") if not env.get(k)]
        if missing:
            raise SystemExit(f"Missing settings: {', '.join(missing)} (see .env.example)")
        if env.get("DEYE_EMAIL"):
            login = {"email": env["DEYE_EMAIL"]}
        elif env.get("DEYE_USERNAME"):
            login = {"username": env["DEYE_USERNAME"]}
        elif env.get("DEYE_MOBILE"):
            login = {"mobile": env["DEYE_MOBILE"], "countryCode": env.get("DEYE_COUNTRY_CODE", "")}
        else:
            raise SystemExit("Set DEYE_EMAIL, DEYE_USERNAME or DEYE_MOBILE.")
        return cls(env.get("DEYE_BASE_URL", DEFAULT_BASE_URL), env["DEYE_APP_ID"],
                   env["DEYE_APP_SECRET"], login, password_hash(env), **kwargs)

    def _request(self, path: str, body: dict, auth: bool = True) -> dict:
        url = f"{self.base_url}/v1.0/{path}"
        headers = {"Content-Type": "application/json"}
        if auth:
            headers["Authorization"] = f"Bearer {self._get_token()}"
        last_err: Exception | None = None
        for attempt in range(self.retries + 1):
            if attempt:
                time.sleep(min(2 ** attempt, 30))
            try:
                resp = self.session.post(url, json=body, headers=headers, timeout=30)
            except requests.RequestException as e:
                last_err = e
                continue
            if resp.status_code == 429 or resp.status_code >= 500:
                last_err = DeyeError(f"HTTP {resp.status_code} from {path}")
                continue
            resp.raise_for_status()
            data = resp.json()
            if data.get("success") is False or str(data.get("code", SUCCESS_CODE)) != SUCCESS_CODE:
                raise DeyeError(f"{path}: code={data.get('code')} msg={data.get('msg')}")
            if self.delay:
                time.sleep(self.delay)
            return data
        raise DeyeError(f"{path} failed after {self.retries + 1} attempts: {last_err}")

    def _get_token(self) -> str:
        if self.token and time.time() < self.token_expires - 60:
            return self.token
        body = {"appSecret": self.app_secret, "password": self.pwd_sha256, **self.login}
        data = self._request(f"account/token?appId={self.app_id}", body, auth=False)
        token = data.get("accessToken") or (data.get("data") or {}).get("accessToken")
        if not token:
            raise DeyeError(f"No accessToken in token response: {data.get('msg')}")
        # The swagger shows the token may come back already prefixed with "Bearer ".
        if token.lower().startswith("bearer "):
            token = token[7:]
        self.token = token
        self.token_expires = time.time() + float(data.get("expiresIn") or 3600)
        return token

    def stations(self) -> list[dict]:
        out, page = [], 1
        while True:
            data = self._request("station/list", {"page": page, "size": 200})
            items = data.get("stationList") or []
            out.extend(items)
            if len(items) < 200 or len(out) >= int(data.get("total") or 0):
                return out
            page += 1

    def station_frames(self, station_id: int, day: date) -> list[dict]:
        """Power frames (~5 min) for one day — what the website's history export contains."""
        data = self._request("station/history", {
            "stationId": station_id, "granularity": 1,
            "startAt": day.isoformat(), "endAt": (day + timedelta(days=1)).isoformat(),
        })
        return data.get("stationDataItems") or []

    def station_daily(self, station_id: int, start: date, end_exclusive: date) -> list[dict]:
        """Daily energy totals, start..end_exclusive.

        The docs say "up to 31 days", but the API answers a 31-day span with
        code 2101012 "should be within 31 days", so callers use 30.
        """
        data = self._request("station/history", {
            "stationId": station_id, "granularity": 2,
            "startAt": start.isoformat(), "endAt": end_exclusive.isoformat(),
        })
        return data.get("stationDataItems") or []

    # --- read-only settings. Never call the /order/* endpoints from here: they change the inverter.

    def station_detail(self, station_id: int) -> dict:
        return strip_envelope(self._request("station/detail", {"stationId": station_id})).get("station") or {}

    def station_devices(self, station_id: int) -> list[dict]:
        out, page = [], 1
        while True:
            data = self._request("station/device", {"stationIds": [station_id], "page": page, "size": 200})
            items = data.get("deviceListItems") or []
            out.extend(items)
            if len(items) < 200 or len(out) >= int(data.get("total") or 0):
                return out
            page += 1

    def device_latest(self, device_sns: list[str]) -> list[dict]:
        out = []
        for i in range(0, len(device_sns), 10):  # API limit: 10 devices per call
            out.extend(self._request("device/latest", {"deviceList": device_sns[i:i + 10]}).get("deviceDataList") or [])
        return out

    def device_measure_points(self, sn: str, device_type: str) -> dict:
        return strip_envelope(self._request("device/measurePoints", {"deviceSn": sn, "deviceType": device_type}))

    def device_config(self, sn: str, kind: str) -> dict:
        """kind: battery, system or tou (/v1.0/config/<kind>)."""
        return strip_envelope(self._request(f"config/{kind}", {"deviceSn": sn}))

    def read_dynamic_control(self, sn: str, timeout: float = 90, poll: float = 5) -> dict:
        """Ask the inverter for its current settings, then poll for the answer.

        This sends a *read* command through the data logger; it doesn't change anything.
        """
        order = self._request("strategy/dynamicControl/read", {"deviceSn": sn})
        order_id = order.get("orderId")
        if not order_id:
            raise DeyeError(f"no orderId in dynamicControl/read response: {order.get('msg')}")
        deadline = time.time() + timeout
        last_err = None
        while time.time() < deadline:
            time.sleep(poll)
            try:
                result = strip_envelope(self._request("strategy/dynamicControl/readResult", {"orderId": order_id}))
            except DeyeError as e:  # typically "still waiting for the device"
                last_err = e
                continue
            if any(v not in (None, [], "") for v in result.values()):
                return result
        raise DeyeError(f"inverter didn't answer within {timeout:.0f}s (order {order_id}): {last_err}")


def strip_envelope(data: dict) -> dict:
    return {k: v for k, v in data.items() if k not in ("code", "msg", "requestId", "success")}


# --------------------------------------------------------------------------- transforms

def parse_timestamp(value, tz: ZoneInfo) -> datetime | None:
    """Deye timestamps are epoch seconds (sometimes ms, sometimes as strings)."""
    if value in (None, ""):
        return None
    try:
        num = float(value)
    except (TypeError, ValueError):
        try:
            dt = datetime.fromisoformat(str(value))
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=tz)
    if num > 10_000_000_000:
        num /= 1000
    return datetime.fromtimestamp(num, tz)


def to_float(value) -> float | None:
    if value in (None, "") or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def frames_to_rows(items: list[dict], tz: ZoneInfo, power_divisor: float) -> list[dict]:
    """Normalise raw frame items: local time, power fields in kW, only the useful fields."""
    rows = {}
    for item in items:
        ts = parse_timestamp(item.get("timeStamp"), tz)
        if ts is None:
            continue
        row = {"time": ts.strftime("%Y-%m-%d %H:%M:%S")}
        for key in POWER_FIELDS:
            num = to_float(item.get(key))
            row[f"{key}_kW"] = None if num is None else round(num / power_divisor, 4)
        for key in FRAME_EXTRA_FIELDS:
            row[key] = item.get(key)
        rows[row["time"]] = row
    return [rows[k] for k in sorted(rows)]


def write_csv(path: Path, rows: list[dict], lead: list[str] | None = None) -> None:
    lead = lead or []
    cols = list(lead)
    for row in rows:
        for key in row:
            if key not in cols:
                cols.append(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    tmp.replace(path)


def frame_columns() -> list[str]:
    """Website export's columns first (Production, Consumption, Grid, Battery, SOC), then the rest."""
    return ["time"] + [f"{k}_kW" for k in POWER_FIELDS[:4]] + ["batterySOC"] + \
        [f"{k}_kW" for k in POWER_FIELDS[4:]] + ["generationRatio"]


def trim_frame_file(path: Path) -> bool:
    """Rewrite a frame file saved by an older version to the current columns. True if changed."""
    with path.open(newline="") as f:
        header = next(csv.reader(f), [])
    cols = frame_columns()
    if header == cols:
        return False
    rows = [{c: r.get(c) for c in cols} for r in read_csv(path)]
    write_csv(path, rows, cols)
    return True


def hourly_rollup(frame_rows: list[dict]) -> list[dict]:
    """kWh per hour = mean power over the hour's frames x 1 h.

    `samples` shows how many frames fed each hour (12 for a full hour of
    5-minute data), so gaps are visible rather than silently filled.
    """
    buckets: dict[str, list[dict]] = defaultdict(list)
    for row in frame_rows:
        buckets[row["time"][:13] + ":00:00"].append(row)

    out = []
    for hour in sorted(buckets):
        rows = buckets[hour]
        rec = {"hour": hour, "samples": len(rows)}
        power_cols = [c for c in rows[0] if c.endswith("_kW")]
        for col in power_cols:
            raw = [to_float(r.get(col)) for r in rows]
            if all(v is None for v in raw):
                continue
            # Deye leaves a field blank when it is zero (e.g. purchasePower when
            # not buying), so a blank in a frame counts as 0 kW, not as missing.
            vals = [v or 0.0 for v in raw]
            name = col[:-3]
            rec[f"{name}_kWh"] = round(sum(vals) / len(vals), 4)
            if name in SIGNED_FIELDS:
                rec[f"{name}_pos_kWh"] = round(sum(v for v in vals if v > 0) / len(vals), 4)
                rec[f"{name}_neg_kWh"] = round(-sum(v for v in vals if v < 0) / len(vals), 4)
        socs = [v for v in (to_float(r.get("batterySOC")) for r in rows) if v is not None]
        if socs:
            rec["batterySOC_min"] = min(socs)
            rec["batterySOC_max"] = max(socs)
            rec["batterySOC_end"] = socs[-1]
        out.append(rec)
    return out


def read_csv(path: Path) -> list[dict]:
    with path.open(newline="") as f:
        return list(csv.DictReader(f))


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


# --------------------------------------------------------------------------- commands

def resolve_station(client: DeyeClient, env: dict, args) -> int:
    """Pick the station, and default --tz / --start from its details if not given."""
    sid = args.station or env.get("DEYE_STATION_ID")
    station = None
    needs_tz = hasattr(args, "tz") and not args.tz
    needs_start = hasattr(args, "start") and not args.start
    if not sid or needs_start or needs_tz:
        stations = client.stations()
        if sid:
            station = next((s for s in stations if str(s.get("id")) == str(sid)), None)
        elif len(stations) == 1:
            station = stations[0]
        else:
            listing = "\n".join(f"  {s.get('id')}  {s.get('name')}" for s in stations)
            raise SystemExit(f"Found {len(stations)} stations; pass --station or set DEYE_STATION_ID:\n{listing}")
        sid = sid or station["id"]
    station = station or {}
    if needs_tz:
        args.tz = station.get("regionTimezone") or None
    if not hasattr(args, "start"):
        return int(sid)
    if not args.start:
        started = parse_timestamp(station.get("startOperatingTime"), ZoneInfo(args.tz) if getattr(args, "tz", None) else _local_tz())
        if not started:
            raise SystemExit("Could not find when the station started; pass --start YYYY-MM-DD.")
        args.start = started.date()
        print(f"Starting from the station's start date, {args.start}")
    if args.start > args.end:
        raise SystemExit("--start is after --end")
    return int(sid)


def cmd_stations(client: DeyeClient, args, env) -> None:
    for s in client.stations():
        print(f"{s.get('id')}\t{s.get('name')}\t{s.get('locationAddress') or ''}")


def cmd_frames(client: DeyeClient, args, env) -> None:
    station = resolve_station(client, env, args)
    tz = ZoneInfo(args.tz) if args.tz else _local_tz()
    out_dir = Path(args.out) / "frames"
    today = datetime.now(tz).date()
    fetched = skipped = trimmed = failed = 0
    for day in daterange(args.start, args.end):
        path = out_dir / f"{day.isoformat()}.csv"
        # Today's (or a future) file is incomplete, so always refetch it.
        if path.exists() and not args.refetch and day < today:
            skipped += 1
            trimmed += trim_frame_file(path)
            continue
        try:
            items = client.station_frames(station, day)
        except DeyeError as e:
            print(f"{day}: ERROR {e}", file=sys.stderr)
            failed += 1
            continue
        rows = frames_to_rows(items, tz, args.power_divisor)
        write_csv(path, rows, frame_columns())
        fetched += 1
        print(f"{day}: {len(rows)} frames")
    print(f"frames: {fetched} days fetched, {skipped} already on disk, {failed} failed -> {out_dir}")
    if trimmed:
        print(f"frames: removed empty columns from {trimmed} older file(s)")
    if failed:
        print("Re-run the same command to retry failed days.", file=sys.stderr)


def cmd_hourly(client, args, env) -> None:
    frames_dir = Path(args.out) / "frames"
    files = sorted(frames_dir.glob("*.csv"))
    if not files:
        raise SystemExit(f"No frame files in {frames_dir}; run the 'frames' command first.")
    rows = []
    for f in files:
        rows.extend(read_csv(f))
    hourly = hourly_rollup(rows)
    path = Path(args.out) / "hourly.csv"
    write_csv(path, hourly, ["hour", "samples"])
    print(f"hourly: {len(hourly)} hours from {len(files)} days -> {path}")


def cmd_daily(client: DeyeClient, args, env) -> None:
    station = resolve_station(client, env, args)
    rows: dict[str, dict] = {}
    failed = 0
    start = args.start
    while start <= args.end:
        end_excl = min(start + timedelta(days=DAILY_CHUNK_DAYS), args.end + timedelta(days=1))
        try:
            items = client.station_daily(station, start, end_excl)
        except DeyeError as e:
            print(f"daily: {start} .. {end_excl - timedelta(days=1)}: ERROR {e}", file=sys.stderr)
            failed += 1
            start = end_excl
            continue
        for item in items:
            y, m, d = item.get("year"), item.get("month"), item.get("day")
            if not (y and m and d):
                continue
            key = f"{int(y):04d}-{int(m):02d}-{int(d):02d}"
            if args.start.isoformat() <= key <= args.end.isoformat():
                rows[key] = {"date": key, **{k: v for k, v in item.items()
                                             if k not in ("year", "month", "day")}}
        print(f"daily: {start} .. {end_excl - timedelta(days=1)}")
        start = end_excl
    path = Path(args.out) / "daily.csv"
    write_csv(path, [rows[k] for k in sorted(rows)], ["date"])
    print(f"daily: {len(rows)} days -> {path}")
    if failed:
        print(f"daily: {failed} chunk(s) failed; re-run 'daily' to retry.", file=sys.stderr)


CONFIG_KINDS = ("battery", "system", "tou")
# Device types the /config/* endpoints apply to.
CONFIGURABLE_TYPES = {"INVERTER", "MICRO_STORAGE_IN_ONE"}


def _try(fn, *a):
    try:
        return fn(*a)
    except DeyeError as e:
        return {"error": str(e)}


def collect_config(client: DeyeClient, station_id: int, read_inverter: bool = False) -> dict:
    """Snapshot of everything readable about the station's setup. Errors are kept, not fatal."""
    devices = client.station_devices(station_id)
    latest_list = _try(client.device_latest, [d["deviceSn"] for d in devices]) if devices else []
    latest_error = latest_list.get("error") if isinstance(latest_list, dict) else None
    latest = {d.get("deviceSn"): d for d in latest_list} if not latest_error else {}
    out_devices = []
    for dev in devices:
        sn, dtype = dev.get("deviceSn"), dev.get("deviceType") or "INVERTER"
        entry = {**dev, "latest": latest.get(sn) or ({"error": latest_error} if latest_error else None)}
        if dtype in CONFIGURABLE_TYPES:
            entry["measurePoints"] = _try(client.device_measure_points, sn, dtype)
            entry["config"] = {kind: _try(client.device_config, sn, kind) for kind in CONFIG_KINDS}
            if read_inverter:
                print(f"{sn}: asking the inverter for its settings (can take a minute)...")
                entry["config"]["dynamicControl"] = _try(client.read_dynamic_control, sn)
        out_devices.append(entry)
    return {
        "exportedAt": datetime.now().astimezone().isoformat(timespec="seconds"),
        "station": _try(client.station_detail, station_id),
        "devices": out_devices,
    }


def flatten_config(snapshot: dict) -> list[dict]:
    """One row per setting: device, section, key, value, unit. Easy to diff between snapshots."""
    rows = []

    def add(device, section, key, value, unit=""):
        if isinstance(value, (dict, list)):
            value = json.dumps(value, ensure_ascii=False)
        rows.append({"device": device, "section": section, "key": key, "value": value, "unit": unit})

    for k, v in (snapshot.get("station") or {}).items():
        add("station", "station", k, v)
    for dev in snapshot.get("devices", []):
        sn = f"{dev.get('deviceType')}:{dev.get('deviceSn')}"
        for k, v in dev.items():
            if k not in ("latest", "config", "measurePoints"):
                add(sn, "device", k, v)
        for section, cfg in (dev.get("config") or {}).items():
            for k, v in cfg.items():
                if k in ("timeUseSettingItems",) and isinstance(v, list):
                    for i, slot in enumerate(v, 1):
                        for sk, sv in slot.items():
                            add(sn, section, f"slot{i}.{sk}", sv)
                else:
                    add(sn, section, k, v)
        latest = dev.get("latest") or {}
        for item in latest.get("dataList") or []:
            add(sn, "latest", item.get("key") or item.get("name"), item.get("value"), item.get("unit") or "")
        mp = dev.get("measurePoints") or {}
        if mp.get("measurePoints"):
            add(sn, "measurePoints", "names", ", ".join(mp["measurePoints"]))
    return rows


def cmd_config(client: DeyeClient, args, env) -> None:
    station = resolve_station(client, env, args)
    snapshot = collect_config(client, station, args.read_inverter)
    out_dir = Path(args.out) / "config"
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    json_path = out_dir / f"config-{stamp}.json"
    json_path.write_text(json.dumps(snapshot, indent=2, ensure_ascii=False))
    rows = flatten_config(snapshot)
    csv_path = out_dir / f"config-{stamp}.csv"
    write_csv(csv_path, rows, ["device", "section", "key", "value", "unit"])
    for dev in snapshot["devices"]:
        errs = [f"{s}: {c['error']}" for s, c in (dev.get("config") or {}).items() if "error" in c]
        for e in errs:
            print(f"{dev.get('deviceSn')}: {e}", file=sys.stderr)
    print(f"config: {len(snapshot['devices'])} device(s), {len(rows)} settings -> {json_path} and {csv_path.name}")


def cmd_all(client, args, env) -> None:
    cmd_frames(client, args, env)
    cmd_hourly(client, args, env)
    cmd_daily(client, args, env)


def _local_tz():
    return datetime.now().astimezone().tzinfo


def parse_date(s: str) -> date:
    return date.fromisoformat(s)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--out", default="export", help="output directory (default: export)")
    p.add_argument("--delay", type=float, default=0.5, help="seconds between API calls (default 0.5)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("stations", help="list your stations and their ids")

    def ranged(name, help_):
        sp = sub.add_parser(name, help=help_)
        sp.add_argument("--station", help="station id (default: DEYE_STATION_ID or your only station)")
        sp.add_argument("--start", type=parse_date, help="first day, YYYY-MM-DD (default: when the station started)")
        sp.add_argument("--end", type=parse_date, default=date.today(), help="last day, YYYY-MM-DD (default today)")
        return sp

    for name, help_ in (("frames", "5-minute power frames, one CSV per day"),
                        ("all", "frames + hourly + daily")):
        sp = ranged(name, help_)
        sp.add_argument("--tz", help="IANA time zone for timestamps, e.g. Asia/Manila (default: the station's time zone)")
        sp.add_argument("--refetch", action="store_true", help="refetch days already on disk")
        sp.add_argument("--power-divisor", type=float, default=1000.0,
                        help="divide API power values by this to get kW (default 1000: API reports W)")
    ranged("daily", "Deye's daily energy totals (kWh) into daily.csv")
    sub.add_parser("hourly", help="roll up downloaded frames into hourly.csv (no API calls)")
    sp = sub.add_parser("config", help="snapshot of station, device, battery, work-mode and TOU settings")
    sp.add_argument("--station", help="station id (default: DEYE_STATION_ID or your only station)")
    sp.add_argument("--read-inverter", action="store_true",
                    help="also send a read command to the inverter for its live settings "
                         "(grid charge, solar sell, TOU days, work mode); read-only, takes up to ~90 s")
    return p


COMMANDS = {"stations": cmd_stations, "frames": cmd_frames, "hourly": cmd_hourly,
            "daily": cmd_daily, "all": cmd_all, "config": cmd_config}


def main(argv=None) -> None:
    load_dotenv(Path(__file__).with_name(".env"))
    load_dotenv(Path.cwd() / ".env")
    args = build_parser().parse_args(argv)
    env = dict(os.environ)
    client = None if args.command == "hourly" else DeyeClient.from_env(env, delay=args.delay)
    COMMANDS[args.command](client, args, env)


if __name__ == "__main__":
    main()
