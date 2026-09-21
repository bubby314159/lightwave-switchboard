#!/usr/bin/env python3
"""
Switchboard: a small web control panel for a first-generation LightwaveRF
Link / Connect Bridge.

The bridge only understands UDP, which a browser can't send, so this script
runs on a computer on your network (a Raspberry Pi is ideal), serves the
control panel, and relays each tap to the bridge.

    python3 switchboard.py
    then open  http://<that computer's address>:8080  on your phone or laptop

Needs Python 3.8 or newer. Nothing to install.
"""

import argparse
import copy
import ipaddress
import json
import os
import re
import socket
import sys
import threading
import traceback
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

LINK_PORT = 9760        # the Link listens for commands here
REPLY_PORT = 9761       # the Link sends its answers here
BROADCAST = "255.255.255.255"
MAX_LEVEL = 32          # dimmer levels run 1..32
DEFAULT_LEVEL = 24
PAIR_WAIT = 30          # seconds to wait for the button press when pairing
LOCAL_SUFFIXES = (".local", ".lan", ".home", ".home.arpa", ".internal")
REPLY_RE = re.compile(r"^\s*(\d{1,3})\s*,\s*(.*?)\s*$", re.S)
HOST_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.\-]{0,251}[A-Za-z0-9])?$")


# --------------------------------------------------------------------------
# Saved settings, rooms, devices and last-known switch positions
# --------------------------------------------------------------------------

class Store:
    def __init__(self, path):
        self.path = path
        self.lock = threading.RLock()
        self.data = {"link_ip": "", "require_reply": True, "rooms": [], "state": {}}
        try:
            with open(path, encoding="utf-8") as f:
                loaded = json.load(f)
            for key in self.data:
                if key in loaded:
                    self.data[key] = loaded[key]
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:
            bad = path + ".bad"
            print("Couldn't read {} ({}). Moving it to {} and starting fresh."
                  .format(path, e, bad), file=sys.stderr)
            try:
                os.replace(path, bad)
            except OSError:
                pass

    def save(self):
        with self.lock:
            tmp = self.path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)

    def snapshot(self):
        with self.lock:
            return copy.deepcopy(self.data)

    def find_room(self, num):
        for r in self.data["rooms"]:
            if r["num"] == num:
                return r
        return None

    def find_device(self, room, dev):
        r = self.find_room(room)
        if r:
            for d in r["devices"]:
                if d["num"] == dev:
                    return r, d
        return r, None


def is_num(v):
    return isinstance(v, int) and not isinstance(v, bool) and 1 <= v <= 99


def clean_name(v, fallback):
    s = re.sub(r"\s+", " ", str(v or "")).strip()[:40]
    return s or fallback


def clean_config(body):
    """Validate what the browser sends and return tidy data, or raise ValueError."""
    rooms_in = body.get("rooms")
    if not isinstance(rooms_in, list) or len(rooms_in) > 32:
        raise ValueError("Rooms must be a list of up to 32 rooms.")
    rooms, seen_rooms = [], set()
    for r in rooms_in:
        if not isinstance(r, dict) or not is_num(r.get("num")):
            raise ValueError("Every room needs a number from 1 to 99.")
        if r["num"] in seen_rooms:
            raise ValueError("Room number {} is used twice.".format(r["num"]))
        seen_rooms.add(r["num"])
        devs_in = r.get("devices", [])
        if not isinstance(devs_in, list) or len(devs_in) > 32:
            raise ValueError("A room can hold up to 32 devices.")
        devs, seen_devs = [], set()
        for d in devs_in:
            if not isinstance(d, dict) or not is_num(d.get("num")):
                raise ValueError("Every device needs a number from 1 to 99.")
            if d["num"] in seen_devs:
                raise ValueError("Device number {} is used twice in {}."
                                 .format(d["num"], clean_name(r.get("name"), "a room")))
            seen_devs.add(d["num"])
            devs.append({
                "num": d["num"],
                "name": clean_name(d.get("name"), "Device {}".format(d["num"])),
                "type": "dimmer" if d.get("type") == "dimmer" else "onoff",
            })
        rooms.append({
            "num": r["num"],
            "name": clean_name(r.get("name"), "Room {}".format(r["num"])),
            "devices": devs,
        })
    ip = str(body.get("link_ip") or "").strip()
    if ip and not HOST_RE.match(ip):
        raise ValueError("That doesn't look like an IP address or host name.")
    return {"rooms": rooms, "link_ip": ip, "require_reply": bool(body.get("require_reply", True))}


# --------------------------------------------------------------------------
# Talking to the Link
# --------------------------------------------------------------------------

