# 🎮 Multiplayer Quiz Game (FastAPI + WebSockets)

A lightweight, real-time multiplayer quiz game built with **Python**, **FastAPI**, and **WebSockets**.  
Players join rooms via code, answer multiple-choice questions, use jokers, and see live results — all in a permanent dark mode UI.

Perfect as a small party game, demo project, or learning example for WebSockets.

---

## ✨ Features

- 🔐 Create & join rooms via short room codes
- 👑 Host controls: start game, reveal answers, next question, kick players
- ⚡ Real-time multiplayer via WebSockets
- ❓ 4-choice questions loaded from a CSV file
- 🎲 Randomized answer order
- 🃏 Jokers:
  - **50/50** – removes two wrong answers
  - **Spy** – see live answer picks of other players
  - **Risk** – double points if correct, lose points if wrong
  - Only **one joker per question** (globally)
- 🟢 Answer reveal with:
  - Correct answer highlighted in green
  - Player names + avatars shown on chosen answers
- 📊 Live scoreboard (name, avatar, score, connection state)
- 🌙 Permanent dark mode
- 🔁 Automatic reconnect on bad connections

---

## 📁 Project Structure

```text
.
├── main.py          # FastAPI server + Web UI (single-file app)
├── questions.csv    # Quiz questions (CSV)
└── README.md
```
📄 CSV Format (questions.csv)
csv
Copy code
Frage;Richtig;Falsch1;Falsch2;Falsch3
Was ist 2+2?;4;3;5;22
Hauptstadt von Frankreich?;Paris;Rom;Berlin;Madrid
Separator: ;

One question per line

Up to 30 questions are used per game

🚀 Setup & Run (Local)
1) Create & activate virtual environment
```pwsh
python -m venv venv
```
# Windows
```pwsh
venv\Scripts\activate
```
# Linux / macOS
source venv/bin/activate
2) Install dependencies
```bash
pip install fastapi "uvicorn[standard]"
```
3) Start the server
```bash
uvicorn main:app --reload --host 0.0.0.0 --port 8000
```
4) Open in browser
```text
http://localhost:8000
```
Open multiple tabs or devices to test multiplayer.

## 🌐 Deploy Behind Nginx (WebSockets Ready)
FastAPI runs internally (e.g. 127.0.0.1:8000), Nginx exposes it publicly.

Minimal Nginx config:

```nginx
map $http_upgrade $connection_upgrade {
    default upgrade;
    '' close;
}

server {
    listen 80;
    server_name your-domain.com;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection $connection_upgrade;
        proxy_set_header Host $host;
        proxy_read_timeout 3600;
    }
}
```
With HTTPS (Let’s Encrypt), WebSockets automatically upgrade to wss://.

## 🧠 Notes
- Game state is stored in memory (perfect for small games / parties)
- Restarting the server resets all rooms
- Designed as a clean MVP — easy to extend with:
 - Database (Redis/Postgres)
 - Auth / user accounts
 - Question editor
 - Mobile / desktop clients

## 🛠️ Tech Stack
- Python 3.10+
- FastAPI
- Uvicorn
- WebSockets
- Vanilla HTML / CSS / JS (no frontend framework)

## 📜 License
MIT — do whatever you want, have fun 🎉
