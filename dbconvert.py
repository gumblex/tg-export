#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import struct
import sqlite3
import binascii
import collections

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

def convert_peerid1(peerid):
    if peerid is not None:
        return tgl_peer_id_t(tgl_peer_id_t.TGL_PEER_CHAT if peerid < 0 else tgl_peer_id_t.TGL_PEER_USER, abs(peerid), 0).to_id()

def convert_peerid2(peerid):
    if peerid is not None:
        return tgl_peer_id_t.loads(peerid).to_id()

def convert_msgid2(msgid):
    if msgid is None:
        return None
    elif len(msgid) == 48:
        return tgl_message_id_t.loads(msgid).id
    else:
        return int(msgid)

def init_db(cur):
    cur.execute('CREATE TABLE IF NOT EXISTS messages ('
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
    cur.execute('CREATE TABLE IF NOT EXISTS users ('
        'id INTEGER PRIMARY KEY,' # peer_id
        'access_hash INTEGER,'
        'phone TEXT,'
        'username TEXT,'
        'first_name TEXT,'
        'last_name TEXT,'
        'flags INTEGER'
    ')')
    cur.execute('CREATE TABLE IF NOT EXISTS chats ('
        'id INTEGER PRIMARY KEY,' # peer_id
        'access_hash INTEGER,'
        'title TEXT,'
        'members_num INTEGER,'
        'flags INTEGER'
    ')')
    cur.execute('CREATE TABLE IF NOT EXISTS channels ('
        'id INTEGER PRIMARY KEY,' # peer_id
        'access_hash INTEGER,'
        'title TEXT,'
        'participants_count INTEGER,'
        'admins_count INTEGER,'
        'kicked_count INTEGER,'
        'flags INTEGER'
    ')')
    cur.execute('CREATE TABLE IF NOT EXISTS peerinfo ('
        'id INTEGER PRIMARY KEY,' # tgl_peer_id_t.to_id
        'type TEXT,'
        'print_name TEXT,'
        'finished INTEGER'
    ')')

FILENAME_IN = 'tg-export2.db'
FILENAME_OUT = 'tg-export3.db'

if len(sys.argv) > 1:
    FILENAME_IN = sys.argv[1]
if len(sys.argv) > 2:
    FILENAME_OUT = sys.argv[2]

if not os.path.isfile(FILENAME_IN):
    print('Database file not found.')
    sys.exit(1)

DB_IN = sqlite3.connect(FILENAME_IN)
CUR_IN = DB_IN.cursor()

for n in CUR_IN.execute("SELECT name FROM sqlite_master WHERE type='table'"):
    if n[0] == 'exportinfo':
        VER = 1
        break
    elif n[0] == 'peerinfo':
        VER = 2
        break
else:
    print('Database not recognized.')
    sys.exit(1)

print('Converting database:')

DB = sqlite3.connect(FILENAME_OUT)
CUR = DB.cursor()
init_db(CUR)

if VER == 1:
    print('* messages')
    for mid, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags in CUR_IN.execute('SELECT * FROM messages ORDER BY id ASC'):
        CUR.execute('REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (mid, convert_peerid1(src), convert_peerid1(dest), text, media, date, convert_peerid1(fwd_src), fwd_date, reply_id, out, unread, service, action, flags))
    print('* users')
    for pid, phone, username, first_name, last_name, flags in CUR_IN.execute('SELECT * FROM users'):
        CUR.execute('REPLACE INTO users VALUES (?,?,?,?,?,?,?)', (pid, 0, phone, username, first_name, last_name, flags))
    print('* chats')
    for pid, title, members_num, flags in CUR_IN.execute('SELECT * FROM chats'):
        CUR.execute('REPLACE INTO chats VALUES (?,?,?,?,?)', (pid, 0, title, members_num, flags))
    print('* peerinfo')
    for pid, print_name, finished in CUR_IN.execute('SELECT * FROM exportinfo'):
        CUR.execute('REPLACE INTO peerinfo VALUES (?,?,?,?)', (convert_peerid1(pid), 'chat' if pid < 0 else 'user', print_name, finished))
else:
    print('* messages')
    for mid, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags in CUR_IN.execute('SELECT * FROM messages ORDER BY date, id ASC'):
        CUR.execute('REPLACE INTO messages VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)', (tgl_message_id_t.loads(mid).id, convert_peerid2(src), convert_peerid2(dest), text, media, date, convert_peerid2(fwd_src), fwd_date, convert_msgid2(reply_id), out, unread, service, action, flags))
    print('* users')
    for pid, permanent_id, phone, username, first_name, last_name, flags in CUR_IN.execute('SELECT * FROM users'):
        CUR.execute('REPLACE INTO users VALUES (?,?,?,?,?,?,?)', (pid, tgl_peer_id_t.loads(permanent_id).access_hash, phone, username, first_name, last_name, flags))
    print('* chats')
    for pid, permanent_id, title, members_num, flags in CUR_IN.execute('SELECT * FROM chats'):
        CUR.execute('REPLACE INTO chats VALUES (?,?,?,?,?)', (pid, tgl_peer_id_t.loads(permanent_id).access_hash, title, members_num, flags))
    print('* channels')
    for pid, permanent_id, title, participants_count, admins_count, kicked_count, flags in CUR_IN.execute('SELECT * FROM channels'):
        CUR.execute('REPLACE INTO channels VALUES (?,?,?,?,?,?,?)', (pid, tgl_peer_id_t.loads(permanent_id).access_hash, title, participants_count, admins_count, kicked_count, flags))
    print('* peerinfo')
    for pid, ptype, print_name, finished in CUR_IN.execute('SELECT * FROM peerinfo'):
        CUR.execute('REPLACE INTO peerinfo VALUES (?,?,?,?)', (convert_peerid2(pid), ptype, print_name, finished))

DB.commit()
print('Done.')
