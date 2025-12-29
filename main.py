import csv
import json
import random
import string
import time
import uuid
from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware

# -----------------------------
# Config
# -----------------------------
QUESTIONS_CSV_PATH = "questions.csv"
MAX_QUESTIONS = 30
QUESTION_TIME_SECONDS = 120
POINTS_PER_QUESTION = 1

AVATARS = ["🦊", "🐼", "🐸", "🐵", "🐯", "🐙", "🐧", "🦄"]
OFFLINE_GRACE_SECONDS = 120  # Spieler "offline" behalten

app = FastAPI(title="Quiz Multiplayer (FastAPI + WebSockets)")

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)

# -----------------------------
# Data Models
# -----------------------------
@dataclass
class Question:
    text: str
    correct: str
    wrong: List[str]


@dataclass
class Player:
    token: str
    name: str
    avatar: str
    score: int = 0
    ws: Optional[WebSocket] = None
    connected: bool = False
    last_seen: float = field(default_factory=lambda: time.time())

    # Joker pro Spiel (je 1x)
    joker_5050: bool = True
    joker_spy: bool = True
    joker_risk: bool = True

    # pro Frage transient
    selected_choice: Optional[int] = None  # 0..3
    used_risk_this_q: bool = False
    used_spy_this_q: bool = False


@dataclass
class RoomState:
    code: str
    host_token: str
    created_at: float = field(default_factory=lambda: time.time())
    players: Dict[str, Player] = field(default_factory=dict)

    phase: str = "lobby"  # lobby | question | reveal | finished
    question_index: int = -1
    question_order: List[int] = field(default_factory=list)

    current_q: Optional[Dict] = None  # {text, choices[4], correct_index}
    q_deadline_ts: Optional[float] = None

    joker_used_this_q: bool = False
    live_picks: Dict[str, Optional[int]] = field(default_factory=dict)

    question_closed: bool = False     # keine Antworten/Joker mehr
    reveal_data: Optional[Dict] = None  # {correct_index, picks_by_choice}


rooms: Dict[str, RoomState] = {}
questions: List[Question] = []


# -----------------------------
# Helpers
# -----------------------------
def load_questions_from_csv(path: str) -> List[Question]:
    out: List[Question] = []
    with open(path, "r", encoding="utf-8") as f:
        reader = csv.reader(f, delimiter=";")
        header = next(reader, None)
        if not header:
            raise RuntimeError("CSV is empty")
        for row in reader:
            if len(row) < 5:
                continue
            out.append(
                Question(
                    text=row[0].strip(),
                    correct=row[1].strip(),
                    wrong=[row[2].strip(), row[3].strip(), row[4].strip()],
                )
            )
    return out[:MAX_QUESTIONS]


def make_room_code() -> str:
    return "".join(random.choice(string.ascii_uppercase + string.digits) for _ in range(5))

def points_for_round(round_number_1_based: int) -> int:
    if 1 <= round_number_1_based <= 4:
        return 1
    if 5 <= round_number_1_based <= 9:
        return 2
    if 10 <= round_number_1_based <= 20:
        return 4
    if 21 <= round_number_1_based <= 25:
        return 6
    if 26 <= round_number_1_based <= 30:
        return 10
    return 1

def build_question_payload(q: Question) -> Dict:
    choices = q.wrong[:] + [q.correct]
    random.shuffle(choices)
    correct_index = choices.index(q.correct)
    return {"text": q.text, "choices": choices, "correct_index": correct_index}


def public_player_view(p: Player) -> Dict:
    return {
        "token": p.token,
        "name": p.name,
        "avatar": p.avatar,
        "score": p.score,
        "connected": p.connected,
        "answered": p.selected_choice is not None,
        "joker_5050": p.joker_5050,
        "joker_spy": p.joker_spy,
        "joker_risk": p.joker_risk,
    }


def room_snapshot(room: RoomState) -> Dict:
    players_sorted = sorted(room.players.values(), key=lambda x: x.score, reverse=True)
    current_round = max(0, room.question_index + 1)
    q_points = points_for_round(current_round) if current_round > 0 else 0
    return {
        "code": room.code,
        "phase": room.phase,
        "host_token": room.host_token,
        "question_index": room.question_index,
        "q_deadline_ts": room.q_deadline_ts,
        "joker_used_this_q": room.joker_used_this_q,
        "question_closed": room.question_closed,
        "reveal_data": room.reveal_data,
        "players": [public_player_view(p) for p in players_sorted],
        "current_q_public": {
            "text": room.current_q["text"],
            "choices": room.current_q["choices"],
        } if room.current_q and room.phase in ("question", "reveal") else None,
        "avatars": AVATARS,
        "question_points": q_points,
    }


