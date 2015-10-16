#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import time
import queue
import random
import socket
import sqlite3
import logging
import tempfile
import argparse
import threading
import functools
import subprocess
import collections


re_msglist = re.compile(r'^\[.*\]$')
re_onemsg = re.compile(r'^\{.+\}$')

logging.basicConfig(stream=sys.stdout, format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)

socket.setdefaulttimeout(60)

class LRUCache:

    def __init__(self, maxlen):
        self.capacity = maxlen
        self.cache = collections.OrderedDict()

    def __getitem__(self, key):
        value = self.cache.pop(key)
        self.cache[key] = value
        return value

    def get(self, key):
        try:
            value = self.cache.pop(key)
            self.cache[key] = value
            return value
        except KeyError:
            return None

    def __setitem__(self, key, value):
        try:
            self.cache.pop(key)
        except KeyError:
            if len(self.cache) >= self.capacity:
                self.cache.popitem(last=False)
        self.cache[key] = value

def retry_or_log(attempts=2):
    def decorator(func):
        @functools.wraps(func)
        def wrapped(*args, **kwargs):
            for att in range(attempts):
                try:
                    return func(*args, **kwargs)
                except Exception as ex:
                    if att == attempts-1:
                        logging.exception('Wrapped function failed.')
        return wrapped
    return decorator

def uniq(seq, key=None): # Dave Kirby
    # Order preserving
    seen = set()
    if key:
        return [x for x in seq if key(x) not in seen and not seen.add(key(x))]
    else:
        return [x for x in seq if x not in seen and not seen.add(x)]

def getpeerid(obj, key):
    if key in obj:
        pid = obj[key]['id']
        if obj[key]['type'] == 'user':
            return pid
        else:
            return -pid

peer_key = lambda peer: (peer['id'], peer['type'])

def init_db(filename):
    global DB, CONN
    DB = sqlite3.connect(filename)
    CONN = DB.cursor()
    CONN.execute('CREATE TABLE IF NOT EXISTS messages ('
    'id INTEGER PRIMARY KEY,'
    'src INTEGER,'
    'dest INTEGER,'
    'text TEXT,'
    'media TEXT,'
    'date INTEGER,'
    'fwd_src INTEGER,'
    'fwd_date INTEGER,'
    'reply_id INTEGER,'
    'out INTEGER,'
    'unread INTEGER,'
    'service INTEGER,'
    'action TEXT,'
    'flags INTEGER'
    ')')
    CONN.execute('CREATE TABLE IF NOT EXISTS users ('
    'id INTEGER PRIMARY KEY,'
    'phone TEXT,'
    'username TEXT,'
    'first_name TEXT,'
    'last_name TEXT,'
    'flags INTEGER'
    ')')
    CONN.execute('CREATE TABLE IF NOT EXISTS chats ('
    'id INTEGER PRIMARY KEY,'
    'title TEXT,'
    'members_num INTEGER,'
    'flags INTEGER'
    ')')
    # no better ways to get print_name by id :|
    CONN.execute('CREATE TABLE IF NOT EXISTS exportinfo ('
    'id INTEGER PRIMARY KEY,'
    'print_name TEXT,'
    'finished INTEGER'
    ')')

def update_peer(peer):
    global PEER_CACHE
    res = PEER_CACHE.get((peer['id'], peer['type']))
    if res:
        return peer
    if peer['type'] == 'user':
        CONN.execute('REPLACE INTO users (id, phone, username, first_name, last_name, flags) VALUES (?,?,?,?,?,?)', (peer['id'], peer.get('phone'), peer.get('username'), peer.get('first_name'), peer.get('last_name'), peer.get('flags')))
        pid = peer['id']
    elif peer['type'] == 'chat':
        CONN.execute('REPLACE INTO chats (id, title, members_num, flags) VALUES (?,?,?,?)', (peer['id'], peer.get('title'), peer.get('members_num'), peer.get('flags')))
        pid = -peer['id']
    if CONN.execute('SELECT print_name FROM exportinfo WHERE id = ?', (pid,)).fetchone():
        CONN.execute('UPDATE exportinfo SET print_name = ? WHERE id = ?', (peer.get('print_name'), pid))
    else:
        CONN.execute('INSERT INTO exportinfo (id, print_name, finished) VALUES (?,?,?)', (pid, peer.get('print_name'), 0))
    PEER_CACHE[(peer['id'], peer['type'])] = peer
    # not support PEER_ENCR_CHAT
    return peer

