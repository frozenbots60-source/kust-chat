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
# KUSTIFY HYPER-X | V10.0 (EVOLUTION)
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
GROUPS_KEY = "kustify:groups:v10" # Hash: name -> json_meta
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
        if group_id in self.active_connections and websocket in self.active_connections[group_id]:
            self.active_connections[group_id].remove(websocket)
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
    meta = {"private": group.is_private, "password": group.password}
    await redis.hset(GROUPS_KEY, name, json.dumps(meta))
    return {"status": "success", "name": name}

@app.post("/api/upload")
async def upload_file(file: UploadFile = File(...)):
    ext = file.filename.split('.')[-1]
    file_key = f"kustify_v10/{int(time.time())}.{ext}"
    s3_client.upload_fileobj(file.file, AWS_CONFIG["bucket"], file_key, ExtraArgs={'ContentType': file.content_type})
    url = s3_client.generate_presigned_url('get_object', Params={'Bucket': AWS_CONFIG["bucket"], 'Key': file_key}, ExpiresIn=604800)
    return {"url": url}

@app.websocket("/ws/{group_id}/{user_id}")
async def websocket_endpoint(websocket: WebSocket, group_id: str, user_id: str):
    await websocket.accept()
    init = await websocket.receive_json()
    user_info = {"id": user_id, "name": init.get("name", "Anon"), "pfp": init.get("pfp", "")}
    await manager.connect(websocket, group_id, user_info)
    
    try:
        while True:
            data = await websocket.receive_json()
            data["user_id"] = user_id
            data["group_id"] = group_id
            data["timestamp"] = time.time()
            
            if data["type"] == "edit":
                # Handle message editing logic
                await redis.publish(GLOBAL_CHANNEL, json.dumps(data))
            else:
                await redis.publish(GLOBAL_CHANNEL, json.dumps(data))
                if data["type"] == "message":
                    await redis.rpush(f"{HISTORY_KEY}{group_id}", json.dumps(data))
    except WebSocketDisconnect:
        manager.disconnect(websocket, group_id, user_id)

# ==========================================
# FRONTEND (WEB3 TELEGRAM STYLE)
# ==========================================

