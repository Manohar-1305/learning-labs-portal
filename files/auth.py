
import hashlib
import os
import re
import secrets
import smtplib
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from functools import wraps

import psycopg2
from authlib.integrations.flask_client import OAuth

from flask import (
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    session,
    url_for,
)
from flask_socketio import join_room
from werkzeug.security import check_password_hash, generate_password_hash

from files.ansible import app, socketio, terminal_lock, terminal_sessions
from files.config import get_db as _get_db, get_cursor, convert_placeholders

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
KEY_PATH = os.path.join(BASE_DIR, ".secret_key")

USERNAME_RE = re.compile(r"^[^@]{3,30}$")
EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")

RESET_MINUTES = 30
PASSWORD_MIN = 3
PASSWORD_MAX = 12

SESSION_JS_VERSION = 1


# ---------- secret key ----------

def _load_secret_key():
    env_key = os.environ.get("SECRET_KEY")

    if env_key:
        return env_key

    if os.path.exists(KEY_PATH):
        with open(KEY_PATH) as f:
            return f.read().strip()

    key = secrets.token_hex(32)

    with open(KEY_PATH, "w") as f:
        f.write(key)

    return key


if not app.secret_key:
    app.secret_key = _load_secret_key()

app.config.setdefault("SESSION_COOKIE_NAME", "learning_labs_session")
app.config.setdefault("SESSION_COOKIE_PATH", "/")
app.config.setdefault("SESSION_COOKIE_HTTPONLY", True)
app.config.setdefault("SESSION_COOKIE_SAMESITE", "Lax")
app.config.setdefault("SESSION_COOKIE_SECURE", False)
app.config.setdefault("PERMANENT_SESSION_LIFETIME", timedelta(days=7))

socketio.manage_session = True


# ---------- Google OAuth ----------
# ---------- Google OAuth ----------
oauth = OAuth(app)

GOOGLE_CLIENT_ID = os.environ.get("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = os.environ.get("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = os.environ.get("GOOGLE_REDIRECT_URI")

if GOOGLE_CLIENT_ID and GOOGLE_CLIENT_SECRET:
    oauth.register(
        name="google",
        client_id=GOOGLE_CLIENT_ID,
        client_secret=GOOGLE_CLIENT_SECRET,
        server_metadata_url="https://accounts.google.com/.well-known/openid-configuration",
        client_kwargs={"scope": "openid email profile"},
    )

@app.route("/auth/google")
def google_login():
    if not GOOGLE_REDIRECT_URI:
        return "GOOGLE_REDIRECT_URI is not configured", 500

    next_url = _safe_next(request.args.get("next"))
    session["google_next"] = next_url

    return oauth.google.authorize_redirect(GOOGLE_REDIRECT_URI)


# ---------- database ----------

def get_db():
    return _get_db()


def db_one(sql, args=()):
    conn = get_db()

    try:
        cur = get_cursor(conn)
        cur.execute(convert_placeholders(sql), args)
        return cur.fetchone()
    finally:
        conn.close()


def db_run(sql, args=()):
    conn = get_db()

    try:
        cur = conn.cursor()
        cur.execute(convert_placeholders(sql), args)
        conn.commit()
        return cur
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def init_db():
    conn = get_db()

    try:
        cur = conn.cursor()

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id BIGSERIAL PRIMARY KEY,
                username TEXT NOT NULL UNIQUE,
                email TEXT NOT NULL UNIQUE,
                password_hash TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )

        cur.execute(
            """
            CREATE TABLE IF NOT EXISTS password_resets (
                id BIGSERIAL PRIMARY KEY,
                user_id BIGINT NOT NULL,
                token_hash TEXT NOT NULL,
                expires_at TEXT NOT NULL,
                used INTEGER NOT NULL DEFAULT 0
            )
            """
        )

        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


init_db()


# ---------- helpers ----------

