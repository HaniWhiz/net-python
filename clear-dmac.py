from __future__ import annotations  # Enables X | None syntax in Python 3.7 - 3.9

import csv
import os
import signal
import sys
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Iterator, Optional

import paramiko
import yaml
from paramiko.channel import Channel

COMMAND = "clear mac address-table dynamic"
BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "switches.yaml"   # Jump server & default login settings
CSV_FILE = BASE_DIR / "switches.csv"       # Switch hostnames / IPs


def verify_ssh_agent() -> None:
    """Verify that ssh-agent is running and has at least one key (e.g., YubiKey) loaded."""
    agent = paramiko.Agent()
    try:
        keys = agent.get_keys()
        if not keys:
            print(f"{datetime.now()} ⚠️  WARNING: No keys found in SSH agent! Make sure your YubiKey is loaded (check 'ssh-add -L').")
        else:
            key_names = ", ".join(k.get_name() for k in keys)
            print(f"{datetime.now()} 🔐 SSH Agent active. Found {len(keys)} key(s): [{key_names}]")
    finally:
        agent.close()


def load_config() -> tuple[Optional[dict], list[tuple[str, dict]]]:
    """
    Read jump/auth defaults from switches.yaml and switch hostnames from switches.csv.
    Works whether switches.csv has a header row ('host', 'ip', 'hostname') or is just a plain column of IPs/hostnames.
    """
    config = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    defaults = config.get("auth") or {}

    if not CSV_FILE.exists():
        raise FileNotFoundError(f"CSV file not found: {CSV_FILE}")

    # utf-8-sig automatically strips any BOM added by Excel for Mac when saving CSVs
    with open(CSV_FILE, mode="r", encoding="utf-8-sig", newline="") as f:
        sample = f.read(2048)
        f.seek(0)
        try:
            # Handle comma, semicolon, or tab delimiters saved by Excel
            dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
        except csv.Error:
            dialect = csv.excel
        rows = list(csv.reader(f, dialect))

    if not rows:
        return config.get("jump"), []

    first_row = [cell.strip().lower() for cell in rows[0]]
    host_synonyms = ("host", "hostname", "ip", "switch", "device")

    # Detect if Row 1 is a header or the first switch
    if any(h in first_row for h in host_synonyms):
        headers = first_row
        data_rows = rows[1:]
    else:
        headers = ["host"]
        data_rows = rows

    switches: list[tuple[str, dict]] = []
    for row in data_rows:
        if not row or all(cell.strip() == "" for cell in row):
            continue

        row_dict: dict = {}
        for idx, header in enumerate(headers):
            if idx < len(row) and row[idx].strip() != "":
                val = row[idx].strip()
                if header in host_synonyms:
                    row_dict["host"] = val
                elif header == "port":
                    row_dict["port"] = int(float(val))
                elif header:
                    row_dict[header] = val

        host = row_dict.get("host")
        if host and not host.startswith("#"):  # Allow commenting out rows with '#'
            merged_auth = {**defaults, **row_dict}
            if not merged_auth.get("username"):
                raise ValueError(
                    f"Missing 'username' for host '{host}'. Set it in {CONFIG_FILE.name} under 'auth' or add a 'username' column in {CSV_FILE.name}."
                )
            switches.append((host, merged_auth))

    return config.get("jump"), switches


def open_ssh_client(host: str, auth: dict, sock=None) -> paramiko.SSHClient:
    """Open and return a single SSHClient connection. Cleans up immediately if connect fails."""
    raw_keys = auth.get("key_files") or []
    if isinstance(raw_keys, str):
        raw_keys = [raw_keys]
    key_files = [os.path.expanduser(k) for k in raw_keys]
    use_agent = auth.get("use_agent", True)

    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            host,
            port=int(auth.get("port", 22)),
            username=auth["username"],
            key_filename=key_files or None,             # 1st: key files from YAML (if any)
            passphrase=auth.get("passphrase") or None,  #      (and their passphrase)
            allow_agent=use_agent,                      # 2nd: keys in your SSH agent (YubiKey)
            password=auth.get("password") or None,      # 3rd: plain-text password, if set
            look_for_keys=False,                        # Don't scan ~/.ssh files automatically
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
            sock=sock,                                  # tunnel through the jump server, if any
        )
        transport = ssh.get_transport()
        if transport:
            transport.set_keepalive(10)
        return ssh
    except Exception:
        ssh.close()
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass
        raise


