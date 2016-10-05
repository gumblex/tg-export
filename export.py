#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import re
import sys
import json
import time
import queue
import random
import socket
import struct
import sqlite3
import logging
import argparse
import binascii
import functools
import collections

import tgcli

__version__ = '3.0'

re_msglist = re.compile(r'^\[.*\]$')
re_onemsg = re.compile(r'^\{.+\}$')
re_getmsg = re.compile(r'^\*\*\* [0-9.]+ id=\d+$')

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

class tgl_peer_id_t(collections.namedtuple('tgl_peer_id_t', 'peer_type peer_id access_hash')):
    '''
    typedef struct {
      int peer_type;
      int peer_id;
      long long access_hash;
    } tgl_peer_id_t;
    '''
    TGL_PEER_USER = 1
    TGL_PEER_CHAT = 2
    TGL_PEER_GEO_CHAT = 3
    TGL_PEER_ENCR_CHAT = 4
    TGL_PEER_CHANNEL = 5
    TGL_PEER_TEMP_ID = 100
    TGL_PEER_RANDOM_ID = 101
    TGL_PEER_UNKNOWN = 0

    @classmethod
    def loads(cls, s):
        return cls._make(struct.unpack('<iiq', binascii.a2b_hex(s.lstrip('$'))))

    def dumps(self):
        return '$' + binascii.b2a_hex(struct.pack('<iiq', *self)).decode('ascii')

    @classmethod
    def from_peer(cls, peer):
        pid = peer['id']
        if isinstance(pid, str):
            return cls.loads(pid)
        elif peer['type'] == 'user':
            return cls(cls.TGL_PEER_USER, pid, 0)
        elif peer['type'] == 'chat':
            return cls(cls.TGL_PEER_CHAT, pid, 0)
        elif peer['type'] == 'encr_chat':
            return cls(cls.TGL_PEER_ENCR_CHAT, pid, 0)
        elif peer['type'] == 'channel':
            return cls(cls.TGL_PEER_CHANNEL, pid, 0)

    def to_id(self):
        # We assume peer_type is unsigned int.
        return self.peer_type<<32 | self.peer_id

class tgl_message_id_t(collections.namedtuple('tgl_message_id_t', 'peer_type peer_id id access_hash')):
    '''
    typedef struct tgl_message_id {
      unsigned peer_type;
      unsigned peer_id;
      long long id;
      long long access_hash;
    } tgl_message_id_t;

    The peer_type, peer_id and access_hash are the same as those of the chat.
    '''
    @classmethod
    def loads(cls, s):
        return cls._make(struct.unpack('<IIqq', binascii.a2b_hex(s)))

    def dumps(self):
        return binascii.b2a_hex(struct.pack('<IIqq', *self)).decode('ascii')

def getpeerid(obj, key):
    if key in obj:
        return tgl_peer_id_t.from_peer(obj[key]).to_id()

def getmsgid(obj, key):
    if key in obj:
        if isinstance(obj[key], int):
            return obj[key]
        else:
            return tgl_message_id_t.loads(obj[key]).id

def print_id(obj):
    try:
        return '%s#id%d' % (obj['peer_type'], obj['peer_id'])
    except KeyError:
        return obj['print_name']

