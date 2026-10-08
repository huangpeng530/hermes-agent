#!/usr/bin/env python3
"""weixin_watchdog.py - auto-recover the weixin gateway after `hermes update` breakage.

Run by scheduled task \\Hermes_WeixinWatchdog every ~10 min. A single tick:

  1. Probe the venv (venv_integrity.py --check).
     - broken  -> run the repairer (check+repair), then restart the gateway
                  (a repaired venv only takes effect in a fresh gateway process).
     - healthy -> no venv action.
  2. Scan agent.log lines appended since the last tick:
     - "Gateway started with no connected platforms ... weixin ... queued for retry"
       or "Reconnect weixin: no bot credential ... removing from retry queue"
       => the gateway permanently gave up on weixin -> restart it.
     - "weixin connected" since last tick -> reset the failure counter.
  2b. Probe the live process table (psutil, exact-argv match):
     - the messaging gateway process is NOT running (a cleanly-stopped gateway
       writes no new log lines and trips neither trigger above, so the bot sat
       dark for ~24 h on 2026-09-28) -> restart it.
  3. Throttle & escalation (state in watchdog.state.json):
     - at most 1 gateway restart per 5 min,
     - after 4 consecutive failing restarts without a fresh "weixin connected",
       stop acting and write ATTENTION.txt (needs a human, e.g. iLink credential).

Safety: only restarts when something is actually wrong, so a healthy system
sees zero gateway restarts from this watchdog.

Exit: 0 = ok / acted normally, 2 = escalation written.
"""
import json
import os
import re
import subprocess
import time


def _hermes_home():
    """Locate HERMES_HOME.

    Precedence: $HERMES_HOME env (set by the Hermes gateway/launcher) >
    auto-detect (script living under <home>/hermes-agent/tools/... ) >
    compiled default.
    """
    h = os.environ.get("HERMES_HOME")
    if h:
        return h.replace("\\", "/")
    p = os.path.abspath(__file__).replace("\\", "/")
    i = p.find("/hermes-agent/")
    if i > 0:
        return p[:i]
    return "E:/BACK-AI/Hermes-win"


HERMES_HOME = _hermes_home()
VENV_PY   = HERMES_HOME + "/hermes-agent/venv/Scripts/python.exe"
HERMES_CLI = HERMES_HOME + "/hermes-agent/venv/Scripts/hermes.exe"
BASE = os.path.join(HERMES_HOME, "weixin")  # runtime state dir (logs, state, lock)
INTEGRITY = os.path.join(BASE, "venv_integrity.py")
STATE = os.path.join(BASE, "watchdog.state.json")
LOCK = os.path.join(BASE, "watchdog.lock")
LOGPATH = HERMES_HOME + "/logs/agent.log"
ATTENTION = os.path.join(BASE, "ATTENTION.txt")

