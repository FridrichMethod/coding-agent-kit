"""Egress attempt client run inside (or outside, as control) the NM profile.

Usage: python3 -I -B client.py SPEC_JSON
SPEC: {"attempts": [...], "detach": OUT_PATH|null, "inherit_fds": [..]}
Each attempt: {"name", "kind": tcp4|tcp6|udp4|udp6|unix|unix_dgram|dns|fdwrite|mach|
icmp4|icmp6|unix_alias|trust_settings, "host", "port", "path", "fd", "service", "link",
"target", "domain"};
unix_self and unix_dgram_self bind and use their own socket inside "path";
unix_alias creates a symbolic ("link": "sym") or hard ("hard") link at "path"
naming the socket "target" and connects through it. icmp4/icmp6 send one
unprivileged datagram-socket echo request. trust_settings counts the
certificates SecTrustSettingsCopyCertificates returns for one trust-settings
domain ("admin" or "system"; the user domain is never queried). Results are JSON on stdout, or in
OUT_PATH written by a double-forked, setsid grandchild when "detach" is set.
Only loopback/fixture endpoints are ever named by mac_probe.py.
"""
import ctypes
import json
import os
import socket
import struct
import sys
import time


def mach_lookup(service):
    """bootstrap_look_up only: no message is sent to the service."""
    lib = ctypes.CDLL('/usr/lib/libSystem.B.dylib', use_errno=True)
    bootstrap = ctypes.c_uint.in_dll(lib, 'bootstrap_port')
    port = ctypes.c_uint(0)
    lib.bootstrap_look_up.argtypes = [ctypes.c_uint, ctypes.c_char_p, ctypes.POINTER(ctypes.c_uint)]
    code = lib.bootstrap_look_up(bootstrap.value, service.encode(), ctypes.byref(port))
    if code == 0 and port.value:
        task = ctypes.c_uint.in_dll(lib, 'mach_task_self_')
        lib.mach_port_deallocate(task.value, port.value)
    return 'found' if code == 0 else 'kern_return:%d' % code