def init_db(filename):
    global DB, CONN
    DB = sqlite3.connect(filename)
    CONN = DB.cursor()
    CONN.execute('CREATE TABLE IF NOT EXISTS messages ('
        'id INTEGER,'   # can be not unique in channels
        'src INTEGER,'  # tgl_peer_id_t.to_id
        'dest INTEGER,' # tgl_peer_id_t.to_id
        'text TEXT,'
        'media TEXT,'
        'date INTEGER,'
        'fwd_src INTEGER,' # tgl_peer_id_t.to_id
        'fwd_date INTEGER,'
        'reply_id INTEGER,'
        'out INTEGER,'
        'unread INTEGER,'
        'service INTEGER,'
        'action TEXT,'
        'flags INTEGER,'
        'PRIMARY KEY (id, dest)'
    ')')
    CONN.execute('CREATE TABLE IF NOT EXISTS users ('
        'id INTEGER PRIMARY KEY,' # peer_id
        'access_hash INTEGER,'
        'phone TEXT,'
        'username TEXT,'
        'first_name TEXT,'
        'last_name TEXT,'
        'flags INTEGER'
    ')')
    CONN.execute('CREATE TABLE IF NOT EXISTS chats ('
        'id INTEGER PRIMARY KEY,' # peer_id
        'access_hash INTEGER,'
        'title TEXT,'
        'members_num INTEGER,'
        'flags INTEGER'
    ')')
    CONN.execute('CREATE TABLE IF NOT EXISTS channels ('
        'id INTEGER PRIMARY KEY,' # peer_id
        'access_hash INTEGER,'
        'title TEXT,'
        'participants_count INTEGER,'
        'admins_count INTEGER,'
        'kicked_count INTEGER,'
        'flags INTEGER'
    ')')
    CONN.execute('CREATE TABLE IF NOT EXISTS peerinfo ('
        'id INTEGER PRIMARY KEY,' # tgl_peer_id_t.to_id
        'type TEXT,'
        'print_name TEXT,'
        'finished INTEGER'
    ')')

def update_peer(peer):
    global PEER_CACHE
    pst = tgl_peer_id_t.from_peer(peer)
    pid = pst.to_id()
    res = PEER_CACHE.get(pid)
    if res:
        return peer
    if 'peer_type' in peer:
        peer_type = peer['peer_type']
    else:
        peer_type = peer['type']
    if 'peer_id' in peer:
        peer_id = peer['peer_id']
    else:
        peer_id = peer['id']
    if peer_type == 'user':
        CONN.execute('REPLACE INTO users VALUES (?,?,?,?,?,?,?)', (peer_id, pst.access_hash, peer.get('phone'), peer.get('username'), peer.get('first_name'), peer.get('last_name'), peer.get('flags')))
    elif peer_type == 'chat':
        CONN.execute('REPLACE INTO chats VALUES (?,?,?,?,?)', (peer_id, pst.access_hash, peer.get('title'), peer.get('members_num'), peer.get('flags')))
    elif peer_type == 'channel':
        CONN.execute('REPLACE INTO channels VALUES (?,?,?,?,?,?,?)', (peer_id, pst.access_hash, peer.get('title'), peer.get('participants_count'), peer.get('admins_count'), peer.get('kicked_count'), peer.get('flags')))
    # not support encr_chat
    if CONN.execute('SELECT print_name FROM peerinfo WHERE id = ?', (pid,)).fetchone():
        CONN.execute('UPDATE peerinfo SET print_name = ? WHERE id = ?', (peer.get('print_name'), pid))
    else:
        CONN.execute('INSERT INTO peerinfo VALUES (?,?,?,?)', (pid, peer_type, peer.get('print_name'), 0))
    PEER_CACHE[pid] = peer
    return peer

def is_finished(peer):
    res = CONN.execute('SELECT finished FROM peerinfo WHERE id = ?', (tgl_peer_id_t.from_peer(peer).to_id(),)).fetchone()
    return res and res[0]

def set_finished(peer, pos):
    CONN.execute('UPDATE peerinfo SET finished = ? WHERE id = ?', (pos, tgl_peer_id_t.from_peer(peer).to_id()))

def reset_finished():
    CONN.execute('UPDATE peerinfo SET finished = 0')

