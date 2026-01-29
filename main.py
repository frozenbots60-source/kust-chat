import os
import json
import asyncio
import boto3
import time
import re
from typing import List, Dict, Optional, Union
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from redis import asyncio as aioredis
from botocore.config import Config

# ==========================================
# KUSTIFY HYPER-X | V10.0 (STABLE)
# ==========================================

app = FastAPI(title="Kustify Hyper-X", version="10.0")

# Infrastructure Config
REDIS_URL = os.getenv("UPSTASH_REDIS_URL") or os.getenv("REDIS_URL")
AWS_CONFIG = {
    "key": os.getenv("BUCKETEER_AWS_ACCESS_KEY_ID"),
    "secret": os.getenv("BUCKETEER_AWS_SECRET_ACCESS_KEY"),
    "region": os.getenv("BUCKETEER_AWS_REGION"),
    "bucket": os.getenv("BUCKETEER_BUCKET_NAME")
}

redis = aioredis.from_url(REDIS_URL, decode_responses=True)
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_CONFIG["key"],
    aws_secret_access_key=AWS_CONFIG["secret"],
    region_name=AWS_CONFIG["region"],
    config=Config(signature_version='s3v4')
)

GLOBAL_CHANNEL = "kustify:global:v10"
GROUPS_KEY = "kustify:groups:v10" 
HISTORY_KEY = "kustify:history:v10:"
USERNAMES_KEY = "kustify:usernames:v10"

class GroupCreate(BaseModel):
    name: str
    is_private: bool = False
    password: Optional[str] = None

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}
        self.global_lookup: Dict[str, WebSocket] = {}
        self.user_meta: Dict[str, dict] = {}

    async def connect(self, websocket: WebSocket, group_id: str, user_info: dict):
        uid = user_info['id']
        if group_id not in self.active_connections:
            self.active_connections[group_id] = []
        self.active_connections[group_id].append(websocket)
        self.global_lookup[uid] = websocket
        self.user_meta[uid] = {**user_info, "group": group_id}
        await self.broadcast_presence(group_id)

    def disconnect(self, websocket: WebSocket, group_id: str, user_id: str):
        if group_id in self.active_connections:
            for conn in self.active_connections[group_id]:
                if conn.client == websocket.client:
                    self.active_connections[group_id].remove(conn)
                    break
        self.global_lookup.pop(user_id, None)
        self.user_meta.pop(user_id, None)

    async def broadcast_presence(self, group_id: str):
        users = [m for uid, m in self.user_meta.items() if m.get("group") == group_id]
        payload = json.dumps({"type": "presence", "count": len(users), "users": users})
        await self.broadcast_local(group_id, payload)

    async def broadcast_local(self, group_id: str, message: str):
        if group_id in self.active_connections:
            for connection in self.active_connections[group_id]:
                try: await connection.send_text(message)
                except: pass

manager = ConnectionManager()

# --- BACKEND LOGIC ---

async def redis_listener():
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            data = json.loads(message["data"])
            mtype = data.get("type")
            if mtype in ["message", "edit", "vc_signal_group"]:
                await manager.broadcast_local(data.get("group_id"), message["data"])
            elif mtype == "dm":
                target = data.get("target_id")
                if target in manager.global_lookup:
                    await manager.global_lookup[target].send_text(message["data"])

@app.on_event("startup")
async def startup():
    # FIX: Check if key exists and is NOT a Hash (to avoid WRONGTYPE error)
    key_type = await redis.type(GROUPS_KEY)
    if key_type != "hash" and key_type != "none":
        await redis.delete(GROUPS_KEY)
    
    if not await redis.hexists(GROUPS_KEY, "Lobby"):
        await redis.hset(GROUPS_KEY, "Lobby", json.dumps({"private": False}))
    asyncio.create_task(redis_listener())

@app.get("/api/groups")
async def get_groups():
    data = await redis.hgetall(GROUPS_KEY)
    return {"groups": {k: json.loads(v) for k, v in data.items()}}

