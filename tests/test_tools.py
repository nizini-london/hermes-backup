"""Tool envelope contracts (QA N1): the never-raise _guarded last resort
must scrub its error text — a crash message can carry credential material."""

import json

import engine
import tools


def test_guarded_error_envelope_is_scrubbed():
    """QA N1: the last-resort envelope embedded str(exc) verbatim — an
    unexpected crash whose message carries credential material leaked it into
    the tool JSON (the only unscrubbed text path in the plugin). It now goes
    through engine.scrub(). The guard has no Secrets context, so bare values
    stay the engine handlers' own scrub responsibility; the env-var NAME=
    pattern is what this path guarantees."""
    def exploding(args, cfg):
        raise ValueError(
            "boom HERMES_BACKUP_RESTIC_PASSWORD=hunter2hunter2 (from leg local)")

    out = tools._guarded("backup_now", exploding, {})
    parsed = json.loads(out)
    assert parsed["ok"] is False
    assert "hunter2hunter2" not in parsed["error"], parsed
    assert "HERMES_BACKUP_RESTIC_PASSWORD=<redacted>" in parsed["error"], parsed


def test_guarded_success_path_untouched():
    """The guard stays transparent when the handler succeeds."""
    out = tools._guarded("backup_status", lambda args, cfg: '{"ok": true}', {})
    assert json.loads(out) == {"ok": True}
