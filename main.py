import os
import json
import asyncio
import boto3
import time
from typing import List, Dict, Optional
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File, HTTPException, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from redis import asyncio as aioredis
from botocore.config import Config

# ==========================================
# KUSTIFY HYPER-X | V9.1 (STABLE - API & UI FIXED)
# ==========================================

app = FastAPI(
    title="Kustify Hyper-X API", 
    description="Next-Gen Infrastructure Chat API for Bots and Custom Clients", 
    version="9.1",
    docs_url="/docs",
    redoc_url="/redoc"
)

# 1. Redis Configuration
RAW_REDIS_URL = os.getenv("UPSTASH_REDIS_URL") or os.getenv("REDIS_URL")
REDIS_URL = RAW_REDIS_URL

# 2. AWS S3 Configuration
AWS_ACCESS_KEY = os.getenv("BUCKETEER_AWS_ACCESS_KEY_ID")
AWS_SECRET_KEY = os.getenv("BUCKETEER_AWS_SECRET_ACCESS_KEY")
AWS_REGION = os.getenv("BUCKETEER_AWS_REGION")
BUCKET_NAME = os.getenv("BUCKETEER_BUCKET_NAME")

# Initialize Redis & S3
redis = aioredis.from_url(REDIS_URL, decode_responses=True)
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION,
    config=Config(signature_version='s3v4')
)

GLOBAL_CHANNEL = "kustify:global:v9"
GROUPS_KEY = "kustify:groups:v9" # Set of public group names
PVT_GROUPS_KEY = "kustify:pvt_groups:v9" # Set of private group names
GROUP_META_KEY = "kustify:group_meta:v9:" # Hash for group details (password, owner)
HISTORY_KEY = "kustify:history:v9:"

class GroupCreateRequest(BaseModel):
    name: str
    type: str = "public" # public or private
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
            if websocket in self.active_connections[group_id]:
                self.active_connections[group_id].remove(websocket)
        if user_id in self.global_lookup: del self.global_lookup[user_id]
        if user_id in self.user_meta: del self.user_meta[user_id]

    async def broadcast_presence(self, group_id: str):
        users = [m for uid, m in self.user_meta.items() if m.get("group") == group_id]
        payload = json.dumps({"type": "presence_update", "group_id": group_id, "count": len(users), "users": users})
        await self.broadcast_local(group_id, payload)

    async def broadcast_local(self, group_id: str, message: str):
        if group_id in self.active_connections:
            for connection in self.active_connections[group_id]:
                try: await connection.send_text(message)
                except: pass

    async def send_personal_message(self, target_id: str, message: str):
        if target_id in self.global_lookup:
            try: await self.global_lookup[target_id].send_text(message); return True
            except: return False
        return False

manager = ConnectionManager()

async def redis_listener():
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            try:
                data = json.loads(message["data"])
                mtype = data.get("type")
                if mtype in ["message", "vc_signal_group", "edit_message", "delete_message"]:
                    await manager.broadcast_local(data.get("group_id"), message["data"])
                elif mtype == "dm":
                    await manager.send_personal_message(data.get("target_id"), message["data"])
            except: pass

@app.on_event("startup")
async def startup_event():
    # Ensure Lobby exists
    if not await redis.sismember(GROUPS_KEY, "Lobby"): 
        await redis.sadd(GROUPS_KEY, "Lobby")
    asyncio.create_task(redis_listener())

# ===========================
# PUBLIC API ENDPOINTS
# ===========================

@app.get("/api/groups", tags=["Groups"], summary="List public groups")
async def get_groups():
    """Returns a list of all public groups."""
    return {"groups": sorted(list(await redis.smembers(GROUPS_KEY)))}

