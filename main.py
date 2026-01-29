import os
import json
import asyncio
import boto3
import time
import re
from typing import List, Dict, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from redis import asyncio as aioredis
from botocore.config import Config

# ==========================================
# KUSTIFY HYPER-X | V9.0 (STABLE - FIXED)
# ==========================================

app = FastAPI(title="Kustify Hyper-X", description="Next-Gen Infrastructure Chat", version="9.0")

# 1. Redis Configuration
RAW_REDIS_URL = os.getenv("UPSTASH_REDIS_URL") or os.getenv("REDIS_URL")
REDIS_URL = RAW_REDIS_URL

# 2. AWS S3 Configuration
AWS_ACCESS_KEY = os.getenv("BUCKETEER_AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("BUCKETEER_AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("BUCKETEER_AWS_REGION")
BUCKET_NAME = os.getenv("BUCKETEER_BUCKET_NAME")

# ==========================================
# BACKEND INFRASTRUCTURE
# ==========================================

# Initialize Redis
redis = aioredis.from_url(REDIS_URL, decode_responses=True)

# Initialize S3 with SigV4 support (Required for newer regions/Bucketeer)
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION,
    config=Config(signature_version='s3v4')
)

# Constants
GLOBAL_CHANNEL = "kustify:global:v9"
GROUPS_KEY = "kustify:groups:v9"
HISTORY_KEY = "kustify:history:v9:"
USERNAMES_KEY = "kustify:usernames:v9"

class GroupCreate(BaseModel):
    name: str

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
            if websocket in self.active_connections[group_id]:
                self.active_connections[group_id].remove(websocket)
                if not self.active_connections[group_id]:
                    del self.active_connections[group_id]
        if user_id in self.global_lookup:
            del self.global_lookup[user_id]
        if user_id in self.user_meta:
            del self.user_meta[user_id]
        return group_id

    async def broadcast_presence(self, group_id: str):
        if group_id not in self.active_connections:
            return
        users_in_group = [
            meta for uid, meta in self.user_meta.items() 
            if meta.get("group") == group_id
        ]
        payload = json.dumps({
            "type": "presence_update",
            "group_id": group_id,
            "count": len(users_in_group),
            "users": users_in_group
        })
        await self.broadcast_local(group_id, payload)

    async def broadcast_local(self, group_id: str, message: str):
        if group_id in self.active_connections:
            for connection in self.active_connections[group_id]:
                try:
                    await connection.send_text(message)
                except Exception:
                    pass

    async def send_personal_message(self, target_id: str, message: str):
        if target_id in self.global_lookup:
            try:
                await self.global_lookup[target_id].send_text(message)
                return True
            except:
                return False
        return False

manager = ConnectionManager()

# --- REDIS LISTENER WORKER ---
async def redis_listener():
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            try:
                data = json.loads(message["data"])
                mtype = data.get("type")
                if mtype == "message" or mtype == "vc_signal_group":
                    group_id = data.get("group_id")
                    if group_id:
                        await manager.broadcast_local(group_id, message["data"])
                elif mtype == "dm":
                    target_id = data.get("target_id")
                    if target_id:
                        await manager.send_personal_message(target_id, message["data"])
            except Exception as e:
                print(f"Redis Listener Error: {e}")

@app.on_event("startup")
async def startup_event():
    # Verify Redis connection and ensure default lobby
    try:
        if not await redis.sismember(GROUPS_KEY, "Lobby"):
            await redis.sadd(GROUPS_KEY, "Lobby")
        asyncio.create_task(redis_listener())
    except Exception as e:
        print(f"CRITICAL: Startup Redis Failure: {e}")

# ==========================================
# API ENDPOINTS
# ==========================================

@app.get("/api/groups")
async def get_groups():
    groups = await redis.smembers(GROUPS_KEY)
    return {"groups": sorted(list(groups))}

@app.post("/api/groups")
async def create_group(group: GroupCreate):
    name = re.sub(r'[^a-zA-Z0-9 _-]', '', group.name)
    if not name: return JSONResponse(status_code=400, content={"error": "Invalid name"})
    await redis.sadd(GROUPS_KEY, name)
    return {"status": "success", "group": name}

