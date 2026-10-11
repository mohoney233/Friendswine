"""Create deliverable archives and verify every archived file after writing."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parent
VERSION = '1.3.0'


def digest(path):
    h = hashlib.sha256()
    with path.open('rb') as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b''):
            h.update(block)
    return h.hexdigest()


def included(root):
    for path in sorted(root.rglob('*')):
        rel = path.relative_to(root)
        if any(part in ('__pycache__', '.git', 'obj') for part in rel.parts):
            continue
        if rel.parts and rel.parts[0] == 'data':
            # Runtime state written next to the app is never shipped.
            continue
        if root.name == 'source' and ('bin' in rel.parts or rel.parts[0] in ('dist', 'downloads')):
            continue
        if path.is_file() and not path.name.endswith(('.pyc', '.pdb')):
            if path.is_symlink():
                raise RuntimeError('Unexpected file link: ' + str(path))
            yield path, rel.as_posix()


def archive(root, filename, prefix):
    destination = ROOT / filename
    if destination.exists():
        raise RuntimeError('Archive exists; inspect before replacing: ' + str(destination))
    expected = {}
    with zipfile.ZipFile(destination, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for path, rel in included(root):
            expected[prefix + '/' + rel] = digest(path)
            z.write(path, prefix + '/' + rel)
    with zipfile.ZipFile(destination) as z:
        if len(z.namelist()) != len(expected) or z.testzip() is not None:
            raise RuntimeError('Archive CRC or file-count validation failed')
        for name, sha in expected.items():
            h = hashlib.sha256()
            with z.open(name) as handle:
                for block in iter(lambda: handle.read(1024 * 1024), b''):
                    h.update(block)
            if h.hexdigest() != sha:
                raise RuntimeError('Archived file differs from verified tree: ' + name)
    return {'file': filename, 'bytes': destination.stat().st_size,
            'files': len(expected), 'sha256': digest(destination),
            'crc_and_all_file_sha256_verified': True}


results = [archive(ROOT / 'package', f'Friendswine-{VERSION}-Windows-x64.zip', f'Friendswine-{VERSION}'),
           archive(ROOT / 'modpack' / f'Friendswine-{VERSION}-Modpack',
                   f'Friendswine-{VERSION}-Modpack.zip', f'Friendswine-{VERSION}-Modpack'),
           archive(ROOT / 'source', f'Friendswine-{VERSION}-Source.zip', f'Friendswine-{VERSION}-Source')]
(ROOT / 'SHA256SUMS.txt').write_text(''.join(r['sha256'] + '  ' + r['file'] + '\n' for r in results), encoding='ascii')
(ROOT / 'archive-verification.json').write_text(json.dumps(results, indent=2) + '\n', encoding='utf8')
print(json.dumps(results, indent=2))
