"""Install a read-only SSH login notice for the pinned DeepInfer container."""
import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shlex
import shutil
import stat
import tempfile

from .deepinfer import docker_command


NOTICE = '当前deepinfer服务为卡空闲时使用，如果你需要使用NPU可直接杀死。'
DESTINATION = Path('/etc/profile.d/llmdeploy-deepinfer-notice.sh')
CGROUP_ROOT = '/sys/fs/cgroup/pids/docker'


def render_notice(container_id, container_name, *, cgroup_root=CGROUP_ROOT, proc_root='/proc'):
    if not isinstance(container_id, str) or not re.fullmatch('[0-9a-f]{64}', container_id):
        raise ValueError('INVALID_CONTAINER_ID')
    if not isinstance(container_name, str) or not re.fullmatch('[A-Za-z0-9][A-Za-z0-9_.-]{0,127}', container_name):
        raise ValueError('INVALID_CONTAINER_NAME')
    for root in (cgroup_root, proc_root):
        if (not isinstance(root, str) or not PurePosixPath(root).is_absolute()
                or '..' in PurePosixPath(root).parts or any(c in root for c in '\x00\r\n')):
            raise ValueError('INVALID_ROOT')
    cgroup_file = str(PurePosixPath(cgroup_root) / container_id / 'cgroup.procs')
    target = '对应容器：' + container_name + '；请只结束此服务，避免误杀其他任务。'
    return f'''# Read-only notice for an interactive SSH login. Installed from the pinned recovery config.
case $- in
    *i*)
        if [ -n "${{SSH_CONNECTION:-}}" ] && [ -t 0 ] && [ -t 1 ]; then
            (
                _llmdeploy_cgroup={shlex.quote(cgroup_file)}
                _llmdeploy_proc={shlex.quote(proc_root)}
                if [ -r "$_llmdeploy_cgroup" ]; then
                    while IFS= read -r _llmdeploy_pid; do
                        case $_llmdeploy_pid in ''|*[!0-9]*) continue ;; esac
                        if awk '
                            /^State:/ {{ alive = ($2 !~ /^[ZXx]$/) }}
                            /^NSpid:/ {{ init = ($NF == 1) }}
                            END {{ exit !(alive && init) }}
                        ' "$_llmdeploy_proc/$_llmdeploy_pid/status" 2>/dev/null; then
                            printf '%s\\n' {shlex.quote(NOTICE)} {shlex.quote(target)}
                            break
                        fi
                    done 2>/dev/null < "$_llmdeploy_cgroup"
                fi
            ) || :
        fi
        ;;
esac
'''


def verify_target(config):
    """Read and pin the same container identity used by the recovery observer."""
    container_id = config['container_id']
    container_name = config['container_name']
    render_notice(container_id, container_name)  # Validate before passing arguments to Docker.
    rows = json.loads(docker_command(config, ['inspect', '--type', 'container', container_id], timeout=15))
    if len(rows) != 1:
        raise ValueError('TARGET_CHANGED')
    row = rows[0]
    digest = hashlib.sha256(json.dumps({'config': row['Config'], 'hostconfig': row['HostConfig']},
                                      sort_keys=True).encode()).hexdigest()
    if (row['Id'] != container_id or row['Name'] != '/' + container_name
            or row['Image'] != config['image_id'] or digest != config['spec_sha256']):
        raise ValueError('TARGET_CHANGED')
    driver = docker_command(config, ['info', '--format', '{{.CgroupDriver}}'], timeout=15).strip()
    if driver != 'cgroupfs' or not Path(CGROUP_ROOT).is_dir():
        raise ValueError('UNSUPPORTED_CGROUP_LAYOUT')
    for directory in (Path(CGROUP_ROOT), *Path(CGROUP_ROOT).parents, Path('/proc')):
        if not directory.stat().st_mode & stat.S_IXOTH:
            raise PermissionError('CGROUP_NOT_ACCESSIBLE_TO_USERS')
    if not shutil.which('awk') or '/etc/profile.d' not in Path('/etc/profile').read_text():
        raise ValueError('UNSUPPORTED_LOGIN_PROFILE')
    if row['State']['Running']:
        pids = Path(CGROUP_ROOT, container_id, 'cgroup.procs')
        pid = row['State']['Pid']
        if (not pids.is_file() or not pids.stat().st_mode & stat.S_IROTH
                or str(pid) not in pids.read_text().splitlines()):
            raise ValueError('CONTAINER_CGROUP_MISMATCH')
        status = Path('/proc', str(pid), 'status')
        if not status.stat().st_mode & stat.S_IROTH:
            raise PermissionError('PROCESS_STATUS_NOT_READABLE')
        fields = dict(line.split(':', 1) for line in status.read_text().splitlines() if ':' in line)
        state = fields.get('State', '').split()
        if fields.get('NSpid', '').split()[-1:] != ['1'] or not state or state[0] in ('Z', 'X', 'x'):
            raise ValueError('CONTAINER_INIT_NOT_RUNNING')
    return row['State']['Running']


def install_atomic(destination, text):
    """Publish complete content once; never replace an existing login script."""
    destination = Path(destination)
    parent = destination.parent
    if not parent.is_dir() or parent.is_symlink():
        raise ValueError('INVALID_DESTINATION_DIRECTORY')
    if os.path.lexists(destination):
        raise FileExistsError('NOTICE_ALREADY_EXISTS')
    fd, tmp = tempfile.mkstemp(prefix='.llmdeploy-notice-', suffix='.tmp', dir=parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8', newline='\n') as out:
            out.write(text)
            out.flush()
            if hasattr(os, 'fchmod'):
                os.fchmod(out.fileno(), 0o644)
            else:
                os.chmod(tmp, 0o644)
            os.fsync(out.fileno())
        os.link(tmp, destination)
    finally:
        os.unlink(tmp)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--install', action='store_true', required=True)
    args = parser.parse_args()
    if os.geteuid() != 0:
        raise PermissionError('ROOT_REQUIRED')
    parent_stat = DESTINATION.parent.stat()
    if parent_stat.st_uid != 0 or parent_stat.st_mode & (stat.S_IWGRP | stat.S_IWOTH):
        raise PermissionError('UNSAFE_PROFILE_DIRECTORY')
    config = json.loads(args.config.read_text(encoding='utf-8'))
    running = verify_target(config)
    install_atomic(DESTINATION, render_notice(config['container_id'], config['container_name']))
    print(json.dumps({'state': 'INSTALLED', 'target_running': running}, ensure_ascii=True))


if __name__ == '__main__':
    main()
