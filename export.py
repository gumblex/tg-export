#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import sys
import json
import time
import queue
import random
import socket
import sqlite3
import logging
import argparse
import functools
import collections

import tgcli

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

def reset_finished():
    CONN.execute('UPDATE exportinfo SET finished = 0')

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

def process(obj):
    if isinstance(obj, list):
        if not obj:
            return (False, 0)
        hit = 0
        for msg in obj:
            hit += log_msg(msg)
        return (True, hit)
    elif isinstance(obj, dict):
        msg = obj
        if msg.get('event') == 'message':
            return (True, log_msg(msg))
        elif msg.get('event') == 'online-status':
            update_peer(msg['user'])
        elif msg.get('event') == 'updates' and 'deleted' in msg.get('updates', []):
            update_peer(msg['peer'])
        return (None, None)
        # ignore non-json lines
    return (None, None)

def purge_queue():
    while 1:
        try:
            process(MSG_Q.get_nowait())
        except queue.Empty:
            break

def logging_fmt(msg):
    if msg.get('event') == 'message':
        dst = msg['to']['print_name'] if 'to' in msg else '<Unknown>'
        src = msg['from']['print_name'] if 'from' in msg else '<Unknown>'
        return ' '.join(filter(None, (dst, src, '>>>', msg.get('text', ''), str(msg.get('media', '')))))
    elif msg.get('event') == 'service':
        dst = msg['to']['print_name'] if 'to' in msg else '<Unknown>'
        src = msg['from']['print_name'] if 'from' in msg else '<Unknown>'
        return ' '.join(filter(None, (dst, src, '>>>', str(msg.get('action', '')))))
    else:
        return repr(msg)[:100]

def logging_status(pos, end=False):
    if pos % 1000:
        sys.stdout.write('.')
    elif pos:
        sys.stdout.write(str(pos))
    if end and pos > 100:
        if pos % 1000:
            sys.stdout.write('%d\n' % pos)
        else:
            sys.stdout.write('\n')
    sys.stdout.flush()

def export_for(item, pos=0, force=False):
    logging.info('Exporting messages for %s from %d' % (item['print_name'], pos))
    try:
        if not pos:
            update_peer(item)
            msglist = TGCLI.cmd_history(item['print_name'], 100)
            res = process(msglist)
            logging_status(pos)
            pos = 100
        else:
            res = (True, 0)
        finished = not force and is_finished(item)
        while res[0] is True and not (finished and res[1]):
            msglist = TGCLI.cmd_history(item['print_name'], 100, pos)
            res = process(msglist)
            logging_status(pos)
            pos += 100
    except Exception:
        logging_status(pos, True)
        return pos
    logging_status(pos, True)
    set_finished(item)

def export_holes():
    got = set(i[0] for i in CONN.execute('SELECT id FROM messages') if isinstance(i[0], int))
    holes = list(set(range(1, max(got) + 1)).difference(got))
    logging.info('Getting the remaining messages [%d-%d]...' % (min(holes), max(holes)))
    failed = []
    # we need some uncertainty to work around the uncertainty of telegram-cli
    random.shuffle(holes)
    for mid in holes:
        try:
            res = process(TGCLI.send_command('get_message %d' % mid))
            if not res[0]:
                logging.warning('ID %d may not exist' % mid)
        except (socket.timeout, ValueError):
            failed.append(mid)
        except Exception:
            failed.append(mid)
            logging.exception('Failed to get message ID %d' % mid)
    purge_queue()
    while failed:
        logging.info('Retrying the remaining messages [%d-%d]...' % (min(failed), max(failed)))
        newlist = []
        # see above
        random.shuffle(failed)
        for mid in failed:
            try:
                res = process(TGCLI.send_command('get_message %d' % mid))
            except Exception:
                failed.append(mid)
        failed = newlist
        purge_queue()

def export_text(force=False):
    if force:
        reset_finished()
    logging.info('Getting contacts...')
    update_peer(TGCLI.cmd_get_self())
    items = TGCLI.cmd_contact_list()
    for item in items:
        update_peer(item)
    purge_queue()
    logging.info('Getting dialogs...')
    dlist = items = lastitems = TGCLI.cmd_dialog_list(100)
    dcount = 100
    while items:
        items = TGCLI.cmd_dialog_list(100, dcount)
        if frozenset(d['id'] for d in items) == frozenset(d['id'] for d in lastitems):
            break
        dlist.extend(items)
        dcount += 100
    for item in dlist:
        update_peer(item)
    logging.info('Exporting messages...')
    failed = []
    random.shuffle(dlist)
    for item in dlist:
        res = export_for(item, 0, force)
        if res is not None:
            failed.append((item, res))
            logging.warning('Failed to get messages for %s from %d' % (item['print_name'], res))
        purge_queue()
    DB.commit()
    while failed:
        newlist = []
        for key, item in enumerate(failed):
            res = export_for(item[0], item[1], force)
            if res is not None:
                newlist.append((item[0], res))
                logging.warning('Failed to get messages for %s from %d' % (item['print_name'], res))
            purge_queue()
        failed = newlist
    DB.commit()
    logging.info('Export to database completed.')

DB = None
CONN = None
PEER_CACHE = LRUCache(10)
MSG_Q = queue.Queue()
TGCLI = None
DLDIR = '.'

def main(argv):
    global TGCLI, DLDIR
    parser = argparse.ArgumentParser(description="Export Telegram messages.")
    parser.add_argument("-o", "--output", help="output path", default="export")
    parser.add_argument("-d", "--db", help="database path", default="tg-export2.db")
    parser.add_argument("-f", "--force", help="force download all messages", action='store_true')
    parser.add_argument("-l", "--logging", help="logging mode (keep running)", action='store_true')
    parser.add_argument("-e", "--tgbin", help="telegram-cli binary path", default="bin/telegram-cli")
    args = parser.parse_args(argv)

    DLDIR = args.output
    init_db(args.db)

    TGCLI = tgcli.TelegramCliInterface(args.tgbin, extra_args=('-W',), run=False, timeout=30)
    TGCLI.on_json = MSG_Q.put
    #TGCLI.on_info = tgcli.do_nothing
    #TGCLI.on_text = MSG_Q.put
    TGCLI.run()
    TGCLI.ready.wait()
    time.sleep(1)

    try:
        if args.logging:
            while TGCLI.ready.is_set():
                d = MSG_Q.get()
                logging.info(logging_fmt(d))
                process(d)
        else:
            export_text(args.force)
            export_holes()
    finally:
        TGCLI.close()
        purge_queue()
        DB.commit()

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
