# tg-export
Export Telegram messages.

## tgcli.py
Simple wrapper for telegram-cli interface.

Example:
```python
tgcli = TelegramCliInterface('../tg/bin/telegram-cli')
dialogs = tgcli.cmd_dialog_list()
```

* `TelegramCliInterface(cmd, extra_args=(), run=True)`

 * `run()` starts the subprocess, needed when object created with `run=False`.

 * `send_command(cmd, timeout=180, resync=True)` sends a command to tg-cli. use `resync` for consuming text since last timeout.

 * `cmd_*(*args, **kwargs)` is the convenience method to send a command and get response. `args` are for the command, `kwargs` are arguments for `TelegramCliInterface.send_command`.

 * `on_info(text)`(callback) is called when a line of text is printed on stdout.
 * `on_json(obj)`(callback) is called with the interpreted object when a line of json is printed on stdout.
 * `on_text(text)`(callback) is called when a line of anything is printed on stdout.
 * `on_start()`(callback) is called after telegram-cli starts.
 * `on_exit()`(callback) is called after telegram-cli dies.

 * `close()` properly ends the subprocess.

* `do_nothing()` function does nothing. (for callbacks)
