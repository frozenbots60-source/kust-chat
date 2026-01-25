import os
import json
import asyncio
import boto3
from typing import List, Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from redis import asyncio as aioredis

app = FastAPI(title="Kustify Ultra", description="Premium Animated Chat", version="4.0")

# --- CONFIGURATION ---

# 1. Redis Configuration
REDIS_URL = os.getenv("UPSTASH_REDIS_URL") or os.getenv("UPSTASH_REDIS_REST_URL")
if not REDIS_URL:
    print("CRITICAL: REDIS_URL is missing.")

# 2. AWS S3 Configuration
AWS_ACCESS_KEY = os.getenv("BUCKETEER_AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("BUCKETEER_AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("BUCKETEER_AWS_REGION")
BUCKET_NAME = os.getenv("BUCKETEER_BUCKET_NAME")

# --- INITIALIZATION ---

redis = aioredis.from_url(REDIS_URL, decode_responses=True)

s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

GLOBAL_CHANNEL = "kustify_global_v4"
GROUPS_KEY = "kustify:groups_v4"

# --- MODELS ---

class GroupCreate(BaseModel):
    name: str

# --- CONNECTION MANAGER ---

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, group_id: str):
        await websocket.accept()
        if group_id not in self.active_connections:
            self.active_connections[group_id] = []
        self.active_connections[group_id].append(websocket)

    def disconnect(self, websocket: WebSocket, group_id: str):
        if group_id in self.active_connections:
            if websocket in self.active_connections[group_id]:
                self.active_connections[group_id].remove(websocket)

    async def broadcast(self, group_id: str, message: dict):
        if group_id in self.active_connections:
            data = json.dumps(message)
            for connection in self.active_connections[group_id]:
                try:
                    await connection.send_text(data)
                except:
                    pass

manager = ConnectionManager()

# --- BACKGROUND WORKER ---

async def redis_listener():
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            data = json.loads(message["data"])
            group_id = data.get("group_id")
            if group_id:
                await manager.broadcast(group_id, data)

@app.on_event("startup")
async def startup_event():
    if not await redis.sismember(GROUPS_KEY, "General"):
        await redis.sadd(GROUPS_KEY, "General")
    asyncio.create_task(redis_listener())

# --- API ---

@app.get("/api/groups")
async def get_groups():
    groups = await redis.smembers(GROUPS_KEY)
    return {"groups": list(groups)}

@app.post("/api/groups")
async def create_group(group: GroupCreate):
    await redis.sadd(GROUPS_KEY, group.name)
    return {"status": "success", "group": group.name}

@app.get("/api/history/{group_id}")
async def get_history(group_id: str, limit: int = 50):
    key = f"kustify:history:{group_id}"
    messages = await redis.lrange(key, -limit, -1)
    return [json.loads(m) for m in messages]

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    try:
        file_key = f"kustify_v4/{file.filename}"
        s3_client.upload_fileobj(
            file.file, BUCKET_NAME, file_key,
            ExtraArgs={'ContentType': file.content_type}
        )
        url = s3_client.generate_presigned_url(
            'get_object',
            Params={'Bucket': BUCKET_NAME, 'Key': file_key},
            ExpiresIn=604800
        )
        return {"url": url}
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})

# --- WEBSOCKET ---

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    await manager.connect(websocket, group_id)
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            mtype = data.get("type")

            if mtype == "heartbeat":
                await websocket.send_text(json.dumps({"type": "heartbeat_ack"}))
                continue

            elif mtype == "message":
                out = {
                    "type": "message",
                    "group_id": group_id,
                    "user_id": user_id,
                    "user_name": data.get("user_name"),
                    "user_pfp": data.get("user_pfp"),
                    "text": data.get("text"),
                    "image_url": data.get("image_url"),
                    "timestamp": data.get("timestamp")
                }
                # Save & Publish
                await redis.rpush(f"kustify:history:{group_id}", json.dumps(out))
                await redis.publish(GLOBAL_CHANNEL, json.dumps(out))

            elif mtype in ["vc_signal", "vc_update", "vc_talking"]:
                # Pass-through for WebRTC signaling and visualizer data
                data["sender_id"] = user_id
                await manager.broadcast(group_id, data)

    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id)
        await manager.broadcast(group_id, {"type": "user_left", "user_id": user_id})

