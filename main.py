import os
import json
import asyncio
import boto3
import time
import re
import uuid
from typing import List, Dict, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from redis import asyncio as aioredis
from botocore.config import Config

# ==========================================
# KUSTIFY HYPER-X | V10.0 (ADVANCED)
# ==========================================

app = FastAPI(title="Kustify Hyper-X", description="Ultra-Infrastructure Chat", version="10.0")

# Redis Configuration
REDIS_URL = os.getenv("UPSTASH_REDIS_URL") or os.getenv("REDIS_URL")
redis = aioredis.from_url(REDIS_URL, decode_responses=True)

# S3 Configuration
s3_client = boto3.client(
    's3',
    aws_access_key_id=os.getenv("BUCKETEER_AWS_ACCESS_KEY_ID"),
    aws_secret_access_key=os.getenv("BUCKETEER_AWS_SECRET_ACCESS_KEY"),
    region_name=os.getenv("BUCKETEER_AWS_REGION"),
    config=Config(signature_version='s3v4')
)
BUCKET_NAME = os.getenv("BUCKETEER_BUCKET_NAME")

# Constants
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

    def disconnect(self, websocket: WebSocket, group_id: str, user_id: str):
        if group_id in self.active_connections:
            if websocket in self.active_connections[group_id]:
                self.active_connections[group_id].remove(websocket)
        if user_id in self.global_lookup: del self.global_lookup[user_id]
        if user_id in self.user_meta: del self.user_meta[user_id]

    async def broadcast_local(self, group_id: str, message: str):
        if group_id in self.active_connections:
            for connection in self.active_connections[group_id]:
                try: await connection.send_text(message)
                except: pass

manager = ConnectionManager()

async def redis_listener():
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            try:
                data = json.loads(message["data"])
                if data.get("target_id"): # DM Logic
                    ws = manager.global_lookup.get(data["target_id"])
                    if ws: await ws.send_text(message["data"])
                else: # Group Logic
                    await manager.broadcast_local(data.get("group_id"), message["data"])
            except: pass

@app.on_event("startup")
async def startup_event():
    if not await redis.sismember(GROUPS_KEY, "Lobby"):
        await redis.sadd(GROUPS_KEY, json.dumps({"name": "Lobby", "private": False}))
    asyncio.create_task(redis_listener())

@app.get("/api/groups")
async def get_groups():
    raw = await redis.smembers(GROUPS_KEY)
    return {"groups": [json.loads(g) for g in raw]}

@app.post("/api/groups")
async def create_group(group: GroupCreate):
    name = re.sub(r'[^a-zA-Z0-9 _-]', '', group.name)
    payload = {"name": name, "private": group.is_private, "password": group.password}
    await redis.sadd(GROUPS_KEY, json.dumps(payload))
    return {"status": "success"}

@app.get("/api/history/{group_id}")
async def get_history(group_id: str):
    msgs = await redis.lrange(f"{HISTORY_KEY}{group_id}", -100, -1)
    return [json.loads(m) for m in msgs]

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = file.filename.split('.')[-1]
    key = f"v10/{int(time.time())}_{uuid.uuid4().hex}.{ext}"
    s3_client.upload_fileobj(file.file, BUCKET_NAME, key, ExtraArgs={'ContentType': file.content_type})
    url = s3_client.generate_presigned_url('get_object', Params={'Bucket': BUCKET_NAME, 'Key': key}, ExpiresIn=604800)
    return {"url": url}

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    await websocket.accept()
    try:
        init = await websocket.receive_json()
        name, pfp = init.get("name", "Anon"), init.get("pfp", "")
        await manager.connect(websocket, group_id, {"id": user_id, "name": name, "pfp": pfp})
        
        while True:
            data = await websocket.receive_json()
            mtype = data.get("type")
            
            if mtype in ["message", "edit", "dm", "vc_signal", "vc_join", "vc_leave"]:
                data["group_id"] = group_id
                data["sender_id"] = user_id
                data["sender_name"] = name
                data["sender_pfp"] = pfp
                data["timestamp"] = time.time()
                
                if mtype == "message":
                    data["msg_id"] = uuid.uuid4().hex
                    await redis.rpush(f"{HISTORY_KEY}{group_id}", json.dumps(data))
                
                if mtype == "edit":
                    # Logic to update history would go here (LREM + RPUSH)
                    pass

                await redis.publish(GLOBAL_CHANNEL, json.dumps(data))
    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id, user_id)

@app.get("/")
async def get(): return HTMLResponse(html_content)

