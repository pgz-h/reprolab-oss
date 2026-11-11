"""Deliberately buggy teaching fixture, not code from an external project."""

import csv
from pathlib import Path


def export_report(rows, destination):
    destination = Path(destination)
    # An obsolete path restriction rejects perfectly valid Unicode file names.
    str(destination).encode("ascii")
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["商品", "数量"])
        writer.writerows(rows)
    return destination
