from flask import Flask, render_template, request, redirect, url_for, flash, make_response, session
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, current_user, login_required, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, time, timedelta
import os, secrets, requests

app = Flask(__name__)
app.config["SQLALCHEMY_DATABASE_URI"] = os.environ.get("DATABASE_URL","sqlite:///gym.db")
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False
app.secret_key = os.environ.get("SECRET_KEY","dev-secret")
BRAND_NAME = os.environ.get("BRAND_NAME","Pgym 2.0")
DEFAULT_LOCATION = os.environ.get("DEFAULT_LOCATION","Pgym 2.0")

db = SQLAlchemy(app)
login_manager = LoginManager(app)
login_manager.login_view = "login"

# --- Models ---
class User(db.Model, UserMixin):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="coach")
    is_active_user = db.Column(db.Boolean, default=True)
    def set_password(self, pw): self.password_hash = generate_password_hash(pw)
    def check_password(self, pw): return check_password_hash(self.password_hash, pw)

class Member(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    email = db.Column(db.String(120), nullable=True, unique=True)
    phone = db.Column(db.String(50), nullable=True)

class ClassSession(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(120), nullable=False)
    coach = db.Column(db.String(120), nullable=True)
    date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    capacity = db.Column(db.Integer, nullable=False, default=2)
    location = db.Column(db.String(120), nullable=True)
    bookings = db.relationship("Booking", backref="class_session", cascade="all, delete-orphan")

class Booking(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    class_id = db.Column(db.Integer, db.ForeignKey("class_session.id"), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    member = db.relationship("Member")
    __table_args__ = (db.UniqueConstraint("member_id", "class_id", name="uq_member_class"),)

class AppSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    weeks_ahead = db.Column(db.Integer, default=int(os.environ.get("WEEKS_AHEAD","4")))
    personal_capacity = db.Column(db.Integer, default=int(os.environ.get("DEFAULT_PERSONAL_CAPACITY","2")))
    personal_duration_min = db.Column(db.Integer, default=int(os.environ.get("DEFAULT_PERSONAL_DURATION_MIN","60")))
    personal_coach = db.Column(db.String(120), default=os.environ.get("PERSONAL_COACH",""))

class Package(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    total = db.Column(db.Integer, nullable=False)
    remaining = db.Column(db.Integer, nullable=False)
    activated_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    member = db.relationship("Member")

class PackagePurchase(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    package_size = db.Column(db.Integer, nullable=False)  # 1, 8, 16, ...
    price = db.Column(db.Numeric(10,2), nullable=True)
    purchased_at = db.Column(db.DateTime, default=datetime.utcnow)
    activated_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
    member = db.relationship("Member")

class MagicToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_id = db.Column(db.Integer, db.ForeignKey("member.id"), nullable=False)
    token = db.Column(db.String(64), unique=True, nullable=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=False)
    used = db.Column(db.Boolean, default=False)
    member = db.relationship("Member")

@login_manager.user_loader
def load_user(user_id):
    return User.query.get(int(user_id))

# --- Helpers ---
def parse_date(s): return datetime.strptime(s, "%Y-%m-%d").date()
def parse_time(s): return datetime.strptime(s, "%H:%M").time()
def daterange(start_date, end_date):
    for n in range((end_date - start_date).days + 1):
        yield start_date + timedelta(n)

def ensure_settings():
    s = AppSettings.query.first()
    if not s:
        s = AppSettings()
        db.session.add(s)
        db.session.commit()
    return s

def slot_ranges_for_personal(day: date):
    wd = day.weekday()
    slots = []
    def add_block(start_h, end_h):
        cur_dt = datetime.combine(day, time(hour=start_h))
        dur = ensure_settings().personal_duration_min
        while cur_dt.time() < time(hour=end_h):
            end_dt = cur_dt + timedelta(minutes=dur)
            if end_dt.time() > time(hour=end_h): break
            slots.append((cur_dt.time(), end_dt.time()))
            cur_dt = end_dt
    if wd in (0,2,4):  # Mon, Wed, Fri
        add_block(8,20)
    elif wd in (1,3):  # Tue, Thu
        add_block(6,20)
    elif wd == 5:      # Sat
        add_block(9,11); add_block(16,19)
    return slots

def upsert_personal_slots():
    settings = ensure_settings()
    today = date.today()
    end_day = today + timedelta(weeks=settings.weeks_ahead)
    for d in daterange(today, end_day):
        for start, end in slot_ranges_for_personal(d):
            exists = ClassSession.query.filter_by(
                title="Personal", date=d, start_time=start, end_time=end, location=DEFAULT_LOCATION
            ).first()
            if not exists:
                cs = ClassSession(
                    title="Personal",
                    coach=settings.personal_coach or None,
                    date=d, start_time=start, end_time=end,
                    capacity=settings.personal_capacity,
                    location=DEFAULT_LOCATION
                )
                db.session.add(cs)
    db.session.commit()

def member_remaining_entries(member_id):
    pkg = Package.query.filter_by(member_id=member_id).order_by(Package.activated_at.desc()).first()
    return pkg.remaining if pkg else 0

def week_bounds(ref: date):
    start = ref - timedelta(days=ref.weekday())
    end = start + timedelta(days=6)
    return start, end

# WhatsApp sender
def send_whatsapp_text(to_e164: str, body: str) -> bool:
    token = os.environ.get("WHATSAPP_TOKEN")
    phone_id = os.environ.get("WHATSAPP_PHONE_ID")
    if not token or not phone_id or not to_e164:
        return False
    url = f"https://graph.facebook.com/v17.0/{phone_id}/messages"
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    data = {
        "messaging_product": "whatsapp",
        "to": to_e164,
        "type": "text",
        "text": {"body": body}
    }
    try:
        r = requests.post(url, headers=headers, json=data, timeout=15)
        r.raise_for_status()
        return True
    except Exception as e:
        print("WhatsApp send error:", e, getattr(r, "text", ""))
        return False

# --- Customer session helpers ---
def current_member():
    mid = session.get("member_id")
    return Member.query.get(mid) if mid else None

def require_member():
    m = current_member()
    if not m:
        flash("Per accedere al profilo, richiedi un link magico.", "warning")
        return redirect(url_for("magic_login_request"))
    return m

# --- Routes (Public) ---
@app.route("/")
def index():
    today = date.today()
    start, end = week_bounds(today)
    sessions = ClassSession.query.filter(ClassSession.date>=start, ClassSession.date<=end)        .order_by(ClassSession.date.asc(), ClassSession.start_time.asc()).all()
    days = { (start + timedelta(days=i)): [] for i in range(7) }
    for s in sessions:
        days[s.date].append(s)
    return render_template("index.html", days=days, start=start, end=end, brand=BRAND_NAME)

@app.route("/calendar")
def calendar_view():
    # prendi parametro GET week (YYYY-MM-DD), altrimenti oggi
    week_str = request.args.get("week")
    if week_str:
        ref_date = datetime.strptime(week_str, "%Y-%m-%d").date()
    else:
        ref_date = date.today()
    start = ref_date - timedelta(days=ref_date.weekday())
    end = start + timedelta(days=6)

    sessions = ClassSession.query.filter(ClassSession.date>=start, ClassSession.date<=end)\
        .order_by(ClassSession.date, ClassSession.start_time).all()

    days = { (start + timedelta(days=i)): [] for i in range(7) }
    for s in sessions:
        days[s.date].append(s)

    prev_week = (start - timedelta(days=7)).strftime("%Y-%m-%d")
    next_week = (start + timedelta(days=7)).strftime("%Y-%m-%d")

    return render_template("calendar.html", days=days, start=start, end=end,
                           prev_week=prev_week, next_week=next_week, brand=BRAND_NAME)


@app.route("/register", methods=["GET","POST"])
def register():
    if request.method == "POST":
        name = request.form.get("name","").strip()
        email = (request.form.get("email") or "").strip().lower() or None
        phone = (request.form.get("phone") or "").strip() or None
        if not name:
            flash("Inserisci il nome.", "danger"); return redirect(url_for("register"))
        if email:
            ex = Member.query.filter_by(email=email).first()
            if ex:
                ex.name = name; ex.phone = phone; db.session.commit()
                flash("Registrazione aggiornata. Benvenuto!", "success"); return redirect(url_for("index"))
        m = Member(name=name, email=email, phone=phone)
        db.session.add(m); db.session.commit()
        flash("Registrazione completata. Benvenuto!", "success")
        return redirect(url_for("index"))
    return render_template("register.html", brand=BRAND_NAME)

# Magic link request
@app.route("/login-magic", methods=["GET","POST"])
def magic_login_request():
    if request.method == "POST":
        email = (request.form.get("email") or "").strip().lower()
        phone = (request.form.get("phone") or "").strip()
        member = None
        if email:
            member = Member.query.filter_by(email=email).first()
        if not member and phone:
            member = Member.query.filter_by(phone=phone).first()
        if not member:
            if not email and not phone:
                flash("Inserisci almeno email o telefono.", "danger")
                return redirect(url_for("magic_login_request"))
            member = Member(name="Cliente", email=email or None, phone=phone or None)
            db.session.add(member); db.session.flush()
        token = secrets.token_urlsafe(32)
        ttl_minutes = 30
        mt = MagicToken(member_id=member.id, token=token, expires_at=datetime.utcnow()+timedelta(minutes=ttl_minutes))
        db.session.add(mt); db.session.commit()
        site_url = os.environ.get("SITE_URL")
        base = site_url.rstrip("/") if site_url else request.host_url.rstrip("/")
        link = f"{base}/m/{token}"
        msg = (
            f"👋 Ciao! Ecco il tuo link di accesso a Pgym 2.0:\n"
            f"{link}\n\n"
            f"⏱️ Valido per {ttl_minutes} minuti."
        )
        sent = False
        if member.phone:
            sent = send_whatsapp_text(member.phone, msg)
        if sent:
            flash("Ti abbiamo inviato il link via WhatsApp 👍", "success")
            return redirect(url_for("index"))
        else:
            return render_template("magic_sent.html", link=link, ttl=ttl_minutes, brand=BRAND_NAME)
    return render_template("login_magic.html", brand=BRAND_NAME)

@app.route("/m/<token>")
def magic_login_token(token):
    mt = MagicToken.query.filter_by(token=token).first()
    if not mt:
        flash("Link non valido.", "danger"); return redirect(url_for("magic_login_request"))
    if mt.used:
        flash("Questo link è già stato usato. Richiedine uno nuovo.", "warning"); return redirect(url_for("magic_login_request"))
    if datetime.utcnow() > mt.expires_at:
        flash("Link scaduto. Richiedine uno nuovo.", "warning"); return redirect(url_for("magic_login_request"))
    session["member_id"] = mt.member_id
    mt.used = True; db.session.commit()
    flash("Accesso effettuato!", "success")
    return redirect(url_for("member_profile"))

@app.route("/me")
def member_profile():
    m = current_member()
    if not m:
        return require_member()
    bookings = Booking.query.filter_by(member_id=m.id).order_by(Booking.created_at.desc()).all()
    return render_template("profile.html", member=m, bookings=bookings, brand=BRAND_NAME)

@app.route("/logout-member")
def logout_member():
    session.pop("member_id", None)
    flash("Sei uscito dall’area cliente.", "info")
    return redirect(url_for("index"))

# Booking and class detail (minimal paths to keep demo concise)
@app.route("/admin/classes/<int:class_id>")
def class_detail(class_id):
    cs = ClassSession.query.get_or_404(class_id)
    spots_left = cs.capacity - len(cs.bookings)
    return render_template("class_detail.html", cs=cs, spots_left=spots_left, brand=BRAND_NAME)

@app.route("/book/<int:class_id>", methods=["GET","POST"])
def book(class_id):
    cs = ClassSession.query.get_or_404(class_id)
    spots_left = cs.capacity - len(cs.bookings)
    if request.method == "POST":
        name = request.form["name"].strip()
        email = request.form.get("email","").strip()
        phone = request.form.get("phone","").strip()
        member = None
        if email: member = Member.query.filter_by(email=email).first()
        if not member and name: member = Member.query.filter_by(name=name).first()
        if not member:
            member = Member(name=name or "Cliente", email=email or None, phone=phone or None)
            db.session.add(member); db.session.flush()
        if len(cs.bookings) >= cs.capacity:
            flash("Capienza raggiunta.", "danger"); db.session.rollback(); return redirect(url_for("index"))
        booking = Booking(member_id=member.id, class_id=cs.id)
        db.session.add(booking); db.session.commit()
        flash("Prenotazione effettuata!", "success")
        return redirect(url_for("class_detail", class_id=cs.id))
    return render_template("book.html", cs=cs, spots_left=spots_left, brand=BRAND_NAME)

@app.route("/ics/booking/<int:booking_id>.ics")
def ics_booking(booking_id):
    b = Booking.query.get_or_404(booking_id)
    s = b.class_session
    dt_start = datetime.combine(s.date, s.start_time)
    dt_end = datetime.combine(s.date, s.end_time)
    ics = f"""BEGIN:VCALENDAR
VERSION:2.0
PRODID:-//Pgym//IT
BEGIN:VEVENT
UID:booking-{b.id}@pgym
DTSTAMP:{datetime.utcnow().strftime('%Y%m%dT%H%M%SZ')}
DTSTART:{dt_start.strftime('%Y%m%dT%H%M%S')}
DTEND:{dt_end.strftime('%Y%m%dT%H%M%S')}
SUMMARY:{s.title} con {s.coach or 'coach'}
LOCATION:{s.location or ''}
DESCRIPTION:Prenotazione per {b.member.name}
END:VEVENT
END:VCALENDAR
"""
    resp = make_response(ics)
    resp.headers["Content-Type"] = "text/calendar; charset=utf-8"
    resp.headers["Content-Disposition"] = f"attachment; filename=booking-{b.id}.ics"
    return resp

# --- Admin auth (placeholder minimal for this update) ---
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        email = request.form["email"].strip().lower()
        pw = request.form["password"]
        u = User.query.filter_by(email=email, is_active_user=True).first()
        if u and u.check_password(pw):
            login_user(u); return redirect(url_for("index"))
        flash("Credenziali non valide.", "danger")
    return render_template("login_magic.html", brand=BRAND_NAME)

@app.route("/logout")
def logout():
    logout_user()
    flash("Logout effettuato.", "info")
    return redirect(url_for("index"))

# CLI init
@app.cli.command("init-db")
def init_db():
    db.create_all()
    if not User.query.filter_by(role="admin").first():
        admin_email = os.environ.get("ADMIN_EMAIL","admin@pgym.local").lower()
        admin_pw = os.environ.get("ADMIN_PASSWORD","admin")
        u = User(name="Admin", email=admin_email, role="admin", is_active_user=True)
        u.set_password(admin_pw); db.session.add(u)
    ensure_settings()
    db.session.commit()
    try:
        upsert_personal_slots()
    except Exception:
        pass
    print("DB inizializzato, admin creato, slot Personal generati (se possibile).")

with app.app_context():
    db.create_all()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
