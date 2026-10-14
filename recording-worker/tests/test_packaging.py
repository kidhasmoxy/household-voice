"""Portable public export checks; no runtime validator dependency."""
import importlib.util
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('worker_export_candidate',ROOT/'scripts/export_candidate.py')
recipe=importlib.util.module_from_spec(spec);spec.loader.exec_module(recipe)

def canonical():
    sys.path.insert(0,str(ROOT/'tests'))
    from validator_source import find_validator
    return find_validator(ROOT)

class PublicExportTests(unittest.TestCase):
    def test_export_runs_without_private_workspace_layout_or_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'public'
            manifest=recipe.export(output,canonical())
            self.assertEqual(manifest['file_count'],len(recipe.PUBLIC_FILES)+4)
            self.assertFalse(manifest['publication_performed']);self.assertFalse(manifest['runtime_fixture_included'])
            env=dict(os.environ);env.pop('HOUSEHOLD_VOICE_RECORDING_VALIDATOR',None)
            result=subprocess.run([sys.executable,'-m','unittest','discover','-s','tests','-p','test_[bcw]*.py','-v'],
                cwd=output/'recording-worker',env=env,text=True,encoding='utf-8',capture_output=True,timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
    def test_existing_destination_is_preserved(self):
        with tempfile.TemporaryDirectory() as directory:
            output=Path(directory)/'existing';output.mkdir();sentinel=output/'keep.txt';sentinel.write_text('preserve')
            with self.assertRaises(FileExistsError):recipe.export(output,canonical())
            self.assertEqual(sentinel.read_text(),'preserve')
    def test_changed_runtime_source_rejects_export(self):
        with tempfile.TemporaryDirectory() as directory:
            fake=Path(directory)/'source';(fake/'tests/evidence').mkdir(parents=True)
            (fake/'tests/evidence/image-build.json').write_text(json.dumps({'source_files':{'client.py':'0'*64}}))
            (fake/'client.py').write_text('modified')
            with self.assertRaisesRegex(ValueError,'changed since'):recipe.export(Path(directory)/'output',canonical(),root=fake)

if __name__=='__main__':unittest.main()
