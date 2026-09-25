import sys
from datetime import date
from pathlib import Path
from zoneinfo import ZoneInfo

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deye_export as dx  # noqa: E402

TZ = ZoneInfo("Asia/Manila")


class FakeResponse:
    def __init__(self, body, status=200):
        self.body, self.status_code = body, status

    def json(self):
        return self.body

    def raise_for_status(self):
        pass


class FakeSession:
    """Answers Deye endpoints from a dict of path -> callable(body) -> response body."""

    def __init__(self, routes):
        self.routes, self.calls = routes, []

    def post(self, url, json=None, headers=None, timeout=None):
        path = url.split("/v1.0/", 1)[1].split("?", 1)[0]
        self.calls.append((path, json, headers))
        return FakeResponse(self.routes[path](json))


def ok(**kw):
    return {"code": "1000000", "success": True, "msg": "success", **kw}


def make_client(routes):
    session = FakeSession({"account/token": lambda b: ok(accessToken="tok", expiresIn=5000), **routes})
    return dx.DeyeClient("https://x", "app", "sec", {"email": "a@b"}, "hash", session=session, delay=0), session


def test_token_then_bearer_and_password_hash():
    client, session = make_client({"station/list": lambda b: ok(total=1, stationList=[{"id": 7, "name": "Home"}])})
    assert client.stations() == [{"id": 7, "name": "Home"}]
    token_call, list_call = session.calls
    assert token_call[1] == {"appSecret": "sec", "password": "hash", "email": "a@b"}
    assert list_call[2]["Authorization"] == "Bearer tok"
    assert dx.password_hash({"DEYE_PASSWORD": "123456"}) == \
        "8d969eef6ecad3c29a3a629280e686cf0c3f5d5a86aff3ca12020c923adc6c92"


def test_api_error_raises():
    client, _ = make_client({"station/list": lambda b: {"code": "2101019", "success": False, "msg": "bad"}})
    try:
        client.stations()
    except dx.DeyeError as e:
        assert "bad" in str(e)
    else:
        raise AssertionError("expected DeyeError")


def test_frames_to_rows_converts_watts_and_local_time():
    # 2026-09-26 00:00 and 00:05 Manila time, out of order and with a duplicate.
    items = [
        {"timeStamp": 1790352300, "generationPower": 0, "consumptionPower": 2250.0, "batteryPower": 2420, "batterySOC": 58},
        {"timeStamp": "1790352000", "generationPower": 0, "consumptionPower": 2090.0, "batteryPower": 2250, "batterySOC": 58},
        {"timeStamp": 1790352000000, "generationPower": 0, "consumptionPower": 2090.0, "batteryPower": 2250, "batterySOC": 58},
    ]
    rows = dx.frames_to_rows(items, TZ, 1000)
    assert [r["time"] for r in rows] == ["2026-09-26 00:00:00", "2026-09-26 00:05:00"]
    assert rows[0]["consumptionPower_kW"] == 2.09
    assert rows[0]["batteryPower_kW"] == 2.25
    assert rows[0]["batterySOC"] == 58


