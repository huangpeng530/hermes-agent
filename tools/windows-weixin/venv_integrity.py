#!/usr/bin/env python3
"""venv_integrity.py - probe & repair the hermes venv deps the weixin gateway needs.

`hermes update` has been observed to mangle venv site-packages:
 - the top-level __init__.py of some packages disappears, leaving a namespace
   shell (import "succeeds" but e.g. aiohttp.ClientSession is gone)
 - whole packages (attrs/aiosignal/idna/aiohappyeyeballs) vanish entirely

The gateway then cannot connect weixin and permanently drops it from the
retry queue, so the fix is: repair the venv, then restart the gateway.

This script probes every weixin-critical module (import + sentinel) and
repairs broken ones:
  1. copy ONLY the missing files from a wheel (local cache, else PyPI).
     Existing files are never overwritten, so locked .pyd held by running
     desktop processes is untouched.
  2. fully-missing packages are (re)installed with `uv pip install`.

Exit 0 = all required deps healthy (or repaired this run); 1 = still broken.
Modes:
  (no args) = check + auto-repair
  --check   = probe only, no repair
Run it with the venv python so probes see the right site-packages:
  E:\\BACK-AI\\Hermes-win\\hermes-agent\\venv\\Scripts\\python.exe venv_integrity.py
"""
import glob
import importlib
import importlib.util
import json
import os
import re
import subprocess
import sys
import urllib.request
import zipfile

def _hermes_home():
    """Locate HERMES_HOME.

    Precedence: $HERMES_HOME env (set by the Hermes gateway/launcher) >
    auto-detect (script living under <home>/hermes-agent/... or
    <home>/weixin/...) > compiled default.
    """
    h = os.environ.get("HERMES_HOME")
    if h:
        return h.replace("\\", "/")
    p = os.path.abspath(__file__).replace("\\", "/")
    i = p.find("/hermes-agent/")
    if i > 0:
        return p[:i]
    j = p.rfind("/weixin/")
    if j > 0 and p[:j + 1].count("/hermes-agent/") == 0:
        return p[:j]
    return "E:/BACK-AI/Hermes-win"


HERMES_HOME = _hermes_home()
VENV_PY = HERMES_HOME + "/hermes-agent/venv/Scripts/python.exe"
SITE = HERMES_HOME + "/hermes-agent/venv/Lib/site-packages"
WHEELS = HERMES_HOME + "/weixin/wheels"
LOG = HERMES_HOME + "/weixin/watchdog.log"

# (import name, sentinel attr or None=import-only, pip name, required-for-weixin)
CHECKS = [
    ("aiohttp", "ClientSession", "aiohttp", True),
    ("multidict", "CIMultiDict", "multidict", True),
    ("yarl", "URL", "yarl", True),
    ("frozenlist", "FrozenList", "frozenlist", True),
    ("propcache", "cached_property", "propcache", True),
    ("aiosignal", "Signal", "aiosignal", True),
    ("idna", "encode", "idna", True),
    ("attr", "define", "attrs", True),
    ("aiohappyeyeballs", "start_connection", "aiohappyeyeballs", True),
    ("certifi", "where", "certifi", False),
    ("cryptography", None, "cryptography", False),
]


