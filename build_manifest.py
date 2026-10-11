"""Generate a deterministic file integrity manifest for the release tree."""
import hashlib
import json
from pathlib import Path
import sys

root = Path(sys.argv[1]).resolve()
files = {}
stray = []
for path in sorted(root.rglob('*')):
    if not path.is_file():
        continue
    rel = path.relative_to(root)
    if path.name in ('manifest.json', '.friendswine-install.json') or '__pycache__' in path.parts:
        continue
    if rel.parts and rel.parts[0] == 'data':
        # Runtime state written next to the app is never part of a release.
        continue
    if path.is_symlink():
        raise RuntimeError('Unexpected symlink: ' + str(path))
    h = hashlib.sha256()
    with path.open('rb') as f:
        for block in iter(lambda: f.read(1024 * 1024), b''):
            h.update(block)
    files[rel.as_posix()] = h.hexdigest()
for entry in root.iterdir():
    if entry.name == 'data':
        stray.append(entry.name)
(root / 'manifest.json').write_text(json.dumps({'product': 'Friendswine', 'version': '1.3.0', 'algorithm': 'SHA-256', 'files': files}, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps({'manifest_files': len(files), 'root': str(root), 'runtime_data_ignored': stray}))
