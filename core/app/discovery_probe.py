"""Camera discovery probe (standard library only).

Runs in a short-lived container on the HOST network (started by core), because multicast discovery
cannot leave Docker's internal network. Prints one JSON object on the last line of stdout.

    python discovery_probe.py '{"ranges": ["192.168.1.0/24"], "timeout": 4}'

Methods, merged by IP address:
  * ONVIF WS-Discovery (multicast 239.255.255.250:3702) - all ONVIF cameras and NVRs
  * Hikvision SADP (multicast 239.255.255.250:37020) - also inactive / ONVIF-disabled Hikvision
  * RTSP port scan of the local /24 networks (or the given ranges) - anything with port 554 open
"""
import concurrent.futures
import ipaddress
import json
import re
import select
import socket
import struct
import sys
import time
import urllib.parse
import uuid

MCAST = "239.255.255.250"
SKIP_IF = ("lo", "docker", "br-", "veth", "virbr", "cni", "flannel", "tun", "tap", "wg", "zt", "tailscale")
VENDORS = [  # (brand id, display name, keywords)
    ("hikvision", "Hikvision", ("hikvision", "hiwatch", "app-webs", "dvrdvs", "hikv")),
    ("dahua", "Dahua", ("dahua", "dh-ipc", "ipc-hfw", "ipc-hdw", "ipc-hdbw", "imou")),
    ("axis", "Axis", ("axis",)),
    ("provision", "Provision-ISR", ("provision", "tvt")),
    ("flir", "FLIR", ("flir",)),
]
WSD_PROBE = ('<?xml version="1.0" encoding="UTF-8"?>'
             '<e:Envelope xmlns:e="http://www.w3.org/2003/05/soap-envelope" '
             'xmlns:w="http://schemas.xmlsoap.org/ws/2004/08/addressing" '
             'xmlns:d="http://schemas.xmlsoap.org/ws/2005/04/discovery" '
             'xmlns:dn="http://www.onvif.org/ver10/network/wsdl"><e:Header>'
             '<w:MessageID>uuid:{id}</w:MessageID>'
             '<w:To e:mustUnderstand="true">urn:schemas-xmlsoap-org:ws:2005:04:discovery</w:To>'
             '<w:Action e:mustUnderstand="true">http://schemas.xmlsoap.org/ws/2005/04/discovery/Probe</w:Action>'
             '</e:Header><e:Body><d:Probe>{types}</d:Probe></e:Body></e:Envelope>')


def local_networks() -> list[tuple[str, str, ipaddress.IPv4Network]]:
    import fcntl
    out = []
    for _, name in socket.if_nameindex():
        if name.startswith(SKIP_IF):
            continue
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            req = struct.pack("256s", name[:15].encode())
            ip = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x8915, req)[20:24])     # SIOCGIFADDR
            mask = socket.inet_ntoa(fcntl.ioctl(s.fileno(), 0x891B, req)[20:24])   # SIOCGIFNETMASK
        except OSError:
            continue
        finally:
            s.close()
        net = ipaddress.ip_network(f"{ip}/{mask}", strict=False)
        if net.prefixlen < 24:          # big networks: scan only our own /24
            net = ipaddress.ip_network(f"{ip}/24", strict=False)
        out.append((name, ip, net))
    return out


def parse_ranges(text: list[str]) -> list[str]:
    hosts: list[str] = []
    for part in text:
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = [x.strip() for x in part.split("-", 1)]
            if "." not in b:
                b = a.rsplit(".", 1)[0] + "." + b
            lo, hi = int(ipaddress.IPv4Address(a)), int(ipaddress.IPv4Address(b))
            hosts += [str(ipaddress.IPv4Address(i)) for i in range(lo, hi + 1)]
        else:
            net = ipaddress.ip_network(part, strict=False)
            hosts += [str(h) for h in (net.hosts() if net.num_addresses > 1 else [net.network_address])]
    return hosts


def vendor_of(text: str) -> tuple[str, str]:
    low = text.lower()
    for brand, display, words in VENDORS:
        if any(w in low for w in words):
            return brand, display
    return "", ""


# ---------------------------------------------------------------- multicast discovery
def parse_wsd(xml: str) -> dict | None:
    xaddrs = re.search(r"<[^>]*XAddrs>([^<]+)<", xml)
    if not xaddrs:
        return None
    ip, port = None, 80
    for u in xaddrs.group(1).split():
        p = urllib.parse.urlsplit(u)
        if p.hostname and re.fullmatch(r"\d+\.\d+\.\d+\.\d+", p.hostname):
            ip, port = p.hostname, p.port or 80
            break
    if not ip:
        return None
    scopes = re.search(r"<[^>]*Scopes[^>]*>([^<]*)<", xml)
    info: dict[str, str] = {}
    for sc in (scopes.group(1).split() if scopes else []):
        m = re.match(r"onvif://www\.onvif\.org/(name|hardware|mac|MAC|location/[^/]*)/(.+)", sc)
        if m:
            info.setdefault(m.group(1).lower().split("/")[0], urllib.parse.unquote(m.group(2)).replace("_", " "))
    kind = "NVR" if re.search(r"NetworkVideoDisplay|Device\b", xml) and "NetworkVideoTransmitter" not in xml else ""
    return {"ip": ip, "onvif_port": port, "name": info.get("name", ""), "model": info.get("hardware", ""),
            "mac": info.get("mac", ""), "kind": kind, "source": "ONVIF"}