def is_finished(peer):
    pid = peer['id'] if peer['type'] == 'user' else -peer['id']
    res = CONN.execute('SELECT finished FROM exportinfo WHERE id = ?', (pid,)).fetchone()
    return res and res[0]

def set_finished(peer):
    pid = peer['id'] if peer['type'] == 'user' else -peer['id']
    if not is_finished(peer):
        CONN.execute('UPDATE exportinfo SET finished = ? WHERE id = ?', (1, pid))

def log_msg(msg):
    if 'from' in msg:
        update_peer(msg['from'])
    if 'to' in msg:
        update_peer(msg['to'])
    if 'fwd_from' in msg:
        update_peer(msg['fwd_from'])
    ret = not CONN.execute('SELECT 1 FROM messages WHERE id = ?', (msg['id'],)).fetchone()
    # REPLACE however
    # there can be messages like {"event": "message", "id": 561865}
    CONN.execute('REPLACE INTO messages (id, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (msg['id'], getpeerid(msg, 'from'), getpeerid(msg, 'to'), msg.get('text'), json.dumps(msg['media']) if 'media' in msg else None, msg.get('date'), getpeerid(msg, 'fwd_from'), msg.get('fwd_date'), msg.get('reply_id'), msg.get('out'), msg.get('unread'), msg.get('service'), json.dumps(msg['action']) if 'action' in msg else None, msg.get('flags')))
    return ret

def process(ln):
    ln = ln.strip()
    try:
        if re_msglist.match(ln):
            msglist = json.loads(ln)
            if not msglist:
                return (False, 0)
            hit = 0
            for msg in json.loads(ln):
                hit += log_msg(msg)
            return (True, hit)
        elif re_onemsg.match(ln):
            msg = json.loads(ln)
            if msg.get('event') == 'message':
                return (True, log_msg(msg))
            elif msg.get('event') == 'online-status':
                update_peer(msg['user'])
            elif msg.get('event') == 'updates' and 'deleted' in msg.get('updates', []):
                update_peer(msg['peer'])
            return (None, None)
        # ignore non-json lines
    except Exception as ex:
        logging.exception('Failed to process a line of result: ' + ln)
    return (None, None)

# child thread
def checkproc():
    global PROC, TGSOCK, TGCMD
    if PROC is None or PROC.poll() is not None:
        fd, sockfile = tempfile.mkstemp()
        os.close(fd)
        os.remove(sockfile)
        PROC = subprocess.Popen((TGCMD, '-k', os.path.join(os.path.dirname(__file__), 'tg-server.pub'), '--json', '-R', '-C', '-S', sockfile), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        while not os.path.exists(sockfile):
            time.sleep(0.5)
        TGSOCK = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        TGSOCK.connect(sockfile)
    return PROC

# child thread
def run_cli():
    while 1:
        logging.info('Starting telegram-cli...')
        checkproc()
        TGREADY.set()
        logging.info('Telegram-cli started.')
        try:
            while 1:
                out = PROC.stdout.readline().decode('utf-8')
                if out:
                    if out[0] not in '[{':
                        logging.info(out.strip())
                    else:
                        logging.debug(out.strip())
                    MSG_Q.put(out)
                else:
                    break
        except BrokenPipeError:
            pass
        TGREADY.clear()
        logging.warning('telegram-cli died.')

def send_command(cmd):
    checkproc()
    TGSOCK.sendall(cmd.encode('utf-8') + b'\n')
    data = TGSOCK.recv(1024)
    lines = data.split(b'\n', 1)
    if not lines[0].startswith(b'ANSWER '):
        raise ValueError('Bad reply from telegram-cli: %s' % lines[0])
    size = int(lines[0][7:].decode('ascii'))
    reply = lines[1] if len(lines) == 2 else b''
    while len(reply) < size:
        reply += TGSOCK.recv(1024)
    return reply.decode('utf-8')

def purge_queue():
    while 1:
        try:
            process(MSG_Q.get_nowait())
        except queue.Empty:
            break

def export_for(item, pos=0, force=False):
    logging.info('Exporting messages for %s from %d' % (item['print_name'], pos))
    try:
        if not pos:
            update_peer(item)
            res = process(send_command('history %s 100' % item['print_name']))
            pos = 100
        else:
            res = (True, 0)
        finished = not force and is_finished(item)
        while res[0] is True and not (finished and res[1]):
            res = process(send_command('history %s 100 %d' % (item['print_name'], pos)))
            pos += 100
    except socket.timeout:
        return pos
    except Exception:
        logging.exception('Failed to get messages for ' + item['print_name'])
        return pos
    set_finished(item)

def export_holes():
    logging.info('Getting the remaining messages...')
    got = set(i[0] for i in CONN.execute('SELECT id FROM messages') if isinstance(i[0], int))
    holes = set(range(1, max(logged) + 1)).difference(logged)
    failed = []
    for mid in holes:
        try:
            res = process(send_command('get_message %d' % mid))
            if not res[0]:
                logging.warning('ID %d may not exist' % mid)
        except Exception:
            failed.append(mid)
            logging.warning('Failed to get message ID %d' % mid)
    purge_queue()
    while failed:
        newlist = []
        for mid in holes:
            try:
                res = process(send_command('get_message %d' % mid))
            except Exception:
                failed.append(mid)
        failed = newlist
        purge_queue()

def export_text(force=False):
    logging.info('Exporting messages...')
    items = json.loads(send_command('contact_list'))
    for item in items:
        update_peer(item)
    purge_queue()
    items = json.loads(send_command('dialog_list 100'))
    dcount = 100
    failed = []
    while items:
        random.shuffle(items)
        for item in items:
            res = export_for(item, 0, force)
            if res is not None:
                failed.append((item, res))
                logging.warning('Failed to get messages for %s from %d' % (item['print_name'], res))
            purge_queue()
        items = json.loads(send_command('dialog_list 100 %d' % dcount))
        dcount += 100
        DB.commit()
    while failed:
        newlist = []
        for key, item in enumerate(failed):
            res = export_for(item[0], item[1], force)
            if res is not None:
                newlist.append((item[0], res))
            purge_queue()
        failed = newlist
    DB.commit()
    logging.info('Export to database completed.')

def export_avatar(pid, ptype):
    if ptype == 'chat':
        pid = -pid
    res = CONN.execute('SELECT print_name FROM exportinfo WHERE id = ?', (pid,)).fetchone()
    if not res:
        return False
    pname = res[0]
    if pid > 0:
        res = json.loads(send_command('load_user_photo ' + pname))
    else:
        res = json.loads(send_command('load_chat_photo ' + pname))
    ...

def output_db():
    pass

def output_txt():
    pass

def output_html():
    pass

DB = None
CONN = None
PEER_CACHE = LRUCache(5)
MSG_Q = queue.Queue()
PROC = None
TGSOCK = None
TGREADY = threading.Event()
TGCMD = 'bin/telegram-cli'
DLDIR = '.'

def main(argv):
    global TGCMD, DLDIR
    parser = argparse.ArgumentParser(description="Export Telegram messages.")
    parser.add_argument("-o", "--output", help="output path", default="export")
    parser.add_argument("-d", "--db", help="database path", default="telegram-export.db")
    parser.add_argument("-t", "--format", help="output format, can be 'db'(default), 'txt', 'html'", default="db")
    parser.add_argument("-f", "--force", help="force download all messages", action='store_true')
    parser.add_argument("-e", "--tgbin", help="Telegram-cli binary path", default="bin/telegram-cli")
    args = parser.parse_args(argv)

    TGCMD = args.tgbin
    DLDIR = args.output
    init_db(args.db)
    if args.format == 'db':
        outfunc = output_db
    elif args.format == 'txt':
        outfunc = output_txt
    elif args.format == 'html':
        outfunc = output_html

    cmdthr = threading.Thread(target=run_cli)
    cmdthr.daemon = True
    cmdthr.start()
    TGREADY.wait()

    try:
        time.sleep(1)
        export_text(args.force)
        export_holes()
    finally:
        if PROC:
            PROC.terminate()
        purge_queue()
        DB.commit()
    outfunc()

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
