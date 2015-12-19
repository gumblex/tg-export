# tg-export
Export Telegram messages.

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

## logfmt.py

```
usage: logfmt.py [-h] [-o OUTPUT] [-d DB] [-b BOTDB] [-D BOTDB_DEST] [-u]
                 [-t TYPE] [-p PEER] [-r RANGE]

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
  -t TYPE, --type TYPE  export type, can be 'txt'(default), 'html'
  -p PEER, --peer PEER  export certain peer id
  -r RANGE, --range RANGE
                        message range in slice format
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