async def ws_send_safe(ws: Optional[WebSocket], payload: Dict):
    if ws is None:
        return
    try:
        await ws.send_text(json.dumps(payload, ensure_ascii=False))
    except Exception:
        pass


async def broadcast_room(room: RoomState, payload: Dict, only_tokens: Optional[Set[str]] = None):
    for token, p in list(room.players.items()):
        if only_tokens is not None and token not in only_tokens:
            continue
        await ws_send_safe(p.ws, payload)


def cleanup_offline_players(room: RoomState):
    now = time.time()
    to_remove = []
    for token, p in room.players.items():
        if not p.connected and (now - p.last_seen) > OFFLINE_GRACE_SECONDS:
            to_remove.append(token)
    for token in to_remove:
        room.players.pop(token, None)
        room.live_picks.pop(token, None)


async def sleep_ms(ms: int):
    import asyncio
    await asyncio.sleep(ms / 1000.0)


async def push_spy_updates(room: RoomState, only_to: Optional[Set[str]] = None):
    # build picks_by_choice like reveal: {0: [{name,avatar,token}], 1: [...], ...}
    picks_by_choice = {0: [], 1: [], 2: [], 3: []}
    for p in room.players.values():
        pick = room.live_picks.get(p.token)
        if pick is None:
            continue
        picks_by_choice[pick].append({
            "name": p.name,
            "avatar": p.avatar,
            "token": p.token,
        })

    spy_tokens = {p.token for p in room.players.values() if p.used_spy_this_q}
    if only_to is not None:
        spy_tokens = spy_tokens.intersection(only_to)
    if not spy_tokens:
        return

    await broadcast_room(
        room,
        {"type": "spy:update", "picks_by_choice": picks_by_choice},
        only_tokens=spy_tokens
    )

    lines = []
    for p in room.players.values():
        pick = room.live_picks.get(p.token)
        lines.append(f"{p.avatar} {p.name} → {fmt_choice(pick)}")

    spy_tokens = {p.token for p in room.players.values() if p.used_spy_this_q}
    if only_to is not None:
        spy_tokens = spy_tokens.intersection(only_to)
    if not spy_tokens:
        return

    await broadcast_room(room, {"type": "spy:update", "lines": lines}, only_tokens=spy_tokens)


# -----------------------------
# Game Flow
# -----------------------------
async def start_game(room: RoomState):
    room.phase = "question"
    room.question_order = list(range(min(len(questions), MAX_QUESTIONS)))
    random.shuffle(room.question_order)
    room.question_index = -1

    # Reset game-wide state
    for p in room.players.values():
        p.score = 0
        p.joker_5050 = True
        p.joker_spy = True
        p.joker_risk = True

    await next_question(room)


async def next_question(room: RoomState):
    room.question_index += 1
    room.joker_used_this_q = False
    room.question_closed = False
    room.reveal_data = None

    room.current_q = None
    room.q_deadline_ts = None
    room.live_picks = {}

    for p in room.players.values():
        p.selected_choice = None
        p.used_risk_this_q = False
        p.used_spy_this_q = False
        room.live_picks[p.token] = None

    if room.question_index >= min(len(room.question_order), MAX_QUESTIONS):
        room.phase = "finished"
        await broadcast_room(room, {"type": "room:update", "room": room_snapshot(room)})
        return

    q_idx = room.question_order[room.question_index]
    q = questions[q_idx]
    room.current_q = build_question_payload(q)
    room.phase = "question"
    room.q_deadline_ts = time.time() + QUESTION_TIME_SECONDS

    await broadcast_room(room, {"type": "room:update", "room": room_snapshot(room)})

    # Timer loop: schließen wenn alle geantwortet ODER Zeit um
    while True:
        if room.phase != "question":
            return

        now = time.time()
        all_answered = all(p.selected_choice is not None for p in room.players.values()) if room.players else False

        if all_answered or (room.q_deadline_ts is not None and now >= room.q_deadline_ts):
            room.question_closed = True
            await broadcast_room(room, {"type": "room:update", "room": room_snapshot(room)})
            return

        await broadcast_room(room, {"type": "tick", "now": now, "deadline": room.q_deadline_ts})
        await sleep_ms(350)


