#!/usr/bin/env python3
# SPDX-License-Identifier: PolyForm-Noncommercial-1.0.0
# Copyright (c) 2025-2026 Ryan Lush <ryan.lush@gmail.com>
#
# This file is part of balance_bot, licensed under the PolyForm
# Noncommercial License 1.0.0. You may use, study, modify, and share
# it for any noncommercial purpose. Commercial use requires a separate
# license from the author -- contact ryan.lush@gmail.com.
# Full license text: see the LICENSE file in the project root, or
# https://polyformproject.org/licenses/noncommercial/1.0.0/

"""
oled_check.py — why is the OLED service running but the screen dark?

Run on the bot:

    sudo ./oled-utils/oled_check.py
    sudo ./oled-utils/oled_check.py --draw      also put a test pattern up

"systemctl says active but nothing is on the display" has about eight causes and
they are indistinguishable from the outside. This walks the chain one link at a
time and stops at the first thing that is actually broken:

    1. is a bbb_oled unit installed, and is exactly ONE of them?
    2. does the I2C bus device node exist?
    3. can the service's user open it?
    4. does anything ACK at the configured address?
    5. are the Python libraries importable AS THAT USER?
    6. does the display initialise?
    7. does it accept a draw?

Step 1 exists because the service was renamed from bbb-oled to bbb_oled. Two
units both driving one I2C display is a genuine failure mode and looks exactly
like a dead screen.
"""
import argparse, glob, grp, json, os, pwd, re, subprocess, sys

OK, BAD, WARN = "  \033[1;32m ok \033[0m", "  \033[1;31mFAIL\033[0m", "  \033[33mwarn\033[0m"
UNIT_DIR = "/etc/systemd/system"


def say(state, msg, fix=None):
    print(f"{state}  {msg}")
    if fix:
        for line in fix.strip().splitlines():
            print(f"        {line.strip()}")


def run(cmd):
    try:
        return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout
    except Exception:
        return ""


def i2c_scan(port):
    """Addresses that ACK on one bus, or None if i2cdetect is unavailable.

    Parses the fixed-width grid by SLOT POSITION. Indexing a row by
    (addr & 0x0F) is wrong for row 00:, which is printed with its first three
    cells omitted, and regex-matching every two-hex token is worse -- it picks
    up the ROW LABELS (00 10 20 ...) and reports an empty bus as eight devices.
    """
    scan = run(["i2cdetect", "-y", "-r", str(port)])
    if not scan:
        return None
    present = []
    for l in scan.splitlines():
        m = re.match(r"^([0-9a-f]{2}):(.*)$", l)
        if not m:
            continue
        base = int(m.group(1), 16)
        body = m.group(2)
        for i in range(16):
            cell = body[i * 3:i * 3 + 3].strip()
            if cell and cell not in ("--", "UU"):
                present.append(base + i)
    return present


def all_buses():
    """[(port, [addrs]), ...] for every /dev/i2c-N, lowest first."""
    out = []
    for dev in sorted(glob.glob("/dev/i2c-*"),
                      key=lambda p: int(p.rsplit("-", 1)[1])):
        try:
            p = int(dev.rsplit("-", 1)[1])
        except ValueError:
            continue
        out.append((p, i2c_scan(p)))
    return out


