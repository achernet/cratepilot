from __future__ import annotations

import subprocess

from cratepilot.doctor import dependency_status, doctor_report


def test_dependency_status_reports_path_and_version(monkeypatch):
    monkeypatch.setattr("cratepilot.doctor.shutil.which", lambda command: f"/tools/{command}")
    monkeypatch.setattr(
        "cratepilot.doctor.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "tool 1.2.3\nmore", ""),
    )
    result = dependency_status((("songrec", ("--version",), "recognition"),))
    assert result[0].available
    assert result[0].path == "/tools/songrec"
    assert result[0].version == "tool 1.2.3"


def test_doctor_requires_complete_toolchain(monkeypatch):
    monkeypatch.setattr("cratepilot.doctor.shutil.which", lambda command: None if command == "songrec" else command)
    monkeypatch.setattr(
        "cratepilot.doctor.subprocess.run",
        lambda command, **kwargs: subprocess.CompletedProcess(command, 0, "1.0", ""),
    )
    report = doctor_report()
    assert report["ok"] is False
    assert next(item for item in report["dependencies"] if item["command"] == "songrec")["available"] is False
