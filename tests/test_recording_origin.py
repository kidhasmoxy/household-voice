"""Public portable receipt classifier checks: stdlib only, temporary SQLite."""
from contextlib import closing
import importlib.util
import json
from pathlib import Path
import sqlite3
import tempfile
from types import SimpleNamespace
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

    def new_contract(self):
        self.execute(self.captures,'ALTER TABLE capture_uploads ADD COLUMN signature TEXT')
        ledger=module.HandoffLedger(self.scheduler,clock=lambda:1000)
        source=SimpleNamespace(device_id='fixture-source',entity_id='assist_satellite.fixture',
                               area_id='fixture-area',profile_id='native-fixture')
        targets=[{'kind':'speaker','target':'media_player.fixture'}]
        row,_=ledger.begin('request',source,targets,playback_contract=module.STANDARD_MA_CONTRACT)
        ledger.advance('request','reserved','capturing')
        ledger.advance('request','capturing','uploading',upload_signature='b'*64)
        ledger.advance('request','uploading','ready',recording_id=self.identifier)
        self.execute(self.captures,'INSERT INTO capture_uploads VALUES(?,?,?,?)',
                     (row['nonce'],'complete',self.identifier,'b'*64))
        return ledger,source,targets

    def test_new_native_contract_reopens_with_standard_ma_and_identity_bound(self):
        ledger,source,targets=self.new_contract();self.assertEqual(self.classify(),'standard_ma')
        reopened=module.HandoffLedger(self.scheduler)
        row,created=reopened.begin('request',source,targets,playback_contract=module.STANDARD_MA_CONTRACT)
        self.assertFalse(created);self.assertEqual(row['signature'],ledger.get('request')['signature'])
        self.assertTrue(row['nonce'].startswith('native_'))
        with self.assertRaises(ValueError):reopened.begin('request',source,targets)

    def test_versioned_contract_needs_matching_authority_source_and_upload_receipt(self):
        ledger,source,targets=self.new_contract()
        self.execute(self.scheduler,'UPDATE native_capture_handoffs SET source_identity=?',
                     (json.dumps({**source.__dict__,'device_id':'other'}),))
        with self.assertRaises(ValueError):self.classify()
        self.execute(self.scheduler,'UPDATE native_capture_handoffs SET source_identity=?',(json.dumps(source.__dict__),))
        self.execute(self.captures,"UPDATE capture_uploads SET signature='wrong'")
        with self.assertRaises(ValueError):self.classify()

    def test_new_contract_loss_or_unknown_version_never_downgrades_to_legacy(self):
        self.new_contract()
        self.execute(self.scheduler,"UPDATE native_capture_handoffs SET playback_contract='unreviewed'")
        with self.assertRaises(ValueError):self.classify()
        self.execute(self.scheduler,'UPDATE native_capture_handoffs SET playback_contract=NULL')
        self.assertEqual(self.classify(),'verified_native')
        self.execute(self.scheduler,'DROP TABLE native_capture_handoffs')
        self.assertEqual(self.classify(),'verified_native')


if __name__=='__main__':
    unittest.main()