html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, viewport-fit=cover">
    <title>KUSTIFY V10</title>
    <script src="https://unpkg.com/peerjs@1.4.7/dist/peerjs.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.0.2/marked.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono&family=Outfit:wght@300;600&display=swap" rel="stylesheet">
    <style>
        :root { --primary: #7000ff; --accent: #00f3ff; --bg: #050505; --panel: rgba(255,255,255,0.05); }
        * { margin: 0; padding: 0; box-sizing: border-box; }
        body { font-family: 'Outfit', sans-serif; background: var(--bg); color: #fff; height: 100dvh; display: flex; overflow: hidden; }
        #sidebar { width: 300px; border-right: 1px solid rgba(255,255,255,0.1); display: flex; flex-direction: column; background: #0a0a0a; transition: 0.3s; }
        .nav-item { padding: 15px; cursor: pointer; border-radius: 8px; margin: 5px 10px; transition: 0.2s; display: flex; justify-content: space-between; }
        .nav-item:hover, .nav-item.active { background: var(--panel); color: var(--accent); }
        #main { flex: 1; display: flex; flex-direction: column; position: relative; }
        #chat { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 10px; }
        .msg { max-width: 80%; padding: 12px; border-radius: 15px; background: #1a1a1a; position: relative; }
        .msg.me { align-self: flex-end; background: var(--primary); }
        .edit-btn { font-size: 10px; opacity: 0.5; cursor: pointer; margin-left: 10px; }
        .preview-area { display: none; padding: 10px; background: #111; border-top: 1px solid var(--primary); }
        .preview-area img { height: 80px; border-radius: 5px; }
        .input-bar { padding: 20px; display: flex; gap: 10px; background: #000; }
        input { flex: 1; background: #111; border: 1px solid #333; color: #fff; padding: 12px; border-radius: 25px; }
        #vc-bar { display: none; padding: 10px; background: var(--primary); display: flex; gap: 10px; overflow-x: auto; }
        .vc-user { width: 40px; height: 40px; border-radius: 50%; border: 2px solid #fff; }
        .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.8); display: none; align-items: center; justify-content: center; z-index: 100; }
        @media (max-width: 768px) { #sidebar { position: absolute; left: -300px; height: 100%; z-index: 90; } #sidebar.open { left: 0; } }
    </style>
</head>
<body>
    <div id="sidebar">
        <div style="padding:25px; font-weight:800; color:var(--primary)">KUSTIFY HYPER-X</div>
        <div id="groups" style="flex:1; overflow-y:auto"></div>
        <button onclick="showCreateGroup()" style="margin:20px; padding:10px; background:none; border:1px dashed #444; color:#888; cursor:pointer">+ New Group</button>
    </div>
    <div id="main">
        <header style="height:70px; border-bottom:1px solid #222; display:flex; align-items:center; padding:0 20px; justify-content:space-between">
            <div style="display:flex; align-items:center">
                <button onclick="document.getElementById('sidebar').classList.toggle('open')" style="background:none; border:none; color:#fff; font-size:20px; margin-right:15px">☰</button>
                <h3 id="group-name">Lobby</h3>
            </div>
            <div style="display:flex; gap:10px">
                <button onclick="startVC()" class="vc-btn">🎤 Join VC</button>
                <button onclick="startScreen()" class="vc-btn" id="screen-btn" style="display:none">🖥️ Share</button>
            </div>
        </header>
        <div id="vc-bar"></div>
        <div id="chat"></div>
        <div class="preview-area" id="preview-box">
            <img id="img-preview" src="">
            <button onclick="cancelUpload()">✕</button>
        </div>
        <div class="input-bar">
            <button onclick="document.getElementById('f').click()">+</button>
            <input type="file" id="f" hidden onchange="previewFile()">
            <input type="text" id="i" placeholder="Message..." onkeydown="if(event.key==='Enter')send()">
            <button onclick="send()">➤</button>
        </div>
    </div>

    <div id="g-modal" class="modal">
        <div style="background:#111; padding:30px; border-radius:15px; width:300px">
            <h4>Create Group</h4>
            <input type="text" id="gn" placeholder="Group Name" style="width:100%; margin:10px 0">
            <label><input type="checkbox" id="gp" onchange="document.getElementById('gpass').style.display=this.checked?'block':'none'"> Private</label>
            <input type="password" id="gpass" placeholder="Password" style="display:none; width:100%; margin:10px 0">
            <button onclick="doCreateGroup()" style="width:100%; padding:10px; background:var(--primary); border:none; color:#fff; margin-top:10px">Create</button>
        </div>
    </div>

    <script>
        let state = { user: "u_"+Math.random().toString(36).substr(2,7), name: "", group: "Lobby", ws: null, peer: null, stream: null, screen: null };
        
        async function previewFile() {
            const file = document.getElementById('f').files[0];
            if(!file) return;
            document.getElementById('img-preview').src = URL.createObjectURL(file);
            document.getElementById('preview-box').style.display = 'flex';
        }

        function cancelUpload() {
            document.getElementById('f').value = '';
            document.getElementById('preview-box').style.display = 'none';
        }

        async function send() {
            const i = document.getElementById('i');
            const file = document.getElementById('f').files[0];
            let url = null;
            if(file) {
                const fd = new FormData(); fd.append('file', file);
                const res = await fetch('/api/upload', {method:'POST', body:fd}).then(r=>r.json());
                url = res.url;
                cancelUpload();
            }
            if(!i.value && !url) return;
            state.ws.send(JSON.stringify({type: "message", text: i.value, image_url: url}));
            i.value = '';
        }

        function renderMessage(d) {
            const c = document.getElementById('chat');
            const m = document.createElement('div');
            const isMe = d.sender_id === state.user;
            m.className = `msg ${isMe ? 'me' : ''}`;
            m.id = `msg-${d.msg_id}`;
            m.innerHTML = `
                <small style="color:var(--accent)">${d.sender_name}</small>
                <div class="txt">${marked.parse(d.text || '')}</div>
                ${d.image_url ? `<img src="${d.image_url}" style="max-width:100%; border-radius:10px; margin-top:5px">` : ''}
                ${isMe ? `<span class="edit-btn" onclick="editMsg('${d.msg_id}')">edit</span>` : ''}
            `;
            c.appendChild(m);
            c.scrollTop = c.scrollHeight;
        }

        function editMsg(id) {
            const newText = prompt("Edit message:");
            if(newText) state.ws.send(JSON.stringify({type: "edit", msg_id: id, text: newText}));
        }

        async function startVC() {
            state.stream = await navigator.mediaDevices.getUserMedia({audio:true});
            state.peer = new Peer(state.user);
            document.getElementById('vc-bar').style.display = 'flex';
            document.getElementById('screen-btn').style.display = 'block';
            state.peer.on('call', call => {
                call.answer(state.stream);
                call.on('stream', s => addAudio(s, call.peer));
            });
            state.ws.send(JSON.stringify({type: "vc_join"}));
        }

        async function startScreen() {
            state.screen = await navigator.mediaDevices.getDisplayMedia({video:true, audio:true});
            // Logic to replace track in existing calls would go here
            alert("Screen Sharing Active (Peer-to-Peer Mesh)");
        }

        function addAudio(s, id) {
            if(document.getElementById('aud-'+id)) return;
            const a = document.createElement('audio');
            a.id = 'aud-'+id; a.srcObject = s; a.autoplay = true;
            document.body.appendChild(a);
        }

        async function init() {
            state.name = prompt("Username:") || "Ghost";
            const gr = await fetch('/api/groups').then(r=>r.json());
            const list = document.getElementById('groups');
            gr.groups.forEach(g => {
                const d = document.createElement('div'); d.className = 'nav-item';
                d.innerHTML = `<span># ${g.name}</span> ${g.private ? '🔒' : ''}`;
                d.onclick = () => { if(g.private && prompt("Password:") !== g.password) return; location.reload(); };
                list.appendChild(d);
            });
            
            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            state.ws = new WebSocket(`${proto}://${location.host}/ws/${state.group}/${state.user}`);
            state.ws.onopen = () => state.ws.send(JSON.stringify({name: state.name}));
            state.ws.onmessage = (e) => {
                const d = JSON.parse(e.data);
                if(d.type === "message") renderMessage(d);
                if(d.type === "edit") {
                    const el = document.querySelector(`#msg-${d.msg_id} .txt`);
                    if(el) el.innerHTML = marked.parse(d.text);
                }
                if(d.type === "vc_join" && state.peer && d.sender_id !== state.user) {
                    const call = state.peer.call(d.sender_id, state.stream);
                    call.on('stream', s => addAudio(s, d.sender_id));
                }
            };
            fetch(`/api/history/${state.group}`).then(r=>r.json()).then(msgs => msgs.forEach(renderMessage));
        }

        function showCreateGroup() { document.getElementById('g-modal').style.display = 'flex'; }
        async function doCreateGroup() {
            const name = document.getElementById('gn').value;
            const is_p = document.getElementById('gp').checked;
            const pass = document.getElementById('gpass').value;
            await fetch('/api/groups', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name, is_private:is_p, password:pass})});
            location.reload();
        }

        init();
    </script>
</body>
</html>
"""
