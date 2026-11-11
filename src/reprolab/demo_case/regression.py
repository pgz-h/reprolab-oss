"""One invariant: the same real export must work with a Chinese filename."""

import csv
from report_exporter import export_report


def test_export_handles_chinese_filename(tmp_path):
    destination = tmp_path / "销售报表.csv"
    result = export_report([["咖啡", 3]], destination)
    assert result == destination
    assert destination.is_file()
    with destination.open(encoding="utf-8", newline="") as stream:
        assert list(csv.reader(stream)) == [["商品", "数量"], ["咖啡", "3"]]
