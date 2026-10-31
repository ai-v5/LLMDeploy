import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

from deploy_recovery.ssh_notice import NOTICE, install_atomic, render_notice, verify_target


class NoticeRenderingTests(unittest.TestCase):
    def test_install_refuses_to_connect_to_unavailable_or_socket_activated_docker(self):
        config = {'docker': 'docker', 'container_id': 'a' * 64, 'container_name': 'target'}
        for service_state, socket_state in [('inactive', 'active'), ('active', 'active')]:
            with self.subTest(service=service_state, socket=socket_state):
                calls = []

                def fake_run(args, **kwargs):
                    calls.append(args)
                    if args[:3] == ['systemctl', 'show', 'docker.service']:
                        return SimpleNamespace(returncode=0, stdout=f'LoadState=loaded\nActiveState={service_state}\n')
                    if args[:3] == ['systemctl', 'show', 'docker.socket']:
                        return SimpleNamespace(returncode=0, stdout=f'LoadState=loaded\nActiveState={socket_state}\n')
                    self.fail('Docker must not be contacted in this state')

                with patch('subprocess.run', side_effect=fake_run):
                    with self.assertRaisesRegex(RuntimeError, 'DOCKER_UNAVAILABLE_OR_SOCKET_ACTIVATED'):
                        verify_target(config)
                self.assertEqual([args[2] for args in calls], ['docker.service', 'docker.socket'])

    def test_install_rechecks_docker_before_reading_cgroup_driver(self):
        identity = 'a' * 64
        row = {'Id': identity, 'Name': '/target', 'Image': 'image', 'Config': {}, 'HostConfig': {}}
        spec_sha = hashlib.sha256(json.dumps({'config': row['Config'], 'hostconfig': row['HostConfig']},
                                             sort_keys=True).encode()).hexdigest()
        config = {'docker': 'docker', 'container_id': identity, 'container_name': 'target',
                  'image_id': 'image', 'spec_sha256': spec_sha}
        socket_checks = 0
        docker_calls = []

        def fake_run(args, **kwargs):
            nonlocal socket_checks
            if args[:3] == ['systemctl', 'show', 'docker.service']:
                return SimpleNamespace(returncode=0, stdout='LoadState=loaded\nActiveState=active\n')
            if args[:3] == ['systemctl', 'show', 'docker.socket']:
                socket_checks += 1
                state = 'inactive' if socket_checks == 1 else 'active'
                return SimpleNamespace(returncode=0, stdout=f'LoadState=loaded\nActiveState={state}\n')
            docker_calls.append(args)
            if args[:2] == ['docker', 'inspect']:
                return SimpleNamespace(returncode=0, stdout=json.dumps([row]))
            self.fail('Docker info must not be contacted after the socket activates')

        with patch('subprocess.run', side_effect=fake_run):
            with self.assertRaisesRegex(RuntimeError, 'DOCKER_UNAVAILABLE_OR_SOCKET_ACTIVATED'):
                verify_target(config)
        self.assertEqual(socket_checks, 2)
        self.assertEqual(len(docker_calls), 1)

    def test_rejects_invalid_identity_and_paths(self):
        for identity in ('short', 'g' * 64, 'a' * 63 + "'", None):
            with self.subTest(identity=identity), self.assertRaises(ValueError):
                render_notice(identity, 'target')
        for name in ('', '../target', 'target;id', 'target\nother'):
            with self.subTest(name=name), self.assertRaises(ValueError):
                render_notice('a' * 64, name)
        with self.assertRaises(ValueError):
            render_notice('a' * 64, 'target', proc_root='/proc/../other')

    def test_install_does_not_replace_existing_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            dest = Path(tmp) / 'notice.sh'
            install_atomic(dest, 'first\n')
            with self.assertRaises(FileExistsError):
                install_atomic(dest, 'second\n')
            self.assertEqual(dest.read_text(encoding='utf-8'), 'first\n')
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ['notice.sh'])


@unittest.skipUnless(os.name == 'posix' and shutil.which('bash') and shutil.which('awk'),
                     'POSIX shell and pseudo-terminal required')
class InteractiveNoticeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.cid = 'a' * 64
        self.cgroup = self.root / 'cgroup' / self.cid
        self.proc = self.root / 'proc'
        self.cgroup.mkdir(parents=True)
        self.proc.mkdir()
        self.notice_file = self.root / 'notice.sh'
        self.notice_file.write_text(render_notice(self.cid, 'deepinfer-test',
                                                  cgroup_root=str(self.cgroup.parent),
                                                  proc_root=str(self.proc)), encoding='utf-8')

    def set_process(self, pid, *, state='S', namespace_pid=1):
        (self.proc / str(pid)).mkdir()
        (self.proc / str(pid) / 'status').write_text(
            f'State:\t{state} (fixture)\nNSpid:\t{pid}\t{namespace_pid}\n', encoding='utf-8')
        (self.cgroup / 'cgroup.procs').write_text(str(pid) + '\n', encoding='utf-8')

    def run_notice(self, *, ssh=True, tty=True, interactive=True, disappearing_cgroup=False):
        import pty
        import select
        import time
        env = os.environ.copy()
        env.pop('SSH_CONNECTION', None)
        if ssh:
            env['SSH_CONNECTION'] = '192.0.2.10 1234 192.0.2.11 22'
        command = ['bash', '--noprofile', '--norc'] + (['-i'] if interactive else [])
        prelude = ''
        if disappearing_cgroup:
            command += ['-e']
            bindir = self.root / 'bin'
            bindir.mkdir()
            bracket = bindir / '['
            bracket.write_text('#!/bin/sh\n/usr/bin/[ "$@"\nresult=$?\ncase "$1" in -r) rm -f -- "$2" ;; esac\nexit "$result"\n', encoding='utf-8')
            bracket.chmod(0o755)
            env['PATH'] = str(bindir) + os.pathsep + env.get('PATH', '')
            prelude = "enable -n '['; "
        command += ['-c', prelude + '. "$1"; printf "NOTICE_TEST_COMPLETE\\n"', 'bash', str(self.notice_file)]
        if not tty:
            return subprocess.run(command, env=env, capture_output=True, text=True, timeout=10, check=True).stdout
        master, slave = pty.openpty()
        process = None
        try:
            process = subprocess.Popen(command, env=env, stdin=slave, stdout=slave, stderr=slave, close_fds=True)
            os.close(slave)
            slave = None
            chunks = []
            deadline = time.monotonic() + 10
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or not select.select([master], [], [], remaining)[0]:
                    self.fail('interactive notice timed out')
                try:
                    data = os.read(master, 65536)
                except OSError:
                    break
                if not data:
                    break
                chunks.append(data)
            output = b''.join(chunks).decode('utf-8', errors='replace')
            self.assertEqual(process.wait(timeout=10), 0, output)
            return output
        finally:
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)
            if slave is not None:
                os.close(slave)
            os.close(master)

    def test_running_pinned_container_shows_notice_once(self):
        self.set_process(123)
        output = self.run_notice()
        self.assertEqual(output.count(NOTICE), 1)
        self.assertIn('deepinfer-test', output)

    def test_stopped_or_non_init_process_does_not_show_notice(self):
        self.assertNotIn(NOTICE, self.run_notice())
        self.set_process(123, namespace_pid=2)
        self.assertNotIn(NOTICE, self.run_notice())

    def test_zombie_init_does_not_show_notice(self):
        self.set_process(123, state='Z')
        self.assertNotIn(NOTICE, self.run_notice())

    def test_noninteractive_and_non_ssh_sessions_stay_quiet(self):
        self.set_process(123)
        self.assertNotIn(NOTICE, self.run_notice(tty=False))
        self.assertNotIn(NOTICE, self.run_notice(interactive=False))
        self.assertNotIn(NOTICE, self.run_notice(ssh=False))

    def test_disappearing_cgroup_does_not_terminate_login(self):
        self.set_process(123)
        output = self.run_notice(disappearing_cgroup=True)
        self.assertNotIn(NOTICE, output)
        self.assertIn('NOTICE_TEST_COMPLETE', output)


if __name__ == '__main__':
    unittest.main()