def log_msg(msg):
    if 'from' in msg:
        update_peer(msg['from'])
    if 'to' in msg:
        update_peer(msg['to'])
    if 'fwd_from' in msg:
        update_peer(msg['fwd_from'])
    ret = not CONN.execute('SELECT 1 FROM messages WHERE id = ? AND dest = ?', (msg['id'], getpeerid(msg, 'to'))).fetchone()
    # there can be messages like {"event": "message", "id": 561865}
    # empty messages can be written, and overwritten
    # json-tg.c:424  if (!(M->flags & TGLMF_CREATED)) { return res; }
    if ret or 'flags' in msg:
        CONN.execute('REPLACE INTO messages VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)', (getmsgid(msg, 'id'), getpeerid(msg, 'from'), getpeerid(msg, 'to'), msg.get('text'), json.dumps(msg['media']) if 'media' in msg else None, msg.get('date'), getpeerid(msg, 'fwd_from'), msg.get('fwd_date'), getmsgid(msg, 'reply_id'), msg.get('out'), msg.get('unread'), msg.get('service'), json.dumps(msg['action']) if 'action' in msg else None, msg.get('flags')))
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
        if msg.get('event') in ('message', 'service', 'read'):
            return (True, log_msg(msg))
        elif msg.get('event') == 'online-status':
            update_peer(msg['user'])
        elif 'peer' in msg:
            update_peer(msg['peer'])
        elif msg.get('result') == 'FAIL':
            if 'can not parse' in msg.get('error', ''):
                TGCLI.cmd_dialog_list()
                #raise ValueError(msg.get('error'))
            return (False, 0)
        return (None, None)
        # ignore non-json lines
    elif obj == '':
        raise ValueError('empty line received')
    return (None, None)

def purge_queue():
    while 1:
        try:
            process(MSG_Q.get_nowait())
        except queue.Empty:
            break

def on_start():
    logging.info('Telegram-cli started.')
    time.sleep(2)
    TGCLI.cmd_dialog_list()
    logging.info('Telegram-cli is ready.')

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

def logging_status(pos, end=False, seg=1000, length=None):
    if pos % seg:
        sys.stdout.write('.')
    elif pos:
        if length:
            sys.stdout.write('%.2f%%' % (pos * 100 / length))
        else:
            sys.stdout.write(str(pos))
    if end:
        if pos and pos % seg:
            if length:
                sys.stdout.write('%.2f%%\n' % (pos * 100 / length))
            else:
                sys.stdout.write('%d\n' % pos)
        else:
            sys.stdout.write('\n')
    sys.stdout.flush()

def export_for(item, pos=0, force=False):
    logging.info('Exporting messages for %s from %d' % (item['print_name'], pos))
    try:
        # Get the first 100
        if not pos:
            update_peer(item)
            msglist = TGCLI.cmd_history(print_id(item), 100)
            res = process(msglist)
            logging_status(pos)
            pos = 100
        else:
            res = (True, 0)
        # Get the recently updated messages until overlapped
        while res[0] is True and not res[1]:
            msglist = TGCLI.cmd_history(print_id(item), 100, pos)
            res = process(msglist)
            logging_status(pos)
            pos += 100
        # If force, then continue
        if not force:
            pos = max(pos, is_finished(item))
        # Else, get messages from the offset of last time
        # Until no message is returned (may be not true)
        while res[0] is True:
            msglist = TGCLI.cmd_history(print_id(item), 100, pos)
            res = process(msglist)
            logging_status(pos)
            pos += 100
    except Exception:
        logging_status(pos, True)
        if pos > is_finished(item):
            set_finished(item, pos)
        return pos
    logging_status(pos, True)
    set_finished(item, pos)

def find_holes(minv, maxv, s):
    for n in range(minv, maxv + 1):
        if n not in s:
            yield n

