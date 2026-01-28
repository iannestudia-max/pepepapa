# server.py
import asyncio
import base64
import io
import json
import time
import uuid
from aiohttp import web, WSMsgType
from PIL import Image
import mss
from pynput.mouse import Controller as MouseController
from pynput.keyboard import Controller as KeyboardController

# ---------- Simple Session Manager ----------
class SessionManager:
    def __init__(self):
        self.session_code = self._gen_code()
        self.pending = {}        # device_id -> {"name":..., "ws":ws, "time":...}
        self.active = {}         # device_id -> {"name":..., "ws":ws, "perms": {...}}
        self.banned = set()
        self._decisions = {}     # device_id -> asyncio.Event()

        # default safe permissions
        self.default_perms = {
            "view_screen": True,
            "control_mouse": False,
            "control_keyboard": False,
            "open_programs": False,
            "delete_files": False
        }

    def _gen_code(self, length=6):
        return "".join(str((uuid.uuid4().int >> i) % 10) for i in range(length))

    def regen_code(self):
        self.session_code = self._gen_code()
        return self.session_code

    def is_banned(self, device_id):
        return device_id in self.banned

    def add_pending(self, device_id, device_name, ws):
        if self.is_banned(device_id): 
            return False
        self.pending[device_id] = {"name": device_name, "ws": ws, "time": time.time()}
        self._decisions.setdefault(device_id, asyncio.Event())
        return True

    def decide_accept(self, device_id):
        data = self.pending.pop(device_id, None)
        if not data: return False
        perms = dict(self.default_perms)
        # by default keep safe (view_screen True)
        self.active[device_id] = {"name": data["name"], "ws": data["ws"], "perms": perms}
        ev = self._decisions.get(device_id)
        if ev and not ev.is_set():
            ev.set()
        return True

    def decide_reject(self, device_id):
        data = self.pending.pop(device_id, None)
        if not data: return False
        ws = data["ws"]
        try:
            asyncio.create_task(ws.send_json({"type":"auth_result","status":"rejected"}))
        except: pass
        ev = self._decisions.get(device_id)
        if ev and not ev.is_set():
            ev.set()
        return True

    def decide_ban(self, device_id):
        data = self.pending.pop(device_id, None)
        self.banned.add(device_id)
        if data:
            ws = data["ws"]
            try:
                asyncio.create_task(ws.send_json({"type":"auth_result","status":"banned"}))
            except: pass
        ev = self._decisions.get(device_id)
        if ev and not ev.is_set():
            ev.set()
        return True

    async def wait_decision(self, device_id, timeout=None):
        ev = self._decisions.setdefault(device_id, asyncio.Event())
        try:
            await asyncio.wait_for(ev.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        # after set, check if active
        return self.active.get(device_id)

    def get_pending_list(self):
        return [{"device_id": k, "name": v["name"], "time": v["time"]} for k,v in self.pending.items()]

    def get_active_list(self):
        return [{"device_id": k, "name": v["name"], "perms": v["perms"]} for k,v in self.active.items()]

    def set_perms(self, device_id, perms):
        if device_id in self.active:
            self.active[device_id]["perms"].update(perms)
            # notify client about permission changes
            ws = self.active[device_id]["ws"]
            try:
                asyncio.create_task(ws.send_json({"type":"permission_update","permissions": self.active[device_id]["perms"]}))
            except: pass
            return True
        return False

    def detach_active(self, device_id):
        self.active.pop(device_id, None)

SM = SessionManager()

# ---------- Screen capturer ----------
mss_inst = mss.mss()
def capture_jpeg_base64(quality=60):
    img = mss_inst.grab(mss_inst.monitors[1])
    im = Image.frombytes("RGB", img.size, img.rgb)
    buf = io.BytesIO()
    im.save(buf, format="JPEG", quality=quality)
    return base64.b64encode(buf.getvalue()).decode('ascii')

# ---------- Input executors ----------
mouse = MouseController()
keyboard = KeyboardController()

def handle_input_event(ev, perms):
    t = ev.get("type")
    if t == "mouse_click":
        if not perms.get("control_mouse"): return False
        x = ev.get("x"); y = ev.get("y")
        # x,y arrive as relative (0..1) from client; transform to absolute
        try:
            sx = mss_inst.monitors[1]["width"]
            sy = mss_inst.monitors[1]["height"]
            ax = int(x * sx)
            ay = int(y * sy)
            mouse.position = (ax, ay)
            mouse.click()
            return True
        except Exception:
            return False
    if t == "type_text":
        if not perms.get("control_keyboard"): return False
        text = ev.get("text","")
        try:
            keyboard.type(text)
            return True
        except:
            return False
    return False

# ---------- Web app ----------
routes = web.RouteTableDef()

# serve static folder
app = web.Application()
app.router.add_static('/static', path='./static', show_index=True)

# index: web client
@routes.get('/')
async def index(req):
    return web.FileResponse('./static/index.html')

# admin panel
@routes.get('/admin')
async def admin(req):
    return web.FileResponse('./static/admin.html')

# API: session code
@routes.get('/api/session_code')
async def api_code(req):
    return web.json_response({"code": SM.session_code})

@routes.post('/api/regenerate')
async def api_regen(req):
    SM.regen_code()
    return web.json_response({"code": SM.session_code})

@routes.get('/api/pending')
async def api_pending(req):
    return web.json_response({"pending": SM.get_pending_list()})

@routes.get('/api/active')
async def api_active(req):
    return web.json_response({"active": SM.get_active_list()})

@routes.post('/api/approve')
async def api_approve(req):
    data = await req.json()
    dev = data.get("device_id")
    ok = SM.decide_accept(dev)
    return web.json_response({"ok": ok})

@routes.post('/api/reject')
async def api_reject(req):
    data = await req.json()
    dev = data.get("device_id")
    ok = SM.decide_reject(dev)
    return web.json_response({"ok": ok})

@routes.post('/api/ban')
async def api_ban(req):
    data = await req.json()
    dev = data.get("device_id")
    ok = SM.decide_ban(dev)
    return web.json_response({"ok": ok})

# WebSocket endpoint used by browser client
@routes.get('/ws')
async def websocket_handler(request):
    ws = web.WebSocketResponse(max_msg_size=0)
    await ws.prepare(request)

    # expect first msg as JSON auth: {"type":"auth","device_name":...,"device_id":...,"code":...}
    async for msg in ws:
        if msg.type == WSMsgType.TEXT:
            try:
                data = json.loads(msg.data)
            except:
                await ws.send_json({"type":"error","msg":"invalid json"})
                continue

            mtype = data.get("type")
            if mtype == "auth":
                device_id = data.get("device_id") or str(uuid.uuid4())
                device_name = data.get("device_name") or "unknown"
                code = data.get("code")
                if SM.is_banned(device_id):
                    await ws.send_json({"type":"auth_result","status":"banned"})
                    await ws.close()
                    break
                if code != SM.session_code:
                    await ws.send_json({"type":"auth_result","status":"wrong_code"})
                    # optionally track tries here
                    continue
                # register pending and notify admin (admin panel polls /api/pending)
                SM.add_pending(device_id, device_name, ws)
                await ws.send_json({"type":"auth_result","status":"pending"})
                # wait for decision
                active_info = await SM.wait_decision(device_id, timeout=3600)
                if not active_info:
                    # timed out or rejected
                    try:
                        await ws.send_json({"type":"auth_result","status":"rejected"})
                    except: pass
                    await ws.close()
                    break
                # now active_info contains active dict (with ws and perms)
                # Send accepted message + permissions
                perms = active_info["perms"]
                await ws.send_json({"type":"auth_result","status":"accepted", "permissions": perms})
                # start frame sending loop and continue listening for input
                send_task = asyncio.create_task(send_frames_loop(ws, device_id))
                # continue to process incoming messages (events) below
            elif mtype == "input_event":
                # find which active device this ws belongs to
                # naive scan
                dev_id = None
                for did, info in SM.active.items():
                    if info["ws"] is ws:
                        dev_id = did; break
                if not dev_id:
                    await ws.send_json({"type":"error","msg":"not active"})
                    continue
                perms = SM.active[dev_id]["perms"]
                ok = handle_input_event(data.get("payload",{}), perms)
                await ws.send_json({"type":"input_ack","ok":ok})
            else:
                await ws.send_json({"type":"error","msg":"unknown type"})
        elif msg.type == WSMsgType.ERROR:
            print('ws connection closed with exception %s' % ws.exception())
    # cleanup when websocket closes: remove from pending or active
    # remove any active mapping that used this ws
    for did, info in list(SM.active.items()):
        if info["ws"] is ws:
            SM.detach_active(did)
    for did, info in list(SM.pending.items()):
        if info["ws"] is ws:
            SM.pending.pop(did, None)
    return ws

# frame sender
async def send_frames_loop(ws, device_id):
    try:
        while True:
            # check permission
            info = SM.active.get(device_id)
            if not info: break
            if not info["perms"].get("view_screen", False):
                await asyncio.sleep(0.5)
                continue
            b64 = capture_jpeg_base64(quality=50)
            # send JSON message with base64 (could be large)
            try:
                await ws.send_json({"type":"frame","b64": b64})
            except Exception:
                break
            await asyncio.sleep(0.12)  # ~8 fps
    except asyncio.CancelledError:
        pass

# attach routes and run
app.add_routes(routes)

if __name__ == '__main__':
    web.run_app(app, host='0.0.0.0', port=8080)
