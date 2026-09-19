"""Home Assistant ingress editor for dated free-import windows."""
import json
import math
import logging
import threading
from urllib.request import Request, urlopen
import os
import tempfile
from datetime import datetime, timezone, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import RLock
from zoneinfo import ZoneInfo

STORE = Path(os.environ.get("MANUAL_TARIFF_WINDOWS_PATH", "/data/manual_tariff_windows.json"))
LOCK = RLock()
LOG = logging.getLogger("tariff_editor")
MAX_WINDOWS = 64


def read_windows():
    try:
        data = json.loads(STORE.read_text())
        return data if isinstance(data, list) else []
    except FileNotFoundError:
        return []


def validate(windows, tz_name):
    if not isinstance(windows, list) or len(windows) > MAX_WINDOWS:
        raise ValueError("Too many periods (maximum 64)")
    ZoneInfo(tz_name)
    parsed = []
    now = datetime.now(timezone.utc)
    for item in windows:
        if not isinstance(item, dict):
            raise ValueError("Invalid period")
        start = datetime.fromisoformat(item["start"])
        end = datetime.fromisoformat(item["end"])
        if start.tzinfo is None or end.tzinfo is None:
            raise ValueError("Periods require a timezone offset")
        a, b = start.astimezone(timezone.utc), end.astimezone(timezone.utc)
        if a >= b or b - a > timedelta(days=7):
            raise ValueError("Period must have a positive duration of at most seven days")
        if a > now + timedelta(days=366):
            raise ValueError("Period is more than one year ahead")
        price = item.get('rate_p', 0)
        if isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0:
            raise ValueError('Price must be a finite non-negative number')
        parsed.append((a, b, float(price)))
    parsed.sort()
    merged = []
    for a, b, price in parsed:
        if merged and a < merged[-1][1] and price != merged[-1][2]:
            raise ValueError('Overlapping periods with different prices')
        if merged and a <= merged[-1][1] and price == merged[-1][2]:
            merged[-1] = (merged[-1][0], max(b, merged[-1][1]), price)
        else:
            merged.append((a, b, price))
    return [{"start": a.isoformat(), "end": b.isoformat(), "rate_p": price} for a, b, price in merged]


def save_windows(windows, tz_name):
    valid = validate(windows, tz_name)
    with LOCK:
        STORE.parent.mkdir(parents=True, exist_ok=True)
        fd, name = tempfile.mkstemp(dir=STORE.parent, prefix=".tariff-", text=True)
        try:
            with os.fdopen(fd, "w") as f:
                json.dump(valid, f)
                f.flush()
                os.fsync(f.fileno())
            os.replace(name, STORE)
        finally:
            if os.path.exists(name):
                os.unlink(name)
    return valid



def set_slot_price(start_text, end_text, price, tz_name):
    """Replace only the selected interval; retain prices on either side."""
    start = datetime.fromisoformat(start_text).astimezone(timezone.utc)
    end = datetime.fromisoformat(end_text).astimezone(timezone.utc)
    if end <= start or end - start != timedelta(minutes=30):
        raise ValueError("Select exactly one half-hour slot")
    if price is not None and (isinstance(price, bool) or not isinstance(price, (int, float)) or not math.isfinite(price) or price < 0):
        raise ValueError("Price must be a finite non-negative number")
    with LOCK:
        retained = []
        for window in read_windows():
            a = datetime.fromisoformat(window["start"]).astimezone(timezone.utc)
            b = datetime.fromisoformat(window["end"]).astimezone(timezone.utc)
            if b <= start or a >= end:
                retained.append(window)
                continue
            if a < start:
                retained.append({**window, "end": start.isoformat()})
            if b > end:
                retained.append({**window, "start": end.isoformat()})
        if price is not None:
            retained.append({"start": start.isoformat(), "end": end.isoformat(), "rate_p": price})
        return save_windows(retained, tz_name)


