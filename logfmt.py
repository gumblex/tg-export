#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import re
import sys
import time
import json
import struct
import sqlite3
import operator
import argparse
import binascii
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
    )
)''', re.I | re.X)
re_bthash = re.compile(r'[0-9a-f]{40}|[a-z2-7]{32}', re.I)
re_limit = re.compile(r'^([0-9]+)(,[0-9]+)?$')
imgfmt = frozenset(('.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp'))

printname = lambda first, last='': (first + ' ' + last if last else first) or '<Unknown>'

strftime = lambda date, fmt='%Y-%m-%d %H:%M:%S': time.strftime(fmt, time.localtime(date))

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

def convert_msgid2(msgid):
    if msgid is None:
        return None
    elif isinstance(msgid, int):
        return msgid
    elif len(msgid) == 48:
        return tgl_message_id_t.loads(msgid).id
    else:
        return int(msgid)

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

class PeerStore(collections.UserDict):

    def __init__(self, *args, **kwds):
        super().__init__(*args, **kwds)
        self.name = {}

    def __setitem__(self, key, value):
        self.data[self._convert(key)] = value

    def setname(self, key, value):
        self.name[value] = self._convert(key)

    def __getitem__(self, key):
        peerid, peertype = self._convert(key)
        try:
            return self.data[(peerid, peertype)]
        except KeyError:
            d = self.data[(peerid, peertype)] = {'id': peerid, 'type': peertype, 'print': '<Unknown>'}
            return d

    def find(self, key):
        try:
            return self.__getitem__(key)
        except Exception:
            if key in self.name:
                return self.data[self.name[key]]
            else:
                for k, v in self.name.items():
                    if key in k:
                        return self.data[v]
        return {'id': None, 'type': 'user', 'print': key}

    @staticmethod
    def _convert(key=None):
        peertype = None
        if isinstance(key, tuple):
            peerid, peertype = key
        else:
            peerid = key
        peer_id = None
        peer_type = tgl_peer_id_t.TGL_PEER_USER
        try:
            peerid = int(peerid)
        except ValueError:
            pass
        if isinstance(peerid, str):
            sp = peerid.split('#id', 1)
            if len(sp) == 2:
                peer_id = int(sp[1])
                peertype = sp[0]
            else:
                peer = tgl_peer_id_t.loads(peerid)
                peer_id = peer.peer_id
                peer_type = peer.peer_type
        elif peerid is None:
            pass
        elif peerid > 4294967296:
            # 1 << 32
            peer_id = peerid & 4294967295
            peer_type = peerid >> 32
        else:
            peer_id = abs(peerid)
            peer_type = tgl_peer_id_t.TGL_PEER_CHAT if peerid < 0 else tgl_peer_id_t.TGL_PEER_USER
        if peertype:
            return (peer_id, peertype)
        elif peer_type == tgl_peer_id_t.TGL_PEER_USER:
            return (peer_id, 'user')
        elif peer_type == tgl_peer_id_t.TGL_PEER_CHAT:
            return (peer_id, 'chat')
        elif peer_type == tgl_peer_id_t.TGL_PEER_ENCR_CHAT:
            return (peer_id, 'encr_chat')
        elif peer_type == tgl_peer_id_t.TGL_PEER_CHANNEL:
            return (peer_id, 'channel')

class Messages:

    def __init__(self, stream=False, template='history.txt'):
        self.peers = PeerStore()
        if stream:
            self.msgs = LRUCache(100)
        else:
            self.msgs = collections.OrderedDict()

        self.db_cli = None
        self.conn_cli = None
        self.db_cli_ver = None
        self.db_bot = None
        self.conn_bot = None

        self.limit = None
        self.hardlimit = None
        self.botdest = None

        self.template = template
        self.stream = stream
        # can be 'bot', 'cli' or None (no conversion)
        self.media_format = 'cli'
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
                for name, sql in self.conn_cli.execute("SELECT name, sql FROM sqlite_master WHERE type='table'"):
                    if name == 'exportinfo':
                        self.db_cli_ver = 1
                        break
                    elif name == 'peerinfo':
                        if 'permanent_id' in sql:
                            self.db_cli_ver = 2
                        else:
                            self.db_cli_ver = 3
                        break
                self.userfromdb('cli')
            elif dbtype == 'bot':
                self.db_bot = sqlite3.connect(filename)
                self.conn_bot = self.db_bot.cursor()
                self.botdest = self.peers.find(botdest)
                if self.botdest['id'] is None:
                    raise KeyError('peer not found: %s' % botdest)
                if self.botdest['type'] == 'user':
                    self.botdest['type'] = 'chat'
                # self.botdest = tgl_peer_id_t.from_peer(self.botdest).to_id()
                self.botdest = (self.botdest['id'], self.botdest['type'])
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
                if self.db_cli_ver == 1:
                    if peer['type'] == 'user':
                        pid = peer['id']
                    else:
                        pid = -peer['id']
                elif self.db_cli_ver == 2:
                    pid = tgl_peer_id_t.from_peer(peer).dumps()
                else:
                    pid = tgl_peer_id_t.from_peer(peer).to_id()
                c = self.conn_cli.execute('SELECT * FROM (SELECT id, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags FROM messages WHERE src=? or dest=? ORDER BY date DESC, id DESC %s) ORDER BY date ASC, id ASC' % limit, (pid, pid))
            else:
                c = self.conn_cli.execute('SELECT * FROM (SELECT id, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags FROM messages ORDER BY date DESC, id DESC %s) ORDER BY date ASC, id ASC' % limit)
            for mid, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags in c:
                if self.media_format == 'bot':
                    media, caption = self.media_cli2bot(media, action)
                    text = text or caption
                yield convert_msgid2(mid), src, dest, text, media, date, fwd_src, fwd_date, convert_msgid2(reply_id), out, unread, service, action, flags
        elif dbtype == 'bot' and self.botdest:
            for mid, src, text, media, date, fwd_src, fwd_date, reply_id in self.conn_bot.execute('SELECT * FROM (SELECT id, src, text, media, date, fwd_src, fwd_date, reply_id FROM messages ORDER BY date DESC, id DESC %s) ORDER BY date ASC, id ASC' % limit):
                if self.media_format == 'cli':
                    media, action = self.media_bot2cli(text, media)
                else:
                    action = None
                yield mid, src, self.botdest, text, media, date, fwd_src, fwd_date, reply_id, 0, 0, bool(action), action, 256
        else:
            raise ValueError('dbtype or self.botdest is invalid')

    def userfromdb(self, dbtype='cli'):
        if dbtype == 'cli':
            for pid, phone, username, first_name, last_name, flags in self.conn_cli.execute('SELECT id, phone, username, first_name, last_name, flags FROM users'):
                self.peers[(pid, 'user')] = {
                    'id': pid,
                    'type': 'user',
                    'phone': phone,
                    'username': username,
                    'first_name': first_name,
                    'last_name': last_name,
                    'print': printname(first_name, last_name),
                    'flags': flags
                }
            for pid, title, members_num, flags in self.conn_cli.execute('SELECT id, title, members_num, flags FROM chats'):
                self.peers[(pid, 'chat')] = {
                    'id': pid,
                    'type': 'chat',
                    'title': title,
                    'members_num': members_num,
                    'print': printname(title),
                    'flags': flags
                }
            if self.db_cli_ver > 1:
                for pid, title, members_num, admins_count, kicked_count, flags in self.conn_cli.execute('SELECT id, title, participants_count, admins_count, kicked_count, flags FROM channels'):
                    self.peers[(pid, 'channel')] = {
                        'id': pid,
                        'type': 'channel',
                        'title': title,
                        # keep compatible with chats
                        'members_num': members_num,
                        'admins_count': admins_count,
                        'kicked_count': kicked_count,
                        'print': printname(title),
                        'flags': flags
                    }
            if self.db_cli_ver == 1:
                sql = 'SELECT id, print_name FROM exportinfo'
            elif self.db_cli_ver == 2:
                sql = 'SELECT permanent_id, print_name FROM peerinfo'
            else:
                sql = 'SELECT id, print_name FROM peerinfo'
            for pid, print_name in self.conn_cli.execute(sql):
                self.peers.setname(pid, print_name)
        elif dbtype == 'bot':
            for pid, username, first_name, last_name in self.conn_bot.execute('SELECT id, username, first_name, last_name FROM users'):
                self.peers[(pid, 'user')].update({
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

        if '_ircuser' in media:
            dm['_ircuser'] = media['_ircuser']
        if mt and not strict:
            dm.update(media[mt])

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
        elif 'venue' in media:
            dm['type'] = 'venue'
            dm['longitude'] = media['venue']['location']['longitude']
            dm['latitude'] = media['venue']['location']['latitude']
            if media['venue']['title']:
                dm['type'] = media['venue']['title']
            dm['address'] = media['venue']['address']
            if 'foursquare_id' in media['venue']:
                dm['provider'] = 'foursquare'
                dm['venue_id'] = media['venue']['foursquare_id']
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

    def media_cli2bot(media=None, action=None):
        type_map = {
            # media
            'photo': 'photo',
            'document': 'document',
            'unsupported': 'document',
            'geo': 'location',
            'venue': 'location',
            'contact': 'contact',
            # action
            'chat_add_user': 'new_chat_participant',
            'chat_add_user_link': 'new_chat_participant',
            'chat_del_user': 'left_chat_participant',
            'chat_rename': 'new_chat_title',
            'chat_change_photo': 'new_chat_photo',
            'chat_delete_photo': 'delete_chat_photo',
            'chat_created': 'group_chat_created'
        }
        d = {}
        caption = None
        if media:
            media = json.loads(media)
        if action:
            action = json.loads(action)
        if media and 'type' in media:
            media = media.copy()
            if media['type'] == 'photo':
                caption = media['caption']
                d['photo'] = []
            elif media['type'] in ('document', 'unsupported'):
                d['document'] = {}
            elif 'longitude' in media:
                # 'type' may be the name of the place
                loc = {
                    'longitude': media['longitude'],
                    'latitude': media['latitude']
                }
                if media['type'] == 'geo':
                    d['location'] = loc
                else:
                    d['venue'] = {
                        'location': loc,
                        'title': media['type'] if media['type'] != 'venue' else '',
                        'address': media['address']
                    }
                    if media.get('provider') == 'foursquare' and 'venue_id' in media:
                        d['venue']['foursquare_id'] = media['venue_id']
            elif media['type'] == 'contact':
                del media['type']
                media['phone_number'] = media.pop('phone')
                d['contact'] = media
            # ignore other undefined types to Bot API
        if action and 'type' in action:
            newname = type_map.get(action['type'])
            if newname.endswith('chat_participant'):
                d[newname] = {
                    'id': action['user']['id'],
                    'first_name': action['user'].get('first_name', ''),
                    'last_name': action['user'].get('last_name', ''),
                    'username': action['user'].get('username', '')
                }
            elif newname == 'new_chat_title':
                d[newname] = action['title']
            elif newname == 'new_chat_photo':
                d[newname] = []
            elif newname in ('delete_chat_photo', 'group_chat_created'):
                d[newname] = True
            # ignore other undefined types to Bot API
        return json.dumps(d) if d else None, caption

    def getmsgs(self, peer=None):
        db = 'cli' if self.db_cli else 'bot'
        for mid, src, dest, text, media, date, fwd_src, fwd_date, reply_id, out, unread, service, action, flags in self.msgfromdb(db, peer):
            src = self.peers[src]
            dest = self.peers[dest]
            if not (db == 'bot' or
                dest['id'] == peer['id'] or
                peer['type'] == 'user' and
                src['id'] == peer['id'] and dest['type'] == 'user'):
                continue
            if fwd_src:
                msgtype = 'fwd'
                extra = {'fwd_src': self.peers[fwd_src], 'fwd_date': fwd_date}
            elif reply_id:
                msgtype = 're'
                remsg = self.msgs.get(reply_id, unkmsg(reply_id))
                if remsg['msgtype'] == 're':
                    remsg = remsg.copy()
                    remsg['extra'] = None
                extra = {'reply': remsg}
            else:
                msgtype, extra = '', None
            media = json.loads(media or '{}')
            if db == 'bot' and '_ircuser' in media:
                src['first_name'] = src['print'] = media['_ircuser']
            msg = {
                'mid': mid,
                'src': src,
                'dest': dest,
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
            yield mid, msg

    def render_peer(self, peer, name=None):
        peer = peer.copy()
        if name:
            peer['print'] = name
        kvars = {
            'peer': peer,
            'gentime': time.time()
        }
        if self.stream:
            kvars['msgs'] = (m for k, m in self.getmsgs(peer))
        else:
            msgs = tuple(m for k, m in self.getmsgs(peer))
            kvars['msgs'] = msgs
            if msgs:
                kvars['start'] = min(msgs, key=operator.itemgetter('date'))['date']
                kvars['end'] = max(msgs, key=operator.itemgetter('date'))['date']
            else:
                kvars['start'] = kvars['end'] = 0
            kvars['count'] = len(msgs)
        template = self.jinjaenv.get_template(self.template)
        yield from template.stream(**kvars)

    def render_peer_json(self, peer, name=None):
        je = json.JSONEncoder(indent=0)
        peer = peer.copy()
        if name:
            peer['print'] = name
        kvars = {
            'peer': peer,
            'gentime': time.time()
        }
        kvars['msgs'] = StreamArray(m for k, m in self.getmsgs(peer))
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

def main(argv):
    parser = argparse.ArgumentParser(description="Format exported database file into human-readable format.")
    parser.add_argument("-o", "--output", help="output path")
    parser.add_argument("-d", "--db", help="tg-export database path", default="tg-export3.db")
    parser.add_argument("-b", "--botdb", help="tg-chatdig bot database path", default="")
    parser.add_argument("-D", "--botdb-dest", help="tg-chatdig bot logged chat id or tg-cli-style peer name")
    parser.add_argument("-u", "--botdb-user", action="store_true", help="use user information in tg-chatdig database first")
    parser.add_argument("-t", "--template", help="export template, can be 'txt'(default), 'html', 'json', or template file name", default="txt")
    parser.add_argument("-P", "--peer-print", help="set print name for the peer")
    parser.add_argument("-l", "--limit", help="limit the number of fetched messages and set the offset")
    parser.add_argument("-L", "--hardlimit", help="set a hard limit of the number of messages, must be used with -l", type=int, default=100000)
    parser.add_argument("-c", "--cachedir", help="the path of media files")
    parser.add_argument("-r", "--urlprefix", help="the url prefix of media files")
    parser.add_argument("peer", help="export certain peer id or tg-cli-style peer print name")
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
    peer = msg.peers.find(args.peer)
    if peer['id'] is None:
        raise KeyError('peer not found: %s' % args.peer)
    if args.output == '-':
        for ln in render_func(peer, args.peer_print):
            sys.stdout.write(ln)
    else:
        fn = args.output
        if args.output is None:
            fn = '%s#id%d' % (peer['type'], peer['id'])
            if args.template == 'json':
                fn += '.json'
            elif '.' in args.template:
                fn += os.path.splitext(args.template)[1]
            else:
                fn += '.' + args.template
        with open(fn, 'w') as f:
            for ln in render_func(peer, args.peer_print):
                f.write(ln)

if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