def test_hourly_rollup():
    rows = [{"time": f"2026-09-26 00:{m:02d}:00", "consumptionPower_kW": "2.0",
             "batteryPower_kW": "3.0" if m < 30 else "-1.0", "batterySOC": str(60 - m // 5)}
            for m in range(0, 60, 5)]
    rows.append({"time": "2026-09-26 01:00:00", "consumptionPower_kW": "1.0", "batteryPower_kW": "",
                 "batterySOC": "48"})
    h0, h1 = dx.hourly_rollup(rows)
    assert h0["samples"] == 12 and h0["consumptionPower_kWh"] == 2.0
    assert h0["batteryPower_pos_kWh"] == 1.5 and h0["batteryPower_neg_kWh"] == 0.5
    assert h0["batteryPower_kWh"] == 1.0
    assert (h0["batterySOC_min"], h0["batterySOC_max"], h0["batterySOC_end"]) == (49, 60, 49)
    assert h1["samples"] == 1 and "batteryPower_kWh" not in h1


def test_hourly_counts_blank_frames_as_zero():
    rows = [{"time": f"2026-09-25 11:{m:02d}:00", "purchasePower_kW": "0.6" if m == 10 else ""}
            for m in range(0, 60, 5)]
    (h,) = dx.hourly_rollup(rows)
    assert h["purchasePower_kWh"] == 0.05  # 0.6 kW for 5 of 60 minutes


def test_frames_command_is_resumable(tmp_path):
    requested = []

    def history(body):
        requested.append(body)
        return ok(stationDataItems=[{"timeStamp": 1790352000, "consumptionPower": 1000}])

    client, _ = make_client({"station/history": history})
    args = dx.build_parser().parse_args(
        ["--out", str(tmp_path), "frames", "--station", "7", "--start", "2026-09-01",
         "--end", "2026-09-02", "--tz", "Asia/Manila"])
    dx.cmd_frames(client, args, {})
    assert [b["startAt"] for b in requested] == ["2026-09-01", "2026-09-02"]
    assert requested[0] == {"stationId": 7, "granularity": 1, "startAt": "2026-09-01", "endAt": "2026-09-02"}
    assert (tmp_path / "frames" / "2026-09-01.csv").exists()

    requested.clear()
    dx.cmd_frames(client, args, {})
    assert requested == []  # both days already on disk

    dx.cmd_hourly(None, args, {})
    assert "consumptionPower_kWh" in (tmp_path / "hourly.csv").read_text()


def test_daily_command_chunks_by_30_days(tmp_path):
    requested = []

    def history(body):
        requested.append((body["startAt"], body["endAt"]))
        start = date.fromisoformat(body["startAt"])
        # Include the (possibly inclusive) end day to check it is de-duplicated.
        end = date.fromisoformat(body["endAt"])
        days = [start + dx.timedelta(days=i) for i in range((end - start).days + 1)]
        return ok(stationDataItems=[{"year": d.year, "month": d.month, "day": d.day,
                                     "consumptionValue": 10.0} for d in days])

    client, _ = make_client({"station/history": history})
    args = dx.build_parser().parse_args(
        ["--out", str(tmp_path), "daily", "--station", "7", "--start", "2026-01-01", "--end", "2026-03-05"])
    dx.cmd_daily(client, args, {})
    assert requested == [("2026-01-01", "2026-01-31"), ("2026-01-31", "2026-03-02"), ("2026-03-02", "2026-03-06")]
    lines = (tmp_path / "daily.csv").read_text().splitlines()
    assert lines[0].startswith("date,") and len(lines) == 1 + 64


def test_token_with_bearer_prefix_is_not_doubled():
    client, session = make_client({"station/list": lambda b: ok(total=0, stationList=[])})
    session.routes["account/token"] = lambda b: ok(accessToken="Bearer tok", expiresIn="5183999")
    client.stations()
    assert session.calls[-1][2]["Authorization"] == "Bearer tok"


def test_station_defaults_for_tz_and_start(tmp_path):
    station = {"id": 7, "name": "Home", "regionTimezone": "Asia/Manila", "startOperatingTime": 1790352000}
    requested = []
    client, _ = make_client({
        "station/list": lambda b: ok(total=1, stationList=[station]),
        "station/history": lambda b: requested.append(b["startAt"]) or ok(stationDataItems=[]),
    })
    args = dx.build_parser().parse_args(["--out", str(tmp_path), "frames", "--end", "2026-09-27"])
    dx.cmd_frames(client, args, {})
    assert args.tz == "Asia/Manila"
    assert requested == ["2026-09-26", "2026-09-27"]


def test_daily_chunk_error_is_reported_not_fatal(tmp_path, capsys):
    def history(body):
        if body["startAt"] == "2026-01-01":
            return {"code": "2101012", "success": False, "msg": "should be within 31 days"}
        return ok(stationDataItems=[{"year": 2026, "month": 2, "day": 1, "consumptionValue": 5.0}])

    client, _ = make_client({"station/history": history})
    args = dx.build_parser().parse_args(
        ["--out", str(tmp_path), "daily", "--station", "7", "--start", "2026-01-01", "--end", "2026-02-10"])
    dx.cmd_daily(client, args, {})
    assert "2101012" in capsys.readouterr().err
    assert "2026-02-01" in (tmp_path / "daily.csv").read_text()


def test_frame_rows_keep_only_useful_columns():
    item = {"timeStamp": 1790352000, "generationPower": 1371, "consumptionPower": 570, "wirePower": 0,
            "batteryPower": -593, "batterySOC": 55, "chargePower": -593, "generationRatio": 100.0,
            "generationValue": None, "irradiateIntensity": None, "pr": None, "year": 2026, "month": 9, "day": 26}
    (row,) = dx.frames_to_rows([item], TZ, 1000)
    assert set(row) == set(dx.frame_columns())
    assert len(dx.frame_columns()) == 11
    assert row["wirePower_kW"] == 0.0 and row["chargePower_kW"] == -0.593 and row["purchasePower_kW"] is None


def test_old_frame_files_are_trimmed_without_api_calls(tmp_path):
    old = tmp_path / "frames" / "2026-09-01.csv"
    old.parent.mkdir(parents=True)
    old.write_text("time,generationPower_kW,consumptionPower_kW,gridPower_kW,purchasePower_kW,wirePower_kW,"
                   "batteryPower_kW,batterySOC,chargePower_kW,dischargePower_kW,irradiateIntensity_kW,"
                   "generationValue,generationRatio,year,month,day\n"
                   "2026-09-01 00:00:00,0.002,0.83,,,0.0,0.986,69.0,,0.986,,,0.0,2026,9,1\n")
    client, session = make_client({})
    args = dx.build_parser().parse_args(
        ["--out", str(tmp_path), "frames", "--station", "7", "--start", "2026-09-01",
         "--end", "2026-09-01", "--tz", "Asia/Manila"])
    dx.cmd_frames(client, args, {})
    assert session.calls == []
    header, line = old.read_text().splitlines()
    assert header.split(",") == dx.frame_columns()
    assert line == "2026-09-01 00:00:00,0.002,0.83,0.0,0.986,69.0,,,,0.986,0.0"
    assert dx.trim_frame_file(old) is False
