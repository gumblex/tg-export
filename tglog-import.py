#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import json
import time
import queue
import sqlite3
import operator
import threading
import itertools
import functools
import subprocess
import collections

DB_NAME = 'telegram-history.db' # SQLite 3 database file name.
CHAT_NAME = '@@Orz_分部喵'

db = sqlite3.connect(DB_NAME)
conn = db.cursor()
conn.execute('''CREATE TABLE IF NOT EXISTS messages (
id INTEGER PRIMARY KEY,
src INTEGER,
dest INTEGER,
text TEXT,
media TEXT,
date INTEGER,
fwd_src INTEGER,
fwd_date INTEGER,
reply_id INTEGER,
out INTEGER,
unread INTEGER,
service INTEGER,
action TEXT,
flags INTEGER
)''')
conn.execute('''CREATE TABLE IF NOT EXISTS users (
id INTEGER PRIMARY KEY,
phone TEXT,
username TEXT,
first_name TEXT,
last_name TEXT,
flags INTEGER
)''')
conn.execute('''CREATE TABLE IF NOT EXISTS chats (
id INTEGER PRIMARY KEY,
title TEXT,
members_num INTEGER,
flags INTEGER
)''')

logged = set()

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

peer_cache = LRUCache(5)

def update_peer(peer):
    global peer_cache
    res = peer_cache.get((peer['id'], peer['type']))
    if res:
        return peer
    if peer['type'] == 'user':
        conn.execute('REPLACE INTO users (id, phone, username, first_name, last_name, flags) VALUES (?,?,?,?,?,?)', (peer['id'], peer.get('phone'), peer.get('username'), peer.get('first_name'), peer.get('last_name'), peer.get('flags')))
    elif peer['type'] == 'chat':
        conn.execute('REPLACE INTO chats (id, title, members_num, flags) VALUES (?,?,?,?)', (peer['id'], peer['title'], peer['members_num'], peer['flags']))
    peer_cache[(peer['id'], peer['type'])] = 1
    # not support PEER_ENCR_CHAT
    return peer

def log_msg(msg):
    update_peer(msg['from'])
    update_peer(msg['to'])
    if 'fwd_from' in msg:
        update_peer(msg['fwd_from'])
    conn.execute('REPLACE INTO messages (id, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (msg['id'], msg['from']['id'], msg['to']['id'], msg.get('text'), json.dumps(msg['media']) if 'media' in msg else None, msg['date'], msg['fwd_from']['id'] if 'fwd_from' in msg else None, msg.get('fwd_date'), msg.get('reply_id'), msg['out'], msg['unread'], msg['service'], json.dumps(msg['action']) if 'action' in msg else None, msg['flags']))
    logged.add(msg['id'])

def init():
    global logged
    logged = set(i[0] for i in conn.execute('SELECT id FROM messages') if isinstance(i[0], int))

def uniq(seq): # Dave Kirby
    # Order preserving
    seen = set()
    return [x for x in seq if x not in seen and not seen.add(x)]

def rangedet(data):
    ranges = []
    for key, group in itertools.groupby(enumerate(data), lambda v: v[0] - v[1]):
        group = tuple(map(operator.itemgetter(1), group))
        ranges.append((group[0], group[-1]))
    return ranges

re_msglist = re.compile(r'^\[.+\]$')
re_onemsg = re.compile(r'^\{.+\}$')

def lineproc(ln):
    ln = ln.strip()
    try:
        if re_msglist.match(ln):
            ret = None
            for msg in json.loads(ln):
                if 'id' in msg and 'from' in msg and 'date' in msg:
                    log_msg(msg)
                    ret = True
            return ret
        elif re_onemsg.match(ln):
            msg = json.loads(ln)
            if 'id' in msg and 'from' in msg and 'date' in msg:
                log_msg(msg)
                return True
            elif msg.get('result') == "FAIL":
                return False
    except Exception as ex:
        print(msg)
        raise ex

def file_input(filename):
    global logged
    with open(filename, 'r') as f:
        for ln in f:
            lineproc(ln)
    db.commit()

findholes = lambda: set(range(max(logged))).difference(logged)
expandholes = lambda holes, n: set(itertools.chain.from_iterable(range(x-n, x+n+1) for x in holes))

def generate_cmd(chatname):
    for start, end in rangedet(sorted(expandholes(findholes(), 2))):
        chunk, rem = divmod(end - start + 1, 100)
        for i in range(chunk):
            yield 'history %s 100 %s' % (chatname, max(max(logged) - (end - i * 100), 0))
        yield 'history %s %s %s' % (chatname, rem, max(max(logged) - start, 0))

proc = None

def enqueue_output(queue):
    global proc
    tgdir = '/home/gumble/dev/tg'
    #tgdir = '/home/gumble/github/tg'
    while 1:
        proc = subprocess.Popen((os.path.join(tgdir, 'bin/telegram-cli'), '--json', '-RC'), stdin=subprocess.PIPE, stdout=subprocess.PIPE, cwd=tgdir)
        try:
            for line in proc.stdout:
                queue.put(line)
        except BrokenPipeError:
            continue
        finally:
            proc.terminate()


def proc_input():
    while len(findholes()) > 100:
        try:
            q = queue.Queue()
            t = threading.Thread(target=enqueue_output, args=(q,))
            t.daemon = True # thread dies with the program
            t.start()
            print('### Launched telegram-cli. Max ID:', max(logged))
            cgen = generate_cmd(CHAT_NAME)
            first = 1
            failcount = 0
            ln, ret = None, None
            while 1:
                try:
                    ln = q.get_nowait() # or q.get(timeout=.1)
                except queue.Empty:
                    time.sleep(2)
                    failcount += 1
                else: # got line
                    # ... do something with line
                    ln = ln.decode('utf-8').rstrip()
                    ret = lineproc(ln)
                if first and not ret:
                    if ln:
                        print(ln)
                elif (ln and (ln[0] == '[' or ret)) or failcount > 5:
                    try:
                        cmd = next(cgen)
                    except StopIteration:
                        continue
                    print(cmd)
                    proc.stdin.write((cmd + '\n').encode('utf-8'))
                    if first:
                        first = 0
                    failcount = 0
                    proc.stdin.flush()
                elif ln:
                    print(ln)
            print('### telegram-cli died.')
        finally:
            db.commit()

init()
#file_input(sys.argv[1])
proc_input()
#db.commit()
