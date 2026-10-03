"""Local web app for FloodBKK: serves the pages and runs update.py when the page's "อัปเดตข้อมูล" button is pressed.

    python scripts/serve.py              http://127.0.0.1:8765  (opens the browser)
    python scripts/serve.py --auto 60    also refresh the data by itself every 60 minutes
    python scripts/serve.py --port 9000 --no-browser

Standard library only. Listens on 127.0.0.1 (this computer only). Stop with Ctrl+C or by closing the window.
"""
import json, os, sys, threading, time, subprocess, webbrowser, argparse
from http.server import ThreadingHTTPServer, SimpleHTTPRequestHandler
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__)); ROOT = os.path.abspath(os.path.join(HERE, '..'))
STATE = dict(running=False, last=None, ok=None, log='', step='', started=None, next_auto=None)
LOCK = threading.Lock()


def run_update(hourly=False):
    """run update.py once (progress lines go to STATE['step']); returns (ok, log). Only one run at a time."""
    if not LOCK.acquire(blocking=False):
        return False, 'กำลังอัปเดตอยู่แล้ว รอสักครู่'
    STATE.update(running=True, step='เริ่มดึงข้อมูล', started=datetime.now().strftime('%H:%M:%S'))
    lines = []
    try:
        cmd = [sys.executable, '-u', os.path.join(HERE, 'update.py')] + (['--hourly'] if hourly else [])
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, encoding='utf-8', errors='replace',
                             env=dict(os.environ, PYTHONUTF8='1'))
        for line in p.stdout:
            line = line.rstrip()
            if line:
                lines.append(line); STATE['step'] = line[:160]; print('  ', line)
        p.wait(timeout=3600)
        ok = p.returncode == 0
    except Exception as e:
        ok = False; lines.append(str(e))
    log = '\n'.join(lines)[-4000:]
    STATE.update(running=False, last=datetime.now().strftime('%Y-%m-%d %H:%M:%S'), ok=ok, log=log, step='เสร็จแล้ว' if ok else 'ไม่สำเร็จ')
    LOCK.release()
    print(f'[{STATE["last"]}] update {"ok" if ok else "FAILED"}')
    return ok, log


def data_time():
    try:
        d = json.load(open(os.path.join(ROOT, 'data', 'model_data.json'), encoding='utf-8'))
        return d.get('fetched')
    except Exception:
        return None


class Handler(SimpleHTTPRequestHandler):
    def __init__(self, *a, **k):
        super().__init__(*a, directory=ROOT, **k)

    def log_message(self, fmt, *args):  # quiet: only API calls
        if args and '/api/' in str(args[0]): super().log_message(fmt, *args)

    def end_headers(self):
        self.send_header('Cache-Control', 'no-store')   # always serve the freshly rebuilt pages
        # pages opened straight from the folder (file://, Origin "null") may call the API too
        if self.path.startswith('/api/'):
            o = self.headers.get('Origin') or ''
            if o == 'null' or o.startswith(('http://127.0.0.1', 'http://localhost')):
                self.send_header('Access-Control-Allow-Origin', o)
        super().end_headers()

    def _json(self, code, obj):
        b = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code); self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(b))); self.end_headers(); self.wfile.write(b)

    def do_GET(self):
        if self.path in ('/', '/index.html'):
            self.send_response(302); self.send_header('Location', '/bkk_drainage.html'); self.end_headers(); return
        if self.path.startswith('/api/status'):
            return self._json(200, dict(STATE, app=True, data_time=data_time()))
        return super().do_GET()

    def do_POST(self):
        # accept only requests coming from our own pages (blocks other websites from triggering updates)
        origin = self.headers.get('Origin') or ''
        host = self.headers.get('Host') or ''
        if origin and origin != 'null' and origin.split('://', 1)[-1] != host:
            return self._json(403, dict(ok=False, log='forbidden origin'))
        if self.path.startswith('/api/update'):
            if STATE['running']:
                return self._json(200, dict(ok=True, started=False, running=True, step=STATE['step']))
            threading.Thread(target=run_update, kwargs=dict(hourly='hourly=1' in self.path), daemon=True).start()
            return self._json(202, dict(ok=True, started=True, running=True))
        self.send_error(404)


def auto_loop(minutes):
    while True:
        STATE['next_auto'] = datetime.fromtimestamp(time.time() + minutes * 60).strftime('%H:%M')
        time.sleep(minutes * 60)
        run_update()


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8765)
    ap.add_argument('--auto', type=int, default=0, help='minutes between automatic updates (0 = off)')
    ap.add_argument('--no-browser', action='store_true')
    a = ap.parse_args()
    srv = ThreadingHTTPServer(('127.0.0.1', a.port), Handler)
    url = f'http://127.0.0.1:{a.port}/'
    print(f'FloodBKK เปิดที่ {url}  (ปิดหน้าต่างนี้หรือกด Ctrl+C เพื่อหยุด)')
    if a.auto > 0:
        threading.Thread(target=auto_loop, args=(a.auto,), daemon=True).start()
        print(f'อัปเดตอัตโนมัติทุก {a.auto} นาที')
    if not a.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
