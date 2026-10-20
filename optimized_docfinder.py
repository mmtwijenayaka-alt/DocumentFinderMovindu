r"""
Document Finder - internal web server (same style as Spool Finder)
- Add many folders (local drive or company server \\server\share\...) -> they are indexed
- Type a document number -> matching files appear while you type
- "More" opens a Details tab (path, size, date, PDF pages / paper size, other revisions, preview)
- Select documents -> Merge PDF / Merge + Compress / Compress / ZIP / original files
- Download with "choose location" (Chrome/Edge), or send by Email, Teams, WhatsApp
- "Required documents": paste a list of numbers -> see which are found / missing
Run:   python docfinder.py     then open  http://<this-pc-ip>:5001
Install: pip install flask waitress pypdf pymupdf        (Ghostscript is optional - best compression)
First login: user Movindu / ChangeMe123  (change it after login)
"""
import io, os, re, sys, sqlite3, socket, threading, subprocess, shutil, time, secrets, smtplib
import zipfile, mimetypes, json, glob, urllib.request
from functools import wraps
from urllib.parse import quote
from email.message import EmailMessage
from datetime import datetime, timezone, timedelta
from flask import Flask, request, jsonify, send_file, Response, session, redirect, g
from werkzeug.security import generate_password_hash, check_password_hash

BASE = os.path.dirname(os.path.abspath(__file__))
# Keep runtime data separate from the code when DOCFINDER_DATA_DIR is set.
# Without it, preserve the original portable behavior: data sits beside this script.
DATA_DIR = os.path.abspath(os.environ.get("DOCFINDER_DATA_DIR") or BASE)
os.makedirs(DATA_DIR, exist_ok=True)
DB = os.path.join(DATA_DIR, "docfinder.db")
OUT = os.path.join(DATA_DIR, "outputs")          # merged / compressed / zip files waiting for download
os.makedirs(OUT, exist_ok=True)
TZ = timezone(timedelta(hours=4))            # Abu Dhabi time
PORT = int(os.environ.get("PORT", 5001))     # 5001 so it can run next to Spool Finder (5000)
MAIN_USER = "Movindu"                        # main user: always Admin, can never be deleted
MAIN_DEFAULT_PASSWORD = os.environ.get("DF_ADMIN_PASSWORD", "ChangeMe123")   # used only when the DB is first created
SHARE_HOURS = 72
TEMP_HOURS = 2

# folders added automatically the first time the database is created (add more in the Folders tab)
DEFAULT_FOLDERS = [
    ("Meram - Job 290092", r"\\Abdfabfs01\mcd$\Materials Dept\Materials\Ongoing Projects\Job # 290092 - Meram (JV NPCC - TR)"),
]
SKIP_NAMES = {"thumbs.db", "desktop.ini", ".ds_store"}

app = Flask(__name__)
app.config["PERMANENT_SESSION_LIFETIME"] = timedelta(days=7)
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
FAILS = {}


# ============================================================
#  DATABASE
# ============================================================
def now():
    return datetime.now(TZ).strftime("%Y-%m-%d %H:%M:%S")


def db():
    con = sqlite3.connect(DB, timeout=60)
    con.row_factory = sqlite3.Row
    return con


def init_db():
    con = db()
    # Set WAL once at startup; repeating journal_mode on every request adds lock/IO overhead.
    con.execute("PRAGMA journal_mode=WAL")
    con.executescript("""
    CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
    CREATE TABLE IF NOT EXISTS users(
        id INTEGER PRIMARY KEY AUTOINCREMENT, username TEXT NOT NULL UNIQUE COLLATE NOCASE, pw_hash TEXT NOT NULL,
        role TEXT NOT NULL DEFAULT 'user', is_main INTEGER NOT NULL DEFAULT 0, created_at TEXT, created_by TEXT, last_login TEXT);
    CREATE TABLE IF NOT EXISTS folders(
        id INTEGER PRIMARY KEY AUTOINCREMENT, label TEXT, path TEXT NOT NULL UNIQUE, enabled INTEGER NOT NULL DEFAULT 1,
        last_scan TEXT, file_count INTEGER NOT NULL DEFAULT 0, error TEXT, added_by TEXT, added_at TEXT);
    CREATE TABLE IF NOT EXISTS files(
        id INTEGER PRIMARY KEY AUTOINCREMENT, folder_id INTEGER NOT NULL, path TEXT, rel TEXT, name TEXT, stem TEXT,
        norm TEXT, normpath TEXT, ext TEXT, size INTEGER, mtime INTEGER, seen INTEGER);
    CREATE UNIQUE INDEX IF NOT EXISTS i_files_rel ON files(folder_id, rel);
    CREATE INDEX IF NOT EXISTS i_files_norm ON files(norm);
    CREATE INDEX IF NOT EXISTS i_files_folder_norm_mtime ON files(folder_id,norm,mtime DESC);
    CREATE TABLE IF NOT EXISTS outputs(
        token TEXT PRIMARY KEY, path TEXT, filename TEXT, created_at TEXT, expires_at INTEGER, created_by TEXT, keep INTEGER DEFAULT 0);
    CREATE TABLE IF NOT EXISTS activity(
        id INTEGER PRIMARY KEY AUTOINCREMENT, at TEXT, by_user TEXT, action TEXT, detail TEXT);
    """)
    if not con.execute("SELECT 1 FROM users WHERE is_main=1").fetchone():
        ex = con.execute("SELECT id FROM users WHERE username=?", (MAIN_USER,)).fetchone()
        if ex:
            con.execute("UPDATE users SET is_main=1, role='admin' WHERE id=?", (ex["id"],))
        else:
            con.execute("INSERT INTO users(username,pw_hash,role,is_main,created_at,created_by) VALUES(?,?,?,?,?,?)",
                        (MAIN_USER, generate_password_hash(MAIN_DEFAULT_PASSWORD, method="pbkdf2:sha256"), "admin", 1, now(), "system"))
    if not con.execute("SELECT 1 FROM meta WHERE key='folders_seeded'").fetchone():
        for label, path in DEFAULT_FOLDERS:
            con.execute("INSERT OR IGNORE INTO folders(label,path,added_by,added_at) VALUES(?,?,?,?)", (label, path, "system", now()))
        con.execute("INSERT INTO meta VALUES('folders_seeded','1')")
    con.commit()
    con.close()


def meta():
    con = db()
    m = {r["key"]: r["value"] for r in con.execute("SELECT key,value FROM meta")}
    con.close()
    return m


def set_meta(pairs):
    con = db()
    with con:
        for k, v in pairs.items():
            con.execute("INSERT OR REPLACE INTO meta VALUES(?,?)", (k, str(v)))
    con.close()


def load_secret():
    m = meta()
    if m.get("secret_key"):
        return m["secret_key"]
    v = secrets.token_hex(32)
    set_meta({"secret_key": v})
    return v


def log(action, detail=""):
    try:
        con = db()
        with con:
            con.execute("INSERT INTO activity(at,by_user,action,detail) VALUES(?,?,?,?)", (now(), uname(), action, str(detail)[:500]))
        con.close()
    except Exception:
        pass


def norm(s):
    """lower-case, only letters+digits: '290092-ISO_0123 Rev.1' -> '290092iso0123rev1'"""
    return re.sub(r"[\W_]+", "", str(s).lower())


EXT_RE = re.compile(r"\.(pdf|dwg|dxf|xlsx?|docx?|zip|tiff?|jpe?g|png|txt|csv)$", re.I)


def strip_ext(s):
    """'290092-ISO-0101.pdf' -> '290092-ISO-0101' (people often paste names with the extension)"""
    return EXT_RE.sub("", str(s).strip())


def fmt_time(ts):
    try:
        return datetime.fromtimestamp(int(ts), TZ).strftime("%Y-%m-%d %H:%M")
    except Exception:
        return ""


def safe_name(s, default="file"):
    s = re.sub(r'[\\/:*?"<>|\r\n\t]+', "_", str(s or "")).strip(" .")
    return s[:150] or default


# ============================================================
#  LOGIN / USERS  (same behaviour as Spool Finder)
# ============================================================
def uname():
    u = getattr(g, "user", None)
    return u["username"] if u else ""


def current_user():
    return getattr(g, "user", None)


def admin_required(fn):
    @wraps(fn)
    def wrapper(*a, **k):
        u = current_user()
        if not u or u["role"] != "admin":
            return jsonify(ok=False, error="Admin access required."), 403
        return fn(*a, **k)
    return wrapper


def pw_problem(p):
    if len(p) < 6:
        return "Password must be at least 6 characters."
    if len(p) > 100:
        return "Password is too long."
    return ""


@app.before_request
def need_login():
    p = request.path
    if p in ("/login", "/api/login", "/favicon.ico") or p.startswith("/share/"):
        return None
    row = None
    uid = session.get("uid")
    if uid:
        con = db()
        row = con.execute("SELECT id,username,role,is_main,pw_hash FROM users WHERE id=?", (uid,)).fetchone()
        con.close()
        if row and session.get("pv") != row["pw_hash"][-16:]:
            row = None
    if row:
        g.user = row
        return None
    session.clear()
    if p.startswith("/api/"):
        return jsonify(ok=False, error="Please log in again."), 401
    return redirect("/login")


@app.get("/login")
def login_page():
    return Response(LOGIN_PAGE, mimetype="text/html")


@app.post("/api/login")
def api_login():
    d = request.get_json(silent=True) or {}
    name = str(d.get("username", "")).strip()
    pw = str(d.get("password", ""))
    key = (request.headers.get("CF-Connecting-IP") or request.remote_addr or "", name.lower())
    t0 = time.time()
    tries = [t for t in FAILS.get(key, []) if t0 - t < 300]
    if len(tries) >= 5:
        return jsonify(ok=False, error="Too many wrong attempts. Wait 5 minutes and try again."), 429
    con = db()
    u = con.execute("SELECT * FROM users WHERE username=?", (name,)).fetchone()
    if not u or not check_password_hash(u["pw_hash"], pw):
        con.close()
        tries.append(t0)
        FAILS[key] = tries
        return jsonify(ok=False, error="Wrong user name or password."), 401
    with con:
        con.execute("UPDATE users SET last_login=? WHERE id=?", (now(), u["id"]))
    con.close()
    FAILS.pop(key, None)
    session.clear()
    session["uid"] = u["id"]
    session["pv"] = u["pw_hash"][-16:]
    session.permanent = True
    return jsonify(ok=True)


@app.post("/api/logout")
def api_logout():
    session.clear()
    return jsonify(ok=True)


@app.get("/api/me")
def api_me():
    u = current_user()
    return jsonify(username=u["username"], role=u["role"], is_main=bool(u["is_main"]))


@app.post("/api/change_password")
def api_change_password():
    d = request.get_json(silent=True) or {}
    u = current_user()
    con = db()
    row = con.execute("SELECT pw_hash FROM users WHERE id=?", (u["id"],)).fetchone()
    if not check_password_hash(row["pw_hash"], str(d.get("old", ""))):
        con.close()
        return jsonify(ok=False, error="Your current password is wrong."), 400
    new = str(d.get("new", ""))
    bad = pw_problem(new)
    if bad:
        con.close()
        return jsonify(ok=False, error=bad), 400
    h = generate_password_hash(new, method="pbkdf2:sha256")
    with con:
        con.execute("UPDATE users SET pw_hash=? WHERE id=?", (h, u["id"]))
    con.close()
    session["pv"] = h[-16:]
    return jsonify(ok=True)


@app.get("/api/users")
@admin_required
def api_users():
    con = db()
    rows = [dict(id=r["id"], username=r["username"], role=r["role"], is_main=bool(r["is_main"]), last_login=r["last_login"] or "")
            for r in con.execute("SELECT * FROM users ORDER BY is_main DESC, username")]
    con.close()
    return jsonify(ok=True, users=rows)


@app.post("/api/users")
@admin_required
def api_user_create():
    d = request.get_json(silent=True) or {}
    name = str(d.get("username", "")).strip()
    role = "admin" if d.get("role") == "admin" else "user"
    if not re.fullmatch(r"[A-Za-z0-9_. -]{2,40}", name):
        return jsonify(ok=False, error="User name: 2-40 letters, numbers, space, . _ -"), 400
    bad = pw_problem(str(d.get("password", "")))
    if bad:
        return jsonify(ok=False, error=bad), 400
    con = db()
    try:
        with con:
            con.execute("INSERT INTO users(username,pw_hash,role,created_at,created_by) VALUES(?,?,?,?,?)",
                        (name, generate_password_hash(str(d["password"]), method="pbkdf2:sha256"), role, now(), uname()))
    except sqlite3.IntegrityError:
        con.close()
        return jsonify(ok=False, error="That user name already exists."), 400
    con.close()
    log("user created", name)
    return jsonify(ok=True)


def _target(con, uid):
    return con.execute("SELECT * FROM users WHERE id=?", (uid,)).fetchone()


@app.post("/api/users/<int:uid>/role")
@admin_required
def api_user_role(uid):
    d = request.get_json(silent=True) or {}
    con = db()
    t = _target(con, uid)
    if not t or t["is_main"]:
        con.close()
        return jsonify(ok=False, error=f"The main user ({MAIN_USER}) is always Admin."), 400
    with con:
        con.execute("UPDATE users SET role=? WHERE id=?", ("admin" if d.get("role") == "admin" else "user", uid))
    con.close()
    return jsonify(ok=True)


