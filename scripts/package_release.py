"""Create public release archives from an explicit source-file allowlist."""
import argparse
import hashlib
import importlib.metadata
import json
import platform
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from core import VERSION

TOP_FILES = (
    'main.py', 'core.py', 'collector.py', 'native_ssh.py', 'notifications.py',
    'tailscale_auth.py', 'test_monitor.py', 'test_tailscale.py', 'test_live_features.py',
    'app.ico', 'README.md', 'LICENSE', 'CHANGELOG.md', 'CONTRIBUTING.md', 'SECURITY.md',
    'THIRD_PARTY_NOTICES.md', 'requirements.txt', 'requirements-build.txt',
    'build.ps1', '.gitignore', '.gitattributes',
)
TREES = ('web', 'docs', '.github', 'scripts', 'third_party')


def source_files():
    found = []
    for name in TOP_FILES:
        path = ROOT / name
        if not path.is_file():
            raise ValueError('Missing source file: ' + name)
        found.append(path)
    for name in TREES:
        folder = ROOT / name
        if not folder.is_dir():
            raise ValueError('Missing source directory: ' + name)
        for path in sorted(folder.rglob('*')):
            if path.is_symlink():
                raise ValueError('Symlink is not allowed in release sources: ' + str(path))
            if not path.is_file():
                continue
            if '__pycache__' in path.parts or path.suffix in ('.pyc', '.pyo'):
                continue
            if path.name in ('connection.json', 'settings.json', 'known_hosts', 'auth.json') or path.suffix.lower() in ('.db', '.sqlite', '.sqlite3', '.pem', '.key'):
                raise ValueError('Private file is not allowed: ' + str(path))
            found.append(path)
    return sorted(set(found), key=lambda p: p.relative_to(ROOT).as_posix())


def add_file(archive, path, name):
    if path.is_symlink():
        raise ValueError('Symlink is not allowed: ' + str(path))
    archive.write(path, name)


def sha256(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def build(dist, output):
    if not (dist / 'CodexMonitor.exe').is_file() or not (dist / '_internal').is_dir():
        raise ValueError('Expected a complete PyInstaller onedir build')
    if set(p.name for p in dist.iterdir()) != {'CodexMonitor.exe', '_internal'}:
        raise ValueError('Unexpected files in build directory; use a fresh build')
    if (dist / '_internal' / 'data').exists():
        raise ValueError('Private data directory found in build')
    output.mkdir(parents=True, exist_ok=True)
    source = output / f'CodexMonitor-v{VERSION}-source.zip'
    files = source_files()
    with zipfile.ZipFile(source, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in files:
            add_file(archive, path, f'codex-monitor-v{VERSION}/' + path.relative_to(ROOT).as_posix())

    names = ('paramiko', 'cryptography', 'bcrypt', 'PyNaCl', 'cffi', 'pycparser', 'invoke',
             'pyinstaller', 'pyinstaller-hooks-contrib', 'altgraph', 'packaging', 'pefile',
             'pywin32-ctypes', 'setuptools')
    info = {
        'application': 'Codex Monitor', 'version': VERSION,
        'python': platform.python_version(), 'platform': platform.system(),
        'architecture': platform.machine(),
        'dependencies': {name: importlib.metadata.version(name) for name in names},
        'source_archive': source.name, 'source_sha256': sha256(source),
        'executable_sha256': sha256(dist / 'CodexMonitor.exe'),
        'source_files_sha256': {path.relative_to(ROOT).as_posix(): sha256(path) for path in files},
    }
    windows = output / f'CodexMonitor-v{VERSION}-windows-x64.zip'
    with zipfile.ZipFile(windows, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for path in sorted(dist.rglob('*')):
            if path.is_file():
                add_file(archive, path, 'CodexMonitor/' + path.relative_to(dist).as_posix())
        for name in ('LICENSE', 'README.md', 'THIRD_PARTY_NOTICES.md', 'CHANGELOG.md'):
            add_file(archive, ROOT / name, 'CodexMonitor/' + name)
        add_file(archive, ROOT / 'docs' / 'USER_GUIDE.zh-CN.md', 'CodexMonitor/使用说明.md')
        for path in sorted((ROOT / 'third_party').rglob('*')):
            if path.is_file():
                add_file(archive, path, 'CodexMonitor/' + path.relative_to(ROOT).as_posix())
        add_file(archive, source, 'CodexMonitor/' + source.name)
        archive.writestr('CodexMonitor/BUILD_INFO.json', json.dumps(info, ensure_ascii=False, indent=2) + '\n')
    (output / 'SHA256SUMS.txt').write_text(''.join(f'{sha256(path)}  {path.name}\n' for path in (windows, source)), encoding='ascii')
    print(json.dumps({'version': VERSION, 'source_files': len(files), 'windows_bytes': windows.stat().st_size,
                      'source_bytes': source.stat().st_size}, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--dist', type=Path, default=ROOT / 'dist' / 'CodexMonitor')
    parser.add_argument('--output', type=Path, default=ROOT / 'release')
    args = parser.parse_args()
    build(args.dist.resolve(), args.output.resolve())
