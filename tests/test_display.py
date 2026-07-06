from __future__ import annotations

from trap.display.submit import render_submit_result


def test_render_submit_passed(capsys):
    render_submit_result(
        {"run": {"passed": True, "id": "r1", "total_score": 0.9}, "view_url": "http://x/run/r1"}
    )
    out = capsys.readouterr().out
    assert "passed" in out and "r1" in out


def test_render_submit_failed_no_url(capsys):
    render_submit_result({})
    assert "failed" in capsys.readouterr().out