@app.post("/api/users/<int:uid>/password")
@admin_required
def api_user_reset_password(uid):
    d = request.get_json(silent=True) or {}
    bad = pw_problem(str(d.get("password", "")))
    if bad:
        return jsonify(ok=False, error=bad), 400
    con = db()
    with con:
        con.execute("UPDATE users SET pw_hash=? WHERE id=?", (generate_password_hash(str(d["password"]), method="pbkdf2:sha256"), uid))
    con.close()
    return jsonify(ok=True)


@app.post("/api/users/<int:uid>/delete")
@admin_required
def api_user_delete(uid):
    con = db()
    t = _target(con, uid)
    if not t or t["is_main"] or t["id"] == current_user()["id"]:
        con.close()
        return jsonify(ok=False, error="This user cannot be deleted."), 400
    with con:
        con.execute("DELETE FROM users WHERE id=?", (uid,))
    con.close()
    return jsonify(ok=True)


# ============================================================
#  FOLDER INDEX  (scan folders -> files table)
# ============================================================
SCAN = {"state": "idle", "folder": "", "count": 0, "message": "", "last_ok": "", "next": ""}
scan_lock = threading.Lock()

UPSERT = """INSERT INTO files(folder_id,path,rel,name,stem,norm,normpath,ext,size,mtime,seen) VALUES(?,?,?,?,?,?,?,?,?,?,?)
ON CONFLICT(folder_id,rel) DO UPDATE SET path=excluded.path,name=excluded.name,stem=excluded.stem,norm=excluded.norm,
normpath=excluded.normpath,ext=excluded.ext,size=excluded.size,mtime=excluded.mtime,seen=excluded.seen
WHERE files.path IS NOT excluded.path OR files.name IS NOT excluded.name OR files.stem IS NOT excluded.stem
   OR files.norm IS NOT excluded.norm OR files.normpath IS NOT excluded.normpath OR files.ext IS NOT excluded.ext
   OR files.size IS NOT excluded.size OR files.mtime IS NOT excluded.mtime"""


def clean_path(p):
    p = str(p or "").strip().strip('"').strip("'").strip()
    if len(p) > 3:
        p = p.rstrip("\\/") if not re.fullmatch(r"[A-Za-z]:[\\/]?", p) else p
    return p


def scan_folder(fr):
    fid, root = fr["id"], fr["path"]
    con = db()
    if not os.path.isdir(root):
        with con:
            con.execute("UPDATE folders SET error=?, last_scan=? WHERE id=?",
                        ("Cannot open this folder from the server PC (path wrong, or this PC/account has no access).", now(), fid))
        con.close()
        return 0
    stamp = int(time.time() * 1000)
    # Track observed paths separately so unchanged file rows do not need a seen-marker update.
    con.execute("CREATE TEMP TABLE scan_seen(folder_id INTEGER, rel TEXT, PRIMARY KEY(folder_id,rel)) WITHOUT ROWID")
    stack, batch, n, bad_dirs = [root], [], 0, []
    SCAN["folder"] = fr["label"] or root

    def flush():
        if not batch:
            return
        with con:
            con.executemany(UPSERT, batch)
            con.executemany("INSERT OR IGNORE INTO scan_seen(folder_id,rel) VALUES(?,?)",
                            ((row[0], row[2]) for row in batch))
        batch.clear()

    while stack:
        d = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            bad_dirs.append(d)
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        stack.append(e.path)
                        continue
                    if not e.is_file():
                        continue
                    nm = e.name
                    low = nm.lower()
                    if nm.startswith("~$") or low in SKIP_NAMES or low.endswith(".tmp"):
                        continue
                    st = e.stat()
                    stem, dot, ext = nm.rpartition(".")
                    if not dot:
                        stem, ext = nm, ""
                    rel = os.path.relpath(e.path, root).replace("\\", "/")
                    batch.append((fid, e.path, rel, nm, stem, norm(stem), norm(rel), ext.lower(), st.st_size, int(st.st_mtime), stamp))
                    n += 1
                except OSError:
                    continue
        if len(batch) >= 2000:
            flush()
            SCAN["count"] = n
    if batch:
        flush()
    SCAN["count"] = n
    # Delete stale rows in SQL instead of loading all indexed paths into Python.
    # Preserve rows under any sub-folder that could not be read during this scan.
    delete_sql = ("DELETE FROM files WHERE folder_id=? AND NOT EXISTS ("
                  "SELECT 1 FROM scan_seen s WHERE s.folder_id=files.folder_id AND s.rel=files.rel)")
    delete_args = [fid]
    if bad_dirs:
        preserve = []
        for bad in bad_dirs:
            prefix = os.path.join(bad, "")
            preserve.append("substr(path,1,?)=?")
            delete_args.extend((len(prefix), prefix))
        delete_sql += " AND NOT (" + " OR ".join(preserve) + ")"
    with con:
        con.execute(delete_sql, delete_args)
        err = f"{len(bad_dirs)} sub-folder(s) could not be read." if bad_dirs else ""
        total = con.execute("SELECT COUNT(*) FROM files WHERE folder_id=?", (fid,)).fetchone()[0]
        con.execute("UPDATE folders SET last_scan=?, file_count=?, error=? WHERE id=?", (now(), total, err, fid))
    con.close()
    return n


def scan_all(only_id=None):
    if not scan_lock.acquire(blocking=False):
        return False
    try:
        SCAN.update(state="scanning", message="Indexing folders...", count=0)
        con = db()
        q = "SELECT * FROM folders WHERE enabled=1" + (" AND id=?" if only_id else "")
        rows = con.execute(q, (only_id,) if only_id else ()).fetchall()
        con.close()
        for fr in rows:
            try:
                scan_folder(fr)
            except Exception as e:
                c = db()
                with c:
                    c.execute("UPDATE folders SET error=?, last_scan=? WHERE id=?", (f"Scan failed: {e}", now(), fr["id"]))
                c.close()
        SCAN.update(state="idle", last_ok=now(), message="Index is up to date.", folder="")
        return True
    finally:
        scan_lock.release()


def start_auto_scan():
    def loop():
        while True:
            try:
                iv = int(meta().get("scan_interval_min", "30") or 0)
            except ValueError:
                iv = 30
            if iv <= 0:
                SCAN["next"] = "off"
                time.sleep(60)
                continue
            scan_all()
            SCAN["next"] = (datetime.now(TZ) + timedelta(minutes=iv)).strftime("%Y-%m-%d %H:%M:%S")
            time.sleep(iv * 60)
    threading.Thread(target=loop, daemon=True).start()


@app.get("/api/folders")
def api_folders():
    con = db()
    rows = [dict(r) for r in con.execute("SELECT * FROM folders ORDER BY id")]
    con.close()
    return jsonify(ok=True, folders=rows, scan=SCAN, interval=int(meta().get("scan_interval_min", "30") or 0))


@app.post("/api/folders")
@admin_required
def api_folder_add():
    d = request.get_json(silent=True) or {}
    path = clean_path(d.get("path"))
    if not path:
        return jsonify(ok=False, error="Paste the folder path first."), 400
    if not os.path.isdir(path):
        return jsonify(ok=False, error="The server cannot open this folder. Check the path, and that the PC/account running this app has access to it."), 400
    label = str(d.get("label", "")).strip()[:80] or os.path.basename(path) or path
    con = db()
    try:
        with con:
            cur = con.execute("INSERT INTO folders(label,path,added_by,added_at) VALUES(?,?,?,?)", (label, path, uname(), now()))
    except sqlite3.IntegrityError:
        con.close()
        return jsonify(ok=False, error="This folder is already added."), 400
    fid = cur.lastrowid
    con.close()
    log("folder added", path)
    threading.Thread(target=scan_all, args=(fid,), daemon=True).start()
    return jsonify(ok=True, id=fid)


@app.post("/api/folders/<int:fid>/toggle")
@admin_required
def api_folder_toggle(fid):
    con = db()
    with con:
        con.execute("UPDATE folders SET enabled=1-enabled WHERE id=?", (fid,))
    con.close()
    return jsonify(ok=True)


@app.post("/api/folders/<int:fid>/delete")
@admin_required
def api_folder_delete(fid):
    con = db()
    with con:
        con.execute("DELETE FROM files WHERE folder_id=?", (fid,))
        con.execute("DELETE FROM folders WHERE id=?", (fid,))
    con.close()
    log("folder removed", fid)
    return jsonify(ok=True)


@app.post("/api/scan")
@admin_required
def api_scan():
    d = request.get_json(silent=True) or {}
    if SCAN["state"] != "idle":
        return jsonify(ok=False, error="An index scan is already running."), 409
    threading.Thread(target=scan_all, args=(d.get("id"),), daemon=True).start()
    return jsonify(ok=True)


# ============================================================
#  SEARCH / DETAILS / FILE ACCESS
# ============================================================
def doc_json(r):
    rel = r["rel"] or ""
    return dict(id=r["id"], name=r["name"], ext=r["ext"] or "", size=r["size"] or 0, mtime=fmt_time(r["mtime"]),
                folder=r["label"] if "label" in r.keys() else "", dir=rel.rpartition("/")[0])


@app.get("/api/meta")
def api_meta():
    con = db()
    total = con.execute("SELECT COUNT(*) FROM files f JOIN folders fo ON fo.id=f.folder_id WHERE fo.enabled=1").fetchone()[0]
    exts = [dict(ext=r[0] or "(none)", n=r[1]) for r in con.execute(
        "SELECT ext, COUNT(*) n FROM files f JOIN folders fo ON fo.id=f.folder_id WHERE fo.enabled=1 GROUP BY ext ORDER BY n DESC LIMIT 25")]
    folders = [dict(id=r["id"], label=r["label"]) for r in con.execute("SELECT id,label FROM folders WHERE enabled=1 ORDER BY label")]
    con.close()
    m, c = meta(), smtp_conf()
    return jsonify(ok=True, total=total, exts=exts, folders=folders, scan=SCAN,
                   smtp=bool(c["host"] and c["sender"]), teams_hook=bool(m.get("teams_webhook")),
                   max_attach_mb=int(m.get("max_attach_mb") or 20), gs=bool(find_gs()))


@app.get("/api/search")
def api_search():
    toks = [norm(t) for t in re.split(r"\s+", strip_ext(request.args.get("q", ""))) if norm(t)]
    if not toks:
        return jsonify(ok=True, items=[], total=0)
    exact = request.args.get("exact") == "1"
    col = "f.normpath" if request.args.get("inpath") == "1" else "f.norm"
    joined = "".join(toks)
    where, args = ["fo.enabled=1"], []
    if exact:
        where.append("f.norm=?")
        args.append(joined)
    else:
        for t in toks:
            where.append(f"{col} LIKE ?")
            args.append(f"%{t}%")
    exts = [e for e in re.split(r"[,\s]+", request.args.get("ext", "").lower()) if re.fullmatch(r"[a-z0-9]{1,8}", e)]
    if exts:
        where.append("f.ext IN (%s)" % ",".join("?" * len(exts)))
        args += exts
    fid = request.args.get("folder", type=int)
    if fid:
        where.append("f.folder_id=?")
        args.append(fid)
    limit = min(max(request.args.get("limit", 300, type=int), 1), 1000)
    w = " AND ".join(where)
    con = db()
    total = con.execute(f"SELECT COUNT(*) FROM files f JOIN folders fo ON fo.id=f.folder_id WHERE {w}", args).fetchone()[0]
    rows = con.execute(
        f"""SELECT f.*, fo.label FROM files f JOIN folders fo ON fo.id=f.folder_id WHERE {w}
            ORDER BY CASE WHEN f.norm=? THEN 0 WHEN f.norm LIKE ? THEN 1 ELSE 2 END, f.name COLLATE NOCASE LIMIT ?""",
        args + [joined, joined + "%", limit]).fetchall()
    con.close()
    return jsonify(ok=True, items=[doc_json(r) for r in rows], total=total)


def get_files(ids):
    """ids -> list of rows in the same order (only files that are inside an indexed folder)"""
    ids = [int(i) for i in ids if str(i).lstrip("-").isdigit()][:500]
    if not ids:
        return []
    con = db()
    rows = con.execute("SELECT f.*, fo.label, fo.path AS root FROM files f JOIN folders fo ON fo.id=f.folder_id WHERE f.id IN (%s)"
                       % ",".join("?" * len(ids)), ids).fetchall()
    con.close()
    by = {r["id"]: r for r in rows}
    return [by[i] for i in ids if i in by]


def inside_root(r):
    try:
        a, b = os.path.normcase(os.path.realpath(r["path"])), os.path.normcase(os.path.realpath(r["root"]))
        return os.path.commonpath([a, b]) == b
    except (ValueError, OSError):
        return False


def one_file(fid):
    rows = get_files([fid])
    if not rows or not inside_root(rows[0]) or not os.path.isfile(rows[0]["path"]):
        return None
    return rows[0]


@app.post("/api/docs")
def api_docs():
    ids = (request.get_json(silent=True) or {}).get("ids", [])
    rows = get_files(ids)
    ok = {r["id"]: r for r in rows if os.path.isfile(r["path"])}
    return jsonify(ok=True, items=[doc_json(r) for r in ok.values()], missing=[i for i in ids if i not in ok])


@app.get("/api/file/<int:fid>/download")
def api_file_download(fid):
    r = one_file(fid)
    if not r:
        return ("File not found (it may have been moved). Rescan the folders.", 404)
    log("download", r["name"])
    return send_file(r["path"], as_attachment=True, download_name=r["name"], conditional=True)


@app.get("/api/file/<int:fid>/view")
def api_file_view(fid):
    r = one_file(fid)
    if not r:
        return ("File not found.", 404)
    mt = mimetypes.guess_type(r["name"])[0] or "application/octet-stream"
    return send_file(r["path"], as_attachment=False, download_name=r["name"], mimetype=mt, conditional=True)


