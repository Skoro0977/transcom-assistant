"""Local drive policy; portable Windows mode is an explicit process setting."""
import os
import re
from pathlib import Path, PureWindowsPath

ROOT = Path(__file__).resolve().parents[2]
PORTABLE_MODE = os.environ.get('TRANSCOM_PORTABLE', '').strip().lower() == 'true'
STORAGE_DRIVE = ROOT.drive.upper() if PORTABLE_MODE else 'D:'


def windows_storage_allowed(path, root, portable=False):
    target, project = PureWindowsPath(path), PureWindowsPath(root)
    drive = project.drive.upper() if portable else 'D:'
    return bool(re.fullmatch(r'[A-Z]:', drive) and target.is_absolute() and target.drive.upper() == drive)


def is_storage_path(path):
    return os.name != 'nt' or windows_storage_allowed(path, ROOT, PORTABLE_MODE)


if not is_storage_path(ROOT):
    raise RuntimeError('Project must stay on D; on another Windows drive run scripts with -Portable')
