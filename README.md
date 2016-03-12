# tg-export
Export Telegram messages, using [telegram-cli](https://github.com/vysheng/tg).

This version (v3) is compatible with `vysheng/tg/master` AND `vysheng/tg/test`
branches.

**Note**: The database format of this version (v3) is not compatible with the old ones.
To convert old databases (v1 or v2), run `python3 dbconvert.py [old.db [new.db]]`

## export.py

```
$ python3 export.py -h
usage: export.py [-h] [-o OUTPUT] [-d DB] [-f] [-l] [-e TGBIN]

Export Telegram messages.

optional arguments:
  -h, --help            show this help message and exit
  -o OUTPUT, --output OUTPUT
                        output path
  -d DB, --db DB        database path
  -f, --force           force download all messages
  -l, --logging         logging mode (keep running)
  -e TGBIN, --tgbin TGBIN
                        telegram-cli binary path
```

**Lots** of workaround about the unreliability of tg-cli is included (in this script and `tgcli.py`), so the script itself may be unreliable as well.

Common problems with tg-cli are:
* Dies arbitrarily.
* No response in the socket interface.
* Slow response in the socket interface.
* Half response in the socket interface, while the another half appears after the timeout.
* Returns an empty array when actually there are remaining messages.

Which is called NO WARRANTY™.

## logfmt.py

This script can process database written by `export.py` or [tg-chatdig](https://github.com/gumblex/tg-chatdig), and write out a human-readable format (txt, html, etc.) according to a jinja2 template.

```
usage: logfmt.py [-h] [-o OUTPUT] [-d DB] [-b BOTDB] [-D BOTDB_DEST] [-u]
                 [-t TEMPLATE] [-p PEER] [-P PEER_PRINT] [-l LIMIT]
                 [-L HARDLIMIT] [-c CACHEDIR] [-r URLPREFIX]

Format exported database file into human-readable format.

optional arguments:
  -h, --help            show this help message and exit
  -o OUTPUT, --output OUTPUT
                        output path
  -d DB, --db DB        tg-export database path
  -b BOTDB, --botdb BOTDB
                        tg-chatdig bot database path
  -D BOTDB_DEST, --botdb-dest BOTDB_DEST
                        tg-chatdig bot logged chat id
  -u, --botdb-user      use user information in tg-chatdig database first
  -t TEMPLATE, --template TEMPLATE
                        export template, can be 'txt'(default), 'html',
                        'json', or template file name
  -p PEER, --peer PEER  export certain peer id
  -P PEER_PRINT, --peer-print PEER_PRINT
                        set print name for the peer
  -l LIMIT, --limit LIMIT
                        limit the number of fetched messages and set the
                        offset
  -L HARDLIMIT, --hardlimit HARDLIMIT
                        set a hard limit of the number of messages, must be
                        used with -l
  -c CACHEDIR, --cachedir CACHEDIR
                        the path of media files
  -r URLPREFIX, --urlprefix URLPREFIX
                        the url prefix of media files
```

## tgcli.py
Simple wrapper for telegram-cli interface.

Example:
```python
tgcli = TelegramCliInterface('../tg/bin/telegram-cli')
dialogs = tgcli.cmd_dialog_list()
```

### TelegramCliInterface(cmd, extra_args=(), run=True)

 * `run()` starts the subprocess, needed when object created with `run=False`.
 * `send_command(cmd, timeout=180, resync=True)` sends a command to tg-cli. use `resync` for consuming text since last timeout.
 * `cmd_*(*args, **kwargs)` is the convenience method to send a command and get response. `args` are for the command, `kwargs` are arguments for `TelegramCliInterface.send_command`.
 * `on_info(text)`(callback) is called when a line of text is printed on stdout.
 * `on_json(obj)`(callback) is called with the interpreted object when a line of json is printed on stdout.
 * `on_text(text)`(callback) is called when a line of anything is printed on stdout.
 * `on_start()`(callback) is called after telegram-cli starts.
 * `on_exit()`(callback) is called after telegram-cli dies.
 * `close()` properly ends the subprocess.

`do_nothing()` function does nothing. (for callbacks)

## License

Now it's LGPLv3+.
