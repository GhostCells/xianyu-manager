"""Mac-only loopback UI for a fixed SSH view tunnel; never runs business services."""
import argparse
import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.request import ProxyHandler, build_opener
import webbrowser

HOST = '127.0.0.1'
PORT = 18768
VIEW = 'http://127.0.0.1:18767/vnc.html'
URL = f'http://{HOST}:{PORT}'
IDENTITY = 'xianyu-local-viewer-v1'
TOKEN = secrets.token_urlsafe(32)
SSH = ['/usr/bin/ssh', '-N', '-T', '-o', 'BatchMode=yes', '-o', 'StrictHostKeyChecking=yes',
       '-o', 'ConnectTimeout=10', '-o', 'ConnectionAttempts=1', '-o', 'ExitOnForwardFailure=yes',
       '-o', 'ServerAliveInterval=30', '-o', 'ServerAliveCountMax=3',
       '-L', '127.0.0.1:18767:127.0.0.1:6080', 'xianyu-cloud']


def page_ready():
    try:
        with build_opener(ProxyHandler({})).open(VIEW, timeout=2) as response:
            body = response.read(65536)
            return response.status == 200 and b'noVNC' in body
    except Exception:
        return False


def port_busy():
    with socket.socket() as client:
        client.settimeout(0.2)
        return client.connect_ex((HOST, 18767)) == 0


