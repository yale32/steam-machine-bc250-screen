# Contributing

Thanks for taking a look. Issues and pull requests are welcome — especially ports to other Turing
screen revisions, and fixes for BC-250 kernel quirks.

## You do not need the hardware

The layout renders headlessly, so most changes can be developed and reviewed on any Linux machine:

```bash
./setup.sh
.venv/bin/python monitor.py --preview /tmp/preview.png   # one frame to a PNG
.venv/bin/python monitor.py --preview-splash /tmp/splash.png
```

Readings the host cannot provide (amdgpu VRAM, KFD compute units, the BC-250's hwmon sensors) show
as `--` rather than failing, so a preview from a non-BC-250 machine will have gaps. That is
expected — it still exercises the full layout code.

CI runs exactly this render on every push, so a change that breaks drawing fails fast.

## Where things live

| File | Responsibility |
|---|---|
| `hoststats.py` | All sysfs / procfs reading. No drawing. Every reader degrades to `None` instead of raising. |
| `monitor.py` | Layout, the RevA screen protocol, dirty-tile diffing, splash, white balance. No data collection. |

Keeping that split intact is the main review criterion: a new stat belongs in `hoststats.py`, and a
new panel in `monitor.py`.

## Style

- `ruff check .` must pass (config in `pyproject.toml`); CI enforces it.
- Match the surrounding code: module docstrings explaining *why*, comments on the non-obvious sysfs
  behaviour, and no new runtime dependencies without a good reason.
- New readers must fail soft. A missing sysfs file, a permission error or an unparseable value
  should return `None`, never raise — the screen is a background service and must not crash on a
  kernel that exposes something slightly differently.

## Testing a change on real hardware

```bash
systemctl --user stop bc250-screen    # only one program can use the screen at a time
.venv/bin/python monitor.py
```

Ctrl-C stops cleanly and turns the screen off. Then `systemctl --user start bc250-screen` to put the
service back.

## Reporting a bug

Please include your board and screen revision, `uname -r`, the output of
`journalctl --user -u bc250-screen -n 50`, and — if the problem is a wrong or missing reading —
the relevant sysfs path and its contents. The issue template asks for these.