def icmp_checksum(data):
    """RFC 1071 ones-complement checksum of an ICMPv4 message."""
    if len(data) % 2:
        data += b'\0'
    total = sum(struct.unpack('!%dH' % (len(data) // 2), data))
    while total >> 16:
        total = (total & 0xffff) + (total >> 16)
    return ~total & 0xffff


def icmp_echo(v6, ident, payload=b'NM-ICMP-PROBE'):
    """One echo request; the kernel fills the ICMPv6 checksum itself."""
    kind = 128 if v6 else 8
    header = struct.pack('!BBHHH', kind, 0, 0, ident & 0xffff, 1)
    checksum = 0 if v6 else icmp_checksum(header + payload)
    return struct.pack('!BBHHH', kind, 0, checksum, ident & 0xffff, 1) + payload


TRUST_DOMAINS = {'admin': 1, 'system': 2}  # kSecTrustSettingsDomainAdmin/System


def trust_settings(domain):
    """Root loading as rustls-native-certs does it on macOS; counts only."""
    sec = ctypes.CDLL('/System/Library/Frameworks/Security.framework/Security')
    cf = ctypes.CDLL('/System/Library/Frameworks/CoreFoundation.framework/CoreFoundation')
    sec.SecTrustSettingsCopyCertificates.argtypes = [ctypes.c_uint32, ctypes.POINTER(ctypes.c_void_p)]
    sec.SecTrustSettingsCopyCertificates.restype = ctypes.c_int32
    cf.CFArrayGetCount.argtypes = [ctypes.c_void_p]
    cf.CFArrayGetCount.restype = ctypes.c_long
    cf.CFRelease.argtypes = [ctypes.c_void_p]
    array = ctypes.c_void_p()
    status = sec.SecTrustSettingsCopyCertificates(TRUST_DOMAINS[domain], ctypes.byref(array))
    count = cf.CFArrayGetCount(array) if status == 0 and array.value else 0
    if array.value:
        cf.CFRelease(array)
    return 'status:%d:count:%d' % (status, count)


def unix_alias(item):
    link, target = item['path'], item['target']
    try:
        os.unlink(link)
    except FileNotFoundError:
        pass
    try:
        (os.symlink if item['link'] == 'sym' else os.link)(target, link)
    except OSError as error:
        return 'link_error:%s:%s' % (type(error).__name__, error.errno)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(3)
    try:
        sock.connect(link)
        sock.sendall(b'NM-ALIAS-PROBE\n')
        return 'connected'
    finally:
        sock.close()
        os.unlink(link)


def attempt(item):
    kind = item['kind']
    try:
        if kind == 'unix_alias':
            return unix_alias(item)
        if kind == 'trust_settings':
            return trust_settings(item['domain'])
        if kind in ('icmp4', 'icmp6'):
            v6 = kind == 'icmp6'
            sock = socket.socket(socket.AF_INET6 if v6 else socket.AF_INET, socket.SOCK_DGRAM,
                                 socket.IPPROTO_ICMPV6 if v6 else socket.IPPROTO_ICMP)
            sock.settimeout(2)
            try:
                sock.sendto(icmp_echo(v6, os.getpid()), (item['host'], 0))
                try:
                    sock.recv(512)
                    return 'reply'
                except TimeoutError:
                    return 'sent_no_reply'
            finally:
                sock.close()
        if kind == 'mach':
            return mach_lookup(item['service'])
        if kind == 'dns':
            infos = socket.getaddrinfo(item['host'], 80, proto=socket.IPPROTO_TCP)
            return 'resolved:%d' % len(infos)
        if kind == 'unix_self':
            path = os.path.join(item['path'], 'self-%d.sock' % os.getpid())
            server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            try:
                server.bind(path)
                server.listen(1)
                peer = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                peer.settimeout(3)
                peer.connect(path)
                peer.close()
                return 'connected'
            finally:
                server.close()
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if kind == 'unix_dgram_self':
            path = os.path.join(item['path'], 'dgram-%d.sock' % os.getpid())
            server = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                server.bind(path)
                peer = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
                try:
                    peer.sendto(b'NM-DGRAM-SELF', path)
                finally:
                    peer.close()
                server.settimeout(3)
                return 'received' if server.recv(64) == b'NM-DGRAM-SELF' else 'mismatch'
            finally:
                server.close()
                try:
                    os.unlink(path)
                except OSError:
                    pass
        if kind == 'unix_dgram':
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
            try:
                sock.sendto(b'NM-UNIX-DGRAM', item['path'])
                return 'sent'
            finally:
                sock.close()
        if kind == 'fdwrite':
            os.write(item['fd'], b'NM-INHERITED-WRITE\n')
            return 'written'
        if kind == 'unix':
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            target = item['path']
        else:
            family = socket.AF_INET6 if kind.endswith('6') else socket.AF_INET
            stype = socket.SOCK_DGRAM if kind.startswith('udp') else socket.SOCK_STREAM
            sock = socket.socket(family, stype)
            target = (item['host'], item['port'])
        sock.settimeout(3)
        try:
            if kind.startswith('udp'):
                sock.sendto(b'NM-UDP-PROBE', target)
                return 'sent'
            sock.connect(target)
            sock.sendall(b'NM-TCP-PROBE\n')
            return 'connected'
        finally:
            sock.close()
    except OSError as error:
        return 'error:%s:%s' % (type(error).__name__, error.errno)


def run(spec):
    out = {'pid': os.getpid(), 'ppid': os.getppid(), 'sid': os.getsid(0), 'results': {}}
    for item in spec.get('attempts', ()):
        out['results'][item['name']] = attempt(item)
    return out


def main():
    spec = json.loads(sys.argv[1])
    target = spec.get('detach')
    if not target:
        print(json.dumps(run(spec)), flush=True)
        return
    if os.fork():
        os._exit(0)  # the direct child exits so the grandchild is reparented
    os.setsid()
    if os.fork():
        os._exit(0)
    for fd in (0, 1, 2):
        try:
            os.close(fd)
        except OSError:
            pass
    deadline = time.monotonic() + 5
    while os.getppid() != 1 and time.monotonic() < deadline:
        time.sleep(.05)
    result = run(spec)
    result['reparented_to_launchd'] = os.getppid() == 1
    temp = target + '.tmp'
    with open(temp, 'w') as handle:
        json.dump(result, handle)
    os.rename(temp, target)


if __name__ == '__main__':
    main()
