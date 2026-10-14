"""Read Supervisor's private config, then irreversibly become the worker.

Invoked with Python -I -S: no site or networking imports while privileged.
Supervisor's root-owned options stay 0600. Nothing writes or relaxes that file.
"""
import ctypes
import json
import os
import stat
import sys

OPTIONS='/data/options.json'
MAX_OPTIONS_BYTES=4096
WORKER_UID=WORKER_GID=10001


class BootstrapRejected(ValueError):
    """Fixed categories only; never configuration/environment/exception content."""


def unique_object(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise BootstrapRejected('configuration_unavailable')
        result[key]=value
    return result


def read_enabled():
    """One bounded no-follow/nonblocking regular-file read; strict bool only."""
    fd=None
    try:
        fd=os.open(OPTIONS,os.O_RDONLY|os.O_NOFOLLOW|os.O_CLOEXEC|os.O_NONBLOCK)
        info=os.fstat(fd)
        if (not stat.S_ISREG(info.st_mode) or info.st_uid!=0
                or stat.S_IMODE(info.st_mode)&0o022 or not 0<info.st_size<=MAX_OPTIONS_BYTES):
            raise BootstrapRejected('configuration_unavailable')
        raw=os.read(fd,MAX_OPTIONS_BYTES+1)
        if not raw or len(raw)>MAX_OPTIONS_BYTES:raise BootstrapRejected('configuration_unavailable')
        options=json.loads(raw.decode('utf-8'),object_pairs_hook=unique_object)
        if not isinstance(options,dict) or set(options)!={'enabled'} or type(options['enabled']) is not bool:
            raise BootstrapRejected('configuration_unavailable')
        return options['enabled']
    except (OSError,ValueError,TypeError,UnicodeError):
        raise BootstrapRejected('configuration_unavailable') from None
    finally:
        if fd is not None:os.close(fd)


def drop_privileges():
    """No fallback to root; clear groups/identity/caps and prohibit privilege gain."""
    try:
        if os.getuid()!=0 or os.geteuid()!=0:raise ValueError()
        libc=ctypes.CDLL(None,use_errno=True)
        # Never preserve root's capabilities when changing the UID.
        if libc.prctl(8,0,0,0,0)!=0:raise ValueError() # PR_SET_KEEPCAPS
        os.setgroups([]);os.setgid(WORKER_GID);os.setuid(WORKER_UID)
        class Header(ctypes.Structure):
            _fields_=[('version',ctypes.c_uint32),('pid',ctypes.c_int)]
        class Data(ctypes.Structure):
            _fields_=[('effective',ctypes.c_uint32),('permitted',ctypes.c_uint32),('inheritable',ctypes.c_uint32)]
        header=Header(0x20080522,0);data=(Data*2)()
        if libc.capset(ctypes.byref(header),ctypes.byref(data))!=0:raise ValueError()
        if libc.capget(ctypes.byref(header),ctypes.byref(data))!=0:raise ValueError()
        if any(item.effective or item.permitted or item.inheritable for item in data):raise ValueError()
        if libc.prctl(38,1,0,0,0)!=0 or libc.prctl(39,0,0,0,0)!=1:raise ValueError() # no-new-privileges
        if (os.getuid()!=WORKER_UID or os.geteuid()!=WORKER_UID
                or os.getgid()!=WORKER_GID or os.getegid()!=WORKER_GID or os.getgroups()!=[]):
            raise ValueError()
    except Exception:
        raise BootstrapRejected('privilege_drop_failed') from None


def run_transport(enabled):
    # Only the now-unprivileged process enables site packages and networking.
    import site
    site.main()
    sys.path.insert(0,'/app')
    from recording_worker.__main__ import main
    main(enabled=enabled)


def main():
    try:
        enabled=read_enabled()
        drop_privileges()
        run_transport(enabled)
    except BootstrapRejected as error:
        print(json.dumps({'status':str(error)}),flush=True)
        return 1
    except Exception:
        print(json.dumps({'status':'bootstrap_unavailable'}),flush=True)
        return 1
    return 0


if __name__=='__main__':sys.exit(main())
