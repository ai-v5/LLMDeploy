"""Inventory device holders while pinning each /proc instance before scanning it."""
import errno
import os
import re
import stat
import time


class InventoryUnknown(RuntimeError):
    pass


def _read_at(directory, name):
    fd = os.open(name, os.O_RDONLY | os.O_CLOEXEC, dir_fd=directory)
    with os.fdopen(fd, 'rb') as stream:
        return stream.read()


def _start_ticks(raw):
    close = raw.rfind(b')')
    if close < 0:
        raise ValueError('invalid process stat')
    fields = raw[close + 2:].split()
    return int(fields[19])


def _map_holds(raw, targets, procfd=None, devices_by_number=None):
    """Match device mappings by inode or by the mapped character device number."""
    for line in raw.splitlines():
        fields = line.split(None, 5)
        if len(fields) < 5 or b':' not in fields[3]:
            raise InventoryUnknown('PROCESS_MAPS_INVALID')
        try:
            major, minor = (int(part, 16) for part in fields[3].split(b':', 1))
            inode = int(fields[4])
            start, end = (int(part, 16) for part in fields[0].split(b'-', 1))
        except ValueError as error:
            raise InventoryUnknown('PROCESS_MAPS_INVALID') from error
        if start >= end or inode < 0:
            raise InventoryUnknown('PROCESS_MAPS_INVALID')
        if (major, minor, inode) in targets:
            return True
        if inode and procfd is not None and devices_by_number is not None:
            # map_files names use canonical hex, even when maps pads addresses.
            region = 'map_files/' + format(start, 'x') + '-' + format(end, 'x')
            try:
                info = os.stat(region, dir_fd=procfd)
            except OSError as error:
                # The process may have exited after its mapping was observed;
                # its backing device was not established before it disappeared.
                raise InventoryUnknown('PROCESS_MAPPING_UNKNOWN') from error
            if (os.major(info.st_dev), os.minor(info.st_dev), info.st_ino) != (major, minor, inode):
                raise InventoryUnknown('PROCESS_MAPPING_IDENTITY_UNKNOWN')
            if stat.S_ISCHR(info.st_mode) and info.st_rdev in devices_by_number:
                return True
    return False


def _own_container(cgroup, container_id):
    if not isinstance(container_id, str) or not re.fullmatch('[0-9a-f]{64}', container_id):
        return False
    cid = container_id.encode()
    for line in cgroup.splitlines():
        parts = line.split(b':', 2)
        if len(parts) != 3:
            continue
        components = parts[2].split(b'/')
        for index, component in enumerate(components):
            if component == b'docker-' + cid + b'.scope':
                return True
            if component == cid and index > 0 and components[index - 1] == b'docker':
                return True
    return False


def _fd_table_holds(procfd, devices_by_number):
    fddir = os.open('fd', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=procfd)
    try:
        for fd_name in os.listdir(fddir):
            try:
                info = os.stat(fd_name, dir_fd=fddir)
            except FileNotFoundError:
                continue
            if stat.S_ISCHR(info.st_mode) and info.st_rdev in devices_by_number:
                return True
        return False
    finally:
        os.close(fddir)


def _thread_fd_holds(procfd, pid, devices_by_number):
    """Inspect each live thread: Linux permits a thread to unshare its fd table."""
    taskdir = os.open('task', os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=procfd)
    try:
        for name in os.listdir(taskdir):
            if not name.isdigit():
                raise InventoryUnknown('THREAD_IDENTITY_UNKNOWN')
            if int(name) == pid:
                continue  # The leader's fd table was already scanned.
            try:
                threadfd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC, dir_fd=taskdir)
            except FileNotFoundError:
                continue  # The thread exited before it was pinned.
            try:
                matched = False
                try:
                    ticks = _start_ticks(_read_at(threadfd, 'stat'))
                    matched = _fd_table_holds(threadfd, devices_by_number)
                    if _start_ticks(_read_at(threadfd, 'stat')) != ticks:
                        raise InventoryUnknown('THREAD_IDENTITY_CHANGED')
                except FileNotFoundError:
                    if matched:
                        raise InventoryUnknown('MATCHED_THREAD_DISAPPEARED')
                    try:
                        _read_at(threadfd, 'stat')
                    except FileNotFoundError:
                        continue  # The pinned thread itself has exited.
                    raise InventoryUnknown('THREAD_FD_INVENTORY_UNKNOWN')
                if matched:
                    return True
            finally:
                os.close(threadfd)
        return False
    finally:
        os.close(taskdir)


