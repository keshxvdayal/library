import os, json, sqlite3, secrets, urllib.request
from datetime import datetime, timedelta
from functools import wraps
from flask import (Flask, g, render_template, request, redirect, url_for,
                   session, flash, abort, jsonify)
from markupsafe import Markup
from werkzeug.security import generate_password_hash, check_password_hash

app = Flask(__name__)
app.config.update(SECRET_KEY=os.environ.get("SECRET_KEY") or secrets.token_hex(32),
                  SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
DB = os.environ.get("DATABASE", "library.db")
FEE, OWNER_SHARE, LOAN_DAYS = 100, 70, 14          # cents, cents, days
OPEN = ("requested", "approved", "active")
SCHEMA = """
create table if not exists users(id integer primary key, name text not null,
  email text unique not null, pw text not null, balance integer not null default 0);
create table if not exists books(id integer primary key, owner_id integer not null references users(id),
  isbn text, title text not null, author text, code text unique not null,
  created text default current_timestamp);
create table if not exists loans(id integer primary key, book_id integer not null references books(id),
  borrower_id integer not null references users(id), status text not null, due text,
  created text default current_timestamp);
create table if not exists tx(id integer primary key, user_id integer not null references users(id),
  cents integer not null, label text not null, ref text unique, created text default current_timestamp);
"""

def db():
    if "db" not in g:
        g.db = sqlite3.connect(DB)
        g.db.row_factory = sqlite3.Row
        g.db.execute("pragma foreign_keys=on")
    return g.db
@app.teardown_appcontext
def _close(e):
    d = g.pop("db", None)
    if d: d.close()
def q(sql, *a): return db().execute(sql, a).fetchall()
def q1(sql, *a): return db().execute(sql, a).fetchone()
def x(sql, *a): return db().execute(sql, a)
with app.app_context():
    db().executescript(SCHEMA)

# ---- helpers ----
def me(): return q1("select * from users where id=?", session["uid"]) if "uid" in session else None
def login_required(f):
    @wraps(f)
    def w(*a, **k):
        if not me(): return redirect(url_for("login"))
        return f(*a, **k)
    return w
def credit(uid, cents, label, ref=None):
    x("update users set balance=balance+? where id=?", cents, uid)
    x("insert into tx(user_id,cents,label,ref) values(?,?,?,?)", uid, cents, label, ref)
@app.before_request
def csrf_protect():
    if request.method == "POST" and (not session.get("csrf") or request.form.get("_csrf") != session["csrf"]):
        abort(400)
@app.context_processor
def inject():
    tok = session.setdefault("csrf", secrets.token_hex(16))
    return {"user": me(), "csrf_field": lambda: Markup(f'<input type="hidden" name="_csrf" value="{tok}">')}
@app.template_filter("usd")
def usd(c): return f"${c/100:,.2f}"

# ---- auth ----
@app.route("/")
def index(): return redirect(url_for("dashboard" if me() else "login"))

@app.route("/signup", methods=["GET", "POST"])
def signup():
    if request.method == "POST":
        n, e, p = request.form["name"].strip(), request.form["email"].strip().lower(), request.form["password"]
        if not n or "@" not in e or len(p) < 8:
            flash("Enter a name, a valid email and a password of 8+ characters.")
        else:
            try:
                c = x("insert into users(name,email,pw) values(?,?,?)", n, e, generate_password_hash(p))
                db().commit(); session.clear(); session["uid"] = c.lastrowid
                return redirect(url_for("dashboard"))
            except sqlite3.IntegrityError:
                flash("That email is already registered.")
    return render_template("auth.html", mode="signup")

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        u = q1("select * from users where email=?", request.form["email"].strip().lower())
        if u and check_password_hash(u["pw"], request.form["password"]):
            session.clear(); session["uid"] = u["id"]
            return redirect(url_for("dashboard"))
        flash("Wrong email or password.")
    return render_template("auth.html", mode="login")

@app.post("/logout")
def logout():
    session.clear(); return redirect(url_for("login"))

# ---- books ----
@app.get("/api/isbn/<isbn>")
@login_required
def isbn_lookup(isbn):
    isbn = "".join(c for c in isbn if c.isdigit() or c in "Xx")
    try:
        url = f"https://openlibrary.org/api/books?bibkeys=ISBN:{isbn}&format=json&jscmd=data"
        with urllib.request.urlopen(url, timeout=6) as r:
            d = json.load(r).get(f"ISBN:{isbn}")
    except Exception:
        d = None
    if not d: return jsonify(found=False)
    return jsonify(found=True, title=d.get("title", ""), author=", ".join(a["name"] for a in d.get("authors", [])))

@app.post("/books")
@login_required
def add_book():
    t = request.form["title"].strip()[:200]
    if not t:
        flash("Title is required."); return redirect(url_for("dashboard"))
    isbn = "".join(c for c in request.form.get("isbn", "") if c.isdigit() or c in "Xx")
    c = x("insert into books(owner_id,isbn,title,author,code) values(?,?,?,?,?)", session["uid"], isbn, t,
          request.form.get("author", "").strip()[:200], "BK-" + secrets.token_hex(4).upper())
    db().commit()
    return redirect(url_for("book_qr", bid=c.lastrowid))

@app.get("/books/<int:bid>")
@login_required
def book_qr(bid):
    b = q1("select * from books where id=? and owner_id=?", bid, session["uid"])
    if not b: abort(404)
    return render_template("book.html", b=b)

@app.post("/books/<int:bid>/delete")
@login_required
def delete_book(bid):
    b = q1("select * from books where id=? and owner_id=?", bid, session["uid"])
    if not b: abort(404)
    if q1("select 1 from loans where book_id=? and status in ('requested','approved','active')", bid):
        flash("This book has an open request or loan."); return redirect(url_for("dashboard"))
    x("delete from loans where book_id=?", bid); x("delete from books where id=?", bid); db().commit()
    return redirect(url_for("dashboard"))

@app.get("/browse")
@login_required
def browse():
    s = f"%{request.args.get('q', '').strip()}%"
    books = q("""select b.*, u.name owner, exists(select 1 from loans l where l.book_id=b.id
                 and l.status in ('requested','approved','active')) busy
                 from books b join users u on u.id=b.owner_id
                 where b.owner_id!=? and (b.title like ? or b.author like ?) order by b.id desc""",
              session["uid"], s, s)
    return render_template("browse.html", books=books)

# ---- loans ----
@app.get("/dashboard")
@login_required
def dashboard():
    uid = session["uid"]
    books = q("""select b.*, (select status from loans where book_id=b.id and status in ('requested','approved','active')) ls
                 from books b where owner_id=? order by id desc""", uid)
    incoming = q("""select l.*, b.title, u.name person from loans l join books b on b.id=l.book_id
                    join users u on u.id=l.borrower_id where b.owner_id=? and l.status in ('requested','approved','active')
                    order by l.id desc""", uid)
    outgoing = q("""select l.*, b.title, u.name person from loans l join books b on b.id=l.book_id
                    join users u on u.id=b.owner_id where l.borrower_id=? order by l.id desc limit 30""", uid)
    return render_template("dashboard.html", books=books, incoming=incoming, outgoing=outgoing)

@app.post("/books/<int:bid>/borrow")
@login_required
def borrow(bid):
    u, b = me(), q1("select * from books where id=?", bid)
    if not b or b["owner_id"] == u["id"]: abort(404)
    if q1("select 1 from loans where book_id=? and status in ('requested','approved','active')", bid):
        flash("Sorry, that book was just taken."); return redirect(url_for("browse"))
    if u["balance"] < FEE:
        flash("Add credits to your wallet first."); return redirect(url_for("wallet"))
    credit(u["id"], -FEE, f"Held for “{b['title']}”")
    x("insert into loans(book_id,borrower_id,status) values(?,?,'requested')", bid, u["id"]); db().commit()
    flash("Request sent. Your $1 is held and refunded if the owner declines or you cancel.")
    return redirect(url_for("dashboard"))

@app.post("/loans/<int:lid>/<act>")
@login_required
def loan_act(lid, act):
    uid = session["uid"]
    l = q1("""select l.*, b.owner_id, b.code, b.title from loans l join books b on b.id=l.book_id where l.id=?""", lid)
    if not l: abort(404)
    owner, bor, st = l["owner_id"] == uid, l["borrower_id"] == uid, l["status"]
    def setst(s, due=None): x("update loans set status=?, due=? where id=?", s, due or l["due"], lid)
    if act == "accept" and owner and st == "requested": setst("approved")
    elif (act == "decline" and owner and st in OPEN[:2]) or (act == "cancel" and bor and st in OPEN[:2]):
        credit(l["borrower_id"], FEE, f"Refund: “{l['title']}”")
        setst("declined" if act == "decline" else "cancelled")
    elif act == "handoff" and bor and st == "approved":
        if request.form.get("code", "").strip().upper() != l["code"]:
            flash("That code doesn’t match the QR inside the book."); return redirect(url_for("dashboard"))
        setst("active", (datetime.utcnow() + timedelta(days=LOAN_DAYS)).strftime("%d %b %Y"))
        credit(l["owner_id"], OWNER_SHARE, f"Lent “{l['title']}”")
    elif act == "return" and owner and st == "active": setst("returned")
    else: abort(400)
    db().commit()
    return redirect(url_for("dashboard"))

# ---- wallet ----
@app.get("/wallet")
@login_required
def wallet():
    return render_template("wallet.html", txs=q("select * from tx where user_id=? order by id desc limit 50", session["uid"]),
                           stripe=bool(os.environ.get("STRIPE_SECRET_KEY")), dev=os.environ.get("DEV_FREE_CREDITS") == "1")

@app.post("/wallet/topup")
@login_required
def topup():
    amt = int(request.form.get("amount", 0))
    if amt not in (5, 10, 20): abort(400)
    key = os.environ.get("STRIPE_SECRET_KEY")
    if key:
        import stripe; stripe.api_key = key
        s = stripe.checkout.Session.create(mode="payment", client_reference_id=str(session["uid"]),
            line_items=[{"quantity": 1, "price_data": {"currency": "usd", "unit_amount": amt * 100,
                         "product_data": {"name": "ShelfShare credits"}}}],
            success_url=url_for("topup_ok", _external=True) + "?sid={CHECKOUT_SESSION_ID}",
            cancel_url=url_for("wallet", _external=True))
        return redirect(s.url, 303)
    if os.environ.get("DEV_FREE_CREDITS") == "1":
        credit(session["uid"], amt * 100, "Dev credits", secrets.token_hex(8)); db().commit()
    else:
        flash("Payments aren’t configured on this server.")
    return redirect(url_for("wallet"))

@app.get("/wallet/success")
@login_required
def topup_ok():
    import stripe; stripe.api_key = os.environ["STRIPE_SECRET_KEY"]
    sid = request.args.get("sid", "")
    s = stripe.checkout.Session.retrieve(sid)
    if s.payment_status == "paid" and s.client_reference_id == str(session["uid"]):
        try:
            credit(session["uid"], s.amount_total, "Wallet top-up", sid); db().commit()
            flash("Credits added.")
        except sqlite3.IntegrityError:
            db().rollback()
    return redirect(url_for("wallet"))

if __name__ == "__main__":
    app.run(debug=os.environ.get("FLASK_DEBUG") == "1")