def _csrf_token():
    if "_csrf" not in session:
        session["_csrf"] = secrets.token_hex(16)

    return session["_csrf"]


app.jinja_env.globals["csrf_token"] = _csrf_token


def _check_csrf():
    token = session.get("_csrf", "")
    sent = request.form.get("_csrf", "")

    if not token or not secrets.compare_digest(
        token.encode(),
        sent.encode(),
    ):
        flash(
            "Your session expired. Please try again.",
            "error",
        )

        referrer = request.referrer or ""

        target = (
            referrer
            if referrer.startswith(request.host_url)
            else "/"
        )

        abort(redirect(target))


def _safe_next(target):
    if (
        target
        and target.startswith("/")
        and not target.startswith("//")
        and "\\" not in target
    ):
        return target

    return "/"


def login_required(view):
    @wraps(view)
    def wrapped(*args, **kwargs):
        if not session.get("user_id"):
            return redirect(
                url_for("login", next=request.path)
            )

        return view(*args, **kwargs)

    return wrapped


def _validate_password(password, confirm):
    if not (
        PASSWORD_MIN <= len(password) <= PASSWORD_MAX
    ):
        return (
            f"Password must be "
            f"{PASSWORD_MIN}-{PASSWORD_MAX} characters."
        )

    if password != confirm:
        return "Passwords do not match."

    return None


def _close_user_terminals(uid):
    with terminal_lock:
        keys = [
            key
            for key, value in terminal_sessions.items()
            if value.get("uid") == uid
        ]

        closing = [
            terminal_sessions.pop(key)
            for key in keys
        ]

    for term in closing:
        for part in ("channel", "client"):
            try:
                term[part].close()
            except Exception:
                pass


def _create_user_session(user):
    session.clear()

    session.permanent = True
    session["session_id"] = secrets.token_urlsafe(32)
    session["user_id"] = int(user["id"])
    session["user"] = user["username"]
    session["_csrf"] = secrets.token_hex(16)
    session["login_time"] = datetime.now(
        timezone.utc
    ).isoformat()


def _send_reset_email(to_email, link):
    host = os.environ.get("MAIL_HOST")

    if not host:
        print(
            f"\n[Password reset] Link for {to_email}: {link}\n",
            flush=True,
        )
        return

    try:
        msg = EmailMessage()
        msg["Subject"] = "Reset your Learning Labs password"
        msg["From"] = os.environ.get(
            "MAIL_FROM",
            os.environ.get("MAIL_USER", "no-reply@learninglabs"),
        )
        msg["To"] = to_email

        msg.set_content(
            f"Use the link below to reset your password. "
            f"It expires in {RESET_MINUTES} minutes.\n\n"
            f"{link}\n\n"
            f"If you did not request this, "
            f"you can ignore this email."
        )

        with smtplib.SMTP(
            host,
            int(os.environ.get("MAIL_PORT", "587")),
        ) as server:
            server.starttls()

            mail_user = os.environ.get("MAIL_USER")

            if mail_user:
                server.login(
                    mail_user,
                    os.environ.get("MAIL_PASSWORD", ""),
                )

            server.send_message(msg)

    except Exception as exc:
        print(
            f"[Password reset] Email failed ({exc}). "
            f"Link: {link}",
            flush=True,
        )


def _find_valid_reset(token):
    token_hash = hashlib.sha256(
        token.encode()
    ).hexdigest()

    row = db_one(
        """
        SELECT *
        FROM password_resets
        WHERE token_hash = ? AND used = 0
        """,
        (token_hash,),
    )

    if not row:
        return None

    if (
        datetime.fromisoformat(row["expires_at"])
        < datetime.now(timezone.utc)
    ):
        return None

    return row


# ---------- Google login routes ----------