@app.get("/api/history/{group_id}")
async def get_history(group_id: str, limit: int = 100):
    key = f"{HISTORY_KEY}{group_id}"
    messages = await redis.lrange(key, -limit, -1)
    return [json.loads(m) for m in messages]

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        ext = file.filename.split('.')[-1]
        safe_name = f"{int(time.time())}_{os.urandom(4).hex()}.{ext}"
        file_key = f"kustify_v9/{safe_name}"
        s3_client.upload_fileobj(
            file.file, BUCKET_NAME, file_key,
            ExtraArgs={'ContentType': file.content_type}
        )
        url = s3_client.generate_presigned_url(
            'get_object', Params={'Bucket': BUCKET_NAME, 'Key': file_key}, ExpiresIn=604800
        )
        return {"url": url}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# ==========================================
# WEBSOCKET CONTROLLER
# ==========================================

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    await websocket.accept()
    
    try:
        init_data = await websocket.receive_json()
        name = init_data.get("name", "Anon").strip()
        pfp = init_data.get("pfp", "")
        
        is_new = await redis.sadd(USERNAMES_KEY, name)
        if is_new == 0:
            await websocket.send_json({"type": "error", "message": "NAME_TAKEN"})
            await websocket.close()
            return
            
        user_info = {"id": user_id, "name": name, "pfp": pfp}
    except Exception:
        await websocket.close()
        return

    await manager.connect(websocket, group_id, user_info)
    
    await websocket.send_text(json.dumps({
        "type": "system", 
        "text": f"Secure Connection Established: {group_id.upper()}"
    }))

    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            mtype = data.get("type")

            if mtype == "heartbeat":
                await websocket.send_text(json.dumps({"type": "heartbeat_ack"}))

            elif mtype == "message":
                msg_id = f"{user_id}-{int(time.time()*1000)}"
                out = {
                    "type": "message",
                    "id": msg_id,
                    "group_id": group_id,
                    "user_id": user_id,
                    "user_name": name,
                    "user_pfp": pfp,
                    "text": data.get("text"),
                    "image_url": data.get("image_url"),
                    "style": data.get("style", "normal"),
                    "timestamp": time.time()
                }
                await redis.rpush(f"{HISTORY_KEY}{group_id}", json.dumps(out))
                await redis.publish(GLOBAL_CHANNEL, json.dumps(out))

            elif mtype == "dm":
                target_id = data.get("target_id")
                if target_id:
                    out = {
                        "type": "dm",
                        "sender_id": user_id,
                        "sender_name": name,
                        "sender_pfp": pfp,
                        "target_id": target_id,
                        "text": data.get("text"),
                        "timestamp": time.time()
                    }
                    await redis.publish(GLOBAL_CHANNEL, json.dumps(out))
                    await websocket.send_text(json.dumps(out))

            elif mtype in ["vc_join", "vc_leave", "vc_signal", "vc_talking"]:
                data["sender_id"] = user_id
                if mtype == "vc_join":
                    data["user_name"] = name
                    data["user_pfp"] = pfp
                
                data["group_id"] = group_id
                
                if mtype == "vc_signal":
                    target = data.get("target_id")
                    if target:
                        payload = json.dumps({"type": "dm", "target_id": target, **data})
                        await redis.publish(GLOBAL_CHANNEL, payload)
                else:
                    data["type"] = "vc_signal_group" 
                    await redis.publish(GLOBAL_CHANNEL, json.dumps(data))

    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id, user_id)
        await redis.srem(USERNAMES_KEY, name)
        await manager.broadcast_presence(group_id)
        await redis.publish(GLOBAL_CHANNEL, json.dumps({
            "type": "vc_signal_group",
            "group_id": group_id,
            "sender_id": user_id,
            "subtype": "vc_leave"
        }))

# ==========================================
# FRONTEND APPLICATION (UNCHANGED)
# ==========================================

@app.get("/")
async def get():
    return HTMLResponse(html_content)

