#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import logging
import argparse

import tgcli

logging.basicConfig(stream=sys.stdout, format='%(asctime)s [%(levelname)s] %(message)s', level=logging.INFO)

def export_avatar_peer(tc, peertype, pid, filename):
    peername = '%s#id%d' % (peertype, pid)
    if os.path.isfile(filename):
        logging.info('Avatar exists: ' + peername)
        return
    res = getattr(tc, 'cmd_load_%s_photo' % peertype)(peername)
    if 'result' in res and res['result'] != 'FAIL':
        os.rename(res['result'], filename)
        logging.info('Exported avatar for %s' % peername)
    else:
        logging.warning('Failed to export avatar for %s: %s' % (peername, res))

def export_avatar_group(tc, grouptype, pid, path):
    peername = '%s#id%d' % (grouptype, pid)
    members = {}
    logging.info('Fetching info for %s' % peername)
    if grouptype == 'channel':
        items = tc.cmd_channel_get_members(peername, 100)
        for item in items:
            members[item['peer_id']] = item
        dcount = 100
        while items:
            items = tc.cmd_channel_get_members(peername, 100, dcount)
            for item in items:
                members[item['peer_id']] = item
            dcount += 100
    else:
        obj = tc.cmd_chat_info(peername)
        for item in obj['members']:
            members[item['peer_id']] = item
    for key in members:
        export_avatar_peer(tc, 'user', key, os.path.join(path, '%d.jpg' % key))

def main(argv):
    parser = argparse.ArgumentParser(description="Export Telegram messages.")
    parser.add_argument("-o", "--output", help="output path", default="export")
    parser.add_argument("-g", "--group", help="export every user's avatar in a group or channel", action='store_true')
    parser.add_argument("-t", "--type", help="peer type, can be 'user', 'chat', 'channel'", default="user")
    parser.add_argument("-i", "--id", help="peer id", type=int)
    parser.add_argument("-e", "--tgbin", help="Telegram-cli binary path", default="bin/telegram-cli")
    args = parser.parse_args(argv)

    with tgcli.TelegramCliInterface(args.tgbin, run=False) as tc:
        tc.cmd_dialog_list()
        if not os.path.isdir(args.output):
            os.mkdir(args.output)
        if args.group:
            export_avatar_group(tc, args.type, args.id, args.output)
        else:
            export_avatar_peer(tc, args.type, args.id, os.path.join(args.output, '%s%d.jpg' % (args.type, args.id)))

if __name__ == '__main__':
    main(sys.argv[1:])
