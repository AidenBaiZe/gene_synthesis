from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from datetime import datetime
from decimal import Decimal
from pathlib import Path

import xxhash
from psycopg.types.json import Jsonb


PARSER_VERSION = "log-exec-2.0.0"

LINE_RE = re.compile(
    r"^(?P<ts>\d{4}/\d{1,2}/\d{1,2}\s+\d{1,2}:\d{2}:\d{2}):\s*(?P<msg>.*)$"
)
INJECT_RE = re.compile(
    r"^(?P<cycle>\d+)\|\s*Inject-(?P<reagent>[^-]+)-(?P<volume>[\d.,]+)"
    r"(?:uL|ul|µL|μL)\s+into\s+-(?P<target>[^;]*);"
    r"Temperature[：:](?P<temperature>-?\d+(?:\.\d+)?)"
    r"Humidity:(?P<humidity>-?\d+(?:\.\d+)?)$"
)
DRAIN_RE = re.compile(
    r"^Drain:(?P<drain>\d+(?:\.\d+)?)\s+Wait:(?P<wait>\d+(?:\.\d+)?)$"
)
COMPLETED_RE = re.compile(r"^Synthesis completed at\s*:(?P<completed>.+)$")
RUN_CODE_RE = re.compile(r"(?<![A-Za-z0-9])P\d{6,}", re.IGNORECASE)
CHANNEL_RE = re.compile(r"(?<![A-Za-z])CH[_\s-]?(\d+)", re.IGNORECASE)
KNOWN_METADATA_RE = re.compile(
    r"^(?:"
    r"---.*---|"
    r"(?:Initialization|Finailzation|Run_Step):.*|"
    r"(?:RUN NAME|RUN DATE|RUN NOTES|SEQUENCE FILE NAME|SUPPORT):.*|"
    r"ACTUAL SYNTHESIS SEQUENCE:.*|"
    r"[A-Za-z0-9_]+\s+-\s*.*"
    r")$"
)


def decode_log(raw: bytes) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return raw.decode(encoding), encoding
        except UnicodeDecodeError:
            continue
    raise ValueError("日志编码无法识别；当前支持 UTF-8、UTF-8 BOM 和 GB18030。")


def extract_log_identity(original_name: str) -> tuple[str, int]:
    file_name = Path(original_name).name
    run_match = RUN_CODE_RE.search(file_name)
    channel_match = CHANNEL_RE.search(file_name)
    if not run_match or not channel_match:
        raise ValueError(
            f"文件名解析不到板号/通道：{file_name}。需要包含类似 P26080101 和 CH_5。"
        )
    return run_match.group(0).upper(), int(channel_match.group(1))


def _json_num(text: str):
    value = Decimal(text)
    if value == value.to_integral_value():
        return int(value)
    return float(value)


def _issue(line_no: int | None, severity: str, code: str, message: str, raw: str | None):
    return {
        "line_no": line_no,
        "severity": severity,
        "issue_code": code,
        "message": message,
        "raw_line_text": raw,
    }


