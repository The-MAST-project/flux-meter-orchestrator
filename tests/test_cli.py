"""The command line, and how the process ends when the camera will not open."""

from __future__ import annotations

import socket
import subprocess
import sys

import pytest
from conftest import LOCALHOST, STARTUP_TIMEOUT_S, free_port, service_command

from flux_meter_service.__main__ import parse_args
from flux_meter_service.save import DEFAULT_SAVE_ROOT

ARGPARSE_USAGE_ERROR = 2


def test_the_port_is_required(capsys):
    with pytest.raises(SystemExit) as exit_info:
        parse_args([])

    assert exit_info.value.code == ARGPARSE_USAGE_ERROR
    assert "--port" in capsys.readouterr().err


def test_the_defaults():
    args = parse_args(["--port", "8123"])

    assert args.port == 8123
    assert args.host == "0.0.0.0"
    assert args.fake is False
    assert args.dll_dir is None
    assert args.save_root == DEFAULT_SAVE_ROOT


@pytest.mark.skipif(sys.platform == "win32", reason="the refusal is for platforms without add_dll_directory")
def test_a_dll_dir_is_refused_off_windows(capsys):
    with pytest.raises(SystemExit) as exit_info:
        parse_args(["--port", "8123", "--dll-dir", "C:/Thorlabs"])

    assert exit_info.value.code == ARGPARSE_USAGE_ERROR
    assert "Windows" in capsys.readouterr().err


@pytest.mark.skipif(sys.platform == "win32", reason="relies on the Thorlabs SDK being absent")
def test_the_process_exits_when_the_real_camera_will_not_open():
    """Without the SDK the camera cannot open, and the process must end rather than serve."""
    port = free_port()
    result = subprocess.run(service_command("--port", str(port)), capture_output=True, text=True, timeout=STARTUP_TIMEOUT_S)

    assert result.returncode != 0
    assert "Thorlabs SDK could not be imported" in result.stderr
    with socket.socket() as sock:
        assert sock.connect_ex((LOCALHOST, port)) != 0
