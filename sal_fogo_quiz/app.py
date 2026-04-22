from __future__ import annotations

import hashlib
import json
import os
import random
import sqlite3
import time
import uuid
import unicodedata
from pathlib import Path
from typing import Any

from flask import Flask, abort, g, jsonify, redirect, render_template, request, session, url_for

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "quiz.db"
QUESTIONS_PATH = BASE_DIR / "perguntas.json"
SECRET_KEY = os.environ.get("SECRET_KEY", "troque-essa-chave-em-producao")
DEV_PASSWORD = os.environ.get("DEV_PASSWORD", "0832")
DEFAULT_TIME_MINUTES = int(os.environ.get("DEFAULT_TIME_MINUTES", "15"))
PHASE_OPTIONS = ["phase1", "phase2", "phase3"]

app = Flask(__name__)
app.config.update(SECRET_KEY=SECRET_KEY)

ELEMENTOS = ["🔥", "💧", "🌪️", "⚡", "🌑", "🌀", "💎", "💨"]


# --------------------------
# Banco de dados
# --------------------------
def get_db() -> sqlite3.Connection:
    if "db" not in g:
        conn = sqlite3.connect(DB_PATH)
        conn.row_factory = sqlite3.Row
        g.db = conn
    return g.db


@app.teardown_appcontext
def close_db(_: Any) -> None:
    db = g.pop("db", None)
    if db is not None:
        db.close()