def log(msg):
    line = "[venv_integrity] " + msg
    print(line, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def healthy(mod, sentinel):
    try:
        spec = importlib.util.find_spec(mod)
        if spec is None:
            return False
        m = importlib.import_module(mod)
    except Exception:
        return False
    if sentinel is None:
        return bool(getattr(m, "__file__", None) or getattr(m, "__path__", None))
    return hasattr(m, sentinel)


def dist_version(pipname):
    key = pipname.lower().replace("_", "-")
    try:
        for d in os.listdir(SITE):
            if not d.endswith(".dist-info"):
                continue
            if d.split("-", 1)[0].lower().replace("_", "-") == key:
                m = re.match(r"^.+?-(.+)\.dist-info$", d)
                if m:
                    return m.group(1)
    except Exception:
        pass
    return None


def find_uv():
    for c in (os.path.join(HERMES_HOME, "bin", "uv.exe"),
              os.path.join(HERMES_HOME, "bin", "uv.EXE")):
        if os.path.exists(c):
            return c
    import shutil as _sh
    return _sh.which("uv")


def wheel_candidates(pipname):
    inst = dist_version(pipname)
    out = []
    for w in sorted(glob.glob(os.path.join(WHEELS, "*.whl"))):
        base = os.path.basename(w)
        m = re.match(r"^([A-Za-z0-9_.]+?)-", base)
        if not m or m.group(1).lower().replace("_", "-") != pipname.lower().replace("_", "-"):
            continue
        if inst and ("-" + inst + "-") in base:
            out.insert(0, w)
        else:
            out.append(w)
    return out


def repair_from_wheels(pipname):
    for w in wheel_candidates(pipname):
        m = re.match(r"^([A-Za-z0-9_.]+?)-(\d.*)-", os.path.basename(w))
        if not m:
            continue
        pkg, ver = m.group(1), m.group(2)
        allow_meta = pkg + "-" + ver + ".dist-info"
        with zipfile.ZipFile(w) as z:
            tops = set()
            for n in z.namelist():
                top = n.split("/", 1)[0]
                if top.endswith(".dist-info") or top.endswith(".data"):
                    continue
                tops.add(top)
            copied = 0
            for n in z.namelist():
                if n.endswith("/"):
                    continue
                top = n.split("/", 1)[0]
                if top not in tops and top != allow_meta:
                    continue
                tgt = os.path.join(SITE, n)
                if os.path.exists(tgt):
                    continue  # never overwrite (locked .pyd / version drift)
                os.makedirs(os.path.dirname(tgt), exist_ok=True)
                with z.open(n) as src, open(tgt, "wb") as dst:
                    dst.write(src.read())
                copied += 1
        log("%s: copied %d missing file(s) from %s" % (pipname, copied, os.path.basename(w)))
        return True
    return False


def repair_via_uv(pipname):
    uv = find_uv()
    if not uv:
        log("%s: uv not found, skipping rung 2" % pipname)
        return False
    import shutil as _sh
    cmd = [uv, "pip", "install", "--python", VENV_PY, pipname]
    env = dict(os.environ, DOTNET_NOLOGO="1", DOTNET_CLI_TELEMETRY_OPTOUT="1")
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=180, env=env)
        log("%s: uv rc=%s %s" % (pipname, r.returncode, (r.stdout or r.stderr).strip()[:160]))
        return r.returncode == 0
    except Exception as e:
        log("%s: uv failed: %r" % (pipname, e))
        return False


def repair_download(pipname):
    try:
        j = json.loads(urllib.request.urlopen(
            "https://pypi.org/pypi/%s/json" % pipname, timeout=20).read())
    except Exception as e:
        log("%s: pypi query failed: %r" % (pipname, e))
        return False
    url = None
    for u in j["urls"]:
        fn = u["filename"]
        if fn.endswith(".whl") and "cp311" in fn and "win_amd64" in fn:
            url = u["url"]
            break
    if not url:
        for u in j["urls"]:
            if u["filename"].endswith(".whl"):
                url = u["url"]
                break
    if not url:
        log("%s: no wheel on pypi?" % pipname)
        return False
    fn = url.split("/")[-1]
    dst = os.path.join(WHEELS, fn)
    if not os.path.exists(dst):
        urllib.request.urlretrieve(url, dst)
        log("%s: cached wheel %s" % (pipname, fn))
    return repair_from_wheels(pipname)


def main():
    os.makedirs(WHEELS, exist_ok=True)
    check_only = "--check" in sys.argv[1:]
    broken = [p for m, a, p, req in CHECKS if not healthy(m, a)]
    for mod, sentinel, pipname, required in CHECKS:
        if healthy(mod, sentinel):
            continue
        log("broken/missing: %s (probe %s.%s)" % (pipname, mod, sentinel or "<import>"))
        if check_only:
            continue
        if not repair_from_wheels(pipname) or not healthy(mod, sentinel):
            repair_via_uv(pipname)
        if not healthy(mod, sentinel):
            repair_download(pipname)
    still = [p for m, a, p, req in CHECKS if not healthy(m, a)]
    if not still:
        log("OK: all %d deps healthy (%d repaired this run)" % (len(CHECKS), len(broken)))
        return 0
    log("FAIL: still broken: %s" % ", ".join(still))
    return 1


if __name__ == "__main__":
    sys.exit(main())