PAGE = """<!doctype html><html lang="en"><head><meta name="viewport" content="width=device-width, initial-scale=1"><title>Free electricity periods</title>
<style>body{font:16px system-ui;margin:0;background:#fafafa;color:#222}main{max-width:720px;margin:auto;padding:20px}section{background:white;border-radius:12px;padding:18px;margin:16px 0;box-shadow:0 1px 5px #ddd}label{display:block;margin:12px 0}input{display:block;font:inherit;padding:8px;max-width:100%;box-sizing:border-box}button{font:inherit;border:0;border-radius:8px;padding:10px 16px;background:#03a9f4;color:white;cursor:pointer;margin:4px}button.remove{background:#666}.period{border-top:1px solid #ddd;padding:12px 0}small{color:#555}#message{min-height:1.5em}</style></head><body><main><h1>Free electricity periods</h1><p>Add or remove supplier-announced free import periods. Prices change in forecasts only; this does not command the battery.</p>
<section><h2>Add a period</h2><label>Start <input id="start" type="datetime-local" step="1800"></label><label>End <input id="end" type="datetime-local" step="1800"></label><button id="add">Add period</button><p id="message" role="status"></p></section><section><h2>Saved periods</h2><div id="periods">Loading…</div></section></main>
<script>
const base = location.pathname.endsWith('/') ? location.pathname : location.pathname + '/';
let timezone='Europe/London';
function localIso(input){const date=new Date(input);if(!Number.isFinite(date.valueOf()))throw Error('Choose a valid date and time');return date.toISOString()}
async function api(method,body){const r=await fetch(base+'api/windows',{method,headers:{'Content-Type':'application/json'},body:body?JSON.stringify(body):undefined});const v=await r.json();if(!r.ok)throw Error(v.error||'Unable to save');return v}
function show(windows){const root=document.getElementById('periods');root.replaceChildren();if(!windows.length){root.textContent='No free periods added.';return}
for(const [i,w] of windows.entries()){const div=document.createElement('div');div.className='period';const title=document.createElement('div');const start=new Date(w.start),end=new Date(w.end);title.textContent=start.toLocaleString(undefined,{timeZone:timezone})+' – '+end.toLocaleString(undefined,{timeZone:timezone});div.append(title);const status=document.createElement('small');status.textContent=end.getTime()<Date.now()?'Expired':start.getTime()<=Date.now()?'Active':'Upcoming';div.append(status);const button=document.createElement('button');button.className='remove';button.textContent='Remove';button.onclick=async()=>{try{saved=(await api('POST',{windows:windows.filter((_,j)=>i!==j)})).windows;show(saved);document.getElementById('message').textContent='Period removed.'}catch(e){document.getElementById('message').textContent=e.message}};div.append(button);root.append(div)}}
let saved=[];
async function refresh(){try{const v=await api('GET');timezone=v.timezone;saved=v.windows;show(saved)}catch(e){document.getElementById('message').textContent=e.message}}
document.getElementById('add').onclick=async()=>{try{const a=localIso(document.getElementById('start').value),b=localIso(document.getElementById('end').value);if(new Date(a)>=new Date(b))throw Error('End must be after start');const v=await api('POST',{windows:[...saved,{start:a,end:b}]});saved=v.windows;show(saved);document.getElementById('message').textContent='Period saved.'}catch(e){document.getElementById('message').textContent=e.message}};
refresh();
</script></body></html>"""


class Handler(BaseHTTPRequestHandler):
    def reply(self, status, content, content_type):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(content)))
        self.end_headers()
        self.wfile.write(content)

    def do_GET(self):
        if self.path.rstrip("/").endswith("/api/windows"):
            payload = {"windows": read_windows(), "timezone": os.getenv("HA_TIMEZONE", "Europe/London")}
            return self.reply(200, json.dumps(payload).encode(), "application/json")
        return self.reply(200, PAGE.encode(), "text/html; charset=utf-8")

    def do_POST(self):
        slot_request = self.path.rstrip("/").endswith("/api/slot")
        if not slot_request and not self.path.rstrip("/").endswith("/api/windows"):
            return self.reply(404, b"{}", "application/json")
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length > 65536:
                raise ValueError("Request too large")
            data = json.loads(self.rfile.read(length))
            tz = os.getenv("HA_TIMEZONE", "Europe/London")
            result = (set_slot_price(data["start"], data["end"], data.get("rate_p"), tz)
                      if slot_request else save_windows(data["windows"], tz))
            return self.reply(200, json.dumps({"windows": result}).encode(), "application/json")
        except (ValueError, KeyError, TypeError, OSError, OverflowError) as exc:
            return self.reply(400, json.dumps({"error": str(exc)}).encode(), "application/json")



def run_mqtt_bridge():
    """Accept HA mqtt.publish commands, never expose the editor's HTTP port to Lovelace."""
    import paho.mqtt.client as mqtt
    token = os.environ.get("SUPERVISOR_TOKEN", "")
    if not token:
        raise RuntimeError("Supervisor token required for MQTT service discovery")
    req = Request("http://supervisor/services/mqtt", headers={"Authorization": f"Bearer {token}"})
    with urlopen(req, timeout=15) as response:
        result = json.load(response)
    cfg = result.get("data", result)
    host = cfg.get("host")
    if not host:
        raise RuntimeError("MQTT broker unavailable")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id="hem_tariff_editor")
    client.username_pw_set(cfg.get("username", ""), cfg.get("password", ""))
    if cfg.get("ssl", cfg.get("tls", False)):
        client.tls_set()
    def on_connect(client, userdata, flags, reason_code, properties=None):
        if int(reason_code) == 0:
            client.subscribe("home_energy_manager/tariff/set_slot", qos=1)
    def on_message(client, userdata, message):
        try:
            data = json.loads(message.payload)
            if not isinstance(data, dict) or set(data) - {"start", "end", "rate_p"}:
                raise ValueError("Invalid slot command")
            set_slot_price(data["start"], data["end"], data.get("rate_p"), os.getenv("HA_TIMEZONE", "Europe/London"))
        except (ValueError, KeyError, TypeError, OSError) as exc:
            LOG.warning("Rejected tariff slot command: %s", exc)
    client.on_connect = on_connect
    client.on_message = on_message
    client.connect(host, int(cfg.get("port", 1883)), keepalive=45)
    client.loop_forever()



if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    def start_bridge():
        try:
            run_mqtt_bridge()
        except Exception:
            LOG.exception("Tariff MQTT command bridge unavailable; ingress editor remains available")
    threading.Thread(target=start_bridge, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", 8099), Handler).serve_forever()
