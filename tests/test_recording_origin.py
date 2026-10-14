"""Public portable receipt classifier checks: stdlib only, temporary SQLite."""
from contextlib import closing
import importlib.util
from pathlib import Path
import sqlite3
import tempfile
import unittest

source=Path(__file__).resolve().parents[1]/'custom_components/household_voice/native_recording_ledger.py'
spec=importlib.util.spec_from_file_location('recording_origin_fixture',source)
module=importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class RecordingOriginTests(unittest.TestCase):
    def setUp(self):
        self.temporary=tempfile.TemporaryDirectory();self.addCleanup(self.temporary.cleanup)
        self.root=Path(self.temporary.name)
        self.scheduler=self.root/'scheduler.sqlite3';self.captures=self.root/'captures.sqlite3'
        self.identifier='a'*32
        self.execute(self.scheduler,'CREATE TABLE fixture(id TEXT)')
        self.execute(self.captures,'CREATE TABLE capture_uploads(nonce TEXT,status TEXT,recording_id TEXT)')

    @staticmethod
    def execute(path,sql,parameters=()):
        with closing(sqlite3.connect(path)) as database,database:
            database.execute(sql,parameters)

    def receipt(self,nonce='legacy-fixture-nonce',status='complete'):
        self.execute(self.captures,'INSERT INTO capture_uploads VALUES(?,?,?)',(nonce,status,self.identifier))

    def classify(self):
        return module.recording_transport_policy(self.scheduler,self.captures,self.identifier)

    def test_unique_completed_non_native_receipt_authorizes_legacy_without_native_table(self):
        self.receipt();self.assertEqual(self.classify(),'legacy_ma')

    def test_native_nonce_without_ledger_table_remains_strict(self):
        self.receipt('native_'+('b'*64));self.assertEqual(self.classify(),'verified_native')

    def test_native_receipt_with_lost_upload_ack_null_recording_association_remains_strict(self):
        nonce='native_'+('b'*64)
        self.execute(self.scheduler,'CREATE TABLE native_capture_handoffs(nonce TEXT,recording_id TEXT)')
        self.execute(self.scheduler,'INSERT INTO native_capture_handoffs VALUES(?,NULL)',(nonce,))
        self.receipt(nonce);self.assertEqual(self.classify(),'verified_native')

    def test_native_recording_or_nonce_association_cannot_downgrade(self):
        self.execute(self.scheduler,'CREATE TABLE native_capture_handoffs(nonce TEXT,recording_id TEXT)')
        self.execute(self.scheduler,'INSERT INTO native_capture_handoffs VALUES(?,?)',('unusual-fixture-native',self.identifier))
        self.assertEqual(self.classify(),'verified_native')
        self.execute(self.scheduler,'UPDATE native_capture_handoffs SET recording_id=NULL')
        self.receipt('unusual-fixture-native');self.assertEqual(self.classify(),'verified_native')

    def test_missing_pending_invalid_duplicate_or_corrupt_receipt_refuses(self):
        with self.assertRaisesRegex(ValueError,'recording_origin_unverified'):self.classify()
        for nonce,status in [('legacy-fixture-nonce','reserved'),('short','complete')]:
            self.execute(self.captures,'DELETE FROM capture_uploads');self.receipt(nonce,status)
            with self.assertRaises(ValueError):self.classify()
        self.receipt()
        with self.assertRaises(ValueError):self.classify()
        self.captures.write_bytes(b'not SQLite')
        with self.assertRaisesRegex(ValueError,'recording_origin_unverified'):self.classify()

    def test_classifier_reads_without_migration_or_metadata_writes(self):
        self.receipt();original=(self.scheduler.read_bytes(),self.captures.read_bytes())
        self.assertEqual(self.classify(),'legacy_ma')
        self.assertEqual(original,(self.scheduler.read_bytes(),self.captures.read_bytes()))

    def test_missing_receipt_file_is_not_created(self):
        path=self.root/'missing.sqlite3'
        with self.assertRaises(ValueError):module.recording_transport_policy(self.scheduler,path,self.identifier)
        self.assertFalse(path.exists())


if __name__=='__main__':
    unittest.main()