class Link:
    """
    Command format:  <n>,!R<room>D<device>F<function>|<line 1>|<line 2>
        F1 on   F0 off   FdP<1-32> dim   (room only) Fa all off   !F*p pair
    The Link answers on UDP 9761 with  <n>,OK  or  <n>,ERR,...
    """

    verbose = False

    def __init__(self, store):
        self.store = store
        self.send_lock = threading.Lock()      # one command at a time
        self._count_lock = threading.Lock()
        self._counter = 99
        self._waiting = {}
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            self.sock.bind(("", REPLY_PORT))
            self.can_hear = True
        except OSError as e:
            print("Couldn't listen on UDP port {} ({}). Commands will still be sent, "
                  "but the Link's confirmations can't be read.".format(REPLY_PORT, e), file=sys.stderr)
            self.sock.bind(("", 0))
            self.can_hear = False
        if self.can_hear:
            threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        while True:
            try:
                data, _ = self.sock.recvfrom(2048)
            except ConnectionResetError:       # Windows: ICMP "port unreachable"
                continue
            except OSError:
                return
            m = REPLY_RE.match(data.decode("utf-8", "replace"))
            if not m:
                continue
            slot = self._waiting.get(int(m.group(1)))
            if slot:
                slot[1]["text"] = m.group(2)
                slot[0].set()

    def _next(self):
        with self._count_lock:
            self._counter = 100 if self._counter >= 999 else self._counter + 1
            return self._counter

    def request(self, body, tries=2, per_try=1.5):
        """Send one command. Returns {"ok", "error", "needs_pairing"}."""
        settings = self.store.snapshot()
        ip = settings["link_ip"] or BROADCAST
        wait = settings["require_reply"] and self.can_hear
        if body == "!F*p":
            wait = True
        n = self._next()
        packet = "{},{}\n".format(n, body).encode("utf-8")
        event, slot = threading.Event(), {}
        if wait:
            self._waiting[n] = (event, slot)
        try:
            for _ in range(tries if wait else 1):
                try:
                    self.sock.sendto(packet, (ip, LINK_PORT))
                except OSError as e:
                    return {"ok": False, "needs_pairing": False,
                            "error": "Couldn't send to the Link ({}). Check the address in Settings.".format(e)}
                if not wait:
                    return {"ok": True}
                if event.wait(per_try):
                    break
            else:
                return {"ok": False, "needs_pairing": False,
                        "error": "The Link didn't answer. Check it's powered on and on the same "
                                 "network as this computer. If it still fails, enter its IP "
                                 "address in Settings."}
        finally:
            self._waiting.pop(n, None)

        text = slot.get("text", "")
        if self.verbose:
            print("Sent {!r}, Link replied {!r}".format(body, text))
        if not text.upper().startswith("ERR"):
            # "OK", or another answer such as ?V="N2.94D" (its firmware version):
            # either way the Link heard us. Only an explicit ERR counts as failure.
            return {"ok": True}
        if "regist" in text.lower():
            return {"ok": False, "needs_pairing": True,
                    "error": "The Link hasn't been paired with this computer yet. "
                             "Open Settings and choose Pair now."}
        return {"ok": False, "needs_pairing": False,
                "error": "The Link reported an error: {}".format(text)}


def label(s):
    """The two text lines are separated by | and , so keep them out of names."""
    return re.sub(r"[|,\r\n]", " ", str(s)).strip()[:16]


# --------------------------------------------------------------------------
# Web server
# --------------------------------------------------------------------------

def host_allowed(header, extra):
    """Refuse odd Host headers so a web page elsewhere can't drive the panel
    by pointing its own domain at this computer (DNS rebinding)."""
    if not header:
        return False
    h = header.strip().lower()
    if h.startswith("["):
        name = h[1:h.find("]")] if "]" in h else h
    else:
        name = h.rsplit(":", 1)[0] if h.count(":") == 1 else h
    if name in extra:
        return True
    try:
        ipaddress.ip_address(name)
        return True
    except ValueError:
        pass
    return "." not in name or name.endswith(LOCAL_SUFFIXES)


