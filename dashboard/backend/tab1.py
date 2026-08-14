"""Tab 1 (Manual / 216 case).

Five interactive terminals the user drives by hand, per DEMO_RUNBOOK_FULL.md
-- nothing here runs commands automatically. Reset kills and restarts all
five shells (fresh prompt, cleared scrollback); it does not touch Docker or
run any script itself.

The registry also owns a "master" channel and four more ("fl-server",
"fl-client-loc1/2/3") used by Tab 4 (dashboard/backend/tab4.py) for a real,
manually-triggered Flower FL round -- same shared PTY infrastructure
throughout, just automated there instead of manually typed.

Tab 2 and Tab 3 (dashboard/backend/tab2.py / tab3.py) used to reuse THIS
registry's own "master"/"producer"/"branch-loc1-3" sessions too, which meant
Tab 1, Tab 2, and Tab 3 were all secretly looking at the same three
terminals. They now each get their own independent set (tab2-*/tab3-*
channel names) in agent_flow.py's separate `agent_registry` -- this
registry's "master"/"producer"/"branch-loc1-3" are Tab 1's alone again.
"""
import json
import os
from pathlib import Path

from fastapi import APIRouter, WebSocket, WebSocketDisconnect

from .pty_manager import PtyRegistry

REPO_ROOT = Path(__file__).resolve().parents[2]
VENV_ROOT = REPO_ROOT / ".venv"

CHANNEL_NAMES = [
    "data-gen", "branch-loc1", "branch-loc2", "branch-loc3", "producer", "master",
    "fl-server", "fl-client-loc1", "fl-client-loc2", "fl-client-loc3",
]

_env = os.environ.copy()
_env.setdefault("PYTHONPATH", str(REPO_ROOT))
_env["TERM"] = "xterm-256color"

# Also send the real `source .venv/bin/activate` command after each shell
# starts -- a login shell's own rc files (.zshrc/.zprofile) can re-prepend
# another Python onto PATH, which would silently undo just setting env vars
# up front. Sending the actual command last is what always wins.
_init_commands = []
if (VENV_ROOT / "bin" / "activate").exists():
    _env.pop("PYTHONHOME", None)
    _env["VIRTUAL_ENV"] = str(VENV_ROOT)
    _env["PATH"] = f"{VENV_ROOT / 'bin'}:{_env.get('PATH', '')}"
    _init_commands.append(f"source {VENV_ROOT / 'bin' / 'activate'}\n".encode())

registry = PtyRegistry(CHANNEL_NAMES, cwd=str(REPO_ROOT), env=_env, init_commands=_init_commands)

router = APIRouter(prefix="/api/tab1")
ws_router = APIRouter()


@router.post("/reset")
async def reset():
    registry.restart_all()
    return {"status": "restarted"}


@ws_router.websocket("/ws/tab1/{channel_name}")
async def ws_channel(websocket: WebSocket, channel_name: str):
    if channel_name not in registry.sessions:
        await websocket.close(code=4404)
        return
    await websocket.accept()
    session = registry.get(channel_name)
    if session.master_fd is None:
        session.start()
    if session.scrollback:
        await websocket.send_bytes(bytes(session.scrollback))
    session.subscribers.add(websocket)
    try:
        while True:
            message = await websocket.receive()
            if message["type"] == "websocket.disconnect":
                break
            data = message.get("bytes")
            if data is not None:
                session.write(data)
                continue
            text = message.get("text")
            if text is None:
                continue
            try:
                control = json.loads(text)
            except json.JSONDecodeError:
                continue
            if control.get("type") == "resize":
                session.resize(int(control.get("rows", 24)), int(control.get("cols", 80)))
    except WebSocketDisconnect:
        pass
    finally:
        session.subscribers.discard(websocket)
