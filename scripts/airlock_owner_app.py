#!/usr/bin/env python3
"""Build a Finder-launchable owner consent app. No credentials enter this bundle."""
from __future__ import annotations

import argparse
import plistlib
from pathlib import Path

LAUNCHER = r'''#!/usr/bin/python3
import json, os, pathlib, stat, subprocess

def alert(message):
    subprocess.run(['/usr/bin/osascript', '-e', 'on run argv', '-e',
        'display alert "LOCKI Owner Consent" message (item 1 of argv)',
        '-e', 'end run', message], check=False, stdout=subprocess.DEVNULL)

try:
    chosen = subprocess.run(['/usr/bin/osascript', '-e',
        'POSIX path of (choose file with prompt "Choose the private LOCKI owner consent configuration")'],
        check=True, capture_output=True, text=True).stdout.strip()
    path = pathlib.Path(chosen)
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd) as f:
        info = os.fstat(f.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_size > 16384:
            raise ValueError('Configuration must be a regular file owned by you, accessible only to you.')
        config = json.load(f)
    repository = pathlib.Path(config['repositoryPath'])
    node = pathlib.Path(config['nodePath'])
    runner = repository / 'scripts/airlock_owner_consent.ts'
    tsx = repository / 'frontend/node_modules/tsx/dist/cli.mjs'
    if not repository.is_absolute() or not node.is_absolute() or not runner.is_file() or not tsx.is_file() or not os.access(node, os.X_OK):
        raise ValueError('Configured repository, Node runtime or installed dependencies are unavailable.')
    os.chdir(repository)
    # Fixed argv, one consent broker, no shell or terminal. Surface failure in Finder.
    result = subprocess.run([str(node), str(tsx), str(runner), '--config', str(path)],
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    if result.returncode:
        alert('Owner authentication or consent connection failed. Check the private configuration and existing CLI login. Pending permission is bounded by its expiry and owner connection.')
except subprocess.CalledProcessError:
    pass  # The owner cancelled the file picker.
except Exception:
    # Configuration/SDK errors may contain sensitive paths; never echo contents.
    alert('The owner consent app could not start. Check the private configuration and installed dependencies. No grant was issued by this launcher.')
'''


def build(destination: Path) -> Path:
    app = destination / "LOCKI Owner Consent.app"
    executable = app / "Contents/MacOS/LOCKI Owner Consent"
    executable.parent.mkdir(parents=True, exist_ok=True)
    executable.write_text(LAUNCHER)
    executable.chmod(0o755)
    with (app / "Contents/Info.plist").open("wb") as f:
        plistlib.dump({
            "CFBundleName": "LOCKI Owner Consent",
            "CFBundleDisplayName": "LOCKI Owner Consent",
            "CFBundleIdentifier": "local.locki.owner-consent",
            "CFBundleVersion": "1",
            "CFBundlePackageType": "APPL",
            "CFBundleExecutable": executable.name,
            "LSUIElement": True,
        }, f)
    return app


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=Path("output/airlock-owner"))
    args = parser.parse_args()
    print(build(args.output))
