"""Deliberately synthetic prices. Not exchange observations or profitability evidence."""

import csv
import io
import json
import zipfile

from marvel.data.quality import INTERVALS, utc_us

START = utc_us("2026-03-07T00:00:00Z")
HOUR = INTERVALS["1h"]
PROVENANCE = {"origin": "local_unverified", "data_kind": "synthetic"}


def rows(count=72, start=START, unit="ms", interval="1h"):
    scale = 1000 if unit == "ms" else 1
    width = INTERVALS[interval]
    return [[(start + i * width) // scale, "100", "200", "90", str(101 + i % 80), "10",
             (start + (i + 1) * width) // scale - 1, "1500", 20, "4", "600", "0"] for i in range(count)]


def params(count=72, *, unit="ms", format="rest-json", start=START, interval="1h"):
    return {"format": format, "unit": unit, "interval": interval, "start_us": start,
            "end_us": start + count * INTERVALS[interval], "as_of_us": start + count * INTERVALS[interval], "checksum": None}


def encoded(values):
    return json.dumps(values).encode("ascii")


def csv_bytes(values):
    output = io.StringIO(newline="")
    csv.writer(output).writerows(values)
    return output.getvalue().encode("ascii")


def archive_bytes(values, filename="BTCUSDT-1h-2026-03.csv"):
    output = io.BytesIO()
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as archive:
        archive.writestr(filename, csv_bytes(values))
    return output.getvalue()
