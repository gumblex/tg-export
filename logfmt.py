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

re_url = re.compile(r'''\b
(
    # URL (gruber v2)
    (?:
        [a-z][\w-]+:(?:/{1,3}|[a-z0-9%])
    |
        www\d{0,3}[.]
    |
        [a-z0-9.\-]+[.][a-z]{2,4}/
    |
        magnet:\?
    )
    (?:
        [^\s()<>]+
    |
        \(([^\s()<>]+|(\([^\s()<>]+\)))*\)
    )+
    (?:
        \(([^\s()<>]+|(\([^\s()<>]+\)))*\)
    |
        [^\s`!()\[\]{};:\'".,<>?«»“”‘’]
    )
|
    # BT Hash
    (?:
        [a-f0-9]{40}
    |
        [a-z2-7]{32}
    )
)''', re.I | re.X)
re_bthash = re.compile(r'[0-9a-f]{40}|[a-z2-7]{32}', re.I)
re_limit = re.compile(r'^([0-9]+)(,[0-9]+)?$')
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

class LRUCache:

    def __init__(self, maxlen):
        self.capacity = maxlen
        self.cache = collections.OrderedDict()

    def __getitem__(self, key):
        value = self.cache.pop(key)
        self.cache[key] = value
        return value

    def get(self, key, default=None):
        try:
            value = self.cache.pop(key)
            self.cache[key] = value
            return value
        except KeyError:
            return default

    def __setitem__(self, key, value):
        try:
            self.cache.pop(key)
        except KeyError:
            if len(self.cache) >= self.capacity:
                self.cache.popitem(last=False)
        self.cache[key] = value

class StreamArray(list):
    def __init__(self, iterable):
        self.iterable = iterable

    def __iter__(self):
        return self.iterable

    # according to the comment below
    def __len__(self):
        return 1