html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no, viewport-fit=cover">
    <title>KUSTIFY HYPER-X</title>
    <script src="https://unpkg.com/peerjs@1.4.7/dist/peerjs.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.0.2/marked.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #050505;
            --panel: rgba(20, 20, 23, 0.85);
            --border: rgba(255, 255, 255, 0.08);
            --primary: #7000ff;
            --accent: #00f3ff;
            --text-main: #eeeeee;
            --text-dim: #888888;
            --glass: blur(20px) saturate(180%);
            --radius: 16px;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; outline: none; -webkit-tap-highlight-color: transparent; }
        
        body {
            font-family: 'Outfit', sans-serif;
            background: var(--bg-dark);
            color: var(--text-main);
            height: 100dvh; 
            overflow: hidden;
            display: flex;
        }

        #infra-canvas {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            z-index: 0; opacity: 0.3; pointer-events: none;
        }

        ::-webkit-scrollbar { width: 4px; }
        ::-webkit-scrollbar-thumb { background: rgba(255,255,255,0.1); border-radius: 2px; }

        #sidebar {
            width: 280px;
            background: rgba(10, 10, 12, 0.8);
            backdrop-filter: var(--glass);
            border-right: 1px solid var(--border);
            z-index: 20;
            display: flex; flex-direction: column;
            transition: transform 0.3s cubic-bezier(0.4, 0, 0.2, 1);
        }

        .brand-area {
            padding: 24px;
            font-family: 'JetBrains Mono', monospace;
            font-weight: 800;
            font-size: 1.2rem;
            letter-spacing: -1px;
            border-bottom: 1px solid var(--border);
            display: flex; justify-content: space-between; align-items: center;
        }

        .user-card {
            padding: 20px;
            display: flex; align-items: center; gap: 12px;
            border-bottom: 1px solid var(--border);
            background: rgba(255,255,255,0.02);
        }
        .user-card img {
            width: 42px; height: 42px; border-radius: 12px;
            border: 2px solid var(--primary);
        }

        .nav-list { flex: 1; padding: 15px; overflow-y: auto; }
        .section-label { padding: 10px 5px; font-size: 0.7rem; color: #666; font-weight: 700; letter-spacing: 1px; margin-top: 10px; }
        
        .nav-item {
            padding: 12px 14px; margin-bottom: 6px;
            border-radius: 8px;
            color: var(--text-dim);
            font-size: 0.95rem;
            cursor: pointer;
            transition: 0.2s;
            display: flex; justify-content: space-between; align-items: center;
        }
        .nav-item:hover { background: rgba(255,255,255,0.05); color: #fff; }
        .nav-item.active { 
            background: rgba(112, 0, 255, 0.15); 
            color: var(--accent); 
            border-left: 3px solid var(--accent);
        }
        .dm-badge { width: 8px; height: 8px; background: var(--primary); border-radius: 50%; }

        .btn-create {
            margin: 20px; padding: 14px;
            border: 1px dashed var(--border);
            color: var(--text-dim);
            text-align: center; border-radius: 12px;
            cursor: pointer; font-size: 0.9rem;
            transition: 0.2s;
        }

        #main-view {
            flex: 1; display: flex; flex-direction: column;
            position: relative; z-index: 5;
            background: radial-gradient(circle at top right, rgba(112,0,255,0.08), transparent 40%);
        }

        header {
            height: 70px;
            display: flex; justify-content: space-between; align-items: center;
            padding: 0 20px;
            border-bottom: 1px solid var(--border);
            background: rgba(5, 5, 5, 0.4);
            backdrop-filter: blur(10px);
        }
        .room-info h2 { font-size: 1.1rem; font-weight: 600; display: flex; align-items: center; gap: 10px; }
        .live-badge { font-size: 0.7rem; padding: 4px 10px; background: rgba(0, 243, 255, 0.1); color: var(--accent); border-radius: 20px; border: 1px solid rgba(0, 243, 255, 0.3); display: none; }
        
        .btn-icon { 
            width: 44px; height: 44px; border-radius: 50%; 
            border: 1px solid var(--border); background: rgba(255,255,255,0.05); 
            color: #fff; cursor: pointer; display: flex; align-items: center; justify-content: center; 
            transition: 0.2s; font-size: 1.2rem;
        }
        .btn-icon:hover { background: var(--primary); border-color: var(--primary); }

        #chat-feed {
            flex: 1; overflow-y: auto; padding: 20px;
            display: flex; flex-direction: column; gap: 15px;
            padding-bottom: 100px; 
        }

        .msg-group { display: flex; gap: 12px; animation: slideIn 0.2s ease-out; width: 100%; }
        .msg-group.me { flex-direction: row-reverse; }
        
        .msg-avatar { 
            width: 38px; height: 38px; border-radius: 10px; 
            object-fit: cover; background: #222; cursor: pointer;
            border: 2px solid transparent; transition: 0.2s;
        }
        .msg-avatar:hover { border-color: var(--accent); }
        
        .msg-bubbles { display: flex; flex-direction: column; gap: 4px; max-width: 75%; }
        .msg-group.me .msg-bubbles { align-items: flex-end; }
        
        .msg-meta { font-size: 0.75rem; color: var(--text-dim); margin-bottom: 2px; }

        .bubble {
            padding: 10px 16px;
            border-radius: 4px 16px 16px 16px;
            background: #1e1e22;
            font-size: 0.95rem;
            line-height: 1.5;
            color: #e4e4e7;
            border: 1px solid rgba(255,255,255,0.05);
            word-wrap: break-word;
        }
        .msg-group.me .bubble {
            background: linear-gradient(135deg, #6002ee, #9c27b0);
            border-radius: 16px 4px 16px 16px;
            border: none;
        }
        .dm-bubble { border: 1px solid var(--accent); background: rgba(0, 243, 255, 0.05); }

        .bubble img { max-width: 100%; border-radius: 8px; margin-top: 8px; }

        .input-wrapper {
            position: absolute; bottom: 0; left: 0; width: 100%;
            padding: 15px;
            background: rgba(5,5,5,0.9);
            backdrop-filter: blur(20px);
            border-top: 1px solid var(--border);
            display: flex; gap: 10px; align-items: center;
        }
        
        .chat-input-box {
            flex: 1; background: #121214;
            border: 1px solid var(--border);
            border-radius: 25px;
            padding: 12px 20px;
            color: #fff; font-size: 1rem;
        }
        .chat-input-box:focus { border-color: var(--primary); }

        #vc-panel {
            position: absolute; top: 80px; right: 20px;
            width: 300px;
            background: rgba(15, 15, 20, 0.95);
            backdrop-filter: blur(30px);
            border: 1px solid rgba(255,255,255,0.1);
            border-radius: 20px;
            box-shadow: 0 20px 50px rgba(0,0,0,0.6);
            display: none; flex-direction: column;
            z-index: 50;
        }
        .vc-grid { padding: 15px; display: grid; grid-template-columns: repeat(4, 1fr); gap: 10px; max-height: 200px; overflow-y: auto; }
        .vc-slot { display: flex; flex-direction: column; align-items: center; position: relative; }
        .vc-slot img { width: 44px; height: 44px; border-radius: 50%; border: 2px solid #333; object-fit: cover; }
        .vc-slot.talking img { border-color: var(--accent); box-shadow: 0 0 10px var(--accent); }
        
        .modal { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.9); z-index: 100; display: flex; align-items: center; justify-content: center; }
        .modal-content { width: 90%; max-width: 400px; text-align: center; background: #111; padding: 30px; border-radius: 20px; border: 1px solid #333; }
        .modern-input { width: 100%; background: #222; border: 1px solid #333; color: white; padding: 15px; border-radius: 10px; margin: 20px 0; text-align: center; font-size: 1.1rem; }
        .btn-start { background: var(--primary); color: white; border: none; padding: 15px; width: 100%; border-radius: 10px; font-weight: 700; cursor: pointer; font-size: 1rem; }

        @media (max-width: 768px) {
            #sidebar { position: fixed; height: 100dvh; transform: translateX(-100%); width: 85%; }
            #sidebar.open { transform: translateX(0); }
            #vc-panel { width: 94%; right: 3%; top: 70px; }
            .bubble { font-size: 1rem; }
            .btn-icon { width: 40px; height: 40px; }
        }
    </style>
</head>
<body>
    <canvas id="infra-canvas"></canvas>

    <div id="setup-modal" class="modal">
        <div class="modal-content">
            <h2 style="font-family:'JetBrains Mono'; color:var(--accent)">IDENTITY_INIT</h2>
            <div style="margin: 20px auto; width: 100px; height: 100px; position: relative;">
                <img id="preview-pfp" src="https://ui-avatars.com/api/?background=random&name=?" style="width:100%; height:100%; border-radius:50%; object-fit:cover; border:2px solid #333;">
                <label for="pfp-upload" style="position:absolute; bottom:0; right:0; background:var(--primary); width:32px; height:32px; border-radius:50%; display:flex; align-items:center; justify-content:center;">📷</label>
            </div>
            <input type="file" id="pfp-upload" hidden onchange="uploadPfp()">
            <input type="text" id="username-input" class="modern-input" placeholder="Unique Alias" maxlength="12">
            <button class="btn-start" onclick="connectToServer()">ENTER SYSTEM</button>
            <p id="error-msg" style="color:#ff4444; font-size:0.8rem; margin-top:10px; display:none;">Alias Taken</p>
        </div>
    </div>

    <div id="sidebar">
        <div class="brand-area">
            KUSTIFY V9
            <span onclick="toggleSidebar()" style="cursor:pointer; font-size:1.5rem; display:none;" id="close-side">×</span>
        </div>
        <div class="user-card">
            <img id="side-pfp">
            <div>
                <div id="side-name" style="font-weight:700"></div>
                <div style="font-size:0.75rem; color:var(--accent)">● SECURE</div>
            </div>
        </div>
        
        <div class="nav-list" id="nav-list">
            <div class="section-label">CHANNELS</div>
            <div id="group-list"></div>
            <div class="section-label">DIRECT MESSAGES</div>
            <div id="dm-list"></div>
        </div>
        <div class="btn-create" onclick="createGroup()">+ New Node</div>
    </div>

    <div id="main-view">
        <header>
            <div class="room-info">
                <button onclick="toggleSidebar()" class="btn-icon" style="margin-right:10px; border:none; background:transparent;">☰</button> 
                <span id="header-title"># Lobby</span>
            </div>
            <div style="display:flex; gap:10px; align-items:center;">
                <span class="live-badge" id="vc-badge">VOICE</span>
                <button class="btn-icon" onclick="joinVoice()" id="join-vc-btn">🎤</button>
            </div>
        </header>

        <div id="chat-feed"></div>

        <div class="input-wrapper">
            <button class="btn-icon" onclick="document.getElementById('file-input').click()">+</button>
            <input type="file" id="file-input" hidden onchange="handleFile()">
            <input type="text" id="msg-input" class="chat-input-box" placeholder="Message..." autocomplete="off">
            <button class="btn-icon" style="background:var(--primary); border-color:var(--primary)" onclick="sendMessage()">➤</button>
        </div>
    </div>

    <div id="vc-panel">
        <div style="padding:15px; border-bottom:1px solid rgba(255,255,255,0.1); font-size:0.8rem; font-weight:700;">VOICE LINK</div>
        <div class="vc-grid" id="vc-grid"></div>
        <div style="padding:15px; display:flex; justify-content:center; gap:15px;">
            <button class="btn-icon" onclick="toggleMute()" id="mute-btn">🎙️</button>
            <button class="btn-icon" style="background:#ff4444; border-color:#ff4444" onclick="leaveVoice()">✖</button>
        </div>
    </div>

    <div id="audio-container" hidden></div>

    <script>
        const state = {
            user: localStorage.getItem("kv9_user") || "",
            uid: localStorage.getItem("kv9_uid") || "u_" + Math.random().toString(36).substr(2, 8),
            pfp: localStorage.getItem("kv9_pfp") || `https://ui-avatars.com/api/?background=random&color=fff&name=User`,
            group: "Lobby",
            dmTarget: null,
            ws: null,
            dms: {},
            users: []
        };
        localStorage.setItem("kv9_uid", state.uid);

        if(window.innerWidth < 768) document.getElementById('close-side').style.display='block';

        window.onload = () => {
            initBackground();
            document.getElementById('preview-pfp').src = state.pfp;
            if(state.user) document.getElementById('username-input').value = state.user;
        };

        async function uploadPfp() {
            const f = document.getElementById('pfp-upload').files[0];
            if(!f) return;
            const fd = new FormData(); fd.append('file', f);
            try {
                const r = await fetch('/api/upload', {method:'POST', body:fd});
                const d = await r.json();
                state.pfp = d.url;
                document.getElementById('preview-pfp').src = state.pfp;
            } catch(e){}
        }

        function connectToServer() {
            const n = document.getElementById('username-input').value.trim();
            if(!n) return;
            state.user = n;
            localStorage.setItem("kv9_user", n);
            localStorage.setItem("kv9_pfp", state.pfp);
            updateProfileUI();
            initConnection();
        }

        function updateProfileUI() {
            document.getElementById('side-pfp').src = state.pfp;
            document.getElementById('side-name').innerText = state.user;
        }

        function toggleSidebar() { document.getElementById('sidebar').classList.toggle('open'); }

        function initConnection() {
            loadGroups();
            connect(state.group);
        }

        async function loadGroups() {
            const r = await fetch('/api/groups');
            const d = await r.json();
            const l = document.getElementById('group-list');
            l.innerHTML = '';
            d.groups.forEach(g => {
                const el = document.createElement('div');
                el.className = `nav-item ${g === state.group && !state.dmTarget ? 'active' : ''}`;
                el.innerHTML = `<span># ${g}</span>`;
                el.onclick = () => { switchChannel(g); };
                l.appendChild(el);
            });
        }

        async function createGroup() {
            const n = prompt("Channel Name:");
            if(n) {
                await fetch('/api/groups', {method:'POST', body:JSON.stringify({name:n}), headers:{'Content-Type':'application/json'}});
                loadGroups();
            }
        }

        function switchChannel(group) {
            state.dmTarget = null;
            state.group = group;
            if(window.innerWidth < 768) toggleSidebar();
            connect(group);
        }

        function connect(group) {
            if(state.ws) state.ws.close();
            leaveVoice();
            document.getElementById('setup-modal').style.display = 'none';
            document.getElementById('header-title').innerText = `# ${group}`;
            document.getElementById('chat-feed').innerHTML = '';
            loadGroups();

            fetch(`/api/history/${group}`).then(r=>r.json()).then(msgs => {
                msgs.forEach(renderMessage);
            });

            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            state.ws = new WebSocket(`${proto}://${location.host}/ws/${group}/${state.uid}`);

            state.ws.onopen = () => {
                state.ws.send(JSON.stringify({name: state.user, pfp: state.pfp}));
                setInterval(() => { if(state.ws.readyState === 1) state.ws.send(JSON.stringify({type:"heartbeat"})); }, 30000);
            };

            state.ws.onmessage = (e) => {
                const d = JSON.parse(e.data);
                if(d.type === "error" && d.message === "NAME_TAKEN") {
                    document.getElementById('setup-modal').style.display = 'flex';
                    document.getElementById('error-msg').style.display = 'block';
                    state.ws.close();
                }
                else if(d.type === "message") { if(!state.dmTarget) renderMessage(d); }
                else if(d.type === "dm") handleIncomingDM(d);
                else if(d.type === "presence_update") updateUsers(d.users);
                else if(d.type === "vc_signal_group") handleVcSignal(d);
            };
        }

        function startDM(uid, name, pfp) {
            if(uid === state.uid) return;
            state.dmTarget = {id: uid, name: name, pfp: pfp};
            document.getElementById('header-title').innerText = `@ ${name}`;
            document.getElementById('chat-feed').innerHTML = '';
            if(state.dms[uid]) state.dms[uid].forEach(renderMessage);
            if(window.innerWidth < 768) toggleSidebar();
            updateDMList();
        }

        function handleIncomingDM(d) {
            const peerId = d.sender_id === state.uid ? d.target_id : d.sender_id;
            if(!state.dms[peerId]) state.dms[peerId] = [];
            state.dms[peerId].push(d);
            if(state.dmTarget && state.dmTarget.id === peerId) renderMessage(d);
            else updateDMList();
        }

        function updateDMList() {
            const l = document.getElementById('dm-list');
            l.innerHTML = '';
            Object.keys(state.dms).forEach(uid => {
                const lastMsg = state.dms[uid][state.dms[uid].length-1];
                const name = lastMsg.sender_id === uid ? lastMsg.sender_name : (state.dmTarget?.id === uid ? state.dmTarget.name : "User");
                const el = document.createElement('div');
                el.className = `nav-item ${state.dmTarget?.id === uid ? 'active' : ''}`;
                el.innerHTML = `<span>@ ${name}</span> <div class="dm-badge"></div>`;
                el.onclick = () => startDM(uid, name, "");
                l.appendChild(el);
            });
        }

        function renderMessage(d) {
            const feed = document.getElementById('chat-feed');
            const isMe = d.sender_id ? (d.sender_id === state.uid) : (d.user_id === state.uid);
            const pfp = d.sender_pfp || d.user_pfp;
            const name = d.sender_name || d.user_name;
            const uid = d.sender_id || d.user_id;
            const grp = document.createElement('div');
            grp.className = `msg-group ${isMe ? 'me' : ''}`;
            grp.innerHTML = `
                <img class="msg-avatar" src="${pfp}" onclick="startDM('${uid}', '${name}', '${pfp}')">
                <div class="msg-bubbles">
                    <div class="msg-meta">${name}</div>
                    <div class="bubble ${d.type === 'dm' ? 'dm-bubble' : ''}">
                        ${d.text ? marked.parse(d.text) : ''}
                        ${d.image_url ? `<img src="${d.image_url}">` : ''}
                    </div>
                </div>
            `;
            feed.appendChild(grp);
            feed.scrollTop = feed.scrollHeight;
        }

        function sendMessage() {
            const inp = document.getElementById('msg-input');
            const txt = inp.value.trim();
            if(!txt || !state.ws) return;
            state.ws.send(JSON.stringify({
                type: state.dmTarget ? "dm" : "message",
                target_id: state.dmTarget?.id,
                text: txt
            }));
            inp.value = '';
        }

        document.getElementById('msg-input').addEventListener('keydown', e => { if(e.key === 'Enter') sendMessage(); });

        async function handleFile() {
            const f = document.getElementById('file-input').files[0];
            if(!f) return;
            const fd = new FormData(); fd.append('file', f);
            try {
                const r = await fetch('/api/upload', {method:'POST', body:fd});
                const res = await r.json();
                state.ws.send(JSON.stringify({
                    type: state.dmTarget ? "dm" : "message",
                    target_id: state.dmTarget?.id,
                    text: "",
                    image_url: res.url
                }));
            } catch(e){}
        }

        function updateUsers(users) { state.users = users; }

        let peer = null, myStream = null, calls = {}, inVc = false;

        async function joinVoice() {
            try { myStream = await navigator.mediaDevices.getUserMedia({audio: true}); } catch(e) { return; }
            inVc = true;
            document.getElementById('vc-panel').style.display = 'flex';
            document.getElementById('vc-badge').style.display = 'block';
            document.getElementById('join-vc-btn').style.display = 'none';
            peer = new Peer(state.uid);
            peer.on('open', () => {
                state.ws.send(JSON.stringify({type: "vc_join"}));
                addVcSlot(state.uid, state.user, state.pfp);
            });
            peer.on('call', call => {
                call.answer(myStream);
                calls[call.peer] = call;
                handleStream(call);
            });
        }

        function handleVcSignal(d) {
            if(!inVc) return;
            if(d.subtype === "vc_leave") { removeVcUser(d.sender_id); return; }
            if(d.type === "vc_join") {
                addVcSlot(d.sender_id, d.user_name, d.user_pfp);
                if(d.sender_id !== state.uid) {
                    const call = peer.call(d.sender_id, myStream);
                    calls[d.sender_id] = call;
                    handleStream(call);
                }
            }
            if(d.type === "vc_talking") {
                const el = document.getElementById(`vc-u-${d.sender_id}`);
                if(el) { el.classList.add('talking'); setTimeout(()=>el.classList.remove('talking'), 200); }
            }
        }

        function handleStream(call) {
            call.on('stream', remoteStream => {
                if(document.getElementById(`aud-${call.peer}`)) return;
                const aud = document.createElement('audio');
                aud.id = `aud-${call.peer}`; aud.srcObject = remoteStream;
                aud.autoplay = true; aud.playsInline = true;
                document.getElementById('audio-container').appendChild(aud);
            });
        }

        function addVcSlot(uid, name, pfp) {
            if(document.getElementById(`vc-u-${uid}`)) return;
            const d = document.createElement('div');
            d.className = 'vc-slot'; d.id = `vc-u-${uid}`;
            d.innerHTML = `<img src="${pfp}"><span>${name.substr(0,6)}</span>`;
            document.getElementById('vc-grid').appendChild(d);
        }

        function removeVcUser(uid) {
            const el = document.getElementById(`vc-u-${uid}`); if(el) el.remove();
            const aud = document.getElementById(`aud-${uid}`); if(aud) aud.remove();
            if(calls[uid]) { calls[uid].close(); delete calls[uid]; }
        }

        function leaveVoice() {
            if(!inVc) return;
            inVc = false;
            state.ws.send(JSON.stringify({type: "vc_leave"}));
            if(peer) peer.destroy();
            if(myStream) myStream.getTracks().forEach(t => t.stop());
            document.getElementById('vc-panel').style.display = 'none';
            document.getElementById('vc-badge').style.display = 'none';
            document.getElementById('join-vc-btn').style.display = 'flex';
            document.getElementById('vc-grid').innerHTML = '';
            calls = {};
        }

        function toggleMute() {
            const track = myStream.getAudioTracks()[0];
            track.enabled = !track.enabled;
            document.getElementById('mute-btn').style.opacity = track.enabled ? 1 : 0.5;
        }

        function initBackground() {
            const canvas = document.getElementById('infra-canvas');
            const ctx = canvas.getContext('2d');
            let w, h, nodes = [];
            function resize() { w = canvas.width = window.innerWidth; h = canvas.height = window.innerHeight; }
            window.addEventListener('resize', resize); resize();
            class Node {
                constructor() { this.x=Math.random()*w; this.y=Math.random()*h; this.vx=(Math.random()-.5); this.vy=(Math.random()-.5); }
                update() { this.x+=this.vx; this.y+=this.vy; if(this.x<0||this.x>w)this.vx*=-1; if(this.y<0||this.y>h)this.vy*=-1; }
            }
            for(let i=0;i<30;i++) nodes.push(new Node());
            function animate() {
                ctx.clearRect(0,0,w,h);
                ctx.fillStyle = '#7000ff';
                for(let i=0; i<nodes.length; i++) {
                    nodes[i].update();
                    ctx.beginPath(); ctx.arc(nodes[i].x, nodes[i].y, 2, 0, Math.PI*2); ctx.fill();
                    for(let j=i+1; j<nodes.length; j++) {
                        let d = Math.hypot(nodes[i].x-nodes[j].x, nodes[i].y-nodes[j].y);
                        if(d<150) {
                            ctx.beginPath(); ctx.moveTo(nodes[i].x,nodes[i].y); ctx.lineTo(nodes[j].x,nodes[j].y);
                            ctx.strokeStyle=`rgba(112,0,255,${1-d/150})`; ctx.stroke();
                        }
                    }
                }
                requestAnimationFrame(animate);
            }
            animate();
        }
    </script>
</body>
</html>
