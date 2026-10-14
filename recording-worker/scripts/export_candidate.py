"""Create a reviewed, portable add-on repository overlay without private files.

Requires an explicit canonical validator source only for test fixtures; that
fixture is never copied into the runtime image. Existing destinations are refused.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re

ROOT=Path(__file__).resolve().parents[1]
PUBLIC_FILES=('Dockerfile','.dockerignore','.gitattributes','config.yaml','requirements.txt','LICENSE',
    'NOTICE.md','README.md','DOCS.md','BUILDING.md','CHANGELOG.md',
    'recording_worker/__init__.py','recording_worker/__main__.py',
    'recording_worker/client.py','recording_worker/protocol.py',
    'tests/test_worker.py','tests/test_control.py','tests/validator_source.py','tests/test_packaging.py',
    'scripts/export_candidate.py','ci/recording-worker-check.yml','tests/evidence/image-build.json')

def export(destination,validator_source,root=ROOT):
    destination=Path(destination);validator_source=Path(validator_source)
    if destination.exists():raise FileExistsError('Refusing to overwrite an existing export')
    if validator_source.name!='recording_session.py' or not validator_source.is_file():
        raise ValueError('An explicit canonical recording_session.py source is required')
    receipt=json.loads((root/'tests/evidence/image-build.json').read_text(encoding='utf-8'))
    for name,expected in receipt['source_files'].items():
        if hashlib.sha256((root/name).read_bytes()).hexdigest()!=expected:
            raise ValueError('Runtime source has changed since the reviewed image build')
    files={'recording-worker/'+name:(root/name).read_bytes() for name in PUBLIC_FILES}
    files['.github/workflows/recording-worker.yml']=(root/'ci/recording-worker-check.yml').read_bytes()
    original=validator_source.read_bytes()
    # The Linux image fixture has LF endings; preserve that reviewed byte form.
    raw=validator_source.read_text(encoding='utf-8').encode('utf-8')
    if hashlib.sha256(raw).hexdigest()!=receipt['test_validator_sha256']:
        raise ValueError('Canonical test validator differs from the reviewed image test source')
    files['recording-worker/tests/fixtures/recording_session.py']=raw
    files['recording-worker/tests/fixtures/source.json']=(json.dumps({
        'origin':'canonical custom_components/household_voice/recording_session.py',
        'origin_project':'https://github.com/kidhasmoxy/household-voice',
        'sha256':hashlib.sha256(raw).hexdigest(),'purpose':'test-only; excluded from runtime image'},indent=2)+'\n').encode()
    provenance=json.loads(files['recording-worker/tests/fixtures/source.json'])
    provenance.update(origin_raw_sha256=hashlib.sha256(original).hexdigest(),line_endings='canonical source normalized to LF')
    files['recording-worker/tests/fixtures/source.json']=(json.dumps(provenance,indent=2)+'\n').encode()
    files['repository.yaml']=b'name: Household Voice apps\nurl: https://github.com/kidhasmoxy/household-voice\nmaintainer: Household Voice contributors\n'
    for raw in files.values():
        text=raw.decode('utf-8')
        if re.search(r'eyJ[A-Za-z0-9_-]{20,}\.[A-Za-z0-9_-]{20,}\.',text):
            raise ValueError('Possible credential in public export')
        if re.search(r'https?://(?:10\.|192\.168\.|172\.(?:1[6-9]|2[0-9]|3[01])\.)',text):
            raise ValueError('Private LAN address in public export')
    hashes={name:hashlib.sha256(raw).hexdigest() for name,raw in sorted(files.items())}
    manifest={'created_at':datetime.now(timezone.utc).isoformat(),'version':receipt['version'],
        'files':hashes,'file_count':len(files),'source_digest':hashlib.sha256(json.dumps(hashes,sort_keys=True).encode()).hexdigest(),
        'reviewed_image_digest':receipt['image_manifest_digest'],
        'canonical_validator_sha256':hashlib.sha256(files['recording-worker/tests/fixtures/recording_session.py']).hexdigest(),
        'runtime_fixture_included':False,'publication_performed':False,
        'destination_kind':'new directory; repository overlay only'}
    destination.mkdir(parents=True)
    for name,raw in files.items():
        target=destination/name
        if not target.resolve().is_relative_to(destination.resolve()):raise ValueError('Invalid export member')
        target.parent.mkdir(parents=True,exist_ok=True);target.write_bytes(raw)
    (destination/'EXPORT-MANIFEST.json').write_text(json.dumps(manifest,indent=2)+'\n',encoding='utf-8')
    return manifest

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--destination',required=True,type=Path)
    parser.add_argument('--validator-source',required=True,type=Path)
    args=parser.parse_args()
    result=export(args.destination,args.validator_source)
    print(json.dumps({'source_digest':result['source_digest'],'files':result['file_count'],
        'reviewed_image_digest':result['reviewed_image_digest'],'publication_performed':False}))

if __name__=='__main__':main()