@app.post("/api/groups/create", tags=["Groups"], summary="Create a new group")
async def create_group(group: GroupCreateRequest):
    """
    Create a new group. 
    - **type**: 'public' (listed) or 'private' (unlisted, requires password/direct link)
    """
    safe_name = "".join(x for x in group.name if x.isalnum() or x in "-_")
    if len(safe_name) < 3: raise HTTPException(400, "Name too short")
    
    if await redis.sismember(GROUPS_KEY, safe_name) or await redis.sismember(PVT_GROUPS_KEY, safe_name):
        raise HTTPException(400, "Group already exists")

    if group.type == "private":
        await redis.sadd(PVT_GROUPS_KEY, safe_name)
        if group.password:
            await redis.hset(f"{GROUP_META_KEY}{safe_name}", mapping={"password": group.password, "type": "private"})
    else:
        await redis.sadd(GROUPS_KEY, safe_name)
    
    return {"status": "created", "name": safe_name, "type": group.type}

@app.post("/api/groups/join_private", tags=["Groups"])
async def check_private_group(name: str = Form(...), password: str = Form("")):
    if not await redis.sismember(PVT_GROUPS_KEY, name):
        raise HTTPException(404, "Group not found")
    
    meta = await redis.hgetall(f"{GROUP_META_KEY}{name}")
    stored_pass = meta.get("password")
    
    if stored_pass and stored_pass != password:
        raise HTTPException(403, "Invalid Password")
        
    return {"status": "authorized", "group": name}

@app.get("/api/history/{group_id}", tags=["Chat"])
async def get_history(group_id: str, limit: int = 100):
    """Fetch chat history for a specific group."""
    messages = await redis.lrange(f"{HISTORY_KEY}{group_id}", -limit, -1)
    return [json.loads(m) for m in messages]

@app.post("/api/upload", tags=["Files"])
async def upload_file(file: UploadFile = File(...)):
    """Upload a file to S3 and get a presigned URL."""
    ext = file.filename.split('.')[-1]
    safe_name = f"{int(time.time())}_{os.urandom(4).hex()}.{ext}"
    file_key = f"kustify_v9/{safe_name}"
    s3_client.upload_fileobj(file.file, BUCKET_NAME, file_key, ExtraArgs={'ContentType': file.content_type})
    url = s3_client.generate_presigned_url('get_object', Params={'Bucket': BUCKET_NAME, 'Key': file_key}, ExpiresIn=604800)
    return {"url": url}

# ===========================
# WEBSOCKET
# ===========================

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    await websocket.accept()
    try:
        init_data = await websocket.receive_json()
        name = init_data.get("name", "Anon").strip()
        pfp = init_data.get("pfp", "").strip()
        # Default PFP if none provided
        if not pfp: pfp = "https://api.dicebear.com/7.x/identicon/svg?seed=" + user_id
        
        user_info = {"id": user_id, "name": name, "pfp": pfp}
        await manager.connect(websocket, group_id, user_info)
    except: return

    try:
        while True:
            data = await websocket.receive_json()
            mtype = data.get("type")

            if mtype == "message":
                # Detect if message is an image URL (basic check for image tag logic in UI)
                text = data.get("text", "")
                
                data.update({
                    "id": f"msg_{int(time.time()*1000)}", 
                    "user_id": user_id, 
                    "user_name": name, 
                    "user_pfp": user_info["pfp"],
                    "group_id": group_id, 
                    "timestamp": time.time()
                })
                await redis.rpush(f"{HISTORY_KEY}{group_id}", json.dumps(data))
                await redis.publish(GLOBAL_CHANNEL, json.dumps(data))

            elif mtype == "edit_message":
                msg_id = data.get("message_id")
                history_key = f"{HISTORY_KEY}{group_id}"
                msgs = await redis.lrange(history_key, 0, -1)
                for i, m_str in enumerate(msgs):
                    m = json.loads(m_str)
                    if m.get("id") == msg_id and m.get("user_id") == user_id:
                        m["text"] = data.get("new_text")
                        m["edited"] = True
                        await redis.lset(history_key, i, json.dumps(m))
                        await redis.publish(GLOBAL_CHANNEL, json.dumps({
                            "type": "edit_message", 
                            "group_id": group_id, 
                            "id": msg_id, 
                            "text": m["text"]
                        }))
                        break

            elif mtype == "delete_message":
                msg_id = data.get("message_id")
                history_key = f"{HISTORY_KEY}{group_id}"
                msgs = await redis.lrange(history_key, 0, -1)
                for m_str in msgs:
                    m = json.loads(m_str)
                    if m.get("id") == msg_id and m.get("user_id") == user_id:
                        await redis.lrem(history_key, 1, m_str)
                        await redis.publish(GLOBAL_CHANNEL, json.dumps({"type": "delete_message", "group_id": group_id, "id": msg_id}))
                        break
            
            elif mtype == "dm":
                data.update({"sender_id": user_id, "timestamp": time.time()})
                await redis.publish(GLOBAL_CHANNEL, json.dumps(data))
                await websocket.send_json(data)

            elif mtype in ["vc_join", "vc_leave", "vc_signal"]:
                data.update({"sender_id": user_id, "group_id": group_id})
                await redis.publish(GLOBAL_CHANNEL, json.dumps({**data, "type": "vc_signal_group"}))

    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id, user_id)
        await manager.broadcast_presence(group_id)

