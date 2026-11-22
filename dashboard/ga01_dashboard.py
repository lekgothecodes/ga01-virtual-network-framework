#!/usr/bin/env python3
"""
GA01 live topology dashboard  (NSS370S GA1 2026, S = 103)

Advanced feature C: graphical topology with OBSERVED state.
  * Router and link state  -> polled live with SNMPv2c (IF-MIB ifDescr / ifOperStatus / ifInOctets / ifOutOctets)
  * R1 <-> ODL BGP state   -> read live from OpenDaylight RESTCONF (loc-rib must contain R1's prefix)
Node positions are drawn by hand; every colour and number on the page comes from a live poll.

Pure Python 3 standard library (no pip installs). Runs on Ubuntu or Windows.
  python3 ga01_dashboard.py --once     # one poll, print results, exit (use to test)
  python3 ga01_dashboard.py            # start dashboard, open http://localhost:8090
"""
import base64, json, random, re, socket, sys, threading, time, urllib.request, urllib.error
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ----------------------------------------------------------------- settings
COMMUNITY = "ga01ro"
ROUTERS = {                        # address the dashboard polls for each router
    "R1": "192.168.103.11",        # R1 management interface (eth4)
    "R2": "10.103.0.202",          # loopbacks (reached through R1 via IS-IS)
    "R3": "10.103.0.203",
    "R4": "10.103.0.204",
}
LINKS = [  # id, router A, iface A, router B, iface B, subnet
    ("R1-R2", "R1", "eth5", "R2", "eth5", "10.103.0.0/30"),
    ("R2-R3", "R2", "eth7", "R3", "eth7", "10.103.0.4/30"),
    ("R3-R4", "R3", "eth5", "R4", "eth7", "10.103.0.8/30"),
    ("R1-R4", "R1", "eth6", "R4", "eth5", "10.103.0.12/30"),
    ("R1-R3", "R1", "eth7", "R3", "eth6", "10.103.0.16/30"),
]
ODL_URL = "http://192.168.103.10:8181/rests/data"
ODL_AUTH = ("admin", "admin")
RIB = "bgp-to-r1"
BGP_PEER = "192.168.103.11"
BGP_PREFIX = "10.103.0.201/32"
POLL_SECONDS = 3
PORT = 8090