def init_db() -> None:
    db = sqlite3.connect(DB_PATH)
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS participants (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_name TEXT NOT NULL,
            participant_name_lower TEXT NOT NULL,
            participant_token TEXT NOT NULL,
            device_fingerprint TEXT NOT NULL,
            round_id TEXT NOT NULL,
            started_at INTEGER NOT NULL,
            finished_at INTEGER,
            UNIQUE(participant_token, round_id),
            UNIQUE(device_fingerprint, round_id),
            UNIQUE(participant_name_lower, round_id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS results (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            participant_name TEXT NOT NULL,
            participant_token TEXT NOT NULL,
            device_fingerprint TEXT NOT NULL,
            score INTEGER NOT NULL,
            wrong_count INTEGER NOT NULL,
            unanswered_count INTEGER NOT NULL,
            elapsed_seconds INTEGER NOT NULL,
            created_at INTEGER NOT NULL,
            round_id TEXT NOT NULL,
            phase_reached TEXT NOT NULL,
            answers_json TEXT NOT NULL,
            UNIQUE(participant_token, round_id)
        )
        """
    )
    db.execute(
        """
        CREATE TABLE IF NOT EXISTS quiz_sessions (
            participant_token TEXT NOT NULL,
            round_id TEXT NOT NULL,
            state_json TEXT NOT NULL,
            updated_at INTEGER NOT NULL,
            PRIMARY KEY (participant_token, round_id)
        )
        """
    )
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('quiz_open', '0')")
    db.execute(
        "INSERT OR IGNORE INTO settings (key, value) VALUES ('current_round_id', ?)",
        (uuid.uuid4().hex,),
    )
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('active_phases', 'phase1,phase2,phase3')")
    db.execute("INSERT OR IGNORE INTO settings (key, value) VALUES ('time_minutes', ?)", (str(DEFAULT_TIME_MINUTES),))
    db.commit()
    db.close()


init_db()


# --------------------------
# Utilitários
# --------------------------
def get_setting(key: str) -> str | None:
    row = get_db().execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else None


def set_setting(key: str, value: str) -> None:
    db = get_db()
    db.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, value),
    )
    db.commit()


def quiz_is_open() -> bool:
    return get_setting("quiz_open") == "1"




def get_active_phases() -> list[str]:
    raw = (get_setting("active_phases") or "phase1,phase2,phase3").strip()
    phases = [part.strip() for part in raw.split(",") if part.strip() in PHASE_OPTIONS]
    return phases or ["phase1", "phase2", "phase3"]


def set_active_phases(phases: list[str]) -> None:
    clean = [phase for phase in phases if phase in PHASE_OPTIONS]
    if not clean:
        clean = ["phase1"]
    set_setting("active_phases", ",".join(clean))


def get_time_minutes() -> int:
    raw = (get_setting("time_minutes") or str(DEFAULT_TIME_MINUTES)).strip()
    try:
        minutes = int(raw)
    except ValueError:
        minutes = DEFAULT_TIME_MINUTES
    return max(1, min(minutes, 180))


def set_time_minutes(minutes: int) -> None:
    minutes = max(1, min(int(minutes), 180))
    set_setting("time_minutes", str(minutes))


def get_total_time_seconds() -> int:
    return get_time_minutes() * 60


def current_round_id() -> str:
    value = get_setting("current_round_id")
    if not value:
        value = uuid.uuid4().hex
        set_setting("current_round_id", value)
    return value


def ensure_participant_token() -> str:
    token = session.get("participant_token")
    if not token:
        token = uuid.uuid4().hex
        session["participant_token"] = token
    return token


def normalize_text(text: str) -> str:
    text = (text or "").strip().lower()
    text = unicodedata.normalize("NFD", text)
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.split())


def validate_name(name: str) -> str | None:
    stripped = (name or "").strip()
    if len(stripped) < 3:
        return "Seu nome precisa ter pelo menos 3 caracteres."
    if not any(emoji in stripped for emoji in ELEMENTOS):
        return "Seu nome precisa ter um elemento: 🔥 💧 🌪️ ⚡ 🌑 🌀 💎 💨"
    return None


def fingerprint_hash(raw_fingerprint: str) -> str:
    raw_fingerprint = (raw_fingerprint or "").strip()
    if not raw_fingerprint:
        raw_fingerprint = f"fallback::{request.headers.get('User-Agent', '')}::{request.remote_addr or ''}"
    return hashlib.sha256(raw_fingerprint.encode("utf-8")).hexdigest()


def load_question_sets() -> dict[str, Any]:
    with open(QUESTIONS_PATH, "r", encoding="utf-8") as f:
        data = json.load(f)
    return data


# --------------------------
# Montagem do quiz
# --------------------------
def shuffled_multiple_choice(question: dict[str, Any], phase_label: str, category: str | None = None) -> dict[str, Any]:
    pairs = list(enumerate(question["options"]))
    random.shuffle(pairs)
    new_options = [opt for _, opt in pairs]
    new_correct_index = 0
    for new_idx, (old_idx, _) in enumerate(pairs):
        if old_idx == int(question["correct_index"]):
            new_correct_index = new_idx
            break
    return {
        "type": "multiple_choice",
        "phase": phase_label,
        "category": category,
        "question": question["question"],
        "options": new_options,
        "correct_index": new_correct_index,
        "accepted_answers": [],
    }


def open_question(question: dict[str, Any], phase_label: str, category: str | None = None) -> dict[str, Any]:
    return {
        "type": "open_text",
        "phase": phase_label,
        "category": category,
        "question": question["question"],
        "options": [],
        "correct_index": None,
        "accepted_answers": [normalize_text(ans) for ans in question["accepted_answers"]],
    }


def build_quiz_state(name: str) -> dict[str, Any]:
    data = load_question_sets()
    compiled_questions: list[dict[str, Any]] = []
    active_phases = get_active_phases()

    if "phase1" in active_phases:
        for q in data["phase1"]:
            compiled_questions.append(shuffled_multiple_choice(q, "Fase 1 • Julgamento Inicial"))

    if "phase2" in active_phases:
        for category, questions in data["phase2"].items():
            for q in questions:
                compiled_questions.append(shuffled_multiple_choice(q, "Fase 2 • Prova das Equipes", category))

    if "phase3" in active_phases:
        for q in data["phase3"]:
            compiled_questions.append(open_question(q, "Fase 3 • Final", "Pergunta Aberta"))

    first_phase = "Rodada Personalizada"
    if compiled_questions:
        first_phase = compiled_questions[0]["phase"]

    return {
        "name": name.strip(),
        "started_at": int(time.time()),
        "current_index": 0,
        "questions": compiled_questions,
        "selected_answers": [],
        "round_id": current_round_id(),
        "finished": False,
        "final_score": None,
        "final_wrong": None,
        "final_unanswered": None,
        "phase_reached": first_phase,
        "active_phases": active_phases,
        "total_time_seconds": get_total_time_seconds(),
    }


def total_questions() -> int:
    return len(build_quiz_state("Temp 🔥")["questions"])


def remaining_seconds(state: dict[str, Any]) -> int:
    elapsed = int(time.time()) - int(state["started_at"])
    total = int(state.get("total_time_seconds", get_total_time_seconds()))
    return max(0, total - elapsed)


def save_quiz_state(state: dict[str, Any]) -> None:
    token = ensure_participant_token()
    round_id = state["round_id"]
    db = get_db()
    db.execute(
        """
        INSERT INTO quiz_sessions (participant_token, round_id, state_json, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(participant_token, round_id)
        DO UPDATE SET state_json = excluded.state_json, updated_at = excluded.updated_at
        """,
        (token, round_id, json.dumps(state, ensure_ascii=False), int(time.time())),
    )
    db.commit()


def clear_quiz_state() -> None:
    token = session.get("participant_token")
    round_id = current_round_id()
    if token:
        db = get_db()
        db.execute(
            "DELETE FROM quiz_sessions WHERE participant_token = ? AND round_id = ?",
            (token, round_id),
        )
        db.commit()


def get_quiz_state() -> dict[str, Any] | None:
    token = session.get("participant_token")
    if not token:
        return None
    row = get_db().execute(
        "SELECT state_json FROM quiz_sessions WHERE participant_token = ? AND round_id = ?",
        (token, current_round_id()),
    ).fetchone()
    if not row:
        return None
    return json.loads(row["state_json"])


def participant_exists_by_device(device_fingerprint: str, round_id: str) -> bool:
    row = get_db().execute(
        "SELECT 1 FROM participants WHERE device_fingerprint = ? AND round_id = ?",
        (device_fingerprint, round_id),
    ).fetchone()
    return row is not None


def participant_exists_by_token(token: str, round_id: str) -> bool:
    row = get_db().execute(
        "SELECT 1 FROM participants WHERE participant_token = ? AND round_id = ?",
        (token, round_id),
    ).fetchone()
    return row is not None


def participant_exists_by_name(name: str, round_id: str) -> bool:
    row = get_db().execute(
        "SELECT 1 FROM participants WHERE participant_name_lower = ? AND round_id = ?",
        (normalize_text(name), round_id),
    ).fetchone()
    return row is not None


def register_participant(name: str, token: str, device_fingerprint: str, round_id: str) -> None:
    db = get_db()
    db.execute(
        """
        INSERT INTO participants (
            participant_name, participant_name_lower, participant_token,
            device_fingerprint, round_id, started_at
        ) VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            name.strip(),
            normalize_text(name),
            token,
            device_fingerprint,
            round_id,
            int(time.time()),
        ),
    )
    db.commit()


def update_participant_finished(token: str, round_id: str) -> None:
    db = get_db()
    db.execute(
        "UPDATE participants SET finished_at = ? WHERE participant_token = ? AND round_id = ?",
        (int(time.time()), token, round_id),
    )
    db.commit()


def answer_is_correct(question: dict[str, Any], answer: Any) -> bool:
    if question["type"] == "multiple_choice":
        try:
            return int(answer) == int(question["correct_index"])
        except (TypeError, ValueError):
            return False
    normalized = normalize_text(str(answer))
    return normalized in question["accepted_answers"]


def finalize_quiz(state: dict[str, Any], timed_out: bool = False) -> int:
    if state.get("finished"):
        return int(state.get("final_score", 0))

    answers = state.get("selected_answers", [])
    questions = state.get("questions", [])
    score = 0
    wrong = 0

    for idx, question in enumerate(questions[: len(answers)]):
        if answer_is_correct(question, answers[idx]):
            score += 1
        else:
            wrong += 1

    unanswered = max(0, len(questions) - len(answers))
    total = int(state.get("total_time_seconds", get_total_time_seconds()))
    elapsed = total - remaining_seconds(state)
    if timed_out:
        elapsed = total

    token = ensure_participant_token()
    device_fingerprint = session.get("device_fingerprint_hash", "")

    db = get_db()
    db.execute(
        """
        INSERT OR REPLACE INTO results (
            participant_name, participant_token, device_fingerprint, score, wrong_count,
            unanswered_count, elapsed_seconds, created_at, round_id, phase_reached, answers_json
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            state["name"],
            token,
            device_fingerprint,
            score,
            wrong,
            unanswered,
            elapsed,
            int(time.time()),
            state["round_id"],
            state.get("phase_reached", "Fase 1 • Julgamento Inicial"),
            json.dumps(answers, ensure_ascii=False),
        ),
    )
    db.commit()
    update_participant_finished(token, state["round_id"])

    state["finished"] = True
    state["final_score"] = score
    state["final_wrong"] = wrong
    state["final_unanswered"] = unanswered
    save_quiz_state(state)
    return score


def current_question_payload(state: dict[str, Any]) -> dict[str, Any]:
    idx = int(state["current_index"])
    return state["questions"][idx]


def phase_progress_label(state: dict[str, Any]) -> str:
    idx = int(state["current_index"])
    questions = state.get("questions", [])
    if not questions:
        return "Rodada Personalizada"
    if idx >= len(questions):
        idx = len(questions) - 1
    return questions[idx]["phase"]


# --------------------------
# Rotas
# --------------------------
@app.route("/")
def index():
    ensure_participant_token()
    return render_template(
        "index.html",
        quiz_open=quiz_is_open(),
        elementos=ELEMENTOS,
        total_time_seconds=get_total_time_seconds(),
        time_minutes=get_time_minutes(),
        active_phases=get_active_phases(),
    )


@app.post("/start")
def start_quiz():
    if not quiz_is_open():
        return jsonify({"ok": False, "message": "O quiz está fechado no momento."}), 400

    payload = request.get_json(silent=True) or {}
    name = str(payload.get("name", ""))
    raw_fingerprint = str(payload.get("device_fingerprint", ""))

    error = validate_name(name)
    if error:
        return jsonify({"ok": False, "message": error}), 400

    if not get_active_phases():
        return jsonify({"ok": False, "message": "Nenhuma fase foi selecionada no painel DEV."}), 400

    token = ensure_participant_token()
    round_id = current_round_id()
    device_hash = fingerprint_hash(raw_fingerprint)
    session["device_fingerprint_hash"] = device_hash

    if participant_exists_by_token(token, round_id):
        return jsonify({"ok": False, "message": "Este navegador já participou desta rodada."}), 400

    if participant_exists_by_device(device_hash, round_id):
        return jsonify({"ok": False, "message": "Este aparelho já entrou nesta rodada."}), 400

    if participant_exists_by_name(name, round_id):
        return jsonify({"ok": False, "message": "Esse nome já foi usado nesta rodada."}), 400

    try:
        register_participant(name, token, device_hash, round_id)
    except sqlite3.IntegrityError:
        return jsonify({"ok": False, "message": "Entrada duplicada detectada para esta rodada."}), 400

    state = build_quiz_state(name)
    save_quiz_state(state)
    return jsonify({"ok": True, "redirect": url_for("quiz_page")})


@app.route("/quiz")
def quiz_page():
    state = get_quiz_state()
    if not state:
        return redirect(url_for("index"))

    if state.get("finished"):
        return redirect(url_for("result_page"))

    remaining = remaining_seconds(state)
    if remaining <= 0:
        finalize_quiz(state, timed_out=True)
        return redirect(url_for("result_page"))

    idx = int(state["current_index"])
    questions = state["questions"]
    if idx >= len(questions):
        finalize_quiz(state)
        return redirect(url_for("result_page"))

    question = current_question_payload(state)
    state["phase_reached"] = phase_progress_label(state)
    save_quiz_state(state)

    template_name = "quiz_open.html" if question["type"] == "open_text" else "quiz.html"
    return render_template(
        template_name,
        participant_name=state["name"],
        question=question,
        current_number=idx + 1,
        total_questions=len(questions),
        remaining_seconds=remaining,
    )


@app.post("/answer")
def answer_question():
    state = get_quiz_state()
    if not state:
        return jsonify({"ok": False, "message": "Sessão do quiz não encontrada."}), 400

    if state.get("finished"):
        return jsonify({"ok": True, "redirect": url_for("result_page")})

    remaining = remaining_seconds(state)
    if remaining <= 0:
        finalize_quiz(state, timed_out=True)
        return jsonify({"ok": True, "redirect": url_for("result_page")})

    idx = int(state["current_index"])
    question = state["questions"][idx]
    payload = request.get_json(silent=True) or {}

    if question["type"] == "multiple_choice":
        selected_index = payload.get("selected_index")
        if selected_index is None:
            return jsonify({"ok": False, "message": "Selecione uma alternativa."}), 400
        try:
            selected_index = int(selected_index)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "Resposta inválida."}), 400
        if not (0 <= selected_index < len(question["options"])):
            return jsonify({"ok": False, "message": "Resposta fora do intervalo."}), 400
        state["selected_answers"].append(selected_index)
    else:
        answer_text = str(payload.get("answer_text", "")).strip()
        if not answer_text:
            return jsonify({"ok": False, "message": "Digite sua resposta antes de confirmar."}), 400
        state["selected_answers"].append(answer_text)

    state["current_index"] = idx + 1
    state["phase_reached"] = phase_progress_label(state)
    save_quiz_state(state)

    if state["current_index"] >= len(state["questions"]):
        finalize_quiz(state)
        return jsonify({"ok": True, "redirect": url_for("result_page")})

    return jsonify({"ok": True, "redirect": url_for("quiz_page")})


@app.route("/result")
def result_page():
    state = get_quiz_state()
    if not state:
        return redirect(url_for("index"))

    if not state.get("finished"):
        finalize_quiz(state, timed_out=remaining_seconds(state) <= 0)

    return render_template(
        "resultado.html",
        participant_name=state["name"],
        score=int(state.get("final_score", 0)),
        wrong_count=int(state.get("final_wrong", 0)),
        unanswered_count=int(state.get("final_unanswered", 0)),
        total_questions=len(state.get("questions", [])),
        total_time_seconds=get_total_time_seconds(),
        time_minutes=get_time_minutes(),
    )


@app.post("/dev/login")
def dev_login():
    payload = request.get_json(silent=True) or {}
    password = str(payload.get("password", ""))
    if password != DEV_PASSWORD:
        return jsonify({"ok": False, "message": "Senha DEV incorreta."}), 401
    session["dev_logged"] = True
    return jsonify({"ok": True, "redirect": url_for("dev_panel")})


def require_dev() -> None:
    if not session.get("dev_logged"):
        abort(403)


@app.route("/dev")
def dev_panel():
    require_dev()
    rows = get_db().execute(
        """
        SELECT
            p.participant_name,
            COALESCE(r.score, 0) AS score,
            COALESCE(r.wrong_count, 0) AS wrong_count,
            COALESCE(r.unanswered_count, 0) AS unanswered_count,
            COALESCE(r.elapsed_seconds, CAST(strftime('%s','now') AS INTEGER) - p.started_at) AS elapsed_seconds,
            COALESCE(r.phase_reached, 'Em andamento') AS phase_reached,
            CASE WHEN p.finished_at IS NULL THEN 'Em andamento' ELSE 'Finalizado' END AS status
        FROM participants p
        LEFT JOIN results r
            ON r.participant_token = p.participant_token
            AND r.round_id = p.round_id
        WHERE p.round_id = ?
        ORDER BY score DESC, elapsed_seconds ASC, p.participant_name ASC
        """,
        (current_round_id(),),
    ).fetchall()
    return render_template(
        "dev.html",
        quiz_open=quiz_is_open(),
        results=rows,
        current_round_id=current_round_id(),
        total_time_seconds=get_total_time_seconds(),
        time_minutes=get_time_minutes(),
        active_phases=get_active_phases(),
    )


@app.post("/dev/action")
def dev_action():
    require_dev()
    payload = request.get_json(silent=True) or {}
    action = str(payload.get("action", ""))

    if action == "open":
        if quiz_is_open():
            return jsonify({"ok": False, "message": "O quiz já está aberto."}), 400
        set_setting("quiz_open", "1")
        clear_quiz_state()
        return jsonify({"ok": True, "message": "Quiz aberto com sucesso."})

    if action == "close":
        if not quiz_is_open():
            return jsonify({"ok": False, "message": "O quiz já está fechado."}), 400
        set_setting("quiz_open", "0")
        return jsonify({"ok": True, "message": "Quiz fechado com sucesso."})


    if action == "set_phases":
        selected = payload.get("phases", [])
        if not isinstance(selected, list):
            return jsonify({"ok": False, "message": "Formato inválido para fases."}), 400
        valid = [phase for phase in selected if phase in PHASE_OPTIONS]
        if not valid:
            return jsonify({"ok": False, "message": "Selecione pelo menos uma fase."}), 400
        set_active_phases(valid)
        return jsonify({"ok": True, "message": "Fases atualizadas com sucesso."})


    if action == "set_time_minutes":
        raw_minutes = payload.get("minutes")
        try:
            minutes = int(raw_minutes)
        except (TypeError, ValueError):
            return jsonify({"ok": False, "message": "Informe um tempo inteiro em minutos."}), 400
        if not (1 <= minutes <= 180):
            return jsonify({"ok": False, "message": "Escolha um tempo entre 1 e 180 minutos."}), 400
        set_time_minutes(minutes)
        return jsonify({"ok": True, "message": f"Tempo da rodada ajustado para {minutes} minuto(s)."})

    if action == "clear":
        db = get_db()
        db.execute("DELETE FROM results")
        db.execute("DELETE FROM participants")
        db.execute("DELETE FROM quiz_sessions")
        db.commit()
        set_setting("current_round_id", uuid.uuid4().hex)
        set_setting("quiz_open", "0")
        session.pop("participant_token", None)
        clear_quiz_state()
        return jsonify(
            {
                "ok": True,
                "message": "Resultados apagados. Nova rodada criada e quiz fechado.",
            }
        )

    return jsonify({"ok": False, "message": "Ação inválida."}), 400


@app.route("/dev/logout")
def dev_logout():
    session.pop("dev_logged", None)
    return redirect(url_for("index"))


@app.route("/reset-local")
def reset_local():
    # utilitário opcional para limpar apenas a sessão local do navegador atual
    session.clear()
    return redirect(url_for("index"))


if __name__ == "__main__":
    init_db()
    app.run(debug=True)
