"""Portable release checks: real SQLite behavior, with no HA/server/physical devices."""
import hashlib
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = ROOT / 'custom_components/household_voice'
spec = importlib.util.spec_from_file_location('release_scheduler', COMPONENT / '_core/scheduler.py')
scheduler = importlib.util.module_from_spec(spec)
spec.loader.exec_module(scheduler)


class ReleaseTests(unittest.TestCase):
    def test_manifest_and_files(self):
        report = json.loads((ROOT / 'PUBLIC-RELEASE-MANIFEST.json').read_text())
        manifest = json.loads((COMPONENT / 'manifest.json').read_text())
        self.assertEqual(manifest['version'], report['version'])
        self.assertEqual(manifest['domain'], 'household_voice')
        self.assertTrue(manifest['config_flow'])
        for name, digest in report['files'].items():
            with self.subTest(name=name):
                path = ROOT / name
                self.assertFalse(path.is_symlink())
                self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(), digest)

    def test_timer_persistence_idempotency_and_stale_revision(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.0]
            path = Path(directory) / 'scheduler.sqlite3'
            service = scheduler.Scheduler(path, clock=lambda: now[0])
            timer = service.create_timer('Fixture timer', 60, 'fixture_room', 'fixture-create')
            now[0] += 20
            reopened = scheduler.Scheduler(path, clock=lambda: now[0])
            self.assertEqual(reopened.create_timer('Fixture timer', 60, 'fixture_room', 'fixture-create'), timer)
            paused = reopened.command(timer['id'], 'pause', 'fixture-pause', timer['revision'])
            self.assertEqual(paused['remaining'], 40)
            with self.assertRaisesRegex(ValueError, 'Revision conflict'):
                reopened.command(timer['id'], 'cancel', 'fixture-stale', timer['revision'])
            again = scheduler.Scheduler(path, clock=lambda: now[0])
            self.assertEqual(again.list(), [paused])

    def test_cancel_survives_restart_and_never_rings(self):
        with tempfile.TemporaryDirectory() as directory:
            now = [1000.0]
            path = Path(directory) / 'scheduler.sqlite3'
            service = scheduler.Scheduler(path, clock=lambda: now[0])
            timer = service.create_timer('Fixture cancel', 5, 'fixture_room', 'fixture-create')
            service.command(timer['id'], 'cancel', 'fixture-cancel', timer['revision'])
            now[0] += 10
            self.assertEqual(scheduler.Scheduler(path, clock=lambda: now[0]).tick(), [])


if __name__ == '__main__':
    unittest.main()
