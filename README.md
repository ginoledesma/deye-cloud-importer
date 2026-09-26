# deye-cloud-importer

Bulk-export your Deye Cloud station history — the same 5-minute data the
deyecloud.com "History" export gives you (`PlantsDetails-History.xlsx`) — for
every day since your system was installed, plus hourly and daily energy totals.

It uses the official [Deye Cloud OpenAPI](https://developer.deyecloud.com/api)
(`/v1.0/station/history`), so no browser scraping.

## Setup

1. Sign in at <https://developer.deyecloud.com> with your Deye Cloud account and
   create an application to get an **App ID** and **App Secret**.
2. Install and configure:

   ```sh
   pip install -r requirements.txt
   cp .env.example .env   # then fill in app id/secret, login, password, region
   ```

   Use the base URL for your account's data center (`eu1-…` or `us1-…`). If
   login fails with the one, try the other.

## Usage

```sh
python deye_export.py stations        # check login works, list station ids
python deye_export.py all             # everything since the station started
```

Or step by step:

```sh
python deye_export.py frames --start 2024-01-01          # 5-minute data, one CSV per day
python deye_export.py hourly                              # roll frames up into hourly kWh (offline)
python deye_export.py daily  --start 2024-01-01          # Deye's own daily kWh totals
```

`--start` defaults to the station's start-of-operation date and `--end` to
today. Timestamps are in the station's time zone (override with `--tz`).

The `frames` step makes one API call per day and saves each day as its own
file, so you can stop it and run it again: days already downloaded are skipped
(today's file is always refreshed). Use `--refetch` to download everything again.

## Output (`export/`)

| File | What it holds |
| --- | --- |
| `frames/YYYY-MM-DD.csv` | ~5-minute power readings in kW, the same data as the website's History export (see below), plus `purchasePower`/`gridPower` (buying/exporting side of Grid), `chargePower`/`dischargePower` (charging/discharging side of Battery, blank below 0.05 kW) and `generationRatio`. A blank power value means 0 |
| `hourly.csv` | kWh per hour for each power column (average kW over the hour × 1 h), with `samples` = number of readings behind it (12 = a complete hour). Grid and battery are also split in two: `wirePower_pos_kWh` = bought from the grid, `wirePower_neg_kWh` = exported, `batteryPower_pos_kWh` = discharged, `batteryPower_neg_kWh` = charged |
| `daily.csv` | Deye's daily totals in kWh: `generationValue`, `consumptionValue`, `purchaseValue` (bought from grid), `gridValue` (fed into grid), `chargeValue`, `dischargeValue`, … |

How the frame columns line up with the website export (checked against a
full day's export: every reading matches):

| Website column | API field |
| --- | --- |
| Production (kW) | `generationPower_kW` |
| Consumption (kW) | `consumptionPower_kW` |
| Grid (kW) | `wirePower_kW` (positive = buying, negative = exporting) |
| Battery (kW) | `batteryPower_kW` (positive = discharging, negative = charging) |
| SOC (%) | `batterySOC` |

The API doesn't return the website's PV, Generator and Grid-tied Inverter
columns at station level. Without a generator, PV equals Production. The other two
columns need the per-device endpoint. That's `/v1.0/device/history`
(see [stoflom/deye-logger](https://github.com/stoflom/deye-logger)).

Power values come back from the API in watts and are converted to kW.

## Settings snapshot

```sh
python deye_export.py config                  # station, devices, battery, work mode, TOU
python deye_export.py config --read-inverter  # also ask the inverter itself (takes up to ~90 s)
```

Each run writes a timestamped pair to `export/config/`. Keep them to track
changes over time:

- `config-YYYYMMDD-HHMMSS.json`: everything as the API returned it.
- `config-YYYYMMDD-HHMMSS.csv`: one row per setting (`device, section, key, value, unit`),
  so two snapshots can be compared with any diff tool.

| Section | Source | What's in it |
| --- | --- | --- |
| `station` | `/station/detail` | name, location, time zone, installed capacity, grid connection type, start date |
| `device` | `/station/device` | each device (inverter, data logger, ...): serial number, type, online status |
| `battery` | `/config/battery` | battery capacity, low and shutdown SOC, max charge/discharge current |
| `system` | `/config/system` | system work mode, energy pattern, max sell / max solar / zero-export power |
| `tou` | `/config/tou` | time-of-use on/off and each time slot (`slotN.time`, `power`, `soc`, `enableGridCharge`, `enableGeneration`) |
| `dynamicControl` | `/strategy/dynamicControl/read` (only with `--read-inverter`) | settings read from the inverter itself: work mode, grid charge on/off and amps, solar sell on/off, TOU days, time slots including `enableSell` |
| `latest` | `/device/latest` | every value the inverter last reported, with units. This includes readings and any settings it reports |
| `measurePoints` | `/device/measurePoints` | names of all values the device can report |

Battery, system and TOU settings only exist for inverters. If an endpoint
fails for your inverter model, the error is stored in the snapshot and the rest
is still saved.

This command only reads. It never calls the API's `/order/*` endpoints, which
change inverter settings. `--read-inverter` sends a *read* request through
the data logger and waits for the reply. Nothing on the inverter changes.

## Tests

```sh
pip install pytest && python -m pytest tests
```