def export_holes():
    '''
    Try to get remaining messages by using message id.
    '''
    # First we get messages that belong to ourselves,
    # i.e. not channel messages or encr-chat
    # 17179869184 = TGL_PEER_ENCR_CHAT 4<<32
    got = set(i[0] for i in CONN.execute('SELECT id FROM messages WHERE dest < 17179869184') if isinstance(i[0], int))
    # it doesn't verify peer_type, peer_id, access_hash
    if got:
        holes = [tgl_message_id_t(1, 0, n, 0) for n in find_holes(1, max(got), got)]
    # Then we get channel (supergroup) messages.
    if TG_TEST:
        channels = [tgl_peer_id_t(tgl_peer_id_t.TGL_PEER_CHANNEL, *i) for i in
                    CONN.execute('SELECT id, access_hash FROM channels')]
        for channel in channels:
            got = set(i[0] for i in CONN.execute('SELECT id FROM messages WHERE dest = ?', (channel.to_id(),)) if isinstance(i[0], int))
            if got:
                holes.extend(tgl_message_id_t(channel.peer_type, channel.peer_id, n, channel.access_hash) for n in find_holes(1, max(got), got))
    length = len(holes)
    logging.info('Getting the remaining %d messages...' % length)
    # we need some uncertainty to work around the uncertainty of telegram-cli
    random.shuffle(holes)
    # list of mids (may be str or int, depending on TG_TEST)
    failed = []
    for k, msg in enumerate(holes, 1):
        if TG_TEST:
            mid = msg.dumps()
        else:
            mid = msg.id
        try:
            res = process(TGCLI.send_command('get_message %s' % mid))
            if not res[0]:
                logging.warning('%r may not exist [%.2f%%]', msg[:3], (k * 100 / length))
            elif k % 10 == 0:
                logging_status(k, False, 100, length)
        except tgcli.TelegramCliExited:
            # interface.c:4295: print_message: Assertion `M' failed.
            logging.warning('%r may not exist [%.2f%%]', msg[:3], (k * 100 / length))
        except Exception:
            failed.append(mid)
            logging.exception('Failed to get message ID %s' % mid)
    logging_status(k, True, 100, length)
    purge_queue()
    while failed:
        length = len(failed)
        logging.info('Retrying the remaining %d messages...' % length)
        newlist = []
        # see above
        random.shuffle(failed)
        for k, mid in enumerate(failed, 1):
            try:
                res = process(TGCLI.send_command('get_message %s' % mid))
            except Exception:
                # such an old bug (`newlist` here was `failed`)
                newlist.append(mid)
            if k % 10 == 0:
                logging_status(k, False, 100, length)
        logging_status(k, True, 100, length)
        failed = newlist
        purge_queue()

def export_text(force=False):
    #if force:
        #reset_finished()
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
    # we need some uncertainty to work around the uncertainty of telegram-cli
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
        for item, pos in failed:
            res = export_for(item, pos, force)
            if res is not None:
                newlist.append((item, res))
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
TG_TEST = True

def main(argv):
    global TGCLI, DLDIR, TG_TEST
    parser = argparse.ArgumentParser(description="Export Telegram messages.")
    parser.add_argument("-o", "--output", help="output path", default="export")
    parser.add_argument("-d", "--db", help="database path", default="tg-export3.db")
    parser.add_argument("-f", "--force", help="force download all messages", action='store_true')
    parser.add_argument("-B", "--batch-only", help="fetch messages in batch only, don't try to get more missing messages", action='store_true')
    parser.add_argument("-l", "--logging", help="logging mode (keep running)", action='store_true')
    parser.add_argument("-L", "--keep-logging", help="first export, then keep logging", action='store_true')
    parser.add_argument("-e", "--tgbin", help="telegram-cli binary path", default="bin/telegram-cli")
    parser.add_argument("-v", "--verbose", help="print debug messages", action='store_true')
    args = parser.parse_args(argv)

    if args.verbose:
        logging.getLogger().setLevel(logging.DEBUG)
        tgcli.logger.setLevel(logging.DEBUG)

    DLDIR = args.output
    init_db(args.db)

    TGCLI = tgcli.TelegramCliInterface(args.tgbin, extra_args=('-W', '-E'), run=False, timeout=30)
    TGCLI.on_json = MSG_Q.put
    TGCLI.on_info = lambda s: tgcli.logger.info(s) if not re_getmsg.match(s) else None
    #TGCLI.on_text = MSG_Q.put
    #TGCLI.on_start = on_start
    TGCLI.run()
    TGCLI.ready.wait()
    time.sleep(1)

    # the 'test' branch of tg has channel support
    TG_TEST = 'channel' in TGCLI.cmd_help()

    try:
        if not args.logging:
            export_text(args.force)
            if not args.batch_only:
                export_holes()
        if args.logging or args.keep_logging:
            while TGCLI.ready.is_set():
                d = MSG_Q.get()
                logging.info(logging_fmt(d))
                process(d)
    finally:
        TGCLI.close()
        purge_queue()
        DB.commit()

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
