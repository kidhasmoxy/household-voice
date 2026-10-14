"""Strict configuration and privilege-transition checks, without real credentials."""
import importlib.util
import io
import json
import stat
from contextlib import ExitStack
from types import SimpleNamespace
from pathlib import Path
import unittest
from unittest.mock import patch

ROOT=Path(__file__).resolve().parents[1]
spec=importlib.util.spec_from_file_location('worker_bootstrap',ROOT/'recording_worker/bootstrap.py')
bootstrap=importlib.util.module_from_spec(spec);spec.loader.exec_module(bootstrap)


class ConfigTests(unittest.TestCase):
    def read(self,raw,*,mode=stat.S_IFREG|0o600,uid=0,size=None,read_error=None):
        info=SimpleNamespace(st_mode=mode,st_uid=uid,st_size=len(raw) if size is None else size)
        with patch.object(bootstrap.os,'O_NOFOLLOW',0x100,create=True),patch.object(bootstrap.os,'O_CLOEXEC',0x200,create=True),patch.object(bootstrap.os,'O_NONBLOCK',0x400,create=True),patch.object(bootstrap.os,'open',return_value=71) as opened,patch.object(bootstrap.os,'fstat',return_value=info),patch.object(bootstrap.os,'read',return_value=raw,side_effect=read_error) as read,patch.object(bootstrap.os,'close') as close:
            try:return bootstrap.read_enabled()
            finally:
                close.assert_called_once_with(71)
                opened.assert_called_once_with('/data/options.json',bootstrap.os.O_RDONLY|0x100|0x200|0x400)
                if read.called:read.assert_called_once_with(71,4097)

    def test_root_private_boolean_options(self):
        self.assertFalse(self.read(b'{"enabled":false}'))
        self.assertTrue(self.read(b'{"enabled":true}'))

    def test_invalid_content_is_fixed_and_bounded(self):
        for raw in (b'',b'private-secret-sentinel',b'\xff',b'[]',b'null',b'{"enabled":1}',b'{"enabled":"false"}',b'{"enabled":null}',b'{}',b'{"enabled":true,"secret":"private-secret-sentinel"}',b'{"enabled":true,"enabled":false}',b'{"enabled":false}\n{}',b' '*4097):
            with self.subTest(raw_length=len(raw)),self.assertRaisesRegex(bootstrap.BootstrapRejected,'^configuration_unavailable$'):
                self.read(raw)

    def test_file_identity_type_mode_and_size_guards(self):
        cases=({'uid':10001},{'mode':stat.S_IFREG|0o620},{'mode':stat.S_IFREG|0o602},{'mode':stat.S_IFIFO|0o600},{'mode':stat.S_IFDIR|0o700},{'mode':stat.S_IFLNK|0o600},{'size':0},{'size':4097})
        for case in cases:
            with self.subTest(case=case),self.assertRaisesRegex(bootstrap.BootstrapRejected,'^configuration_unavailable$'):
                self.read(b'{"enabled":false}',**case)

    def test_growth_after_fstat_is_still_bounded(self):
        with self.assertRaisesRegex(bootstrap.BootstrapRejected,'^configuration_unavailable$'):
            self.read(b' '*4097,size=19)

    def test_read_and_open_failures_are_redacted(self):
        with self.assertRaisesRegex(bootstrap.BootstrapRejected,'^configuration_unavailable$'):
            self.read(b'{"enabled":false}',read_error=PermissionError('private-secret-sentinel'))
        with patch.object(bootstrap.os,'O_NOFOLLOW',0x100,create=True),patch.object(bootstrap.os,'O_CLOEXEC',0x200,create=True),patch.object(bootstrap.os,'O_NONBLOCK',0x400,create=True),patch.object(bootstrap.os,'open',side_effect=PermissionError('private-secret-sentinel')),patch.object(bootstrap.os,'close') as close,self.assertRaisesRegex(bootstrap.BootstrapRejected,'^configuration_unavailable$'):
            bootstrap.read_enabled()
        close.assert_not_called()


