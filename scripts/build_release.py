"""Build the install asset from the audited explicit manifest, excluding history/state."""
import argparse
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]


def build(tag=None):
    record = json.loads((ROOT / 'PUBLIC-RELEASE-MANIFEST.json').read_text())
    manifest = json.loads((ROOT / 'custom_components/household_voice/manifest.json').read_text())
    if tag is not None and tag != 'v' + manifest['version']:
        raise ValueError('Git tag must match the integration manifest version')
    prefix = 'custom_components/household_voice/'
    files = {}
    for relative, expected in sorted(record['files'].items()):
        path = ROOT / relative
        if path.is_symlink() or ROOT.resolve() not in path.resolve().parents:
            raise ValueError('Unsafe manifest path')
        raw = path.read_bytes()
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError('Release manifest hash mismatch: ' + relative)
        if relative.startswith(prefix):
            files[relative[len(prefix):]] = raw
    if 'manifest.json' not in files:
        raise ValueError('Integration manifest missing')
    for name in ('LICENSE', 'NOTICE.md'):
        files[name] = (ROOT / name).read_bytes()
    destination = ROOT / 'dist'
    destination.mkdir(exist_ok=True)
    target = destination / 'household_voice.zip'
    with zipfile.ZipFile(target, 'w', compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for relative, raw in sorted(files.items()):
            info = zipfile.ZipInfo(relative, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, raw)
    digest = hashlib.sha256(target.read_bytes()).hexdigest()
    target.with_suffix('.zip.sha256').write_text(digest + '  household_voice.zip\n')
    print(json.dumps({'asset': target.name, 'files': len(files), 'sha256': digest}))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--tag')
    build(parser.parse_args().tag)