html_content = """
<!DOCTYPE html>
<html>
<head>
    <title>KUSTIFY HYPER-X V10</title>
    <script src="https://unpkg.com/peerjs@1.4.7/dist/peerjs.min.js"></script>
    <style>
        :root { --p: #7000ff; --s: #00f3ff; --bg: #0a0a0c; --glass: rgba(255,255,255,0.03); }
        body { background: var(--bg); color: #fff; font-family: 'Inter', sans-serif; margin: 0; display: flex; height: 100vh; overflow: hidden; }
        
        #sidebar { width: 320px; border-right: 1px solid rgba(255,255,255,0.1); background: rgba(10,10,12,0.6); backdrop-filter: blur(20px); z-index: 10; }
        #main { flex: 1; display: flex; flex-direction: column; position: relative; }
        
        /* Message Styling */
        .msg { padding: 8px 15px; max-width: 70%; border-radius: 18px; margin-bottom: 4px; position: relative; }
        .msg.me { align-self: flex-end; background: var(--p); border-bottom-right-radius: 4px; }
        .msg.other { align-self: flex-start; background: var(--glass); border-bottom-left-radius: 4px; border: 1px solid rgba(255,255,255,0.05); }
        .edit-btn { font-size: 10px; cursor: pointer; opacity: 0.5; margin-left: 8px; }
        
        /* Preview Overlay */
        #preview-box { position: absolute; bottom: 80px; left: 20px; display: none; background: #111; padding: 10px; border-radius: 12px; border: 1px solid var(--s); }
        #preview-img { max-height: 150px; border-radius: 8px; }

        /* VC UI */
        #vc-overlay { position: fixed; top: 20px; right: 20px; width: 240px; background: rgba(0,0,0,0.8); border-radius: 16px; padding: 15px; display: none; border: 1px solid var(--p); box-shadow: 0 0 20px rgba(112,0,255,0.3); }
        video { width: 100%; border-radius: 8px; background: #000; margin-top: 10px; }
    </style>
</head>
<body>
    <div id="sidebar">
        <div style="padding: 25px; border-bottom: 1px solid rgba(255,255,255,0.05)">
            <h2 style="color: var(--s); letter-spacing: -1px;">KUSTIFY <span style="font-weight: 300; opacity: 0.5;">X10</span></h2>
            <button onclick="showCreateGroup()" style="width:100%; padding:10px; background:var(--glass); border:1px solid rgba(255,255,255,0.1); color:#fff; border-radius:8px; cursor:pointer;">+ New Channel</button>
        </div>
        <div id="groups-list" style="padding: 10px;"></div>
    </div>

    <div id="main">
        <div id="chat-header" style="padding: 15px 25px; display:flex; justify-content:space-between; align-items:center; background: rgba(255,255,255,0.02);">
            <div id="active-group-name"># Lobby</div>
            <div style="display:flex; gap:10px;">
                <button onclick="startCall(false)" class="btn">🎤 Voice</button>
                <button onclick="startCall(true)" class="btn">🖥️ Share</button>
            </div>
        </div>

        <div id="feed" style="flex:1; overflow-y:auto; padding: 25px; display:flex; flex-direction:column;"></div>

        <div id="preview-box">
            <img id="preview-img">
            <button onclick="cancelUpload()" style="display:block; width:100%; color:#ff4444; background:none; border:none; margin-top:5px;">Cancel</button>
        </div>

        <div style="padding: 20px; display:flex; gap:12px; background: rgba(10,10,12,0.8);">
            <input type="file" id="file-in" hidden onchange="showPreview(event)">
            <button onclick="document.getElementById('file-in').click()" style="background:none; border:none; font-size:20px; cursor:pointer;">📎</button>
            <input type="text" id="msg-in" placeholder="Message..." style="flex:1; background:var(--glass); border:1px solid rgba(255,255,255,0.1); padding:12px 20px; border-radius:25px; color:#fff;">
            <button onclick="sendMsg()" style="background:var(--p); border:none; width:45px; height:45px; border-radius:50%; color:white; cursor:pointer;">➤</button>
        </div>
    </div>

    <div id="vc-overlay">
        <div style="font-size:12px; color:var(--s); margin-bottom:10px;">INFRA_LINK: ACTIVE</div>
        <div id="remote-streams"></div>
        <video id="local-video" autoplay muted playsinline></video>
        <button onclick="endCall()" style="width:100%; margin-top:10px; background:#ff4444; border:none; padding:8px; border-radius:8px; color:#fff;">Disconnect</button>
    </div>

    <script>
        let currentFile = null;
        let editingId = null;
        let peer = null;
        let localStream = null;

        function showPreview(e) {
            const file = e.target.files[0];
            if (!file) return;
            currentFile = file;
            const url = URL.createObjectURL(file);
            document.getElementById('preview-img').src = url;
            document.getElementById('preview-box').style.display = 'block';
        }

        async function sendMsg() {
            const text = document.getElementById('msg-in').value;
            const payload = { type: "message", text: text };

            if (editingId) {
                payload.type = "edit";
                payload.id = editingId;
                editingId = null;
            }

            if (currentFile) {
                const fd = new FormData(); fd.append('file', currentFile);
                const res = await fetch('/api/upload', {method:'POST', body:fd}).then(r=>r.json());
                payload.image_url = res.url;
                cancelUpload();
            }

            state.ws.send(JSON.stringify(payload));
            document.getElementById('msg-in').value = '';
        }

        async function startCall(withScreen) {
            document.getElementById('vc-overlay').style.display = 'block';
            localStream = withScreen ? 
                await navigator.mediaDevices.getDisplayMedia({video: true, audio: true}) :
                await navigator.mediaDevices.getUserMedia({audio: true, video: false});
            
            if(withScreen) document.getElementById('local-video').srcObject = localStream;
            
            peer = new Peer(state.uid);
            peer.on('call', call => {
                call.answer(localStream);
                call.on('stream', stream => renderRemoteStream(stream, call.peer));
            });
            state.ws.send(JSON.stringify({type: "vc_join"}));
        }

        function renderRemoteStream(stream, id) {
            if(document.getElementById(`v-${id}`)) return;
            const v = document.createElement('video');
            v.id = `v-${id}`; v.srcObject = stream; v.autoplay = true;
            document.getElementById('remote-streams').appendChild(v);
        }

        function cancelUpload() {
            currentFile = null;
            document.getElementById('preview-box').style.display = 'none';
        }

        // Add context menu for editing
        window.addEventListener('contextmenu', e => {
            const bubble = e.target.closest('.msg.me');
            if(bubble) {
                e.preventDefault();
                editingId = bubble.id;
                document.getElementById('msg-in').value = bubble.querySelector('.text').innerText;
                document.getElementById('msg-in').focus();
            }
        });
    </script>
</body>
</html>
"""

@app.get("/")
async def get():
    return HTMLResponse(html_content)