# -----------------------------
# Web UI (Dark Mode permanent)
# -----------------------------
INDEX_HTML = """
<!doctype html>
<html lang="de">
<head>
  <meta charset="utf-8"/>
  <meta name="viewport" content="width=device-width,initial-scale=1"/>
  <title>Quiz Multiplayer</title>
  <style>
    :root{
      --bg:#0b0e14;
      --panel:#111827;
      --panel2:#0f172a;
      --border:#243042;
      --text:#e6e6e6;
      --muted:#aab0bf;
      --btn:#151f33;
      --btnBorder:#2f3c52;
      --accent:#7c3aed;  /* lila vibe */
      --good:#3ddc84;
      --warn:#ffd166;
    }

    *{ box-sizing:border-box; }
    html,body{ height:100%; }
    body{
      margin:0;
      background: radial-gradient(1200px 600px at 50% 0%, rgba(124,58,237,0.25), transparent 60%), var(--bg);
      color:var(--text);
      font-family: system-ui, -apple-system, Segoe UI, Roboto, sans-serif;
    }

    /* ---- App Layout ---- */
    .app{
      height:100vh;
      display:flex;
      flex-direction:column;
    }

    .topbar{
      height:64px;
      display:flex;
      align-items:center;
      justify-content:space-between;
      padding: 0 18px;
      border-bottom:1px solid rgba(255,255,255,0.06);
      background: linear-gradient(180deg, rgba(17,24,39,0.85), rgba(17,24,39,0.55));
      backdrop-filter: blur(10px);
    }
    .brand{
      display:flex;
      align-items:center;
      gap:12px;
      font-weight:800;
      letter-spacing:0.3px;
    }
    .brand .logo{
      width:38px; height:38px;
      border-radius:12px;
      background: rgba(124,58,237,0.25);
      border:1px solid rgba(124,58,237,0.35);
      display:flex; align-items:center; justify-content:center;
      font-weight:900;
      color:#d8c7ff;
    }

    .topbar-right{
      display:flex;
      align-items:center;
      gap:10px;
    }

    .pill{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding: 8px 12px;
      border-radius: 999px;
      border:1px solid rgba(255,255,255,0.10);
      background: rgba(15,23,42,0.65);
      font-size: 12px;
      color: var(--muted);
      white-space:nowrap;
    }
    .pill strong{ color: var(--text); font-weight:700; }

    .jokerbar{
      display:flex;
      align-items:center;
      gap:10px;
    }
    .jokerbtn{
      display:inline-flex;
      align-items:center;
      gap:8px;
      padding: 10px 14px;
      border-radius: 999px;
      border:1px solid rgba(255,255,255,0.12);
      background: rgba(15,23,42,0.45);
      color: var(--text);
      cursor:pointer;
      font-weight:700;
      font-size: 13px;
    }
    .jokerbtn:hover{ filter: brightness(1.08); }
    .jokerbtn:disabled{
      opacity:0.35;
      cursor:not-allowed;
      filter:none;
    }

    .main{
      flex:1;
      display:grid;
      grid-template-columns: 320px 1fr 360px; /* left | game | scoreboard */
      gap:16px;
      padding: 16px;
      overflow:hidden;
    }

    .panel{
      border:1px solid rgba(255,255,255,0.08);
      background: rgba(17,24,39,0.65);
      backdrop-filter: blur(10px);
      border-radius: 18px;
      padding:14px;
      overflow:hidden;
      min-height:0;
    }

    /* Left connect panel */
    .connect h3{ margin:0 0 10px 0; font-size:16px; }
    .label{ font-size:12px; color: var(--muted); margin-top:10px; }
    input, select{
      width:100%;
      margin-top:6px;
      padding:10px 12px;
      border-radius: 12px;
      border:1px solid rgba(255,255,255,0.12);
      background: rgba(11,14,20,0.55);
      color: var(--text);
      outline:none;
    }
    .row{ display:flex; gap:10px; margin-top:12px; }
    .btn{
      flex:1;
      padding: 10px 12px;
      border-radius: 12px;
      border:1px solid rgba(255,255,255,0.12);
      background: rgba(15,23,42,0.65);
      color: var(--text);
      cursor:pointer;
      font-weight:700;
    }
    .btn:hover{ filter: brightness(1.08); }
    .btn:disabled{ opacity:0.45; cursor:not-allowed; filter:none; }
    .status{ margin-top:10px; color: var(--muted); font-size:13px; }

    /* Center game */
    .game{
      display:flex;
      flex-direction:column;
      gap:14px;
      min-width:0;
    }
    .gameheader{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
    }
    .phase{
      padding: 6px 10px;
      border-radius:999px;
      border:1px solid rgba(255,255,255,0.12);
      background: rgba(15,23,42,0.55);
      color: var(--muted);
      font-size:12px;
    }

    .questionCard{
      border:1px solid rgba(124,58,237,0.25);
      background: rgba(15,23,42,0.55);
      border-radius: 18px;
      padding: 18px;
      text-align:center;
      font-size: 22px;
      font-weight:800;
      line-height:1.25;
      min-height: 92px;
      display:flex;
      align-items:center;
      justify-content:center;
    }

    .answers{
      display:grid;
      grid-template-columns: 1fr 1fr;
      gap:14px;
    }

    .answerBtn{
      border-radius: 16px;
      border:1px solid rgba(255,255,255,0.12);
      background: rgba(17,24,39,0.55);
      padding: 16px;
      text-align:left;
      cursor:pointer;
      color: var(--text);
      font-weight:800;
      min-height: 72px;
      position:relative;
      overflow:hidden;
    }
    .answerBtn:hover{ filter: brightness(1.07); }
    .answerBtn:disabled{ opacity:0.35; cursor:not-allowed; filter:none; }
    .answerBtn.correct{
      border-color: rgba(61,220,132,0.9) !important;
      box-shadow: 0 0 0 2px rgba(61,220,132,0.18) inset;
    }
    .pickedLine{
      margin-top:10px;
      color: var(--muted);
      font-size: 12px;
      font-weight:600;
    }
    .closedInfo{
      color: var(--warn);
      font-weight:700;
      margin-top: 2px;
      font-size: 13px;
    }

    .hostbar{
      display:flex;
      gap:10px;
      flex-wrap:wrap;
      margin-top: 4px;
    }
    .hostbar .btn{ flex:unset; min-width: 150px; }

    /* Right scoreboard */
    .scoreboard h3{ margin:0 0 10px 0; font-size:16px; }
    .sbList{
      list-style:none;
      padding:0; margin:0;
      display:flex;
      flex-direction:column;
      gap:10px;
      overflow:auto;
      max-height: calc(100vh - 64px - 32px);
    }
    .sbItem{
      display:flex;
      align-items:center;
      justify-content:space-between;
      gap:10px;
      padding: 10px 12px;
      border-radius: 14px;
      border:1px solid rgba(255,255,255,0.10);
      background: rgba(15,23,42,0.45);
      cursor:pointer;
    }
    .sbLeft{
      display:flex;
      align-items:center;
      gap:10px;
      min-width:0;
    }
    .sbAvatar{
      width:34px; height:34px;
      border-radius: 12px;
      display:flex;
      align-items:center;
      justify-content:center;
      background: rgba(124,58,237,0.18);
      border:1px solid rgba(124,58,237,0.25);
      font-size:18px;
    }
    .sbName{
      font-weight:800;
      overflow:hidden;
      text-overflow:ellipsis;
      white-space:nowrap;
      max-width: 170px;
    }
    .sbMeta{ color: var(--muted); font-size: 12px; }
    .sbScore{ font-weight:900; }
    .tag{
      font-size:11px;
      padding: 3px 8px;
      border-radius: 999px;
      border:1px solid rgba(255,255,255,0.12);
      color: var(--muted);
    }
    .tag.ok{ border-color: rgba(61,220,132,0.45); color: rgba(61,220,132,0.95); }
    .tag.bad{ border-color: rgba(255,92,92,0.45); color: rgba(255,92,92,0.95); }
  </style>
</head>
<body>
<div class="app">

  <div class="topbar">
    <div class="brand">
      <div class="logo">Q</div>
      <div>Quiz Multiplayer</div>
    </div>

    <div class="jokerbar">
      <button class="jokerbtn" id="j5050">🌓 50/50</button>
      <button class="jokerbtn" id="jspy">🕵️ SPY</button>
      <button class="jokerbtn" id="jrisk">🎲 RISK</button>
    </div>

    <div class="topbar-right">
      <div class="pill"><strong id="roundPill">Round 0</strong></div>
      <div class="pill"><strong id="pointsPill">0 Points</strong></div>
    </div>
  </div>

  <div class="main">
    <!-- Left: connect stays always -->
    <div class="panel connect">
      <h3>Verbinden</h3>

      <div class="label">Name</div>
      <input id="name" placeholder="Name" />

      <div class="label">Avatar</div>
      <select id="avatar"></select>

      <div class="label">Room Code</div>
      <input id="code" placeholder="z.B. ABC12" />

      <div class="row">
        <button class="btn" id="create">Room erstellen</button>
        <button class="btn" id="join">Join</button>
      </div>

      <div class="status" id="status">nicht verbunden</div>
      <div class="status" style="font-size:12px;">Reconnect: Token wird im Browser gespeichert.</div>

      <div style="margin-top:14px; color:var(--muted); font-size:12px;">
        <div><b>Spy View</b></div>
        <div id="spyview" style="margin-top:6px;"></div>
      </div>
    </div>

    <!-- Center: game -->
    <div class="panel game">
      <div class="gameheader">
        <div class="phase" id="phase">lobby</div>
        <div class="phase" id="timer"></div>
      </div>

      <div class="questionCard" id="question">Warte auf Spielstart…</div>

      <div class="answers" id="choices"></div>

      <div class="closedInfo" id="closedInfo"></div>

      <div class="hostbar">
        <button class="btn" id="start">Start (Host)</button>
        <button class="btn" id="reveal">Auswerten (Host)</button>
        <button class="btn" id="next">Nächste Frage (Host)</button>
        <button class="btn" id="kick">Kick (Host)</button>
      </div>

      <div class="status" style="margin-top:4px;">
        Kick: Spieler im Scoreboard anklicken.
      </div>
    </div>

    <!-- Right: scoreboard -->
    <div class="panel scoreboard">
      <h3>Scoreboard</h3>
      <ul class="sbList" id="scoreboard"></ul>
    </div>
  </div>

</div>

<script>
let ws = null;
let room = null;
let myToken = localStorage.getItem("player_token") || "";
let selectedKickToken = null;
let hiddenIndices = new Set();
let spyActive = false;
let lastRoomCode = "";
let lastQuestionIndex = -999;
let spyPicks = null; // {0:[],1:[],2:[],3:[]}

function qs(id){ return document.getElementById(id); }
function setStatus(s){ qs("status").textContent = s; }

function wsUrl(code){
  const proto = location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${location.host}/ws/${code}`;
}

function connect(code, isCreate){
  lastRoomCode = code;
  if (ws){ try{ ws.close(); }catch(e){} }
  setStatus("verbinde...");
  ws = new WebSocket(wsUrl(code));

  ws.onopen = () => {
    setStatus("verbunden");
    const name = (qs("name").value.trim() || (isCreate ? "Host" : "Player"));
    const avatar = qs("avatar").value;
    ws.send(JSON.stringify({ type:"hello", token: myToken, name, avatar, create: !!isCreate }));
  };

  ws.onclose = () => {
    setStatus("getrennt – reconnect...");
    setTimeout(() => {
      if (lastRoomCode) connect(lastRoomCode, false);
    }, 1200);
  };

  ws.onmessage = (ev) => {
    const msg = JSON.parse(ev.data);

    if (msg.type === "hello:ok"){
      myToken = msg.token;
      localStorage.setItem("player_token", myToken);
      qs("code").value = msg.room_code;
      lastRoomCode = msg.room_code;
    }

    if (msg.type === "room:update"){
      room = msg.room;

      if (room && room.question_index !== lastQuestionIndex){
        lastQuestionIndex = room.question_index;
        hiddenIndices = new Set();
        spyActive = false;
        qs("spyview").textContent = "";
        qs("timer").textContent = "";
        qs("closedInfo").textContent = "";
      }

      render(room);
    }

    if (msg.type === "tick"){
      if (msg.deadline){
        const left = Math.max(0, Math.ceil(msg.deadline - msg.now));
        qs("timer").textContent = left > 0 ? `⏱ ${left}s` : "";
      }
    }

    if (msg.type === "joker:5050"){
      hiddenIndices = new Set(msg.hide_indices);
      render(room);
    }

    if (msg.type === "spy:update") {
      if (spyActive) {
        spyPicks = msg.picks_by_choice || null;
        render(room);
      }
    }

    if (msg.type === "error"){
      alert(msg.message);
    }
  };
}

function send(type, payload={}){
  if (!ws || ws.readyState !== 1) return;
  ws.send(JSON.stringify(Object.assign({type}, payload)));
}

function choose(i){ send("answer:submit", { choice: i }); }

function selectKick(token){
  selectedKickToken = token;
  qs("kick").textContent = "Kick (Host) → " + token.slice(0,8);
}

function getMe(r){
  if (!r || !r.players) return null;
  return r.players.find(p => p.token === myToken) || null;
}

function render(r){
  if (!r) return;

  qs("phase").textContent = r.phase;

  // round + points (simple: points = score of you; round = question_index+1)
  const me = getMe(r);
  qs("roundPill").textContent = `Round ${Math.max(0, r.question_index + 1)}`;
  qs("pointsPill").textContent = `${r.question_points || 0} Points`;

  // Joker grey-out after usage (per player + global lock + closed)
  const canUseJokers = (r.phase === "question" && !r.question_closed && !r.joker_used_this_q);
  qs("j5050").disabled = !(canUseJokers && me && me.joker_5050);
  qs("jspy").disabled = !(canUseJokers && me && me.joker_spy);
  qs("jrisk").disabled = !(canUseJokers && me && me.joker_risk);

  // closed info
  if (r.phase === "question" && r.question_closed){
    qs("closedInfo").textContent = "Antworten geschlossen – Host muss auswerten.";
  } else {
    qs("closedInfo").textContent = "";
  }

  const amHost = (r.host_token === myToken);
  qs("start").disabled = !amHost;
  qs("reveal").disabled = !(amHost && r.phase === "question" && r.question_closed);
  qs("next").disabled = !(amHost && r.phase === "reveal");
  qs("kick").disabled = !amHost || !selectedKickToken;

  // question
  if (r.current_q_public){
    qs("question").textContent = r.current_q_public.text;

    const choices = r.current_q_public.choices;
    const reveal = r.reveal_data; // correct_index + picks_by_choice
    const correctIndex = (reveal && typeof reveal.correct_index === "number") ? reveal.correct_index : null;

    const html = choices.map((c, i) => {
      const hidden = hiddenIndices.has(i);
      const disabled = (r.phase !== "question") || r.question_closed || hidden;

      const isCorrect = (r.phase === "reveal" && correctIndex === i);
      const cls = isCorrect ? "answerBtn correct" : "answerBtn";

      let picksHtml = "";

      if (r.phase === "reveal" && reveal && reveal.picks_by_choice) {
        const picks = reveal.picks_by_choice[i] || [];
        if (picks.length) {
          picksHtml =
            "<div class='pickedLine'>" +
            picks.map(p => `${p.avatar} ${p.name}`).join(" • ") +
            "</div>";
        }
      }
      
      if (r.phase === "question" && spyActive && spyPicks) {
        const picks = spyPicks[i] || [];
        if (picks.length) {
          picksHtml =
            "<div class='pickedLine'>" +
            picks.map(p => `${p.avatar} ${p.name}`).join(" • ") +
            "</div>";
        }
      }

      const label = hidden ? "—" : `${String.fromCharCode(65+i)}: ${c}`;
      return `<button class="${cls}" ${disabled ? "disabled":""} onclick="choose(${i})">${label}${picksHtml}</button>`;
    }).join("");

    qs("choices").innerHTML = html;
  } else {
    qs("question").textContent = "Warte auf Spielstart…";
    qs("choices").innerHTML = "";
  }

  // scoreboard
  const sb = r.players.map(p => {
    const status = p.connected ? "ok" : "bad";
    const ans = p.answered ? "✅" : "";
    const you = (p.token === myToken) ? "du" : "";
    return `
      <li class="sbItem" onclick="selectKick('${p.token}')">
        <div class="sbLeft">
          <div class="sbAvatar">${p.avatar}</div>
          <div style="min-width:0">
            <div class="sbName">${p.name}</div>
            <div class="sbMeta">${ans} <span class="tag ${status}">${p.connected ? "online" : "offline"}</span> ${you ? `<span class="tag">${you}</span>` : ""}</div>
          </div>
        </div>
        <div class="sbScore">${p.score}</div>
      </li>
    `;
  }).join("");

  qs("scoreboard").innerHTML = sb;
}

// Buttons
qs("create").onclick = () => connect("NEW", true);
qs("join").onclick = () => {
  const code = (qs("code").value.trim() || "").toUpperCase();
  if (!code) return alert("Room Code fehlt");
  connect(code, false);
};

qs("start").onclick = () => send("game:start", {});
qs("reveal").onclick = () => send("host:reveal", {});
qs("next").onclick = () => send("host:next", {});
qs("kick").onclick = () => {
  if (!selectedKickToken) return;
  send("player:kick", { target_token: selectedKickToken });
};

qs("j5050").onclick = () => send("joker:5050", {});
qs("jspy").onclick = () => { spyActive = true; send("joker:spy", {}); };
qs("jrisk").onclick = () => send("joker:risk", {});

// init avatars
const sel = qs("avatar");
const AVATARS = __AVATARS__;
AVATARS.forEach(a => {
  const opt = document.createElement("option");
  opt.value = a; opt.textContent = a;
  sel.appendChild(opt);
});
sel.value = sel.options[0].value;
</script>
</body>
</html>
""".replace("__AVATARS__", json.dumps(AVATARS, ensure_ascii=False))




