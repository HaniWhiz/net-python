import sys
# sys.path.append(r"C:\Users\4421184561\Anaconda3\Lib\site-packages")  # uncomment if paramiko isn't found
import os
import time
import signal
from datetime import datetime
from pathlib import Path

import paramiko
import yaml
from paramiko.channel import Channel

# ##################################################################
# ###                                                            ###
# ###   >>>  HOW OFTEN TO RUN  <<<    change this for testing    ###
# ###                                                            ###
# ##################################################################
RUN_EVERY = 24 * 60 * 60      # <<< 24h (normal)
# RUN_EVERY = 1 * 60          # <<< 1m  (test) - move the # to the line above to use it
# ##################################################################

CHECK_EVERY = 30              # the while loop wakes up every 30 seconds
COMMAND = "clear ip arp"
CONFIG_FILE = Path(__file__).resolve().parent / "switches.yaml"   # jump server, switches, logins

jump: paramiko.SSHClient | None = None     # connection to the jump server
client: paramiko.SSHClient | None = None   # connection to the current switch
chan: Channel | None = None                # shell on the current switch


def send(cmd, wait=1.0):
    if chan is None:
        raise RuntimeError("SSH channel is not connected")
    chan.send(cmd + "\n")
    time.sleep(wait)
    return chan.recv(999999).decode(errors="ignore")


def logout():
    """Log out of the current switch."""
    global client, chan
    if chan:
        chan.close()
    if client:
        client.close()
    client = None
    chan = None


def close_jump():
    """Log out of the jump server."""
    global jump
    if jump:
        jump.close()
        print(f"{datetime.now()} 🔒 Logged out of jump server")
    jump = None


def ctrl_c_handler(signum, frame):
    print("\n🛑 Ctrl+C detected")
    logout()
    close_jump()
    print("SSH disconnected")
    sys.exit(0)


signal.signal(signal.SIGINT, ctrl_c_handler)


def load_config():
    """Read switches.yaml. Returns (jump server settings or None, list of (switch, login settings))."""
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = yaml.safe_load(f) or {}
    defaults = config.get("auth") or {}
    switches = []
    for entry in config.get("hosts") or []:
        if isinstance(entry, str):                               # "- 10.0.0.11"
            entry = {"host": entry}
        switches.append((entry["host"], {**defaults, **entry}))  # switch settings win
    return config.get("jump"), switches


def ssh_connect(host, auth, sock=None):
    """Open one SSH connection with the login settings from the YAML."""
    key_files = [os.path.expanduser(k) for k in auth.get("key_files") or []]
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(
            host,
            port=auth.get("port", 22),
            username=auth["username"],
            key_filename=key_files or None,             # 1st: key files from the YAML
            passphrase=auth.get("passphrase") or None,  #      (and their passphrase)
            allow_agent=auth.get("use_agent", True),    # 2nd: keys in your SSH agent (YubiKey)
            password=auth.get("password") or None,      # 3rd: plain-text password, if set
            look_for_keys=False,
            timeout=15,
            sock=sock,                                  # tunnel through the jump server, if any
        )
    except Exception:
        ssh.close()
        raise
    return ssh


def login(host, auth):
    """Log in to a switch, through the jump server if there is one (like ssh -J)."""
    global client, chan
    sock = None
    if jump:
        transport = jump.get_transport()
        if transport is None:
            raise RuntimeError("jump server is not connected")
        sock = transport.open_channel(
            "direct-tcpip", (host, auth.get("port", 22)), ("127.0.0.1", 0), timeout=15
        )
    client = ssh_connect(host, auth, sock)
    chan = client.invoke_shell()
    chan.settimeout(30)       # never wait forever for the switch
    time.sleep(1)
    chan.recv(999999)         # skip the login banner


def clear_arp(host):
    output = send(COMMAND, wait=5)
    if "%" in output or not output.strip().endswith("#"):
        print(f"{datetime.now()} ❌ {host} did not accept '{COMMAND}':\n{output.strip()}")
    else:
        print(f"{datetime.now()} ✅ ARP cleared on {host}")


last_run = 0  # 0 = never, so the first run starts right away

while True:
    if time.time() - last_run >= RUN_EVERY:
        last_run = time.time()
        try:
            jump_auth, switches = load_config()  # read every run, so YAML changes apply next time
        except Exception as e:
            print(f"{datetime.now()} 📢 Can't read {CONFIG_FILE.name}: {e}")
            jump_auth, switches = None, []

        if jump_auth:
            try:
                print(f"{datetime.now()} 🔑 Logging in to jump server {jump_auth['host']}")
                jump = ssh_connect(jump_auth["host"], jump_auth)
            except Exception as e:
                print(f"{datetime.now()} 📢 Jump server failed: {type(e).__name__}: {e}")
                switches = []  # the switches can't be reached without it

        for host, auth in switches:
            print(f"{datetime.now()} 🔑 Logging in to {host}")
            try:
                login(host, auth)
                clear_arp(host)
            except Exception as e:
                print(f"{datetime.now()} 📢 {host} failed: {type(e).__name__}: {e}")
            finally:
                logout()
                print(f"{datetime.now()} 🔒 Logged out of {host}")

        close_jump()
        next_run = datetime.fromtimestamp(last_run + RUN_EVERY)
        print(f"{datetime.now()} ⏭  Next run: {next_run:%Y-%m-%d %H:%M}")
    time.sleep(CHECK_EVERY)