class DropTests(unittest.TestCase):
    def exercise(self,*,fail=None,uid=0,remaining_cap=False,final_uid=10001):
        events=[]
        class Lib:
            def prctl(self,option,*args):
                events.append(('prctl',option))
                if fail=='prctl':return -1
                return 1 if option==39 else 0
            def capset(self,*args):
                events.append(('capset',))
                return -1 if fail=='capset' else 0
            def capget(self,header,data):
                events.append(('capget',))
                if remaining_cap:data._obj[0].effective=1
                return -1 if fail=='capget' else 0
        def identity(name):
            def call(value):
                events.append((name,value))
                if fail==name:raise PermissionError('private-secret-sentinel')
            return call
        with ExitStack() as stack:
            for name in ('getuid','geteuid'):stack.enter_context(patch.object(bootstrap.os,name,side_effect=[uid,final_uid],create=True))
            for name in ('getgid','getegid'):stack.enter_context(patch.object(bootstrap.os,name,return_value=10001,create=True))
            stack.enter_context(patch.object(bootstrap.os,'getgroups',return_value=[],create=True))
            for name in ('setgroups','setgid','setuid'):stack.enter_context(patch.object(bootstrap.os,name,side_effect=identity(name),create=True))
            stack.enter_context(patch.object(bootstrap.ctypes,'CDLL',return_value=Lib()))
            bootstrap.drop_privileges()
        return events

    def test_order_clears_groups_before_identity_and_caps(self):
        events=self.exercise()
        self.assertEqual(events,[('prctl',8),('setgroups',[]),('setgid',10001),('setuid',10001),('capset',),('capget',),('prctl',38),('prctl',39)])

    def test_failed_drop_never_falls_back(self):
        for failure in ('prctl','setgroups','setgid','setuid','capset','capget'):
            with self.subTest(failure=failure),self.assertRaisesRegex(bootstrap.BootstrapRejected,'^privilege_drop_failed$'):
                self.exercise(fail=failure)

    def test_nonroot_start_or_residual_privilege_rejected(self):
        for arguments in ({'uid':10001},{'remaining_cap':True},{'final_uid':0}):
            with self.subTest(arguments=arguments),self.assertRaisesRegex(bootstrap.BootstrapRejected,'^privilege_drop_failed$'):
                self.exercise(**arguments)


class BootstrapOrderTests(unittest.TestCase):
    def test_network_runtime_only_called_after_drop(self):
        events=[]
        with patch.object(bootstrap,'read_enabled',side_effect=lambda:events.append('read') or False),patch.object(bootstrap,'drop_privileges',side_effect=lambda:events.append('drop')),patch.object(bootstrap,'run_transport',side_effect=lambda enabled:events.append(('transport',enabled))):
            self.assertEqual(bootstrap.main(),0)
        self.assertEqual(events,['read','drop',('transport',False)])

    def test_configuration_or_drop_failure_never_calls_runtime(self):
        for source,category in (('read_enabled','configuration_unavailable'),('drop_privileges','privilege_drop_failed')):
            with self.subTest(source=source),patch.object(bootstrap,'read_enabled',return_value=False),patch.object(bootstrap,'drop_privileges'),patch.object(bootstrap,source,side_effect=bootstrap.BootstrapRejected(category)),patch.object(bootstrap,'run_transport') as runtime,patch('sys.stdout',new_callable=io.StringIO) as output:
                self.assertEqual(bootstrap.main(),1)
                self.assertEqual(json.loads(output.getvalue()),{'status':category})
                runtime.assert_not_called()

    def test_unexpected_failure_never_exposes_exception(self):
        with patch.object(bootstrap,'read_enabled',side_effect=RuntimeError('private-secret-sentinel')),patch.object(bootstrap,'run_transport') as runtime,patch('sys.stdout',new_callable=io.StringIO) as output:
            self.assertEqual(bootstrap.main(),1)
            self.assertEqual(json.loads(output.getvalue()),{'status':'bootstrap_unavailable'})
            runtime.assert_not_called()


if __name__=='__main__':unittest.main()
