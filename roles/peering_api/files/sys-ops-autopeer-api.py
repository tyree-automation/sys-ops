#!/usr/bin/env python3
"""
sys-ops automatic peering API (stdlib only).

Endpoints:
  GET  /api/peering/info          — this node's peering details
  POST /api/peering/request       — submit a peering request
  GET  /api/peering/request/<id>  — check a request's status

Requests are queued under /var/lib/sys-ops-autopeer/{pending,applied,
approved,rejected}. With auto_apply enabled, valid requests are applied
immediately: a WireGuard tunnel (dn42a-<name>) plus a BIRD session in
/etc/bird/peers/. Otherwise an operator reviews them with
scripts/peering-requests.py.

Configuration: /etc/sys-ops/autopeer.json (templated by Ansible).
"""

import ipaddress
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

CONFIG_PATH = os.environ.get("AUTOPEER_CONFIG", "/etc/sys-ops/autopeer.json")
SPOOL = "/var/lib/sys-ops-autopeer"
WG_DIR = "/etc/wireguard"
BIRD_PEERS_DIR = "/etc/bird/peers"
MAX_BODY = 8192

RE_NAME = re.compile(r"^[a-z0-9]{2,8}$")
RE_WG_KEY = re.compile(r"^[A-Za-z0-9+/]{42}[AEIMQUYcgkosw048]=$")
RE_HOST = re.compile(r"^[A-Za-z0-9][A-Za-z0-9.\-]{0,253}$")

with open(CONFIG_PATH, encoding="utf-8") as _f:
    CFG = json.load(_f)


def now_utc():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def list_requests(*states):
    out = []
    for state in states:
        directory = os.path.join(SPOOL, state)
        for fn in sorted(os.listdir(directory)):
            if fn.endswith(".json"):
                with open(os.path.join(directory, fn), encoding="utf-8") as f:
                    out.append(json.load(f))
    return out