# --- FRONTEND ---

@app.get("/")
async def get():
    return HTMLResponse(html_content)

html_content = """
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0, maximum-scale=1.0, user-scalable=no">
    <title>Kustify Ultra</title>
    <script src="https://unpkg.com/peerjs@1.4.7/dist/peerjs.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap" rel="stylesheet">
    <style>
        :root {
            --bg-dark: #09090b;
            --bg-glass: rgba(24, 24, 27, 0.7);
            --bg-panel: #18181b;
            --primary: #8b5cf6;
            --primary-glow: rgba(139, 92, 246, 0.5);
            --accent: #ec4899;
            --text-main: #fafafa;
            --text-muted: #a1a1aa;
            --border: rgba(255, 255, 255, 0.1);
            --msg-me: linear-gradient(135deg, #7c3aed, #db2777);
            --msg-other: #27272a;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; outline: none; -webkit-tap-highlight-color: transparent; }
        
        body {
            font-family: 'Outfit', sans-serif;
            background: var(--bg-dark);
            background-image: radial-gradient(circle at 10% 20%, rgba(139, 92, 246, 0.15) 0%, transparent 20%),
                              radial-gradient(circle at 90% 80%, rgba(236, 72, 153, 0.15) 0%, transparent 20%);
            color: var(--text-main);
            height: 100vh;
            display: flex;
            overflow: hidden;
        }

        /* --- UI COMPONENTS --- */
        
        /* Sidebar */
        #sidebar {
            width: 300px;
            background: var(--bg-glass);
            backdrop-filter: blur(20px);
            border-right: 1px solid var(--border);
            display: flex; flex-direction: column;
            z-index: 50;
            transition: transform 0.4s cubic-bezier(0.16, 1, 0.3, 1);
        }

        .brand {
            padding: 25px; font-size: 1.8rem; font-weight: 800;
            background: linear-gradient(to right, #fff, #a1a1aa);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent;
            border-bottom: 1px solid var(--border);
            display: flex; justify-content: space-between; align-items: center;
        }

        .user-profile-widget {
            padding: 15px; border-bottom: 1px solid var(--border);
            display: flex; align-items: center; gap: 10px; cursor: pointer;
            transition: 0.2s;
        }
        .user-profile-widget:hover { background: rgba(255,255,255,0.05); }
        .my-pfp { width: 40px; height: 40px; border-radius: 50%; object-fit: cover; border: 2px solid var(--primary); }

        .group-list { flex: 1; padding: 15px; overflow-y: auto; }
        .group-item {
            padding: 14px 18px; margin-bottom: 8px; border-radius: 12px;
            cursor: pointer; font-weight: 500; color: var(--text-muted);
            transition: all 0.3s ease; position: relative; overflow: hidden;
        }
        .group-item:hover { background: rgba(255,255,255,0.05); color: white; transform: translateX(5px); }
        .group-item.active { background: rgba(139, 92, 246, 0.15); color: var(--primary); border: 1px solid var(--primary-glow); }

        .create-btn {
            margin: 15px; padding: 12px; border-radius: 12px;
            background: rgba(255,255,255,0.03); border: 1px dashed var(--border);
            color: var(--text-muted); cursor: pointer; text-align: center;
            transition: 0.2s;
        }
        .create-btn:hover { border-color: var(--primary); color: var(--primary); background: rgba(139, 92, 246, 0.05); }

        /* Chat Area */
        #chat-area {
            flex: 1; display: flex; flex-direction: column;
            position: relative; background: transparent;
        }

        #chat-header {
            padding: 15px 25px;
            background: rgba(9, 9, 11, 0.8);
            backdrop-filter: blur(10px);
            border-bottom: 1px solid var(--border);
            display: flex; justify-content: space-between; align-items: center;
        }

        .vc-btn {
            padding: 10px 20px; border-radius: 30px; border: none;
            font-weight: 600; cursor: pointer;
            background: linear-gradient(135deg, #10b981, #059669);
            color: white; box-shadow: 0 4px 15px rgba(16, 185, 129, 0.3);
            transition: transform 0.2s;
            display: flex; align-items: center; gap: 8px;
        }
        .vc-btn:active { transform: scale(0.95); }

        #messages {
            flex: 1; overflow-y: auto; padding: 20px;
            display: flex; flex-direction: column; gap: 15px;
            scroll-behavior: smooth;
        }

        .message {
            max-width: 75%; display: flex; gap: 12px;
            animation: slideIn 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes slideIn { from { opacity: 0; transform: translateY(20px); } to { opacity: 1; transform: translateY(0); } }

        .msg-content {
            padding: 12px 18px; border-radius: 18px;
            font-size: 0.95rem; line-height: 1.5;
            position: relative; box-shadow: 0 2px 10px rgba(0,0,0,0.2);
        }
        
        .msg-own { align-self: flex-end; flex-direction: row-reverse; }
        .msg-own .msg-content { background: var(--msg-me); color: white; border-bottom-right-radius: 4px; }
        
        .msg-other { align-self: flex-start; }
        .msg-other .msg-content { background: var(--msg-other); border-bottom-left-radius: 4px; }

        .msg-avatar {
            width: 35px; height: 35px; border-radius: 50%; object-fit: cover;
            box-shadow: 0 2px 8px rgba(0,0,0,0.3);
        }

        .msg-meta { font-size: 0.75rem; margin-bottom: 4px; opacity: 0.8; font-weight: 600; }
        .msg-img { max-width: 100%; border-radius: 12px; margin-top: 10px; cursor: pointer; transition: transform 0.2s; }
        .msg-img:hover { transform: scale(1.02); }

        #input-area {
            padding: 20px; background: rgba(9, 9, 11, 0.9);
            backdrop-filter: blur(10px);
            border-top: 1px solid var(--border);
            display: flex; gap: 12px; align-items: center;
        }

        #msg-input {
            flex: 1; background: var(--bg-panel); border: 1px solid var(--border);
            padding: 14px 24px; border-radius: 30px; color: white;
            font-size: 1rem; transition: 0.2s;
        }
        #msg-input:focus { border-color: var(--primary); box-shadow: 0 0 15px rgba(139, 92, 246, 0.2); }

        .icon-btn {
            width: 48px; height: 48px; border-radius: 50%; border: none;
            display: flex; align-items: center; justify-content: center;
            cursor: pointer; font-size: 1.2rem; transition: 0.2s;
        }
        .attach-btn { background: rgba(255,255,255,0.05); color: var(--text-muted); }
        .attach-btn:hover { background: rgba(255,255,255,0.1); color: white; }
        .send-btn { background: var(--primary); color: white; box-shadow: 0 4px 15px var(--primary-glow); }
        .send-btn:hover { transform: scale(1.05); }

        /* --- MODALS --- */
        .modal-overlay {
            position: fixed; top: 0; left: 0; width: 100%; height: 100%;
            background: rgba(0,0,0,0.8); backdrop-filter: blur(8px);
            z-index: 100; display: none; justify-content: center; align-items: center;
            animation: fadeIn 0.3s ease;
        }
        @keyframes fadeIn { from { opacity: 0; } to { opacity: 1; } }

        .modal-box {
            background: #18181b; width: 90%; max-width: 420px;
            padding: 30px; border-radius: 24px; border: 1px solid var(--border);
            box-shadow: 0 20px 50px rgba(0,0,0,0.5);
            text-align: center;
            animation: scaleUp 0.3s cubic-bezier(0.16, 1, 0.3, 1);
        }
        @keyframes scaleUp { from { transform: scale(0.9); opacity: 0; } to { transform: scale(1); opacity: 1; } }

        .modal-box input {
            width: 100%; padding: 14px; margin: 20px 0; background: #09090b;
            border: 1px solid var(--border); border-radius: 12px; color: white;
        }
        .modal-box button {
            width: 100%; padding: 14px; background: var(--primary); border: none;
            border-radius: 12px; color: white; font-weight: bold; cursor: pointer;
        }

        /* --- VOICE CHAT --- */
        .vc-grid {
            display: grid; grid-template-columns: repeat(auto-fill, minmax(100px, 1fr));
            gap: 20px; margin: 20px 0; max-height: 400px; overflow-y: auto;
        }
        
        .vc-user {
            display: flex; flex-direction: column; align-items: center; gap: 8px;
        }
        
        .vc-avatar-container {
            position: relative; width: 80px; height: 80px;
            display: flex; justify-content: center; align-items: center;
        }
        
        .vc-avatar {
            width: 70px; height: 70px; border-radius: 50%; object-fit: cover;
            z-index: 2; border: 3px solid #27272a;
        }

        /* The Visualizer Ring */
        .vc-ring {
            position: absolute; top: 50%; left: 50%; transform: translate(-50%, -50%);
            width: 70px; height: 70px; border-radius: 50%;
            background: var(--primary); opacity: 0; z-index: 1;
            transition: width 0.05s, height 0.05s, opacity 0.1s;
        }

        .vc-mic-status {
            position: absolute; bottom: 0; right: 0; width: 24px; height: 24px;
            background: #ef4444; border-radius: 50%; border: 3px solid #18181b;
            display: flex; align-items: center; justify-content: center; font-size: 10px;
            z-index: 3;
        }

        /* --- MOBILE --- */
        @media (max-width: 768px) {
            #sidebar { position: fixed; height: 100%; transform: translateX(-100%); width: 80%; }
            #sidebar.open { transform: translateX(0); }
            .menu-btn { display: block; background: none; border: none; color: white; font-size: 1.5rem; margin-right: 15px; }
            .message { max-width: 90%; }
        }
        @media (min-width: 769px) { .menu-btn { display: none; } }

    </style>
</head>
<body>

    <div id="setup-modal" class="modal-overlay" style="display: flex;">
        <div class="modal-box">
            <h2 style="font-weight: 800; font-size: 2rem; margin-bottom: 5px;">Kustify</h2>
            <p style="color: var(--text-muted);">Set up your profile</p>
            
            <div style="margin: 20px auto; width: 100px; height: 100px; position: relative;">
                <img id="preview-pfp" src="https://ui-avatars.com/api/?background=random" style="width: 100%; height: 100%; border-radius: 50%; object-fit: cover;">
                <label for="pfp-upload" style="position: absolute; bottom: 0; right: 0; background: var(--primary); width: 32px; height: 32px; border-radius: 50%; display: flex; align-items: center; justify-content: center; cursor: pointer;">📷</label>
                <input type="file" id="pfp-upload" style="display: none;" onchange="uploadPfp()">
            </div>

            <input type="text" id="username-input" placeholder="Display Name">
            <button onclick="saveProfile()">Enter Kustify</button>
        </div>
    </div>

    <div id="vc-modal" class="modal-overlay">
        <div class="modal-box" style="max-width: 600px;">
            <div style="display: flex; justify-content: space-between; align-items: center; margin-bottom: 20px;">
                <h3>Voice Chat</h3>
                <span style="background: #064e3b; color: #34d399; padding: 4px 10px; border-radius: 20px; font-size: 0.8rem;">● Live</span>
            </div>
            
            <div class="vc-grid" id="vc-grid"></div>
            
            <div style="display: flex; justify-content: center; gap: 15px; margin-top: 20px;">
                <button onclick="toggleMute()" id="mute-btn" style="width: 50px; background: #27272a;">🎤</button>
                <button onclick="leaveVoice()" style="width: 50px; background: #ef4444;">❌</button>
            </div>
        </div>
    </div>

    <div id="sidebar">
        <div class="brand">Kustify <span style="font-size: 1rem; cursor: pointer;" onclick="toggleSidebar()">✕</span></div>
        <div class="user-profile-widget">
            <img id="side-pfp" class="my-pfp" src="">
            <div>
                <div id="side-name" style="font-weight: bold;"></div>
                <div style="font-size: 0.8rem; color: var(--text-muted);">Online</div>
            </div>
        </div>
        <div class="group-list" id="group-list"></div>
        <div class="create-btn" onclick="createGroup()">+ Create New Room</div>
    </div>

    <div id="chat-area">
        <div id="chat-header">
            <div style="display: flex; align-items: center;">
                <button class="menu-btn" onclick="toggleSidebar()">☰</button>
                <h3 id="header-title"># General</h3>
            </div>
            <button class="vc-btn" onclick="joinVoice()">
                <svg width="20" height="20" fill="none" stroke="currentColor" stroke-width="2" viewBox="0 0 24 24"><path d="M12 1a3 3 0 0 0-3 3v8a3 3 0 0 0 6 0V4a3 3 0 0 0-3-3z"/><path d="M19 10v2a7 7 0 0 1-14 0v-2"/></svg>
                Join Voice
            </button>
        </div>

        <div id="messages"></div>

        <div id="input-area">
            <button class="icon-btn attach-btn" onclick="document.getElementById('file-input').click()">+</button>
            <input type="file" id="file-input" hidden onchange="handleFile()">
            <input type="text" id="msg-input" placeholder="Type a message..." autocomplete="off">
            <button class="icon-btn send-btn" onclick="sendMessage()">➤</button>
        </div>
    </div>

    <div id="audio-container" hidden></div>

    <script>
        // --- STATE ---
        const state = {
            user: localStorage.getItem("k_user") || "",
            uid: localStorage.getItem("k_uid") || "u_" + Math.random().toString(36).substr(2),
            pfp: localStorage.getItem("k_pfp") || `https://ui-avatars.com/api/?background=random&name=User`,
            group: "General",
            ws: null,
            hb: null
        };
        localStorage.setItem("k_uid", state.uid);

        // --- VC STATE ---
        let peer = null;
        let myStream = null;
        let audioCtx = null;
        let analyser = null;
        let dataArray = null;
        let micLoop = null;
        let isMuted = false;

        // --- INIT ---
        window.onload = () => {
            if(state.user) {
                document.getElementById('setup-modal').style.display = 'none';
                updateProfileUI();
                initApp();
            } else {
                document.getElementById('preview-pfp').src = state.pfp;
            }
        };

        async function uploadPfp() {
            const file = document.getElementById('pfp-upload').files[0];
            if(!file) return;
            const form = new FormData(); form.append('file', file);
            try {
                const res = await fetch('/api/upload', {method: 'POST', body: form});
                const data = await res.json();
                state.pfp = data.url;
                document.getElementById('preview-pfp').src = state.pfp;
            } catch(e) { alert("Upload failed"); }
        }

        function saveProfile() {
            const name = document.getElementById('username-input').value.trim();
            if(!name) return;
            state.user = name;
            localStorage.setItem("k_user", state.user);
            localStorage.setItem("k_pfp", state.pfp);
            document.getElementById('setup-modal').style.display = 'none';
            updateProfileUI();
            initApp();
        }

        function updateProfileUI() {
            document.getElementById('side-pfp').src = state.pfp;
            document.getElementById('side-name').innerText = state.user;
        }

        function toggleSidebar() { document.getElementById('sidebar').classList.toggle('open'); }

        async function initApp() {
            loadGroups();
            connect(state.group);
        }

        async function loadGroups() {
            const res = await fetch('/api/groups');
            const data = await res.json();
            const list = document.getElementById('group-list');
            list.innerHTML = '';
            data.groups.forEach(g => {
                const div = document.createElement('div');
                div.className = `group-item ${g === state.group ? 'active' : ''}`;
                div.innerText = `# ${g}`;
                div.onclick = () => {
                    if(window.innerWidth < 768) toggleSidebar();
                    connect(g);
                };
                list.appendChild(div);
            });
        }

        async function createGroup() {
            const name = prompt("Group Name:");
            if(name) {
                await fetch('/api/groups', {method: 'POST', headers:{'Content-Type':'application/json'}, body: JSON.stringify({name})});
                loadGroups();
            }
        }

        // --- CHAT ---
        function connect(group) {
            if(state.ws) state.ws.close();
            if(state.hb) clearInterval(state.hb);
            leaveVoice();

            state.group = group;
            document.getElementById('header-title').innerText = `# ${group}`;
            loadGroups();
            document.getElementById('messages').innerHTML = '';

            fetch(`/api/history/${group}`).then(r=>r.json()).then(msgs => msgs.forEach(renderMsg));

            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            state.ws = new WebSocket(`${proto}://${location.host}/ws/${group}/${state.uid}`);

            state.ws.onopen = () => {
                state.hb = setInterval(() => state.ws.send(JSON.stringify({type: "heartbeat"})), 30000);
            };

            state.ws.onmessage = (e) => {
                const d = JSON.parse(e.data);
                if(d.type === "message") renderMsg(d);
                if(d.type.startsWith("vc_")) handleVcEvent(d);
            };
        }

        function renderMsg(d) {
            const box = document.getElementById('messages');
            const isMe = d.user_id === state.uid;
            const div = document.createElement('div');
            div.className = `message ${isMe ? 'msg-own' : 'msg-other'}`;
            
            let html = `
                <img class="msg-avatar" src="${d.user_pfp || 'https://ui-avatars.com/api/?name=?'}" >
                <div class="msg-content">
                    <div class="msg-meta">${d.user_name}</div>
                    ${d.text ? `<div>${d.text}</div>` : ''}
                    ${d.image_url ? `<img src="${d.image_url}" class="msg-img" onclick="window.open(this.src)">` : ''}
                </div>
            `;
            div.innerHTML = html;
            box.appendChild(div);
            box.scrollTop = box.scrollHeight;
        }

        function sendMessage() {
            const inp = document.getElementById('msg-input');
            const txt = inp.value.trim();
            if(!txt) return;
            state.ws.send(JSON.stringify({
                type: "message",
                user_name: state.user,
                user_pfp: state.pfp,
                text: txt,
                timestamp: Date.now()
            }));
            inp.value = '';
        }

        document.getElementById('msg-input').addEventListener('keypress', e => { if(e.key==='Enter') sendMessage(); });

        async function handleFile() {
            const file = document.getElementById('file-input').files[0];
            if(!file) return;
            const form = new FormData(); form.append('file', file);
            
            const btn = document.querySelector('.attach-btn');
            const old = btn.innerHTML; btn.innerHTML = '...';
            
            try {
                const res = await fetch('/api/upload', {method: 'POST', body: form});
                const d = await res.json();
                state.ws.send(JSON.stringify({
                    type: "message",
                    user_name: state.user,
                    user_pfp: state.pfp,
                    text: "",
                    image_url: d.url,
                    timestamp: Date.now()
                }));
            } catch(e) { alert("Err"); } 
            finally { btn.innerHTML = old; document.getElementById('file-input').value = ''; }
        }

        // --- VC (VISUALIZER & LOGIC) ---
        
        function joinVoice() {
            navigator.mediaDevices.getUserMedia({audio: true}).then(stream => {
                myStream = stream;
                isMuted = false;
                document.getElementById('vc-modal').style.display = 'flex';
                
                // Add Self
                addVcUser(state.uid, state.user, state.pfp);
                
                // Audio Context for Visualizer
                audioCtx = new (window.AudioContext || window.webkitAudioContext)();
                const src = audioCtx.createMediaStreamSource(stream);
                analyser = audioCtx.createAnalyser();
                analyser.fftSize = 64;
                src.connect(analyser);
                dataArray = new Uint8Array(analyser.frequencyBinCount);
                
                startVisualizerLoop();

                // PeerJS
                peer = new Peer(state.uid);
                peer.on('open', () => {
                    sendVc({type: "vc_signal", payload: {event: "join"}});
                });
                
                peer.on('call', call => {
                    call.answer(stream);
                    const aud = document.createElement('audio');
                    call.on('stream', s => playStream(aud, s));
                });
            }).catch(e => alert("Mic blocked"));
        }

        function sendVc(data) {
            state.ws.send(JSON.stringify({...data, sender_name: state.user, sender_pfp: state.pfp}));
        }

        function handleVcEvent(d) {
            if(d.sender_id === state.uid) return;
            
            if(d.type === "vc_signal") {
                if(d.payload.event === "join") {
                    addVcUser(d.sender_id, d.sender_name, d.sender_pfp);
                    const call = peer.call(d.sender_id, myStream);
                    const aud = document.createElement('audio');
                    call.on('stream', s => playStream(aud, s));
                    sendVc({type: "vc_signal", payload: {event: "ack"}, target_id: d.sender_id});
                }
                if(d.payload.event === "ack") {
                    addVcUser(d.sender_id, d.sender_name, d.sender_pfp);
                }
            }
            
            if(d.type === "vc_talking") {
                // Animate other user's ring based on volume
                const ring = document.getElementById(`ring-${d.sender_id}`);
                if(ring) {
                    const scale = 1 + (d.vol / 100);
                    ring.style.opacity = d.vol > 5 ? 0.8 : 0;
                    ring.style.width = `${70 * scale}px`;
                    ring.style.height = `${70 * scale}px`;
                }
            }
            
            if(d.type === "vc_update") {
                const el = document.getElementById(`mic-${d.sender_id}`);
                if(el) el.style.display = d.muted ? 'flex' : 'none';
            }
        }

        function startVisualizerLoop() {
            micLoop = setInterval(() => {
                if(!analyser || isMuted) return;
                analyser.getByteFrequencyData(dataArray);
                let sum = 0;
                for(let i=0; i<dataArray.length; i++) sum += dataArray[i];
                const avg = sum / dataArray.length;
                
                // Animate self
                const ring = document.getElementById(`ring-${state.uid}`);
                if(ring) {
                    const scale = 1 + (avg / 100);
                    ring.style.opacity = avg > 5 ? 0.8 : 0;
                    ring.style.width = `${70 * scale}px`;
                    ring.style.height = `${70 * scale}px`;
                }
                
                // Broadcast volume for others to animate
                if(avg > 5) sendVc({type: "vc_talking", vol: avg});
            }, 100);
        }

        function addVcUser(uid, name, pfp) {
            if(document.getElementById(`vc-u-${uid}`)) return;
            const grid = document.getElementById('vc-grid');
            const div = document.createElement('div');
            div.className = 'vc-user';
            div.id = `vc-u-${uid}`;
            div.innerHTML = `
                <div class="vc-avatar-container">
                    <div class="vc-ring" id="ring-${uid}"></div>
                    <img class="vc-avatar" src="${pfp}">
                    <div class="vc-mic-status" id="mic-${uid}" style="display:none;">✕</div>
                </div>
                <div style="font-size:0.8rem;">${name}</div>
            `;
            grid.appendChild(div);
        }

        function playStream(aud, s) {
            aud.srcObject = s;
            aud.addEventListener('loadedmetadata', () => aud.play());
            document.getElementById('audio-container').appendChild(aud);
        }

        function toggleMute() {
            isMuted = !isMuted;
            myStream.getAudioTracks()[0].enabled = !isMuted;
            document.getElementById('mute-btn').style.background = isMuted ? '#fff' : '#27272a';
            document.getElementById('mute-btn').style.color = isMuted ? '#000' : '#fff';
            
            // UI
            document.getElementById(`mic-${state.uid}`).style.display = isMuted ? 'flex' : 'none';
            sendVc({type: "vc_update", muted: isMuted});
        }

        function leaveVoice() {
            if(peer) peer.destroy();
            if(myStream) myStream.getTracks().forEach(t => t.stop());
            if(micLoop) clearInterval(micLoop);
            if(audioCtx) audioCtx.close();
            
            document.getElementById('vc-modal').style.display = 'none';
            document.getElementById('vc-grid').innerHTML = '';
            document.getElementById('audio-container').innerHTML = '';
        }

    </script>
</body>
</html>
"""
