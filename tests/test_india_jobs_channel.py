# -*- coding: utf-8 -*-
"""Dedicated tests for the ``india_jobs`` channel.

The channel is glue only: check() reports whether python-jobspy is importable
(without importing it) and otherwise falls back to Jina Reader for the
Wellfound/YC city pages. The career reference carries the slug gotchas that
were verified live, so the docs are pinned here too.
"""

import subprocess
import sys
from pathlib import Path

from agent_reach import cli
from agent_reach.channels import get_channel
from agent_reach.channels import india_jobs as ij
from agent_reach.channels.india_jobs import IndiaJobsChannel

_CAREER = Path(ij.__file__).resolve().parent.parent / "skill" / "references" / "career.md"


def test_registered():
    assert isinstance(get_channel("india_jobs"), IndiaJobsChannel)


def test_can_handle_naukri_and_wellfound_only():
    ch = IndiaJobsChannel()
    for url in [
        "https://www.naukri.com/job-listings-backend-123",
        "https://wellfound.com/startups/location/noida",
    ]:
        assert ch.can_handle(url) is True, url
    for url in ["https://naukri.com.evil.test/x", "https://linkedin.com/jobs", ""]:
        assert ch.can_handle(url) is False, url


def test_check_ok_when_jobspy_installed(monkeypatch):
    monkeypatch.setattr(ij, "_jobspy_installed", lambda: True)
    ch = IndiaJobsChannel()
    status, message = ch.check()
    assert status == "ok"
    assert ch.active_backend == "python-jobspy"
    assert sys.executable in message


def test_check_warns_with_jina_fallback_when_missing(monkeypatch):
    monkeypatch.setattr(ij, "_jobspy_installed", lambda: False)
    ch = IndiaJobsChannel()
    status, message = ch.check()
    assert status == "warn"
    assert ch.active_backend == "Jina Reader"
    assert "agent-reach install --channels=india_jobs" in message


def test_jobspy_probe_does_not_crash_on_bad_spec(monkeypatch):
    def _boom(_name):
        raise ValueError("bad spec")

    monkeypatch.setattr(ij.importlib.util, "find_spec", _boom)
    assert ij._jobspy_installed() is False


def test_installer_pip_installs_into_own_interpreter(monkeypatch, capsys):
    calls = []
    monkeypatch.setattr(ij, "_jobspy_installed", lambda: False)

    def _fake_run(cmd, **kwargs):
        calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, "", "")

    monkeypatch.setattr(subprocess, "run", _fake_run)
    assert cli._install_india_jobs_deps() is True
    assert calls == [[sys.executable, "-m", "pip", "install", "python-jobspy"]]
    assert "python-jobspy installed" in capsys.readouterr().out


def test_installer_reports_failure(monkeypatch, capsys):
    monkeypatch.setattr(ij, "_jobspy_installed", lambda: False)
    monkeypatch.setattr(
        subprocess, "run", lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "err")
    )
    assert cli._install_india_jobs_deps() is False
    assert "pip install python-jobspy" in capsys.readouterr().out


def test_career_reference_pins_verified_city_slugs():
    career = _CAREER.read_text(encoding="utf-8")
    # Wellfound's Chennai page is "madras"; "chennai" silently falls back to global.
    assert "wellfound.com/startups/location/madras" in career
    assert "wellfound.com/startups/location/noida" in career
    # Wellfound's Gurugram page is "gurgaon"; "gurugram" renders but has 0 results.
    assert "wellfound.com/startups/location/gurgaon" in career
    assert "wellfound.com/startups/location/gurugram\"" not in career
    assert 'site_name=["naukri", "linkedin"]' in career