@app.post("/api/groups")
async def create_group(group: GroupCreate):
    name = re.sub(r'[^a-zA-Z0-9]', '', group.name)
    if not name: return JSONResponse(status_code=400, content={"error": "Invalid name"})
    meta = {"private": group.is_private, "password": group.password}
    await redis.hset(GROUPS_KEY, name, json.dumps(meta))
    return {"status": "success", "name": name}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = file.filename.split('.')[-1]
    file_key = f"kustify_v10/{int(time.time())}_{os.urandom(2).hex()}.{ext}"
    s3_client.upload_fileobj(file.file, AWS_CONFIG["bucket"], file_key, ExtraArgs={'ContentType': file.content_type})
    url = s3_client.generate_presigned_url('get_object', Params={'Bucket': AWS_CONFIG["bucket"], 'Key': file_key}, ExpiresIn=604800)
    return {"url": url}

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    await websocket.accept()
    try:
        init = await websocket.receive_json()
        user_info = {"id": user_id, "name": init.get("name", "Anon"), "pfp": init.get("pfp", "")}
        await manager.connect(websocket, group_id, user_info)
        
        while True:
            data = await websocket.receive_json()
            data["user_id"] = user_id
            data["group_id"] = group_id
            data["timestamp"] = time.time()
            
            if data.get("type") == "edit":
                await redis.publish(GLOBAL_CHANNEL, json.dumps(data))
            else:
                await redis.publish(GLOBAL_CHANNEL, json.dumps(data))
                if data.get("type") == "message":
                    await redis.rpush(f"{HISTORY_KEY}{group_id}", json.dumps(data))
    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id, user_id)
    except Exception:
        pass

# ==========================================
# FRONTEND (WEB3 TELEGRAM STYLE)
# ==========================================

