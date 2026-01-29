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
# KUSTIFY HYPER-X | V9.2 (VC + MOBILE SPLIT + PERSISTENCE)
# ==========================================

app = FastAPI(
    title="Kustify Hyper-X API", 
    description="Next-Gen Infrastructure Chat API for Bots and Custom Clients", 
    version="9.2",
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
                # Broadcast relevant types to the group
                if mtype in ["message", "edit_message", "delete_message", "vc_signal_group", "vc_user_state"]:
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
    messages = await redis.lrange(f"{HISTORY_KEY}{group_id}", -limit, -1)
    return [json.loads(m) for m in messages]

@app.post("/api/upload", tags=["Files"])
async def upload_file(file: UploadFile = File(...)):
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
        if not pfp: pfp = "https://api.dicebear.com/7.x/identicon/svg?seed=" + user_id
        
        user_info = {"id": user_id, "name": name, "pfp": pfp}
        await manager.connect(websocket, group_id, user_info)
    except: return

    try:
        while True:
            data = await websocket.receive_json()
            mtype = data.get("type")

            if mtype == "message":
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
            
            # --- Voice Chat Signaling ---
            elif mtype in ["vc_join", "vc_leave", "vc_signal"]:
                data.update({"sender_id": user_id, "group_id": group_id})
                # Broadcast signals to group so peers can connect
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
    <script src="https://unpkg.com/peerjs@1.5.1/dist/peerjs.min.js"></script>
    <script src="https://cdnjs.cloudflare.com/ajax/libs/marked/4.0.2/marked.min.js"></script>
    <link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;700&family=Outfit:wght@300;400;600;800&display=swap" rel="stylesheet">
    <style>
        :root { --bg-dark: #050505; --panel: rgba(20, 20, 23, 0.95); --border: rgba(255, 255, 255, 0.08); --primary: #7000ff; --accent: #00f3ff; --text-main: #eeeeee; --text-dim: #888888; --glass: blur(20px) saturate(180%); --radius: 16px; }
        * { margin: 0; padding: 0; box-sizing: border-box; outline: none; }
        body { font-family: 'Outfit', sans-serif; background: var(--bg-dark); color: var(--text-main); height: 100dvh; overflow: hidden; display: flex; }
        
        /* Layout & Mobile Split */
        #sidebar { width: 280px; background: rgba(10, 10, 12, 0.95); border-right: 1px solid var(--border); z-index: 20; display: flex; flex-direction: column; transition: transform 0.3s ease; }
        #main-view { flex: 1; display: flex; flex-direction: column; position: relative; background: #000; }
        
        /* On Mobile: Split View Logic */
        @media (max-width: 768px) {
            body.view-sidebar #sidebar { display: flex; width: 100%; position: relative; transform: none; }
            body.view-sidebar #main-view { display: none; }
            
            body.view-chat #sidebar { display: none; }
            body.view-chat #main-view { display: flex; width: 100%; }
            
            .back-btn { display: block !important; }
        }
        
        .back-btn { display: none; background: none; border: none; color: #fff; font-size: 1.5rem; margin-right: 15px; cursor: pointer; }

        /* Sidebar Styling */
        .brand-area { padding: 24px; font-family: 'JetBrains Mono'; font-weight: 800; border-bottom: 1px solid var(--border); color: var(--primary); display: flex; justify-content: space-between; align-items: center; }
        .nav-list { flex: 1; padding: 15px; overflow-y: auto; }
        .nav-section { font-size: 0.75rem; text-transform: uppercase; color: #555; margin: 15px 0 5px 5px; font-weight: 700; }
        .nav-item { padding: 14px; border-radius: 8px; color: var(--text-dim); cursor: pointer; margin-bottom: 5px; display: flex; justify-content: space-between; align-items: center; background: rgba(255,255,255,0.02); }
        .nav-item:hover { background: rgba(255,255,255,0.05); color: #fff; }
        .nav-item.active { background: rgba(112, 0, 255, 0.15); color: var(--accent); border-left: 3px solid var(--accent); }
        
        /* Chat Feed */
        header { height: 60px; display: flex; justify-content: space-between; align-items: center; padding: 0 15px; border-bottom: 1px solid var(--border); background: rgba(10,10,12,0.9); }
        #chat-feed { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 20px; }
        
        .msg-row { display: flex; gap: 12px; align-items: flex-end; }
        .msg-row.me { flex-direction: row-reverse; }
        .pfp-icon { width: 35px; height: 35px; border-radius: 50%; object-fit: cover; border: 2px solid var(--border); background: #222; flex-shrink: 0; }
        .msg-content { display: flex; flex-direction: column; max-width: 75%; }
        .msg-info { font-size: 0.75rem; color: var(--text-dim); margin-bottom: 4px; display: flex; gap: 8px; }
        .me .msg-info { justify-content: flex-end; }
        .bubble { padding: 12px 16px; border-radius: 12px; background: #1a1a1d; color: #fff; word-wrap: break-word; line-height: 1.5; }
        .me .bubble { background: var(--primary); border-bottom-right-radius: 2px; }
        .msg-row:not(.me) .bubble { border-bottom-left-radius: 2px; }
        .bubble img { max-width: 100%; border-radius: 8px; margin-top: 5px; display: block; }
        
        /* Input & Controls */
        .input-wrapper { padding: 15px; background: #08080a; border-top: 1px solid var(--border); display: flex; gap: 10px; align-items: center; }
        .chat-input-box { flex: 1; background: #151518; border: 1px solid var(--border); color: #fff; padding: 12px 20px; border-radius: 30px; font-family: inherit; }
        .btn-icon { background: none; border: none; color: var(--text-dim); cursor: pointer; padding: 8px; font-size: 1.2rem; transition: 0.2s; }
        .btn-icon:hover { color: var(--accent); }

        /* Voice Chat Controls */
        #vc-controls { display: none; align-items: center; gap: 10px; background: rgba(0, 243, 255, 0.1); padding: 5px 15px; border-radius: 20px; border: 1px solid rgba(0, 243, 255, 0.3); }
        #vc-status { font-size: 0.8rem; color: var(--accent); font-weight: 600; }
        .btn-vc-leave { background: #ff4444; color: white; border: none; border-radius: 50%; width: 30px; height: 30px; cursor: pointer; font-size: 0.8rem; display: flex; align-items: center; justify-content: center; }

        /* Modals & Context */
        .modal { position: fixed; inset: 0; background: rgba(0,0,0,0.9); z-index: 100; display: none; align-items: center; justify-content: center; backdrop-filter: blur(5px); }
        .modal-content { background: #111; border: 1px solid var(--border); padding: 30px; border-radius: 20px; width: 90%; max-width: 400px; text-align: center; }
        .input-std { width: 100%; padding: 15px; background: #050505; border: 1px solid #333; color: #fff; border-radius: 10px; margin-bottom: 15px; }
        .btn-primary { background: var(--primary); color: #fff; border: none; padding: 15px; width: 100%; border-radius: 10px; font-weight: 700; cursor: pointer; }
        .btn-text { background: none; border: none; color: var(--text-dim); margin-top: 10px; cursor: pointer; text-decoration: underline; }
        
        #ctx-menu { position: fixed; background: #222; border: 1px solid var(--border); border-radius: 8px; padding: 5px; display: none; z-index: 1000; box-shadow: 0 10px 30px rgba(0,0,0,0.5); width: 150px; }
        .ctx-item { padding: 10px; cursor: pointer; font-size: 0.9rem; color: #eee; border-radius: 4px; }
        .ctx-item:hover { background: var(--primary); }
        .ctx-item.delete { color: #ff4444; }
    </style>
</head>
<body class="view-sidebar" onclick="hideCtx()">

    <div id="setup-modal" class="modal" style="display: flex;">
        <div class="modal-content">
            <h1 style="font-family: 'JetBrains Mono'; margin-bottom: 20px; color: var(--accent);">IDENTITY_INIT</h1>
            <input type="text" id="username-input" class="input-std" placeholder="Display Name">
            <input type="text" id="pfp-input" class="input-std" placeholder="Avatar URL (Optional)">
            <button class="btn-primary" onclick="saveUser()">CONNECT SYSTEM</button>
        </div>
    </div>

    <div id="create-group-modal" class="modal">
        <div class="modal-content">
            <h3>New Channel</h3><br>
            <input type="text" id="new-group-name" class="input-std" placeholder="Channel Name">
            <select id="new-group-type" class="input-std" onchange="togglePassField()">
                <option value="public">Public</option>
                <option value="private">Private (Hidden)</option>
            </select>
            <input type="password" id="new-group-pass" class="input-std" placeholder="Password" style="display:none;">
            <button class="btn-primary" onclick="createGroup()">CREATE</button>
            <button class="btn-text" onclick="closeModal('create-group-modal')">Cancel</button>
        </div>
    </div>
    
    <div id="auth-modal" class="modal">
        <div class="modal-content">
            <h3>Locked Channel</h3><br>
            <p id="auth-target" style="margin-bottom:15px; color:var(--accent);"></p>
            <input type="password" id="auth-pass" class="input-std" placeholder="Access Code">
            <button class="btn-primary" onclick="submitAuth()">UNLOCK</button>
            <button class="btn-text" onclick="closeModal('auth-modal')">Abort</button>
        </div>
    </div>

    <div id="ctx-menu">
        <div class="ctx-item" onclick="handleCtx('edit')">Edit</div>
        <div class="ctx-item" onclick="handleCtx('copy')">Copy</div>
        <div class="ctx-item delete" onclick="handleCtx('delete')">Delete</div>
    </div>

    <div id="sidebar">
        <div class="brand-area">
            <span>HYPER-X v9.2</span>
            <button class="btn-icon" onclick="openCreateModal()">+</button>
        </div>
        <div class="nav-list" id="group-list"></div>
    </div>

    <div id="main-view">
        <header>
            <div style="display:flex; align-items:center;">
                <button class="back-btn" onclick="showSidebar()">←</button>
                <div>
                    <h3 id="header-title" style="line-height:1;"># Lobby</h3>
                    <div id="users-online" style="font-size:0.7rem; color:var(--text-dim); margin-top:2px;">Connecting...</div>
                </div>
            </div>
            <div style="display: flex; gap: 10px; align-items: center;">
                <div id="vc-controls">
                    <span id="vc-status">🔊 Voice Active</span>
                    <button class="btn-vc-leave" onclick="leaveVC()">✕</button>
                </div>
                <button class="btn-icon" id="btn-join-vc" onclick="joinVC()" title="Join Voice">🎤</button>
            </div>
        </header>
        
        <div id="chat-feed"></div>
        
        <div class="input-wrapper">
            <button class="btn-icon" onclick="document.getElementById('file-input').click()">📎</button>
            <input type="file" id="file-input" hidden onchange="uploadFile(this)">
            <input type="text" id="msg-input" class="chat-input-box" placeholder="Write message..." onkeypress="if(event.key==='Enter') sendMessage()">
            <button class="btn-icon" onclick="sendMessage()" style="color:var(--primary);">➤</button>
        </div>
    </div>

    <script>
        const state = {
            user: getCookie("k_user"),
            uid: getCookie("k_uid") || "u_" + Math.random().toString(36).substr(2, 9),
            pfp: getCookie("k_pfp") || "",
            group: "Lobby", 
            ws: null,
            ctxTarget: null, ctxId: null,
            peer: null,
            myPeerId: null,
            vcCalls: {},
            inVC: false,
            joinedPvtGroups: JSON.parse(localStorage.getItem('k_joined_groups') || '[]')
        };

        function setCookie(n, v) { const d = new Date(); d.setTime(d.getTime() + (365*24*60*60*1000)); document.cookie = `${n}=${v};expires=${d.toUTCString()};path=/`; }
        function getCookie(n) { const v = document.cookie.match('(^|;) ?' + n + '=([^;]*)(;|$)'); return v ? v[2] : null; }

        // Mobile Nav Logic
        function showChat() { document.body.classList.remove('view-sidebar'); document.body.classList.add('view-chat'); }
        function showSidebar() { document.body.classList.remove('view-chat'); document.body.classList.add('view-sidebar'); }

        window.onload = () => {
            if(state.user) { 
                document.getElementById('setup-modal').style.display = 'none';
                init();
            }
        };

        // --- Auth & Setup ---
        function saveUser() {
            const n = document.getElementById('username-input').value.trim();
            const p = document.getElementById('pfp-input').value.trim();
            if(!n) return;
            state.user = n; state.pfp = p;
            setCookie("k_user", n); setCookie("k_uid", state.uid); setCookie("k_pfp", p);
            document.getElementById('setup-modal').style.display = 'none';
            init();
        }

        function togglePassField() {
            const type = document.getElementById('new-group-type').value;
            document.getElementById('new-group-pass').style.display = type === 'private' ? 'block' : 'none';
        }
        function openCreateModal() { document.getElementById('create-group-modal').style.display = 'flex'; }
        function closeModal(id) { document.getElementById(id).style.display = 'none'; }

        // --- Group Logic ---
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
                if(type === 'private') rememberPrivateGroup(name);
                init(); // Refresh list
                switchGroup(name);
            } else alert("Error: " + (await res.json()).detail);
        }

        function rememberPrivateGroup(name) {
            if(!state.joinedPvtGroups.includes(name)) {
                state.joinedPvtGroups.push(name);
                localStorage.setItem('k_joined_groups', JSON.stringify(state.joinedPvtGroups));
            }
        }

        async function init() {
            const r = await fetch('/api/groups'); 
            const d = await r.json();
            const gl = document.getElementById('group-list');
            gl.innerHTML = '';
            
            // Public Groups
            const pubHeader = document.createElement('div'); pubHeader.className='nav-section'; pubHeader.innerText='Public';
            gl.appendChild(pubHeader);
            d.groups.forEach(g => renderGroupItem(g, gl));

            // Private Groups (Persistent)
            if(state.joinedPvtGroups.length > 0) {
                const pvtHeader = document.createElement('div'); pvtHeader.className='nav-section'; pvtHeader.innerText='Private';
                gl.appendChild(pvtHeader);
                state.joinedPvtGroups.forEach(g => renderGroupItem(g, gl, true));
            }

            // Join Btn
            const joinBtn = document.createElement('div');
            joinBtn.className = 'nav-item';
            joinBtn.style.justifyContent = 'center';
            joinBtn.innerHTML = '<small>+ Join Private</small>';
            joinBtn.onclick = () => {
                const name = prompt("Enter Private Group Name:");
                if(name) switchGroup(name, true);
            };
            gl.appendChild(joinBtn);

            // Connect to current group
            connect();
        }

        function renderGroupItem(name, container, isPrivate=false) {
            const el = document.createElement('div'); 
            el.className = `nav-item ${name===state.group?'active':''}`;
            el.innerHTML = `<span># ${name}</span> ${isPrivate?'🔒':''}`;
            el.onclick = () => switchGroup(name, isPrivate);
            container.appendChild(el);
        }

        async function switchGroup(name, isPrivate = false) {
            if(state.inVC) leaveVC(); // Auto leave VC on switch

            // Logic to determine if we need auth (simplified: if persistent, assume auth'd or check again)
            // Ideally backend validates token, here we simulate check
            if(isPrivate || !document.querySelector(`.nav-item`)) { 
                // Check if valid private group
                const fd = new FormData(); fd.append('name', name);
                const check = await fetch('/api/groups/join_private', {method:'POST', body:fd});
                if(check.status === 403) {
                    state.pendingGroup = name;
                    document.getElementById('auth-target').innerText = name;
                    document.getElementById('auth-modal').style.display = 'flex';
                    return;
                } else if(!check.ok) {
                     return alert("Group not found"); 
                }
                // If success, ensure it's remembered
                rememberPrivateGroup(name);
            }

            state.group = name;
            document.querySelectorAll('.nav-item').forEach(el => el.classList.remove('active'));
            // Re-render list to highlight active
            init(); 
            
            document.getElementById('header-title').innerText = `# ${name}`;
            showChat(); // Mobile transition
            connect();
        }

        async function submitAuth() {
            const pass = document.getElementById('auth-pass').value;
            const fd = new FormData(); fd.append('name', state.pendingGroup); fd.append('password', pass);
            const res = await fetch('/api/groups/join_private', {method:'POST', body:fd});
            if(res.ok) {
                closeModal('auth-modal');
                document.getElementById('auth-pass').value = '';
                rememberPrivateGroup(state.pendingGroup);
                switchGroup(state.pendingGroup);
            } else alert("Access Denied");
        }

        // --- WebSocket & Chat ---
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
                if(d.type === "vc_signal_group") handleVCSignal(d);
            };
        }

        function renderMessage(d) {
            const feed = document.getElementById('chat-feed');
            const isMe = d.user_id === state.uid;
            const div = document.createElement('div');
            div.className = `msg-row ${isMe ? 'me' : ''}`;
            div.setAttribute('data-id', d.id);
            
            if(isMe) {
                div.oncontextmenu = (e) => { e.preventDefault(); showCtx(e.clientX, e.clientY, d.id, d.text); };
                let timer;
                div.addEventListener('touchstart', () => timer = setTimeout(()=>showCtx(100, 300, d.id, d.text), 800));
                div.addEventListener('touchend', () => clearTimeout(timer));
            }

            const pfpSrc = d.user_pfp || `https://api.dicebear.com/7.x/identicon/svg?seed=${d.user_id}`;
            div.innerHTML = `
                <img src="${pfpSrc}" class="pfp-icon">
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
            try {
                const r = await fetch('/api/upload', {method:'POST', body: fd});
                const d = await r.json();
                state.ws.send(JSON.stringify({type: "message", text: `![Image](${d.url})`}));
            } catch(e) { alert("Upload Failed"); }
        }

        // --- Voice Chat System (PeerJS) ---
        function joinVC() {
            if(state.inVC) return;
            state.peer = new Peer(undefined); // Using PeerJS Cloud
            
            state.peer.on('open', (id) => {
                state.myPeerId = id;
                state.inVC = true;
                document.getElementById('vc-controls').style.display = 'flex';
                document.getElementById('btn-join-vc').style.display = 'none';
                
                // Get Local Stream
                navigator.mediaDevices.getUserMedia({audio: true, video: false}).then(stream => {
                    state.localStream = stream;
                    // Announce Join
                    state.ws.send(JSON.stringify({type: "vc_join", peer_id: id}));
                    
                    // Answer Incoming
                    state.peer.on('call', call => {
                        call.answer(stream);
                        handleStream(call);
                    });
                }).catch(e => { alert("Mic Access Denied"); leaveVC(); });
            });
        }

        function leaveVC() {
            if(!state.inVC) return;
            state.ws.send(JSON.stringify({type: "vc_leave", peer_id: state.myPeerId}));
            if(state.peer) state.peer.destroy();
            if(state.localStream) state.localStream.getTracks().forEach(t => t.stop());
            state.inVC = false;
            state.vcCalls = {};
            document.getElementById('vc-controls').style.display = 'none';
            document.getElementById('btn-join-vc').style.display = 'block';
        }

        function handleVCSignal(d) {
            if(!state.inVC || d.sender_id === state.uid) return;
            
            if(d.type === "vc_signal_group" && d.vc_type === "join") {
                // Someone joined, call them
                // Note: In strict logic, we check d.type from socket. 
                // We mapped WS 'vc_join' -> 'vc_signal_group' with 'vc_type' implicit in data or separate field
                // Re-aligning with backend: backend sends type="vc_signal_group", payload includes original fields
            }
            
            // If someone new joined (d.peer_id present) and isn't me
            if(d.peer_id && d.type === "vc_signal_group") { 
                // Using backend reflection: WS sends "vc_join" -> Backend publishes "vc_signal_group" + "peer_id"
                // Check original intent from payload
                // Actually, let's just use the `d` object directly.
                
                if(d.peer_id && !state.vcCalls[d.peer_id]) {
                     // Connect to new peer
                     const call = state.peer.call(d.peer_id, state.localStream);
                     handleStream(call);
                }
            }
        }

        function handleStream(call) {
            state.vcCalls[call.peer] = call;
            call.on('stream', userStream => {
                const audio = document.createElement('audio');
                audio.srcObject = userStream;
                audio.play();
                // Store audio ref if needed to remove later
            });
            call.on('close', () => { delete state.vcCalls[call.peer]; });
        }

        // --- Context Menu ---
        function showCtx(x, y, id, text) {
            const menu = document.getElementById('ctx-menu');
            menu.style.display = 'block'; menu.style.left = x + 'px'; menu.style.top = y + 'px';
            state.ctxTarget = text; state.ctxId = id;
        }
        function hideCtx() { document.getElementById('ctx-menu').style.display = 'none'; }
        function handleCtx(a) {
            if(a==='delete' && confirm("Delete?")) state.ws.send(JSON.stringify({type: "delete_message", message_id: state.ctxId}));
            if(a==='edit') {
                const n = prompt("Edit:", state.ctxTarget);
                if(n) state.ws.send(JSON.stringify({type: "edit_message", message_id: state.ctxId, new_text: n}));
            }
            if(a==='copy') navigator.clipboard.writeText(state.ctxTarget);
            hideCtx();
        }
    </script>
</body>
</html>
"""