def parse_sadp(xml: str) -> dict | None:
    def tag(t):
        m = re.search(rf"<{t}>([^<]*)</{t}>", xml)
        return m.group(1).strip() if m else ""
    ip = tag("IPv4Address")
    if not ip:
        return None
    active = tag("Activated").lower()
    return {"ip": ip, "model": tag("DeviceDescription") or tag("DeviceType"), "mac": tag("MAC").replace("-", ":"),
            "http_port": int(tag("HttpPort") or 80), "name": "", "vendor_hint": "hikvision", "source": "SADP",
            "note": "not activated - activate it with SADP or its web page first" if active == "false" else ""}


def multicast(local_ips: list[str], timeout: float) -> list[dict]:
    socks = []
    for ip in local_ips or ["0.0.0.0"]:
        for port, payloads in ((3702, [WSD_PROBE.format(id=uuid.uuid4(), types="<d:Types>dn:NetworkVideoTransmitter</d:Types>"),
                                       WSD_PROBE.format(id=uuid.uuid4(), types="")]),
                               (37020, [f'<?xml version="1.0" encoding="utf-8"?><Probe><Uuid>{str(uuid.uuid4()).upper()}'
                                        "</Uuid><Types>inquiry</Types></Probe>"])):
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
                if ip != "0.0.0.0":
                    s.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_IF, socket.inet_aton(ip))
                s.bind((ip, 0))
                for p in payloads:
                    s.sendto(p.encode(), (MCAST, port))
                socks.append((s, port))
            except OSError:
                s.close()
    found, end = [], time.time() + timeout
    while socks and time.time() < end:
        ready, _, _ = select.select([s for s, _ in socks], [], [], max(0.05, end - time.time()))
        for s in ready:
            try:
                data, _ = s.recvfrom(65535)
            except OSError:
                continue
            port = dict((id(x), p) for x, p in socks)[id(s)]
            d = (parse_wsd if port == 3702 else parse_sadp)(data.decode(errors="replace"))
            if d:
                found.append(d)
    for s, _ in socks:
        s.close()
    return found


# ---------------------------------------------------------------- RTSP port scan
def _banner(ip: str, port: int, request: bytes, timeout: float) -> str:
    try:
        with socket.create_connection((ip, port), timeout=timeout) as c:
            c.settimeout(timeout)
            c.sendall(request)
            return c.recv(4096).decode(errors="replace")
    except OSError:
        return ""


def check_host(ip: str, rtsp_port: int, timeout: float) -> dict | None:
    try:
        socket.create_connection((ip, rtsp_port), timeout=timeout).close()
    except OSError:
        return None
    rtsp = _banner(ip, rtsp_port, f"OPTIONS rtsp://{ip}:{rtsp_port}/ RTSP/1.0\r\nCSeq: 1\r\nUser-Agent: Ellalon\r\n\r\n".encode(), 1.5)
    web = _banner(ip, 80, f"GET / HTTP/1.0\r\nHost: {ip}\r\n\r\n".encode(), 1.5)
    server = " ".join(re.findall(r"(?im)^server:\s*(.+)$", rtsp + "\n" + web))
    title = re.search(r"(?is)<title>(.*?)</title>", web)
    return {"ip": ip, "rtsp": True, "rtsp_port": rtsp_port, "banner": f"{server} {title.group(1).strip() if title else ''}".strip(),
            "source": "RTSP"}


def scan(hosts: list[str], rtsp_port: int, timeout: float) -> list[dict]:
    with concurrent.futures.ThreadPoolExecutor(max_workers=200) as ex:
        return [r for r in ex.map(lambda h: check_host(h, rtsp_port, timeout), hosts) if r]


# ---------------------------------------------------------------- merge
def merge(items: list[dict]) -> list[dict]:
    by_ip: dict[str, dict] = {}
    for it in items:
        d = by_ip.setdefault(it["ip"], {"ip": it["ip"], "sources": [], "name": "", "model": "", "mac": "",
                                        "onvif_port": None, "rtsp": False, "note": "", "banner": "", "kind": ""})
        if it["source"] not in d["sources"]:
            d["sources"].append(it["source"])
        for k in ("name", "model", "mac", "note", "banner", "kind"):
            if it.get(k) and not d[k]:
                d[k] = it[k]
        if it.get("onvif_port"):
            d["onvif_port"] = it["onvif_port"]
        if it.get("rtsp"):
            d["rtsp"] = True
        if it.get("vendor_hint"):
            d["vendor_hint"] = it["vendor_hint"]
    out = []
    for d in by_ip.values():
        brand, display = vendor_of(" ".join([d.get("vendor_hint", ""), d["name"], d["model"], d["banner"]]))
        d["vendor"] = display
        d["brand"] = brand or ("onvif" if d["onvif_port"] else "rtsp")
        if brand == "provision" and d["kind"] == "NVR":
            d["brand"] = "provision_nvr"
        d.pop("vendor_hint", None)
        out.append(d)
    return sorted(out, key=lambda d: tuple(int(x) for x in d["ip"].split(".")))


def main():
    args = json.loads(sys.argv[1]) if len(sys.argv) > 1 else {}
    t0, timeout = time.time(), float(args.get("timeout", 4))
    nets = local_networks()
    hosts = parse_ranges(args.get("ranges") or []) or [str(h) for _, _, n in nets for h in n.hosts()]
    hosts = hosts[:4096]
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as ex:
        mc = ex.submit(multicast, [ip for _, ip, _ in nets], timeout)
        sc = ex.submit(scan, hosts, int(args.get("rtsp_port", 554)), float(args.get("connect_timeout", 0.6)))
        found = mc.result() + sc.result()
    print(json.dumps({"devices": merge(found), "networks": [str(n) for _, _, n in nets],
                      "scanned": len(hosts), "seconds": round(time.time() - t0, 1)}))


if __name__ == "__main__":
    main()
