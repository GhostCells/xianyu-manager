"""Privileged preflight ONLY inside `unshare --net`; never run on host netns.

Synthetic HTTP endpoints have no uplink. This is not runtime-UID acceptance.
"""
import json
import os
from pathlib import Path
import subprocess as sp
import sys
import time

assert os.stat('/proc/self/ns/net').st_ino != os.stat('/proc/1/ns/net').st_ino
root = Path(sys.argv[1])
children = []


def run(*args, input=None):
    result = sp.run(args, input=input, text=True, capture_output=True)
    if result.returncode:
        raise RuntimeError(f'{args}: {result.stderr}')
    return result.stdout


try:
    for name, peer, addr, gateway in [
        ('xmg-host', 'b', '10.203.0.2/30', '10.203.0.1'),
        ('tailscale0', 'e', '8.8.8.8/24', '8.8.8.1'),
        ('other-host', 'n', '10.204.0.2/30', '10.204.0.1'),
    ]:
        p = sp.Popen(['unshare', '--net', 'sleep', '120'])
        children.append(p)
        for _ in range(100):
            assert p.poll() is None
            if os.stat(f'/proc/{p.pid}/ns/net').st_ino != os.stat('/proc/self/ns/net').st_ino:
                break
            time.sleep(.02)
        else:
            raise RuntimeError('CHILD_NAMESPACE_NOT_READY')
        run('ip', 'link', 'add', name, 'type', 'veth', 'peer', 'name', peer)
        run('ip', 'link', 'set', 'dev', peer, 'netns', str(p.pid))
        run('ip', 'addr', 'add', gateway + ('/24' if name == 'tailscale0' else '/30'), 'dev', name)
        run('ip', 'link', 'set', 'dev', name, 'up')
        prefix = ['nsenter', '-t', str(p.pid), '-n']
        run(*prefix, 'ip', 'addr', 'add', addr, 'dev', peer)
        run(*prefix, 'ip', 'link', 'set', 'dev', peer, 'up')
        run(*prefix, 'ip', 'link', 'set', 'lo', 'up')
        run(*prefix, 'ip', 'route', 'add', 'default', 'via', gateway)
    for file in ['host-forward.nft', 'guard.nft']:
        run('nft', '-f', str(Path(__file__).parent / file))
    run('sysctl', '-qw', 'net.ipv4.ip_forward=1')
    run('nft', '-f', '-', input='table ip synthetic_accept {\n chain forward {\n type filter hook forward priority 0; policy accept;\n counter accept\n }\n}\n')
    server = sp.Popen(['nsenter', '-t', str(children[1].pid), '-n', 'python3', '-m', 'http.server',
                       '80', '--bind', '8.8.8.8', '--directory', '/nonexistent'],
                      stdout=sp.DEVNULL, stderr=sp.DEVNULL)
    time.sleep(.3)

    def probe(p):
        return sp.run(['nsenter', '-t', str(p.pid), '-n', 'curl', '--noproxy', '*', '-s',
                       '--max-time', '1', 'http://8.8.8.8/'],
                      stdout=sp.DEVNULL, stderr=sp.DEVNULL).returncode == 0

    results = {'empty_lease_blocks_business': not probe(children[0])}
    run('nft', 'add', 'element', 'inet', 'xianyu_guard', 'lease', '{ 10.203.0.2 timeout 3s }')
    results['exact_business_path_allowed'] = probe(children[0])
    results['nonbusiness_blocked_despite_later_accept'] = not probe(children[2])
    time.sleep(3)
    results['expired_lease_blocks_despite_later_accept'] = not probe(children[0])
    (root / 'isolated-forward-results.json').write_text(json.dumps(results, indent=2))
    (root / 'isolated-forward-counters.txt').write_text(run('nft', 'list', 'ruleset'))
    print(json.dumps(results))
    assert all(results.values())
finally:
    if 'server' in locals():
        server.terminate()
        server.wait()
    for p in children:
        p.terminate()
        p.wait()