class Handler(BaseHTTPRequestHandler):
    server_version = "Switchboard/1.0"
    verbose = False

    def log_message(self, fmt, *args):
        if self.verbose:
            super().log_message(fmt, *args)

    # -- plumbing ----------------------------------------------------------
    def _send(self, code, body, ctype):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Content-Security-Policy",
                         "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; "
                         "connect-src 'self'; manifest-src 'self'; base-uri 'none'; form-action 'none'")
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code, obj):
        self._send(code, json.dumps(obj), "application/json; charset=utf-8")

    def _error(self, code, message):
        self._json(code, {"ok": False, "error": message})

    def _checked(self):
        if not host_allowed(self.headers.get("Host"), self.server.allow_hosts):
            self._error(403, "Blocked: this panel doesn't accept requests addressed to that name. "
                             "Use the computer's IP address, or start the panel with --allow-host.")
            return False
        return True

    def _public(self):
        data = self.server.store.snapshot()
        data["ok"] = True
        data["can_hear"] = self.server.link.can_hear
        return data

    # -- routes ------------------------------------------------------------
    def do_GET(self):
        if not self._checked():
            return
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self._send(200, PAGE, "text/html; charset=utf-8")
        elif path == "/api/config":
            self._json(200, self._public())
        elif path == "/manifest.webmanifest":
            self._json(200, {"name": "Switchboard", "short_name": "Switchboard", "start_url": "/",
                             "display": "standalone", "background_color": "#0E1318",
                             "theme_color": "#0E1318"})
        else:
            self._error(404, "Not found.")

    def do_POST(self):
        if not self._checked():
            return
        # Requiring JSON means other websites can't send this without a
        # permission check the browser will refuse.
        if not self.headers.get("Content-Type", "").lower().startswith("application/json"):
            return self._error(415, "Send JSON.")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if not 0 < length <= 65536:
                raise ValueError
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(body, dict):
                raise ValueError
        except (ValueError, UnicodeDecodeError):
            return self._error(400, "That request wasn't valid JSON.")
        path = self.path.split("?", 1)[0]
        try:
            if path == "/api/config":
                self._post_config(body)
            elif path == "/api/command":
                self._post_command(body)
            elif path == "/api/pair":
                self._post_pair()
            else:
                self._error(404, "Not found.")
        except Exception as e:                       # keep the panel alive
            if self.verbose:
                traceback.print_exc()
            self._error(500, "Something went wrong inside the panel: {}".format(e))

    def _post_config(self, body):
        store = self.server.store
        try:
            new = clean_config(body)
        except ValueError as e:
            return self._error(400, str(e))
        with store.lock:
            store.data.update(new)
            live = {"{}-{}".format(r["num"], d["num"]) for r in new["rooms"] for d in r["devices"]}
            store.data["state"] = {k: v for k, v in store.data["state"].items() if k in live}
            store.save()
        self._json(200, self._public())

    def _post_command(self, body):
        store, link = self.server.store, self.server.link
        action, room = body.get("action"), body.get("room")
        if not is_num(room):
            return self._error(400, "Missing room number.")

        if action == "room_off":
            r = store.find_room(room)
            if not r:
                return self._error(400, "That room isn't set up.")
            with link.send_lock:
                result = link.request("!R{}Fa|{}|All off".format(room, label(r["name"])))
            if result["ok"]:
                with store.lock:
                    for d in r["devices"]:
                        key = "{}-{}".format(room, d["num"])
                        st = dict(store.data["state"].get(key, {}))
                        st["on"] = False
                        store.data["state"][key] = st
                    store.save()
        elif action in ("on", "off", "dim"):
            dev = body.get("device")
            r, d = store.find_device(room, dev) if is_num(dev) else (None, None)
            if not d:
                return self._error(400, "That device isn't set up.")
            key = "{}-{}".format(room, dev)
            prev = store.snapshot()["state"].get(key, {})
            level = None
            if d["type"] == "dimmer" and action != "off":
                lvl = body.get("level") if action == "dim" else prev.get("level")
                level = lvl if isinstance(lvl, int) and 1 <= lvl <= MAX_LEVEL else DEFAULT_LEVEL
                fn = "dP{}".format(level)
            elif action == "dim":
                return self._error(400, "That device isn't a dimmer.")
            else:
                fn = "1" if action == "on" else "0"
            text = "Off" if action == "off" else "On"
            with link.send_lock:
                result = link.request("!R{}D{}F{}|{}|{}".format(room, dev, fn, label(d["name"]), text))
            if result["ok"]:
                st = {"on": action != "off"}
                if d["type"] == "dimmer":
                    st["level"] = level or prev.get("level") or DEFAULT_LEVEL
                with store.lock:
                    store.data["state"][key] = st
                    store.save()
        else:
            return self._error(400, "Unknown action.")

        if result["ok"]:
            self._json(200, {"ok": True, "state": store.snapshot()["state"]})
        else:
            self._json(200, result)

    def _post_pair(self):
        link = self.server.link
        if not link.can_hear:
            return self._json(200, {"ok": False, "error":
                "This panel couldn't listen on UDP port {}, so it can't hear the Link's reply. "
                "Close any other LightwaveRF software on this computer and restart the panel."
                .format(REPLY_PORT)})
        result = link.request("!F*p", tries=1, per_try=PAIR_WAIT)
        if not result["ok"] and "didn't answer" in result.get("error", ""):
            result["error"] = ("The Link didn't answer within {} seconds. Press the Link's button "
                               "straight after choosing Pair now, and check the address in Settings."
                               .format(PAIR_WAIT))
        self._json(200, result)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True


def lan_address():
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("10.255.255.255", 1))          # sends nothing; just picks the right interface
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def main():
    here = os.path.dirname(os.path.abspath(__file__))
    ap = argparse.ArgumentParser(description="Web control panel for a LightwaveRF Gen 1 Link.")
    ap.add_argument("--port", type=int, default=8080, help="web port (default 8080)")
    ap.add_argument("--host", default="0.0.0.0",
                    help="address to listen on (default 0.0.0.0, i.e. your whole network; "
                         "use 127.0.0.1 for this computer only)")
    ap.add_argument("--link-ip", help="the Link's IP address (default: broadcast to the network)")
    ap.add_argument("--config", default=os.path.join(here, "switchboard.json"),
                    help="where rooms and settings are saved (default: next to this script)")
    ap.add_argument("--allow-host", action="append", default=[], metavar="NAME",
                    help="also accept this host name in the address bar (repeatable)")
    ap.add_argument("--verbose", action="store_true", help="log every web request")
    args = ap.parse_args()

    store = Store(args.config)
    if args.link_ip:
        store.data["link_ip"] = args.link_ip
        store.save()
    link = Link(store)

    Handler.verbose = Link.verbose = args.verbose
    try:
        httpd = Server((args.host, args.port), Handler)
    except OSError as e:
        sys.exit("Couldn't start on port {}: {}. Try --port with another number.".format(args.port, e))
    httpd.store, httpd.link = store, link
    httpd.allow_hosts = {h.lower() for h in args.allow_host}

    print("Switchboard is running.")
    print("  On this computer:  http://localhost:{}".format(args.port))
    lan = lan_address()
    if lan and args.host in ("0.0.0.0", ""):
        print("  From your phone:   http://{}:{}".format(lan, args.port))
        print("  Anyone on your network can use the panel. Don't expose this port to the internet.")
    print("  Saved in:          {}".format(store.path))
    print("  Press Ctrl+C to stop.")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")


