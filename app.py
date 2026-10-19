import sqlite3
import os
import re
import json
import time
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from functools import wraps
from flask import Flask, request, jsonify, g, send_from_directory
from flask_cors import CORS
import bcrypt
import jwt

# ─────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────
IS_PROD = os.environ.get("FLASK_ENV") == "production"
SECRET_KEY = os.environ.get("SECRET_KEY")
if not SECRET_KEY:
    if IS_PROD:
        raise RuntimeError("SECRET_KEY environment variable is required in production")
    SECRET_KEY = "fayou-secret-dev-key-DO-NOT-USE-IN-PROD"
    print("WARNING: using insecure dev SECRET_KEY. Set the SECRET_KEY env var for production.")

DB_PATH = os.path.join(os.path.dirname(__file__), "fayou.db")
TOKEN_EXPIRY_HOURS = 24 * 7

ALLOWED_ORIGINS = [o.strip() for o in os.environ.get("ALLOWED_ORIGINS", "http://localhost:5000").split(",") if o.strip()]

app = Flask(__name__, static_folder="frontend", static_url_path="")
CORS(app, resources={r"/api/*": {"origins": ALLOWED_ORIGINS}}, supports_credentials=True)

DEFAULT_WILAYA = "16 - Alger"


# ─────────────────────────────────────────────
# DB HELPERS
# ─────────────────────────────────────────────
def get_db():
    if "db" not in g:
        g.db = sqlite3.connect(DB_PATH)
        g.db.row_factory = sqlite3.Row
        g.db.execute("PRAGMA foreign_keys = ON")
        g.db.execute("PRAGMA journal_mode = WAL")
    return g.db

@app.teardown_appcontext
def close_db(exc=None):
    db = g.pop("db", None)
    if db:
        db.close()

def q(sql, params=(), one=False):
    cur = get_db().execute(sql, params)
    r = cur.fetchone() if one else cur.fetchall()
    return dict(r) if (one and r) else ([dict(x) for x in r] if r else (None if one else []))

def run(sql, params=()):
    db = get_db()
    cur = db.execute(sql, params)
    db.commit()
    return cur.lastrowid


