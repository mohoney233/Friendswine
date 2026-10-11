"""Rebuild a complete Windows x64 package from this source archive.

Requires a Windows x64 Python and .NET SDK 8.0.424. Downloads only the pinned
official Python embeddable archive, unless supplied through --python-zip.
No game process is accessed or started.
"""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import sys
import urllib.request
import zipfile

ROOT = Path(__file__).resolve().parent
PYTHON_URL = 'https://www.python.org/ftp/python/3.14.0/python-3.14.0-embed-amd64.zip'
PYTHON_SHA = '8d4d3590c10449d78aa4375f534e6d5f3027d67fdc362dd1a882279db6f90fdf'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--python-zip', type=Path)
    parser.add_argument('--out', type=Path, default=ROOT / 'dist' / 'Friendswine-1.3.0')
    args = parser.parse_args()
    out = args.out.resolve()
    if out.exists():
        raise RuntimeError('Output already exists; choose a new --out directory.')
    archive = args.python_zip
    if archive is None:
        archive = ROOT / 'downloads' / 'python-3.14.0-embed-amd64.zip'
        archive.parent.mkdir(parents=True, exist_ok=True)
        if not archive.exists():
            urllib.request.urlretrieve(PYTHON_URL, archive)
    if digest(archive) != PYTHON_SHA:
        raise RuntimeError('Official Python archive SHA-256 mismatch.')
    out.mkdir(parents=True)
    shutil.copytree(ROOT / 'app', out / 'app', ignore=shutil.ignore_patterns('__pycache__'))
    shutil.copytree(ROOT / 'templates', out, dirs_exist_ok=True)
    (out / 'runtime').mkdir()
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            if not (out / 'runtime' / member.filename).resolve().is_relative_to(out / 'runtime'):
                raise RuntimeError('Unexpected runtime archive path.')
        z.extractall(out / 'runtime')
    (out / 'runtime' / 'python314._pth').write_text('python314.zip\n.\n../app\n', encoding='ascii')
    subprocess.run(['dotnet', 'publish', str(ROOT / 'injector/fuxiang-inject.csproj'),
                    '-c', 'Release', '-r', 'win-x64', '--self-contained', 'true',
                    '-p:RuntimeFrameworkVersion=8.0.30', '-p:PublishSingleFile=false',
                    '-o', str(out / 'bin')], check=True, cwd=ROOT)
    for pdb in (out / 'bin').glob('*.pdb'):
        pdb.unlink()
    subprocess.run([sys.executable, '-B', str(ROOT / 'build_manifest.py'), str(out)], check=True)
    # The tree must match the manifest it ships: the manifest is the release's
    # file inventory, and every archived file is hashed again when packing.
    manifest = json.loads((out / 'manifest.json').read_text(encoding='utf-8'))
    for name, expected in manifest['files'].items():
        path = out / name
        if not path.is_file() or digest(path) != expected:
            raise RuntimeError('Built tree does not match its manifest: ' + name)
    print(json.dumps({'built': str(out), 'game_modified': False}))


if __name__ == '__main__':
    main()