@app.route("/auth/google/callback")
def google_callback():
    if not GOOGLE_CLIENT_ID or not GOOGLE_CLIENT_SECRET:
        flash("Google authentication is not configured.", "error")
        return redirect(url_for("login"))

    try:
        token = oauth.google.authorize_access_token()
        user_info = token.get("userinfo")

        if not user_info:
            user_info = oauth.google.userinfo()

        email = (user_info.get("email") or "").strip().lower()
        email_verified = user_info.get("email_verified", False)
        google_name = (
            user_info.get("name")
            or email.split("@")[0]
        ).strip()

        if not email or email_verified is not True:
            flash("Google account email is not verified.", "error")
            return redirect(url_for("login"))

        if not EMAIL_RE.match(email):
            flash("Google returned an invalid email address.", "error")
            return redirect(url_for("login"))

        conn = get_db()

        try:
            cur = conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor
            )

            cur.execute(
                """
                SELECT id, username, email
                FROM users
                WHERE LOWER(email) = %s
                """,
                (email,),
            )

            user = cur.fetchone()

            if not user:
                base_username = re.sub(
                    r"[^a-zA-Z0-9_.-]",
                    "",
                    email.split("@")[0],
                )[:30] or "googleuser"

                username = base_username
                suffix = 1

                while True:
                    cur.execute(
                        "SELECT id FROM users WHERE username = %s",
                        (username,),
                    )

                    if not cur.fetchone():
                        break

                    suffix_text = str(suffix)
                    username = (
                        base_username[:30 - len(suffix_text)]
                        + suffix_text
                    )
                    suffix += 1

                cur.execute(
                    """
                    INSERT INTO users
                    (username, email, password_hash, created_at)
                    VALUES (%s, %s, %s, %s)
                    RETURNING id, username, email
                    """,
                    (
                        username,
                        email,
                        generate_password_hash(
                            secrets.token_urlsafe(48)
                        ),
                        datetime.now(timezone.utc).isoformat(),
                    ),
                )

                user = cur.fetchone()
                conn.commit()

        except Exception:
            conn.rollback()
            raise

        finally:
            conn.close()

        next_url = session.get("google_next", "/")
        _create_user_session(user)

        return redirect(_safe_next(next_url))

    except Exception:
        app.logger.exception("Google authentication failed")
        flash("Google login failed. Please try again.", "error")
        return redirect(url_for("login"))


# ---------- socket sessions ----------

@socketio.on("join_slot")
def join_slot(*args):
    session_id = session.get("session_id")

    if session_id and session.get("user_id"):
        join_room("session-" + session_id)


# ---------- stale-session protection ----------

@app.before_request
def reject_stale_session():
    if not request.path.startswith("/api/"):
        return None

    page_uid = request.headers.get("X-Page-Uid")
    uid = session.get("user_id")

    if page_uid and str(uid or "") != page_uid:
        resp = jsonify({
            "success": False,
            "error": "Your account session is no longer valid.",
        })

        resp.status_code = 409
        resp.headers["X-Account-Changed"] = "1"

        return resp

    return None


# ---------- session guard injection ----------

@app.after_request
def inject_session_guard(resp):
    if (
        request.path.startswith("/api/")
        or resp.mimetype == "text/html"
    ):
        resp.headers["Cache-Control"] = "no-store"

    if (
        session.get("user_id")
        and resp.status_code == 200
        and resp.mimetype == "text/html"
        and not resp.direct_passthrough
    ):
        body = resp.get_data(as_text=True)

        if "session-guard" not in body:
            tag = (
                '<script id="session-guard" '
                'data-uid="%d" '
                'data-session="%s" '
                'src="/static/session.js?v=%d"></script>'
                % (
                    int(session["user_id"]),
                    session.get("session_id", ""),
                    SESSION_JS_VERSION,
                )
            )

            new_body, count = re.subn(
                r"<head[^>]*>",
                lambda match: match.group(0) + tag,
                body,
                count=1,
                flags=re.I,
            )

            if count:
                resp.set_data(new_body)

    return resp

