import os
import json
import sqlite3
import requests
from werkzeug.security import generate_password_hash, check_password_hash
from flask import Flask, request, jsonify, session, render_template, redirect, url_for
from flask_socketio import SocketIO, emit, join_room

app = Flask(__name__)
app.secret_key = os.environ.get("SECRET_KEY", "ROBLOX_CLONE_SUPER_SECRET_SESSION_ENCRYPTION_KEY_STRING")
PAYSTACK_SECRET_KEY = os.environ.get("PAYSTACK_SECRET_KEY", "sk_test_your_paystack_key")
ADMIN_PIN = os.environ.get("ADMIN_PIN", "2704")

socketio = SocketIO(app, cors_allowed_origins="*")
DB_FILE = "game_economy.db"

# --- 1. DATABASE SETUP & HELPERS ---
def get_db():
    conn = sqlite3.connect(DB_FILE)
    conn.row_factory = sqlite3.Row
    return conn

def init_db():
    with get_db() as conn:
        # Users table with starting currency & custom NGN exchange rate
        conn.execute('''
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                username TEXT UNIQUE NOT NULL,
                password_hash TEXT NOT NULL,
                coins INTEGER DEFAULT 100,
                coin_rate REAL DEFAULT 10.00
            )
        ''')
        try:
            conn.execute("ALTER TABLE users ADD COLUMN coin_rate REAL DEFAULT 10.00")
        except sqlite3.OperationalError:
            pass

        # Store items catalog
        conn.execute('''
            CREATE TABLE IF NOT EXISTS items (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                name TEXT UNIQUE NOT NULL,
                price INTEGER NOT NULL
            )
        ''')

        # Player inventory linking users and items
        conn.execute('''
            CREATE TABLE IF NOT EXISTS inventory (
                user_id INTEGER,
                item_id INTEGER,
                quantity INTEGER DEFAULT 1,
                PRIMARY KEY (user_id, item_id),
                FOREIGN KEY (user_id) REFERENCES users(id),
                FOREIGN KEY (item_id) REFERENCES items(id)
            )
        ''')

        # Pending player-to-player trades
        conn.execute('''
            CREATE TABLE IF NOT EXISTS trades (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id INTEGER NOT NULL,
                receiver_id INTEGER NOT NULL,
                offered_item_id INTEGER,
                offered_coins INTEGER DEFAULT 0,
                requested_item_id INTEGER,
                requested_coins INTEGER DEFAULT 0,
                status TEXT DEFAULT 'pending',
                FOREIGN KEY (sender_id) REFERENCES users(id),
                FOREIGN KEY (receiver_id) REFERENCES users(id)
            )
        ''')

        # Player-created games published via Prodlox Studio
        conn.execute('''
            CREATE TABLE IF NOT EXISTS custom_games (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                creator_username TEXT NOT NULL,
                title TEXT NOT NULL,
                blocks_json TEXT NOT NULL
            )
        ''')

        # Admin-managed experience directory
        conn.execute('''
            CREATE TABLE IF NOT EXISTS game_directory (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                game_id TEXT UNIQUE NOT NULL,
                game_name TEXT NOT NULL,
                scene_path TEXT NOT NULL,
                players INTEGER DEFAULT 1200
            )
        ''')

        # Seed default shop items
        conn.execute("INSERT OR IGNORE INTO items (name, price) VALUES ('Speed Coil', 50)")
        conn.execute("INSERT OR IGNORE INTO items (name, price) VALUES ('Gravity Coil', 75)")
        conn.execute("INSERT OR IGNORE INTO items (name, price) VALUES ('Golden Sword', 150)")

        # Seed starter experiences
        starter_games = [
            ("sandbox", "Voxel Sandbox", "res://MainWorld.tscn", 14520),
            ("sky_obby", "Mega Sky Obby", "res://MainWorld.tscn", 28410),
            ("coin_rush", "Coin Rush Simulator", "res://MainWorld.tscn", 19300),
            ("disaster", "Natural Disaster Survival", "res://MainWorld.tscn", 34120),
            ("tycoon", "Miner's Coin Tycoon", "res://MainWorld.tscn", 22890),
            ("sfoth", "Sword Fight Heights", "res://MainWorld.tscn", 11450),
            ("color_party", "Color Block Party", "res://MainWorld.tscn", 16780),
            ("speed_run", "Speed Run 4", "res://MainWorld.tscn", 15900),
            ("zombies", "Zombie Survival Wave", "res://MainWorld.tscn", 25600),
            ("laser_tag", "Laser Tag Arena", "res://MainWorld.tscn", 13200),
            ("rp_town", "Roleplay Town", "res://MainWorld.tscn", 41200),
            ("fishing", "Fishing Simulator", "res://MainWorld.tscn", 18400),
        ]
        conn.executemany(
            "INSERT OR IGNORE INTO game_directory (game_id, game_name, scene_path, players) VALUES (?, ?, ?, ?)",
            starter_games
        )
        conn.commit()

