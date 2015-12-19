#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import time
import json
import socket
import shutil
import logging
import tempfile
import threading
import subprocess

tg_server_pub = '''-----BEGIN RSA PUBLIC KEY-----
MIIBCgKCAQEAwVACPi9w23mF3tBkdZz+zwrzKOaaQdr01vAbU4E1pvkfj4sqDsm6
lyDONS789sVoD/xCS9Y0hkkC3gtL1tSfTlgCMOOul9lcixlEKzwKENj1Yz/s7daS
an9tqw3bfUV/nqgbhGX81v/+7RFAEd+RwFnK7a+XYl9sluzHRyVVaTTveB2GazTw
Efzk2DWgkBluml8OREmvfraX3bkHZJTKX4EQSjBbbdJ2ZXIsRrYOXfaA+xayEGB+
8hdlLmAjbCVfaigxX0CDqWeR1yFL9kwd9P0NsZRPsmoqVwMbMu7mStFai6aIhc3n
Slv8kg9qv1m6XHVQY3PnEw+QQtqSIXklHwIDAQAB
-----END RSA PUBLIC KEY-----
'''

logger = logging.getLogger('tgcli')
logger.setLevel(logging.INFO)
do_nothing = lambda *args, **kwargs: None

class TelegramCliInterface:
    def __init__(self, cmd, extra_args=(), run=True):
        self.cmd = cmd
        self.extra_args = tuple(extra_args)
        self.proc = None
        self.sock = None
        self.ready = threading.Event()
        self.closed = False
        self.thread = None
        self.tmpdir = tempfile.mkdtemp()
        # Event callbacks
        # `on_info`, `on_json` and `on_text` are for stdout
        self.on_info = logger.info
        self.on_json = logger.debug
        self.on_text = do_nothing
        self.on_start = lambda: logger.info('Telegram-cli started.')
        self.on_exit = lambda: logger.warning('Telegram-cli died.')
        if run:
            self.run()

    def _get_pubkey(self):
        tgdir = os.path.abspath(os.path.join(os.path.dirname(os.path.realpath(self.cmd)), '..'))
        paths = [
            os.path.join(tgdir, 'tg-server.pub'),
            os.path.join(tgdir, 'server.pub'),
            '/etc/telegram-cli/server.pub',
            '/usr/local/etc/telegram-cli/server.pub',
            os.path.join(self.tmpdir, 'tg-server.pub')
        ]
        for path in paths:
            if os.path.isfile(path):
                return path
        else:
            with open(path, 'w') as f:
                f.write(tg_server_pub)
            return path

    def checkproc(self):
        if self.closed or self.proc and self.proc.poll() is None:
            return self.proc
        sockfile = os.path.join(self.tmpdir, 'tgcli.sock')
        self.proc = subprocess.Popen((self.cmd, '-k', self._get_pubkey(), '--json', '-R', '-C', '-S', sockfile) + self.extra_args, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        while not os.path.exists(sockfile):
            time.sleep(0.5)
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.connect(sockfile)
        return self.proc

    def _run_cli(self):
        while not self.closed:
            self.checkproc()
            self.ready.set()
            self.on_start()
            try:
                while self.ready.is_set():
                    out = self.proc.stdout.readline().decode('utf-8')
                    if out:
                        self.on_text(out)
                        if out[0] in '[{':
                            try:
                                self.on_json(json.loads(out.strip()))
                            except ValueError:
                                self.on_info(out.strip())
                        else:
                            self.on_info(out.strip())
                    else:
                        break
            except BrokenPipeError:
                pass
            finally:
                if self.proc and self.proc.poll() is not None:
                    self.proc.terminate()
            self.ready.clear()
            self.on_exit()

    def run(self):
        self.thread = threading.Thread(target=self._run_cli)
        self.thread.daemon = True
        self.thread.start()

    def close(self):
        if self.closed:
            return
        self.ready.clear()
        self.closed = True
        try:
            self.proc.wait(2)
        except subprocess.TimeoutExpired:
            self.proc.kill()
        if self.thread:
            self.thread.join(2)
        if self.tmpdir:
            shutil.rmtree(self.tmpdir, True)
            self.tmpdir = None

    def __enter__(self):
        if not self.thread:
            self.run()
        self.ready.wait()
        return self

    def __exit__(self, exc_type, exc_value, traceback):
        self.close()

    def __del__(self):
        self.close()

    def send_command(self, cmd, timeout=180, resync=True):
        '''
        Send a command to tg-cli.
        use `resync` for consuming text since last timeout.
        '''
        self.checkproc()
        self.sock.settimeout(timeout)
        self.sock.sendall(cmd.encode('utf-8') + b'\n')
        data = self.sock.recv(1024)
        lines = data.split(b'\n', 1)
        if not lines[0].startswith(b'ANSWER '):
            if resync:
                while self.ready.is_set() and not lines[0].startswith(b'ANSWER '):
                    data = self.sock.recv(1024)
                    lines = data.split(b'\n', 1)
            else:
                raise ValueError('Bad reply from telegram-cli: %s' % lines[0][:20])
        size = int(lines[0][7:].decode('ascii'))
        reply = lines[1] if len(lines) == 2 else b''
        while self.ready.is_set() and len(reply) < size:
            reply += self.sock.recv(1024)
        ret = reply.decode('utf-8')
        try:
            return json.loads(ret)
        except ValueError:
            return ret

    def __getattr__(self, name):
        '''
        Convenience command calling: cmd_*(*args, **kwargs)
        `args` are for the tg-cli command
        `kwargs` are for `send_command`
        '''
        if name.startswith('cmd_'):
            fn = lambda *args, **kwargs: self.send_command(' '.join(map(str, (name[4:],) + args)), **kwargs)
            return fn
        else:
            raise AttributeError

if __name__ == "__main__":
    import sys
    logging.basicConfig(stream=sys.stderr, format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)
    with TelegramCliInterface(sys.argv[1]) as tgcli:
        for ln in sys.stdin:
            print(tgcli.send_command(ln.strip()))
