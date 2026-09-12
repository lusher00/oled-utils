# oled-utils

A status display for an I2C SSD1306/SH1106 panel on a Linux SBC, and the
diagnostic that tells you why it is dark.

`oled_status.py` is one file with no configuration required. At startup it
works out the board, which I2C bus the panel is on, its address, a usable
monospace font, where the CPU temperature lives, the default-route interface,
and which of the services it knows about are installed. The same file runs on a
Raspberry Pi 5 and a BeagleBone Blue.

    ./oled_status.py                  run it
    ./oled_status.py --probe          print everything it detected, exit
    ./oled_status.py --simulate       draw to the terminal, no hardware
    ./oled_status.py --simulate --png /tmp/f.png     one PNG per page
    sudo ./oled_status.py --install   write + enable a systemd unit
    sudo ./oled_status.py --uninstall

`oled_check.py` answers the one question this hardware keeps asking: *systemctl
says the service is active, so why is the screen dark?* It walks the chain one
link at a time — unit installed (and exactly one of them), bus node present,
service user able to open it, something ACKing at the address, libraries
importable **as that user inside the unit's sandbox**, display initialising,
display accepting a draw — and stops at the first link that is actually broken.

    sudo ./oled_check.py
    sudo ./oled_check.py --draw       also put a test pattern up

## Pages

    BOT   IP, the primary service's state, battery       (the pinned page)
    NET   hostname, IP, SSID + RSSI or interface
    SYS   CPU and load, temperature and throttle flags, memory and disk
    SVC   every watched unit's state, battery or board power

The pinned page is dealt back in between the others, so whatever the cycle is
doing you are never more than one dwell away from the bot's state. `--pin none`
turns that off, `--pages bot,sys` narrows the set, `--page-sec` sets the dwell.

## The layout, and the two-colour panels

The usual 128x64 modules are not one panel. Rows 0–15 are a physically separate
yellow segment with a dead gap beneath them. Lay out four rows on a 12px pitch
from y=0 — which is the obvious thing to do, and what this did for a year — and
the second row lands square on that seam: the top third of those glyphs prints
yellow, the bottom two thirds blue, split by the gap. It reads as a doubled,
half-erased line, and nothing about the data explains it, because the data was
always fine.

So: the header bar is sized to fill the band exactly, the body starts below the
gap, and **nothing is ever drawn across row 16**. If your panel's segment is a
different height, `--banner-h N` moves the line; `--no-header` drops the bar and
uses plain rows for a single-colour panel.

Everything else in the layout is measured rather than assumed — the value
column comes from the widest label on that page, not from labels padded with
spaces, which line up only while the font is monospaced. The font is whichever
of a dozen candidate paths the image happens to have, and one of them is not.

Each page is built against the row budget the panel actually has, so a 128x32
gets a terser version of a page rather than the 128x64 version sliced in half.

## Installing as a service

`--install` writes `/etc/systemd/system/oled-status.service` and enables it. The
unit is hardened (`ProtectSystem=strict`, `NoNewPrivileges`, no device access
beyond `char-i2c`) and it refuses to install over another unit that already
drives a panel, because two of those on one bus is a dead screen with no error
anywhere.

`SupplementaryGroups=i2c gpio` lists both names on purpose. Most images ship
`/dev/i2c-*` as `root:i2c`; the BeagleBone image ships it `root:gpio`, and a
unit granting only `i2c` starts fine and then exits 1 on first bus access,
which systemd reports as a restart loop with no hint of a permission problem.
`usermod -aG` is not sufficient: systemd starts a service with exactly the
groups the unit names.

The service publishes its own status to `$RUNTIME_DIRECTORY/status.json`, which
systemd creates on start and removes on stop. That file exists because a
display loop that connected once and has dropped every frame since still looks
green to systemd — and because a stopped service must not leave a stale "ok"
behind for a dashboard to believe.

## Dependencies

    sudo apt install -y python3-pil i2c-tools
    pip3 install luma.oled smbus2        # or: apt install python3-luma.oled

`--simulate` needs only Pillow.

## Relationship to balance_bot

`balance_bot/oled-utils/` carries a byte-identical vendored copy of
`oled_status.py`, installed there as `/usr/local/bin/bbb_oled.py` under the
`bbb_oled` unit so the bot's existing unit, `/etc/default/bbb_oled` and
dashboard paths keep working. This directory is upstream; sync with:

    cmp oled_status.py ~/balance_bot/oled-utils/oled_status.py

`--i2c-port` and `--batt-path` are accepted as aliases of `--i2c-bus` and
`--battery` for exactly that reason — an existing `/etc/default/bbb_oled`
written for the old script still parses.

`attic/` holds the original BeagleBone-only script this grew out of. It is kept
for reference and is not installed by anything.

## License

PolyForm Noncommercial 1.0.0 — see LICENSE. Commercial use requires a separate
license from the author: ryan.lush@gmail.com
