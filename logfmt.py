#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import json
import sqlite3
import operator
import argparse
import collections

import jinja2

re_url = re.compile(r'(?i)\b((?:[a-z][\w-]+:(?:/{1,3}|[a-z0-9%])|www\d{0,3}[.]|[a-z0-9.\-]+[.][a-z]{2,4}/)(?:[^\s()<>]+|\(([^\s()<>]+|(\([^\s()<>]+\)))*\))+(?:\(([^\s()<>]+|(\([^\s()<>]+\)))*\)|[^\s`!()\[\]{};:\'".,<>?«»“”‘’]))', re.I)
imgfmt = frozenset(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'))

printname = lambda first, last='': (first + ' ' + last if last else first) or '<Unknown>'

strftime = lambda date, fmt='%Y-%m-%d %H:%M:%S': time.strftime(fmt, time.localtime(date))

unkpeer = lambda pid=None: {'id': pid, 'print': '<Unknown>'}
unkuser = lambda user: {
    'id': user['id'],
    'first_name': user['first_name'],
    'last_name': user.get('last_name'),
    'username': user.get('username'),
    'type': 'user',
    'flags': 256,
    'print': printname(user['first_name'], user.get('last_name'))
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

def media_bot2cli(text, media=None):
    if not media:
        return None, None
    dm = {}
    da = {}
    if ('audio' in media or 'document' in media
        or 'sticker' in media or 'video' in media
        or 'voice' in media):
        dm['type'] = 'document'
    elif 'photo' in media:
        dm['type'] = 'photo'
        dm['caption'] = text or ''
    elif 'contact' in media:
        dm['type'] = 'contact'
        dm['phone'] = media['contact']['phone_number']
        dm['first_name'] = media['contact']['first_name']
        dm['last_name'] = media['contact'].get('last_name')
        dm['user_id'] = media['contact'].get('user_id')
    elif 'location' in media:
        dm['type'] = 'geo'
        dm['longitude'] = media['location']['longitude']
        dm['latitude'] = media['location']['latitude']
    elif 'new_chat_participant' in media:
        user = media['new_chat_participant']
        da['type'] = 'chat_add_user'
        da['user'] = self.peer.get(user['id']) or unkuser(user)
    elif 'left_chat_participant' in media:
        user = media['left_chat_participant']
        da['type'] = 'chat_del_user'
        da['user'] = self.peer.get(user['id']) or unkuser(user)
    elif 'new_chat_title' in media:
        da['type'] = 'chat_rename'
        da['title'] = media['new_chat_title']
    elif 'new_chat_photo' in media:
        da['type'] = 'chat_change_photo'
    elif 'delete_chat_photo' in media:
        da['type'] = 'chat_delete_photo'
    elif 'group_chat_created' in media:
        da['type'] = 'chat_created'
        da['title'] = ''
    return json.dumps(dm) if dm else None, json.dumps(da) if da else None

class Messages:

    def __init__(self, isbotdb=False):
        self.peers = collections.defaultdict(unkpeer)
        self.msgs = {}
        self.db_cli = None
        self.conn_cli = None
        self.db_bot = None
        self.conn_bot = None
        self.botdest = None
        self.isbotdb = isbotdb
        self.dialogs = collections.defaultdict(set)
        self.template = 'history.txt'
        self.jinjaenv = jinja2.Environment(loader=jinja2.FileSystemLoader('templates'))
        self.jinjaenv.filters['strftime'] = strftime
        self.jinjaenv.filters['autolink'] = autolink
        self.jinjaenv.filters['smartname'] = smartname

    def init_db(self, filename, dbtype='cli', botuserdb=False, botdest=None):
        if os.path.isfile(filename):
            if dbtype == 'cli':
                self.db_cli = sqlite3.connect(filename)
                self.conn_cli = self.db_cli.cursor()
                self.userfromdb('cli')
            elif dbtype == 'bot':
                self.db_bot = sqlite3.connect(filename)
                self.conn_bot = self.db_bot.cursor()
                self.botdest = botdest
                if botuserdb or not self.db_cli:
                    self.userfromdb('bot')
        else:
            raise FileNotFoundError('Database not found: ' + filename)

    def msgfromdb(self, dbtype='cli', botdest=None):
        if dbtype == 'cli':
            for row in self.conn_cli.execute('SELECT id, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags FROM messages ORDER BY id ASC'):
                yield row
        elif dbtype == 'bot' and botdest:
            for mid, src, text, media, date, fwd_src, fwd_date, reply_id in self.conn_bot.execute('SELECT id, src, text, media, date, fwd_src, fwd_date, reply_id FROM messages ORDER BY date ASC, id ASC'):
                service, action = media_bot2cli(text, media)
                yield mid, src, botdest, text, media, date, fwd_src, fwd_date, reply_id, 0, 0, service, action, 256
        else:
            raise ValueError('dbtype or botdest is invalid')

    def userfromdb(self, dbtype='cli'):
        if dbtype == 'cli':
            for pid, phone, username, first_name, last_name, flags in self.conn_cli.execute('SELECT id, phone, username, first_name, last_name, flags FROM users'):
                self.peers[pid] = {
                    'id': pid,
                    'phone': phone,
                    'username': username,
                    'first_name': first_name,
                    'last_name': last_name,
                    'print': printname(first_name, last_name),
                    'flags': flags
                }
            for pid, title, members_num, flags in self.conn_cli.execute('SELECT id, title, members_num, flags FROM chats'):
                self.peers[-pid] = {
                    'id': -pid,
                    'title': title,
                    'members_num': members_num,
                    'print': printname(title),
                    'flags': flags
                }
        elif dbtype == 'bot':
            for pid, username, first_name, last_name in self.conn_bot.execute('SELECT id, username, first_name, last_name FROM users'):
                self.peers[pid].update({
                    'id': pid,
                    'username': username,
                    'first_name': first_name,
                    'last_name': last_name,
                    'print': printname(first_name, last_name)
                })



    def getmsgs(self):
        for mid, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags in self.conn_cli.execute('SELECT id, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags FROM messages ORDER BY id ASC'):
            if fwd_src:
                msgtype = 'fwd'
                extra = {'fwd_src': self.peers[fwd_src], 'fwd_date': fwd_date}
            elif reply_id:
                msgtype = 're'
                extra = {'reply': self.msgs.get(reply_id, unkmsg(reply_id))}
            else:
                msgtype, extra = '', None
            media = json.loads(media or '{}')
            msg = {
                'mid': mid,
                'src': self.peers[src],
                'dest': self.peers[dest],
                'text': text or media.get('caption'),
                'media': media,
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

def autolink(text, img=True):
    match = re_url.search(text)
    if not match:
        return text
    if img and os.path.splitext(match.group(1))[1] in imgfmt:
        return match.expand('<a href="\1"><img src="\1"></a>')
    else:
        return match.expand('<a href="\1">\1</a>')

def smartname(user, limit=20):
    if 'first_name' not in user:
        return '<%s>' % 'Unknown'[:limit-2]
    first, last = user['first_name'], user.get('last_name', '')
    pn = printname(first, last)
    if len(pn) > limit:
        if len(first) > limit:
            return first.split(None, 1)[0][:limit]
        else:
            return first[:limit]
    else:
        return pn

DB = None
CONN = None

def main(argv):
    parser = argparse.ArgumentParser(description="Export Telegram messages.")
    parser.add_argument("-o", "--output", help="output path", default="export.txt")
    parser.add_argument("-d", "--db", help="tg-export database path", default="telegram-export.db")
    parser.add_argument("-b", "--botdb", help="tg-chatdig database path", default="")
    parser.add_argument("-D", "--botdb-dest", help="tg-chatdig logged chat id")
    parser.add_argument("-u", "--botdb-user", action="store_true", help="use user information in tg-chatdig database first")
    parser.add_argument("-t", "--type", help="export type, can be 'txt'(default), 'html'", default="txt")
    parser.add_argument("-p", "--peer", help="export certain peer id", type=int)
    args = parser.parse_args(argv)

    msg = Messages()
    if args.type == 'html':
        msg.template = 'simple.html'
    if args.db:
        msg.init_db(args.db, 'cli')
    if args.botdb:
        msg.init_db(args.botdb, 'bot', args.botdb_user, args.botdb_dest)
    msg.getmsgs()
    with open(args.output, 'w') as f:
        f.write(msg.render_peer(args.peer))

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
