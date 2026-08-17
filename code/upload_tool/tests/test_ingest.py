from decimal import Decimal
from pathlib import Path
import sys

import pytest


TOOL_DIR = Path(__file__).resolve().parents[1]
ROOT = TOOL_DIR.parents[1]
sys.path.insert(0, str(TOOL_DIR))

from ingest import decode_log, extract_log_identity, parse_log  # noqa: E402


def log_files():
    folder = ROOT / "日志文件"
    original = folder / "合成工艺及log文件-P26080101 CH_5log.txt"
    modified = folder / "合成工艺及log文件-P26080101 CH_5log_模拟参数调整.txt"
    if not original.exists() or not modified.exists():
        pytest.skip("工作区中的 CH5 回归日志不存在")
    return original, modified


def ami_step(parsed, cycle_no):
    cycle = next(row for row in parsed["cycles"] if row["cycle_no"] == cycle_no)
    return next(row for row in cycle["inner_steps"] if row["reagent_code"] == "AMI")


def test_real_logs_capture_three_adjustment_types():
    original_path, modified_path = log_files()
    original = parse_log(original_path.read_bytes(), original_path.name)
    modified = parse_log(modified_path.read_bytes(), modified_path.name)

    assert [ami_step(modified, number)["event_count"] for number in (1, 2, 3)] == [3, 2, 1]
    assert ami_step(original, 2)["event_count"] == 3
    assert ami_step(original, 3)["event_count"] == 3

    assert len(ami_step(original, 4)["events"][0]["pulses"]) == 4
    assert len(ami_step(modified, 4)["events"][0]["pulses"]) == 3

    old_event = ami_step(original, 5)["events"][0]
    new_event = ami_step(modified, 5)["events"][0]
    assert new_event["injection_volumes_ul"] == [Decimal("67.5"), Decimal("45")]
    assert old_event["injection_volumes_ul"] == [Decimal("75"), Decimal("50")]
    assert new_event["pulses"] == [[0, 4500], [13.5, 27000], [18, 27000], [7200, 900]]

    assert original["issues"] == []
    assert modified["issues"] == []
    assert modified["event_total"] == original["event_total"] - 3
    assert modified["operation_total"] == original["operation_total"] - 13


def test_parser_supports_decimal_drain_and_utf8_bom():
    raw = (
        "2026/8/1 10:00:00: 01| Inject-AMI-75.5uL into -A;Temperature：30.1Humidity:62.3\n"
        "2026/8/1 10:00:01: Drain:13.5 Wait:27000\n"
        "2026/8/1 10:00:02: Synthesis completed at :2026/8/1 10:00:02\n"
    ).encode("utf-8-sig")
    parsed = parse_log(raw, "P26080101_CH_5.txt")
    assert parsed["encoding"] == "utf-8-sig"
    assert parsed["cycles"][0]["inner_steps"][0]["events"][0]["pulses"] == [[13.5, 27000]]
    assert parsed["status"] == "COMPLETED"


def test_unknown_line_is_audited_instead_of_silently_dropped():
    raw = (
        "unexpected header\n"
        "2026/8/1 10:00:00: 01| Inject-AMI-75uL into -A;Temperature:30Humidity:60\n"
        "2026/8/1 10:00:01: Drain:10 Wait:1000\n"
    ).encode()
    parsed = parse_log(raw, "P26080101 CH_5.txt")
    assert [(item["line_no"], item["issue_code"]) for item in parsed["issues"]] == [
        (1, "UNRECOGNIZED_LINE")
    ]


def test_invalid_filename_and_encoding_are_rejected():
    with pytest.raises(ValueError, match="板号/通道"):
        extract_log_identity("invalid.txt")
    with pytest.raises(ValueError, match="编码"):
        decode_log(b"\xff\xfe\x00\x81")