PAGE = r"""<!doctype html>
<html lang="en-GB">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<meta name="theme-color" content="#DCE1E5" media="(prefers-color-scheme: light)">
<meta name="theme-color" content="#0E1318" media="(prefers-color-scheme: dark)">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-title" content="Switchboard">
<link rel="manifest" href="/manifest.webmanifest">
<title>Switchboard</title>
<style>
:root {
  color-scheme: light dark;
  --bg: #DCE1E5;
  --plate: #F5F6F7;
  --edge: #C2CAD1;
  --field: #FFFFFF;
  --ink: #16202A;
  --soft: #526070;
  --well: #C9D0D6;
  --face-hi: #FDFDFD;
  --face-lo: #CDD4DA;
  --face-mid: #E6EAED;
  --neon: #FF8A1F;
  --danger: #B3261E;
  --good: #1B7A3E;
  --focus: #1B5FD1;
  --shadow: 0 1px 0 rgba(255,255,255,.7) inset, 0 1px 3px rgba(22,32,42,.16);
}
@media (prefers-color-scheme: dark) {
  :root {
    --bg: #0E1318;
    --plate: #182028;
    --edge: #2B3641;
    --field: #0F151B;
    --ink: #E7ECF0;
    --soft: #93A1AE;
    --well: #0C1015;
    --face-hi: #3A4653;
    --face-lo: #1F2831;
    --face-mid: #2B3540;
    --neon: #FFA24A;
    --danger: #FF8C82;
    --good: #6FD08C;
    --focus: #84B3FF;
    --shadow: 0 1px 0 rgba(255,255,255,.05) inset, 0 1px 3px rgba(0,0,0,.5);
  }
}

* { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  min-height: 100vh;
  min-height: 100dvh;
  background: var(--bg);
  color: var(--ink);
  font: 16px/1.45 "Avenir Next", "Segoe UI Variable Text", "Segoe UI", system-ui, -apple-system, sans-serif;
  padding: env(safe-area-inset-top) env(safe-area-inset-right) env(safe-area-inset-bottom) env(safe-area-inset-left);
}
main, .top { width: 100%; max-width: 900px; margin: 0 auto; padding-left: 16px; padding-right: 16px; }
main { padding-bottom: 96px; }

.top {
  display: flex; align-items: center; justify-content: space-between; gap: 12px;
  padding-top: 18px; padding-bottom: 4px;
}
h1 { margin: 0; font-size: 1.6rem; font-weight: 750; letter-spacing: -0.02em; }
.actions { display: flex; gap: 8px; }

.btn {
  font: inherit; font-size: .95rem; font-weight: 600;
  color: var(--ink); background: transparent;
  border: 1px solid var(--edge); border-radius: 8px;
  padding: 9px 14px; min-height: 40px; cursor: pointer;
}
.btn:hover { background: color-mix(in srgb, var(--ink) 7%, transparent); }
.btn:active { background: color-mix(in srgb, var(--ink) 13%, transparent); }
.btn[disabled] { opacity: .5; cursor: default; }
.btn.small { padding: 6px 12px; min-height: 34px; font-size: .88rem; }
.btn.primary { background: var(--ink); color: var(--bg); border-color: var(--ink); }
.btn.primary:hover { background: color-mix(in srgb, var(--ink) 88%, var(--bg)); }
.btn.danger { color: var(--danger); }
.btn[aria-pressed="true"] { background: var(--ink); color: var(--bg); border-color: var(--ink); }
:focus-visible { outline: 3px solid var(--focus); outline-offset: 2px; }

.room { margin-top: 26px; }
.room-head { display: flex; align-items: center; justify-content: space-between; gap: 12px; margin-bottom: 10px; }
.room-head.edit { align-items: flex-end; flex-wrap: wrap; }
h2 { margin: 0; font-size: 1.1rem; font-weight: 700; letter-spacing: -0.005em; }

.grid { display: grid; gap: 12px; grid-template-columns: repeat(auto-fill, minmax(min(100%, 270px), 1fr)); }

.tile {
  display: flex; align-items: center; gap: 16px;
  padding: 14px 16px 14px 14px;
  background: var(--plate);
  border: 1px solid var(--edge);
  border-radius: 16px;
  box-shadow: var(--shadow);
}
.info { flex: 1; min-width: 0; }
.name { font-weight: 650; font-size: 1.05rem; overflow-wrap: anywhere; }
.state { color: var(--soft); font-size: .9rem; margin-top: 1px; }
input[type=range] { width: 100%; margin: 12px 0 2px; accent-color: var(--ink); }

/* The rocker: tap the top half for on, the bottom half for off. */
.rocker {
  position: relative; flex: none; width: 56px; height: 92px;
  border-radius: 12px; background: var(--well);
  box-shadow: inset 0 2px 5px rgba(0,0,0,.28), 0 1px 0 rgba(255,255,255,.25);
}
.rocker .face {
  position: absolute; inset: 6px; border-radius: 8px;
  background: var(--face-mid);
  box-shadow: 0 1px 2px rgba(0,0,0,.35);
  transform: perspective(170px) rotateX(0deg);
  transition: transform .14s ease-out;
}
.rocker[data-pos=on]  .face { background: linear-gradient(var(--face-lo), var(--face-hi)); transform: perspective(170px) rotateX(11deg); }
.rocker[data-pos=off] .face { background: linear-gradient(var(--face-hi), var(--face-lo)); transform: perspective(170px) rotateX(-11deg); }
.mk-on, .mk-off { position: absolute; left: 50%; }
.mk-on  { top: 13px; width: 4px; height: 15px; margin-left: -2px; border-radius: 2px; background: var(--soft); transition: background .14s, box-shadow .14s; }
.mk-off { bottom: 13px; width: 13px; height: 13px; margin-left: -6.5px; border-radius: 50%; border: 3px solid var(--soft); }
.rocker[data-pos=on] .mk-on { background: var(--neon); box-shadow: 0 0 9px 1px var(--neon); }
.half { position: absolute; left: 0; right: 0; height: 50%; margin: 0; padding: 0; border: 0; background: transparent; cursor: pointer; -webkit-tap-highlight-color: transparent; }
.half.top { top: 0; border-radius: 12px 12px 0 0; }
.half.bottom { bottom: 0; border-radius: 0 0 12px 12px; }
.half:focus-visible { outline-offset: -3px; }

/* Edit mode */
.hint { color: var(--soft); font-size: .92rem; margin: 14px 0 0; max-width: 62ch; }
.tile.edit { flex-wrap: wrap; align-items: flex-end; gap: 10px; padding: 14px; }
.fld { display: flex; flex-direction: column; gap: 4px; font-size: .82rem; color: var(--soft); }
.fld.grow { flex: 1 1 150px; }
.fld.num { width: 92px; }
input[type=text], input[type=number], select {
  font: inherit; font-size: 1rem; color: var(--ink); background: var(--field);
  border: 1px solid var(--edge); border-radius: 8px; padding: 8px 10px; min-height: 40px; width: 100%;
}
.add-room { display: block; width: 100%; margin-top: 26px; padding: 16px; border-style: dashed; }

.empty { margin-top: 40px; max-width: 52ch; }
.empty h2 { font-size: 1.3rem; margin-bottom: 10px; }
.empty ol { padding-left: 1.2em; margin: 0 0 18px; }
.empty li { margin-bottom: 6px; }
.row { display: flex; gap: 10px; flex-wrap: wrap; }

/* Toast */
#toast {
  display: none; position: fixed; left: 50%; transform: translateX(-50%);
  bottom: calc(16px + env(safe-area-inset-bottom)); z-index: 10;
  width: max-content; max-width: min(520px, calc(100vw - 24px));
  align-items: center; gap: 12px; padding: 12px 14px;
  background: var(--ink); color: var(--bg); border-radius: 12px;
  box-shadow: 0 6px 24px rgba(0,0,0,.3); font-size: .95rem;
}
#toast.show { display: flex; }
#toast .btn { color: var(--bg); border-color: color-mix(in srgb, var(--bg) 50%, transparent); flex: none; }

/* Settings dialog */
dialog {
  width: calc(100% - 32px); max-width: 460px; padding: 22px;
  color: var(--ink); background: var(--plate);
  border: 1px solid var(--edge); border-radius: 16px;
}
dialog::backdrop { background: rgba(8, 12, 16, .55); }
dialog h2 { font-size: 1.25rem; margin-bottom: 14px; }
dialog h3 { font-size: 1rem; margin: 20px 0 4px; }
dialog p, .help { color: var(--soft); font-size: .9rem; margin: 6px 0 0; }
.check { display: flex; gap: 10px; align-items: flex-start; margin-top: 16px; }
.check input { margin-top: 4px; width: 18px; height: 18px; flex: none; accent-color: var(--ink); }
.status { min-height: 1.4em; margin-top: 10px; font-size: .92rem; }
.status.good { color: var(--good); }
.status.bad { color: var(--danger); }
.dlg-foot { display: flex; justify-content: flex-end; gap: 10px; margin-top: 22px; }

@media (prefers-reduced-motion: reduce) { * { transition: none !important; } }
</style>
</head>
<body>
<header class="top">
  <h1>Switchboard</h1>
  <div class="actions">
    <button type="button" class="btn" id="editBtn" aria-pressed="false">Edit</button>
    <button type="button" class="btn" id="settingsBtn">Settings</button>
  </div>
</header>
<main id="root"><p class="hint">Loading…</p></main>
<div id="toast" role="status" aria-live="polite"></div>

<dialog id="settings" aria-labelledby="settingsTitle">
  <h2 id="settingsTitle">Settings</h2>

  <label class="fld" for="setIp">Link address (optional)</label>
  <input type="text" id="setIp" placeholder="Find automatically" autocomplete="off" autocapitalize="off" spellcheck="false">
  <p class="help">Leave blank to broadcast to your whole network. If commands don't get through, enter the Link's IP address, which is listed in your router's connected devices.</p>

  <label class="check">
    <input type="checkbox" id="setConfirm">
    <span>Wait for the Link to confirm each command
      <span class="help" style="display:block">Turn this off if sockets switch but this panel still reports an error.</span></span>
  </label>

  <h3>Pair with your Link</h3>
  <p>The Link only accepts commands from devices it has been paired with. Pair once, using the computer that runs this panel.</p>
  <div class="row" style="margin-top:10px"><button type="button" class="btn" id="pairBtn">Pair now</button></div>
  <div class="status" id="pairStatus" aria-live="polite"></div>

  <h3>What the switches show</h3>
  <p>The Link can't report which sockets are on. Each rocker shows the last command sent from this panel, so it won't notice a remote or a button press on the socket.</p>

  <div class="dlg-foot">
    <button type="button" class="btn" id="closeSettings">Close</button>
    <button type="button" class="btn primary" id="saveSettings">Save settings</button>
  </div>
</dialog>

<script>
(() => {
  'use strict';
  const $ = (sel, el = document) => el.querySelector(sel);
  const esc = s => String(s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  const MAX_LEVEL = 32, DEFAULT_LEVEL = 24;
  const pct = l => Math.round(l / MAX_LEVEL * 100);
  const keyOf = (room, dev) => room + '-' + dev;
  const root = $('#root');
  const dlg = $('#settings');

  let cfg = { rooms: [], state: {}, link_ip: '', require_reply: true };
  let editing = false;
  let saveTimer = null;

  /* ---------- server calls ---------- */
  async function api(path, body) {
    try {
      const res = await fetch(path, body === undefined ? {} : {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(body)
      });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) {
        data.ok = false;
        if (!data.error) data.error = 'The panel server returned an error (' + res.status + ').';
      }
      return data;
    } catch (err) {
      return { ok: false, error: "Can't reach the panel server. Check that it's still running on your computer." };
    }
  }

  /* ---------- toast ---------- */
  let toastTimer;
  const hideToast = () => { $('#toast').className = ''; };
  function toast(msg, opts = {}) {
    const el = $('#toast');
    el.textContent = '';
    const span = document.createElement('span');
    span.textContent = msg;
    el.append(span);
    if (opts.action) {
      const b = document.createElement('button');
      b.type = 'button'; b.className = 'btn small'; b.textContent = opts.action.label;
      b.addEventListener('click', () => { hideToast(); opts.action.run(); });
      el.append(b);
    }
    el.className = 'show';
    clearTimeout(toastTimer);
    toastTimer = setTimeout(hideToast, opts.error ? 9000 : 3000);
  }
  function fail(data) {
    toast(data.error || 'Something went wrong.', {
      error: true,
      action: data.needs_pairing ? { label: 'Open settings', run: openSettings } : null
    });
  }

  /* ---------- rendering ---------- */
  const posOf = st => (!st ? 'none' : st.on ? 'on' : 'off');
  function stateText(d, st) {
    if (!st) return 'Not switched yet';
    if (!st.on) return 'Off';
    return d.type === 'dimmer' ? 'On, ' + pct(st.level || DEFAULT_LEVEL) + '%' : 'On';
  }

  function tileHTML(r, d, ri, di) {
    const st = cfg.state[keyOf(r.num, d.num)];
    const n = esc(d.name);
    const ids = `data-ri="${ri}" data-di="${di}"`;
    if (editing) {
      return `<div class="tile edit">
        <label class="fld grow">Name<input type="text" maxlength="40" data-f="dname" ${ids} value="${n}"></label>
        <label class="fld num">Device number<input type="number" min="1" max="99" inputmode="numeric" data-f="dnum" ${ids} value="${d.num}"></label>
        <label class="fld">Type<select data-f="dtype" ${ids}>
          <option value="onoff"${d.type === 'dimmer' ? '' : ' selected'}>On and off</option>
          <option value="dimmer"${d.type === 'dimmer' ? ' selected' : ''}>Dimmer</option>
        </select></label>
        <button type="button" class="btn small danger" data-act="del-dev" ${ids}>Remove</button>
      </div>`;
    }
    const slider = d.type === 'dimmer'
      ? `<input type="range" min="1" max="${MAX_LEVEL}" value="${(st && st.level) || DEFAULT_LEVEL}" data-act="dim" ${ids} data-fid="dim-${ri}-${di}" aria-label="${n} brightness">`
      : '';
    return `<div class="tile" ${ids}>
      <div class="rocker" role="group" aria-label="${n}" data-pos="${posOf(st)}">
        <span class="face" aria-hidden="true"><i class="mk-on"></i><i class="mk-off"></i></span>
        <button type="button" class="half top" data-act="on" ${ids} data-fid="on-${ri}-${di}" aria-pressed="${!!st && st.on}" aria-label="Turn ${n} on"></button>
        <button type="button" class="half bottom" data-act="off" ${ids} data-fid="off-${ri}-${di}" aria-pressed="${!!st && !st.on}" aria-label="Turn ${n} off"></button>
      </div>
      <div class="info"><div class="name">${n}</div><div class="state">${stateText(d, st)}</div>${slider}</div>
    </div>`;
  }

  function roomHTML(r, ri) {
    const head = editing
      ? `<div class="room-head edit">
          <label class="fld grow">Room name<input type="text" maxlength="40" data-f="rname" data-ri="${ri}" value="${esc(r.name)}"></label>
          <label class="fld num">Room number<input type="number" min="1" max="99" inputmode="numeric" data-f="rnum" data-ri="${ri}" value="${r.num}"></label>
          <button type="button" class="btn small danger" data-act="del-room" data-ri="${ri}">Delete room</button>
        </div>`
      : `<div class="room-head"><h2>${esc(r.name)}</h2>${r.devices.length ? `<button type="button" class="btn small" data-act="room-off" data-ri="${ri}">All off</button>` : ''}</div>`;
    const tiles = r.devices.map((d, di) => tileHTML(r, d, ri, di)).join('');
    const add = editing ? `<div style="margin-top:12px"><button type="button" class="btn small" data-act="add-dev" data-ri="${ri}" data-fid="add-dev-${ri}">Add device</button></div>` : '';
    return `<section class="room">${head}<div class="grid">${tiles}</div>${add}</section>`;
  }

  function emptyHTML() {
    return `<div class="empty">
      <h2>Pair, then add your sockets</h2>
      <ol>
        <li>Open Settings and pair this panel with your Link.</li>
        <li>Choose Edit, add a room, then add each socket using the room and device numbers you paired it to.</li>
      </ol>
      <div class="row">
        <button type="button" class="btn primary" data-act="open-settings">Open settings</button>
        <button type="button" class="btn" data-act="start-edit">Add rooms and sockets</button>
      </div>
    </div>`;
  }

  function render() {
    const active = document.activeElement;
    const focusId = active && active.dataset ? active.dataset.fid : null;
    $('#editBtn').textContent = editing ? 'Done' : 'Edit';
    $('#editBtn').setAttribute('aria-pressed', String(editing));
    if (!cfg.rooms.length && !editing) {
      root.innerHTML = emptyHTML();
    } else {
      const hint = editing
        ? '<p class="hint">Room and device numbers are the ones you paired each socket to, matching your LightwaveRF remote. Changes save as you go.</p>'
        : '';
      root.innerHTML = hint + cfg.rooms.map(roomHTML).join('') +
        (editing ? '<button type="button" class="btn add-room" data-act="add-room">Add a room</button>' : '');
    }
    if (focusId) {
      const el = root.querySelector('[data-fid="' + focusId + '"]');
      if (el) el.focus();
    }
  }

  // Update switch positions in place so the rocker can animate.
  function paintAll() {
    cfg.rooms.forEach((r, ri) => r.devices.forEach((d, di) => {
      const el = root.querySelector('.tile[data-ri="' + ri + '"][data-di="' + di + '"]');
      if (!el || editing) return;
      const st = cfg.state[keyOf(r.num, d.num)];
      el.querySelector('.rocker').dataset.pos = posOf(st);
      el.querySelector('.top').setAttribute('aria-pressed', String(!!st && st.on));
      el.querySelector('.bottom').setAttribute('aria-pressed', String(!!st && !st.on));
      el.querySelector('.state').textContent = stateText(d, st);
      const rng = el.querySelector('input[type=range]');
      if (rng && st && st.level) rng.value = st.level;
    }));
  }

  /* ---------- switching ---------- */
  async function sendDevice(ri, di, action, level) {
    const r = cfg.rooms[ri], d = r.devices[di], key = keyOf(r.num, d.num);
    const prev = cfg.state[key];
    const lvl = level || (prev && prev.level) || DEFAULT_LEVEL;
    const next = { on: action !== 'off' };
    if (d.type === 'dimmer') next.level = lvl;
    cfg.state[key] = next;
    paintAll();
    const data = await api('/api/command', { room: r.num, device: d.num, action, level: lvl });
    if (data.ok) {
      cfg.state = data.state;
    } else {
      if (prev) cfg.state[key] = prev; else delete cfg.state[key];
      fail(data);
    }
    paintAll();
  }

  async function sendRoomOff(ri) {
    const r = cfg.rooms[ri];
    const saved = {};
    r.devices.forEach(d => {
      const k = keyOf(r.num, d.num);
      saved[k] = cfg.state[k];
      cfg.state[k] = Object.assign({}, cfg.state[k], { on: false });
    });
    paintAll();
    const data = await api('/api/command', { room: r.num, action: 'room_off' });
    if (data.ok) {
      cfg.state = data.state;
    } else {
      Object.keys(saved).forEach(k => { if (saved[k]) cfg.state[k] = saved[k]; else delete cfg.state[k]; });
      fail(data);
    }
    paintAll();
  }

  /* ---------- editing ---------- */
  const payload = () => ({ rooms: cfg.rooms, link_ip: cfg.link_ip, require_reply: cfg.require_reply });
  async function saveNow() {
    clearTimeout(saveTimer); saveTimer = null;
    const data = await api('/api/config', payload());
    if (data.ok) { cfg.state = data.state; return true; }
    toast(data.error, { error: true });
    return false;
  }
  function scheduleSave() { clearTimeout(saveTimer); saveTimer = setTimeout(saveNow, 500); }

  function nextFree(list, min = 1) {
    let n = min;
    while (list.includes(n)) n++;
    return n;
  }

  function onEditInput(t) {
    const r = cfg.rooms[+t.dataset.ri];
    if (!r) return;
    const d = r.devices[+t.dataset.di];
    if (t.dataset.f === 'rname') r.name = t.value.trim() || 'Room ' + r.num;
    else if (t.dataset.f === 'dname' && d) d.name = t.value.trim() || 'Device ' + d.num;
    else return;
    scheduleSave();
  }

  function onEditChange(t) {
    const ri = +t.dataset.ri, r = cfg.rooms[ri];
    if (!r) return;
    const f = t.dataset.f;
    if (f === 'rnum') {
      const n = parseInt(t.value, 10);
      const clash = cfg.rooms.some((x, i) => i !== ri && x.num === n);
      if (!(n >= 1 && n <= 99) || clash) {
        toast(clash ? 'Room number ' + n + ' is already used by another room.' : 'Room numbers run from 1 to 99.', { error: true });
        t.value = r.num; return;
      }
      r.num = n;
    } else if (f === 'dnum' || f === 'dtype') {
      const d = r.devices[+t.dataset.di];
      if (!d) return;
      if (f === 'dtype') {
        d.type = t.value === 'dimmer' ? 'dimmer' : 'onoff';
      } else {
        const n = parseInt(t.value, 10);
        const clash = r.devices.some(x => x !== d && x.num === n);
        if (!(n >= 1 && n <= 99) || clash) {
          toast(clash ? 'Device number ' + n + ' is already used in this room.' : 'Device numbers run from 1 to 99.', { error: true });
          t.value = d.num; return;
        }
        d.num = n;
      }
    } else return;
    scheduleSave();
  }

  async function setEditing(on) {
    if (!on && saveTimer) await saveNow();
    editing = on;
    render();
  }

  /* ---------- events ---------- */
  root.addEventListener('click', e => {
    const b = e.target.closest('[data-act]');
    if (!b) return;
    const act = b.dataset.act, ri = +b.dataset.ri, di = +b.dataset.di;
    switch (act) {
      case 'on': case 'off': sendDevice(ri, di, act); break;
      case 'room-off': sendRoomOff(ri); break;
      case 'open-settings': openSettings(); break;
      case 'start-edit': setEditing(true); break;
      case 'add-room': {
        const num = nextFree(cfg.rooms.map(r => r.num));
        cfg.rooms.push({ num, name: 'Room ' + num, devices: [] });
        render(); scheduleSave();
        const inputs = root.querySelectorAll('[data-f="rname"]');
        if (inputs.length) { const last = inputs[inputs.length - 1]; last.focus(); last.select(); }
        break;
      }
      case 'add-dev': {
        const r = cfg.rooms[ri];
        const num = nextFree(r.devices.map(d => d.num));
        r.devices.push({ num, name: 'Socket ' + num, type: 'onoff' });
        render(); scheduleSave();
        const names = root.querySelectorAll('[data-f="dname"][data-ri="' + ri + '"]');
        if (names.length) { const last = names[names.length - 1]; last.focus(); last.select(); }
        break;
      }
      case 'del-room': {
        const r = cfg.rooms[ri];
        const n = r.devices.length;
        if (!confirm('Delete ' + r.name + (n ? ' and its ' + n + (n === 1 ? ' device?' : ' devices?') : '?'))) break;
        cfg.rooms.splice(ri, 1); render(); scheduleSave();
        break;
      }
      case 'del-dev': {
        const r = cfg.rooms[ri];
        if (!confirm('Remove ' + r.devices[di].name + '?')) break;
        r.devices.splice(di, 1); render(); scheduleSave();
        break;
      }
    }
  });

  root.addEventListener('input', e => {
    const t = e.target;
    if (t.dataset.f) { onEditInput(t); return; }
    if (t.dataset.act === 'dim') {   // live percentage while dragging
      t.closest('.tile').querySelector('.state').textContent = 'On, ' + pct(+t.value) + '%';
    }
  });

  root.addEventListener('change', e => {
    const t = e.target;
    if (t.dataset.f) { onEditChange(t); if (t.dataset.f === 'dtype') render(); return; }
    if (t.dataset.act === 'dim') sendDevice(+t.dataset.ri, +t.dataset.di, 'dim', +t.value);
  });

  $('#editBtn').addEventListener('click', () => setEditing(!editing));
  $('#settingsBtn').addEventListener('click', openSettings);

  /* ---------- settings ---------- */
  function openSettings() {
    $('#setIp').value = cfg.link_ip || '';
    $('#setConfirm').checked = !!cfg.require_reply;
    $('#pairStatus').textContent = '';
    if (!dlg.open) dlg.showModal();
  }
  $('#closeSettings').addEventListener('click', () => dlg.close());
  $('#saveSettings').addEventListener('click', async () => {
    cfg.link_ip = $('#setIp').value.trim();
    cfg.require_reply = $('#setConfirm').checked;
    if (await saveNow()) { dlg.close(); toast('Settings saved'); }
  });
  $('#pairBtn').addEventListener('click', async () => {
    const btn = $('#pairBtn'), out = $('#pairStatus');
    cfg.link_ip = $('#setIp').value.trim();
    cfg.require_reply = $('#setConfirm').checked;
    btn.disabled = true;
    out.className = 'status';
    out.textContent = 'Saving settings…';
    if (!(await saveNow())) { btn.disabled = false; out.textContent = ''; return; }
    out.textContent = 'Press the button on your Link now. Waiting up to 30 seconds…';
    const data = await api('/api/pair', {});
    btn.disabled = false;
    if (data.ok) {
      out.className = 'status good';
      out.textContent = 'Paired. You can now switch sockets from this panel.';
    } else {
      out.className = 'status bad';
      out.textContent = data.error;
    }
  });

  /* ---------- start up ---------- */
  async function refresh(first) {
    if (editing || (saveTimer && !first)) return;
    const data = await api('/api/config');
    if (!data.ok) {
      if (first) {
        root.innerHTML = '<div class="empty"><h2>Can\u2019t reach the panel</h2><p>' + esc(data.error) + '</p></div>';
      }
      return;
    }
    const sameRooms = JSON.stringify(data.rooms) === JSON.stringify(cfg.rooms);
    cfg = data;
    if (sameRooms && !first) paintAll(); else render();
  }
  document.addEventListener('visibilitychange', () => { if (!document.hidden) refresh(false); });
  refresh(true);
})();
</script>
</body>
</html>
"""

if __name__ == "__main__":
    main()