def parse_log(raw: bytes, original_name: str) -> dict:
    text, encoding = decode_log(raw)
    file_name = Path(original_name).name
    run_code, channel_no = extract_log_identity(file_name)
    recipe_file_name = None
    events = []
    aux_events = []
    issues = []
    current = None
    completed_at = None

    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        match = LINE_RE.match(line)
        if not match:
            issues.append(
                _issue(
                    line_no,
                    "WARNING",
                    "UNRECOGNIZED_LINE",
                    "该行没有标准时间戳，未参与结构化解析。",
                    line,
                )
            )
            continue
        ts = datetime.strptime(match.group("ts"), "%Y/%m/%d %H:%M:%S")
        msg = match.group("msg").strip()
        if msg.startswith("PROCESS FILE NAME:"):
            recipe_file_name = msg.split(":", 1)[1].strip()
            continue
        completed = COMPLETED_RE.match(msg)
        if completed:
            try:
                completed_at = datetime.strptime(
                    completed.group("completed").strip(), "%Y/%m/%d %H:%M:%S"
                )
            except ValueError:
                issues.append(
                    _issue(
                        line_no,
                        "ERROR",
                        "INVALID_COMPLETION_TIME",
                        "完成时间格式无法解析。",
                        line,
                    )
                )
            continue
        inject = INJECT_RE.match(msg)
        if inject:
            current = {
                "cycle_no": int(inject.group("cycle")),
                "reagent_code": inject.group("reagent").strip(),
                "injection_volumes_ul": [
                    Decimal(part) for part in inject.group("volume").strip().split(",")
                ],
                "monomer_code": inject.group("target").strip() or None,
                "temperature_c": Decimal(inject.group("temperature")),
                "humidity_percent": Decimal(inject.group("humidity")),
                "event_time": ts,
                "source_line_no": line_no,
                "raw_line_text": line,
                "pulses": [],
            }
            (aux_events if current["cycle_no"] == 0 else events).append(current)
            continue
        drain = DRAIN_RE.match(msg)
        if drain:
            if current is None:
                issues.append(
                    _issue(
                        line_no,
                        "WARNING",
                        "ORPHAN_DRAIN",
                        "Drain/Wait 前没有可关联的 Inject。",
                        line,
                    )
                )
            else:
                wait_value = Decimal(drain.group("wait"))
                if wait_value != wait_value.to_integral_value():
                    issues.append(
                        _issue(
                            line_no,
                            "ERROR",
                            "NON_INTEGER_WAIT",
                            "Wait 必须是整数毫秒，该段未写入。",
                            line,
                        )
                    )
                else:
                    current["pulses"].append(
                        [_json_num(drain.group("drain")), int(wait_value)]
                    )
            continue
        if not KNOWN_METADATA_RE.match(msg):
            issues.append(
                _issue(
                    line_no,
                    "WARNING",
                    "UNRECOGNIZED_MESSAGE",
                    "带时间戳的消息类型暂未识别，已保留原文。",
                    line,
                )
            )

    for event in events + aux_events:
        if not event["pulses"]:
            issues.append(
                _issue(
                    event["source_line_no"],
                    "WARNING",
                    "INJECT_WITHOUT_DRAIN",
                    "Inject 后没有解析到 Drain/Wait 操作。",
                    event["raw_line_text"],
                )
            )

    cycles = defaultdict(list)
    for event in events:
        cycles[event["cycle_no"]].append(event)

    parsed_cycles = []
    global_event_no = 0
    for cycle_no in sorted(cycles):
        cycle_events = cycles[cycle_no]
        inner_steps = []
        type_seen = defaultdict(int)
        for event in cycle_events:
            if not inner_steps or inner_steps[-1]["reagent_code"] != event["reagent_code"]:
                type_seen[event["reagent_code"]] += 1
                inner_steps.append(
                    {
                        "step_order": len(inner_steps) + 1,
                        "reagent_code": event["reagent_code"],
                        "step_occurrence": type_seen[event["reagent_code"]],
                        "events": [],
                    }
                )
            step = inner_steps[-1]
            global_event_no += 1
            event["inner_event_no"] = len(step["events"]) + 1
            event["global_event_no"] = global_event_no
            step["events"].append(event)
            event["event_no"] = sum(len(item["events"]) for item in inner_steps)
        for step in inner_steps:
            step["pulse_sequence"] = [event["pulses"] for event in step["events"]]
            step["pulse_code"] = ";".join(
                _pulse_code(event["pulses"]) for event in step["events"]
            )
            step["event_count"] = len(step["events"])
            step["operation_count"] = sum(len(event["pulses"]) for event in step["events"])
        parsed_cycles.append(
            {
                "cycle_no": cycle_no,
                "monomer_code": next(
                    (row["monomer_code"] for row in cycle_events if row["monomer_code"]),
                    "?",
                ),
                "inner_steps": inner_steps,
                "inner_step_count": len(inner_steps),
                "event_count": len(cycle_events),
                "ami_event_count": sum(
                    len(step["events"])
                    for step in inner_steps
                    if step["reagent_code"] == "AMI"
                ),
                "operation_count": sum(len(event["pulses"]) for event in cycle_events),
                "started_at": cycle_events[0]["event_time"],
                "completed_at": cycle_events[-1]["event_time"],
            }
        )

    if not parsed_cycles:
        raise ValueError(
            f"{file_name} 不是可入库的执行日志：没有解析到 01| 及以后的 Inject。"
            "若这是说明/预期差异文件，请上传对应的仪器 log。"
        )

    aux = _split_aux(aux_events, events)
    return {
        "file_name": file_name,
        "encoding": encoding,
        "sha256": hashlib.sha256(raw).hexdigest(),
        "xxhash": xxhash.xxh3_64(raw).hexdigest(),
        "byte_size": len(raw),
        "run_code": run_code,
        "channel_no": channel_no,
        "recipe_file_name": recipe_file_name,
        "observed_sequence_3to5": "".join(
            cycle["monomer_code"] for cycle in parsed_cycles
        ),
        "started_at": parsed_cycles[0]["started_at"],
        "completed_at": completed_at,
        "status": "COMPLETED" if completed_at else "UNKNOWN",
        "cycles": parsed_cycles,
        "event_total": global_event_no,
        "operation_total": sum(cycle["operation_count"] for cycle in parsed_cycles),
        "inner_shape": [
            "-".join(
                f"{step['reagent_code']}x{len(step['events'])}"
                for step in cycle["inner_steps"]
            )
            for cycle in parsed_cycles
        ],
        "aux": aux,
        "issues": issues,
    }