def _transfer_item(conn, from_user_id, to_user_id, item_id):
    conn.execute(
        "UPDATE inventory SET quantity = quantity - 1 WHERE user_id = ? AND item_id = ?",
        (from_user_id, item_id)
    )
    conn.execute(
        "DELETE FROM inventory WHERE user_id = ? AND item_id = ? AND quantity <= 0",
        (from_user_id, item_id)
    )
    conn.execute('''
        INSERT INTO inventory (user_id, item_id, quantity)
        VALUES (?, ?, 1)
        ON CONFLICT(user_id, item_id) DO UPDATE SET quantity = quantity + 1
    ''', (to_user_id, item_id))

# --- 2. ADMIN CONTROL MATRIX ROUTES ---
@app.route("/admin", methods=["GET"])
def admin_dashboard():
    if not session.get("admin_authenticated"):
        return render_template("admin.html", display_login=True, auth_failed=request.args.get("failed") == "1")

    with get_db() as conn:
        users = conn.execute("SELECT username, coins, COALESCE(coin_rate, 10.00) AS coin_rate FROM users").fetchall()
        games = conn.execute("SELECT game_id, game_name, scene_path, players FROM game_directory ORDER BY players DESC").fetchall()

    return render_template("admin.html", display_login=False, users=users, games=games)

@app.route("/admin/login", methods=["POST"])
def admin_login():
    pin = request.form.get("pin", "").strip()
    if pin == ADMIN_PIN:
        session["admin_authenticated"] = True
        return redirect(url_for("admin_dashboard"))
    return redirect(url_for("admin_dashboard", failed="1"))

@app.route("/admin/logout", methods=["GET"])
def admin_logout():
    session.pop("admin_authenticated", None)
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/set-balance", methods=["POST"])
def admin_set_balance():
    if not session.get("admin_authenticated"):
        return redirect(url_for("admin_dashboard"))

    username = request.form.get("username", "").strip()
    new_balance = int(request.form.get("new_balance", 0))

    with get_db() as conn:
        conn.execute("UPDATE users SET coins = ? WHERE username = ?", (new_balance, username))
        conn.commit()

    socketio.emit("balance_update", {"username": username, "coins": new_balance})
    return redirect(url_for("admin_dashboard"))

@app.route("/admin/modify-price", methods=["POST"])
def admin_modify_price():
    if not session.get("admin_authenticated"):
        return redirect(url_for("admin_dashboard"))

    username = request.form.get("username", "").strip()
    new_price = float(request.form.get("new_price", 10.0))

    with get_db() as conn:
        conn.execute("UPDATE users SET coin_rate = ? WHERE username = ?", (new_price, username))
        conn.commit()

    return redirect(url_for("admin_dashboard"))

@app.route("/admin/add-game", methods=["POST"])
def admin_add_game():
    if not session.get("admin_authenticated"):
        return redirect(url_for("admin_dashboard"))

    game_id = request.form.get("game_id", "").strip()
    game_name = request.form.get("game_name", "").strip()
    scene_path = request.form.get("scene_path", "res://MainWorld.tscn").strip()
    players = int(request.form.get("players", 1000))

    with get_db() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO game_directory (game_id, game_name, scene_path, players) VALUES (?, ?, ?, ?)",
            (game_id, game_name, scene_path, players)
        )
        conn.commit()

    socketio.emit("admin_game_added", {
        "game_id": game_id,
        "game_name": game_name,
        "scene_path": scene_path,
        "players": players
    })
    return redirect(url_for("admin_dashboard"))

# --- 3. AUTHENTICATION & SHOP ROUTES ---
@app.route("/register", methods=["POST"])
def register():
    data = request.json
    username = data.get("username")
    password = data.get("password")
    
    if not username or not password:
        return jsonify({"error": "Username and password required"}), 400

    try:
        with get_db() as conn:
            cursor = conn.execute(
                "INSERT INTO users (username, password_hash) VALUES (?, ?)",
                (username, generate_password_hash(password))
            )
            session["user_id"] = cursor.lastrowid
            session["username"] = username
            conn.commit()
        return jsonify({"message": "Registered successfully", "username": username, "coins": 100}), 201
    except sqlite3.IntegrityError:
        return jsonify({"error": "Username already taken"}), 409

@app.route("/login", methods=["POST"])
def login():
    data = request.json
    username = data.get("username")
    password = data.get("password")

    with get_db() as conn:
        user = conn.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        
    if user and check_password_hash(user["password_hash"], password):
        session["user_id"] = user["id"]
        session["username"] = user["username"]
        return jsonify({"message": "Logged in", "username": user["username"], "coins": user["coins"]}), 200
