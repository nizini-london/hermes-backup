"""Log discipline: verdict regex over a LARGE tail, progress lines never
parse as verdicts, DRILL lines, bounded rotation."""

import engine


def _write_log(lines):
    path = engine.log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("\n".join(lines) + "\n")
    return path


PROGRESS = [f"[0:{i // 60:02d}:{i % 60:02d}] {i * 2}.00%  {i}0/100 files 1234 B{i // 7}  5.00 MiB/s"
            for i in range(500)]


def test_verdict_regex_survives_500_progress_lines(data_root):
    """The widget bug: verdict-to-EOF distance grows unbounded — a small
    fixed tail intermittently misses the verdict. 64KB does not."""
    lines = PROGRESS + ["[2026-11-05 09:59:00] OK (local=1 r2=1) snapshot=local=abcd1234"]
    _write_log(lines)
    verdict = engine.last_run_verdict()
    assert verdict is not None
    assert verdict["verdict"] == "OK"
    assert verdict["epoch"] is not None and isinstance(verdict["epoch"], int)
    assert "local=1" in verdict["detail"]


def test_progress_lines_never_parse_as_verdicts(data_root):
    """restic progress like '[0:04] 100.00%' brackets non-dates — rejected."""
    _write_log(PROGRESS)
    assert engine.last_run_verdict() is None


def test_progress_then_fail_then_ok(data_root):
    """Multiple verdicts: the LAST one wins (matches reference semantics)."""
    lines = PROGRESS + [
        "[2026-11-05 08:00:00] FAIL (local=1 r2=0) local=boom",
        "[0:02] 55.00% more progress",
        "[2026-11-05 08:05:00] OK (local=1 r2=1)",
    ]
    _write_log(lines)
    verdict = engine.last_run_verdict()
    assert verdict["verdict"] == "OK"
    assert verdict["time"] == "2026-11-05 08:05:00"


def test_log_tail_window_is_64kb(data_root):
    filler = ["x" * 200] * 5000  # ~1MB of noise
    lines = filler + ["[2026-11-05 10:00:00] OK (local=1 r2=1)"]
    _write_log(lines)
    assert len(engine.log_tail().encode()) <= engine.LOG_TAIL_BYTES + 256
    assert engine.last_run_verdict()["verdict"] == "OK"


def test_drill_line_parse(data_root):
    _write_log([
        "[2026-11-05 10:00:00] OK (local=1 r2=1)",
        "DRILL 2026-11-05 PASS driver=agent",
    ])
    drill = engine.last_drill_verdict()
    assert drill == {"date": "2026-11-05", "epoch": engine._stamp_epoch("2026-11-05 00:00:00"),
                     "verdict": "PASS", "driver": "agent"}


def test_drill_line_latest_wins_and_invalid_ignored(data_root):
    _write_log([
        "DRILL 2026-10-01 FAIL driver=shaun",
        "DRILL not-a-date",
        "DRILL 2026-11-05 PASS driver=operator",
    ])
    drill = engine.last_drill_verdict()
    assert drill["date"] == "2026-11-05" and drill["driver"] == "operator"
    assert drill["verdict"] == "PASS"


def test_append_drill_line_normalizes(data_root):
    engine.append_drill_line("weird-verdict", "shaun")
    drill = engine.last_drill_verdict()
    assert drill["verdict"] == "FAIL"  # anything not PASS/FAIL is FAIL
    assert drill["driver"] == "shaun"


def test_log_rotation(data_root, monkeypatch):
    monkeypatch.setattr(engine, "LOG_MAX_BYTES", 2048)
    monkeypatch.setattr(engine, "LOG_KEEP_BYTES", 512)
    path = engine.log_path()
    for i in range(40):
        engine.log_append("FILLER " + "y" * 200)
    assert path.stat().st_size < 4096, "log must rotate, not grow unbounded"
    # and the log still parses after rotation
    engine.log_append("OK (local=1 r2=1)")
    assert engine.last_run_verdict()["verdict"] == "OK"
