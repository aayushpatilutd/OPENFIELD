"""Sessions, roles and the decorators that guard every route."""

import logging
from functools import wraps

from flask import session, request, jsonify, redirect
from werkzeug.security import generate_password_hash, check_password_hash

import db

log = logging.getLogger(__name__)

# Who may do what. Deliberately coarse — four roles an AP team actually has,
# rather than a permission matrix nobody will maintain.
ROLES = {
    "admin":    {"view", "edit", "approve", "push", "admin"},
    "approver": {"view", "edit", "approve", "push"},
    "clerk":    {"view", "edit"},
    "viewer":   {"view"},
}


def current_user():
    uid = session.get("user_id")
    if not uid:
        return None
    con = db.get_db()
    try:
        r = con.execute("SELECT * FROM users WHERE id=? AND active=1",
                        (uid,)).fetchone()
    finally:
        con.close()
    return dict(r) if r else None


def can(permission: str) -> bool:
    u = current_user()
    if not u:
        return False
    return permission in ROLES.get(u.get("role") or "viewer", set())


def login(email: str, password: str):
    con = db.get_db()
    try:
        r = con.execute("SELECT * FROM users WHERE lower(email)=lower(?) "
                        "AND active=1", (email.strip(),)).fetchone()
    finally:
        con.close()
    if not r or not check_password_hash(r["password_hash"], password):
        # One message for both cases: saying "no such user" tells an attacker
        # which addresses exist.
        return None, "That email and password do not match."
    session["user_id"] = r["id"]
    session["email"] = r["email"]
    session["role"] = r["role"]
    session.permanent = True
    con = db.get_db()
    try:
        con.execute("UPDATE users SET last_login=? WHERE id=?",
                    (db.utcnow(), r["id"]))
        con.commit()
    finally:
        con.close()
    db.say("auth", f"{r['email']} signed in", actor=r["email"])
    return dict(r), None


def logout():
    who = session.get("email")
    session.clear()
    if who:
        db.say("auth", f"{who} signed out", actor=who)


def require_login(f):
    @wraps(f)
    def wrapper(*a, **kw):
        if not session.get("user_id"):
            if request.path.startswith("/api/"):
                return jsonify({"error": "Not signed in",
                                "login_required": True}), 401
            return redirect("/login")
        return f(*a, **kw)
    return wrapper


def require(permission: str):
    def decorator(f):
        @wraps(f)
        def wrapper(*a, **kw):
            if not session.get("user_id"):
                if request.path.startswith("/api/"):
                    return jsonify({"error": "Not signed in",
                                    "login_required": True}), 401
                return redirect("/login")
            if not can(permission):
                if request.path.startswith("/api/"):
                    return jsonify({"error": f"Your role cannot {permission}."
                                    }), 403
                return redirect("/")
            return f(*a, **kw)
        return wrapper
    return decorator


def create_user(email, name, password, role="clerk", actor=""):
    if role not in ROLES:
        return None, f"'{role}' is not a role I know."
    con = db.get_db()
    try:
        if con.execute("SELECT 1 FROM users WHERE lower(email)=lower(?)",
                       (email,)).fetchone():
            return None, "That email already has an account."
        cur = con.execute("""INSERT INTO users
            (email,name,password_hash,role,active,created_at)
            VALUES (?,?,?,?,1,?)""",
            (email.strip(), name, generate_password_hash(password), role,
             db.utcnow()))
        con.commit()
        db.say("auth", f"Created the account {email} ({role})", actor=actor)
        return cur.lastrowid, None
    finally:
        con.close()


def set_password(user_id, password, actor=""):
    con = db.get_db()
    try:
        con.execute("UPDATE users SET password_hash=? WHERE id=?",
                    (generate_password_hash(password), user_id))
        con.commit()
    finally:
        con.close()
    db.say("auth", "Changed a password", actor=actor)
    return True


def using_default_password() -> bool:
    """True while the seeded admin can still sign in with 'changeme'.

    The UI shows a banner until this is false. A portal holding API
    credentials for four systems should not sit on a documented password
    quietly.
    """
    con = db.get_db()
    try:
        r = con.execute("SELECT password_hash FROM users "
                        "WHERE email='admin@invoicehub.local'").fetchone()
    finally:
        con.close()
    return bool(r and check_password_hash(r["password_hash"], "changeme"))
