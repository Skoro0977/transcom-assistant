import os
import socket
import subprocess

import pytest

from apps.api.paths import ROOT, windows_storage_allowed


def test_portable_windows_requires_explicit_opt_in_and_stays_on_project_drive():
    assert not windows_storage_allowed('C:/Projects/app', 'C:/Projects/app')
    assert windows_storage_allowed('D:/CodexData/app/data', 'D:/CodexData/app')
    assert windows_storage_allowed('C:/Projects/app/data', 'C:/Projects/app', portable=True)
    assert not windows_storage_allowed('D:/data', 'C:/Projects/app', portable=True)
    assert not windows_storage_allowed('C:data', 'C:/Projects/app', portable=True)
    assert not windows_storage_allowed('//server/share/data', 'C:/Projects/app', portable=True)
    assert not windows_storage_allowed('//server/share/data', '//server/share/app', portable=True)


@pytest.mark.skipif(os.name != 'nt', reason='Windows startup check')
def test_startup_check_rejects_a_port_that_is_already_owned():
    with socket.socket() as listener:
        listener.bind(('127.0.0.1', 0))
        listener.listen()
        result = subprocess.run(['powershell.exe', '-NoProfile', '-ExecutionPolicy', 'Bypass',
            '-File', str(ROOT / 'scripts/check-windows.ps1'), '-Port', str(listener.getsockname()[1])],
            check=False, capture_output=True, text=True, timeout=30)
    assert result.returncode != 0 and 'already in use' in result.stderr