def unit_setting(path, key):
    try:
        for line in open(path):
            if line.startswith(key + "="):
                return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
            formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--draw", action="store_true", help="draw a test pattern if it opens")
    ap.add_argument("--port", type=int, default=None, help="override I2C bus number")
    ap.add_argument("--addr", default=None, help="override I2C address, e.g. 0x3C")
    a = ap.parse_args()
    fail = False

    # ── 1. units ────────────────────────────────────────────────────────────
    units = sorted(set(glob.glob(f"{UNIT_DIR}/bbb?oled.service"))
                   | set(glob.glob(f"{UNIT_DIR}/*oled*.service")))
    if not units:
        say(BAD, "no OLED unit installed",
            """sudo ./oled-utils/install.sh          (on the bot: unit bbb_oled)
               sudo ./oled_status.py --install       (anywhere else: unit oled-status)""")
        return 1
    if len(units) > 1:
        say(BAD, f"TWO units installed: {', '.join(os.path.basename(u) for u in units)}",
            """Both will start and both will drive the same I2C display, and the
               result looks exactly like a dead panel. Keep one; for the old
               hyphenated name that is:
                 sudo systemctl disable --now bbb-oled
                 sudo rm /etc/systemd/system/bbb-oled.service
                 sudo systemctl daemon-reload""")
        fail = True
    unit = units[-1]
    name = os.path.basename(unit).replace(".service", "")
    say(OK, f"unit: {os.path.basename(unit)}")

    active = run(["systemctl", "is-active", name]).strip()
    say(OK if active == "active" else BAD, f"{name} is {active or 'unknown'}")

    # ── 2. what is it configured to talk to? ────────────────────────────────
    env_file = (unit_setting(unit, "EnvironmentFile") or "").lstrip("-")
    args = ""
    if env_file and os.path.exists(env_file):
        for line in open(env_file):
            if line.startswith("OLED_ARGS"):
                args = line.split("=", 1)[1].strip().strip('"')
    port = a.port
    addr = a.addr
    if port is None:
        m = re.search(r"--i2c-(?:port|bus)\s+(\d+)", args)
        # No bus in OLED_ARGS is not a misconfiguration any more: the script
        # scans every /dev/i2c-* when it is not told one. Check bus 1 anyway,
        # since that is what it finds on this board.
        port = int(m.group(1)) if m else 1
    if addr is None:
        m = re.search(r"--i2c-addr\s+(\S+)", args)
        addr = m.group(1) if m else "0x3C"
    addr_i = int(addr, 0)
    say(OK, f"configured: bus i2c-{port}, address {addr_i:#04x}"
            f"{'  (from ' + env_file + ')' if args else '  (defaults — no OLED_ARGS set)'}")

    # ── 3. bus node ─────────────────────────────────────────────────────────
    dev = f"/dev/i2c-{port}"
    if not os.path.exists(dev):
        say(BAD, f"{dev} does not exist",
            f"""The unit has ConditionPathExists={dev}, so systemd will report the
                service as active-but-skipped and you get no error. Enable the I2C
                overlay in /boot/uEnv.txt, or point --i2c-port at a bus that exists:
                  ls /dev/i2c-*""")
        return 1
    st = os.stat(dev)
    grp_name = grp.getgrgid(st.st_gid).gr_name
    say(OK, f"{dev} exists, owner {pwd.getpwuid(st.st_uid).pw_name}:{grp_name}, "
            f"mode {oct(st.st_mode & 0o777)}")

    # ── 4. can the SERVICE USER open it? ────────────────────────────────────
    run_user = unit_setting(unit, "User") or "root"
    # Check membership of the group that ACTUALLY owns the device node, not a
    # hardcoded "i2c". On this board /dev/i2c-1 is root:gpio, so a check written
    # against "i2c" silently passed a user who could not open the bus at all --
    # which is exactly the false pass that hid this fault.
    try:
        members = set(grp.getgrnam(grp_name).gr_mem)
    except KeyError:
        members = set()
    world_rw = bool(st.st_mode & 0o006)
    if run_user == "root" or world_rw:
        say(OK, f"service user '{run_user}' can open the bus")
    elif run_user in members:
        say(OK, f"service user '{run_user}' is in group '{grp_name}'")
    else:
        say(BAD, f"service runs as '{run_user}', which is NOT in group '{grp_name}'",
            f"""{dev} is owned by root:{grp_name} mode {oct(st.st_mode & 0o777)}, so only
                root and members of '{grp_name}' can open it.
                  sudo usermod -aG {grp_name} {run_user}
                The unit must ALSO grant it, or systemd drops the supplementary
                groups. In {os.path.basename(unit)}:
                  SupplementaryGroups={grp_name}
                then: sudo systemctl daemon-reload && sudo systemctl restart {name}""")
        fail = True

    # And the unit's own SupplementaryGroups must include it -- being in the
    # group as a login user is not enough for a systemd service.
    sup = unit_setting(unit, "SupplementaryGroups") or ""
    if run_user != "root" and grp_name not in sup.split():
        say(BAD, f"unit grants SupplementaryGroups={sup or '(none)'}, "
                 f"which does not include '{grp_name}'",
            f"""systemd starts the service with exactly the groups listed here;
                adding the user to '{grp_name}' with usermod is NOT enough.
                  SupplementaryGroups={(sup + ' ' + grp_name).strip()}""")
        fail = True
    elif run_user != "root":
        say(OK, f"unit grants SupplementaryGroups={sup}")

    # ── 5. does anything answer at that address? ────────────────────────────
    present = i2c_scan(port)
    if present is None:
        say(WARN, "i2cdetect not available — skipping the bus scan",
            "sudo apt install i2c-tools")
    elif addr_i in present:
        say(OK, f"a device ACKs at {addr_i:#04x}")
    else:
        others = " ".join(f"{x:02x}" for x in sorted(set(present))) or "NOTHING"
        say(BAD, f"nothing answers at {addr_i:#04x} on i2c-{port}"
                 f"  (this bus has: {others})")

        # An empty configured bus is a dead end on its own: it cannot tell
        # "panel unplugged" from "panel is on a different bus". The BeagleBone
        # Blue has several, and which header maps to which number is not
        # obvious. So sweep them all and let the answer name itself.
        say(WARN, "sweeping every I2C bus for an SSD1306 ...")
        panel_at = []
        for p, addrs in all_buses():
            if addrs is None:
                continue
            shown = " ".join(f"{x:02x}" for x in sorted(set(addrs))) or "(empty)"
            hit = [x for x in addrs if x in (0x3C, 0x3D)]
            print(f"        i2c-{p}: {shown}{'   <-- SSD1306 address' if hit else ''}")
            for x in hit:
                panel_at.append((p, x))

        if panel_at:
            p, x = panel_at[0]
            say(BAD, f"the panel looks like it is on i2c-{p} at {x:#04x}, "
                     f"not i2c-{port} at {addr_i:#04x}",
                f"""Point the service at it, in {env_file or '/etc/default/bbb_oled'}:
                      OLED_ARGS="--i2c-port {p} --i2c-addr {x:#04x} --service balance_bot"
                    then: sudo systemctl restart {name}
                    The unit also has ConditionPathExists=/dev/i2c-{port}; change that
                    to /dev/i2c-{p} in oled-utils/bbb_oled.service and reinstall, or
                    systemd will skip the service entirely when that node is absent.""")
        else:
            say(BAD, "no SSD1306 answers on ANY bus",
                """Nothing at 0x3C or 0x3D anywhere means the panel is not talking at
                   all, which is power or wiring, not configuration:
                     - 3.3V and GND at the module (measure at the module, not the
                       header -- a broken jumper reads fine at the source)
                     - SDA and SCL not swapped
                     - some cheap panels need their address jumper bridged; an
                       unbridged floating jumper answers at neither address
                     - the Blue's GH1.25 connectors are physically identical and
                       adjacent; a panel in the UART, CAN or GPS shell fits
                       perfectly and is on no I2C bus at all
                   If a bus above shows devices, that bus is electrically fine, so
                   the fault is on the panel side of the connector.""")
        fail = True

    # ── 6. libraries, AS THE SERVICE USER AND INSIDE THE UNIT'S SANDBOX ─────
    # Asking "can debian import luma?" in a login shell is the wrong question
    # and this check used to get it wrong. `pip3 install --user` puts the
    # modules in /home/debian/.local, and the unit sets ProtectHome=yes, which
    # replaces /home with an empty directory for the service. So the import
    # succeeds for you at a prompt and fails for systemd, and the checker
    # cheerfully passed a service that was dying on ImportError every 5s.
    #
    # So: resolve WHERE each module lives, then ask whether the sandbox can see
    # that path.
    probe = (
        "import importlib.util as u, json\n"
        "o = {}\n"
        "for n in ('luma.oled', 'PIL'):\n"
        "    try:\n"
        "        s = u.find_spec(n)\n"
        "    except Exception:\n"
        "        s = None\n"
        "    p = None\n"
        "    if s is not None:\n"
        "        p = s.origin\n"
        "        if not p and s.submodule_search_locations:\n"
        "            p = list(s.submodule_search_locations)[0]\n"
        "    o[n] = p\n"
        "print(json.dumps(o))\n")
    # Run it the way SYSTEMD will resolve imports, not the way a login shell
    # does. With ProtectHome=yes the service gets an empty /home, so ~/.local is
    # gone -- PYTHONNOUSERSITE=1 reproduces that view. Checking the login view
    # instead is what made this pass a service that was dying on ImportError.
    def probe_as(no_user_site):
        cmd = ["python3", "-c", probe]
        if no_user_site:
            cmd = ["env", "PYTHONNOUSERSITE=1"] + cmd
        if run_user != "root":
            cmd = ["runuser", "-u", run_user, "--"] + cmd
        return run(cmd)

    out = probe_as(True)
    login_out = probe_as(False)
    try:
        mods = json.loads(out.strip() or "{}")
    except ValueError:
        mods = {}

    missing = [n for n, p in mods.items() if not p]
    if not mods:
        say(WARN, f"could not probe modules as '{run_user}'")
    elif missing:
        say(BAD, f"missing Python modules for '{run_user}': {', '.join(missing)}",
            "Install them SYSTEM-WIDE, not with --user -- see the ProtectHome\n"
            "note below for why:\n"
            "  sudo pip3 install --break-system-packages luma.oled pillow")
        fail = True
    else:
        say(OK, f"luma.oled and pillow importable as '{run_user}'")

    # Where the login shell finds them, if that differs. Purely informational
    # now -- a module that resolves in BOTH views is fine no matter which path
    # the shell happens to report, and treating a /home path as fatal was what
    # made this refuse to install a working setup.
    try:
        login_mods = json.loads(login_out.strip() or "{}")
    except ValueError:
        login_mods = {}
    shadowed = {n: p for n, p in login_mods.items()
                if p and p != mods.get(n)
                and (p.startswith("/home/") or p.startswith("/root/"))}
    if shadowed and not missing:
        for n, p in sorted(shadowed.items()):
            say(WARN, f"{n}: your shell uses {p}, the service uses {mods.get(n)}",
                f"""Two copies are installed and the per-user one shadows the system one
                    for you but not for the service. Harmless, but they can drift to
                    different versions, which makes "works in my shell" misleading:
                      sudo -u {run_user} pip3 uninstall -y luma.oled luma.core pillow""")

    # ── 7. actually open it ─────────────────────────────────────────────────
    if fail:
        print()
        print("  Stopping here — fix the FAIL lines above first.")
        return 1
    try:
        from luma.core.interface.serial import i2c
        from luma.oled.device import ssd1306
        serial = i2c(port=port, address=addr_i)
        device = ssd1306(serial, width=128, height=64)
        say(OK, "SSD1306 initialised")
        if a.draw:
            from luma.core.render import canvas
            with canvas(device) as draw:
                draw.rectangle((0, 0, 127, 63), outline=255)
                draw.text((6, 12), "OLED OK", fill=255)
                draw.text((6, 28), f"i2c-{port} {addr_i:#04x}", fill=255)
                draw.text((6, 44), "oled_check.py", fill=255)
            say(OK, "test pattern drawn — look at the panel")
            print()
            print("  If the panel is still dark with all of the above passing, the")
            print("  fault is the panel, its ribbon, or its supply — not software.")
        else:
            print()
            print("  Everything checks out. Re-run with --draw to put a pattern up.")
    except Exception as e:
        say(BAD, f"could not drive the display: {e}",
            f"""The bus and address are right and the libraries are present, so this
                is the panel or its wiring. Check 3.3V and GND at the module, and
                that SDA/SCL are not swapped.""")
        return 1

    print()
    print(f"  Recent service log:   journalctl -u {name} -n 30 --no-pager")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        print()
