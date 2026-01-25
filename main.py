import os
import json
import asyncio
import boto3
from typing import List, Dict, Optional, Set
from fastapi import FastAPI, WebSocket, WebSocketDisconnect, UploadFile, File
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel
from redis import asyncio as aioredis

app = FastAPI(title="Kustify Ultra", description="Premium Animated Chat", version="7.0")

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

# Initialize Redis
redis = aioredis.from_url(REDIS_URL, decode_responses=True)

# Initialize S3
s3_client = boto3.client(
    's3',
    aws_access_key_id=AWS_ACCESS_KEY,
    aws_secret_access_key=AWS_SECRET_KEY,
    region_name=AWS_REGION
)

GLOBAL_CHANNEL = "kustify_global_v7"
GROUPS_KEY = "kustify:groups_v7"
# Key pattern for VC participants: kustify:vc:{group_id}

# --- MODELS ---

class GroupCreate(BaseModel):
    name: str

# --- CONNECTION MANAGER ---

class ConnectionManager:
    def __init__(self):
        self.active_connections: Dict[str, List[WebSocket]] = {}

    async def connect(self, websocket: WebSocket, group_id: str):
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
            for connection in self.active_connections[group_id].copy():
                try:
                    await connection.send_text(data)
                except Exception:
                    pass

manager = ConnectionManager()

# --- BACKGROUND WORKER ---

async def redis_listener():
    pubsub = redis.pubsub()
    await pubsub.subscribe(GLOBAL_CHANNEL)
    async for message in pubsub.listen():
        if message["type"] == "message":
            try:
                data = json.loads(message["data"])
                group_id = data.get("group_id")
                if group_id:
                    await manager.broadcast(group_id, data)
            except:
                pass

@app.on_event("startup")
async def startup_event():
    if not await redis.sismember(GROUPS_KEY, "General"):
        await redis.sadd(GROUPS_KEY, "General")
    asyncio.create_task(redis_listener())

# --- API ENDPOINTS ---

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
        file_key = f"kustify_v7/{file.filename}"
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