@contextmanager
def jump_session(jump_auth: Optional[dict]) -> Iterator[Optional[paramiko.SSHClient]]:
    """Context manager for the jump host. Guarantees logout when the script finishes or errors."""
    if not jump_auth:
        yield None
        return

    host = jump_auth["host"]
    print(f"{datetime.now()} 🔑 Logging in to jump server {host}")
    jump_client = open_ssh_client(host, jump_auth)
    try:
        yield jump_client
    finally:
        try:
            jump_client.close()
        except Exception:
            pass
        print(f"{datetime.now()} 🔒 Logged out of jump server {host}")


@contextmanager
def switch_shell(host: str, auth: dict, jump_client: Optional[paramiko.SSHClient]) -> Iterator[Channel]:
    """
    Context manager for a single switch SSH session.
    Opens the connection, yields the shell, and guarantees full VTY + TCP teardown on exit or error.
    """
    sock = None
    client: Optional[paramiko.SSHClient] = None
    chan: Optional[Channel] = None

    print(f"{datetime.now()} 🔑 Logging in to {host}")
    try:
        if jump_client is not None:
            transport = jump_client.get_transport()
            if transport is None or not transport.is_active():
                raise RuntimeError("Jump server connection is no longer active")
            sock = transport.open_channel(
                "direct-tcpip", (host, int(auth.get("port", 22))), ("127.0.0.1", 0), timeout=15
            )

        client = open_ssh_client(host, auth, sock=sock)
        chan = client.invoke_shell()
        chan.settimeout(30)
        time.sleep(1.0)
        if chan.recv_ready():
            chan.recv(999999)  # flush initial login banner

        yield chan

    finally:
        # 1. Gracefully exit Cisco VTY line so the switch doesn't hold an orphaned session
        if chan is not None:
            try:
                if not chan.closed:
                    chan.send("exit\n")
                    time.sleep(0.2)
            except Exception:
                pass
            try:
                chan.close()
            except Exception:
                pass

        # 2. Close the Paramiko SSH client
        if client is not None:
            try:
                client.close()
            except Exception:
                pass

        # 3. Close the jump-host direct-tcpip forwarding channel (if used)
        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

        print(f"{datetime.now()} 🔒 Logged out of {host}")


def send_cmd(chan: Channel, cmd: str, wait: float = 1.0) -> str:
    """Send a command to the active switch channel and return the output."""
    chan.send(cmd + "\n")
    time.sleep(wait)
    output = ""
    while chan.recv_ready():
        output += chan.recv(999999).decode(errors="ignore")
    return output


def clear_mac_table(chan: Channel, host: str) -> None:
    """Run 'clear mac address-table dynamic' and verify the prompt."""
    output = send_cmd(chan, COMMAND, wait=3.0)
    # Handle IOS prompts that ask for [confirm] if any exist, though dynamic usually doesn't
    if "[confirm]" in output.lower():
        output += send_cmd(chan, "", wait=2.0)

    if "%" in output or not output.strip().endswith("#"):
        print(f"{datetime.now()} ❌ {host} did not accept '{COMMAND}':\n{output.strip()}")
    else:
        print(f"{datetime.now()} ✅ Dynamic MAC address table cleared on {host}")


def _handle_sigterm(signum, frame):
    # Raising SystemExit triggers all active 'finally:' blocks in the context managers!
    print(f"\n{datetime.now()} 🛑 Termination signal received. Closing active SSH session...")
    sys.exit(1)


signal.signal(signal.SIGINT, _handle_sigterm)
signal.signal(signal.SIGTERM, _handle_sigterm)


def main() -> None:
    verify_ssh_agent()

    try:
        jump_auth, switches = load_config()
    except Exception as e:
        print(f"{datetime.now()} 📢 Configuration error: {type(e).__name__}: {e}")
        sys.exit(1)

    if not switches:
        print(f"{datetime.now()} ⚠️  No switches found in {CSV_FILE.name}. Exiting.")
        return

    try:
        with jump_session(jump_auth) as jump_client:
            for host, auth in switches:
                try:
                    with switch_shell(host, auth, jump_client) as chan:
                        clear_mac_table(chan, host)
                except Exception as e:
                    print(f"{datetime.now()} 📢 {host} failed: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"{datetime.now()} 📢 Jump server failed: {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"{datetime.now()} 🏁 All switches processed. All SSH sessions closed.")


if __name__ == "__main__":
    main()
