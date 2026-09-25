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
| `frames/YYYY-MM-DD.csv` | ~5-minute power readings in kW, the same data as the website's History export (see below) |
| `hourly.csv` | kWh per hour for each power column (average kW over the hour × 1 h), with `samples` = number of readings behind it (12 = a complete hour). Grid and battery are also split into `_pos_kWh` / `_neg_kWh` |
| `daily.csv` | Deye's daily totals in kWh: `generationValue`, `consumptionValue`, `purchaseValue` (bought from grid), `gridValue` (fed into grid), `chargeValue`, `dischargeValue`, … |

How the frame columns line up with the website export:

| Website column | API field |
| --- | --- |
| Production (kW) | `generationPower_kW` |
| Consumption (kW) | `consumptionPower_kW` |
| Grid (kW) | `gridPower_kW` (also `purchasePower_kW` = bought, `wirePower_kW` = fed in) |
| Battery (kW) | `batteryPower_kW` (positive = discharging) |
| SOC (%) | `batterySOC` |

The API doesn't return the website's PV, Generator and Grid-tied Inverter
columns at station level. Those would need the per-device `/v1.0/device/history`
endpoint (see [stoflom/deye-logger](https://github.com/stoflom/deye-logger)).

The API documentation doesn't give units. The tool assumes power comes back in
watts and divides by 1000. Compare one day's `frames/*.csv` with a website
export. If the values are 1000× too small, re-run with `--power-divisor 1 --refetch`.

## Tests

```sh
pip install pytest && python -m pytest tests
```
