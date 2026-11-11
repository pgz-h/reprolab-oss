"""Corrected teaching fixture: preserve the Unicode path."""

import csv
from pathlib import Path


def export_report(rows, destination):
    destination = Path(destination)
    with destination.open("w", encoding="utf-8", newline="") as stream:
        writer = csv.writer(stream)
        writer.writerow(["商品", "数量"])
        writer.writerows(rows)
    return destination