@app.get("/", response_class=HTMLResponse)
def index():
    return INDEX_HTML


# -----------------------------
# WebSocket Protocol
# -----------------------------
@app.websocket("/ws/{room_code}")
async def ws_room(ws: WebSocket, room_code: str):
    await ws.accept()

    current_room: Optional[RoomState] = None
    me: Optional[Player] = None

    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            mtype = msg.get("type")

            # HELLO / CONNECT / RECONNECT
            if mtype == "hello":
                create = bool(msg.get("create", False))
                name = (msg.get("name") or "Player").strip()[:24]
                avatar = msg.get("avatar") or AVATARS[0]
                if avatar not in AVATARS:
                    avatar = AVATARS[0]

                token = (msg.get("token") or "").strip()

                # Create new room
                if create:
                    code = make_room_code()
                    host_token = token or str(uuid.uuid4())
                    room = RoomState(code=code, host_token=host_token)
                    rooms[code] = room
                    current_room = room

                    p = room.players.get(host_token)
                    if not p:
                        p = Player(token=host_token, name=name, avatar=avatar)
                        room.players[host_token] = p

                    p.ws = ws
                    p.connected = True
                    p.last_seen = time.time()
                    me = p

                    await ws_send_safe(ws, {"type": "hello:ok", "token": host_token, "room_code": code})
                    await broadcast_room(room, {"type": "room:update", "room": room_snapshot(room)})
                    continue

                # Join existing room
                code = room_code.upper()
                if code == "NEW":
                    await ws_send_safe(ws, {"type": "error", "message": "Zum Join bitte einen echten Room Code eingeben."})
                    continue

                room = rooms.get(code)
                if not room:
                    await ws_send_safe(ws, {"type": "error", "message": "Room nicht gefunden."})
                    continue
                current_room = room

                # Reconnect or new player
                if token and token in room.players:
                    p = room.players[token]
                    p.name = name
                    p.avatar = avatar
                else:
                    token = str(uuid.uuid4())
                    p = Player(token=token, name=name, avatar=avatar)
                    room.players[token] = p

                p.ws = ws
                p.connected = True
                p.last_seen = time.time()
                me = p

                await ws_send_safe(ws, {"type": "hello:ok", "token": token, "room_code": code})
                await broadcast_room(room, {"type": "room:update", "room": room_snapshot(room)})
                continue

            # needs room + me
            if current_room is None or me is None:
                await ws_send_safe(ws, {"type": "error", "message": "Nicht initialisiert. Sende zuerst hello."})
                continue

            room = current_room

            # keepalive (optional)
            if mtype == "ping":
                me.last_seen = time.time()
                await ws_send_safe(ws, {"type": "pong"})
                continue

            # HOST: start game
            if mtype == "game:start":
                if me.token != room.host_token:
                    await ws_send_safe(ws, {"type": "error", "message": "Nur der Host kann starten."})
                    continue
                if not room.players:
                    await ws_send_safe(ws, {"type": "error", "message": "Keine Spieler im Room."})
                    continue
                import asyncio
                asyncio.create_task(start_game(room))
                continue

            # HOST: kick
            if mtype == "player:kick":
                if me.token != room.host_token:
                    await ws_send_safe(ws, {"type": "error", "message": "Nur der Host kann kicken."})
                    continue
                target = msg.get("target_token")
                if not target or target not in room.players:
                    await ws_send_safe(ws, {"type": "error", "message": "Ziel nicht gefunden."})
                    continue
                if target == room.host_token:
                    await ws_send_safe(ws, {"type": "error", "message": "Host kann nicht gekickt werden."})
                    continue

                target_p = room.players[target]
                await ws_send_safe(target_p.ws, {"type": "error", "message": "Du wurdest vom Host gekickt."})
                try:
                    if target_p.ws:
                        await target_p.ws.close()
                except Exception:
                    pass

                room.players.pop(target, None)
                room.live_picks.pop(target, None)
                await broadcast_room(room, {"type": "room:update", "room": room_snapshot(room)})
                continue

            # HOST: reveal (Auswertung anzeigen + scoring)
            if mtype == "host:reveal":
                if me.token != room.host_token:
                    await ws_send_safe(ws, {"type": "error", "message": "Nur der Host kann auswerten."})
                    continue
                if room.phase != "question" or not room.current_q:
                    await ws_send_safe(ws, {"type": "error", "message": "Gerade keine aktive Frage."})
                    continue
                if not room.question_closed:
                    await ws_send_safe(ws, {"type": "error", "message": "Warte bis alle geantwortet haben oder Zeit abgelaufen ist."})
                    continue

                picks_by_choice = {0: [], 1: [], 2: [], 3: []}
                for p in room.players.values():
                    if p.selected_choice is not None:
                        picks_by_choice[p.selected_choice].append({
                            "name": p.name,
                            "avatar": p.avatar,
                            "token": p.token,
                        })

                correct_index = room.current_q["correct_index"]
                round_no = room.question_index + 1
                q_points = points_for_round(round_no)

                # scoring happens once, here
                for p in room.players.values():
                  if p.selected_choice is None:
                      continue
                  is_correct = (p.selected_choice == correct_index)

                  if p.used_risk_this_q:
                      if is_correct:
                          p.score += 2 * q_points
                      else:
                          p.score -= q_points
                  else:
                      if is_correct:
                          p.score += q_points

                room.phase = "reveal"
                room.reveal_data = {
                    "correct_index": correct_index,
                    "picks_by_choice": picks_by_choice
                }

                await broadcast_room(room, {"type": "room:update", "room": room_snapshot(room)})
                continue

            # HOST: next question
            if mtype == "host:next":
                if me.token != room.host_token:
                    await ws_send_safe(ws, {"type": "error", "message": "Nur der Host kann fortfahren."})
                    continue
                if room.phase != "reveal":
                    await ws_send_safe(ws, {"type": "error", "message": "Erst auswerten, dann nächste Frage."})
                    continue
                import asyncio
                asyncio.create_task(next_question(room))
                continue

            # Answer submit
            if mtype == "answer:submit":
                if room.phase != "question" or not room.current_q:
                    await ws_send_safe(ws, {"type": "error", "message": "Gerade keine aktive Frage."})
                    continue
                if room.question_closed:
                    await ws_send_safe(ws, {"type": "error", "message": "Antworten sind geschlossen."})
                    continue

                choice = msg.get("choice")
                if not isinstance(choice, int) or choice < 0 or choice > 3:
                    await ws_send_safe(ws, {"type": "error", "message": "Ungültige Antwort."})
                    continue

                # accept first answer only
                if me.selected_choice is None:
                    me.selected_choice = choice
                    room.live_picks[me.token] = choice

                    await push_spy_updates(room)
                    await broadcast_room(room, {"type": "room:update", "room": room_snapshot(room)})
                continue

            # Jokers
            if isinstance(mtype, str) and mtype.startswith("joker:"):
                if room.phase != "question" or not room.current_q:
                    await ws_send_safe(ws, {"type": "error", "message": "Joker nur während einer Frage."})
                    continue
                if room.question_closed:
                    await ws_send_safe(ws, {"type": "error", "message": "Joker sind geschlossen."})
                    continue
                if room.joker_used_this_q:
                    await ws_send_safe(ws, {"type": "error", "message": "Diese Frage wurde bereits ein Joker genutzt."})
                    continue

                # 50/50
                if mtype == "joker:5050":
                    if not me.joker_5050:
                        await ws_send_safe(ws, {"type": "error", "message": "50/50 Joker bereits verbraucht."})
                        continue
                    correct_index = room.current_q["correct_index"]
                    wrong_indices = [i for i in range(4) if i != correct_index]
                    hide = random.sample(wrong_indices, 2)

                    me.joker_5050 = False
                    room.joker_used_this_q = True
                    await ws_send_safe(ws, {"type": "joker:5050", "hide_indices": hide})
                    await broadcast_room(room, {"type": "room:update", "room": room_snapshot(room)})
                    continue

                # Spy
                if mtype == "joker:spy":
                    if not me.joker_spy:
                        await ws_send_safe(ws, {"type": "error", "message": "Spy Joker bereits verbraucht."})
                        continue
                    me.joker_spy = False
                    me.used_spy_this_q = True
                    room.joker_used_this_q = True

                    await push_spy_updates(room, only_to={me.token})
                    await broadcast_room(room, {"type": "room:update", "room": room_snapshot(room)})
                    continue

                # Risk
                if mtype == "joker:risk":
                    if not me.joker_risk:
                        await ws_send_safe(ws, {"type": "error", "message": "Risk Joker bereits verbraucht."})
                        continue
                    me.joker_risk = False
                    me.used_risk_this_q = True
                    room.joker_used_this_q = True

                    await ws_send_safe(ws, {"type": "info", "message": f"Risk aktiv: richtig = +{2*POINTS_PER_QUESTION}, falsch = -{POINTS_PER_QUESTION}"})
                    await broadcast_room(room, {"type": "room:update", "room": room_snapshot(room)})
                    continue

                await ws_send_safe(ws, {"type": "error", "message": "Unbekannter Joker."})
                continue

            # unknown
            await ws_send_safe(ws, {"type": "error", "message": f"Unbekannter Nachrichtentyp: {mtype}"})

    except WebSocketDisconnect:
        pass
    finally:
        if current_room and me:
            me.connected = False
            me.ws = None
            me.last_seen = time.time()
            cleanup_offline_players(current_room)
            try:
                await broadcast_room(current_room, {"type": "room:update", "room": room_snapshot(current_room)})
            except Exception:
                pass


# -----------------------------
# Startup
# -----------------------------
@app.on_event("startup")
def on_startup():
    global questions
    questions = load_questions_from_csv(QUESTIONS_CSV_PATH)
    if len(questions) < 1:
        raise RuntimeError("No questions loaded from CSV (questions.csv missing or empty?)")
