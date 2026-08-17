from __future__ import annotations

import json
from datetime import date, datetime
from decimal import Decimal
from io import BytesIO
from pathlib import Path

import psycopg
from flask import (
    Flask,
    Response,
    abort,
    flash,
    jsonify,
    render_template,
    request,
    send_file,
)
from psycopg.rows import dict_row

from ingest import ingest_raw_log
from pg_auth import resolve_password


CONFIG_PATH = Path(__file__).resolve().parent / "db_config.json"

app = Flask(__name__)
app.secret_key = "gene-synthesis-log-upload-v2"

HISTORY_SQL = """
SELECT
    r.run_id,
    r.run_code,
    r.channel_no,
    r.attempt_no,
    r.current_version_id = v.version_id AS is_current,
    v.version_id,
    v.version_no,
    v.ingest_status::text AS ingest_status,
    v.run_status::text AS status,
    v.completed_at,
    v.imported_at,
    v.observed_sequence_3to5 AS sequence_3to5,
    v.cycle_count,
    v.event_count AS event_total,
    v.operation_count,
    v.aux_event_count,
    v.issue_count,
    v.error_summary,
    f.file_id,
    f.original_file_name,
    f.sha256,
    f.fast_hash,
    f.blob_status::text AS blob_status,
    (
        SELECT shape.inner_shape
        FROM v_cycle_inner_shape shape
        WHERE shape.version_id = v.version_id AND shape.cycle_no = 1
    ) AS inner_shape
FROM synthesis_run r
JOIN run_log_version v ON v.run_id = r.run_id
JOIN source_file f ON f.file_id = v.source_file_id
ORDER BY v.imported_at DESC, v.version_id DESC
LIMIT 200
"""


def load_config() -> dict:
    config = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    config.pop("password", None)
    return config


