"""Clear ARP entries on Cisco Nexus switches and report the result.

Run from Terminal with:  python3 arp_clear_v2.py
"""

import getpass
import logging
import os
import sys
import time
from pathlib import Path

from netmiko import ConnectHandler
from netmiko.exceptions import (
    NetmikoAuthenticationException,
    NetmikoTimeoutException,
    ReadTimeout,
)

# ---- Settings you may need to change ----------------------------------------

# The switches to clear. Test with one IP first, e.g. ["10.0.0.11"].
SWITCHES = ["10.0.0.11", "10.0.0.12"]

# The clear command. Confirm its syntax on your switch with: clear ip arp ?
CLEAR_CMD = "clear ip arp"

# Command used to count ARP entries before and after clearing.
COUNT_CMD = "show ip arp summary"

# Seconds to wait between switches.
PAUSE_SECONDS = 5

# The log file is saved next to this script, whatever folder you run it from.
LOG_FILE = Path(__file__).resolve().parent / "arp_clear.log"


# ---- Logging: show messages on screen and save them to the log file ---------

log = logging.getLogger("arp_clear")
log.setLevel(logging.INFO)
formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")

console = logging.StreamHandler(sys.stdout)
console.setFormatter(formatter)
log.addHandler(console)

file_handler = logging.FileHandler(LOG_FILE)
file_handler.setFormatter(formatter)
log.addHandler(file_handler)


class CommandRejected(Exception):
    """The switch returned an error message instead of running the command."""


def one_line(text):
    """Turn multi-line switch output into a single line for the log."""
    return " | ".join(line.strip() for line in text.splitlines() if line.strip())


def run(conn, command):
    """Send one command and raise an error if the switch rejected it.

    NX-OS reports problems as text starting with '%'. Netmiko does not raise
    an error for these, so we check the output ourselves.
    """
    output = conn.send_command(command, read_timeout=30)
    if any(line.lstrip().startswith("%") for line in output.splitlines()):
        raise CommandRejected(f"'{command}' rejected: {one_line(output)}")
    return output


def clear_arp(host, username, password):
    """Log in, count ARP entries, clear them, count again, then log out."""
    # Leaving this with-block logs out of the switch, even if an error happens.
    with ConnectHandler(
        device_type="cisco_nxos",
        host=host,
        username=username,
        password=password,
        conn_timeout=15,
    ) as conn:
        before = run(conn, COUNT_CMD)
        run(conn, CLEAR_CMD)
        after = run(conn, COUNT_CMD)
    log.info("%s cleared. before=%s | after=%s", host, one_line(before), one_line(after))


def get_credentials():
    """Use NET_USERNAME and NET_PASSWORD if they are set, otherwise ask for them."""
    username = os.environ.get("NET_USERNAME") or input("Switch username: ")
    password = os.environ.get("NET_PASSWORD") or getpass.getpass("Switch password: ")
    return username, password


def main():
    username, password = get_credentials()
    failures = 0

    for i, host in enumerate(SWITCHES):
        if i > 0:
            time.sleep(PAUSE_SECONDS)  # don't clear every switch at the same instant
        try:
            clear_arp(host, username, password)
        except CommandRejected as exc:
            failures += 1
            log.error("%s failed: %s", host, exc)
        except NetmikoAuthenticationException:
            failures += 1
            log.error("%s failed: wrong username or password", host)
        except (NetmikoTimeoutException, ReadTimeout):
            failures += 1
            log.error("%s failed: timed out (switch unreachable or SSH disabled)", host)
        except Exception:
            failures += 1
            log.exception("%s failed: unexpected error", host)

    log.info("Finished: %d of %d switches failed", failures, len(SWITCHES))
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())