@app.get("/")
async def get(): return HTMLResponse(html_content)

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
        :root { --bg-dark: #050505; --panel: rgba(20, 20, 23, 0.95); --border: rgba(255, 255, 255, 0.08); --primary: #7000ff; --accent: #00f3ff; --text-main: #eeeeee; --text-dim: #888888; --glass: blur(20px) saturate(180%); --radius: 16px; }
        * { margin: 0; padding: 0; box-sizing: border-box; outline: none; }
        body { font-family: 'Outfit', sans-serif; background: var(--bg-dark); color: var(--text-main); height: 100dvh; overflow: hidden; display: flex; }
        
        /* Layout */
        #sidebar { width: 280px; background: rgba(10, 10, 12, 0.9); backdrop-filter: var(--glass); border-right: 1px solid var(--border); z-index: 20; display: flex; flex-direction: column; transition: transform 0.3s ease; }
        .brand-area { padding: 24px; font-family: 'JetBrains Mono'; font-weight: 800; border-bottom: 1px solid var(--border); color: var(--primary); display: flex; justify-content: space-between; align-items: center; }
        .nav-list { flex: 1; padding: 15px; overflow-y: auto; }
        .nav-item { padding: 12px; border-radius: 8px; color: var(--text-dim); cursor: pointer; margin-bottom: 5px; display: flex; justify-content: space-between; align-items: center; }
        .nav-item:hover { background: rgba(255,255,255,0.05); }
        .nav-item.active { background: rgba(112, 0, 255, 0.1); color: var(--accent); border-left: 3px solid var(--accent); }
        .nav-footer { padding: 20px; border-top: 1px solid var(--border); font-size: 0.8rem; text-align: center; color: var(--text-dim); }
        
        #main-view { flex: 1; display: flex; flex-direction: column; position: relative; }
        header { height: 70px; display: flex; justify-content: space-between; align-items: center; padding: 0 20px; border-bottom: 1px solid var(--border); background: rgba(5,5,5,0.8); backdrop-filter: blur(10px); }
        
        /* Chat Feed */
        #chat-feed { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 20px; }
        
        .msg-row { display: flex; gap: 12px; align-items: flex-end; }
        .msg-row.me { flex-direction: row-reverse; }
        
        .pfp-icon { width: 35px; height: 35px; border-radius: 50%; object-fit: cover; border: 2px solid var(--border); background: #222; flex-shrink: 0; }
        
        .msg-content { display: flex; flex-direction: column; max-width: 70%; }
        .msg-info { font-size: 0.75rem; color: var(--text-dim); margin-bottom: 4px; display: flex; gap: 8px; }
        .me .msg-info { justify-content: flex-end; }
        
        .bubble { padding: 12px 16px; border-radius: 12px; background: #1a1a1d; color: #fff; word-wrap: break-word; position: relative; line-height: 1.5; }
        .me .bubble { background: var(--primary); border-bottom-right-radius: 2px; }
        .msg-row:not(.me) .bubble { border-bottom-left-radius: 2px; }
        
        /* Image Handling */
        .bubble img { max-width: 100%; height: auto; border-radius: 8px; margin-top: 5px; display: block; cursor: pointer; }
        
        /* Context Menu */
        #ctx-menu { position: fixed; background: #222; border: 1px solid var(--border); border-radius: 8px; padding: 5px; display: none; z-index: 1000; box-shadow: 0 10px 30px rgba(0,0,0,0.5); width: 150px; }
        .ctx-item { padding: 10px; cursor: pointer; font-size: 0.9rem; color: #eee; border-radius: 4px; }
        .ctx-item:hover { background: var(--primary); }
        .ctx-item.delete { color: #ff4444; }
        .ctx-item.delete:hover { background: rgba(255, 68, 68, 0.2); }

        /* Input Area */
        .input-wrapper { padding: 20px; background: #000; border-top: 1px solid var(--border); display: flex; gap: 10px; align-items: center; }
        .chat-input-box { flex: 1; background: #111; border: 1px solid var(--border); color: #fff; padding: 12px 20px; border-radius: 30px; font-family: inherit; }
        .btn-icon { background: none; border: none; color: var(--text-dim); cursor: pointer; padding: 10px; font-size: 1.2rem; transition: 0.2s; }
        .btn-icon:hover { color: var(--accent); }
        
        /* Modals */
        .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.9); z-index: 100; display: none; align-items: center; justify-content: center; backdrop-filter: blur(5px); }
        .modal-content { background: #111; border: 1px solid var(--border); padding: 30px; border-radius: 20px; width: 90%; max-width: 400px; text-align: center; }
        .input-std { width: 100%; padding: 15px; background: #050505; border: 1px solid #333; color: #fff; border-radius: 10px; margin-bottom: 15px; }
        .btn-primary { background: var(--primary); color: #fff; border: none; padding: 15px; width: 100%; border-radius: 10px; font-weight: 700; cursor: pointer; }
        .btn-text { background: none; border: none; color: var(--text-dim); margin-top: 10px; cursor: pointer; text-decoration: underline; }

        @media (max-width: 768px) { #sidebar { position: fixed; height: 100%; transform: translateX(-100%); } #sidebar.open { transform: translateX(0); } }
    </style>
</head>
<body onclick="hideCtx()">

    <div id="setup-modal" class="modal" style="display: flex;">
        <div class="modal-content">
            <h1 style="font-family: 'JetBrains Mono'; margin-bottom: 20px; color: var(--accent);">IDENTITY_INIT</h1>
            <input type="text" id="username-input" class="input-std" placeholder="Display Name (e.g. Neo)">
            <input type="text" id="pfp-input" class="input-std" placeholder="Avatar URL (Optional)">
            <button class="btn-primary" onclick="saveUser()">CONNECT TO MATRIX</button>
            <p style="margin-top:15px; font-size:0.8rem; color:#666;">Leave URL empty for auto-generated avatar</p>
        </div>
    </div>

    <div id="create-group-modal" class="modal">
        <div class="modal-content">
            <h3>Create Channel</h3><br>
            <input type="text" id="new-group-name" class="input-std" placeholder="Channel Name">
            <select id="new-group-type" class="input-std" onchange="togglePassField()">
                <option value="public">Public (Listed)</option>
                <option value="private">Private (Password)</option>
            </select>
            <input type="password" id="new-group-pass" class="input-std" placeholder="Password" style="display:none;">
            <button class="btn-primary" onclick="createGroup()">INITIALIZE</button>
            <button class="btn-text" onclick="closeModal('create-group-modal')">Cancel</button>
        </div>
    </div>
    
    <div id="auth-modal" class="modal">
        <div class="modal-content">
            <h3>Restricted Access</h3><br>
            <p style="margin-bottom:15px; color:#aaa;">Enter credentials for <span id="auth-target" style="color:#fff;"></span></p>
            <input type="password" id="auth-pass" class="input-std" placeholder="Access Code">
            <button class="btn-primary" onclick="submitAuth()">DECRYPT</button>
            <button class="btn-text" onclick="closeModal('auth-modal')">Abort</button>
        </div>
    </div>

    <div id="ctx-menu">
        <div class="ctx-item" onclick="handleCtx('edit')">Edit Message</div>
        <div class="ctx-item" onclick="handleCtx('copy')">Copy Text</div>
        <div class="ctx-item delete" onclick="handleCtx('delete')">Delete</div>
    </div>

    <div id="sidebar">
        <div class="brand-area">
            <span>HYPER-X v9.1</span>
            <button class="btn-icon" onclick="openCreateModal()" style="font-size:1.2rem;">+</button>
        </div>
        <div class="nav-list" id="group-list"></div>
        <div class="nav-footer">
            <a href="/docs" target="_blank" style="color:var(--text-dim); text-decoration:none;">API Documentation</a>
        </div>
    </div>

    <div id="main-view">
        <header>
            <div style="display:flex; align-items:center; gap:10px;">
                <button class="btn-icon" onclick="document.getElementById('sidebar').classList.toggle('open')" style="display:none;">☰</button>
                <h3 id="header-title"># Lobby</h3>
            </div>
            <div id="users-online" style="font-size:0.8rem; color:var(--accent);"></div>
        </header>
        
        <div id="chat-feed"></div>
        
        <div class="input-wrapper">
            <button class="btn-icon" onclick="document.getElementById('file-input').click()">📎</button>
            <input type="file" id="file-input" hidden onchange="uploadFile(this)">
            <input type="text" id="msg-input" class="chat-input-box" placeholder="Transmission..." onkeypress="if(event.key==='Enter') sendMessage()">
            <button class="btn-icon" onclick="sendMessage()" style="color:var(--primary); background:rgba(112,0,255,0.1); border-radius:50%; width:45px; height:45px;">➤</button>
        </div>
    </div>

    <script>
        const state = {
            user: getCookie("k_user"),
            uid: getCookie("k_uid") || "u_" + Math.random().toString(36).substr(2, 9),
            pfp: getCookie("k_pfp") || "",
            group: "Lobby", 
            ws: null,
            ctxTarget: null,
            ctxId: null
        };

        function setCookie(n, v) { const d = new Date(); d.setTime(d.getTime() + (365*24*60*60*1000)); document.cookie = `${n}=${v};expires=${d.toUTCString()};path=/`; }
        function getCookie(n) { const v = document.cookie.match('(^|;) ?' + n + '=([^;]*)(;|$)'); return v ? v[2] : null; }

        window.onload = () => {
            if(state.user) { 
                document.getElementById('setup-modal').style.display = 'none';
                init();
            }
        };

        function togglePassField() {
            const type = document.getElementById('new-group-type').value;
            document.getElementById('new-group-pass').style.display = type === 'private' ? 'block' : 'none';
        }

        function openCreateModal() { document.getElementById('create-group-modal').style.display = 'flex'; }
        function closeModal(id) { document.getElementById(id).style.display = 'none'; }

        async function createGroup() {
            const name = document.getElementById('new-group-name').value.trim();
            const type = document.getElementById('new-group-type').value;
            const pass = document.getElementById('new-group-pass').value;
            
            if(!name) return alert("Name required");
            
            const res = await fetch('/api/groups/create', {
                method: 'POST',
                headers: {'Content-Type': 'application/json'},
                body: JSON.stringify({name, type, password: pass})
            });
            
            if(res.ok) {
                closeModal('create-group-modal');
                if(type === 'public') init(); // Refresh list
                else switchGroup(name, true);
            } else {
                alert("Failed: " + (await res.json()).detail);
            }
        }

        function saveUser() {
            const n = document.getElementById('username-input').value.trim();
            const p = document.getElementById('pfp-input').value.trim();
            if(!n) return;
            state.user = n;
            state.pfp = p;
            setCookie("k_user", n);
            setCookie("k_uid", state.uid);
            setCookie("k_pfp", p);
            document.getElementById('setup-modal').style.display = 'none';
            init();
        }

        async function init() {
            const r = await fetch('/api/groups'); 
            const d = await r.json();
            const gl = document.getElementById('group-list');
            gl.innerHTML = '';
            
            // Static Lobby
            renderGroupItem("Lobby", gl);
            
            d.groups.forEach(g => {
                if(g !== "Lobby") renderGroupItem(g, gl);
            });
            
            // Add Private Group Joiner Button
            const pvtBtn = document.createElement('div');
            pvtBtn.className = 'nav-item';
            pvtBtn.innerHTML = '<span style="opacity:0.5">+ Join Private</span>';
            pvtBtn.onclick = () => {
                const name = prompt("Enter Private Group Name:");
                if(name) switchGroup(name, true);
            };
            gl.appendChild(pvtBtn);
            
            connect();
        }

        function renderGroupItem(name, container) {
            const el = document.createElement('div'); 
            el.className = `nav-item ${name===state.group?'active':''}`;
            el.innerHTML = `<span># ${name}</span>`;
            el.onclick = () => switchGroup(name);
            container.appendChild(el);
        }

        async function switchGroup(name, isPrivate = false) {
            if(isPrivate) {
                // Check auth
                const fd = new FormData(); fd.append('name', name);
                const check = await fetch('/api/groups/join_private', {method:'POST', body:fd});
                if(check.status === 403) {
                    state.pendingGroup = name;
                    document.getElementById('auth-target').innerText = name;
                    document.getElementById('auth-modal').style.display = 'flex';
                    return;
                } else if (!check.ok) {
                    return alert("Group not found");
                }
            }
            state.group = name;
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            document.getElementById('header-title').innerText = `# ${name}`;
            connect();
        }

        async function submitAuth() {
            const pass = document.getElementById('auth-pass').value;
            const fd = new FormData(); fd.append('name', state.pendingGroup); fd.append('password', pass);
            const res = await fetch('/api/groups/join_private', {method:'POST', body:fd});
            if(res.ok) {
                closeModal('auth-modal');
                document.getElementById('auth-pass').value = '';
                state.group = state.pendingGroup;
                document.getElementById('header-title').innerText = `# ${state.group} (PvT)`;
                connect();
            } else {
                alert("Access Denied");
            }
        }

        function connect() {
            if(state.ws) state.ws.close();
            document.getElementById('chat-feed').innerHTML = '';
            
            fetch(`/api/history/${state.group}`).then(r=>r.json()).then(m => m.forEach(renderMessage));
            
            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            state.ws = new WebSocket(`${proto}://${location.host}/ws/${state.group}/${state.uid}`);
            state.ws.onopen = () => state.ws.send(JSON.stringify({name: state.user, pfp: state.pfp}));
            state.ws.onmessage = (e) => {
                const d = JSON.parse(e.data);
                if(d.type === "message") renderMessage(d);
                if(d.type === "presence_update") document.getElementById('users-online').innerText = `● ${d.count} Online`;
                if(d.type === "edit_message") {
                    const el = document.querySelector(`[data-id="${d.id}"] .bubble`);
                    if(el) el.innerHTML = marked.parse(d.text) + ' <small style="opacity:0.5; font-size:0.6rem;">(edited)</small>';
                }
                if(d.type === "delete_message") document.querySelector(`[data-id="${d.id}"]`)?.remove();
            };
        }

        function renderMessage(d) {
            const feed = document.getElementById('chat-feed');
            const isMe = d.user_id === state.uid;
            
            const div = document.createElement('div');
            div.className = `msg-row ${isMe ? 'me' : ''}`;
            div.setAttribute('data-id', d.id);
            
            // Context Menu Event
            if(isMe) {
                div.oncontextmenu = (e) => {
                    e.preventDefault();
                    showCtx(e.clientX, e.clientY, d.id, d.text);
                };
                // Mobile Long Press
                let timer;
                div.addEventListener('touchstart', () => timer = setTimeout(()=>showCtx(100, 300, d.id, d.text), 800));
                div.addEventListener('touchend', () => clearTimeout(timer));
            }

            const pfpSrc = d.user_pfp || `https://api.dicebear.com/7.x/identicon/svg?seed=${d.user_id}`;
            
            div.innerHTML = `
                <img src="${pfpSrc}" class="pfp-icon" title="${d.user_name}">
                <div class="msg-content">
                    <div class="msg-info">
                        <span style="font-weight:700; color:${isMe? 'var(--accent)' : '#fff'}">${d.user_name}</span>
                        <span style="opacity:0.5">${new Date(d.timestamp*1000).toLocaleTimeString([], {hour: '2-digit', minute:'2-digit'})}</span>
                    </div>
                    <div class="bubble">${marked.parse(d.text)}${d.edited ? ' <small style="opacity:0.5; font-size:0.6rem;">(edited)</small>' : ''}</div>
                </div>
            `;
            feed.appendChild(div); feed.scrollTop = feed.scrollHeight;
        }

        // --- Context Menu Logic ---
        function showCtx(x, y, id, text) {
            const menu = document.getElementById('ctx-menu');
            menu.style.display = 'block';
            menu.style.left = x + 'px';
            menu.style.top = y + 'px';
            state.ctxTarget = text;
            state.ctxId = id;
        }

        function hideCtx() { document.getElementById('ctx-menu').style.display = 'none'; }

        function handleCtx(action) {
            if(action === 'delete') {
                if(confirm("Delete this transmission?")) state.ws.send(JSON.stringify({type: "delete_message", message_id: state.ctxId}));
            }
            if(action === 'edit') {
                const old = document.querySelector(`[data-id="${state.ctxId}"] .bubble`).innerText.replace('(edited)', '').trim();
                const n = prompt("Edit message:", old);
                if(n && n !== old) state.ws.send(JSON.stringify({type: "edit_message", message_id: state.ctxId, new_text: n}));
            }
            if(action === 'copy') {
                const txt = document.querySelector(`[data-id="${state.ctxId}"] .bubble`).innerText;
                navigator.clipboard.writeText(txt);
            }
            hideCtx();
        }

        // --- Core Functions ---
        function sendMessage() {
            const inp = document.getElementById('msg-input');
            if(!inp.value.trim()) return;
            state.ws.send(JSON.stringify({type: "message", text: inp.value}));
            inp.value = '';
        }

        async function uploadFile(input) {
            const file = input.files[0];
            if(!file) return;
            const fd = new FormData(); fd.append('file', file);
            
            const btn = document.querySelector('.input-wrapper button');
            const originalTxt = btn.innerText;
            btn.innerText = "⏳";
            
            try {
                const r = await fetch('/api/upload', {method:'POST', body: fd});
                const d = await r.json();
                state.ws.send(JSON.stringify({type: "message", text: `![Image](${d.url})`}));
            } catch(e) { alert("Upload Failed"); }
            
            btn.innerText = originalTxt;
            input.value = '';
        }
    </script>
</body>
</html>
"""
