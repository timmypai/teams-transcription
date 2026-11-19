"""Owner-only fixed runtime media policy; no arbitrary paths, OAuth or network."""
import json
import os
from pathlib import Path
import stat
import tempfile

from .assistant_config import load_assistant_settings
from .runtime_guard import RuntimeLiveGuard


def save_media_policy(settings, hosts):
    RuntimeLiveGuard('owner-settings',hosts)  # validate only; [] is deny-all
    if os.environ.get('ASSISTANT_ALLOWED_EGRESS_HOSTS'):
        raise ValueError('media_policy_environment_override')
    root=Path(settings.data_root);parent=root/'config';path=parent/'runtime.yml'
    for target,directory in ((root,True),(parent,True),(path,False)):
        info=target.lstat()
        if (target.absolute()!=target.resolve() or info.st_uid!=os.getuid() or info.st_mode&0o077
                or not (stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode))):
            raise ValueError('private_runtime_required')
    if path.stat().st_nlink!=1:
        raise ValueError('private_runtime_required')
    current=load_assistant_settings(path)
    if current.data_root!=settings.data_root:
        raise ValueError('runtime_root_mismatch')
    raw=path.read_bytes()
    if len(raw)>65536:raise ValueError('invalid_assistant_configuration')
    import yaml
    document=yaml.safe_load(raw)
    document['allowed_egress_hosts']=list(hosts)
    # Validate with actual precedence before touching disk; preserve all other
    # operator keys/values, not a defaults-filled replacement of their config.
    candidate=load_assistant_settings(overrides=document)
    if candidate.allowed_egress_hosts!=hosts:
        raise ValueError('media_policy_environment_override')
    fd,name=tempfile.mkstemp(prefix='.runtime-',dir=parent)
    temporary=Path(name)
    try:
        with os.fdopen(fd,'w') as out:
            json.dump(document,out);out.flush();os.fsync(out.fileno())
        os.replace(temporary,path)
        directory=os.open(parent,os.O_RDONLY|os.O_DIRECTORY)
        try:os.fsync(directory)
        finally:os.close(directory)
    finally:temporary.unlink(missing_ok=True)
    settings.allowed_egress_hosts=list(hosts)
    return dict(saved=True,egress_deny_all=not hosts,host_count=len(hosts))