# ----------------------------------------------------------- minimal SNMPv2c
def _len(n):
    if n < 0x80:
        return bytes([n])
    b = n.to_bytes((n.bit_length() + 7) // 8, "big")
    return bytes([0x80 | len(b)]) + b

def _tlv(tag, val):
    return bytes([tag]) + _len(len(val)) + val

def _int(i):
    return _tlv(0x02, i.to_bytes(max(1, (i.bit_length() + 8) // 8), "big", signed=True))

def _oid(oid):
    p = [int(x) for x in oid.strip(".").split(".")]
    body = bytearray([40 * p[0] + p[1]])
    for n in p[2:]:
        chunk = [n & 0x7F]
        n >>= 7
        while n:
            chunk.append(0x80 | (n & 0x7F))
            n >>= 7
        body += bytes(reversed(chunk))
    return _tlv(0x06, bytes(body))

def _read(data, i):
    tag = data[i]
    ln = data[i + 1]
    i += 2
    if ln & 0x80:
        k = ln & 0x7F
        ln = int.from_bytes(data[i:i + k], "big")
        i += k
    return tag, data[i:i + ln], i + ln

def _items(v):
    out, i = [], 0
    while i < len(v):
        t, val, i = _read(v, i)
        out.append((t, val))
    return out

def _dec_oid(v):
    p = [v[0] // 40, v[0] % 40]
    n = 0
    for b in v[1:]:
        n = (n << 7) | (b & 0x7F)
        if not b & 0x80:
            p.append(n)
            n = 0
    return ".".join(map(str, p))

def _value(t, v):
    if t == 0x02:
        return int.from_bytes(v, "big", signed=True)
    if t in (0x41, 0x42, 0x43, 0x46):          # Counter32, Gauge32, TimeTicks, Counter64
        return int.from_bytes(v, "big")
    if t == 0x04:
        return v.decode("utf-8", "replace")
    if t == 0x06:
        return _dec_oid(v)
    return None                                # NULL, noSuchObject, endOfMibView ...

def snmp(host, oids, pdu=0xA0, timeout=1.5):
    """Send one GET (0xA0) or GETNEXT (0xA1); return list of (oid, value)."""
    reqid = random.randint(1, 2**30)
    vbl = b"".join(_tlv(0x30, _oid(o) + b"\x05\x00") for o in oids)
    msg = _tlv(0x30, _int(1) + _tlv(0x04, COMMUNITY.encode()) +
               _tlv(pdu, _int(reqid) + _int(0) + _int(0) + _tlv(0x30, vbl)))
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    s.settimeout(timeout)
    try:
        s.sendto(msg, (host, 161))
        end = time.time() + timeout
        while True:
            s.settimeout(max(0.05, end - time.time()))
            data, _ = s.recvfrom(65535)
            _, top, _ = _read(data, 0)
            parts = _items(top)
            pdu_items = _items(parts[2][1])
            if _value(*pdu_items[0]) != reqid:
                continue
            if _value(*pdu_items[1]) != 0:
                raise RuntimeError("SNMP error-status %d" % _value(*pdu_items[1]))
            res = []
            for _, vb in _items(pdu_items[3][1]):
                (ot, ov), (vt, vv) = _items(vb)
                res.append((_dec_oid(ov), _value(vt, vv)))
            return res
    finally:
        s.close()

def walk(host, base, limit=80):
    out, oid = [], base
    for _ in range(limit):
        o, v = snmp(host, [oid], pdu=0xA1)[0]
        if not o.startswith(base + ".") or v is None:
            break
        out.append((o, v))
        oid = o
    return out

IF_DESCR = "1.3.6.1.2.1.2.2.1.2"
IF_OPER = "1.3.6.1.2.1.2.2.1.8"
IF_IN = "1.3.6.1.2.1.2.2.1.10"
IF_OUT = "1.3.6.1.2.1.2.2.1.16"
SYS_UPTIME = "1.3.6.1.2.1.1.3.0"
OPER = {1: "up", 2: "down", 3: "testing", 5: "dormant", 6: "notPresent", 7: "lowerLayerDown"}

# ------------------------------------------------------------------ poller
state = {"routers": {}, "links": [], "bgp": {}, "events": [], "time": None, "poll_ms": 0}
lock = threading.Lock()
_prev = {}          # (router, iface) -> (t, in, out)
_ifindex = {}       # router -> {name: index}
_last = {}          # event detection
_rx_seen = {}       # (router, iface) -> (last in-counter, time it last changed)
SILENT_SECONDS = 8  # no frames received for this long => link down (IS-IS hellos arrive every ~3 s)

def ticks(t):
    s = t // 100
    return "%dd %02d:%02d:%02d" % (s // 86400, s % 86400 // 3600, s % 3600 // 60, s % 60)

def poll_router(name, ip):
    r = {"ip": ip, "reachable": False, "uptime": "", "error": "", "ifs": {}}
    try:
        if name not in _ifindex:
            _ifindex[name] = {v: int(o.rsplit(".", 1)[1]) for o, v in walk(ip, IF_DESCR)}
        idx = _ifindex[name]
        wanted = [n for n in sorted(idx) if n.startswith("eth")]
        oids = [SYS_UPTIME]
        for n in wanted:
            i = idx[n]
            oids += ["%s.%d" % (IF_OPER, i), "%s.%d" % (IF_IN, i), "%s.%d" % (IF_OUT, i)]
        vals = dict(snmp(ip, oids))
        now = time.time()
        r["reachable"] = True
        r["uptime"] = ticks(vals.get(SYS_UPTIME) or 0)
        for n in wanted:
            i = idx[n]
            oper = OPER.get(vals.get("%s.%d" % (IF_OPER, i)), "unknown")
            cin = vals.get("%s.%d" % (IF_IN, i)) or 0
            cout = vals.get("%s.%d" % (IF_OUT, i)) or 0
            bin_ = bout = None
            p = _prev.get((name, n))
            if p and now > p[0]:
                dt = now - p[0]
                bin_ = ((cin - p[1]) % 2**32) * 8 / dt
                bout = ((cout - p[2]) % 2**32) * 8 / dt
            _prev[(name, n)] = (now, cin, cout)
            seen = _rx_seen.get((name, n))
            if seen is None or seen[0] != cin:
                _rx_seen[(name, n)] = (cin, now)
                rx_age = 0
            else:
                rx_age = now - seen[1]
            r["ifs"][n] = {"oper": oper, "in": cin, "out": cout, "in_bps": bin_, "out_bps": bout,
                           "rx_age": round(rx_age)}
    except Exception as e:  # timeout, refused, bad reply
        _ifindex.pop(name, None)
        r["error"] = "no SNMP reply" if isinstance(e, socket.timeout) else str(e)[:80]
    return r

def odl_get(path):
    req = urllib.request.Request(ODL_URL + path, headers={
        "Accept": "application/json",
        "Authorization": "Basic " + base64.b64encode(("%s:%s" % ODL_AUTH).encode()).decode()})
    try:
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, ""

def poll_bgp():
    b = {"state": "unknown", "detail": "", "prefixes": []}
    try:
        code, body = odl_get("/bgp-rib:bgp-rib/rib=%s/loc-rib?content=nonconfig" % RIB)
        prefixes = sorted(set(re.findall(r'"prefix"\s*:\s*"([^"]+)"', body))) if code == 200 else []
        b["prefixes"] = prefixes
        if BGP_PREFIX in prefixes:
            b["state"] = "up"
            b["detail"] = "Established: %s received in ODL loc-rib" % BGP_PREFIX
        else:
            b["state"] = "down"
            b["detail"] = "ODL reachable, but %s not in loc-rib (HTTP %s)" % (BGP_PREFIX, code)
    except Exception as e:
        b["state"] = "down"
        b["detail"] = "ODL RESTCONF unreachable (%s)" % str(e)[:60]
    return b

def link_state(routers, a, ai, b, bi):
    ia = routers.get(a, {}).get("ifs", {}).get(ai, {})
    ib = routers.get(b, {}).get("ifs", {}).get(bi, {})
    ea, eb = ia.get("oper"), ib.get("oper")
    if ea == "up" and eb == "up":
        silent = max(ia.get("rx_age", 0), ib.get("rx_age", 0))
        if silent > SILENT_SECONDS:
            return "silent", ea, eb
        return "up", ea, eb
    if "down" in (ea, eb) or (ea and ea != "up") or (eb and eb != "up"):
        if ea is None and eb is None:
            return "unknown", ea, eb
        return "down", ea, eb
    return "unknown", ea, eb

def event(msg):
    state["events"].insert(0, {"t": time.strftime("%H:%M:%S"), "msg": msg})
    del state["events"][20:]

def poll_once():
    t0 = time.time()
    results = {}
    threads = []
    for n, ip in ROUTERS.items():
        th = threading.Thread(target=lambda n=n, ip=ip: results.__setitem__(n, poll_router(n, ip)))
        th.start()
        threads.append(th)
    bgp = poll_bgp()
    for th in threads:
        th.join()
    links = []
    for lid, a, ai, b, bi, sub in LINKS:
        st, ea, eb = link_state(results, a, ai, b, bi)
        reason = ""
        if st == "silent":
            st = "down"
            reason = "no frames received (link suspended?)"
        elif st == "down":
            reason = "ifOperStatus %s/%s" % (ea, eb)
        ia = results.get(a, {}).get("ifs", {}).get(ai, {})
        bps = None
        if ia.get("in_bps") is not None:
            bps = ia["in_bps"] + ia["out_bps"]
        links.append({"id": lid, "a": a, "ai": ai, "b": b, "bi": bi, "subnet": sub,
                      "state": st, "a_oper": ea, "b_oper": eb, "bps": bps, "reason": reason})
    with lock:
        for n, r in results.items():
            key = ("node", n)
            now = "reachable" if r["reachable"] else "unreachable"
            if _last.get(key) not in (None, now):
                event("%s SNMP %s" % (n, now))
            _last[key] = now
        for l in links:
            key = ("link", l["id"])
            if _last.get(key) not in (None, l["state"]):
                event("Link %s (%s %s / %s %s) is %s %s" % (l["id"], l["a"], l["ai"], l["b"], l["bi"], l["state"].upper(), ("- " + l["reason"]) if l["reason"] else ""))
            _last[key] = l["state"]
        if _last.get("bgp") not in (None, bgp["state"]):
            event("BGP R1 to ODL is %s" % bgp["state"].upper())
        _last["bgp"] = bgp["state"]
        state.update(routers=results, links=links, bgp=bgp,
                     time=time.strftime("%H:%M:%S"), poll_ms=int((time.time() - t0) * 1000))

def poller():
    while True:
        try:
            poll_once()
        except Exception as e:
            print("poll error:", e)
        time.sleep(POLL_SECONDS)

# -------------------------------------------------------------------- web
PAGE = r"""<!doctype html><html><head><meta charset="utf-8"><title>GA01 Live Topology</title>
<meta name="viewport" content="width=device-width,initial-scale=1">
<style>
:root{--bg:#f3f5f8;--card:#fff;--ink:#18202e;--muted:#5d6878;--line:#d6dbe3;--up:#1f9d55;--down:#d64545;--unk:#9aa3af;--accent:#b5620f}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--ink);font:15px/1.5 system-ui,"Segoe UI",Ubuntu,sans-serif}
header{padding:14px 20px;background:#18202e;color:#fff;display:flex;flex-wrap:wrap;gap:8px 24px;align-items:baseline}
header h1{margin:0;font-size:20px}header span{color:#c9d1dc;font-size:13px}#poll{color:#ffd29a}
main{display:grid;grid-template-columns:minmax(0,1.4fr) minmax(0,1fr);gap:16px;padding:16px 20px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px 14px;min-width:0}
h2{font-size:13px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);margin:0 0 8px}
svg{width:100%;height:auto;display:block}
.link{stroke-width:6;stroke-linecap:round}.link.up{stroke:var(--up)}.link.down{stroke:var(--down);stroke-dasharray:10 8}.link.unknown{stroke:var(--unk);stroke-dasharray:3 7}
.node{stroke:#18202e;stroke-width:2}.node.up{fill:#e5f6ec}.node.down{fill:#fde8e8}.node.unknown{fill:#eef0f3}
.nname{font:700 18px system-ui,sans-serif;fill:#18202e}.small{font:12px ui-monospace,Consolas,monospace;fill:#455063}
.lbl{font:600 12px ui-monospace,Consolas,monospace;paint-order:stroke;stroke:#fff;stroke-width:4px;fill:#18202e}
.odl{fill:#fff4e6;stroke:var(--accent);stroke-width:2}
table{border-collapse:collapse;width:100%;font-size:13px}td,th{padding:4px 6px;border-bottom:1px solid var(--line);text-align:left;white-space:nowrap}
th{color:var(--muted);font-weight:600}.num{font-family:ui-monospace,Consolas,monospace;text-align:right}
.pill{display:inline-block;padding:1px 8px;border-radius:10px;font-size:12px;font-weight:600;color:#fff}.pill.up{background:var(--up)}.pill.down{background:var(--down)}.pill.unknown{background:var(--unk)}
.tablewrap{overflow-x:auto}ul{margin:0;padding-left:18px;font-size:13px}li{margin-bottom:3px}
.legend{display:flex;flex-wrap:wrap;gap:6px 16px;font-size:12px;color:var(--muted);margin-top:6px}
.legend i{display:inline-block;width:22px;height:5px;margin-right:5px;vertical-align:middle;border-radius:3px}
@media(max-width:900px){main{grid-template-columns:minmax(0,1fr)}}
</style></head><body>
<header><h1>GA01 Live Topology &middot; S = 103</h1><span>Observed via SNMPv2c (IF-MIB) + OpenDaylight RESTCONF</span><span id="poll">waiting for first poll...</span></header>
<main>
<div class="card"><h2>Topology (positions drawn, colours observed)</h2>
<svg viewBox="0 0 700 430">
 <line id="L-MGMT" class="link unknown" x1="190" y1="75" x2="352" y2="75"/>
 <line id="L-R1-R2" class="link unknown" x1="380" y1="75" x2="200" y2="220"/>
 <line id="L-R1-R4" class="link unknown" x1="380" y1="75" x2="560" y2="220"/>
 <line id="L-R1-R3" class="link unknown" x1="380" y1="75" x2="380" y2="365"/>
 <line id="L-R2-R3" class="link unknown" x1="200" y1="220" x2="380" y2="365"/>
 <line id="L-R3-R4" class="link unknown" x1="560" y1="220" x2="380" y2="365"/>
 <rect class="odl" x="10" y="45" width="180" height="62" rx="8"/>
 <text class="nname" x="22" y="70" style="font-size:15px">OpenDaylight</text>
 <text class="small" x="22" y="90">192.168.103.10</text>
 <text id="T-MGMT" class="lbl" x="271" y="64" text-anchor="middle">BGP ?</text>
 <text id="T-R1-R2" class="lbl" x="262" y="140" text-anchor="end">10.103.0.0/30</text>
 <text id="T-R1-R4" class="lbl" x="498" y="140">10.103.0.12/30</text>
 <text id="T-R1-R3" class="lbl" x="370" y="250" text-anchor="end">10.103.0.16/30</text>
 <text id="T-R2-R3" class="lbl" x="262" y="318" text-anchor="end">10.103.0.4/30</text>
 <text id="T-R3-R4" class="lbl" x="498" y="318">10.103.0.8/30</text>
 <g id="N-R1"><circle class="node unknown" cx="380" cy="75" r="30"/><text class="nname" x="380" y="81" text-anchor="middle">R1</text><text class="small" x="380" y="30" text-anchor="middle">-</text></g>
 <g id="N-R2"><circle class="node unknown" cx="200" cy="220" r="30"/><text class="nname" x="200" y="226" text-anchor="middle">R2</text><text class="small" x="200" y="272" text-anchor="middle">-</text></g>
 <g id="N-R4"><circle class="node unknown" cx="560" cy="220" r="30"/><text class="nname" x="560" y="226" text-anchor="middle">R4</text><text class="small" x="560" y="272" text-anchor="middle">-</text></g>
 <g id="N-R3"><circle class="node unknown" cx="380" cy="365" r="30"/><text class="nname" x="380" y="371" text-anchor="middle">R3</text><text class="small" x="380" y="417" text-anchor="middle">-</text></g>
</svg>
<div class="legend"><span><i style="background:var(--up)"></i>up (both ends ifOperStatus=up)</span><span><i style="background:var(--down)"></i>down (ifOperStatus down, or no frames for 8 s)</span><span><i style="background:var(--unk)"></i>no data yet</span></div>
</div>
<div style="display:grid;gap:16px;min-width:0">
 <div class="card"><h2>BGP R1 &harr; OpenDaylight</h2><div id="bgp">-</div></div>
 <div class="card"><h2>Events (live changes)</h2><ul id="events"><li>none yet</li></ul></div>
</div>
<div class="card" style="grid-column:1/-1"><h2>Interfaces (live SNMP counters)</h2><div class="tablewrap"><table id="ifs"></table></div></div>
</main>
<script>
function rate(b){if(b==null)return '-';if(b>=1e6)return (b/1e6).toFixed(2)+' Mb/s';if(b>=1e3)return (b/1e3).toFixed(1)+' kb/s';return b.toFixed(0)+' b/s'}
function esc(s){return String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]))}
async function tick(){
 let s;try{s=await (await fetch('/api/state',{cache:'no-store'})).json()}catch(e){document.getElementById('poll').textContent='dashboard server not responding';return}
 if(!s.time)return;
 document.getElementById('poll').textContent='last poll '+s.time+' ('+s.poll_ms+' ms), every 3 s';
 for(const l of s.links){document.getElementById('L-'+l.id).setAttribute('class','link '+l.state);
  document.getElementById('T-'+l.id).textContent=l.subnet+'  '+(l.state=='up'?rate(l.bps):l.state.toUpperCase()+(l.reason&&l.reason.startsWith('no frames')?' (no frames)':''));}
 for(const [n,r] of Object.entries(s.routers)){const g=document.getElementById('N-'+n);if(!g)continue;
  g.querySelector('circle').setAttribute('class','node '+(r.reachable?'up':'down'));
  g.querySelector('.small').textContent=r.reachable?('up '+r.uptime):('SNMP: '+(r.error||'no reply'));}
 const b=s.bgp||{};document.getElementById('L-MGMT').setAttribute('class','link '+(b.state||'unknown'));
 document.getElementById('T-MGMT').textContent='BGP '+(b.state||'?').toUpperCase();
 document.getElementById('bgp').innerHTML='<span class="pill '+(b.state||'unknown')+'">'+esc((b.state||'unknown').toUpperCase())+'</span> '+esc(b.detail||'')+
  '<div style="font-size:13px;color:#5d6878;margin-top:6px">ODL loc-rib prefixes: '+esc((b.prefixes||[]).join(', ')||'none')+'</div>';
 document.getElementById('events').innerHTML=(s.events.length?s.events.map(e=>'<li><b>'+e.t+'</b> '+esc(e.msg)+'</li>').join(''):'<li>none yet</li>');
 let h='<tr><th>Router</th><th>Interface</th><th>Oper status</th><th class="num">ifInOctets</th><th class="num">ifOutOctets</th><th class="num">In rate</th><th class="num">Out rate</th><th class="num">Last frame</th></tr>';
 for(const [n,r] of Object.entries(s.routers)){if(!r.reachable){h+='<tr><td>'+n+'</td><td colspan="6">'+esc(r.ip)+' &ndash; '+esc(r.error)+'</td></tr>';continue}
  for(const [i,v] of Object.entries(r.ifs)){const st=v.oper=='up'?'up':(v.oper=='down'?'down':'unknown');
   h+='<tr><td>'+n+'</td><td>'+i+'</td><td><span class="pill '+st+'">'+v.oper+'</span></td><td class="num">'+v.in+'</td><td class="num">'+v.out+'</td><td class="num">'+rate(v.in_bps)+'</td><td class="num">'+rate(v.out_bps)+'</td><td class="num">'+v.rx_age+' s ago</td></tr>'}}
 document.getElementById('ifs').innerHTML=h;
}
tick();setInterval(tick,3000);
</script></body></html>"""

class Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path.startswith("/api/state"):
            with lock:
                body = json.dumps(state).encode()
            ctype = "application/json"
        else:
            body = PAGE.encode()
            ctype = "text/html; charset=utf-8"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, *a):
        pass

if __name__ == "__main__":
    if "--once" in sys.argv:
        poll_once()
        for n, r in state["routers"].items():
            print("%-3s %-15s %s" % (n, r["ip"], ("OK  uptime " + r["uptime"]) if r["reachable"] else "FAIL " + r["error"]))
            for i, v in r["ifs"].items():
                print("      %-5s %-5s in=%s out=%s" % (i, v["oper"], v["in"], v["out"]))
        for l in state["links"]:
            print("link %-6s %-8s %s" % (l["id"], l["state"], l["subnet"]))
        print("BGP:", state["bgp"].get("state"), "-", state["bgp"].get("detail"))
        sys.exit(0)
    threading.Thread(target=poller, daemon=True).start()
    print("GA01 dashboard running: open http://localhost:%d  (Ctrl+C to stop)" % PORT)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