class Messages:

    def __init__(self, stream=False, template='history.txt'):
        self.peers = collections.defaultdict(unkpeer)
        if stream:
            self.msgs = LRUCache(100)
        else:
            self.msgs = collections.OrderedDict()

        self.db_cli = None
        self.conn_cli = None
        self.db_bot = None
        self.conn_bot = None

        self.limit = None
        self.hardlimit = None
        self.botdest = None

        self.template = template
        self.stream = stream
        self.cachedir = None
        self.urlprefix = None
        self.jinjaenv = jinja2.Environment(loader=jinja2.FileSystemLoader('templates'))
        self.jinjaenv.filters['strftime'] = strftime
        self.jinjaenv.filters['autolink'] = autolink
        self.jinjaenv.filters['isimg'] = lambda url: os.path.splitext(url)[1] in imgfmt
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

    def msgfromdb(self, dbtype='cli', peer=None):
        if self.limit:
            match = re_limit.match(self.limit)
            if match:
                if match.group(2):
                    limit = 'LIMIT %d OFFSET %s' % (min(int(match.group(1)), self.hardlimit), match.group(2)[1:])
                else:
                    limit = 'LIMIT %d' % min(int(match.group(1)), self.hardlimit)
            else:
                limit = 'LIMIT %d' % self.hardlimit
        else:
            limit = ''
        if dbtype == 'cli':
            if peer:
                where = 'WHERE src=%d or dest=%d' % (peer, peer)
            for row in self.conn_cli.execute('SELECT * FROM (SELECT id, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags FROM messages %s ORDER BY date DESC, id DESC %s) ORDER BY date ASC, id ASC' % (where, limit)):
                yield row
        elif dbtype == 'bot' and self.botdest:
            for mid, src, text, media, date, fwd_src, fwd_date, reply_id in self.conn_bot.execute('SELECT * FROM (SELECT id, src, text, media, date, fwd_src, fwd_date, reply_id FROM messages ORDER BY date DESC, id DESC %s) ORDER BY date ASC, id ASC' % limit):
                media, action = self.media_bot2cli(text, media)
                yield mid, src, self.botdest, text, media, date, fwd_src, fwd_date, reply_id, 0, 0, bool(action), action, 256
        else:
            raise ValueError('dbtype or self.botdest is invalid')

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

    def media_bot2cli(self, text, media=None, strict=False):
        if not media:
            return None, None
        media = json.loads(media)
        dm = {}
        da = {}

        mt = None
        if self.cachedir:
            mt = media.keys() & frozenset(('audio', 'document', 'sticker', 'video', 'voice'))
            file_id = None
            if mt:
                mt = mt.pop()
                file_id = media[mt]['file_id']
            elif 'photo' in media:
                file_id = max(media['photo'], key=lambda x: x['width'])['file_id']
            if file_id:
                for fn in os.listdir(self.cachedir):
                    if fn.startswith(file_id):
                        dm['url'] = self.urlprefix + fn
                        break

        if mt and not strict:
            dm.update(dm[mt])

        if ('audio' in media or 'document' in media
            or 'sticker' in media or 'video' in media
            or 'voice' in media):
            if strict:
                dm['type'] = 'document'
            else:
                dm['type'] = mt or 'document'
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
            da['user'] = self.peers.get(user['id']) or unkuser(user)
        elif 'left_chat_participant' in media:
            user = media['left_chat_participant']
            da['type'] = 'chat_del_user'
            da['user'] = self.peers.get(user['id']) or unkuser(user)
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

    def getmsgs(self, peer=None):
        db = 'cli' if self.db_cli else 'bot'
        for mid, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags in self.msgfromdb(db, peer):
            if fwd_src:
                msgtype = 'fwd'
                extra = {'fwd_src': self.peers[fwd_src], 'fwd_date': fwd_date}
            elif reply_id:
                msgtype = 're'
                extra = {'reply': self.msgs.get(reply_id, unkmsg(reply_id))}
            else:
                msgtype, extra = '', None
            media = json.loads(media or '{}')
            src = self.peers[src]
            if db == 'bot' and '_ircuser' in media:
                src['first_name'] = src['print'] = media['_ircuser']
            msg = {
                'mid': mid,
                'src': src,
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
            if db == 'cli':
                if dest == peer and (out or dest and dest < 0):
                    yield mid, msg
                elif src == peer:
                    yield mid, msg
            else:
                yield mid, msg

    def render_peer(self, pid, name=None):
        peer = self.peers[pid].copy()
        if name:
            peer['print'] = name
        kvars = {
            'peer': peer,
            'gentime': time.time()
        }
        if self.stream:
            kvars['msgs'] = (m for k, m in self.getmsgs(pid))
        else:
            msgs = tuple(m for k, m in self.getmsgs(pid))
            kvars['msgs'] = msgs
            kvars['start'] = min(msgs, key=operator.itemgetter('date'))['date']
            kvars['end'] = max(msgs, key=operator.itemgetter('date'))['date']
            kvars['count'] = len(msgs)
        template = self.jinjaenv.get_template(self.template)
        yield from template.stream(**kvars)

    def render_peer_json(self, pid, name=None):
        je = json.JSONEncoder()
        peer = self.peers[pid].copy()
        if name:
            peer['print'] = name
        kvars = {
            'peer': peer,
            'gentime': time.time()
        }
        kvars['msgs'] = StreamArray(m for k, m in self.getmsgs(pid))
        yield from je.iterencode(kvars)

def autolink(text, img=True):
    ret = []
    lastpos = 0
    for match in re_url.finditer(text):
        start, end = match.span()
        url = text[start:end]
        if re_bthash.match(url):
            ret.append('%s<a href="magnet:?xt=urn:btih:%s">%s</a>' % (text[lastpos:start], url, url))
        elif img and os.path.splitext(url)[1] in imgfmt:
            ret.append('%s<a href="%s"><img src="%s"></a>' % (text[lastpos:start], url, url))
        else:
            ret.append('%s<a href="%s">%s</a>' % (text[lastpos:start], url, url))
        lastpos = end
    ret.append(text[lastpos:])
    return ''.join(ret)

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
    parser = argparse.ArgumentParser(description="Format exported database file into human-readable format.")
    parser.add_argument("-o", "--output", help="output path", default="export.txt")
    parser.add_argument("-d", "--db", help="tg-export database path", default="telegram-export.db")
    parser.add_argument("-b", "--botdb", help="tg-chatdig bot database path", default="")
    parser.add_argument("-D", "--botdb-dest", help="tg-chatdig bot logged chat id", type=int)
    parser.add_argument("-u", "--botdb-user", action="store_true", help="use user information in tg-chatdig database first")
    parser.add_argument("-t", "--template", help="export template, can be 'txt'(default), 'html', 'json', or template file name", default="txt")
    parser.add_argument("-p", "--peer", help="export certain peer id", type=int)
    parser.add_argument("-P", "--peer-print", help="set print name for the peer")
    parser.add_argument("-l", "--limit", help="limit the number of fetched messages and set the offset")
    parser.add_argument("-L", "--hardlimit", help="set a hard limit of the number of messages, must be used with -l", type=int, default=100000)
    parser.add_argument("-c", "--cachedir", help="the path of media files")
    parser.add_argument("-r", "--urlprefix", help="the url prefix of media files")
    args = parser.parse_args(argv)

    msg = Messages(stream=args.template.endswith('html'))
    msg.limit = args.limit
    msg.hardlimit = args.hardlimit
    msg.cachedir = args.cachedir
    msg.urlprefix = args.urlprefix
    render_func = msg.render_peer
    if args.template == 'html':
        msg.template = 'simple.html'
    elif args.template == 'txt':
        msg.template = 'history.txt'
    elif args.template == 'json':
        render_func = msg.render_peer_json
    else:
        msg.template = args.template
    if args.db:
        msg.init_db(args.db, 'cli')
    if args.botdb:
        msg.init_db(args.botdb, 'bot', args.botdb_user or not args.db, args.botdb_dest)
    if args.output == '-':
        for ln in render_func(args.peer, args.peer_print):
            sys.stdout.write(ln)
    else:
        with open(args.output, 'w') as f:
            for ln in render_func(args.peer, args.peer_print):
                f.write(ln)

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
