#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import sqlite3
import operator
import argparse
import collections

import jinja2

printname = lambda first, last='': (first + ' ' + last if last else first) or '<Unknown>'

def init_db(filename):
    global DB, CONN
    if os.path.isfile(filename):
        DB = sqlite3.connect(filename)
        CONN = DB.cursor()
    else:
        raise FileNotFoundError('Database not found: ' + filename)

class Peers:

    def __init__(self):
        self.peers = {}

    def __len__(self):
        return len(self.peers)

    def __getitem__(self, key):
        try:
            return self.peers[key]
        except KeyError:
            return {'id': key, 'print': '<Unknown>'}

    def load(self):
        for pid, phone, username, first_name, last_name, flags in CONN.execute('SELECT id, phone, username, first_name, last_name, flags FROM users'):
            self.peers[pid] = {
                'id': pid,
                'phone': phone,
                'username': username,
                'first_name': first_name,
                'last_name': last_name,
                'print': printname(first_name, last_name),
                'flags': flags
            }
        for pid, title, members_num, flags in CONN.execute('SELECT id, title, members_num, flags FROM chats'):
            self.peers[-pid] = {
                'id': -pid,
                'title': title,
                'members_num': members_num,
                'print': printname(title),
                'flags': flags
            }

unkmsg = lambda mid: {
    'mid': mid,
    'src': {'id': 0, 'print': '<Unknown>'},
    'dest': {'id': 0, 'print': '<Unknown>'},
    'text': '<Lost message>',
    'media': {},
    'date': 0,
    'msgtype': '',
    'extra': None,
    'out': 0,
    'unread': 0,
    'service': 0,
    'action': {},
    'flags': 0
}

def strftime(date, fmt='%Y-%m-%d %H:%M:%S'):
    return time.strftime(fmt, time.localtime(date))

class Messages:

    def __init__(self, isbotdb=False):
        self.peers = Peers()
        self.peers.load()
        self.msgs = {}
        self.isbotdb = isbotdb
        self.dialogs = collections.defaultdict(set)
        self.template = 'history.txt'
        self.jinjaenv = jinja2.Environment(loader=jinja2.FileSystemLoader('templates'))
        self.jinjaenv.filters['strftime'] = strftime

    def getmsgs(self):
        for mid, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags in CONN.execute('SELECT id, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags FROM messages ORDER BY id ASC'):
            if fwd_src:
                msgtype = 'fwd'
                extra = {'fwd_src': self.peers[fwd_src], 'fwd_date': fwd_date}
            elif reply_id:
                msgtype = 're'
                extra = {'reply': self.msgs.get(reply_id, unkmsg(reply_id))}
            else:
                msgtype, extra = '', None
            msg = {
                'mid': mid,
                'src': self.peers[src],
                'dest': self.peers[dest],
                'text': text,
                'media': json.loads(media or '{}'),
                'date': date,
                'msgtype': msgtype,
                'extra': extra,
                'out': out,
                'unread': unread,
                'service': service,
                'action': json.loads(action or '{}'),
                'flags': flags
            }
            self.msgs[mid] = msg
            if out or dest and dest < 0:
                self.dialogs[dest].add(mid)
            elif src:
                self.dialogs[src].add(mid)

    def render_peer(self, pid):
        msgs = tuple(self.msgs[m] for m in sorted(self.msgs.keys()) if m in self.dialogs[pid])
        start = min(msgs, key=operator.itemgetter('date'))['date']
        end = max(msgs, key=operator.itemgetter('date'))['date']
        kvars = {
            'msgs': msgs,
            'peer': self.peers[pid],
            'gentime': time.time(),
            'start': start,
            'end': end,
            'count': len(msgs)
        }
        template = self.jinjaenv.get_template(self.template)
        return template.render(**kvars)

DB = None
CONN = None

def main(argv):
    parser = argparse.ArgumentParser(description="Export Telegram messages.")
    parser.add_argument("-o", "--output", help="output path", default="export.txt")
    parser.add_argument("-d", "--db", help="database path", default="telegram-export.db")
    parser.add_argument("-t", "--type", help="export type, can be 'txt'(default), 'html'", default="txt")
    parser.add_argument("-p", "--peer", help="export certain peer id", type=int)
    args = parser.parse_args(argv)

    init_db(args.db)
    msg = Messages()
    msg.getmsgs()
    with open(args.output, 'w') as f:
        f.write(msg.render_peer(args.peer))

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
