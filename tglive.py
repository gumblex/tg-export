#!/usr/bin/env python3
# -*- coding: utf-8 -*-

'''
Proof-of-concept telegram message live broadcasting with
[live-danmaku-hime](https://github.com/m13253/live-danmaku-hime)
'''

import sys
import time
import tgcli
import jinja2
import logging
import textwrap

logging.basicConfig(stream=sys.stderr,
    format='%(asctime)s [%(levelname)s] %(message)s', level=logging.DEBUG)

txt_template = '''[{{ msg.date|strftime('%H:%M') }} {{ msg.to.print_name[:8] }}] {{ msg.from.print_name }}{% if 'fwd_from' in msg %} [Fwd: {{ msg.fwd_from.print_name }}]
{%- elif 'reply_id' in msg %} [Re]
{%- endif %} >{% if msg.text %} {{ msg.text }}{% endif %}{% if msg.media %} [{{ msg.media.type }}]{% endif %}{% if msg.service %} [{{ msg.action.type }}]{% endif %}'''

jinjaenv = jinja2.Environment(loader=jinja2.DictLoader({'txt': txt_template}))
jinjaenv.filters['strftime'] = lambda date, fmt='%Y-%m-%d %H:%M:%S': time.strftime(fmt, time.localtime(date))

template = jinjaenv.get_template('txt')

WIDTH = 35

def print_msg(msg):
    logging.debug(msg)
    try:
        if msg.get('event') in ('message', 'service'):
            s = template.render(msg=msg).strip()
            s = '\n'.join(textwrap.wrap(s, WIDTH)) + '\n'
            sys.stdout.write(s)
            sys.stdout.flush()
    except Exception:
        logging.exception('Failed to process a message.')


with tgcli.TelegramCliInterface(sys.argv[1]) as c:
    c.on_json = print_msg
    for ln in sys.stdin:
        l = ln.strip()
        if l == 'q':
            break
        elif l.isdigit():
            WIDTH = int(l)