PAPER = {"A0": (841, 1189), "A1": (594, 841), "A2": (420, 594), "A3": (297, 420), "A4": (210, 297), "A5": (148, 210)}


def pdf_info(path):
    from pypdf import PdfReader
    r = PdfReader(path)
    if r.is_encrypted:
        try:
            r.decrypt("")
        except Exception:
            return {"encrypted": True}
    out = {"pages": len(r.pages)}
    try:
        p = r.pages[0]
        w, h = float(p.mediabox.width) * 25.4 / 72, float(p.mediabox.height) * 25.4 / 72
        paper = next((k for k, (a, b) in PAPER.items() if (abs(w - a) < 8 and abs(h - b) < 8) or (abs(w - b) < 8 and abs(h - a) < 8)), "")
        out["page_size"] = f"{round(w)} x {round(h)} mm" + (f" ({paper})" if paper else "")
    except Exception:
        pass
    try:
        md = r.metadata or {}
        for k, lab in (("/Title", "title"), ("/Author", "author"), ("/Creator", "creator")):
            if md.get(k):
                out[lab] = str(md.get(k))[:120]
    except Exception:
        pass
    return out


REV = re.compile(r"[\s_\-\.]+(rev(ision)?|r)[\s_\-\.]*[0-9a-z]{1,3}$", re.I)


@app.get("/api/file/<int:fid>/info")
def api_file_info(fid):
    r = one_file(fid)
    if not r:
        return jsonify(ok=False, error="File not found (it may have been moved). Rescan the folders."), 404
    st = os.stat(r["path"])
    info = dict(doc_json(r), path=r["path"], root=r["root"], created=fmt_time(st.st_ctime), mtime=fmt_time(st.st_mtime), size=st.st_size)
    if r["ext"] == "pdf":
        try:
            info["pdf"] = pdf_info(r["path"])
        except Exception as e:
            info["pdf"] = {"error": f"Could not read PDF details: {e}"}
    base = norm(REV.sub("", r["stem"] or ""))
    related = []
    if len(base) >= 5:
        con = db()
        rows = con.execute("""SELECT f.*, fo.label FROM files f JOIN folders fo ON fo.id=f.folder_id
                              WHERE fo.enabled=1 AND f.norm LIKE ? AND f.id<>? ORDER BY f.mtime DESC LIMIT 40""", (base + "%", fid)).fetchall()
        con.close()
        related = [doc_json(x) for x in rows]
    return jsonify(ok=True, info=info, related=related)


@app.post("/api/required")
def api_required():
    """Batch exact spool/document lookup; optional similar-name fallback is slower."""
    d = request.get_json(silent=True) or {}
    items = [str(x).strip() for x in d.get("items", []) if str(x).strip()][:500]
    exts = [e for e in re.split(r"[,\s]+", str(d.get("ext", "")).lower()) if re.fullmatch(r"[a-z0-9]{1,8}", e)]
    similar = bool(d.get("similar")) and not bool(d.get("exact"))
    ef, ea = "", []
    if exts:
        ef += " AND f.ext IN (%s)" % ",".join("?" * len(exts))
        ea += exts
    try:
        fid = int(d.get("folder") or 0)
    except (TypeError, ValueError):
        fid = 0
    if fid:
        ef += " AND f.folder_id=?"
        ea.append(fid)
    normalized = [norm(strip_ext(it)) for it in items]
    wanted = list(dict.fromkeys(n for n in normalized if len(n) >= 2))
    matches_by_norm = {}
    con = db()
    if wanted:
        marks = ",".join("?" * len(wanted))
        sql = f"""SELECT * FROM (
                    SELECT f.*, fo.label,
                           ROW_NUMBER() OVER (PARTITION BY f.norm ORDER BY f.mtime DESC) AS _rank
                    FROM files f JOIN folders fo ON fo.id=f.folder_id
                    WHERE fo.enabled=1 AND f.norm IN ({marks}){ef}
                  ) WHERE _rank<=8 ORDER BY norm, mtime DESC"""
        for r in con.execute(sql, wanted + ea):
            matches_by_norm.setdefault(r["norm"], []).append(r)
    results = []
    for it, n in zip(items, normalized):
        rows = matches_by_norm.get(n, []) if len(n) >= 2 else []
        exact = bool(rows)
        if not rows and similar and len(n) >= 2:
            rows = con.execute(
                f"SELECT f.*, fo.label FROM files f JOIN folders fo ON fo.id=f.folder_id "
                f"WHERE fo.enabled=1 AND f.norm LIKE ?{ef} "
                "ORDER BY (f.norm LIKE ?) DESC, f.mtime DESC LIMIT 8",
                [f"%{n}%"] + ea + [n + "%"]
            ).fetchall()
        results.append(dict(q=it, status="found" if rows and exact else ("similar" if rows else "missing"),
                            matches=[doc_json(x) for x in rows]))
    con.close()
    return jsonify(ok=True, results=results)



#  OUTPUTS: merge / compress / zip   (+ temporary download links)
# ============================================================
def find_gs():
    for n in ("gswin64c", "gswin32c", "gs"):
        p = shutil.which(n)
        if p:
            return p
    for pat in (r"C:\Program Files\gs\gs*\bin\gswin64c.exe", r"C:\Program Files (x86)\gs\gs*\bin\gswin32c.exe"):
        hits = sorted(glob.glob(pat))
        if hits:
            return hits[-1]
    return ""


LEVELS = {"low": ("/printer", 200, 150, 85), "medium": ("/ebook", 150, 110, 70), "high": ("/screen", 110, 72, 50)}