html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>KUSTIFY HYPER-X V10</title>
    <script src="https://unpkg.com/peerjs@1.4.7/dist/peerjs.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.0.2/marked.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600&display=swap" rel="stylesheet">
    <style>
        :root { --p: #7000ff; --s: #00f3ff; --bg: #050507; --panel: #111114; --glass: rgba(255,255,255,0.03); }
        * { box-sizing: border-box; }
        body { background: var(--bg); color: #fff; font-family: 'Outfit', sans-serif; margin: 0; display: flex; height: 100vh; overflow: hidden; }
        
        #sidebar { width: 320px; border-right: 1px solid rgba(255,255,255,0.08); background: var(--panel); z-index: 10; display: flex; flex-direction: column; }
        #main { flex: 1; display: flex; flex-direction: column; position: relative; background: radial-gradient(circle at top right, #101018, #050507); }
        
        .nav-item { padding: 12px 20px; margin: 4px 10px; border-radius: 12px; cursor: pointer; transition: 0.2s; color: #888; display: flex; justify-content: space-between; align-items: center; }
        .nav-item:hover { background: var(--glass); color: #fff; }
        .nav-item.active { background: rgba(112, 0, 255, 0.1); color: var(--s); border: 1px solid rgba(0, 243, 255, 0.2); }

        #feed { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 8px; }
        .msg { padding: 12px 16px; max-width: 75%; border-radius: 18px; position: relative; font-size: 0.95rem; line-height: 1.4; }
        .msg.me { align-self: flex-end; background: var(--p); border-bottom-right-radius: 4px; box-shadow: 0 4px 15px rgba(112,0,255,0.2); }
        .msg.other { align-self: flex-start; background: #1c1c21; border-bottom-left-radius: 4px; }
        .msg .meta { font-size: 0.7rem; opacity: 0.5; margin-top: 4px; display: flex; justify-content: flex-end; gap: 5px; }
        
        #preview-box { position: absolute; bottom: 90px; left: 20px; display: none; background: #16161a; padding: 12px; border-radius: 16px; border: 1px solid var(--s); z-index: 100; box-shadow: 0 10px 30px rgba(0,0,0,0.5); }
        #preview-img { max-height: 200px; border-radius: 10px; display: block; }

        #vc-overlay { position: fixed; top: 20px; right: 20px; width: 280px; background: rgba(15,15,20,0.95); backdrop-filter: blur(10px); border-radius: 20px; padding: 20px; display: none; border: 1px solid var(--p); z-index: 1000; }
        video { width: 100%; border-radius: 12px; background: #000; margin-top: 10px; border: 1px solid #333; }
        .btn { background: var(--glass); border: 1px solid rgba(255,255,255,0.1); color: #fff; padding: 8px 16px; border-radius: 10px; cursor: pointer; font-family: inherit; font-size: 0.85rem; }
        .btn-p { background: var(--p); border: none; }
        
        .input-area { padding: 20px; background: rgba(10,10,12,0.9); border-top: 1px solid rgba(255,255,255,0.05); display: flex; gap: 12px; align-items: center; }
        #msg-in { flex: 1; background: #1c1c21; border: 1px solid transparent; padding: 14px 20px; border-radius: 30px; color: #fff; outline: none; transition: 0.2s; }
        #msg-in:focus { border-color: var(--p); background: #222228; }
    </style>
</head>
<body>
    <div id="sidebar">
        <div style="padding: 30px 20px;">
            <h2 style="color: var(--s); margin: 0; font-weight: 600; font-size: 1.4rem;">KUSTIFY <span style="font-weight: 300; opacity: 0.4;">V10</span></h2>
            <p style="font-size: 0.7rem; color: #555; margin: 5px 0 20px;">HYPER-X INFRASTRUCTURE</p>
            <button onclick="createGroupPrompt()" class="btn" style="width:100%; padding: 12px;">+ Create Channel</button>
        </div>
        <div id="groups-list" style="flex:1; overflow-y:auto;"></div>
    </div>

    <div id="main">
        <div style="padding: 15px 25px; display:flex; justify-content:space-between; align-items:center; border-bottom: 1px solid rgba(255,255,255,0.05); backdrop-filter: blur(10px);">
            <div id="active-group-name" style="font-weight: 600; font-size: 1.1rem;"># Lobby</div>
            <div style="display:flex; gap:10px;">
                <button onclick="startCall(false)" class="btn">🎤 Voice</button>
                <button onclick="startCall(true)" class="btn">🖥️ Share</button>
            </div>
        </div>

        <div id="feed"></div>

        <div id="preview-box">
            <img id="preview-img">
            <div style="display:flex; justify-content:space-between; margin-top:10px;">
                <span style="font-size:0.7rem; color:var(--s)">PREVIEW_MODE</span>
                <button onclick="cancelUpload()" style="background:none; border:none; color:#ff4444; cursor:pointer; font-size:0.75rem;">REMOVE</button>
            </div>
        </div>

        <div class="input-area">
            <input type="file" id="file-in" hidden onchange="showPreview(event)">
            <button onclick="document.getElementById('file-in').click()" style="background:none; border:none; font-size:1.4rem; cursor:pointer; opacity:0.6;">📎</button>
            <input type="text" id="msg-in" placeholder="Write a message..." autocomplete="off">
            <button onclick="sendMsg()" class="btn btn-p" style="width:48px; height:48px; border-radius:50%; font-size:1.2rem;">➤</button>
        </div>
    </div>

    <div id="vc-overlay">
        <div style="display:flex; justify-content:space-between; align-items:center;">
            <span style="font-size:0.7rem; color:var(--s); font-weight:600;">STREAM_ACTIVE</span>
            <div id="vc-timer" style="font-size:0.7rem; opacity:0.5;">00:00</div>
        </div>
        <div id="remote-streams"></div>
        <video id="local-video" autoplay muted playsinline></video>
        <div style="display:grid; grid-template-columns: 1fr 1fr; gap:10px; margin-top:15px;">
            <button onclick="toggleMute()" class="btn" id="mute-btn">Mute</button>
            <button onclick="endCall()" class="btn" style="background:#ff4444; border:none;">Leave</button>
        </div>
    </div>

    <script>
        const state = {
            user: localStorage.getItem("kv9_user") || "Anon",
            uid: "u_" + Math.random().toString(36).substr(2, 9),
            pfp: "https://ui-avatars.com/api/?background=7000ff&color=fff&name=K",
            group: "Lobby",
            ws: null
        };

        let currentFile = null;
        let editingId = null;
        let peer = null;
        let localStream = null;

        function showPreview(e) {
            const file = e.target.files[0];
            if (!file) return;
            currentFile = file;
            document.getElementById('preview-img').src = URL.createObjectURL(file);
            document.getElementById('preview-box').style.display = 'block';
        }

        function cancelUpload() {
            currentFile = null;
            document.getElementById('preview-box').style.display = 'none';
            document.getElementById('file-in').value = '';
        }

        async function createGroupPrompt() {
            const name = prompt("Channel Name:");
            if(!name) return;
            const isPrivate = confirm("Make Private?");
            await fetch('/api/groups', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({ name, is_private: isPrivate })
            });
            loadGroups();
        }

        async function loadGroups() {
            const res = await fetch('/api/groups').then(r=>r.json());
            const list = document.getElementById('groups-list');
            list.innerHTML = '';
            Object.keys(res.groups).forEach(name => {
                const g = res.groups[name];
                const div = document.createElement('div');
                div.className = `nav-item ${state.group === name ? 'active' : ''}`;
                div.innerHTML = `<span># ${name}</span> ${g.private ? '🔒' : ''}`;
                div.onclick = () => switchGroup(name);
                list.appendChild(div);
            });
        }

        function switchGroup(name) {
            state.group = name;
            document.getElementById('active-group-name').innerText = `# ${name}`;
            document.getElementById('feed').innerHTML = '';
            initWS();
            loadGroups();
        }

        function initWS() {
            if(state.ws) state.ws.close();
            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            state.ws = new WebSocket(`${proto}://${location.host}/ws/${state.group}/${state.uid}`);
            state.ws.onopen = () => state.ws.send(JSON.stringify({name: state.user, pfp: state.pfp}));
            state.ws.onmessage = (e) => {
                const data = JSON.parse(e.data);
                if(data.type === "message") renderMsg(data);
                if(data.type === "edit") updateMsg(data);
            };
        }

        async function sendMsg() {
            const input = document.getElementById('msg-in');
            const text = input.value.trim();
            if(!text && !currentFile) return;

            const payload = { type: "message", text: text, id: `m_${Date.now()}` };
            
            if(editingId) {
                payload.type = "edit";
                payload.id = editingId;
                editingId = null;
            }

            if(currentFile) {
                const fd = new FormData(); fd.append('file', currentFile);
                const res = await fetch('/api/upload', {method:'POST', body:fd}).then(r=>r.json());
                payload.image_url = res.url;
                cancelUpload();
            }

            state.ws.send(JSON.stringify(payload));
            input.value = '';
        }

        function renderMsg(d) {
            const feed = document.getElementById('feed');
            const isMe = d.user_id === state.uid;
            const div = document.createElement('div');
            div.id = d.id;
            div.className = `msg ${isMe ? 'me' : 'other'}`;
            div.innerHTML = `
                <div class="text">${marked.parse(d.text || '')}</div>
                ${d.image_url ? `<img src="${d.image_url}" style="max-width:100%; border-radius:8px; margin-top:8px;">` : ''}
                <div class="meta">${new Date(d.timestamp*1000).toLocaleTimeString([], {hour:'2-digit', minute:'2-digit'})} ${isMe ? '✓' : ''}</div>
            `;
            feed.appendChild(div);
            feed.scrollTop = feed.scrollHeight;
        }

        function updateMsg(d) {
            const el = document.getElementById(d.id);
            if(el) {
                el.querySelector('.text').innerHTML = marked.parse(d.text) + ' <small style="opacity:0.5">(edited)</small>';
            }
        }

        async function startCall(screen) {
            document.getElementById('vc-overlay').style.display = 'block';
            localStream = screen ? 
                await navigator.mediaDevices.getDisplayMedia({video: true, audio: true}) :
                await navigator.mediaDevices.getUserMedia({audio: true, video: false});
            
            if(screen) document.getElementById('local-video').srcObject = localStream;
            
            peer = new Peer(state.uid);
            peer.on('call', call => {
                call.answer(localStream);
                call.on('stream', s => renderRemote(s, call.peer));
            });
            state.ws.send(JSON.stringify({type: "vc_join"}));
        }

        function renderRemote(s, id) {
            if(document.getElementById('v-'+id)) return;
            const v = document.createElement('video');
            v.id = 'v-'+id; v.srcObject = s; v.autoplay = true;
            document.getElementById('remote-streams').appendChild(v);
        }

        function endCall() { location.reload(); }

        window.addEventListener('contextmenu', e => {
            const m = e.target.closest('.msg.me');
            if(m) {
                e.preventDefault();
                editingId = m.id;
                document.getElementById('msg-in').value = m.querySelector('.text').innerText.replace('(edited)', '').trim();
                document.getElementById('msg-in').focus();
            }
        });

        window.onload = () => { loadGroups(); initWS(); };
    </script>
</body>
</html>
"""