# ─────────────────────────────────────────────
# SCHEMA
# ─────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id            TEXT PRIMARY KEY,
    email         TEXT UNIQUE NOT NULL,
    password      TEXT NOT NULL,
    name          TEXT NOT NULL,
    initials      TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT 'user',   -- user | admin
    phone         TEXT,
    wilaya        TEXT,
    address       TEXT,
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
    store_name    TEXT,
    store_status  TEXT NOT NULL DEFAULT 'none',    -- none | pending | approved | rejected
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    type          TEXT NOT NULL,                 -- sell | enchere | echange | service | location | annonce
    title         TEXT NOT NULL,
    description   TEXT NOT NULL,
    price         INTEGER DEFAULT 0,
    category      TEXT NOT NULL,
    wilaya        TEXT NOT NULL,
    emoji         TEXT DEFAULT 'package',         -- Lucide icon name (see module docstring)
    photos        TEXT DEFAULT '[]',              -- JSON array of image data URLs (max 6)
    exchange_for  TEXT,                            -- echange only, optional
    condition     TEXT,                            -- sell/echange only, optional: neuf | occasion
    seller_id     TEXT NOT NULL,
    seller_name   TEXT NOT NULL,
    seller_email  TEXT NOT NULL,
    seller_phone  TEXT,
    is_store      INTEGER DEFAULT 0,
    store_name    TEXT,
    status        TEXT NOT NULL DEFAULT 'pending', -- pending | approved | rejected
    auction_end   TEXT,
    created_at    TEXT NOT NULL,
    FOREIGN KEY(seller_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS bids (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    listing_id  INTEGER NOT NULL,
    user_id     TEXT NOT NULL,
    user_name   TEXT NOT NULL,
    amount      INTEGER NOT NULL,
    created_at  TEXT NOT NULL,
    FOREIGN KEY(listing_id) REFERENCES listings(id),
    FOREIGN KEY(user_id) REFERENCES users(id)
);

CREATE TABLE IF NOT EXISTS notifications (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     TEXT NOT NULL,
    title       TEXT NOT NULL,
    body        TEXT NOT NULL,
    is_read     INTEGER DEFAULT 0,
    created_at  TEXT NOT NULL,
    FOREIGN KEY(user_id) REFERENCES users(id)
);
"""

def now_str():
    return datetime.now(timezone.utc).isoformat()


# ─────────────────────────────────────────────
# SCHEMA REPAIR
# ─────────────────────────────────────────────
# Columns each table MUST have for the current codebase to work. If an
# existing fayou.db was created by an older version of this app (e.g. the
# old delivery/tracking system), its tables may be missing columns that
# `ALTER TABLE ADD COLUMN` never gets a chance to add (that only happens for
# columns listed in run_migrations, and SQLite can't rename/retype columns
# via ALTER TABLE anyway). Left alone, this causes hard crashes like
# "sqlite3.OperationalError: no such column: listing_id".
REQUIRED_COLUMNS = {
    "users": {"id", "email", "password", "name", "initials", "role", "phone", "wilaya",
              "address", "status", "store_name", "store_status", "created_at"},
    "listings": {"id", "type", "title", "description", "price", "category", "wilaya", "emoji",
                 "photos", "exchange_for", "condition", "seller_id", "seller_name", "seller_email",
                 "seller_phone", "is_store", "store_name", "status", "auction_end", "created_at"},
    "bids": {"id", "listing_id", "user_id", "user_name", "amount", "created_at"},
    "notifications": {"id", "user_id", "title", "body", "is_read", "created_at"},
}

def repair_schema(db):
    """Rename any pre-existing table that doesn't match the columns this
    codebase expects, so that the CREATE TABLE IF NOT EXISTS statements
    right after this can recreate it cleanly. Old data isn't deleted — it's
    preserved under a `<table>_legacy_<timestamp>` name in case it's needed."""
    existing = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    for table, required in REQUIRED_COLUMNS.items():
        if table not in existing:
            continue
        cols = {r[1] for r in db.execute(f"PRAGMA table_info({table})")}
        missing = required - cols
        if missing:
            legacy = f"{table}_legacy_{int(time.time())}"
            db.execute(f"ALTER TABLE {table} RENAME TO {legacy}")
            print(f"WARNING: table '{table}' was missing columns {missing} "
                  f"(likely an old schema version) -> renamed to '{legacy}' "
                  f"and will be recreated empty.")
    db.commit()


def run_migrations(db):
    """Adds any columns that a pre-existing fayou.db (from an older version
    of this schema) might be missing. CREATE TABLE IF NOT EXISTS only
    creates the table when it's absent — it never alters an existing one,
    so upgrading the schema needs explicit ALTER TABLE statements here."""
    migrations = [
        "ALTER TABLE users ADD COLUMN store_name TEXT",
        "ALTER TABLE users ADD COLUMN store_status TEXT NOT NULL DEFAULT 'none'",
        "ALTER TABLE listings ADD COLUMN is_store INTEGER DEFAULT 0",
        "ALTER TABLE listings ADD COLUMN store_name TEXT",
        "ALTER TABLE listings ADD COLUMN auction_end TEXT",
        "ALTER TABLE listings ADD COLUMN photos TEXT DEFAULT '[]'",
        "ALTER TABLE listings ADD COLUMN exchange_for TEXT",
        "ALTER TABLE listings ADD COLUMN condition TEXT",
    ]
    for stmt in migrations:
        try:
            db.execute(stmt)
        except sqlite3.OperationalError:
            pass  # column already exists
    db.commit()

def seed_db():
    fresh = not os.path.exists(DB_PATH)
    db = sqlite3.connect(DB_PATH)
    db.row_factory = sqlite3.Row
    repair_schema(db)
    db.executescript(SCHEMA)
    db.commit()
    run_migrations(db)

    already = db.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    if already == 0:
        today = now_str()
        users = [
            ("demo_user",  "user@fayou.dz",  "Ahmed Benali", "AB", "user",  "0555123456", "16 - Alger", "Bab Ezzouar, Alger", "approved", "Boutique Ahmed", "approved"),
            ("demo_admin", "admin@fayou.dz", "Admin Fayou",  "AF", "admin", "0770000001", "16 - Alger", "Alger Centre",       "approved", None, "none"),
        ]
        passes = {"demo_user": "password123", "demo_admin": "admin123"}
        for (uid, email, name, initials, role, phone, wilaya, addr, status, store_name, store_status) in users:
            hashed = bcrypt.hashpw(passes[uid].encode(), bcrypt.gensalt()).decode()
            db.execute(
                "INSERT INTO users (id,email,password,name,initials,role,phone,wilaya,address,status,store_name,store_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (uid, email, hashed, name, initials, role, phone, wilaya, addr, status, store_name, store_status, today)
            )

        # emoji values below are Lucide icon names (see module docstring), not literal emoji.
        listings = [
            ("echange", "Échange PS5 contre vélo VTT",        "PS5 avec 2 jeux, cherche vélo VTT en bon état.",           0, "Sport",        "19 - Sétif", "gamepad-2", "demo_user", "Ahmed Benali", "user@fayou.dz", "occasion"),
            ("sell",    "Samsung Galaxy A54 - 128Go",        "Smartphone Samsung 5G, AMOLED 6.4\", très bon état.", 55000, "Électronique", "16 - Alger", "smartphone", "demo_user", "Ahmed Benali", "user@fayou.dz", "occasion"),
            ("sell",    "Nike Air Max 90",                    "Chaussures de running légères, taille 42.",           12500, "Mode",         "31 - Oran",  "footprints", "demo_user", "Ahmed Benali", "user@fayou.dz", "neuf"),
            ("enchere", "Collection de timbres algériens",    "Timbres 1962-2000, plus de 500 pièces.",               8000, "Autre",        "16 - Alger", "coins", "demo_admin", "Admin Fayou", "admin@fayou.dz", None),
            ("service", "Cours de guitare à domicile",        "10 ans d'expérience, débutants et intermédiaires.",     2500, "Services",     "16 - Alger", "music", "demo_user", "Ahmed Benali", "user@fayou.dz", None),
            ("location","Appartement F3 à louer - Alger",     "F3 meublé, proche métro, 3ème étage avec ascenseur.",  45000, "Immobilier",   "16 - Alger", "home", "demo_user", "Ahmed Benali", "user@fayou.dz", None),
            ("annonce", "Recrutement chauffeur livreur",       "Recherche chauffeur livreur temps plein, permis B.",       0, "Services",     "16 - Alger", "megaphone", "demo_admin", "Admin Fayou", "admin@fayou.dz", None),
        ]
        for (typ, title, desc, price, cat, wilaya, emoji, seller_id, seller_name, seller_email, cond) in listings:
            aend = (datetime.now(timezone.utc) + timedelta(days=3)).isoformat() if typ == "enchere" else None
            exch = "vélo VTT" if typ == "echange" else None
            seller_phone = "0555123456" if seller_id == "demo_user" else "0770000001"
            is_store = 1 if seller_id == "demo_user" else 0
            store_name = "Boutique Ahmed" if seller_id == "demo_user" else None
            db.execute(
                """INSERT INTO listings (type,title,description,price,category,wilaya,emoji,photos,exchange_for,condition,
                   seller_id,seller_name,seller_email,seller_phone,is_store,store_name,status,auction_end,created_at)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (typ, title, desc, price, cat, wilaya, emoji, "[]", exch, cond, seller_id, seller_name, seller_email,
                 seller_phone, is_store, store_name, "approved", aend, today)
            )
    db.commit()
    db.close()
    print(("Created and seeded" if fresh else "Loaded existing") + f" database at {DB_PATH}")


# ─────────────────────────────────────────────
# AUTH
# ─────────────────────────────────────────────
def make_token(user_id):
    payload = {"sub": user_id, "exp": datetime.now(timezone.utc) + timedelta(hours=TOKEN_EXPIRY_HOURS), "iat": datetime.now(timezone.utc)}
    return jwt.encode(payload, SECRET_KEY, algorithm="HS256")

def decode_token(token):
    try:
        return jwt.decode(token, SECRET_KEY, algorithms=["HS256"])
    except (jwt.ExpiredSignatureError, jwt.InvalidTokenError):
        return None

def get_token():
    h = request.headers.get("Authorization", "")
    if h.startswith("Bearer "):
        return h[7:]
    return request.cookies.get("token")

def get_optional_user():
    token = get_token()
    if not token:
        return None
    payload = decode_token(token)
    if not payload:
        return None
    return q("SELECT * FROM users WHERE id=?", (payload["sub"],), one=True)

def auth_required(f):
    @wraps(f)
    def wrapper(*args, **kwargs):
        token = get_token()
        if not token:
            return jsonify({"error": "Non authentifié"}), 401
        payload = decode_token(token)
        if not payload:
            return jsonify({"error": "Token invalide ou expiré"}), 401
        user = q("SELECT * FROM users WHERE id=?", (payload["sub"],), one=True)
        if not user:
            return jsonify({"error": "Utilisateur introuvable"}), 401
        g.current_user = user
        return f(*args, **kwargs)
    return wrapper

def role_required(*roles):
    def decorator(f):
        @wraps(f)
        @auth_required
        def wrapper(*args, **kwargs):
            if g.current_user["role"] not in roles:
                return jsonify({"error": "Accès refusé"}), 403
            return f(*args, **kwargs)
        return wrapper
    return decorator

def approved_required(f):
    """Only an admin-approved account may post listings or bid."""
    @wraps(f)
    @auth_required
    def wrapper(*args, **kwargs):
        if g.current_user["status"] != "approved":
            return jsonify({"error": "Votre compte est en attente de validation par un administrateur"}), 403
        return f(*args, **kwargs)
    return wrapper


# Basic login rate limiting (in-memory; fine for a single-process demo).
_login_attempts = {}
LOGIN_WINDOW_SEC = 15 * 60
LOGIN_MAX_ATTEMPTS = 6

def _login_key():
    ip = request.headers.get("X-Forwarded-For", request.remote_addr) or "unknown"
    email = (request.get_json(silent=True) or {}).get("email", "").strip().lower()
    return f"{ip}:{email}"

def login_rate_limited():
    key = _login_key()
    now = time.time()
    attempts = [t for t in _login_attempts.get(key, []) if now - t < LOGIN_WINDOW_SEC]
    _login_attempts[key] = attempts
    return len(attempts) >= LOGIN_MAX_ATTEMPTS

def register_login_failure():
    _login_attempts.setdefault(_login_key(), []).append(time.time())

def clear_login_failures():
    _login_attempts.pop(_login_key(), None)


EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")

def safe_user(u):
    if not u:
        return None
    d = dict(u)
    d.pop("password", None)
    return d

def push_notif(user_id, title, body):
    run("INSERT INTO notifications (user_id,title,body,is_read,created_at) VALUES (?,?,?,0,?)", (user_id, title, body, now_str()))

def push_notif_all_admins(title, body):
    for admin in q("SELECT id FROM users WHERE role='admin'"):
        push_notif(admin["id"], title, body)

def paginate_args():
    try:
        limit = min(max(int(request.args.get("limit", 24)), 1), 100)
    except ValueError:
        limit = 24
    try:
        offset = max(int(request.args.get("offset", 0)), 0)
    except ValueError:
        offset = 0
    return limit, offset


# ─────────────────────────────────────────────
# AUTH ENDPOINTS
# ─────────────────────────────────────────────
@app.route("/api/auth/register", methods=["POST"])
def register():
    data = request.get_json() or {}
    first = data.get("first_name", "").strip()
    last = data.get("last_name", "").strip()
    email = data.get("email", "").strip().lower()
    phone = data.get("phone", "").strip()
    wilaya = data.get("wilaya", DEFAULT_WILAYA)
    password = data.get("password", "")
    confirm = data.get("confirm_password", "")

    if not all([first, last, email, phone, password]):
        return jsonify({"error": "Tous les champs sont obligatoires"}), 400
    if len(password) < 8:
        return jsonify({"error": "Mot de passe trop court (min 8 caractères)"}), 400
    if password != confirm:
        return jsonify({"error": "Les mots de passe ne correspondent pas"}), 400
    if not EMAIL_RE.match(email):
        return jsonify({"error": "Email invalide"}), 400
    if q("SELECT id FROM users WHERE email=?", (email,), one=True):
        return jsonify({"error": "Cet email est déjà utilisé"}), 409

    uid = "u_" + uuid.uuid4().hex[:12]
    name = f"{first} {last}"
    initials = (first[0] + last[0]).upper()
    hashed = bcrypt.hashpw(password.encode(), bcrypt.gensalt()).decode()
    run("INSERT INTO users (id,email,password,name,initials,role,phone,wilaya,address,status,store_status,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (uid, email, hashed, name, initials, "user", phone, wilaya, "", "pending", "none", now_str()))
    push_notif_all_admins("Nouveau compte à valider", f"{name} ({email}) a créé un compte et attend une validation.")
    return jsonify({"message": "Compte créé! En attente de validation par l'administrateur."}), 201


@app.route("/api/auth/login", methods=["POST"])
def login():
    if login_rate_limited():
        return jsonify({"error": "Trop de tentatives. Réessayez dans quelques minutes."}), 429
    data = request.get_json() or {}
    email = data.get("email", "").strip().lower()
    password = data.get("password", "")
    if not email or not password:
        return jsonify({"error": "Email et mot de passe requis"}), 400
    user = q("SELECT * FROM users WHERE email=?", (email,), one=True)
    if not user or not bcrypt.checkpw(password.encode(), user["password"].encode()):
        register_login_failure()
        return jsonify({"error": "Email ou mot de passe incorrect"}), 401
    clear_login_failures()
    token = make_token(user["id"])
    return jsonify({"token": token, "user": safe_user(user)})


@app.route("/api/auth/me", methods=["GET"])
@auth_required
def me():
    return jsonify({"user": safe_user(g.current_user)})


@app.route("/api/auth/profile", methods=["PUT"])
@auth_required
def update_profile():
    data = request.get_json() or {}
    u = g.current_user
    name = data.get("name", u["name"]).strip()
    phone = data.get("phone", u["phone"] or "").strip()
    address = data.get("address", u["address"] or "").strip()
    wilaya = data.get("wilaya", u["wilaya"] or DEFAULT_WILAYA)
    if not name:
        return jsonify({"error": "Le nom est obligatoire"}), 400
    initials = "".join(w[0] for w in name.split() if w).upper()[:2] or "NA"
    run("UPDATE users SET name=?,initials=?,phone=?,address=?,wilaya=? WHERE id=?", (name, initials, phone, address, wilaya, u["id"]))
    return jsonify({"user": safe_user(q("SELECT * FROM users WHERE id=?", (u["id"],), one=True))})


@app.route("/api/auth/password", methods=["PUT"])
@auth_required
def change_password():
    data = request.get_json() or {}
    old, new, conf = data.get("old_password", ""), data.get("new_password", ""), data.get("confirm_password", "")
    if not old or not new:
        return jsonify({"error": "Tous les champs sont requis"}), 400
    if len(new) < 8:
        return jsonify({"error": "Nouveau mot de passe trop court (min 8 caractères)"}), 400
    if new != conf:
        return jsonify({"error": "Les mots de passe ne correspondent pas"}), 400
    u = q("SELECT * FROM users WHERE id=?", (g.current_user["id"],), one=True)
    if not bcrypt.checkpw(old.encode(), u["password"].encode()):
        return jsonify({"error": "Mot de passe actuel incorrect"}), 401
    hashed = bcrypt.hashpw(new.encode(), bcrypt.gensalt()).decode()
    run("UPDATE users SET password=? WHERE id=?", (hashed, u["id"]))
    return jsonify({"message": "Mot de passe mis à jour"})


# ─────────────────────────────────────────────
# STORE (name/badge only — approval does not gate selling)
# ─────────────────────────────────────────────
@app.route("/api/store/request", methods=["POST"])
@approved_required
def request_store():
    u = g.current_user
    store_name = (request.get_json() or {}).get("store_name", "").strip()
    if not store_name:
        return jsonify({"error": "Le nom de la boutique est requis"}), 400
    run("UPDATE users SET store_name=?, store_status='pending' WHERE id=?", (store_name, u["id"]))
    push_notif_all_admins("Demande de boutique", f"{u['name']} demande l'ouverture de la boutique \"{store_name}\".")
    return jsonify({"message": "Demande envoyée. Un administrateur va la valider."}), 201


@app.route("/api/store/me", methods=["GET"])
@auth_required
def my_store():
    u = q("SELECT store_name, store_status FROM users WHERE id=?", (g.current_user["id"],), one=True)
    return jsonify(u)


# ─────────────────────────────────────────────
# LISTINGS (sell / enchere / echange / service / location / annonce)
# ─────────────────────────────────────────────
LISTING_TYPES = ("sell", "enchere", "echange", "service", "location", "annonce")

# Every listing type is open to any approved user — there is no admin-only
# type. Individual listings (of any type) still require admin approval
# before they're publicly visible; that's what actually gates content, see
# the "status" column and the /api/admin/listings/<id>/approve endpoint.

# Types for which a price > 0 is mandatory at creation time.
PRICE_REQUIRED_TYPES = ("sell",)

# Types for which the "condition" (neuf/occasion) field is meaningful.
CONDITION_TYPES = ("sell", "echange")
VALID_CONDITIONS = ("neuf", "occasion")

MAX_PHOTOS = 6

# Fallback Lucide icon name per type, used only if the client doesn't send
# one. Mirrors TYPE_CFG.icon on the frontend.
DEFAULT_EMOJI = {
    "sell": "package", "enchere": "coins", "echange": "repeat",
    "service": "wrench", "location": "home", "annonce": "megaphone",
}

def parse_listing(listing):
    """Deserialize the photos JSON column into a real list for API responses."""
    if listing is None:
        return None
    try:
        listing["photos"] = json.loads(listing.get("photos") or "[]")
    except (TypeError, ValueError):
        listing["photos"] = []
    return listing

def with_bid_info(listing):
    listing = parse_listing(listing)
    if listing["type"] == "enchere":
        top = q("SELECT amount, user_name FROM bids WHERE listing_id=? ORDER BY amount DESC LIMIT 1", (listing["id"],), one=True)
        listing["highest_bid"] = top
        listing["bid_count"] = q("SELECT COUNT(*) c FROM bids WHERE listing_id=?", (listing["id"],), one=True)["c"]
    return listing

@app.route("/api/listings", methods=["GET"])
def get_listings():
    """Public: only approved listings. Guests get full contact info by design
    (email + phone) so they can reach the seller directly — the spec asks
    for this explicitly, it's not an oversight."""
    typ = request.args.get("type", "").strip()
    search = request.args.get("search", "").strip()
    category = request.args.get("category", "").strip()
    wilaya = request.args.get("wilaya", "").strip()
    condition = request.args.get("condition", "").strip().lower()
    sort = request.args.get("sort", "recent")
    limit, offset = paginate_args()

    sql, params = "SELECT * FROM listings WHERE status='approved'", []
    if typ in LISTING_TYPES:
        sql += " AND type=?"; params.append(typ)
    if search:
        sql += " AND (title LIKE ? OR description LIKE ?)"; params += [f"%{search}%", f"%{search}%"]
    if category:
        sql += " AND category=?"; params.append(category)
    if wilaya:
        sql += " AND wilaya=?"; params.append(wilaya)
    if condition in VALID_CONDITIONS:
        sql += " AND condition=?"; params.append(condition)
    sql += {"price_asc": " ORDER BY price ASC", "price_desc": " ORDER BY price DESC"}.get(sort, " ORDER BY id DESC")

    total = q("SELECT COUNT(*) c FROM (" + sql + ")", params, one=True)["c"]
    sql += " LIMIT ? OFFSET ?"; params += [limit, offset]
    listings = [with_bid_info(l) for l in q(sql, params)]
    return jsonify({"listings": listings, "total": total, "limit": limit, "offset": offset})


@app.route("/api/listings/<int:lid>", methods=["GET"])
def get_listing(lid):
    l = q("SELECT * FROM listings WHERE id=?", (lid,), one=True)
    if not l:
        return jsonify({"error": "Introuvable"}), 404
    viewer = get_optional_user()
    is_owner_or_admin = viewer and (viewer["id"] == l["seller_id"] or viewer["role"] == "admin")
    if l["status"] != "approved" and not is_owner_or_admin:
        return jsonify({"error": "Introuvable"}), 404
    return jsonify({"listing": with_bid_info(l)})


@app.route("/api/listings/mine", methods=["GET"])
@auth_required
def my_listings():
    listings = [parse_listing(l) for l in q("SELECT * FROM listings WHERE seller_id=? ORDER BY id DESC", (g.current_user["id"],))]
    return jsonify({"listings": listings})


@app.route("/api/listings", methods=["POST"])
@approved_required
def create_listing():
    u = g.current_user
    data = request.get_json() or {}
    typ = data.get("type", "sell")
    if typ not in LISTING_TYPES:
        return jsonify({"error": "Type invalide"}), 400

    title = data.get("title", "").strip()
    desc = data.get("description", "").strip()
    price = int(data.get("price", 0) or 0)
    category = data.get("category", "Autre")
    wilaya = data.get("wilaya", u["wilaya"] or DEFAULT_WILAYA)
    emoji = (data.get("emoji") or "").strip() or DEFAULT_EMOJI.get(typ, "package")

    if not title or not desc:
        return jsonify({"error": "Titre et description obligatoires"}), 400
    if typ in PRICE_REQUIRED_TYPES and price <= 0:
        return jsonify({"error": "Le prix est obligatoire pour une vente"}), 400

    # Photos: optional, up to MAX_PHOTOS data-URL strings.
    raw_photos = data.get("photos") or []
    if not isinstance(raw_photos, list):
        raw_photos = []
    photos = [p for p in raw_photos if isinstance(p, str) and p.strip()][:MAX_PHOTOS]

    # Exchange-for: only meaningful for "echange", and always optional.
    exchange_for = None
    if typ == "echange":
        exchange_for = (data.get("exchange_for") or "").strip() or None

    # Condition (neuf/occasion): only meaningful for sell/echange, optional.
    condition = None
    if typ in CONDITION_TYPES:
        condition = (data.get("condition") or "").strip().lower() or "occasion"
        if condition not in VALID_CONDITIONS:
            condition = "occasion"

    auction_end = None
    if typ == "enchere":
        days = int(data.get("auction_days", 3) or 3)
        auction_end = (datetime.now(timezone.utc) + timedelta(days=max(1, days))).isoformat()

    lid = run(
        """INSERT INTO listings (type,title,description,price,category,wilaya,emoji,photos,exchange_for,condition,seller_id,seller_name,
           seller_email,seller_phone,is_store,store_name,status,auction_end,created_at)
           VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (typ, title, desc, price, category, wilaya, emoji, json.dumps(photos), exchange_for, condition, u["id"], u["name"], u["email"], u["phone"],
         1 if u["store_status"] == "approved" else 0, u["store_name"], "pending", auction_end, now_str())
    )
    push_notif_all_admins("Nouvelle publication à valider", f"{u['name']} a publié \"{title}\" ({typ}).")
    return jsonify({"listing": parse_listing(q("SELECT * FROM listings WHERE id=?", (lid,), one=True)),
                    "message": "Publication envoyée. Elle sera visible après validation par un administrateur."}), 201


@app.route("/api/listings/<int:lid>", methods=["DELETE"])
@auth_required
def delete_listing(lid):
    u = g.current_user
    l = q("SELECT * FROM listings WHERE id=?", (lid,), one=True)
    if not l:
        return jsonify({"error": "Introuvable"}), 404
    if l["seller_id"] != u["id"] and u["role"] != "admin":
        return jsonify({"error": "Accès refusé"}), 403
    run("DELETE FROM bids WHERE listing_id=?", (lid,))
    run("DELETE FROM listings WHERE id=?", (lid,))
    return jsonify({"message": "Publication supprimée"})


# ─────────────────────────────────────────────
# BIDS (enchere only)
# ─────────────────────────────────────────────
@app.route("/api/listings/<int:lid>/bids", methods=["GET"])
def get_bids(lid):
    return jsonify({"bids": q("SELECT * FROM bids WHERE listing_id=? ORDER BY amount DESC", (lid,))})


@app.route("/api/listings/<int:lid>/bids", methods=["POST"])
@approved_required
def place_bid(lid):
    u = g.current_user
    l = q("SELECT * FROM listings WHERE id=?", (lid,), one=True)
    if not l or l["status"] != "approved":
        return jsonify({"error": "Introuvable"}), 404
    if l["type"] != "enchere":
        return jsonify({"error": "Cette publication n'est pas une enchère"}), 400
    if l["auction_end"] and datetime.fromisoformat(l["auction_end"]) < datetime.now(timezone.utc):
        return jsonify({"error": "Cette enchère est terminée"}), 400
    if l["seller_id"] == u["id"]:
        return jsonify({"error": "Vous ne pouvez pas enchérir sur votre propre annonce"}), 403
    amount = int((request.get_json() or {}).get("amount", 0) or 0)
    top = q("SELECT amount FROM bids WHERE listing_id=? ORDER BY amount DESC LIMIT 1", (lid,), one=True)
    floor = top["amount"] if top else l["price"]
    if amount <= floor:
        return jsonify({"error": f"L'enchère doit dépasser {floor} DA"}), 400
    run("INSERT INTO bids (listing_id,user_id,user_name,amount,created_at) VALUES (?,?,?,?,?)", (lid, u["id"], u["name"], amount, now_str()))
    push_notif(l["seller_id"], "Nouvelle enchère", f"{u['name']} a enchéri {amount} DA sur \"{l['title']}\".")
    return jsonify({"message": "Enchère placée"}), 201


# ─────────────────────────────────────────────
# NOTIFICATIONS
# ─────────────────────────────────────────────
@app.route("/api/notifications", methods=["GET"])
@auth_required
def get_notifications():
    notifs = q("SELECT * FROM notifications WHERE user_id=? ORDER BY id DESC LIMIT 50", (g.current_user["id"],))
    return jsonify({"notifications": notifs, "unread": sum(1 for n in notifs if not n["is_read"])})

@app.route("/api/notifications/read-all", methods=["PUT"])
@auth_required
def mark_all_read():
    run("UPDATE notifications SET is_read=1 WHERE user_id=?", (g.current_user["id"],))
    return jsonify({"message": "OK"})

@app.route("/api/notifications/<int:nid>/read", methods=["PUT"])
@auth_required
def mark_read(nid):
    run("UPDATE notifications SET is_read=1 WHERE id=? AND user_id=?", (nid, g.current_user["id"]))
    return jsonify({"message": "OK"})


# ─────────────────────────────────────────────
# ADMIN — ACCOUNTS
# ─────────────────────────────────────────────
@app.route("/api/admin/users", methods=["GET"])
@role_required("admin")
def admin_get_users():
    status = request.args.get("status", "")
    search = request.args.get("search", "").strip()
    sql, params = "SELECT * FROM users WHERE role != 'admin'", []
    if status:
        sql += " AND status=?"; params.append(status)
    if search:
        sql += " AND (name LIKE ? OR email LIKE ?)"; params += [f"%{search}%", f"%{search}%"]
    sql += " ORDER BY created_at DESC"
    return jsonify({"users": [safe_user(u) for u in q(sql, params)]})

@app.route("/api/admin/users/<uid>/approve", methods=["PUT"])
@role_required("admin")
def approve_user(uid):
    if not q("SELECT id FROM users WHERE id=?", (uid,), one=True):
        return jsonify({"error": "Utilisateur introuvable"}), 404
    run("UPDATE users SET status='approved' WHERE id=?", (uid,))
    push_notif(uid, "Compte approuvé", "Votre compte Fayou Services est actif. Vous pouvez publier vos annonces.")
    return jsonify({"user": safe_user(q("SELECT * FROM users WHERE id=?", (uid,), one=True))})

@app.route("/api/admin/users/<uid>/reject", methods=["PUT"])
@role_required("admin")
def reject_user(uid):
    if not q("SELECT id FROM users WHERE id=?", (uid,), one=True):
        return jsonify({"error": "Utilisateur introuvable"}), 404
    run("UPDATE users SET status='rejected' WHERE id=?", (uid,))
    push_notif(uid, "Compte refusé", "Votre demande d'accès a été refusée.")
    return jsonify({"user": safe_user(q("SELECT * FROM users WHERE id=?", (uid,), one=True))})

@app.route("/api/admin/users/<uid>", methods=["DELETE"])
@role_required("admin")
def admin_delete_user(uid):
    if not q("SELECT id FROM users WHERE id=?", (uid,), one=True):
        return jsonify({"error": "Utilisateur introuvable"}), 404
    db = get_db()
    # Foreign keys are enforced per-connection. Older/legacy tables that may
    # still exist in an upgraded fayou.db (e.g. from a previous schema
    # version) can carry FK references to users(id) that the explicit
    # cascade below doesn't know about, which makes a plain DELETE on the
    # user row fail with "FOREIGN KEY constraint failed" even though every
    # table this codebase actually uses has been cleaned up correctly.
    # We do the deliberate cascade first (so current-schema data stays
    # consistent), then temporarily relax FK enforcement just for the final
    # user delete so leftover/legacy references can't block it.
    listing_ids = [l["id"] for l in q("SELECT id FROM listings WHERE seller_id=?", (uid,))]
    for lid in listing_ids:
        run("DELETE FROM bids WHERE listing_id=?", (lid,))
    run("DELETE FROM listings WHERE seller_id=?", (uid,))
    run("DELETE FROM bids WHERE user_id=?", (uid,))
    run("DELETE FROM notifications WHERE user_id=?", (uid,))
    db.execute("PRAGMA foreign_keys = OFF")
    try:
        run("DELETE FROM users WHERE id=?", (uid,))
    finally:
        db.execute("PRAGMA foreign_keys = ON")
    return jsonify({"message": "Utilisateur supprimé"})


# ─────────────────────────────────────────────
# ADMIN — STORE REQUESTS
# ─────────────────────────────────────────────
@app.route("/api/admin/stores", methods=["GET"])
@role_required("admin")
def admin_get_stores():
    status = request.args.get("status", "")
    sql, params = "SELECT id,name,email,phone,store_name,store_status FROM users WHERE store_status != 'none'", []
    if status:
        sql += " AND store_status=?"; params.append(status)
    sql += " ORDER BY created_at DESC"
    return jsonify({"stores": q(sql, params)})

@app.route("/api/admin/stores/<uid>/approve", methods=["PUT"])
@role_required("admin")
def approve_store(uid):
    u = q("SELECT * FROM users WHERE id=?", (uid,), one=True)
    if not u:
        return jsonify({"error": "Utilisateur introuvable"}), 404
    run("UPDATE users SET store_status='approved' WHERE id=?", (uid,))
    run("UPDATE listings SET is_store=1, store_name=? WHERE seller_id=?", (u["store_name"], uid))
    push_notif(uid, "Boutique approuvée", f"Votre boutique \"{u['store_name']}\" est maintenant active.")
    return jsonify({"message": "Boutique approuvée"})

@app.route("/api/admin/stores/<uid>/reject", methods=["PUT"])
@role_required("admin")
def reject_store(uid):
    if not q("SELECT id FROM users WHERE id=?", (uid,), one=True):
        return jsonify({"error": "Utilisateur introuvable"}), 404
    run("UPDATE users SET store_status='rejected' WHERE id=?", (uid,))
    push_notif(uid, "Boutique refusée", "Votre demande d'ouverture de boutique a été refusée.")
    return jsonify({"message": "Boutique refusée"})


# ─────────────────────────────────────────────
# ADMIN — LISTINGS MODERATION
# ─────────────────────────────────────────────
@app.route("/api/admin/listings", methods=["GET"])
@role_required("admin")
def admin_get_listings():
    status = request.args.get("status", "")
    typ = request.args.get("type", "")
    sql, params = "SELECT * FROM listings WHERE 1=1", []
    if status:
        sql += " AND status=?"; params.append(status)
    if typ in LISTING_TYPES:
        sql += " AND type=?"; params.append(typ)
    sql += " ORDER BY id DESC"
    listings = [parse_listing(l) for l in q(sql, params)]
    return jsonify({"listings": listings})

@app.route("/api/admin/listings/<int:lid>/approve", methods=["PUT"])
@role_required("admin")
def admin_approve_listing(lid):
    l = q("SELECT * FROM listings WHERE id=?", (lid,), one=True)
    if not l:
        return jsonify({"error": "Introuvable"}), 404
    run("UPDATE listings SET status='approved' WHERE id=?", (lid,))
    push_notif(l["seller_id"], "Publication approuvée", f"\"{l['title']}\" est maintenant visible sur la plateforme.")
    return jsonify({"message": "Publication approuvée"})

@app.route("/api/admin/listings/<int:lid>/reject", methods=["PUT"])
@role_required("admin")
def admin_reject_listing(lid):
    l = q("SELECT * FROM listings WHERE id=?", (lid,), one=True)
    if not l:
        return jsonify({"error": "Introuvable"}), 404
    run("UPDATE listings SET status='rejected' WHERE id=?", (lid,))
    push_notif(l["seller_id"], "Publication refusée", f"\"{l['title']}\" n'a pas été approuvée.")
    return jsonify({"message": "Publication refusée"})


# ─────────────────────────────────────────────
# ADMIN — STATS
# ─────────────────────────────────────────────
@app.route("/api/admin/stats", methods=["GET"])
@role_required("admin")
def admin_stats():
    return jsonify({
        "total_users": q("SELECT COUNT(*) c FROM users WHERE role='user'", one=True)["c"],
        "pending_users": q("SELECT COUNT(*) c FROM users WHERE status='pending'", one=True)["c"],
        "pending_listings": q("SELECT COUNT(*) c FROM listings WHERE status='pending'", one=True)["c"],
        "pending_stores": q("SELECT COUNT(*) c FROM users WHERE store_status='pending'", one=True)["c"],
        "approved_listings": q("SELECT COUNT(*) c FROM listings WHERE status='approved'", one=True)["c"],
        "by_type": q("SELECT type, COUNT(*) c FROM listings WHERE status='approved' GROUP BY type"),
    })


# ─────────────────────────────────────────────
# FRONTEND SERVING
# ─────────────────────────────────────────────
@app.route("/", defaults={"path": ""})
@app.route("/<path:path>")
def serve_frontend(path):
    if path and os.path.exists(os.path.join(app.static_folder, path)):
        return send_from_directory(app.static_folder, path)
    return send_from_directory(app.static_folder, "index.html")


if __name__ == "__main__":
    seed_db()
    print("Fayou Services API running on http://localhost:5000")
    app.run(debug=not IS_PROD, port=5000, host="0.0.0.0")