def save_config(config: dict) -> None:
    CONFIG_PATH.write_text(
        json.dumps(
            {
                "host": config["host"],
                "port": config["port"],
                "dbname": config["dbname"],
                "user": config["user"],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )


def open_connection(*, autocommit: bool = False, row_factory=None):
    password = resolve_password()
    if not password:
        raise RuntimeError(
            "未找到数据库密码。请设置 SYNTH_LOG_PGPASSWORD，或使用本机 pgAdmin 已保存的凭据。"
        )
    kwargs = load_config()
    if row_factory is not None:
        kwargs["row_factory"] = row_factory
    return psycopg.connect(**kwargs, password=password, autocommit=autocommit)


def fetch_history() -> list:
    try:
        with open_connection(autocommit=True, row_factory=dict_row) as conn:
            conn.execute("SET search_path TO synth_log, public")
            return list(conn.execute(HISTORY_SQL))
    except Exception:
        return []


def json_value(value):
    if isinstance(value, dict):
        return {key: json_value(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [json_value(item) for item in value]
    if isinstance(value, Decimal):
        return str(value)
    if isinstance(value, (datetime, date)):
        return value.isoformat()
    return value


def _bool_form(name: str) -> bool:
    return str(request.form.get(name, "")).lower() in {"1", "true", "yes", "on"}


@app.route("/", methods=["GET", "POST"])
def index():
    config = load_config()
    results = []
    if request.method == "POST":
        config = {
            "host": request.form["host"],
            "port": int(request.form["port"]),
            "dbname": request.form["dbname"],
            "user": request.form["user"],
        }
        save_config(config)
        try:
            run_id_text = request.form.get("run_id", "").strip()
            existing_run_id = int(run_id_text) if run_id_text else None
            new_run = _bool_form("new_run")
            with open_connection(autocommit=False) as conn:
                for item in request.files.getlist("files"):
                    if not item.filename.lower().endswith(".txt"):
                        results.append(
                            {"file_name": item.filename, "error": "本期只接受 .txt 合成日志。"}
                        )
                        continue
                    results.append(
                        ingest_raw_log(
                            conn,
                            item.read(),
                            item.filename,
                            existing_run_id=existing_run_id,
                            new_run=new_run,
                        )
                    )
            success_count = sum(1 for row in results if not row.get("error"))
            flash(f"处理 {len(results)} 个文件，成功 {success_count} 个。", "message")
        except (ValueError, RuntimeError, psycopg.Error) as exc:
            flash(f"上传失败：{exc}", "error")
    return render_template(
        "index.html",
        config=config,
        results=results,
        history=fetch_history(),
    )


@app.post("/api/logs")
def api_upload_log():
    item = request.files.get("file")
    if item is None or not item.filename:
        return jsonify({"error": "multipart/form-data 中缺少 file。"}), 400
    if not item.filename.lower().endswith(".txt"):
        return jsonify({"error": "本期只接受 .txt 合成日志。"}), 415
    try:
        run_id_text = request.form.get("run_id", "").strip()
        existing_run_id = int(run_id_text) if run_id_text else None
        with open_connection(autocommit=False) as conn:
            result = ingest_raw_log(
                conn,
                item.read(),
                item.filename,
                existing_run_id=existing_run_id,
                new_run=_bool_form("new_run"),
            )
        status_code = 422 if result.get("error") else (200 if result.get("skipped") else 201)
        return jsonify(json_value(result)), status_code
    except (ValueError, RuntimeError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 400


@app.get("/api/runs")
def api_runs():
    try:
        limit = min(max(int(request.args.get("limit", 50)), 1), 100)
        cursor = request.args.get("cursor")
        cursor_id = int(cursor) if cursor else None
    except ValueError:
        return jsonify({"error": "cursor 和 limit 必须是整数。"}), 400
    sql = """
        SELECT * FROM synth_log.v_current_run
        WHERE (%s::bigint IS NULL OR run_id < %s)
        ORDER BY run_id DESC
        LIMIT %s
    """
    try:
        with open_connection(autocommit=True, row_factory=dict_row) as conn:
            rows = list(conn.execute(sql, (cursor_id, cursor_id, limit + 1)))
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = rows[-1]["run_id"] if has_more and rows else None
        return jsonify(
            {"items": json_value(rows), "next_cursor": next_cursor}
        )
    except (RuntimeError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 503


@app.get("/api/runs/<int:run_id>/versions")
def api_run_versions(run_id: int):
    sql = """
        SELECT v.*, f.original_file_name, f.sha256, f.byte_size,
               f.blob_status::text AS blob_status,
               r.current_version_id = v.version_id AS is_current
        FROM synth_log.run_log_version v
        JOIN synth_log.synthesis_run r ON r.run_id = v.run_id
        JOIN synth_log.source_file f ON f.file_id = v.source_file_id
        WHERE v.run_id = %s
        ORDER BY v.version_no DESC, v.version_id DESC
    """
    try:
        with open_connection(autocommit=True, row_factory=dict_row) as conn:
            rows = list(conn.execute(sql, (run_id,)))
        if not rows:
            abort(404)
        return jsonify({"items": json_value(rows)})
    except psycopg.Error as exc:
        return jsonify({"error": str(exc)}), 503


@app.get("/api/log-versions/<int:version_id>")
def api_log_version(version_id: int):
    try:
        with open_connection(autocommit=True, row_factory=dict_row) as conn:
            version = conn.execute(
                """
                SELECT v.*, r.run_code, r.channel_no, r.attempt_no,
                       r.current_version_id = v.version_id AS is_current,
                       f.file_id, f.original_file_name, f.sha256,
                       f.blob_status::text AS blob_status
                FROM synth_log.run_log_version v
                LEFT JOIN synth_log.synthesis_run r ON r.run_id = v.run_id
                JOIN synth_log.source_file f ON f.file_id = v.source_file_id
                WHERE v.version_id = %s
                """,
                (version_id,),
            ).fetchone()
            if version is None:
                abort(404)
            cycles = list(
                conn.execute(
                    """
                    SELECT cycle_id, cycle_no, monomer_code, inner_step_count,
                           event_count, ami_event_count, operation_count,
                           started_at, completed_at
                    FROM synth_log.synthesis_cycle
                    WHERE version_id = %s ORDER BY cycle_no
                    """,
                    (version_id,),
                )
            )
            steps = list(
                conn.execute(
                    """
                    SELECT s.inner_step_id, s.cycle_id, s.step_order,
                           s.reagent_code, s.step_occurrence,
                           s.event_count, s.operation_count
                    FROM synth_log.synthesis_inner_step s
                    JOIN synth_log.synthesis_cycle c ON c.cycle_id = s.cycle_id
                    WHERE c.version_id = %s
                    ORDER BY c.cycle_no, s.step_order
                    """,
                    (version_id,),
                )
            )
            events = list(
                conn.execute(
                    """
                    SELECT e.event_id, e.inner_step_id, e.event_no,
                           e.inner_event_no, e.global_event_no,
                           e.injection_volumes_ul, e.operation_count,
                           e.drain_profile_id, e.temperature_c,
                           e.humidity_percent, e.event_time,
                           e.source_line_no, e.pulse_sequence
                    FROM synth_log.synthesis_event e
                    JOIN synth_log.synthesis_inner_step s ON s.inner_step_id = e.inner_step_id
                    JOIN synth_log.synthesis_cycle c ON c.cycle_id = s.cycle_id
                    WHERE c.version_id = %s
                    ORDER BY e.global_event_no
                    """,
                    (version_id,),
                )
            )
            issues = list(
                conn.execute(
                    """
                    SELECT line_no, severity::text AS severity, issue_code,
                           message, raw_line_text
                    FROM synth_log.log_parse_issue
                    WHERE version_id = %s ORDER BY line_no NULLS FIRST, issue_id
                    """,
                    (version_id,),
                )
            )
        return jsonify(
            json_value(
                {
                    "version": version,
                    "cycles": cycles,
                    "inner_steps": steps,
                    "events": events,
                    "issues": issues,
                }
            )
        )
    except psycopg.Error as exc:
        return jsonify({"error": str(exc)}), 503


def _source_file(file_id: int):
    with open_connection(autocommit=True, row_factory=dict_row) as conn:
        return conn.execute(
            """
            SELECT f.*, b.content
            FROM synth_log.source_file f
            LEFT JOIN synth_log.source_file_blob b ON b.file_id = f.file_id
            WHERE f.file_id = %s
            """,
            (file_id,),
        ).fetchone()


@app.get("/api/source-files/<int:file_id>/content")
def source_file_content(file_id: int):
    try:
        row = _source_file(file_id)
    except (RuntimeError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 503
    if row is None:
        abort(404)
    if row["content"] is None:
        return jsonify({"error": "旧记录的原始文件尚未补回。"}), 404
    encoding = row["detected_encoding"] or "utf-8-sig"
    try:
        text = bytes(row["content"]).decode(encoding)
    except (LookupError, UnicodeDecodeError):
        text = bytes(row["content"]).decode("utf-8", errors="replace")
    return Response(text, content_type="text/plain; charset=utf-8")


@app.get("/api/source-files/<int:file_id>/download")
def source_file_download(file_id: int):
    try:
        row = _source_file(file_id)
    except (RuntimeError, psycopg.Error) as exc:
        return jsonify({"error": str(exc)}), 503
    if row is None:
        abort(404)
    if row["content"] is None:
        return jsonify({"error": "旧记录的原始文件尚未补回。"}), 404
    return send_file(
        BytesIO(bytes(row["content"])),
        mimetype=row["content_type"],
        as_attachment=True,
        download_name=row["original_file_name"],
        max_age=0,
    )


if __name__ == "__main__":
    print("http://127.0.0.1:5050")
    app.run(host="127.0.0.1", port=5050)