class Tunnel:
    def __init__(self, demo=False):
        self.lock = threading.Lock()
        self.process = None
        self.connecting = False
        self.message = '点击连接，建立本机到云端的图形隧道。'
        self.demo = demo
        self.demo_ready = False

    def status(self):
        ready = self.demo_ready if self.demo else page_ready()
        with self.lock:
            return {'identity': IDENTITY, 'ready': ready, 'connecting': self.connecting,
                    'message': self.message, 'demo': self.demo, 'url': VIEW}

    def connect(self):
        with self.lock:
            if self.connecting:
                return
            self.connecting = True
            self.message = '正在连接，最多约30秒，请勿重复点击。'
        threading.Thread(target=self._connect, daemon=True).start()

    def _connect(self):
        try:
            if self.demo:
                self.demo_ready = True
                self.message = '演示连接成功，未执行SSH。'
                return
            if page_ready():
                self.message = '已复用现有连接；没有新建隧道。'
                return
            # Never terminate another user's SSH process or a cloud service.
            if self.process and self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=5)
            if port_busy():
                self.message = '18767端口已被其他进程占用，但noVNC未响应；未关闭该进程。'
                return
            self.process = subprocess.Popen(SSH, stdin=subprocess.DEVNULL,
                                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            deadline = time.monotonic() + 30
            while time.monotonic() < deadline:
                if page_ready():
                    self.message = 'noVNC页面已连通。请打开画面并本人输入noVNC密码。'
                    return
                if self.process.poll() is not None:
                    self.message = 'SSH连接失败。请检查Tailscale；首次SSH授权需在终端完成。'
                    return
                time.sleep(0.5)
            if self.process.poll() is None:
                self.process.terminate()
                self.process.wait(timeout=5)
            self.message = '连接超时，已停止本次尝试。可稍后重试；未重启云端服务。'
        except Exception:
            self.message = '连接未完成。请检查本机SSH配置和Tailscale后重试。'
        finally:
            with self.lock:
                self.connecting = False


HTML = '''<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>闲鱼云端画面 · 连接助手</title>
<style>
*{box-sizing:border-box}body{margin:0;background:#f2f6f4;color:#18352e;font:16px/1.65 system-ui,sans-serif}
main{max-width:650px;margin:9vh auto;padding:32px;background:white;border:1px solid #dce8e2;border-radius:24px}
.eyebrow{color:#087e60;font-size:13px;letter-spacing:2px}h1{font-size:30px;margin:10px 0}p{color:#60736c}
.status{padding:20px;background:#f2f6f4;border-radius:14px;margin:24px 0}.status strong{font-size:19px}
#detail{margin-bottom:0}.actions{display:flex;gap:12px;flex-wrap:wrap}button,a{font:inherit;border-radius:12px;padding:13px 22px}
button{background:#087e60;color:white;border:0;cursor:pointer}button:disabled{opacity:.5;cursor:wait}
a{color:#087e60;border:1px solid #a3c7b9;text-decoration:none}a[aria-disabled=true]{pointer-events:none;opacity:.4}
small{display:block;color:#66776f;margin-top:22px}#demo{color:#9a5b00} @media(max-width:680px){main{margin:24px 14px;padding:24px}h1{font-size:26px}}
</style><main><div class="eyebrow">ACCOUNT 2 · 仅远程查看</div><h1>连接闲鱼云端画面</h1>
<p>不必再复制SSH命令。先连接，再打开原来的Chrome画面。</p><div id="demo"></div>
<section class="status" role="status" aria-live="polite"><strong id="status">正在检查连接…</strong>
<p id="detail">这里只检查图形入口，不代表闲鱼业务已登录。</p></section>
<div class="actions"><button id="connect">连接云端画面</button>
<a id="open" href="http://127.0.0.1:18767/vnc.html" target="_blank" rel="noopener noreferrer" aria-disabled="true">打开noVNC ↗</a></div>
<small>如提示密码，请输入原noVNC密码，不是闲鱼密码或邮箱授权码。<br>
仅恢复图形隧道；不重启Chrome、不清Cookie、不修改发货配置。关闭此网页不会关闭云端业务。</small>
<details><summary>无法连接时怎么办？</summary><p>确认Mac已联网、Tailscale已连接。首次SSH连接或密钥需要解锁时，在终端运行 <code>ssh xianyu-cloud</code>，完成后退出再重试。不要在此网页输入任何密码。</p></details>
</main><script>
const button=document.querySelector('#connect'),link=document.querySelector('#open');
async function update(){try{const r=await fetch('/status',{cache:'no-store'});if(!r.ok)throw Error();const s=await r.json();
document.querySelector('#status').textContent=s.connecting?'连接中…':s.ready?'图形入口已连通':'图形入口未连接';
document.querySelector('#detail').textContent=s.message;button.disabled=s.connecting;button.textContent=s.ready?'检查 / 重新连接':'连接云端画面';
link.setAttribute('aria-disabled',String(!s.ready));link.tabIndex=s.ready?0:-1;
document.querySelector('#demo').textContent=s.demo?'演示模式：不连接服务器':'';
}catch(e){document.querySelector('#status').textContent='本机助手未响应';document.querySelector('#detail').textContent='请重新双击启动脚本。';button.disabled=true;link.setAttribute('aria-disabled','true');}}
button.addEventListener('click',async()=>{button.disabled=true;document.querySelector('#status').textContent='连接中…';
try{const r=await fetch('/connect',{method:'POST',headers:{'X-Viewer-Token':'__TOKEN__'}});if(!r.ok)throw Error();}
catch(e){document.querySelector('#detail').textContent='启动请求失败，请重新打开本机助手。';}await update();});
async function poll(){await update();setTimeout(poll,3000)}poll();
</script></html>'''


def handler(tunnel):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args): pass

        def reply(self, code, data, content_type='application/json'):
            data = data if isinstance(data, bytes) else json.dumps(data).encode()
            self.send_response(code)
            self.send_header('Content-Type', content_type)
            self.send_header('Content-Length', str(len(data)))
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Content-Security-Policy', "frame-ancestors 'self' http://127.0.0.1:18766")
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get('Host') == f'{HOST}:{PORT}'

        def do_GET(self):
            if not self.valid_host():
                return self.reply(403, {})
            if self.path == '/':
                return self.reply(200, HTML.replace('__TOKEN__', TOKEN).encode(), 'text/html; charset=utf-8')
            if self.path == '/status':
                return self.reply(200, tunnel.status())
            self.reply(404, {})

        def do_POST(self):
            if (not self.valid_host() or self.headers.get('Origin') != URL
                    or not secrets.compare_digest(self.headers.get('X-Viewer-Token', ''), TOKEN)):
                return self.reply(403, {})
            if self.path != '/connect':
                return self.reply(404, {})
            tunnel.connect()
            self.reply(202, {'started': True})
    return Handler


def launch():
    opener = build_opener(ProxyHandler({}))
    def existing():
        try:
            with opener.open(URL + '/status', timeout=3) as r:
                return json.load(r).get('identity') == IDENTITY
        except Exception:
            return False
    if not existing():
        subprocess.Popen([sys.executable, str(Path(__file__).resolve())], start_new_session=True,
                         stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        for _ in range(20):
            if existing(): break
            time.sleep(.2)
        else:
            raise SystemExit('本机助手启动失败，18768端口可能已被占用。')
    webbrowser.open(URL)


if __name__ == '__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--launch', action='store_true')
    parser.add_argument('--demo', action='store_true')
    args = parser.parse_args()
    if args.launch:
        launch()
    else:
        ThreadingHTTPServer((HOST, PORT), handler(Tunnel(demo=args.demo))).serve_forever()
