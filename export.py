#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import time
import queue
import socket
import sqlite3
import logging
import tempfile
import argparse
import operator
import threading
import itertools
import functools
import subprocess
import collections

re_msglist = re.compile(r'^\[.*\]$')
re_onemsg = re.compile(r'^\{.+\}$')

logging.basicConfig(stream=sys.stdout, format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)

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

def uniq(seq, key=None): # Dave Kirby
    # Order preserving
    seen = set()
    if key:
        return [x for x in seq if key(x) not in seen and not seen.add(key(x))]
    else:
        return [x for x in seq if x not in seen and not seen.add(x)]

peer_key = lambda peer: (peer['id'], peer['type'])

def init_db(filename):
    global db, conn
    db = sqlite3.connect(filename)
    conn = db.cursor()
    conn.execute('CREATE TABLE IF NOT EXISTS messages ('
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
    conn.execute('CREATE TABLE IF NOT EXISTS users ('
    'id INTEGER PRIMARY KEY,'
    'phone TEXT,'
    'username TEXT,'
    'first_name TEXT,'
    'last_name TEXT,'
    'flags INTEGER'
    ')')
    conn.execute('CREATE TABLE IF NOT EXISTS chats ('
    'id INTEGER PRIMARY KEY,'
    'title TEXT,'
    'members_num INTEGER,'
    'flags INTEGER'
    ')')
    # no better ways to get print_name by id :|
    conn.execute('CREATE TABLE IF NOT EXISTS printnames ('
    'id INTEGER PRIMARY KEY,'
    'print_name TEXT'
    ')')

def update_peer(peer):
    global peer_cache
    res = peer_cache.get((peer['id'], peer['type']))
    if res:
        return peer
    if peer['type'] == 'user':
        conn.execute('REPLACE INTO users (id, phone, username, first_name, last_name, flags) VALUES (?,?,?,?,?,?)', (peer['id'], peer.get('phone'), peer.get('username'), peer.get('first_name'), peer.get('last_name'), peer.get('flags')))
        conn.execute('REPLACE INTO printnames (id, print_name) VALUES (?,?)', (peer['id'], peer.get('print_name')))
    elif peer['type'] == 'chat':
        conn.execute('REPLACE INTO chats (id, title, members_num, flags) VALUES (?,?,?,?)', (peer['id'], peer.get('title'), peer.get('members_num'), peer.get('flags')))
        conn.execute('REPLACE INTO printnames (id, print_name) VALUES (?,?)', (-peer['id'], peer.get('print_name')))
    peer_cache[(peer['id'], peer['type'])] = peer
    # not support PEER_ENCR_CHAT
    return peer

def log_msg(msg):
    update_peer(msg['from'])
    update_peer(msg['to'])
    if 'fwd_from' in msg:
        update_peer(msg['fwd_from'])
    conn.execute('REPLACE INTO messages (id, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (msg['id'], msg['from']['id'], msg['to']['id'], msg.get('text'), json.dumps(msg['media']) if 'media' in msg else None, msg['date'], msg['fwd_from']['id'] if 'fwd_from' in msg else None, msg.get('fwd_date'), msg.get('reply_id'), msg['out'], msg['unread'], msg['service'], json.dumps(msg['action']) if 'action' in msg else None, msg['flags']))

def process(ln):
    ln = ln.strip()
    try:
        if re_msglist.match(ln):
            msglist = json.loads(ln)
            if not msglist:
                return None
            for msg in json.loads(ln):
                log_msg(msg)
            return True
        elif re_onemsg.match(ln):
            msg = json.loads(ln)
            if msg.get('event') == 'message':
                log_msg(msg)
                return True
            return None
        # ignore non-json lines
    except Exception as ex:
        logging.exception('Failed to process a line of result: ' % ln)

# child thread
def checkproc():
    global proc, sockfile, tgsock
    if proc is None or proc.poll() is not None:
        fd, sockfile = tempfile.mkstemp()
        os.close(fd)
        os.remove(sockfile)
        proc = subprocess.Popen((os.path.join(tgdir, tgcmd), '--json', '-R', '-C', '-S', sockfile), stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=tgdir)
        tgsock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        tgsock.connect(sockfile)
    return proc

# child thread
def run_cli():
    while 1:
        logging.info('Starting telegram-cli...')
        checkproc()
        try:
            out = proc.stdout.readline()
            while out:
                msg_q.put(out)
        except BrokenPipeError:
            pass
        logging.info('telegram-cli died.')

def send_command(cmd):
    tgsock.sendall(cmd.encode('utf-8') + b'\n')
    data = tgsock.recv(1024)
    lines = data.split(b'\n', 1)
    if not lines[0].startswith(b'ANSWER '):
        raise ValueError('Bad reply from telegram-cli: %s')
    size = int(lines[0][7:])
    reply = lines[1] if len(lines) == 2 else b''
    while len(reply) < size:
        reply += tgsock.recv(1024)
    return reply.decode('utf-8')

def purge_queue():
    while 1:
        try:
            process(msg_q.get_nowait())
        except queue.Empty:
            break

def export_text():
    items = json.loads(send_command('contact_list'))
    for item in items:
        update_peer(item)
    purge_queue()
    items = json.loads(send_command('dialog_list 100'))
    dcount = 100
    while items:
        for item in items:
            update_peer(item)
            res = process(send_command('history %s 50' % item['print_name']))
            count = 50
            while res:
                res = process(send_command('history %s 50 %d' % (item['print_name'], count)))
                count += 50
            purge_queue()
        items = json.loads(send_command('dialog_list 100 %d' % dcount))
        dcount += 100
    db.commit()

def export_avatar(pid, ptype):
    if ptype == 'chat':
        pid = -pid
    res = conn.execute('SELECT print_name FROM printnames WHERE id = ?', (pid,)).fetchone()
    if not res:
        return False
    pname = res[0]
    if pid > 0
        res = json.loads(send_command('load_user_photo ' + pname))
    else:
        res = json.loads(send_command('load_chat_photo ' + pname))

CFG = {
"tgdir": ".",
"tgcmd": "bin/telegram-cli",
"db": "telegram.db",
"db": "telegram.db",

}


tgdir = '.'
tgcmd = 'bin/telegram-cli'

db = None
conn = None
peer_cache = LRUCache(5)
proc = None
sockfile = None
tgsock = None
msg_q = queue.Queue()
download_dir = '.'

def main():
    parser = argparse.ArgumentParser(description="A flexible backup tool.")
    parser.add_argument("PATH", nargs='+', help="Paths to archive")