# --- WEBSOCKET ENDPOINT ---

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    await websocket.accept()
    await manager.connect(websocket, group_id)
    
    # Track if this specific socket connection is in VC
    in_vc = False
    
    try:
        while True:
            raw = await websocket.receive_text()
            data = json.loads(raw)
            mtype = data.get("type")

            if mtype == "heartbeat":
                await websocket.send_text(json.dumps({"type": "heartbeat_ack"}))
                continue

            # --- CHAT MESSAGES ---
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
                await redis.rpush(f"kustify:history:{group_id}", json.dumps(out))
                await redis.publish(GLOBAL_CHANNEL, json.dumps(out))

            # --- NEW ROBUST VC LOGIC ---
            
            elif mtype == "vc_join":
                # 1. Add user to Redis Set of participants
                vc_key = f"kustify:vc:{group_id}"
                user_info = json.dumps({
                    "id": user_id,
                    "name": data.get("user_name"),
                    "pfp": data.get("user_pfp")
                })
                # Store user info mapped by ID for easy retrieval if needed, 
                # but for simple sets we just store the JSON string.
                # To avoid duplicates if info changes, we might want to clean up, 
                # but simple Set add is fine for this scope.
                await redis.sadd(vc_key, user_info)
                in_vc = True
                
                # 2. Fetch ALL current participants
                participants = await redis.smembers(vc_key)
                parsed_participants = [json.loads(p) for p in participants]
                
                # 3. Broadcast the UPDATED LIST to EVERYONE in the group
                # This forces everyone to sync their UI and connections
                update_msg = {
                    "type": "vc_state_update",
                    "group_id": group_id,
                    "participants": parsed_participants
                }
                await manager.broadcast(group_id, update_msg)

            elif mtype == "vc_leave":
                vc_key = f"kustify:vc:{group_id}"
                # We need to remove the specific user entry. 
                # Since we stored JSON, we need to find and remove it.
                # A simpler way for removal is to scan or just broadcast the ID to remove.
                # But to keep State correct, let's remove.
                members = await redis.smembers(vc_key)
                for m in members:
                    m_json = json.loads(m)
                    if m_json["id"] == user_id:
                        await redis.srem(vc_key, m)
                        break
                
                in_vc = False
                
                # Broadcast removal
                await manager.broadcast(group_id, {
                    "type": "vc_user_left",
                    "group_id": group_id,
                    "user_id": user_id
                })

            # Pass-through signals (WebRTC Offer/Answer/ICE)
            elif mtype == "vc_signal":
                # Direct signal to a specific target
                target_id = data.get("target_id")
                data["sender_id"] = user_id
                # We broadcast to group, clients filter by target_id
                await manager.broadcast(group_id, data)

            elif mtype in ["vc_talking", "vc_update"]:
                data["sender_id"] = user_id
                await manager.broadcast(group_id, data)

    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id)
        # Auto-cleanup VC if they disconnected while in it
        if in_vc:
            vc_key = f"kustify:vc:{group_id}"
            members = await redis.smembers(vc_key)
            for m in members:
                try:
                    m_json = json.loads(m)
                    if m_json["id"] == user_id:
                        await redis.srem(vc_key, m)
                        break
                except:
                    pass
            
            # Notify others
            await manager.broadcast(group_id, {
                "type": "vc_user_left",
                "group_id": group_id,
                "user_id": user_id
            })
            
    except Exception as e:
        manager.disconnect(websocket, group_id)

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
            --border: rgba(255, 255, 255, 0.1);
            --msg-me: linear-gradient(135deg, #7c3aed, #db2777);
            --msg-other: #27272a;
        }

        * { margin: 0; padding: 0; box-sizing: border-box; outline: none; -webkit-tap-highlight-color: transparent; }
        
        body {
            font-family: 'Outfit', sans-serif;
            background: var(--bg-dark);
            color: #fafafa; height: 100vh; display: flex; overflow: hidden;
        }

        #sidebar {
            width: 300px; background: var(--bg-glass); backdrop-filter: blur(20px);
            border-right: 1px solid var(--border); display: flex; flex-direction: column;
            z-index: 50; transition: transform 0.3s ease;
        }

        .brand { padding: 25px; font-size: 1.8rem; font-weight: 800; border-bottom: 1px solid var(--border); }
        
        .user-profile-widget {
            padding: 15px; border-bottom: 1px solid var(--border); display: flex; align-items: center; gap: 10px;
        }
        .my-pfp { width: 40px; height: 40px; border-radius: 50%; object-fit: cover; border: 2px solid var(--primary); }

        .group-list { flex: 1; padding: 15px; overflow-y: auto; }
        .group-item {
            padding: 14px 18px; margin-bottom: 8px; border-radius: 12px; cursor: pointer; color: #a1a1aa;
            transition: 0.2s;
        }
        .group-item.active { background: rgba(139, 92, 246, 0.15); color: var(--primary); }

        .create-btn {
            margin: 15px; padding: 12px; border-radius: 12px; border: 1px dashed var(--border);
            color: #a1a1aa; cursor: pointer; text-align: center;
        }

        #chat-area { flex: 1; display: flex; flex-direction: column; position: relative; }
        #chat-header {
            padding: 15px 25px; background: rgba(9, 9, 11, 0.8); backdrop-filter: blur(10px);
            border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center;
        }

        .vc-btn {
            padding: 10px 20px; border-radius: 30px; border: none; font-weight: 600; cursor: pointer;
            background: linear-gradient(135deg, #10b981, #059669); color: white; display: flex; align-items: center; gap: 8px;
        }

        #messages { flex: 1; overflow-y: auto; padding: 20px; display: flex; flex-direction: column; gap: 15px; }
        .message { max-width: 75%; display: flex; gap: 12px; }
        .msg-own { align-self: flex-end; flex-direction: row-reverse; }
        .msg-content { padding: 12px 18px; border-radius: 18px; font-size: 0.95rem; line-height: 1.5; }
        .msg-own .msg-content { background: var(--msg-me); }
        .msg-other .msg-content { background: var(--msg-other); }
        .msg-avatar { width: 35px; height: 35px; border-radius: 50%; object-fit: cover; }
        .msg-img { max-width: 100%; border-radius: 12px; margin-top: 10px; cursor: pointer; }

        #input-area {
            padding: 20px; background: rgba(9, 9, 11, 0.9); border-top: 1px solid var(--border);
            display: flex; gap: 12px; align-items: center;
        }
        #msg-input { flex: 1; background: #18181b; border: 1px solid var(--border); padding: 14px 24px; border-radius: 30px; color: white; }
        .icon-btn { width: 48px; height: 48px; border-radius: 50%; border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; }
        .send-btn { background: var(--primary); color: white; }

        /* VC WIDGET */
        #vc-widget {
            position: fixed; top: 80px; right: 20px; width: 320px;
            background: rgba(24, 24, 27, 0.95); backdrop-filter: blur(15px);
            border: 1px solid var(--border); border-radius: 20px;
            box-shadow: 0 10px 40px rgba(0,0,0,0.5); z-index: 90;
            display: none; flex-direction: column;
        }
        .vc-header { padding: 15px; border-bottom: 1px solid var(--border); display: flex; justify-content: space-between; align-items: center; cursor: move; }
        .vc-grid { display: grid; grid-template-columns: repeat(3, 1fr); gap: 10px; padding: 15px; max-height: 300px; overflow-y: auto; }
        .vc-user { display: flex; flex-direction: column; align-items: center; gap: 5px; }
        .vc-avatar-container { position: relative; width: 60px; height: 60px; display: flex; justify-content: center; align-items: center; }
        .vc-avatar { width: 50px; height: 50px; border-radius: 50%; object-fit: cover; z-index: 2; border: 2px solid #27272a; }
        .vc-ring { position: absolute; width: 50px; height: 50px; border-radius: 50%; background: var(--primary); opacity: 0; z-index: 1; transition: 0.1s; }
        .vc-mic-status { position: absolute; bottom: 0; right: 0; width: 18px; height: 18px; background: #ef4444; border-radius: 50%; display: flex; justify-content: center; align-items: center; font-size: 8px; z-index: 3; }
        .vc-controls { padding: 15px; border-top: 1px solid var(--border); display: flex; justify-content: center; gap: 15px; }
        .vc-circle-btn { width: 40px; height: 40px; border-radius: 50%; border: none; cursor: pointer; display: flex; align-items: center; justify-content: center; }

        /* Modal */
        .modal-overlay { position: fixed; top: 0; left: 0; width: 100%; height: 100%; background: rgba(0,0,0,0.8); z-index: 100; display: none; justify-content: center; align-items: center; }
        .modal-box { background: #18181b; width: 90%; max-width: 400px; padding: 30px; border-radius: 24px; text-align: center; }
        .modal-box input { width: 100%; padding: 12px; margin: 20px 0; background: #09090b; border: 1px solid var(--border); color: white; border-radius: 8px; }
        
        @media (max-width: 768px) {
            #sidebar { position: fixed; height: 100%; transform: translateX(-100%); width: 80%; }
            #sidebar.open { transform: translateX(0); }
            #vc-widget { width: 90%; top: 60px; right: 5%; }
        }
    </style>
</head>
<body>
    <div id="setup-modal" class="modal-overlay" style="display: flex;">
        <div class="modal-box">
            <h2>Kustify</h2>
            <p style="color:#a1a1aa">Set up your profile</p>
            <div style="margin:20px auto; width:100px; height:100px; position:relative;">
                <img id="preview-pfp" src="https://ui-avatars.com/api/?background=random" style="width:100%; height:100%; border-radius:50%; object-fit:cover;">
                <label for="pfp-upload" style="position:absolute; bottom:0; right:0; background:var(--primary); width:32px; height:32px; border-radius:50%; display:flex; justify-content:center; align-items:center; cursor:pointer;">📷</label>
                <input type="file" id="pfp-upload" hidden onchange="uploadPfp()">
            </div>
            <input type="text" id="username-input" placeholder="Display Name">
            <button onclick="saveProfile()" style="padding:12px; width:100%; background:var(--primary); border:none; color:white; border-radius:8px; font-weight:bold;">Enter</button>
        </div>
    </div>

    <div id="vc-widget">
        <div class="vc-header" id="vc-header">
            <div><span style="color:#34d399">●</span> Voice Chat</div>
        </div>
        <div class="vc-grid" id="vc-grid"></div>
        <div class="vc-controls">
            <button class="vc-circle-btn" onclick="toggleMute()" id="mute-btn" style="background:#27272a">🎤</button>
            <button class="vc-circle-btn" onclick="leaveVoice()" style="background:#ef4444">❌</button>
        </div>
    </div>

    <div id="sidebar">
        <div class="brand">Kustify <span onclick="toggleSidebar()" style="float:right; cursor:pointer; font-size:1rem">✕</span></div>
        <div class="user-profile-widget"><img id="side-pfp" class="my-pfp"> <span id="side-name"></span></div>
        <div class="group-list" id="group-list"></div>
        <div class="create-btn" onclick="createGroup()">+ Create New Room</div>
    </div>

    <div id="chat-area">
        <div id="chat-header">
            <div><button onclick="toggleSidebar()" style="background:none; border:none; color:white; font-size:1.5rem; margin-right:10px;">☰</button> <span id="header-title"># General</span></div>
            <button class="vc-btn" onclick="joinVoice()">🎤 Join Voice</button>
        </div>
        <div id="messages"></div>
        <div id="input-area">
            <button class="icon-btn" onclick="document.getElementById('file-input').click()">+</button>
            <input type="file" id="file-input" hidden onchange="handleFile()">
            <input type="text" id="msg-input" placeholder="Type a message...">
            <button class="icon-btn send-btn" onclick="sendMessage()">➤</button>
        </div>
    </div>

    <div id="audio-container" hidden></div>

    <script>
        // STATE
        const state = {
            user: localStorage.getItem("k_user") || "",
            uid: localStorage.getItem("k_uid") || "u_" + Math.random().toString(36).substr(2),
            pfp: localStorage.getItem("k_pfp") || `https://ui-avatars.com/api/?background=random&name=User`,
            group: "General",
            ws: null,
            hb: null
        };
        localStorage.setItem("k_uid", state.uid);

        // VC VARIABLES
        let peer = null;
        let myStream = null;
        let activeCalls = {}; // Stores PeerJS calls
        let joinedVc = false;
        let audioCtx, analyser, dataArray, micLoop;
        let isMuted = false;

        window.onload = () => {
            if(state.user) {
                document.getElementById('setup-modal').style.display='none';
                updateProfileUI(); initApp();
            } else { document.getElementById('preview-pfp').src = state.pfp; }
            dragElement(document.getElementById("vc-widget"));
        };

        async function uploadPfp() {
            const f = document.getElementById('pfp-upload').files[0];
            if(!f) return;
            const form = new FormData(); form.append('file', f);
            try {
                const r = await fetch('/api/upload', {method:'POST', body:form});
                const d = await r.json(); state.pfp = d.url;
                document.getElementById('preview-pfp').src = state.pfp;
            } catch(e) { alert("Fail"); }
        }

        function saveProfile() {
            const n = document.getElementById('username-input').value.trim();
            if(!n) return;
            state.user = n; localStorage.setItem("k_user", n); localStorage.setItem("k_pfp", state.pfp);
            document.getElementById('setup-modal').style.display='none';
            updateProfileUI(); initApp();
        }
        function updateProfileUI() {
            document.getElementById('side-pfp').src = state.pfp;
            document.getElementById('side-name').innerText = state.user;
        }
        function toggleSidebar() { document.getElementById('sidebar').classList.toggle('open'); }

        async function initApp() { loadGroups(); connect(state.group); }
        async function loadGroups() {
            const r = await fetch('/api/groups'); const d = await r.json();
            const l = document.getElementById('group-list'); l.innerHTML='';
            d.groups.forEach(g => {
                const div = document.createElement('div'); div.className = `group-item ${g===state.group?'active':''}`;
                div.innerText = `# ${g}`; div.onclick=()=>{toggleSidebar(); connect(g);};
                l.appendChild(div);
            });
        }
        async function createGroup() {
            const n = prompt("Name:"); if(n) {
                await fetch('/api/groups', {method:'POST', headers:{'Content-Type':'application/json'}, body:JSON.stringify({name:n})});
                loadGroups();
            }
        }

        // CHAT
        function connect(group) {
            if(state.ws) state.ws.close();
            if(state.hb) clearInterval(state.hb);
            leaveVoice();

            state.group = group; document.getElementById('header-title').innerText = `# ${group}`;
            document.getElementById('messages').innerHTML = ''; loadGroups();

            fetch(`/api/history/${group}`).then(r=>r.json()).then(msgs => msgs.forEach(renderMsg));

            const proto = location.protocol === 'https:' ? 'wss' : 'ws';
            state.ws = new WebSocket(`${proto}://${location.host}/ws/${group}/${state.uid}`);

            state.ws.onopen = () => { state.hb = setInterval(() => state.ws.send(JSON.stringify({type:"heartbeat"})), 30000); };
            state.ws.onmessage = (e) => {
                const d = JSON.parse(e.data);
                if(d.type === "message") renderMsg(d);
                if(d.type === "vc_state_update") handleVcState(d.participants);
                if(d.type === "vc_user_left") removeVcUser(d.user_id);
                if(d.type === "vc_talking") animateRing(d.sender_id, d.vol);
                if(d.type === "vc_update") updateMicIcon(d.sender_id, d.muted);
                if(d.type === "vc_signal" && d.target_id === state.uid) handleSignal(d);
            };
        }

        function renderMsg(d) {
            const b = document.getElementById('messages');
            const isMe = d.user_id === state.uid;
            const div = document.createElement('div'); div.className = `message ${isMe?'msg-own':'msg-other'}`;
            div.innerHTML = `
                <img class="msg-avatar" src="${d.user_pfp}">
                <div class="msg-content">
                    <div class="msg-meta">${d.user_name}</div>
                    ${d.text?`<div>${d.text}</div>`:''}
                    ${d.image_url?`<img src="${d.image_url}" class="msg-img" onclick="window.open(this.src)">`:''}
                </div>
            `;
            b.appendChild(div); b.scrollTop = b.scrollHeight;
        }

        function sendMessage() {
            const i = document.getElementById('msg-input'); const t = i.value.trim();
            if(!t) return;
            state.ws.send(JSON.stringify({type:"message", user_name:state.user, user_pfp:state.pfp, text:t, timestamp:Date.now()}));
            i.value='';
        }
        document.getElementById('msg-input').addEventListener('keypress', e=>{if(e.key==='Enter')sendMessage();});
        async function handleFile() {
            const f = document.getElementById('file-input').files[0]; if(!f) return;
            const fm = new FormData(); fm.append('file', f);
            try {
                const r = await fetch('/api/upload', {method:'POST', body:fm}); const d = await r.json();
                state.ws.send(JSON.stringify({type:"message", user_name:state.user, user_pfp:state.pfp, text:"", image_url:d.url, timestamp:Date.now()}));
            } catch(e){} document.getElementById('file-input').value='';
        }

        // --- NEW SERVER-AUTHORITATIVE VC LOGIC ---

        function joinVoice() {
            navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true, noiseSuppression:true}, video:false}).then(stream => {
                myStream = stream; joinedVc = true;
                
                document.getElementById('vc-widget').style.display='flex';
                
                // Audio Viz
                audioCtx = new (window.AudioContext||window.webkitAudioContext)();
                const src = audioCtx.createMediaStreamSource(stream);
                analyser = audioCtx.createAnalyser(); analyser.fftSize = 64;
                src.connect(analyser); dataArray = new Uint8Array(analyser.frequencyBinCount);
                startMicLoop();

                // Init Peer
                peer = new Peer(state.uid);
                
                peer.on('open', (id) => {
                    // Tell server we joined. Server will send back current list of users.
                    state.ws.send(JSON.stringify({
                        type: "vc_join",
                        user_name: state.user,
                        user_pfp: state.pfp
                    }));
                });

                // Answer calls from others who join AFTER me
                peer.on('call', call => {
                    call.answer(stream);
                    handleCallStream(call);
                });

            }).catch(e => alert("Mic Error"));
        }

        // Called when server sends the full participant list
        function handleVcState(participants) {
            if(!joinedVc) return;

            participants.forEach(p => {
                // Ensure UI exists
                addVcUserToUI(p.id, p.name, p.pfp);

                // If it's not me, and I'm not already connected, CALL THEM
                if(p.id !== state.uid && !activeCalls[p.id]) {
                    console.log("Calling", p.id);
                    const call = peer.call(p.id, myStream);
                    handleCallStream(call);
                    activeCalls[p.id] = call;
                }
            });
        }

        function handleCallStream(call) {
            call.on('stream', remoteStream => {
                // Play audio
                let aud = document.getElementById(`audio-${call.peer}`);
                if(!aud) {
                    aud = document.createElement('audio');
                    aud.id = `audio-${call.peer}`;
                    aud.autoplay = true;
                    document.getElementById('audio-container').appendChild(aud);
                }
                aud.srcObject = remoteStream;
                aud.play().catch(console.error);
            });
        }

        // We don't really use this explicitly anymore since state sync handles it, 
        // but it's good for fallback signaling if needed.
        function handleSignal(d) {
            // pass
        }

        function leaveVoice() {
            if(!joinedVc) return;
            joinedVc = false;
            
            // Tell server
            if(state.ws) state.ws.send(JSON.stringify({type: "vc_leave"}));

            // Cleanup
            if(peer) peer.destroy();
            if(myStream) myStream.getTracks().forEach(t=>t.stop());
            if(audioCtx) audioCtx.close();
            clearInterval(micLoop);

            document.getElementById('vc-widget').style.display='none';
            document.getElementById('vc-grid').innerHTML='';
            document.getElementById('audio-container').innerHTML='';
            activeCalls = {};
        }

        function addVcUserToUI(uid, name, pfp) {
            if(document.getElementById(`vc-u-${uid}`)) return;
            const grid = document.getElementById('vc-grid');
            const div = document.createElement('div');
            div.className = 'vc-user'; div.id = `vc-u-${uid}`;
            div.innerHTML = `
                <div class="vc-avatar-container">
                    <div class="vc-ring" id="ring-${uid}"></div>
                    <img class="vc-avatar" src="${pfp}">
                    <div class="vc-mic-status" id="mic-${uid}" style="display:none;">✕</div>
                </div>
                <div style="font-size:0.75rem;">${name.substr(0,8)}</div>
            `;
            grid.appendChild(div);
        }

        function removeVcUser(uid) {
            const el = document.getElementById(`vc-u-${uid}`); if(el) el.remove();
            const aud = document.getElementById(`audio-${uid}`); if(aud) aud.remove();
            if(activeCalls[uid]) { activeCalls[uid].close(); delete activeCalls[uid]; }
        }

        function startMicLoop() {
            micLoop = setInterval(() => {
                if(!analyser || isMuted) return;
                analyser.getByteFrequencyData(dataArray);
                let sum=0; for(let x of dataArray) sum+=x;
                const avg = sum/dataArray.length;
                
                animateRing(state.uid, avg);
                if(avg>10) state.ws.send(JSON.stringify({type:"vc_talking", vol:avg}));
            }, 100);
        }

        function animateRing(uid, vol) {
            const el = document.getElementById(`ring-${uid}`);
            if(el) {
                const s = 1 + (vol/100);
                el.style.transform = `translate(-50%,-50%) scale(${s})`;
                el.style.opacity = vol>5 ? 0.8 : 0;
            }
        }

        function toggleMute() {
            isMuted = !isMuted; myStream.getAudioTracks()[0].enabled = !isMuted;
            document.getElementById('mute-btn').style.background = isMuted?'white':'#27272a';
            document.getElementById('mute-btn').style.color = isMuted?'black':'white';
            updateMicIcon(state.uid, isMuted);
            state.ws.send(JSON.stringify({type:"vc_update", muted:isMuted}));
        }

        function updateMicIcon(uid, muted) {
            const el = document.getElementById(`mic-${uid}`);
            if(el) el.style.display = muted ? 'flex' : 'none';
        }

        function dragElement(elmnt) {
            var pos1=0,pos2=0,pos3=0,pos4=0;
            const hdr = document.getElementById("vc-header");
            if(hdr) { hdr.onmousedown = dragMouseDown; hdr.ontouchstart = dragMouseDown; }
            function dragMouseDown(e) {
                e = e || window.event;
                if(e.type==='touchstart'){pos3=e.touches[0].clientX;pos4=e.touches[0].clientY;}
                else{pos3=e.clientX;pos4=e.clientY;}
                document.onmouseup=closeDrag;document.onmousemove=drag;
                document.ontouchend=closeDrag;document.ontouchmove=drag;
            }
            function drag(e) {
                e=e||window.event; e.preventDefault();
                let cx,cy;
                if(e.type==='touchmove'){cx=e.touches[0].clientX;cy=e.touches[0].clientY;}
                else{cx=e.clientX;cy=e.clientY;}
                pos1=pos3-cx; pos2=pos4-cy; pos3=cx; pos4=cy;
                elmnt.style.top=(elmnt.offsetTop-pos2)+"px"; elmnt.style.left=(elmnt.offsetLeft-pos1)+"px";
            }
            function closeDrag() { document.onmouseup=null;document.onmousemove=null;document.ontouchend=null;document.ontouchmove=null; }
        }
    </script>
</body>
</html>
"""