def _split_aux(aux_events: list, synth_events: list) -> dict:
    init, final = [], []
    if synth_events:
        first = min(row["source_line_no"] for row in synth_events)
        last = max(row["source_line_no"] for row in synth_events)
    else:
        first = last = None
    for event in aux_events:
        if first is None or event["source_line_no"] < first:
            init.append(event)
        elif last is None or event["source_line_no"] > last:
            final.append(event)
        else:
            final.append(event)
    for bucket in (init, final):
        for no, event in enumerate(bucket, 1):
            event["event_no"] = no
    return {"INITIALIZATION": init, "FINALIZATION": final}


def _pulse_code(pulses: list) -> str:
    return "|".join(f"{drain}:{wait}" for drain, wait in pulses)


def _save_source_file(cur, raw: bytes, original_name: str, encoding: str | None) -> int:
    sha256 = hashlib.sha256(raw).hexdigest()
    fast_hash = xxhash.xxh3_64(raw).hexdigest()
    cur.execute(
        """
        INSERT INTO source_file (
            file_role, original_file_name, content_type, byte_size,
            sha256, fast_hash, detected_encoding, blob_status
        ) VALUES ('SYNTHESIS_LOG', %s, 'text/plain', %s, %s, %s, %s, 'PRESENT')
        ON CONFLICT (file_role, sha256) WHERE sha256 IS NOT NULL
        DO UPDATE SET
            fast_hash = EXCLUDED.fast_hash,
            byte_size = EXCLUDED.byte_size,
            detected_encoding = COALESCE(source_file.detected_encoding, EXCLUDED.detected_encoding),
            blob_status = 'PRESENT'
        RETURNING file_id
        """,
        (Path(original_name).name, len(raw), sha256, fast_hash, encoding),
    )
    file_id = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO source_file_blob (file_id, content)
        VALUES (%s, %s)
        ON CONFLICT (file_id) DO UPDATE SET content = EXCLUDED.content
        """,
        (file_id, raw),
    )
    return file_id


def _select_or_create_run(
    cur,
    parsed: dict,
    existing_run_id: int | None,
    new_run: bool,
) -> tuple[int, int]:
    if existing_run_id is not None:
        cur.execute(
            """
            SELECT run_id, attempt_no, run_code, channel_no
            FROM synthesis_run WHERE run_id = %s FOR UPDATE
            """,
            (existing_run_id,),
        )
        row = cur.fetchone()
        if not row:
            raise ValueError(f"指定的 run_id={existing_run_id} 不存在。")
        if row[2] != parsed["run_code"] or row[3] != parsed["channel_no"]:
            raise ValueError("指定 run_id 的板号/通道与上传文件名不一致。")
        return row[0], row[1]

    if not new_run:
        cur.execute(
            """
            SELECT run_id, attempt_no
            FROM synthesis_run
            WHERE run_code = %s AND channel_no = %s
            ORDER BY attempt_no DESC LIMIT 1 FOR UPDATE
            """,
            (parsed["run_code"], parsed["channel_no"]),
        )
        row = cur.fetchone()
        if row:
            return row[0], row[1]

    cur.execute(
        """
        SELECT COALESCE(MAX(attempt_no), 0) + 1
        FROM synthesis_run
        WHERE run_code = %s AND channel_no = %s
        """,
        (parsed["run_code"], parsed["channel_no"]),
    )
    attempt_no = cur.fetchone()[0]
    cur.execute(
        """
        INSERT INTO synthesis_run (run_code, channel_no, attempt_no)
        VALUES (%s, %s, %s)
        RETURNING run_id
        """,
        (parsed["run_code"], parsed["channel_no"], attempt_no),
    )
    return cur.fetchone()[0], attempt_no


def _find_run_for_failed_parse(cur, original_name: str) -> tuple[int | None, int | None]:
    try:
        run_code, channel_no = extract_log_identity(original_name)
    except ValueError:
        return None, None
    cur.execute(
        """
        SELECT run_id, attempt_no FROM synthesis_run
        WHERE run_code = %s AND channel_no = %s
        ORDER BY attempt_no DESC LIMIT 1
        """,
        (run_code, channel_no),
    )
    row = cur.fetchone()
    return row if row else (None, None)


def _next_version_no(cur, run_id: int | None) -> int | None:
    if run_id is None:
        return None
    cur.execute("SELECT run_id FROM synthesis_run WHERE run_id = %s FOR UPDATE", (run_id,))
    cur.execute(
        "SELECT COALESCE(MAX(version_no), 0) + 1 FROM run_log_version WHERE run_id = %s",
        (run_id,),
    )
    return cur.fetchone()[0]


def _insert_issue_rows(cur, version_id: int, issues: list[dict]) -> None:
    if not issues:
        return
    cur.executemany(
        """
        INSERT INTO log_parse_issue (
            version_id, line_no, severity, issue_code, message, raw_line_text
        ) VALUES (%s, %s, %s, %s, %s, %s)
        """,
        [
            (
                version_id,
                issue["line_no"],
                issue["severity"],
                issue["issue_code"],
                issue["message"],
                issue["raw_line_text"],
            )
            for issue in issues
        ],
    )


def _profile_id(cur, pulses: list, profile_cache: dict, created: set, reused: set) -> int:
    key = _pulse_code(pulses)
    if key in profile_cache:
        reused.add(profile_cache[key])
        return profile_cache[key]
    cur.execute("SELECT profile_id FROM drain_profile WHERE pulse_sequence = %s", (Jsonb(pulses),))
    row = cur.fetchone()
    cur.execute("SELECT synth_log.upsert_drain_profile(%s)", (Jsonb(pulses),))
    profile_id = cur.fetchone()[0]
    (reused if row else created).add(profile_id)
    profile_cache[key] = profile_id
    return profile_id


def _insert_event(cur, inner_step_id: int, event: dict, profile_cache, created, reused):
    profile_id = _profile_id(cur, event["pulses"], profile_cache, created, reused)
    cur.execute(
        """
        INSERT INTO synthesis_event (
            inner_step_id, drain_profile_id, event_no, inner_event_no,
            global_event_no, injection_volumes_ul, operation_count,
            temperature_c, humidity_percent, event_time, source_line_no,
            raw_line_text, pulse_code, pulse_sequence
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        RETURNING event_id
        """,
        (
            inner_step_id,
            profile_id,
            event["event_no"],
            event["inner_event_no"],
            event["global_event_no"],
            event["injection_volumes_ul"],
            len(event["pulses"]),
            event["temperature_c"],
            event["humidity_percent"],
            event["event_time"],
            event["source_line_no"],
            event["raw_line_text"],
            _pulse_code(event["pulses"]),
            Jsonb(event["pulses"]),
        ),
    )
    event_id = cur.fetchone()[0]
    if event["pulses"]:
        cur.executemany(
            """
            INSERT INTO synthesis_event_segment (event_id, segment_no, drain_value, wait_ms)
            VALUES (%s, %s, %s, %s)
            """,
            [
                (event_id, no, drain, wait)
                for no, (drain, wait) in enumerate(event["pulses"], 1)
            ],
        )


def _insert_parsed_data(cur, version_id: int, parsed: dict, created: set, reused: set):
    profile_cache = {}
    for cycle in parsed["cycles"]:
        cur.execute(
            """
            INSERT INTO synthesis_cycle (
                version_id, cycle_no, monomer_code, inner_step_count,
                event_count, ami_event_count, operation_count, started_at, completed_at
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            RETURNING cycle_id
            """,
            (
                version_id,
                cycle["cycle_no"],
                cycle["monomer_code"],
                cycle["inner_step_count"],
                cycle["event_count"],
                cycle["ami_event_count"],
                cycle["operation_count"],
                cycle["started_at"],
                cycle["completed_at"],
            ),
        )
        cycle_id = cur.fetchone()[0]
        for step in cycle["inner_steps"]:
            cur.execute(
                """
                INSERT INTO synthesis_inner_step (
                    cycle_id, step_order, reagent_code, step_occurrence,
                    event_count, operation_count, pulse_code, pulse_sequence
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
                RETURNING inner_step_id
                """,
                (
                    cycle_id,
                    step["step_order"],
                    step["reagent_code"],
                    step["step_occurrence"],
                    step["event_count"],
                    step["operation_count"],
                    step["pulse_code"],
                    Jsonb(step["pulse_sequence"]),
                ),
            )
            inner_step_id = cur.fetchone()[0]
            for event in step["events"]:
                _insert_event(
                    cur, inner_step_id, event, profile_cache, created, reused
                )

    for phase, rows in parsed["aux"].items():
        for event in rows:
            profile_id = _profile_id(
                cur, event["pulses"], profile_cache, created, reused
            )
            cur.execute(
                """
                INSERT INTO run_aux_event (
                    version_id, phase, event_no, reagent_code, injection_volumes_ul,
                    drain_profile_id, pulse_code, pulse_sequence, temperature_c,
                    humidity_percent, event_time, source_line_no, raw_line_text
                ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                """,
                (
                    version_id,
                    phase,
                    event["event_no"],
                    event["reagent_code"],
                    event["injection_volumes_ul"],
                    profile_id,
                    _pulse_code(event["pulses"]),
                    Jsonb(event["pulses"]),
                    event["temperature_c"],
                    event["humidity_percent"],
                    event["event_time"],
                    event["source_line_no"],
                    event["raw_line_text"],
                ),
            )
    _insert_issue_rows(cur, version_id, parsed["issues"])


def _result(parsed: dict, run_id: int, version_id: int, version_no: int, attempt_no: int,
            file_id: int, created: set, reused: set, **extra) -> dict:
    return {
        "file_name": parsed["file_name"],
        "file_id": file_id,
        "run_id": run_id,
        "version_id": version_id,
        "version_no": version_no,
        "attempt_no": attempt_no,
        "run_code": parsed["run_code"],
        "channel_no": parsed["channel_no"],
        "status": parsed["status"],
        "completed_at": parsed["completed_at"],
        "sequence": parsed["observed_sequence_3to5"],
        "event_total": parsed["event_total"],
        "operation_total": parsed["operation_total"],
        "cycle_count": len(parsed["cycles"]),
        "inner_shape_sample": parsed["inner_shape"][0] if parsed["inner_shape"] else "",
        "init_count": len(parsed["aux"]["INITIALIZATION"]),
        "final_count": len(parsed["aux"]["FINALIZATION"]),
        "issue_count": len(parsed["issues"]),
        "new_profiles": len(created),
        "reused_profiles": len(reused),
        "sha256": parsed["sha256"],
        **extra,
    }


def ingest_raw_log(
    conn,
    raw: bytes,
    original_name: str,
    *,
    existing_run_id: int | None = None,
    new_run: bool = False,
) -> dict:
    """Persist raw bytes and one parse version, committing one file atomically."""
    if not raw:
        return {"file_name": Path(original_name).name, "error": "上传文件为空。"}

    try:
        parsed = parse_log(raw, original_name)
        parse_error = None
        encoding = parsed["encoding"]
    except Exception as exc:
        parsed = None
        parse_error = str(exc)
        try:
            _, encoding = decode_log(raw)
        except ValueError:
            encoding = None

    with conn.transaction():
        with conn.cursor() as cur:
            cur.execute("SET LOCAL search_path TO synth_log, public")
            file_id = _save_source_file(cur, raw, original_name, encoding)

            if parsed is None:
                run_id, attempt_no = _find_run_for_failed_parse(cur, original_name)
                cur.execute(
                    """
                    SELECT version_id, version_no
                    FROM run_log_version
                    WHERE source_file_id = %s AND parser_version = %s
                      AND ingest_status = 'FAILED'
                      AND run_id IS NOT DISTINCT FROM %s
                    ORDER BY version_id DESC LIMIT 1
                    """,
                    (file_id, PARSER_VERSION, run_id),
                )
                duplicate = cur.fetchone()
                if duplicate:
                    return {
                        "file_name": Path(original_name).name,
                        "file_id": file_id,
                        "run_id": run_id,
                        "version_id": duplicate[0],
                        "version_no": duplicate[1],
                        "skipped": True,
                        "error": parse_error,
                    }
                version_no = _next_version_no(cur, run_id)
                cur.execute(
                    """
                    INSERT INTO run_log_version (
                        run_id, version_no, source_file_id, uploaded_file_name,
                        parser_version, ingest_status, error_summary, issue_count
                    ) VALUES (%s, %s, %s, %s, %s, 'FAILED', %s, 1)
                    RETURNING version_id
                    """,
                    (
                        run_id,
                        version_no,
                        file_id,
                        Path(original_name).name,
                        PARSER_VERSION,
                        parse_error,
                    ),
                )
                version_id = cur.fetchone()[0]
                _insert_issue_rows(
                    cur,
                    version_id,
                    [
                        _issue(
                            None,
                            "ERROR",
                            "PARSE_FAILED",
                            parse_error,
                            None,
                        )
                    ],
                )
                return {
                    "file_name": Path(original_name).name,
                    "file_id": file_id,
                    "run_id": run_id,
                    "version_id": version_id,
                    "version_no": version_no,
                    "attempt_no": attempt_no,
                    "error": parse_error,
                    "saved_failed_version": True,
                }

            run_id, attempt_no = _select_or_create_run(
                cur, parsed, existing_run_id, new_run
            )
            cur.execute(
                """
                SELECT version_id, version_no, ingest_status::text
                FROM run_log_version
                WHERE run_id = %s AND source_file_id = %s AND parser_version = %s
                """,
                (run_id, file_id, PARSER_VERSION),
            )
            duplicate = cur.fetchone()
            if duplicate:
                return _result(
                    parsed,
                    run_id,
                    duplicate[0],
                    duplicate[1],
                    attempt_no,
                    file_id,
                    set(),
                    set(),
                    skipped=True,
                    note=f"相同文件已由 {PARSER_VERSION} 解析，未重复入库。",
                )

            version_no = _next_version_no(cur, run_id)
            cur.execute(
                """
                INSERT INTO run_log_version (
                    run_id, version_no, source_file_id, uploaded_file_name,
                    parser_version, ingest_status, recipe_file_name,
                    observed_sequence_3to5, started_at, completed_at, run_status
                ) VALUES (%s, %s, %s, %s, %s, 'PENDING', %s, %s, %s, %s, %s)
                RETURNING version_id
                """,
                (
                    run_id,
                    version_no,
                    file_id,
                    parsed["file_name"],
                    PARSER_VERSION,
                    parsed["recipe_file_name"],
                    parsed["observed_sequence_3to5"],
                    parsed["started_at"],
                    parsed["completed_at"],
                    parsed["status"],
                ),
            )
            version_id = cur.fetchone()[0]
            created, reused = set(), set()

            try:
                with conn.transaction():
                    _insert_parsed_data(cur, version_id, parsed, created, reused)
            except Exception as exc:
                error_summary = f"结构化数据写入失败：{exc}"
                cur.execute(
                    """
                    UPDATE run_log_version
                    SET ingest_status = 'FAILED', error_summary = %s, issue_count = 1
                    WHERE version_id = %s
                    """,
                    (error_summary, version_id),
                )
                _insert_issue_rows(
                    cur,
                    version_id,
                    [_issue(None, "ERROR", "DATABASE_WRITE_FAILED", error_summary, None)],
                )
                return {
                    "file_name": parsed["file_name"],
                    "file_id": file_id,
                    "run_id": run_id,
                    "version_id": version_id,
                    "version_no": version_no,
                    "attempt_no": attempt_no,
                    "error": error_summary,
                    "saved_failed_version": True,
                }

            aux_count = sum(len(rows) for rows in parsed["aux"].values())
            cur.execute(
                """
                UPDATE run_log_version
                SET ingest_status = 'SUCCEEDED',
                    cycle_count = %s,
                    event_count = %s,
                    operation_count = %s,
                    aux_event_count = %s,
                    issue_count = %s
                WHERE version_id = %s
                """,
                (
                    len(parsed["cycles"]),
                    parsed["event_total"],
                    parsed["operation_total"],
                    aux_count,
                    len(parsed["issues"]),
                    version_id,
                ),
            )
            cur.execute(
                """
                UPDATE synthesis_run
                SET current_version_id = %s,
                    started_at = %s,
                    completed_at = %s,
                    status = %s,
                    updated_at = CURRENT_TIMESTAMP
                WHERE run_id = %s
                """,
                (
                    version_id,
                    parsed["started_at"],
                    parsed["completed_at"],
                    parsed["status"],
                    run_id,
                ),
            )
            return _result(
                parsed,
                run_id,
                version_id,
                version_no,
                attempt_no,
                file_id,
                created,
                reused,
            )