def compress_pdf(src, dst, level="medium"):
    """Returns a short text saying how it was compressed. If nothing was gained the original is copied."""
    pdfset, thr, tgt, q = LEVELS.get(level, LEVELS["medium"])
    method = ""
    gs = find_gs()
    if gs:
        try:
            cmd = [gs, "-sDEVICE=pdfwrite", "-dCompatibilityLevel=1.5", f"-dPDFSETTINGS={pdfset}", "-dNOPAUSE", "-dQUIET", "-dBATCH",
                   "-dSAFER", f"-sOutputFile={dst}", src]
            subprocess.run(cmd, check=True, timeout=900, capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
            method = "Ghostscript"
        except Exception:
            method = ""
    if not method:
        try:
            import fitz
            doc = fitz.open(src)
            try:
                doc.rewrite_images(dpi_threshold=thr, dpi_target=tgt, quality=q, bitonal=False)   # keeps scanned line drawings sharp
            except Exception:
                pass
            doc.save(dst, garbage=4, deflate=True, clean=True)
            doc.close()
            method = "PyMuPDF"
        except Exception:
            method = ""
    if not method:
        try:
            from pypdf import PdfReader, PdfWriter
            r = PdfReader(src)
            try:
                w = PdfWriter(clone_from=r)
            except TypeError:
                w = PdfWriter()
                w.append(r)
            for p in w.pages:
                p.compress_content_streams()
                if level != "low":
                    try:
                        for im in p.images:
                            im.replace(im.image, quality=q)
                    except Exception:
                        pass
            try:
                w.compress_identical_objects(remove_identicals=True, remove_orphans=True)
            except Exception:
                pass
            with open(dst, "wb") as fh:
                w.write(fh)
            method = "pypdf"
        except Exception as e:
            shutil.copyfile(src, dst)
            return f"could not compress ({e}); original kept"
    if os.path.getsize(dst) >= os.path.getsize(src):
        shutil.copyfile(src, dst)
        return method + " - no size gain, original kept"
    return method


def merge_pdfs(rows, dst, warns):
    from pypdf import PdfReader, PdfWriter
    w = PdfWriter()
    used = 0
    for r in rows:
        try:
            rd = PdfReader(r["path"])
            if rd.is_encrypted and not rd.decrypt(""):
                warns.append(f"{r['name']}: password protected - skipped")
                continue
            w.append(rd, outline_item=r["stem"])         # one bookmark per document
            used += 1
        except Exception as e:
            warns.append(f"{r['name']}: could not be read ({str(e)[:60]}) - skipped")
    if not used:
        raise ValueError("None of the selected PDFs could be merged.")
    with open(dst, "wb") as fh:
        w.write(fh)
    return used, len(w.pages)


def zip_files(pairs, dst):
    seen = {}
    with zipfile.ZipFile(dst, "w", zipfile.ZIP_DEFLATED) as z:
        for path, name in pairs:
            k = name.lower()
            if k in seen:
                seen[k] += 1
                stem, dot, ext = name.rpartition(".")
                name = f"{stem or name} ({seen[k]})" + (dot + ext if dot and stem else "")
            else:
                seen[k] = 1
            skip = name.lower().endswith((".pdf", ".zip", ".jpg", ".jpeg", ".png"))
            z.write(path, name, compress_type=zipfile.ZIP_STORED if skip else zipfile.ZIP_DEFLATED)


def clean_outputs():
    con = db()
    t = int(time.time())
    with con:
        for r in con.execute("SELECT token,path,keep FROM outputs WHERE expires_at<?", (t,)).fetchall():
            if not r["keep"]:
                try:
                    os.remove(r["path"])
                except OSError:
                    pass
        con.execute("DELETE FROM outputs WHERE expires_at<?", (t,))
    con.close()


def register(path, filename, keep=False, hours=TEMP_HOURS):
    token = secrets.token_urlsafe(24)
    con = db()
    with con:
        con.execute("INSERT INTO outputs(token,path,filename,created_at,expires_at,created_by,keep) VALUES(?,?,?,?,?,?,?)",
                    (token, path, filename, now(), int(time.time()) + hours * 3600, uname(), 1 if keep else 0))
    con.close()
    return dict(token=token, name=filename, size=os.path.getsize(path), url=f"/share/{token}/{quote(filename)}")


def want_name(name, default, ext):
    n = safe_name(name, "") if name else ""
    if not n:
        return default
    return n if n.lower().endswith("." + ext) else n + "." + ext


@app.post("/api/process")
def api_process():
    d = request.get_json(silent=True) or {}
    action = d.get("action", "original")
    level = d.get("level", "medium") if d.get("level") in LEVELS else "medium"
    zip_it = bool(d.get("zip"))
    rows = [r for r in get_files(d.get("ids", [])) if inside_root(r)]
    warns = []
    missing = [r["name"] for r in rows if not os.path.isfile(r["path"])]
    rows = [r for r in rows if os.path.isfile(r["path"])]
    if missing:
        warns.append(f"{len(missing)} file(s) no longer exist and were skipped: " + ", ".join(missing[:5]))
    if not rows:
        return jsonify(ok=False, error="No selected file could be found. Rescan the folders and select again."), 400
    clean_outputs()
    stamp = datetime.now(TZ).strftime("%Y%m%d_%H%M")
    outs, info = [], ""
    orig = sum(r["size"] or 0 for r in rows)
    try:
        if action == "zip" or (action == "original" and zip_it and len(rows) > 1):
            name = want_name(d.get("name"), f"Documents_{stamp}.zip", "zip")
            tmp = os.path.join(OUT, secrets.token_hex(8) + ".zip")
            zip_files([(r["path"], r["name"]) for r in rows], tmp)
            outs.append(register(tmp, name))
            info = f"{len(rows)} file(s) zipped."
        elif action == "original":
            for r in rows:
                outs.append(register(r["path"], r["name"], keep=True))
            info = f"{len(rows)} original file(s)."
        elif action in ("merge", "merge_compress"):
            pdfs = [r for r in rows if r["ext"] == "pdf"]
            if len(rows) != len(pdfs):
                warns.append(f"{len(rows) - len(pdfs)} non-PDF file(s) skipped (only PDFs can be merged).")
            if not pdfs:
                return jsonify(ok=False, error="Select at least one PDF to merge."), 400
            comp = action == "merge_compress"
            name = want_name(d.get("name"), f"Merged_{stamp}.pdf", "pdf")
            tmp = os.path.join(OUT, secrets.token_hex(8) + ".pdf")
            used, pages = merge_pdfs(pdfs, tmp, warns)
            before = os.path.getsize(tmp)
            info = f"Merged {used} PDF(s) = {pages} pages."
            if comp:
                tmp2 = tmp + ".c.pdf"
                how = compress_pdf(tmp, tmp2, level)
                os.replace(tmp2, tmp)
                after = os.path.getsize(tmp)
                info += f" Compressed {before / 1048576:.1f} MB -> {after / 1048576:.1f} MB ({how})."
            outs.append(register(tmp, name))
        elif action == "compress":
            pdfs = [r for r in rows if r["ext"] == "pdf"]
            if len(rows) != len(pdfs):
                warns.append(f"{len(rows) - len(pdfs)} non-PDF file(s) skipped (only PDFs can be compressed).")
            if not pdfs:
                return jsonify(ok=False, error="Select at least one PDF to compress."), 400
            done, before, after = [], 0, 0
            for r in pdfs:
                tmp = os.path.join(OUT, secrets.token_hex(8) + ".pdf")
                how = compress_pdf(r["path"], tmp, level)
                before += r["size"] or 0
                after += os.path.getsize(tmp)
                done.append((tmp, r["stem"] + "_compressed.pdf"))
            info = f"Compressed {len(done)} PDF(s): {before / 1048576:.1f} MB -> {after / 1048576:.1f} MB ({how})."
            if zip_it and len(done) > 1:
                tmp = os.path.join(OUT, secrets.token_hex(8) + ".zip")
                zip_files([(p, n) for p, n in done], tmp)
                for p, _ in done:
                    os.remove(p)
                outs.append(register(tmp, want_name(d.get("name"), f"Compressed_{stamp}.zip", "zip")))
            else:
                for p, n in done:
                    outs.append(register(p, want_name(d.get("name"), n, "pdf") if len(done) == 1 else n))
        else:
            return jsonify(ok=False, error="Unknown action."), 400
    except Exception as e:
        return jsonify(ok=False, error=f"Could not prepare the files: {e}"), 400
    if action == "original" and len(outs) == 1 and d.get("name"):
        outs[0]["name"] = safe_name(d["name"], outs[0]["name"])
    log(action, f"{len(rows)} file(s): " + ", ".join(r["name"] for r in rows[:4]) + (" ..." if len(rows) > 4 else ""))
    return jsonify(ok=True, outputs=outs, info=info, warnings=warns, original_size=orig)


@app.get("/api/output/<token>/view")
def api_output_view(token):
    """Inline view of a prepared (merged / compressed) file - used by Preview and Print. Login required."""
    con = db()
    r = con.execute("SELECT path,filename FROM outputs WHERE token=? AND expires_at>=?", (token, int(time.time()))).fetchone()
    con.close()
    if not r or not os.path.exists(r["path"]):
        return ("This prepared file expired. Press Preview / Print again.", 404)
    mt = mimetypes.guess_type(r["filename"])[0] or "application/octet-stream"
    return send_file(r["path"], as_attachment=False, download_name=r["filename"], mimetype=mt, conditional=True)


@app.get("/share/<token>/<path:name>")
def share_download(token, name):
    con = db()
    r = con.execute("SELECT path,filename FROM outputs WHERE token=? AND expires_at>=?", (token, int(time.time()))).fetchone()
    con.close()
    if not r or not os.path.exists(r["path"]):
        return ("This download link has expired or is not valid.", 404)
    return send_file(r["path"], as_attachment=True, download_name=r["filename"], conditional=True)


# ============================================================
#  SEND: link / email / Teams / WhatsApp
# ============================================================
TUNNEL = {"url": ""}


def start_tunnel():
    """Free Cloudflare quick tunnel (public https link) if cloudflared is present - needed for 'choose location' on phones and for links outside the company."""
    if os.environ.get("NO_TUNNEL"):
        return
    exe = os.path.join(BASE, "cloudflared.exe")
    if not os.path.exists(exe):
        exe = shutil.which("cloudflared")
    if not exe:
        print("  (cloudflared not found - public https link is off)")
        return

    def run():
        while True:
            try:
                p = subprocess.Popen([exe, "tunnel", "--url", f"http://localhost:{PORT}", "--no-autoupdate"], stdout=subprocess.PIPE,
                                     stderr=subprocess.STDOUT, text=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
                for line in p.stdout:
                    m = re.search(r"https://(?!api\.)[a-z0-9-]+\.trycloudflare\.com", line)
                    if m and m.group(0) != TUNNEL["url"]:
                        TUNNEL["url"] = m.group(0)
                        print(f"\n  PUBLIC LINK (https): {TUNNEL['url']}\n")
                p.wait()
            except Exception as e:
                print("tunnel error:", e)
            TUNNEL["url"] = ""
            time.sleep(5)
    threading.Thread(target=run, daemon=True).start()


def public_base():
    fixed = (meta().get("public_url") or "").strip().rstrip("/")
    if fixed:
        return fixed, True
    host = request.host.split(":")[0].lower()
    private = (host == "localhost" or "." not in host or
               re.match(r"^(127\.|10\.|192\.168\.|172\.(1[6-9]|2\d|3[01])\.|169\.254\.)", host) is not None)
    if TUNNEL["url"] and private:
        return TUNNEL["url"], True
    return request.host_url.rstrip("/"), (not private)


def tokens_rows(tokens):
    con = db()
    out = []
    for t in tokens[:50]:
        r = con.execute("SELECT * FROM outputs WHERE token=? AND expires_at>=?", (str(t), int(time.time()))).fetchone()
        if r and os.path.exists(r["path"]):
            out.append(r)
    con.close()
    return out


@app.post("/api/share")
def api_share():
    d = request.get_json(silent=True) or {}
    rows = tokens_rows(d.get("tokens", []))
    if not rows:
        return jsonify(ok=False, error="The prepared files expired. Press the button again."), 400
    base, public = public_base()
    exp = int(time.time()) + SHARE_HOURS * 3600
    con = db()
    with con:
        for r in rows:
            con.execute("UPDATE outputs SET expires_at=? WHERE token=?", (exp, r["token"]))
    con.close()
    links = [dict(name=r["filename"], url=f"{base}/share/{r['token']}/{quote(r['filename'])}") for r in rows]
    log("share link", ", ".join(r["filename"] for r in rows[:4]))
    return jsonify(ok=True, links=links, public=public, hours=SHARE_HOURS)


def smtp_conf():
    m = meta()

    def pick(env, key, default=""):
        return os.environ.get(env) or m.get(key) or default
    try:
        port = int(pick("SMTP_PORT", "smtp_port", "587"))
    except ValueError:
        port = 587
    user = pick("SMTP_USER", "smtp_user")
    return dict(host=pick("SMTP_HOST", "smtp_host"), port=port, user=user, password=pick("SMTP_PASSWORD", "smtp_password"),
                sender=pick("SMTP_FROM", "smtp_from") or user, security=pick("SMTP_SECURITY", "smtp_security", "starttls").lower())


@app.get("/api/settings")
@admin_required
def api_settings_get():
    m, c = meta(), smtp_conf()
    return jsonify(ok=True, host=c["host"], port=c["port"], user=c["user"], sender=c["sender"], security=c["security"],
                   has_password=bool(c["password"]), teams_webhook=m.get("teams_webhook", ""), max_attach_mb=int(m.get("max_attach_mb") or 20),
                   public_url=m.get("public_url", ""), interval=int(m.get("scan_interval_min", "30") or 0))


@app.post("/api/settings")
@admin_required
def api_settings_set():
    d = request.get_json(silent=True) or {}
    vals = {}
    if "host" in d:
        port = str(d.get("port", "587")).strip() or "587"
        if not port.isdigit():
            return jsonify(ok=False, error="Port must be a number (587 or 465)."), 400
        sec = str(d.get("security", "starttls")).lower()
        vals.update(smtp_host=str(d.get("host", "")).strip(), smtp_port=port, smtp_user=str(d.get("user", "")).strip(),
                    smtp_from=str(d.get("sender", "")).strip(), smtp_security=sec if sec in ("starttls", "ssl", "none") else "starttls")
        if str(d.get("password", "")):
            vals["smtp_password"] = str(d["password"])
    if "teams_webhook" in d:
        u = str(d["teams_webhook"]).strip()
        if u and not u.startswith("https://"):
            return jsonify(ok=False, error="The Teams webhook must start with https://"), 400
        vals["teams_webhook"] = u
    if "max_attach_mb" in d:
        vals["max_attach_mb"] = max(1, min(int(d["max_attach_mb"] or 20), 100))
    if "public_url" in d:
        vals["public_url"] = str(d["public_url"]).strip().rstrip("/")
    if "interval" in d:
        vals["scan_interval_min"] = max(0, min(int(d["interval"] or 0), 1440))
    set_meta(vals)
    return jsonify(ok=True)


@app.get("/api/activity")
@admin_required
def api_activity():
    con = db()
    rows = [dict(r) for r in con.execute("SELECT at,by_user,action,detail FROM activity ORDER BY id DESC LIMIT 150")]
    con.close()
    return jsonify(ok=True, rows=rows)


@app.post("/api/send_email")
def api_send_email():
    d = request.get_json(silent=True) or {}
    to = [x for x in re.split(r"[;,\s]+", str(d.get("to", ""))) if x]
    if not to or len(to) > 20 or not all(re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", x) for x in to):
        return jsonify(ok=False, error="Enter valid email address(es), separated by commas."), 400
    c = smtp_conf()
    if not c["host"] or not c["sender"]:
        return jsonify(ok=False, error="Email sending is not set up yet (Admin > Settings > Email)."), 400
    rows = tokens_rows(d.get("tokens", []))
    if not rows:
        return jsonify(ok=False, error="The prepared files expired. Press Send again."), 400
    msg = EmailMessage()
    msg["From"], msg["To"] = c["sender"], ", ".join(to)
    msg["Subject"] = str(d.get("subject") or "Documents").strip()[:200]
    note = str(d.get("message") or "").strip()
    if d.get("mode") == "link":
        base, public = public_base()
        exp = int(time.time()) + SHARE_HOURS * 3600
        con = db()
        with con:
            for r in rows:
                con.execute("UPDATE outputs SET expires_at=? WHERE token=?", (exp, r["token"]))
        con.close()
        links = "\n".join(f"{r['filename']}: {base}/share/{r['token']}/{quote(r['filename'])}" for r in rows)
        msg.set_content((note + "\n\n" if note else "") + f"Download link(s) - valid {SHARE_HOURS} hours:\n{links}\n\nSent from Document Finder by {uname()}.")
    else:
        limit = int(meta().get("max_attach_mb") or 20) * 1048576
        if sum(os.path.getsize(r["path"]) for r in rows) > limit:
            return jsonify(ok=False, code="too_big", error=f"The files are bigger than {limit // 1048576} MB. Choose 'Send a download link' or compress first."), 400
        msg.set_content((note + "\n\n" if note else "") + f"{len(rows)} file(s) attached.\nSent from Document Finder by {uname()}.")
        for r in rows:
            mt = (mimetypes.guess_type(r["filename"])[0] or "application/octet-stream").split("/")
            with open(r["path"], "rb") as fh:
                msg.add_attachment(fh.read(), maintype=mt[0], subtype=mt[1], filename=r["filename"])
    try:
        if c["security"] == "ssl":
            s = smtplib.SMTP_SSL(c["host"], c["port"], timeout=60)
        else:
            s = smtplib.SMTP(c["host"], c["port"], timeout=60)
            if c["security"] == "starttls":
                s.ehlo()
                s.starttls()
                s.ehlo()
        with s:
            if c["user"]:
                s.login(c["user"], c["password"])
            s.send_message(msg)
    except smtplib.SMTPAuthenticationError:
        return jsonify(ok=False, error="The email server refused the login. Check the user name / app password in Settings."), 400
    except Exception as e:
        return jsonify(ok=False, error=f"Could not send the email: {e}"), 400
    log("email", f"to {', '.join(to)}: " + ", ".join(r["filename"] for r in rows[:4]))
    return jsonify(ok=True, sent_to=to)


@app.post("/api/send_teams")
def api_send_teams():
    """Posts the download link(s) into a Teams channel through the Teams (Workflows) webhook set by the admin."""
    d = request.get_json(silent=True) or {}
    hook = meta().get("teams_webhook", "")
    if not hook:
        return jsonify(ok=False, error="Teams channel posting is not set up (Admin > Settings > Teams)."), 400
    rows = tokens_rows(d.get("tokens", []))
    if not rows:
        return jsonify(ok=False, error="The prepared files expired. Press Send again."), 400
    base, public = public_base()
    exp = int(time.time()) + SHARE_HOURS * 3600
    con = db()
    with con:
        for r in rows:
            con.execute("UPDATE outputs SET expires_at=? WHERE token=?", (exp, r["token"]))
    con.close()
    card = {"type": "message", "attachments": [{"contentType": "application/vnd.microsoft.card.adaptive", "contentUrl": None, "content": {
        "$schema": "http://adaptivecards.io/schemas/adaptive-card.json", "type": "AdaptiveCard", "version": "1.4",
        "body": [{"type": "TextBlock", "text": "Documents shared by " + uname(), "weight": "Bolder", "size": "Medium"},
                 {"type": "TextBlock", "text": str(d.get("message") or "")[:1000] or "Download links (valid %d hours)" % SHARE_HOURS, "wrap": True}],
        "actions": [{"type": "Action.OpenUrl", "title": r["filename"][:60], "url": f"{base}/share/{r['token']}/{quote(r['filename'])}"} for r in rows[:6]]}}]}
    try:
        req = urllib.request.Request(hook, data=json.dumps(card).encode(), headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as e:
        return jsonify(ok=False, error=f"Teams did not accept the message: {e}"), 400
    log("teams", ", ".join(r["filename"] for r in rows[:4]))
    return jsonify(ok=True, public=public)


@app.post("/api/log")
def api_log():
    d = request.get_json(silent=True) or {}
    if d.get("action") in ("whatsapp", "teams link", "print"):
        log(d["action"], d.get("detail", ""))
    return jsonify(ok=True)


@app.get("/")
def index():
    return Response(PAGE, mimetype="text/html")


# ============================================================
#  PAGES
# ============================================================
PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Document Finder</title>
<style>
*{box-sizing:border-box}body{font-family:Segoe UI,Arial,sans-serif;margin:0;background:#f3f5f8;color:#1c2733}
header{background:#12355b;color:#fff;padding:14px 22px;display:flex;justify-content:space-between;align-items:center;flex-wrap:wrap;gap:8px}
header h1{margin:0;font-size:20px}header small{opacity:.85}.byline{font-size:12px;opacity:.9}
main{max-width:1250px;margin:18px auto;padding:0 16px}
.tabs{display:flex;gap:6px;margin-bottom:12px;flex-wrap:wrap}
.tab{padding:9px 18px;border-radius:8px 8px 0 0;background:#dfe5ec;cursor:pointer;font-weight:600}.tab.on{background:#fff;color:#12355b}
.card{background:#fff;border-radius:0 8px 8px 8px;padding:18px;box-shadow:0 1px 4px #0002;display:none}
.row{display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:14px}
input[type=text],input[type=password],input[type=email],input[type=number]{font-size:16px;padding:9px 12px;border:2px solid #9fb3c8;border-radius:8px}
input.big{flex:1;min-width:240px;font-size:20px;padding:10px 14px}input:focus,select:focus,textarea:focus{outline:none;border-color:#12355b}
button{font-size:15px;padding:10px 18px;border:0;border-radius:8px;cursor:pointer;font-weight:600;background:#12355b;color:#fff}
button.go{background:#1f9d55}button:disabled{background:#b8c2cc;cursor:not-allowed}button.sec{background:#e6ebf1;color:#12355b}
button.danger{background:#b42318;color:#fff}button.sm{padding:4px 10px;font-size:13px}button.wa{background:#1fa855}button.tm{background:#5059c9}
table{border-collapse:collapse;width:100%}td{padding:8px 9px;border-bottom:1px solid #e3e8ee;font-size:14px;vertical-align:top}
.grid th{background:#12355b;color:#fff;text-align:left;padding:9px;font-size:13px;position:sticky;top:0;white-space:nowrap}
.grid tbody tr:nth-child(even) td{background:#f6f9fc}.grid tbody tr:hover td{background:#e3eefb}.grid tr.sel td{background:#e6f6ec!important}
.grid tr.miss td{background:#ffcdcd!important;color:#8f1010}.grid tr.sim td{background:#fff3cd!important}
mark{background:#ffd54f;padding:0 1px;border-radius:2px}.msg{color:#667}.ok{color:#1b7a43;font-weight:600}.err{color:#b42318;font-weight:600}
.wrap{overflow:auto;max-height:68vh}.chip{display:inline-block;background:#e6ebf1;border-radius:14px;padding:3px 10px;font-size:12px;font-weight:700;color:#12355b}
.badge{padding:2px 9px;border-radius:10px;font-size:12px;font-weight:700}.b-ok{background:#e4f4ea;color:#1b7a43}.b-sim{background:#fff0c2;color:#8a6100}.b-no{background:#fde2e2;color:#b42318}
.fi{font-size:15px;padding:9px;border:2px solid #9fb3c8;border-radius:8px;background:#fff}select.fi{min-width:140px}
textarea{width:100%;font:15px ui-monospace,Consolas,monospace;padding:10px;border:2px solid #9fb3c8;border-radius:8px;min-height:140px}
.ov{position:fixed;inset:0;background:#0008;display:none;align-items:center;justify-content:center;z-index:50;padding:12px}
.mbox{background:#fff;border-radius:10px;padding:18px;width:min(640px,100%);max-height:90vh;overflow:auto}.mbox label{font-weight:600;font-size:14px}
.mbox .fi,.mbox input[type=text],.mbox input[type=email],.mbox textarea{width:100%;display:block;margin:4px 0 10px}.mbox textarea{min-height:90px;font-family:Segoe UI,Arial,sans-serif}
.box{border:1px solid #d8e0e8;border-radius:8px;padding:12px;margin-bottom:14px;background:#fafcff}.box h3{margin:0 0 8px;font-size:15px;color:#12355b}
.opts{display:grid;grid-template-columns:repeat(auto-fill,minmax(230px,1fr));gap:8px}.opts label{border:2px solid #d8e0e8;border-radius:8px;padding:9px 11px;cursor:pointer;background:#fff;font-size:14px}
.opts label:has(input:checked){border-color:#12355b;background:#eaf2fc}.opts small{display:block;color:#667;font-weight:400}
.kv{display:grid;grid-template-columns:170px 1fr;gap:6px 12px;font-size:14px}.kv b{color:#456}.kv span{overflow-wrap:anywhere}
.userbar{display:flex;gap:8px;align-items:center;font-size:13px;flex-wrap:wrap}.userbar button{padding:4px 10px;font-size:13px}
body.nonadmin .adm{display:none!important}.hint{background:#edf5ff;border-radius:8px;padding:9px 12px;font-size:13px;color:#234;margin-bottom:12px}
textarea#q{flex:1;min-width:240px;font:20px Segoe UI,Arial,sans-serif;padding:10px 14px;min-height:50px;max-height:220px;resize:vertical;overflow:auto;line-height:1.3}
iframe.prev{width:100%;height:560px;border:1px solid #cdd7e1;border-radius:8px;background:#eee}.ico{font-weight:700;color:#12355b}
@media(max-width:700px){button{width:100%}td,.grid th{font-size:12.5px;padding:6px}.tab{padding:8px 10px;font-size:14px}.kv{grid-template-columns:1fr}}
</style></head><body class="nonadmin">
<header><h1>Document Finder</h1><small id="meta">loading...</small><small class="byline">Created by Movindu Teshan Wijenayaka</small><span id="userbar" class="userbar"></span></header>
<main>
<div class="tabs">
 <div class="tab on" id="t1" onclick="tab(1)">Search</div>
 <div class="tab" id="t2" onclick="tab(2)">Selected <span class="chip" id="selc">0</span></div>
 <div class="tab" id="t3" onclick="tab(3)">Required documents</div>
 <div class="tab" id="t4" onclick="tab(4)">Details</div>
 <div class="tab adm" id="t5" onclick="tab(5)">Folders</div>
 <div class="tab adm" id="t6" onclick="tab(6)">Settings</div>
</div>

<!-- 1 SEARCH -->
<div class="card" id="p1">
 <div class="row"><textarea id="q" rows="1" placeholder="Type ONE document number (results appear while you type) - or paste MANY names, one per line / from Excel, to see Found / Not found" autocomplete="off" autofocus oninput="onType()"></textarea>
  <select class="fi" id="extsel" onchange="doSearch()"><option value="">All types</option></select>
  <select class="fi" id="foldersel" onchange="doSearch()"><option value="">All folders</option></select></div>
 <div class="row" style="margin-top:-6px"><label><input type="checkbox" id="exact" onchange="doSearch()"> Exact number only</label>
  <label><input type="checkbox" id="inpath" onchange="doSearch()"> Also match folder names</label>
  <button class="sec sm" id="addshownBtn" onclick="addShown()">Select all shown</button><button class="go sm" onclick="tab(2)">Selected &rarr;</button><button class="sec sm" onclick="printSearch()">Print list</button><span id="sinfo" class="msg"></span></div>
 <div id="bulk" style="display:none"><div class="row"><b id="bksum"></b><button class="sec sm" onclick="reqFilter('all')">All</button><button class="sec sm" onclick="reqFilter('found')">Found only</button><button class="sec sm" onclick="reqFilter('missing')">Missing only</button></div>
  <div class="row"><button onclick="reqAdd(false,'bk')">Add ticked files to Selected</button><button class="sec" onclick="reqAdd(true,'bk')">Add ALL matches</button><button class="sec" onclick="reqCopyMissing()">Copy missing list</button><button class="sec" onclick="reqCsv()">Download result (CSV)</button><button class="sec" onclick="printList('bktab','Document check - Found / Not found')">Print list</button></div>
  <div class="wrap"><table class="grid" id="bktab"><thead><tr><th>Name</th><th>Status</th><th>Matching files</th></tr></thead><tbody></tbody></table></div></div>
 <div class="wrap" id="singlewrap"><table class="grid" id="res" style="display:none"><thead><tr><th><input type="checkbox" id="allbox" onchange="selAll(this.checked)"></th><th>Document</th><th>Type</th><th>Size</th><th>Modified</th><th>Folder</th><th></th></tr></thead><tbody></tbody></table></div>
</div>

<!-- 2 SELECTED -->
<div class="card" id="p2">
 <div class="row"><b id="selinfo"></b><button class="sec sm" onclick="clearSel()">Clear all</button><button class="sec sm" onclick="checkSel()">Check files still exist</button><button class="go sm" onclick="selOut(false)">Preview selected</button><button class="go sm" onclick="selOut(true)">Print selected</button><button class="sec sm" onclick="printList('seltab','Selected documents',$('selinfo').textContent)">Print list</button></div>
 <div class="wrap" style="max-height:38vh"><table class="grid" id="seltab"><thead><tr><th>#</th><th>Document</th><th>Type</th><th>Size</th><th>Folder</th><th></th></tr></thead><tbody></tbody></table></div>
 <div class="box" style="margin-top:14px"><h3>1. What do you want to make?</h3>
  <div class="opts">
   <label><input type="radio" name="proc" value="original" checked onchange="procUI()"> Original files<small>as they are</small></label>
   <label><input type="radio" name="proc" value="merge" onchange="procUI()"> Merge into one PDF<small>in the order above, with bookmarks</small></label>
   <label><input type="radio" name="proc" value="merge_compress" onchange="procUI()"> Merge + Compress<small>one smaller PDF</small></label>
   <label><input type="radio" name="proc" value="compress" onchange="procUI()"> Compress PDF(s)<small>each PDF separately</small></label>
   <label><input type="radio" name="proc" value="zip" onchange="procUI()"> ZIP<small>all files in one .zip</small></label></div>
  <div class="row" style="margin:10px 0 0"><span id="lvlbox" style="display:none">Compression: <select class="fi" id="level"><option value="low">Low (best quality)</option><option value="medium" selected>Medium (recommended)</option><option value="high">High (smallest)</option></select></span>
   <span>File name (optional): <input type="text" id="outname" placeholder="auto" style="width:260px"></span></div></div>
 <div class="box"><h3>2. Download or send</h3>
  <div class="row" style="margin-bottom:6px"><button class="go" onclick="doDownload()">Download (choose location)</button><button onclick="openSend('email')">Send by Email</button>
   <button class="tm" onclick="openSend('teams')">Send by Teams</button><button class="wa" onclick="openSend('whatsapp')">Send by WhatsApp</button><button class="sec" onclick="openSend('link')">Copy download link</button></div>
  <div id="fsahint" class="hint" style="display:none"></div><div id="procmsg" class="msg"></div></div>
</div>

<!-- 3 REQUIRED -->
<div class="card" id="p3">
 <div class="hint">Paste the document numbers you need (one per line, or copy a column from Excel). The app checks which ones exist in the indexed folders.</div>
 <textarea id="reqtxt" placeholder="290092-ISO-0101&#10;290092-ISO-0102&#10;..."></textarea>
 <div class="row" style="margin-top:10px"><button class="go" onclick="reqCheck()">Check availability</button><label><input type="checkbox" id="reqpdf" checked> PDF files only</label><label title="Exact-only uses indexed lookups. Similar-name matching scans the index and can take longer."><input type="checkbox" id="reqsim"> Include similar names (slower)</label><span id="reqsum" class="msg"></span></div>
 <div class="row" id="reqact" style="display:none"><button onclick="reqAdd(false,'req')">Add ticked files to Selected</button><button class="sec" onclick="reqAdd(true,'req')">Tick + add ALL matches</button><button class="sec" onclick="reqCopyMissing()">Copy missing list</button><button class="sec" onclick="reqCsv()">Download result (CSV)</button><button class="sec" onclick="printList('reqtab','Required documents - Found / Not found')">Print list</button></div>
 <div class="wrap"><table class="grid" id="reqtab" style="display:none"><thead><tr><th>Required number</th><th>Status</th><th>Matching files</th></tr></thead><tbody></tbody></table></div>
</div>

<!-- 4 DETAILS -->
<div class="card" id="p4"><div id="detbox" class="msg">Press <b>More</b> on a document in the Search tab to see its details here.</div></div>

<!-- 5 FOLDERS -->
<div class="card adm" id="p5">
 <div class="hint">Add any folder the <b>server PC</b> can open: a local folder (<code>D:\Projects</code>) or a company server path (<code>\\Abdfabfs01\mcd$\Materials Dept\...</code>). Sub-folders are included automatically.</div>
 <div class="row"><input type="text" id="fpath" class="big" placeholder="Folder path (paste here)"><input type="text" id="flabel" placeholder="Name (optional)" style="width:220px"><button class="go" onclick="addFolder()">Add folder</button></div>
 <div class="row"><button class="sec" onclick="rescan(null)">Rescan all now</button><button class="sec" onclick="printList('ftab','Indexed folders')">Print list</button><span>Auto-rescan every <input type="number" id="fint" min="0" max="1440" style="width:80px"> min (0 = off)</span><button class="sec sm" onclick="saveInt()">Save</button><span id="fmsg" class="msg"></span></div>
 <div class="wrap"><table class="grid" id="ftab"><thead><tr><th>Name</th><th>Path</th><th>Files</th><th>Last scan</th><th>Status</th><th></th></tr></thead><tbody></tbody></table></div>
</div>

<!-- 6 SETTINGS -->
<div class="card adm" id="p6">
 <div class="box"><h3>Email (SMTP)</h3><div class="row"><input type="text" id="sHost" placeholder="SMTP server (e.g. smtp.office365.com)"><input type="number" id="sPort" placeholder="Port" style="width:90px" value="587">
  <select class="fi" id="sSec"><option value="starttls">STARTTLS (587)</option><option value="ssl">SSL (465)</option><option value="none">None</option></select></div>
  <div class="row"><input type="text" id="sUser" placeholder="User name"><input type="password" id="sPass" placeholder="Password / app password (leave empty to keep)"><input type="text" id="sFrom" placeholder="From address"></div>
  <div class="row"><span>Max attachment size <input type="number" id="sMax" style="width:80px" value="20"> MB</span><button onclick="saveSmtp()">Save email settings</button><span id="smsg" class="msg"></span></div></div>
 <div class="box"><h3>Teams channel (optional)</h3><div class="row"><input type="text" class="big" id="sTeams" placeholder="Teams Workflows webhook URL (https://...)"><button onclick="saveTeams()">Save</button></div>
  <div class="msg">Without this the "Send by Teams" button opens Teams' own share window. With it you also get "Post to Teams channel".</div></div>
 <div class="box"><h3>Public address for links</h3><div class="row"><input type="text" class="big" id="sPub" placeholder="Optional fixed address, e.g. https://docs.mycompany.com"><button onclick="savePub()">Save</button></div>
  <div class="msg">Links in emails / WhatsApp / Teams use this address. If empty, the PC address (or the cloudflared https link) is used.</div></div>
 <div class="box"><h3>Users</h3><div class="row"><input type="text" id="uName" placeholder="New user name"><input type="password" id="uPass" placeholder="Password (min 6)"><select class="fi" id="uRole"><option value="user">User</option><option value="admin">Admin</option></select><button onclick="addUser()">Add user</button><span id="umsg" class="msg"></span></div>
  <table class="grid" id="utab"><thead><tr><th>User</th><th>Role</th><th>Last login</th><th></th></tr></thead><tbody></tbody></table></div>
 <div class="box"><h3>Recent activity <button class="sec sm" onclick="printList('atab','Recent activity')">Print</button></h3><div class="wrap" style="max-height:260px"><table class="grid" id="atab"><thead><tr><th>When</th><th>User</th><th>Action</th><th>Detail</th></tr></thead><tbody></tbody></table></div></div>
</div>
</main>

<!-- PREVIEW WINDOW -->
<div class="ov" id="pvov"><div class="mbox" style="width:min(1150px,100%);height:94vh;max-height:94vh;display:flex;flex-direction:column;overflow:hidden">
 <div class="row" style="margin-bottom:8px"><b id="pvName" style="flex:1;min-width:200px;overflow-wrap:anywhere"></b>
  <button class="sec sm" onclick="pvStep(-1)" id="pvPrev">&#9664; Prev</button><span id="pvPos" class="msg"></span><button class="sec sm" onclick="pvStep(1)" id="pvNext">Next &#9654;</button>
  <button class="go sm" onclick="pvPrint()">Print</button><button class="sec sm" onclick="pvDownload()">Download</button><button class="sec sm" onclick="pvTab()">New tab</button><button class="danger sm" onclick="closePv()">Close</button></div>
 <iframe id="pvFrame" style="flex:1;width:100%;border:1px solid #cdd7e1;border-radius:8px;background:#eee"></iframe></div></div>

<!-- SEND DIALOG -->
<div class="ov" id="sendov"><div class="mbox"><h3 id="shTitle" style="margin-top:0"></h3>
 <div id="shEmail"><label>To (comma separated)</label><input type="text" id="shTo" placeholder="name@company.com, other@company.com"><label>Subject</label><input type="text" id="shSub">
  <label>Delivery</label><div class="opts" style="margin-bottom:10px"><label><input type="radio" name="deliv" value="attach" checked> Attach the file(s)<small>max <span id="shMax">20</span> MB</small></label><label><input type="radio" name="deliv" value="link"> Send a download link<small>for big files</small></label></div></div>
 <div id="shPhone" style="display:none"><label>WhatsApp number (optional, with country code, e.g. 9715XXXXXXXX)</label><input type="text" id="shPh"></div>
 <label>Message</label><textarea id="shMsg"></textarea>
 <div id="shRes" class="msg" style="margin-bottom:8px"></div>
 <div class="row"><button class="go" id="shGo" onclick="doSend()">Send</button><button class="sec" id="shHook" style="display:none" onclick="doTeamsHook()">Post to Teams channel</button><button class="sec" onclick="closeSend()">Close</button></div></div></div>

<script>
const $=id=>document.getElementById(id);
const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function fmtSize(n){n=+n||0;if(n<1024)return n+' B';if(n<1048576)return (n/1024).toFixed(0)+' KB';if(n<1073741824)return (n/1048576).toFixed(1)+' MB';return (n/1073741824).toFixed(2)+' GB'}
async function api(url,body){const o=body===undefined?{}:{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify(body)};
 const r=await fetch(url,o);if(r.status==401){location='/login';throw new Error('login')}let j={};try{j=await r.json()}catch(e){}
 if(!r.ok||j.ok===false){const e=new Error(j.error||('Error '+r.status));e.code=j.code;throw e}return j}
let ME={},META={},SEL=new Map(),LAST=[],REQ=[],CUR=null,sTimer,sCtl,SENDKIND='';
const FSA=!!(window.showSaveFilePicker&&window.showDirectoryPicker&&window.isSecureContext);
function loadSel(){try{JSON.parse(localStorage.getItem('df_sel')||'[]').forEach(d=>SEL.set(d.id,d))}catch(e){}}
function saveSel(){try{localStorage.setItem('df_sel',JSON.stringify([...SEL.values()]))}catch(e){}$('selc').textContent=SEL.size}
function tab(n){for(let i=1;i<=6;i++){$('t'+i).classList.toggle('on',i==n);$('p'+i).style.display=i==n?'block':'none'}
 if(n==2)renderSel();if(n==5)loadFolders();if(n==6)loadSettings()}
function mark(name,q){let h=esc(name);const toks=q.split(/\s+/).filter(Boolean);
 toks.forEach(t=>{const re=new RegExp(t.split('').map(c=>c.replace(/[.*+?^${}()|[\]\\\/]/g,'\\$&')).join('[\\W_]*'),'ig');
  h=h.replace(re,m=>m.includes('&')||m.includes(';')?m:'<mark>'+m+'</mark>')});return h}

/* ---------- start ---------- */
async function boot(){loadSel();ME=await api('/api/me');if(ME.role=='admin')document.body.classList.remove('nonadmin');
 $('userbar').innerHTML='<span>'+esc(ME.username)+' ('+ME.role+')</span><button onclick="chgPw()">Change password</button><button onclick="logout()">Log out</button>';
 await loadMeta();saveSel();tab(1);setInterval(()=>{if(META.scan&&META.scan.state!='idle')loadMeta()},3000)}
async function loadMeta(){META=await api('/api/meta');
 $('meta').textContent=META.total.toLocaleString()+' files indexed'+(META.scan.state!='idle'?' - indexing '+META.scan.count.toLocaleString()+'...':'');
 const es=$('extsel'),cur=es.value;es.innerHTML='<option value="">All types</option>'+META.exts.map(e=>'<option value="'+esc(e.ext=='(none)'?'':e.ext)+'">'+esc(e.ext)+' ('+e.n.toLocaleString()+')</option>').join('');es.value=cur;
 const fs=$('foldersel'),fc=fs.value;fs.innerHTML='<option value="">All folders</option>'+META.folders.map(f=>'<option value="'+f.id+'">'+esc(f.label)+'</option>').join('');fs.value=fc;
 const h=$('fsahint');if(!FSA){h.style.display='block';h.innerHTML='Your browser will save into its normal <b>Downloads</b> folder here. To pick the folder every time: open the site through the <b>https link</b> (cloudflared) in Chrome/Edge, or turn on "Ask where to save each file" in the browser download settings.'}
 else{h.style.display='block';h.innerHTML='&#10003; Choose-location download is available in this browser.'}
 $('shMax').textContent=META.max_attach_mb;$('shHook').style.display=META.teams_hook?'inline-block':'none'}
async function logout(){await api('/api/logout',{});location='/login'}
async function chgPw(){const o=prompt('Current password:');if(o===null)return;const n=prompt('New password (min 6 characters):');if(!n)return;
 try{await api('/api/change_password',{old:o,new:n});alert('Password changed.')}catch(e){alert(e.message)}}

/* ---------- search ---------- */
function parseNames(v){const seen=new Set(),out=[];v.split(/[\r\n,;\t]+/).map(s=>s.trim()).filter(Boolean).forEach(s=>{const k=s.toLowerCase();if(!seen.has(k)){seen.add(k);out.push(s)}});return out}
function onType(){const t=$('q');t.rows=Math.min(8,Math.max(1,t.value.split('\n').length));clearTimeout(sTimer);sTimer=setTimeout(doSearch,parseNames(t.value).length>1?600:180)}
let bseq=0;
async function bulkSearch(names){const my=++bseq;if(sCtl)sCtl.abort();$('bulk').style.display='block';$('singlewrap').style.display='none';$('addshownBtn').style.display='none';$('sinfo').textContent='Checking '+names.length+' names...';
 try{const j=await api('/api/required',{items:names,ext:$('extsel').value,folder:$('foldersel').value,exact:$('exact').checked});if(my!=bseq)return;REQ=j.results;REQF='all';reqRender('bk');$('sinfo').textContent=names.length+' names checked'}
 catch(e){if(my==bseq)$('sinfo').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
async function doSearch(){const names=parseNames($('q').value);const q=names.length==1?names[0]:'';bseq++;
 if(names.length>1){return bulkSearch(names)}
 $('bulk').style.display='none';$('singlewrap').style.display='block';$('addshownBtn').style.display='inline-block';
 if(!q){$('res').style.display='none';$('sinfo').textContent='';LAST=[];return}
 if(sCtl)sCtl.abort();sCtl=new AbortController();
 const p=new URLSearchParams({q,exact:$('exact').checked?1:0,inpath:$('inpath').checked?1:0,ext:$('extsel').value,folder:$('foldersel').value});
 try{const r=await fetch('/api/search?'+p,{signal:sCtl.signal});if(r.status==401){location='/login';return}const j=await r.json();
  LAST=j.items;$('sinfo').textContent=j.total.toLocaleString()+' found'+(j.total>j.items.length?' (showing first '+j.items.length+' - type more to narrow)':'');
  const tb=$('res').querySelector('tbody');$('res').style.display=LAST.length?'table':'none';
  tb.innerHTML=LAST.map((d,i)=>'<tr class="'+(SEL.has(d.id)?'sel':'')+'" id="r'+d.id+'"><td><input type="checkbox" '+(SEL.has(d.id)?'checked':'')+' onchange="toggle('+i+',this.checked)"></td><td>'+mark(d.name,q)+'</td><td>'+esc(d.ext.toUpperCase())+'</td><td>'+fmtSize(d.size)+'</td><td>'+esc(d.mtime)+'</td><td title="'+esc(d.dir)+'">'+esc(d.folder)+(d.dir?'<br><small class="msg">'+esc(d.dir)+'</small>':'')+'</td><td style="white-space:nowrap"><button class="sec sm" onclick="pv(\'s\','+i+')">Preview</button> <button class="sec sm" onclick="pv(\'s\','+i+',true)">Print</button> <button class="sec sm" onclick="openDetails('+d.id+')">More</button> <button class="sec sm" onclick="location=\'/api/file/'+d.id+'/download\'">Download</button></td></tr>').join('')}
 catch(e){if(e.name!='AbortError')$('sinfo').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
function toggle(i,on){const d=LAST[i];on?SEL.set(d.id,d):SEL.delete(d.id);$('r'+d.id).classList.toggle('sel',on);saveSel()}
function selAll(on){LAST.forEach((d,i)=>toggle(i,on));document.querySelectorAll('#res tbody input').forEach(c=>c.checked=on)}
function addShown(){$('allbox').checked=true;selAll(true)}

/* ---------- details ---------- */
async function openDetails(id){tab(4);$('detbox').innerHTML='<span class="msg">Loading...</span>';
 try{const j=await api('/api/file/'+id+'/info');CUR=j.info;const i=j.info,p=i.pdf||{};
  const kv=[['File name',i.name],['Type',i.ext.toUpperCase()],['Size',fmtSize(i.size)],['Modified',i.mtime],['Created',i.created],['Folder',i.folder],['Sub-folder',i.dir||'(top)'],['Full path',i.path]];
  if(p.pages)kv.push(['PDF pages',p.pages]);if(p.page_size)kv.push(['Paper size',p.page_size]);if(p.title)kv.push(['PDF title',p.title]);if(p.author)kv.push(['Author',p.author]);if(p.creator)kv.push(['Created with',p.creator]);if(p.encrypted)kv.push(['PDF','Password protected']);if(p.error)kv.push(['PDF',p.error]);
  const prev=['pdf','png','jpg','jpeg','gif','webp','txt'].includes(i.ext);
  $('detbox').innerHTML='<h2 style="margin:0 0 10px;overflow-wrap:anywhere">'+esc(i.name)+'</h2><div class="row"><button class="go" onclick="detDownload()">Download (choose location)</button><button class="go" onclick="pvDoc(CUR)">Preview</button><button class="go" onclick="pvDoc(CUR,true)">Print file</button><button class="sec" onclick="printList(\'detbox\',CUR.name)">Print details</button><button class="sec" onclick="window.open(\'/api/file/'+i.id+'/view\',\'_blank\')">Open in new tab</button><button id="detsel" class="sec" onclick="detSel()"></button><button class="sec" onclick="navigator.clipboard&&navigator.clipboard.writeText(CUR.path)">Copy server path</button></div><div class="kv">'+kv.map(x=>'<b>'+esc(x[0])+'</b><span>'+esc(x[1])+'</span>').join('')+'</div>'
   +(j.related.length?'<h3>Other files with the same number / other revisions ('+j.related.length+')</h3><div class="wrap" style="max-height:260px"><table class="grid"><thead><tr><th></th><th>Document</th><th>Size</th><th>Modified</th><th>Folder</th><th></th></tr></thead><tbody>'+j.related.map((d,k)=>'<tr><td><input type="checkbox" '+(SEL.has(d.id)?'checked':'')+' onchange="relSel('+k+',this.checked)"></td><td>'+esc(d.name)+'</td><td>'+fmtSize(d.size)+'</td><td>'+esc(d.mtime)+'</td><td>'+esc(d.folder)+'</td><td style="white-space:nowrap"><button class="sec sm" onclick="pv(\'rel\','+k+')">Preview</button> <button class="sec sm" onclick="pv(\'rel\','+k+',true)">Print</button> <button class="sec sm" onclick="openDetails('+d.id+')">More</button></td></tr>').join('')+'</tbody></table></div>':'')
   +(prev?'<h3>Preview</h3><iframe class="prev" src="/api/file/'+i.id+'/view"></iframe>':'');
  window.REL=j.related;detSelBtn()}catch(e){$('detbox').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
function detSelBtn(){$('detsel').textContent=SEL.has(CUR.id)?'Remove from Selected':'Add to Selected'}
function detSel(){SEL.has(CUR.id)?SEL.delete(CUR.id):SEL.set(CUR.id,CUR);saveSel();detSelBtn()}
function relSel(k,on){const d=REL[k];on?SEL.set(d.id,d):SEL.delete(d.id);saveSel()}
async function detDownload(){await runDownload({ids:[CUR.id],action:'original'},[CUR])}

/* ---------- selected ---------- */
function renderSel(){const a=[...SEL.values()];$('seltab').querySelector('tbody').innerHTML=a.map((d,i)=>'<tr><td>'+(i+1)+'</td><td>'+esc(d.name)+'</td><td>'+esc(d.ext.toUpperCase())+'</td><td>'+fmtSize(d.size)+'</td><td>'+esc(d.folder)+'</td><td style="white-space:nowrap"><button class="sec sm" onclick="pv(\'sel\','+i+')">Preview</button> <button class="sec sm" onclick="pv(\'sel\','+i+',true)">Print</button> <button class="sec sm" onclick="mv('+d.id+',-1)">&#9650;</button> <button class="sec sm" onclick="mv('+d.id+',1)">&#9660;</button> <button class="sec sm" onclick="rm('+d.id+')">Remove</button></td></tr>').join('');
 const tot=a.reduce((s,d)=>s+(d.size||0),0),pdf=a.filter(d=>d.ext=='pdf').length;$('selinfo').textContent=a.length+' selected - '+fmtSize(tot)+' ('+pdf+' PDF)';saveSel();procUI()}
function mv(id,dir){const a=[...SEL.values()],i=a.findIndex(d=>d.id==id),j=i+dir;if(j<0||j>=a.length)return;[a[i],a[j]]=[a[j],a[i]];SEL=new Map(a.map(d=>[d.id,d]));renderSel()}
function rm(id){SEL.delete(id);renderSel()}function clearSel(){SEL.clear();renderSel()}
async function checkSel(){try{const j=await api('/api/docs',{ids:[...SEL.keys()]});j.missing.forEach(i=>SEL.delete(i));renderSel();$('procmsg').innerHTML=j.missing.length?'<span class="err">'+j.missing.length+' file(s) no longer exist and were removed.</span>':'<span class="ok">All selected files exist.</span>'}catch(e){$('procmsg').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
function procAct(){return document.querySelector('input[name=proc]:checked').value}
function procUI(){const a=procAct();$('lvlbox').style.display=(a=='compress'||a=='merge_compress')?'inline':'none'}
function stampName(){const d=new Date(),p=n=>String(n).padStart(2,'0');return d.getFullYear()+p(d.getMonth()+1)+p(d.getDate())+'_'+p(d.getHours())+p(d.getMinutes())}
function plan(forShare){const ids=[...SEL.keys()],a=procAct();return {ids,action:a,level:$('level').value,name:$('outname').value.trim(),zip:!!forShare}}
function single(pl){if(['merge','merge_compress','zip'].includes(pl.action)||pl.zip)return true;return pl.ids.length==1}
function suggest(pl){const first=SEL.get(pl.ids[0])||{name:'file',ext:''};const st=first.name.replace(/\.[^.]+$/,'');
 if(pl.name)return pl.name;if(pl.action=='merge')return 'Merged_'+stampName()+'.pdf';if(pl.action=='merge_compress')return 'Merged_'+stampName()+'.pdf';
 if(pl.action=='zip'||(pl.zip&&pl.ids.length>1))return 'Documents_'+stampName()+'.zip';if(pl.action=='compress')return st+'_compressed.pdf';return first.name}
function msg(h){$('procmsg').innerHTML=h}
async function doDownload(){if(!SEL.size){msg('<span class="err">Select documents first.</span>');return}
 await runDownload(plan(false),[...SEL.values()])}
async function runDownload(pl,docs){let target=null;const one=single(pl);
 if(one&&(pl.action!='original'||pl.ids.length==1)){pl.name=suggest(pl)}
 try{if(FSA){target=one?await showSaveFilePicker({suggestedName:pl.name||docs[0].name}):await showDirectoryPicker({mode:'readwrite'})}}
 catch(e){if(e.name=='AbortError')return;target=null}
 msg('Preparing files... (merging / compressing large drawings can take a minute)');
 try{const j=await api('/api/process',pl);await writeOut(j.outputs,target);
  msg('<span class="ok">Done. </span>'+esc(j.info)+(j.warnings.length?'<br><span class="err">'+j.warnings.map(esc).join('<br>')+'</span>':''))}
 catch(e){msg('<span class="err">'+esc(e.message)+'</span>')}}
async function streamTo(url,fh){const r=await fetch(url);if(!r.ok)throw new Error('Download failed ('+r.status+')');const w=await fh.createWritable();await r.body.pipeTo(w)}
async function writeOut(outs,target){
 if(target&&target.kind=='file'&&outs.length==1){await streamTo(outs[0].url,target);return}
 if(target&&target.kind=='directory'){const used=new Set();for(const o of outs){let n=o.name;while(used.has(n.toLowerCase()))n='copy_'+n;used.add(n.toLowerCase());
   const fh=await target.getFileHandle(n,{create:true});await streamTo(o.url,fh)}return}
 for(const o of outs){const a=document.createElement('a');a.href=o.url;a.download=o.name;document.body.appendChild(a);a.click();a.remove();await new Promise(r=>setTimeout(r,500))}}

/* ---------- send ---------- */
function openSend(kind){if(!SEL.size){msg('<span class="err">Select documents first.</span>');return}SENDKIND=kind;
 $('shTitle').textContent={email:'Send by Email',teams:'Send by Teams',whatsapp:'Send by WhatsApp',link:'Copy download link'}[kind];
 $('shEmail').style.display=kind=='email'?'block':'none';$('shPhone').style.display=kind=='whatsapp'?'block':'none';
 $('shGo').textContent={email:'Send email',teams:'Open Teams',whatsapp:'Prepare WhatsApp message',link:'Create link'}[kind];$('shHook').style.display=(kind=='teams'&&META.teams_hook)?'inline-block':'none';
 $('shRes').innerHTML='';$('shSub').value='Documents - '+SEL.size+' file(s)';$('shMsg').value='';$('sendov').style.display='flex'}
function closeSend(){$('sendov').style.display='none'}
function linkPlan(){const pl=plan(true);if(single(pl))pl.name=suggest(pl);return pl}
async function doSend(){const b=$('shGo');b.disabled=true;$('shRes').innerHTML='Preparing files...';
 try{if(SENDKIND=='email'){const mode=document.querySelector('input[name=deliv]:checked').value;const pl=mode=='link'?linkPlan():plan(false);if(single(pl)&&pl.action!='original')pl.name=suggest(pl);
   const p=await api('/api/process',pl);const j=await api('/api/send_email',{to:$('shTo').value,subject:$('shSub').value,message:$('shMsg').value,tokens:p.outputs.map(o=>o.token),mode});
   $('shRes').innerHTML='<span class="ok">Email sent to '+esc(j.sent_to.join(', '))+'</span>'+(p.warnings.length?'<br><span class="err">'+p.warnings.map(esc).join('<br>')+'</span>':'')}
  else{const p=await api('/api/process',linkPlan());const s=await api('/api/share',{tokens:p.outputs.map(o=>o.token)});
   const list=s.links.map(l=>l.name+': '+l.url).join('\n');const txt=($('shMsg').value.trim()?$('shMsg').value.trim()+'\n\n':'')+list+'\n(link valid '+s.hours+' hours)';
   const warn=s.public?'':'<br><span class="err">These links work only inside the company network. Start cloudflared or set a public address in Settings for outside people.</span>';
   if(SENDKIND=='whatsapp'){const ph=$('shPh').value.replace(/[^0-9]/g,'');const u='https://wa.me/'+ph+'?text='+encodeURIComponent(txt);api('/api/log',{action:'whatsapp',detail:s.links.map(l=>l.name).join(', ')}).catch(()=>{});
    $('shRes').innerHTML='<a href="'+u+'" target="_blank"><button class="wa">Open WhatsApp</button></a>'+warn;window.open(u,'_blank')}
   else if(SENDKIND=='teams'){const u='https://teams.microsoft.com/share?href='+encodeURIComponent(s.links[0].url)+'&msgText='+encodeURIComponent(txt);api('/api/log',{action:'teams link',detail:s.links.map(l=>l.name).join(', ')}).catch(()=>{});
    $('shRes').innerHTML='<a href="'+u+'" target="_blank"><button class="tm">Open Teams</button></a>'+warn;window.open(u,'_blank')}
   else{let ok=false;try{await navigator.clipboard.writeText(list);ok=true}catch(e){}$('shRes').innerHTML=(ok?'<span class="ok">Link copied.</span>':'Copy the link below:')+'<br><textarea readonly style="min-height:70px">'+esc(list)+'</textarea>'+warn}}}
 catch(e){$('shRes').innerHTML='<span class="err">'+esc(e.message)+'</span>'+(e.code=='too_big'?'<br>Tip: choose "Send a download link", or Merge + Compress first.':'')}
 b.disabled=false}
async function doTeamsHook(){$('shRes').innerHTML='Posting...';try{const p=await api('/api/process',linkPlan());const j=await api('/api/send_teams',{tokens:p.outputs.map(o=>o.token),message:$('shMsg').value});
 $('shRes').innerHTML='<span class="ok">Posted to the Teams channel.</span>'+(j.public?'':'<br><span class="err">Links work only inside the company network.</span>')}catch(e){$('shRes').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}

/* ---------- required documents ---------- */
let REQF='all',REQP='req';
async function reqCheck(){const items=parseNames($('reqtxt').value);
 if(!items.length){$('reqsum').textContent='Paste some document numbers first.';return}$('reqsum').textContent='Checking '+items.length+'...';
 try{const j=await api('/api/required',{items,ext:$('reqpdf').checked?'pdf':'',similar:$('reqsim').checked});REQ=j.results;REQF='all';reqRender('req')}catch(e){$('reqsum').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
function reqFilter(f){REQF=f;reqRender(REQP)}
function reqRender(p){REQP=p;const f=REQ.filter(r=>r.status=='found').length,s=REQ.filter(r=>r.status=='similar').length,m=REQ.filter(r=>r.status=='missing').length;
 const sum='<span class="ok">Found '+f+'</span> &middot; <b style="color:#8a6100">Similar '+s+'</b> &middot; <span class="err">Not found '+m+'</span> &nbsp;of '+REQ.length;
 if(p=='bk')$('bksum').innerHTML=sum;else{$('reqsum').innerHTML=sum;$('reqact').style.display='flex'}
 $(p+'tab').style.display='table';
 $(p+'tab').querySelector('tbody').innerHTML=REQ.map((r,i)=>[r,i]).filter(([r])=>REQF=='all'||(REQF=='found'?r.status!='missing':r.status=='missing')).map(([r,i])=>'<tr class="'+(r.status=='missing'?'miss':r.status=='similar'?'sim':'')+'"><td><b>'+esc(r.q)+'</b></td><td><span class="badge '+(r.status=='found'?'b-ok':r.status=='similar'?'b-sim':'b-no')+'">'+(r.status=='found'?'Found':r.status=='similar'?'Similar name':'Not found')+'</span></td><td>'+
  (r.matches.length?r.matches.map((d,k)=>'<label style="display:block"><input type="checkbox" data-r="'+i+'" data-k="'+k+'" '+(k==0&&r.status=='found'?'checked':'')+'> '+esc(d.name)+' <small class="msg">'+fmtSize(d.size)+' &middot; '+esc(d.mtime)+' &middot; '+esc(d.folder)+'</small> <a href="#" onclick="openDetails('+d.id+');return false">More</a> &middot; <a href="#" onclick="pvReq('+i+','+k+');return false">Preview</a> &middot; <a href="#" onclick="pvReq('+i+','+k+',true);return false">Print</a></label>').join(''):'<span class="msg">not found in the indexed folders</span>')+'</td></tr>').join('')}
function reqAdd(all,p){p=p||REQP;let n=0;document.querySelectorAll('#'+p+'tab input[type=checkbox]').forEach(c=>{if(all)c.checked=true;if(c.checked){const d=REQ[c.dataset.r].matches[c.dataset.k];SEL.set(d.id,d);n++}});saveSel();
 const el=$(p=='bk'?'bksum':'reqsum');el.innerHTML+=' &mdash; <span class="ok">'+n+' file(s) added to Selected</span>'}
function reqCopyMissing(){const t=REQ.filter(r=>r.status=='missing').map(r=>r.q).join('\n');navigator.clipboard?navigator.clipboard.writeText(t).then(()=>alert('Missing list copied ('+t.split('\n').filter(Boolean).length+').')):prompt('Copy:',t)}
function reqCsv(){const q=s=>'"'+String(s).replace(/"/g,'""')+'"';const rows=[['Required','Status','File','Size','Modified','Folder']];REQ.forEach(r=>{if(!r.matches.length)rows.push([r.q,r.status,'','','','']);r.matches.forEach(d=>rows.push([r.q,r.status,d.name,d.size,d.mtime,d.folder]))});
 const b=new Blob(['\ufeff'+rows.map(r=>r.map(q).join(',')).join('\r\n')],{type:'text/csv'});const a=document.createElement('a');a.href=URL.createObjectURL(b);a.download='required_documents_'+stampName()+'.csv';a.click()}

/* ---------- folders (admin) ---------- */
async function loadFolders(){try{const j=await api('/api/folders');$('fint').value=j.interval;
 $('ftab').querySelector('tbody').innerHTML=j.folders.map(f=>'<tr><td>'+esc(f.label)+'</td><td style="overflow-wrap:anywhere">'+esc(f.path)+'</td><td>'+(f.file_count||0).toLocaleString()+'</td><td>'+esc(f.last_scan||'-')+'</td><td>'+(f.error?'<span class="err">'+esc(f.error)+'</span>':(f.enabled?'<span class="ok">OK</span>':'<span class="msg">Disabled</span>'))+'</td><td style="white-space:nowrap"><button class="sec sm" onclick="togFolder('+f.id+')">'+(f.enabled?'Disable':'Enable')+'</button> <button class="sec sm" onclick="rescan('+f.id+')">Rescan</button> <button class="danger sm" onclick="delFolder('+f.id+')">Remove</button></td></tr>').join('');
 $('fmsg').textContent=j.scan.state!='idle'?'Indexing '+j.scan.count.toLocaleString()+' files...':(j.scan.next&&j.scan.next!='off'?'Next auto scan: '+j.scan.next:'');
 if(j.scan.state!='idle')setTimeout(()=>{if($('p5').style.display=='block')loadFolders()},2500);else loadMeta()}catch(e){$('fmsg').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
async function addFolder(){try{await api('/api/folders',{path:$('fpath').value,label:$('flabel').value});$('fpath').value='';$('flabel').value='';$('fmsg').innerHTML='<span class="ok">Folder added - indexing started.</span>';setTimeout(loadFolders,800)}catch(e){$('fmsg').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
async function togFolder(id){await api('/api/folders/'+id+'/toggle',{});loadFolders();loadMeta()}
async function delFolder(id){if(!confirm('Remove this folder from the index? (Your files are NOT deleted.)'))return;await api('/api/folders/'+id+'/delete',{});loadFolders();loadMeta()}
async function rescan(id){try{await api('/api/scan',{id});setTimeout(loadFolders,600);loadMeta()}catch(e){$('fmsg').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
async function saveInt(){await api('/api/settings',{interval:+$('fint').value||0});$('fmsg').innerHTML='<span class="ok">Saved.</span>'}

/* ---------- settings (admin) ---------- */
async function loadSettings(){try{const s=await api('/api/settings');$('sHost').value=s.host;$('sPort').value=s.port;$('sSec').value=s.security;$('sUser').value=s.user;$('sFrom').value=s.sender;$('sPass').placeholder=s.has_password?'(password saved - leave empty to keep)':'Password / app password';$('sMax').value=s.max_attach_mb;$('sTeams').value=s.teams_webhook;$('sPub').value=s.public_url;
 const u=await api('/api/users');$('utab').querySelector('tbody').innerHTML=u.users.map(x=>'<tr><td>'+esc(x.username)+'</td><td>'+x.role+'</td><td>'+esc(x.last_login||'-')+'</td><td>'+(x.is_main?'<span class="msg">main admin</span>':'<button class="sec sm" onclick="resetPw('+x.id+')">Reset password</button> <button class="sec sm" onclick="setRole('+x.id+',\''+(x.role=='admin'?'user':'admin')+'\')">Make '+(x.role=='admin'?'user':'admin')+'</button> <button class="danger sm" onclick="delUser('+x.id+')">Delete</button>')+'</td></tr>').join('');
 const a=await api('/api/activity');$('atab').querySelector('tbody').innerHTML=a.rows.map(r=>'<tr><td>'+esc(r.at)+'</td><td>'+esc(r.by_user)+'</td><td>'+esc(r.action)+'</td><td>'+esc(r.detail)+'</td></tr>').join('')}catch(e){}}
async function saveSmtp(){try{await api('/api/settings',{host:$('sHost').value,port:$('sPort').value,security:$('sSec').value,user:$('sUser').value,sender:$('sFrom').value,password:$('sPass').value,max_attach_mb:+$('sMax').value||20});$('sPass').value='';$('smsg').innerHTML='<span class="ok">Saved.</span>';loadMeta()}catch(e){$('smsg').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
async function saveTeams(){try{await api('/api/settings',{teams_webhook:$('sTeams').value});$('smsg').innerHTML='<span class="ok">Teams saved.</span>';loadMeta()}catch(e){$('smsg').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
async function savePub(){await api('/api/settings',{public_url:$('sPub').value});$('smsg').innerHTML='<span class="ok">Saved.</span>'}
async function addUser(){try{await api('/api/users',{username:$('uName').value,password:$('uPass').value,role:$('uRole').value});$('uName').value=$('uPass').value='';$('umsg').innerHTML='<span class="ok">User added.</span>';loadSettings()}catch(e){$('umsg').innerHTML='<span class="err">'+esc(e.message)+'</span>'}}
async function resetPw(id){const p=prompt('New password for this user (min 6):');if(!p)return;try{await api('/api/users/'+id+'/password',{password:p});alert('Password reset.')}catch(e){alert(e.message)}}
async function setRole(id,r){await api('/api/users/'+id+'/role',{role:r});loadSettings()}
async function delUser(id){if(confirm('Delete this user?')){try{await api('/api/users/'+id+'/delete',{});loadSettings()}catch(e){alert(e.message)}}}
/* ---------- preview + print ---------- */
const PREV_EXT=['pdf','png','jpg','jpeg','gif','webp','bmp','svg','txt'];
let PV={list:[],i:0,auto:false};
const canPrev=d=>PREV_EXT.includes((d.ext||'').toLowerCase());
const viewUrl=d=>d.url||('/api/file/'+d.id+'/view');
function pvLists(n){return n=='s'?LAST:n=='sel'?[...SEL.values()]:n=='rel'?(window.REL||[]):[]}
function pv(n,i,auto){PV.list=pvLists(n);PV.i=i;PV.auto=!!auto;pvShow()}
function pvDoc(d,auto){PV.list=[d];PV.i=0;PV.auto=!!auto;pvShow()}
function pvReq(i,k,auto){pvDoc(REQ[i].matches[k],auto)}
function pvShow(){const d=PV.list[PV.i];if(!d)return;const fr=$('pvFrame'),many=PV.list.length>1;
 $('pvName').textContent=d.name;$('pvPos').textContent=many?(PV.i+1)+' / '+PV.list.length:'';$('pvPrev').style.display=$('pvNext').style.display=many?'inline-block':'none';
 $('pvov').style.display='flex';
 if(canPrev(d)){fr.removeAttribute('srcdoc');fr.src=viewUrl(d)}
 else{PV.auto=false;fr.removeAttribute('src');fr.srcdoc='<body style="font:16px Segoe UI,Arial,sans-serif;padding:24px;color:#234">Preview is not available for <b>.'+esc((d.ext||'').toUpperCase())+'</b> files in the browser.<br><br>Use <b>Download</b> and open it in its own program.</body>'}}
function pvStep(dir){if(PV.list.length<2)return;PV.i=(PV.i+dir+PV.list.length)%PV.list.length;PV.auto=false;pvShow()}
function pvPrint(){const d=PV.list[PV.i];if(!d)return;if(!canPrev(d)){alert('.'+(d.ext||'').toUpperCase()+' files cannot be printed from the browser. Download it and print from its own program.');return}
 try{const w=$('pvFrame').contentWindow;w.focus();w.print();api('/api/log',{action:'print',detail:d.name}).catch(()=>{})}
 catch(e){window.open(viewUrl(d),'_blank');alert('Use the print button of the opened PDF viewer.')}}
function pvDownload(){const d=PV.list[PV.i];if(d)location=d.dl||('/api/file/'+d.id+'/download')}
function pvTab(){const d=PV.list[PV.i];if(d)window.open(viewUrl(d),'_blank')}
function closePv(){$('pvov').style.display='none';$('pvFrame').removeAttribute('src');$('pvFrame').removeAttribute('srcdoc')}
$('pvFrame').addEventListener('load',()=>{if(PV.auto){PV.auto=false;setTimeout(pvPrint,500)}});
document.addEventListener('keydown',e=>{if($('pvov').style.display=='flex'){if(e.key=='Escape')closePv();else if(e.key=='ArrowRight')pvStep(1);else if(e.key=='ArrowLeft')pvStep(-1)}});
async function selOut(auto){const ids=[...SEL.keys()];if(!ids.length){msg('<span class="err">Select documents first.</span>');return}
 const a=procAct();if(ids.length==1&&(a=='original'||a=='zip')){pvDoc(SEL.get(ids[0]),auto);return}
 const action=a=='merge_compress'?'merge_compress':(a=='compress'&&ids.length==1?'compress':'merge');
 msg('Preparing one PDF from the selected files, in the order shown...');
 try{const j=await api('/api/process',{ids,action,level:$('level').value,name:'',zip:false});const o=j.outputs[0];
  pvDoc({id:0,name:o.name,ext:'pdf',url:'/api/output/'+o.token+'/view',dl:o.url},auto);
  msg('<span class="ok">Ready. </span>'+esc(j.info)+(j.warnings.length?'<br><span class="err">'+j.warnings.map(esc).join('<br>')+'</span>':''))}
 catch(e){msg('<span class="err">'+esc(e.message)+'</span>')}}
function printSearch(){if($('bulk').style.display=='block')printList('bktab','Document check - Found / Not found');else printList('res','Search results: '+$('q').value.trim(),$('sinfo').textContent)}
function printList(elId,title,extra){const src=$(elId);if(!src||src.style.display=='none'||!src.textContent.trim()){alert('Nothing to print yet.');return}
 const c=src.cloneNode(true);c.querySelectorAll('button,input,iframe,a,script').forEach(e=>e.remove());c.removeAttribute('style');
 const w=window.open('','_blank');if(!w){alert('Please allow pop-ups for this site to print.');return}
 w.document.write('<!doctype html><html><head><meta charset="utf-8"><title>'+esc(title)+'</title><style>body{font:12px Segoe UI,Arial,sans-serif;margin:16px;color:#111}h2{margin:0 0 2px;font-size:17px}.sub{color:#555;margin-bottom:10px}table{border-collapse:collapse;width:100%}th,td{border:1px solid #999;padding:4px 6px;text-align:left;vertical-align:top}th{background:#e9eef4}tr{page-break-inside:avoid}.kv{display:grid;grid-template-columns:150px 1fr;gap:4px 10px}.msg{color:#555}mark{background:none}label{display:block}</style></head><body><h2>'+esc(title)+'</h2><div class="sub">Printed by '+esc(ME.username||'')+' on '+new Date().toLocaleString()+(extra?' - '+esc(extra):'')+'</div>'+c.outerHTML+'</body></html>');
 w.document.close();w.focus();setTimeout(()=>{w.print()},300)}
boot();
</script></body></html>"""

LOGIN_PAGE = r"""<!doctype html><html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Document Finder - Login</title>
<style>body{font-family:Segoe UI,Arial,sans-serif;background:#f3f5f8;margin:0;display:flex;min-height:100vh;align-items:center;justify-content:center}.box{background:#fff;padding:28px;border-radius:12px;box-shadow:0 2px 12px #0003;width:min(380px,92vw)}h1{margin:0 0 4px;color:#12355b;font-size:24px}p{color:#667;margin:0 0 18px}input{width:100%;font-size:18px;padding:11px;border:2px solid #9fb3c8;border-radius:8px;margin-bottom:12px;box-sizing:border-box}button{width:100%;font-size:18px;padding:11px;border:0;border-radius:8px;background:#12355b;color:#fff;font-weight:600;cursor:pointer}button:disabled{background:#b8c2cc}.err{color:#b42318;font-weight:600;min-height:22px;margin-bottom:8px}</style></head><body>
<form class="box" onsubmit="go();return false"><h1>Document Finder</h1><p>Please log in with your user name.</p>
<input id="u" placeholder="User name" autocomplete="username" autofocus><input id="p" type="password" placeholder="Password" autocomplete="current-password">
<div class="err" id="e"></div><button id="b">Log in</button></form>
<script>
async function go(){const b=document.getElementById('b'),e=document.getElementById('e');b.disabled=true;e.textContent='';
 try{const r=await fetch('/api/login',{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({username:document.getElementById('u').value,password:document.getElementById('p').value})});
  const j=await r.json();if(j.ok){location='/'}else{e.textContent=j.error||'Login failed.'}}
 catch(x){e.textContent='Server not reachable.'}
 b.disabled=false}
</script></body></html>"""

init_db()
app.secret_key = load_secret()

if __name__ == "__main__":
    try:
        ip = socket.gethostbyname(socket.gethostname())
    except Exception:
        ip = "this-pc-ip"
    start_tunnel()
    start_auto_scan()
    print(f"\n  Document Finder running.\n  On this PC : http://localhost:{PORT}\n  Others (same network): http://{ip}:{PORT}")
    print(f"  Ghostscript (best PDF compression): {'found' if find_gs() else 'not found - PyMuPDF/pypdf will be used'}\n")
    try:
        from waitress import serve
        serve(app, host="0.0.0.0", port=PORT, threads=8)
    except ImportError:
        app.run(host="0.0.0.0", port=PORT)