def node_pubkey():
    try:
        with open(os.path.join(WG_DIR, "dn42-publickey"), encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def our_info():
    return {
        "node": CFG["node"],
        "asn": CFG["asn"],
        "wg_pubkey": node_pubkey(),
        "endpoint": CFG.get("endpoint", ""),
        "link_local6": CFG["link_local6"],
        "port_scheme": f"{CFG['port_base']} + (your ASN mod 10000)",
        "auto_apply": bool(CFG.get("auto_apply")),
    }


def alloc_port(asn):
    return int(CFG["port_base"]) + asn % 10000


def port_in_use(port):
    for fn in os.listdir(WG_DIR):
        if fn.endswith(".conf"):
            try:
                with open(os.path.join(WG_DIR, fn), encoding="utf-8") as f:
                    if re.search(rf"^ListenPort\s*=\s*{port}$", f.read(), re.M):
                        return True
            except OSError:
                continue
    return False


def registry_lookup(asn):
    """Best-effort existence check against a dn42 registry explorer."""
    if not CFG.get("registry_check", True):
        return "skipped"
    url = CFG.get("registry_api", "https://explorer.burble.com/api/registry") \
        + f"/aut-num/AS{asn}"
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            if resp.status == 200 and b"aut-num" in resp.read(4096):
                return "found"
        return "not-found"
    except Exception:
        return "unknown"


class ValidationError(Exception):
    pass


def validate(body, client_ip):
    if not isinstance(body, dict):
        raise ValidationError("body must be a JSON object")

    try:
        asn = int(body.get("asn", 0))
    except (TypeError, ValueError):
        raise ValidationError("asn must be a number")
    if not (int(CFG.get("asn_min", 4242420000)) <= asn <= int(CFG.get("asn_max", 4242429999))):
        raise ValidationError("asn outside the accepted dn42 range")
    if asn == int(CFG["asn"]):
        raise ValidationError("that is this network's ASN")

    name = str(body.get("name") or f"as{asn % 10000}").lower()
    if not RE_NAME.match(name):
        raise ValidationError("name must be 2-8 chars [a-z0-9]")

    wg_pubkey = str(body.get("wg_pubkey", "")).strip()
    if not RE_WG_KEY.match(wg_pubkey):
        raise ValidationError("wg_pubkey is not a valid WireGuard public key")
    if wg_pubkey == node_pubkey():
        raise ValidationError("wg_pubkey is this node's own key")

    peer_v6 = str(body.get("peer_v6", "")).strip().lower()
    try:
        addr = ipaddress.IPv6Address(peer_v6)
    except ValueError:
        raise ValidationError("peer_v6 must be a valid IPv6 address")
    if not addr.is_link_local:
        raise ValidationError("peer_v6 must be link-local (fe80::/10)")

    endpoint = str(body.get("endpoint", "")).strip()
    if endpoint:
        host, sep, port = endpoint.rpartition(":")
        if not sep or not port.isdigit() or not 0 < int(port) < 65536:
            raise ValidationError("endpoint must be host:port")
        if not RE_HOST.match(host.strip("[]")):
            raise ValidationError("endpoint host looks invalid")

    contact = str(body.get("contact", ""))[:120]

    # Duplicates / abuse limits.
    existing = list_requests("pending", "applied")
    if any(r["asn"] == asn for r in existing):
        raise ValidationError("a request for this ASN is already queued")
    if sum(1 for r in existing if r.get("client_ip") == client_ip) >= 3:
        raise ValidationError("too many open requests from your address")
    if len(existing) >= int(CFG.get("max_open_requests", 20)):
        raise ValidationError("request queue is full — contact the operator")
    iface = f"dn42a-{name}"
    if os.path.exists(os.path.join(WG_DIR, f"{iface}.conf")) or any(
        r["name"] == name for r in existing
    ):
        raise ValidationError("that peer name is taken — pick another")
    port = alloc_port(asn)
    if port_in_use(port):
        raise ValidationError(
            f"port {port} is already in use here — contact the operator")

    return {
        "id": f"{time.strftime('%Y%m%d%H%M%S')}-as{asn}",
        "received": now_utc(),
        "client_ip": client_ip,
        "status": "pending",
        "name": name,
        "asn": asn,
        "wg_pubkey": wg_pubkey,
        "endpoint": endpoint,
        "peer_v6": peer_v6,
        "mp_bgp": bool(body.get("mp_bgp", True)),
        "contact": contact,
        "port": port,
    }


def write_request(request, state):
    request["status"] = state
    path = os.path.join(SPOOL, state, f"{request['id']}.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(request, f, indent=2)
    return path


def run(*argv):
    return subprocess.run(list(argv), capture_output=True, text=True, check=False)


def apply_request(request):
    """Configure the tunnel + BGP session right now (auto_apply mode)."""
    iface = f"dn42a-{request['name']}"
    with open(os.path.join(WG_DIR, "dn42-privatekey"), encoding="utf-8") as f:
        privkey = f.read().strip()

    wg_conf = [
        "# Managed by sys-ops-autopeer — request " + request["id"],
        "[Interface]",
        f"PrivateKey = {privkey}",
        f"ListenPort = {request['port']}",
        f"Address = {CFG['link_local6']}/64",
        "Table = off",
        "",
        "[Peer]",
        f"PublicKey = {request['wg_pubkey']}",
    ]
    if request["endpoint"]:
        wg_conf += [f"Endpoint = {request['endpoint']}", "PersistentKeepalive = 25"]
    wg_conf.append("AllowedIPs = 0.0.0.0/0, ::/0")
    wg_path = os.path.join(WG_DIR, f"{iface}.conf")
    fd = os.open(wg_path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as f:
        f.write("\n".join(wg_conf) + "\n")

    proto = re.sub(r"[^a-z0-9]", "_", request["name"])
    bird_conf = [
        "# Managed by sys-ops-autopeer — request " + request["id"],
        f"protocol bgp dn42a_{proto} from dnpeers {{",
        f"  neighbor {request['peer_v6']} % '{iface}' as {request['asn']};",
    ]
    if request["mp_bgp"]:
        bird_conf += ["  ipv4 {", "    extended next hop on;", "  };"]
    else:
        bird_conf += ["  ipv4 { import none; export none; };"]
    bird_conf.append("}")
    with open(os.path.join(BIRD_PEERS_DIR, f"dn42a_{proto}.conf"), "w",
              encoding="utf-8") as f:
        f.write("\n".join(bird_conf) + "\n")

    steps = [
        ("wg-quick", run("systemctl", "enable", "--now", f"wg-quick@{iface}")),
        ("bird", run("birdc", "configure")),
    ]
    if shutil.which("ufw"):
        run("ufw", "allow", f"{request['port']}/udp",
            "comment", f"dn42 autopeer {request['name']}")
        run("ufw", "allow", "in", "on", iface)
        run("ufw", "route", "allow", "in", "on", iface)
    errors = [name for name, proc in steps if proc.returncode != 0]
    if errors:
        raise RuntimeError("failed steps: " + ", ".join(errors))


class Handler(BaseHTTPRequestHandler):
    server_version = "sys-ops-autopeer"

    def send_json(self, code, payload):
        data = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        path = self.path.split("?", 1)[0].rstrip("/")
        if path == "/api/peering/info":
            return self.send_json(200, our_info())
        match = re.fullmatch(r"/api/peering/request/([A-Za-z0-9\-]+)", path)
        if match:
            for req in list_requests("pending", "applied", "approved", "rejected"):
                if req["id"] == match.group(1):
                    return self.send_json(200, {
                        "id": req["id"], "status": req["status"],
                        "name": req["name"], "asn": req["asn"],
                    })
            return self.send_json(404, {"error": "unknown request id"})
        return self.send_json(404, {"error": "not found"})

    def do_POST(self):
        if self.path.split("?", 1)[0].rstrip("/") != "/api/peering/request":
            return self.send_json(404, {"error": "not found"})
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_BODY:
            return self.send_json(413, {"error": "request body too large"})
        try:
            body = json.loads(self.rfile.read(length).decode("utf-8"))
            request = validate(body, self.client_address[0])
        except (ValueError, ValidationError) as exc:
            return self.send_json(400, {"error": str(exc)})

        request["registry"] = registry_lookup(request["asn"])

        if CFG.get("auto_apply") and request["registry"] in ("found", "skipped"):
            try:
                apply_request(request)
            except Exception as exc:  # keep the request queued for review
                request["error"] = str(exc)
                write_request(request, "pending")
                return self.send_json(202, {
                    "id": request["id"], "status": "pending",
                    "detail": "auto-apply failed; queued for operator review",
                    "our": our_info(),
                })
            write_request(request, "applied")
            return self.send_json(201, {
                "id": request["id"], "status": "applied",
                "detail": "tunnel and BGP session are live",
                "our": {**our_info(), "port": request["port"]},
            })

        write_request(request, "pending")
        return self.send_json(202, {
            "id": request["id"], "status": "pending",
            "detail": "queued for operator review",
            "our": {**our_info(), "port": request["port"]},
        })

    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.client_address[0], fmt % args))


def main():
    for state in ("pending", "applied", "approved", "rejected"):
        os.makedirs(os.path.join(SPOOL, state), exist_ok=True)
    listen = (CFG.get("listen", "0.0.0.0"), int(CFG.get("port", 8042)))
    print(f"sys-ops-autopeer listening on {listen[0]}:{listen[1]} "
          f"(auto_apply={'on' if CFG.get('auto_apply') else 'off'})")
    ThreadingHTTPServer(listen, Handler).serve_forever()


if __name__ == "__main__":
    main()
