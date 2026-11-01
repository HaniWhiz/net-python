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

COMMANDS = [
    "clear mac address-table dynamic",
    "configure terminal",
    "feature scheduler",
    "scheduler logfile size 1024",
    "scheduler job name MAC-CLEAR",
    "show clock >> bootflash:mac_clear_history.txt ; show mac address-table count >> bootflash:mac_clear_history.txt ; clear mac address-table dynamic ; show mac address-table count >> bootflash:mac_clear_history.txt",
    "exit",
    "scheduler schedule name MAC-CLEAR-DAILY",
    "job name MAC-CLEAR",
    "time daily 15:30",
    "exit",
    "end",
    "copy running-config startup-config",
]

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "switches.yaml"   # Jump server & default login settings
CSV_FILE = BASE_DIR / "bamdad.csv"       # Switch hostnames / IPs


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
    Read jump/auth defaults from switches.yaml and switch hostnames from switches.csv (or fallback to YAML hosts).
    Automatically sanitizes hosts (strips CIDR masks like /64 and ignores header rows).
    """
    config = {}
    if CONFIG_FILE.exists():
        with open(CONFIG_FILE, encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    defaults = config.get("auth") or {}

    raw_hosts = []
    if CSV_FILE.exists():
        with open(CSV_FILE, mode="r", encoding="utf-8-sig", newline="") as f:
            sample = f.read(2048)
            f.seek(0)
            try:
                dialect = csv.Sniffer().sniff(sample, delimiters=",;\t")
            except csv.Error:
                dialect = csv.excel
            reader = csv.reader(f, dialect)
            for row in reader:
                for cell in row:
                    if cell:
                        raw_hosts.append(cell.strip())
    else:
        for entry in config.get("hosts") or []:
            if isinstance(entry, str):
                raw_hosts.append(entry.strip())
            elif isinstance(entry, dict) and "host" in entry:
                raw_hosts.append(str(entry["host"]).strip())

    ignored_words = ("ip", "address", "hostname", "host", "switch", "device", "ip address")
    switches: list[tuple[str, dict]] = []

    for raw in raw_hosts:
        if not raw or raw.startswith("#"):
            continue

        # Strip CIDR mask (e.g., /64) if present
        host = raw.split("/")[0].strip()

        # Skip header labels
        if host.lower() in ignored_words or "ip address" in host.lower():
            continue

        merged_auth = {**defaults}
        if not merged_auth.get("username"):
            raise ValueError(
                f"Missing 'username' in {CONFIG_FILE.name}. Please set 'username' under the 'auth' section."
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
            key_filename=key_files or None,
            passphrase=auth.get("passphrase") or None,
            allow_agent=use_agent,
            password=auth.get("password") or None,
            look_for_keys=False,
            timeout=15,
            banner_timeout=15,
            auth_timeout=15,
            sock=sock,
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


def read_until_prompt(chan: Channel, timeout: float = 15.0) -> str:
    """
    Continuously read from the channel until a Cisco prompt ('#' or '>'), '[confirm]',
    or 'Destination filename' appears at the end of the output, or until timeout expires.
    """
    output = ""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if chan.recv_ready():
            chunk = chan.recv(99999).decode(errors="ignore")
            output += chunk
            stripped = output.strip()
            lower_stripped = stripped.lower()
            if (
                stripped.endswith("#")
                or stripped.endswith(">")
                or lower_stripped.endswith("[confirm]")
                or "destination filename" in lower_stripped.splitlines()[-1:]
            ):
                # Small pause to make sure no trailing bytes are still in flight
                time.sleep(0.1)
                if not chan.recv_ready():
                    break
        else:
            time.sleep(0.15)
    return output


@contextmanager
def jump_session(jump_auth: Optional[dict]) -> Iterator[Optional[paramiko.SSHClient]]:
    """Context manager for the jump host. Guarantees logout when done."""
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
    """Context manager for a single switch SSH session with strict cleanup."""
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

        # Wait dynamically for the initial login banner and prompt ('#' or '>')
        initial_output = read_until_prompt(chan, timeout=12.0)
        if not initial_output.strip().endswith(("#", ">")):
            # Send a newline to wake up the VTY prompt if the banner paused
            chan.send("\n")
            initial_output += read_until_prompt(chan, timeout=5.0)

        yield chan

    finally:
        if chan is not None:
            try:
                if not chan.closed:
                    # Send 'end' and 'exit' to ensure clean exit even if interrupted mid-config
                    chan.send("end\n")
                    time.sleep(0.1)
                    chan.send("exit\n")
                    time.sleep(0.2)
            except Exception:
                pass
            try:
                chan.close()
            except Exception:
                pass

        if client is not None:
            try:
                client.close()
            except Exception:
                pass

        if sock is not None:
            try:
                sock.close()
            except Exception:
                pass

        print(f"{datetime.now()} 🔒 Logged out of {host}")


def configure_mac_scheduler(chan: Channel, host: str) -> None:
    """Clear dynamic MAC table immediately, configure NX-OS daily scheduler, and save config."""
    # Flush any leftover characters before sending commands
    while chan.recv_ready():
        chan.recv(99999)

    for cmd in COMMANDS:
        # Saving config on Nexus switches can take 20-45 seconds
        cmd_timeout = 60.0 if cmd.startswith("copy ") else 15.0

        chan.send(cmd + "\n")
        output = read_until_prompt(chan, timeout=cmd_timeout)

        # Handle confirmation prompts if any appear
        lower_out = output.lower()
        if "[confirm]" in lower_out or "destination filename" in lower_out:
            chan.send("\n")
            output += read_until_prompt(chan, timeout=cmd_timeout)

        # Fallback if 'clear mac address-table dynamic' requires hyphenated syntax on older platforms
        if cmd == "clear mac address-table dynamic" and "% invalid" in output.lower():
            alt_cmd = "clear mac-address-table dynamic"
            chan.send(alt_cmd + "\n")
            output = read_until_prompt(chan, timeout=cmd_timeout)

        # Check for CLI error indicators (ignoring benign NX-OS '[#####]' progress bars)
        has_error = any(
            line.strip().startswith("%") or "% invalid" in line.lower() or "syntax error" in line.lower()
            for line in output.splitlines()
        )

        if has_error or not output.strip().endswith("#"):
            print(f"{datetime.now()} ❌ {host} rejected command '{cmd}':\n{output.strip()}")
            return

    print(f"{datetime.now()} ✅ Dynamic MAC table cleared & 'MAC-CLEAR-DAILY' scheduled (15:30) on {host}")


def _handle_sigterm(signum, frame):
    print(f"\n{datetime.now()} 🛑 Termination signal received. Closing active SSH sessions...")
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
        print(f"{datetime.now()} ⚠️  No valid switches found. Check your CSV or YAML file. Exiting.")
        return

    try:
        with jump_session(jump_auth) as jump_client:
            for host, auth in switches:
                try:
                    with switch_shell(host, auth, jump_client) as chan:
                        configure_mac_scheduler(chan, host)
                except Exception as e:
                    print(f"{datetime.now()} 📢 {host} failed: {type(e).__name__}: {e}")
    except Exception as e:
        print(f"{datetime.now()} 📢 Jump server failed: {type(e).__name__}: {e}")
        sys.exit(1)

    print(f"{datetime.now()} 🏁 All switches processed. All SSH sessions closed.")


if __name__ == "__main__":
    main()
