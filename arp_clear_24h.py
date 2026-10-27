import sys
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
CONFIG_FILE = Path(__file__).resolve().parent / "switches.yaml"   # hosts + login settings

client: paramiko.SSHClient | None = None
chan: Channel | None = None


def send(cmd, wait=1.0):
    if chan is None:
        raise RuntimeError("SSH channel is not connected")
    chan.send(cmd + "\n")
    time.sleep(wait)
    return chan.recv(999999).decode(errors="ignore")


def logout():
    global client, chan
    if chan:
        chan.close()
    if client:
        client.close()
    client = None
    chan = None


def ctrl_c_handler(signum, frame):
    print("\n🛑 Ctrl+C detected")
    logout()
    print("SSH disconnected")
    sys.exit(0)


signal.signal(signal.SIGINT, ctrl_c_handler)


def load_switches():
    """Read switches.yaml and return a list of (host, login settings)."""
    with open(CONFIG_FILE) as f:
        config = yaml.safe_load(f) or {}
    defaults = config.get("auth") or {}
    switches = []
    for entry in config.get("hosts") or []:
        if isinstance(entry, str):                            # "- 10.0.0.11"
            entry = {"host": entry}
        switches.append((entry["host"], {**defaults, **entry}))  # host settings win
    return switches


def login(host, auth):
    global client, chan
    key_files = [os.path.expanduser(k) for k in auth.get("key_files") or []]
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    client.connect(
        host,
        port=auth.get("port", 22),
        username=auth["username"],
        key_filename=key_files or None,             # 1st: key files from the YAML
        passphrase=auth.get("passphrase") or None,  #      (and their passphrase)
        allow_agent=auth.get("use_agent", True),    # 2nd: keys in your SSH agent (YubiKey)
        password=auth.get("password") or None,      # 3rd: plain-text password, if set
        look_for_keys=False,
        timeout=15,
    )
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
            switches = load_switches()  # read every run, so YAML changes apply next time
        except Exception as e:
            print(f"{datetime.now()} 📢 Can't read {CONFIG_FILE.name}: {e}")
            switches = []
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
        next_run = datetime.fromtimestamp(last_run + RUN_EVERY)
        print(f"{datetime.now()} ⏭  Next run: {next_run:%Y-%m-%d %H:%M}")
    time.sleep(CHECK_EVERY)