def inventory_device_holders(devices, container_id, watcher=None, precheck=None, *, skip_owned=False):
    """Return pinned holder identities; incomplete permission/I/O checks fail closed.

    A process or individual descriptor that disappears before its device is
    observed is absent from this sample, as with any polling inventory.
    ``skip_owned`` omits the exact Docker task's processes entirely.  Callers
    must first pin its container ID and verify the same running instance; it
    must stay false when checking idle or preparing to start a stopped task.
    """
    if not devices:
        raise InventoryUnknown('DEVICE_LIST_UNKNOWN')
    scan_started = time.monotonic()
    try:
        device_stats = [os.stat(path) for path in devices]
        if not all(stat.S_ISCHR(row.st_mode) for row in device_stats):
            raise InventoryUnknown('DEVICE_LAYOUT_UNKNOWN')
        devices_by_number = {row.st_rdev for row in device_stats}
        maps_by_inode = {(os.major(row.st_dev), os.minor(row.st_dev), row.st_ino) for row in device_stats}
        entries = os.scandir('/proc')
    except OSError as error:
        raise InventoryUnknown('DEVICE_INVENTORY_UNKNOWN') from error
    holders = []
    with entries:
        for entry in entries:
            if not entry.name.isdigit():
                continue
            pid = int(entry.name)
            procfd = None
            try:
                procfd = os.open(entry.path, os.O_RDONLY | os.O_DIRECTORY | os.O_CLOEXEC)
                ticks = _start_ticks(_read_at(procfd, 'stat'))
                name = _read_at(procfd, 'comm').strip()
                cgroup = _read_at(procfd, 'cgroup')
                if skip_owned and _own_container(cgroup, container_id):
                    if _start_ticks(_read_at(procfd, 'stat')) != ticks or _read_at(procfd, 'cgroup') != cgroup:
                        raise InventoryUnknown('OWN_PROCESS_IDENTITY_CHANGED')
                    continue
                if watcher is not None and name in (b'npu-smi', b'hccn_tool'):
                    if not watcher.observe(pid) and not watcher.prove_live(pid, ticks, time.monotonic()):
                        # A short-lived collector's command may not be readable yet.
                        # The second observation must still precede the device scan;
                        # callers must prove the identity again after inventory.
                        time.sleep(.002)
                        watcher.observe(pid)
                preverified = precheck(pid, ticks, name) is True if precheck is not None else False
                # This barrier is specific to this PID and follows observation;
                # scan_started still identifies the beginning of the inventory.
                holder_scan_started = time.monotonic()
                matched = _fd_table_holds(procfd, devices_by_number)
                if not matched:
                    matched = _thread_fd_holds(procfd, pid, devices_by_number)
                if not matched:
                    matched = _map_holds(_read_at(procfd, 'maps'), maps_by_inode, procfd, devices_by_number)
                if matched:
                    holders.append({'pid': pid, 'start_ticks': ticks, 'scan_started': scan_started,
                                    'holder_scan_started': holder_scan_started,
                                    'own_container': _own_container(cgroup, container_id),
                                    'preverified': preverified, 'process_name': name.decode('utf8', errors='replace')})
            except OSError as error:
                if error.errno in (errno.ENOENT, errno.ESRCH) and procfd is not None:
                    try:
                        _read_at(procfd, 'stat')
                    except OSError as check:
                        if check.errno in (errno.ENOENT, errno.ESRCH):
                            continue
                        raise InventoryUnknown('PROCESS_INVENTORY_UNKNOWN') from check
                    raise InventoryUnknown('PROCESS_INVENTORY_UNKNOWN') from error
                if error.errno not in (errno.ENOENT, errno.ESRCH):
                    raise InventoryUnknown('PROCESS_INVENTORY_UNKNOWN') from error
            except (ValueError, IndexError, UnicodeError) as error:
                raise InventoryUnknown('PROCESS_IDENTITY_UNKNOWN') from error
            finally:
                if procfd is not None:
                    os.close(procfd)
    return holders
