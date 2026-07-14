from __future__ import annotations

from trap.display.submit import render_submit_result


def test_render_submit_success(capsys):
    render_submit_result({"run": {"id": "r1"}, "view_url": "http://x/runs/r1"})
    out = capsys.readouterr().out
    assert "submitted" in out and "r1" in out and "http://x/runs/r1" in out


def test_render_submit_lean_response(capsys):
    # missing keys still render — success was already decided by the HTTP status
    render_submit_result({})
    out = capsys.readouterr().out
    assert "submitted" in out and "?" in out