# exact phrases observed in agent.log
GIVEUP = re.compile(r"removing from retry queue|no bot credential on queued config")
NOCONN = re.compile(r"no connected platforms.*weixin")
CONNECTED = re.compile(r"weixin connected")
# agent.log is shared: our own agent sessions log tool output that can echo the
# phrases above. Only lines emitted by the gateway process itself count.
GATEWAY_LINE = re.compile(
    r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d+ (INFO|WARNING|ERROR) gateway\.run: ")

RESTART_COOLDOWN_S = 300   # max 1 gateway restart / 5 min
MAX_CONSEC_FAILS = 4       # escalation threshold
LOCK_MAX_AGE_S = 900       # stale lock if a tick ever wedges


def log(msg):
    line = "[" + time.strftime("%Y-%m-%d %H:%M:%S") + "] [weixin_watchdog] " + msg
    print(line, flush=True)
    try:
        with open(os.path.join(BASE, "watchdog.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def load_state():
    default = {"log_pos": 0, "seeded": False,
               "last_restart_ts": 0, "consec_fail": 0}
    if not os.path.exists(STATE):
        # seed: start scanning at the CURRENT end of the log so old historical
        # give-up lines don't trigger an immediate pointless restart
        try:
            default["log_pos"] = os.path.getsize(LOGPATH)
        except OSError:
            pass
        default["seeded"] = True
        save_state(default)
        return default
    try:
        with open(STATE, "r", encoding="utf-8") as f:
            st = json.load(f)
        default.update(st)
    except Exception:
        pass
    return default


def save_state(st):
    tmp = STATE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(st, f, indent=2)
    os.replace(tmp, STATE)


def venv_broken():
    r = subprocess.run([VENV_PY, INTEGRITY, "--check"],
                       capture_output=True, text=True, timeout=300)
    for l in r.stdout.strip().splitlines():
        log("venv: " + l)
    return r.returncode != 0


def repair_venv():
    r = subprocess.run([VENV_PY, INTEGRITY],
                       capture_output=True, text=True, timeout=900)
    for l in r.stdout.strip().splitlines():
        log("repair: " + l)
    return r.returncode == 0


def restart_gateway():
    if NO_RESTART:
        log("would restart gateway (suppressed by --no-restart)")
        return True
    log("gateway restart via hermes CLI ...")
    try:
        r = subprocess.run([HERMES_CLI, "gateway", "restart"],
                           capture_output=True, text=True, timeout=180)
        out = (r.stdout or "") + (r.stderr or "")
        log("restart rc=%s: %s" % (r.returncode, " ".join(out.strip().split())[:200]))
        return r.returncode == 0
    except Exception as e:
        log("restart failed: %r" % e)
        return False


def read_new_log(st):
    try:
        size = os.path.getsize(LOGPATH)
        pos = st.get("log_pos", 0)
        if pos > size:      # rotated / truncated
            pos = 0
        with open(LOGPATH, "rb") as f:
            f.seek(pos)
            data = f.read()
        st["log_pos"] = pos + len(data)
        return data.decode("utf-8", "replace").splitlines()
    except FileNotFoundError:
        return []


def _is_messaging_gateway(tokens):
    """True only if this argv is the long-running MESSAGING gateway
    (`... gateway run`).

    Never the desktop headless backend (`hermes_cli.main serve ...`) and never
    a transient `hermes gateway status|restart|stop` CLI call. The signature
    is the adjacent `gateway` + `run` token pair, confirmed to belong to hermes
    (a `hermes_cli.main` module in argv, or a hermes-named executable in the
    first two tokens). We match the exact token pair, never a bare argv
    substring (argv-substring process inference is the bug class behind the
    fleet-update incidents; see root AGENTS.md)."""
    for k in range(len(tokens) - 1):
        if tokens[k] == "gateway" and tokens[k + 1] == "run":
            is_hermes = (
                any(t == "hermes_cli.main" for t in tokens)
                or any("hermes" in t.lower() for t in tokens[:2])
            )
            if is_hermes:
                return True
    return False


def gateway_process_alive():
    """True if a messaging-gateway process is currently running.

    The watchdog historically acted only on (1) a broken venv and (2) a
    give-up line in agent.log. A CLEANLY-STOPPED gateway (e.g. an out-of-band
    sync restart whose start half never came up) writes no new log lines and
    trips neither trigger, so the bot sat dark ~24 h on 2026-09-28. This third
    trigger probes the live process table instead.

    Uses psutil (present in the venv) + exact-argv subcommand matching. On a
    psutil import/scan failure we fail CLOSED (return True = assume alive) so a
    transient process-table hiccup can never cause a needless restart."""
    try:
        import psutil
    except Exception:
        log("psutil unavailable; assuming gateway alive (fail-closed)")
        return True
    for p in psutil.process_iter(["cmdline"]):
        try:
            toks = [t for t in (p.info.get("cmdline") or []) if t]
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        if toks and _is_messaging_gateway(toks):
            return True
    return False


NO_RESTART = False  # set in __main__ when --no-restart passed

def main():
    st = load_state()
    now = time.time()
    exit_code = 0
    global NO_RESTART

    # ---- 1. venv integrity ----
    broken = venv_broken()
    if broken:
        if repair_venv():
            log("venv repaired")
            if can_restart(st, now):
                restart_gateway()
                st["last_restart_ts"] = now
                st["consec_fail"] += 1
            else:
                log("restart throttled; will retry next tick (venv already repaired)")
        else:
            log("venv repair FAILED")
            st["consec_fail"] += 1
            save_state(st)
            if st["consec_fail"] >= MAX_CONSEC_FAILS:
                escalate(st)
                return 2

    # ---- 2. log-based detection ----
    new = read_new_log(st)
    gw = [l for l in new if GATEWAY_LINE.match(l)]
    gave_up = any(GIVEUP.search(l) or NOCONN.search(l) for l in gw)
    connected = any(CONNECTED.search(l) for l in gw)

    if connected:
        st["consec_fail"] = 0
        log("weixin connected detected; fail counter reset")

    if gave_up:
        log("weixin permanently dropped by gateway -> restarting")
        if can_restart(st, now):
            ok = restart_gateway()
            st["last_restart_ts"] = now
            st["consec_fail"] += 1
            if not ok:
                exit_code = 2
            else:
                log("restart issued; expecting reconnect within next ticks")
        else:
            log("restart throttled; next tick will restart")
            st["consec_fail"] += 1

    # ---- 2b. live-process detection (a cleanly-stopped gateway) ----
    if not gateway_process_alive():
        log("messaging gateway process not found -> restarting")
        if can_restart(st, now):
            ok = restart_gateway()
            st["last_restart_ts"] = now
            st["consec_fail"] += 1
            if not ok:
                exit_code = 2
            else:
                log("restart issued for missing gateway; expecting it up within next ticks")
        else:
            log("gateway missing but restart throttled (cooldown); next tick will restart")
            st["consec_fail"] += 1

    save_state(st)

    # ---- 3. escalation ----
    if st["consec_fail"] >= MAX_CONSEC_FAILS:
        escalate(st)
        st["consec_fail"] = 0
        save_state(st)
        return 2
    return exit_code


def can_restart(st, now):
    return (now - st.get("last_restart_ts", 0)) >= RESTART_COOLDOWN_S


def escalate(st):
    log("*** ESCALATION: %d failing restarts without reconnect - "
        "writing ATTENTION.txt (check iLink credential / network) ***"
        % MAX_CONSEC_FAILS)
    try:
        with open(ATTENTION, "w", encoding="utf-8") as f:
            f.write(time.strftime("%Y-%m-%d %H:%M:%S") +
                    "\nweixin gateway keeps failing after restarts.\n"
                    "Check: 1) iLink/weixin credential in weixin\\accounts\n"
                    "2) network to ilinkai.weixin.qq.com\n"
                    "3) hermes-agent\\venv health (run venv_integrity.py)\n")
    except Exception as e:
        log("attention write failed: %r" % e)


def acquire_lock():
    """Prevent overlapping ticks (a long venv repair can exceed the 10-min interval)."""
    try:
        if os.path.exists(LOCK):
            age = time.time() - os.path.getmtime(LOCK)
            if age < LOCK_MAX_AGE_S:
                return False
        with open(LOCK, "w", encoding="utf-8") as f:
            f.write(str(os.getpid()))
        return True
    except Exception:
        return True  # fail open: better double-check than never-check


def release_lock():
    try:
        if os.path.exists(LOCK):
            os.remove(LOCK)
    except Exception:
        pass


if __name__ == "__main__":
    import sys
    if "--no-restart" in sys.argv:
        NO_RESTART = True
    if not acquire_lock():
        print("another watchdog tick still running; skipping this one")
        sys.exit(0)
    try:
        sys.exit(main())
    finally:
        release_lock()
