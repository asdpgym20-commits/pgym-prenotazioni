from flask import Flask, render_template, request, redirect, url_for, flash, make_response
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager, login_user, logout_user, current_user, login_required, UserMixin
from werkzeug.security import generate_password_hash, check_password_hash
from datetime import datetime, date, time, timedelta
import os

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
    role = db.Column(db.String(20), nullable=False, default="coach")  # "admin" or "coach"
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
    package_size = db.Column(db.Integer, nullable=False)  # 1, 8, 16, 24, ...
    price = db.Column(db.Numeric(10,2), nullable=True)
    purchased_at = db.Column(db.DateTime, default=datetime.utcnow)
    activated_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime, nullable=True)
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
    wd = day.weekday()  # 0=Mon
    slots = []
    def add_block(start_h, end_h):
        cur_dt = datetime.combine(day, time(hour=start_h))
        dur = ensure_settings().personal_duration_min
        while cur_dt.time() < time(hour=end_h):
            end_dt = cur_dt + timedelta(minutes=dur)
            if end_dt.time() > time(hour=end_h): break
            slots.append((cur_dt.time(), end_dt.time()))
            cur_dt = end_dt
    if wd in (0,2,4):  # Mon, Wed, Fri: 08-20
        add_block(8,20)
    elif wd in (1,3):  # Tue, Thu: 06-20
        add_block(6,20)
    elif wd == 5:      # Sat: 09-11 and 16-19
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

# --- Routes (Public) ---
@app.route("/")
def index():
    day = request.args.get("date")
    if day:
        try: start = parse_date(day)
        except: start = date.today()
    else:
        start = date.today()
    end = start + timedelta(days=7)
    sessions = ClassSession.query.filter(ClassSession.date>=start, ClassSession.date<end)        .order_by(ClassSession.date.asc(), ClassSession.start_time.asc()).all()
    spots_left = {s.id: s.capacity - len(s.bookings) for s in sessions}
    return render_template("index.html", sessions=sessions, spots_left=spots_left, day=start, end_day=end, brand=BRAND_NAME)

@app.route("/calendar")
def calendar_view():
    today = date.today()
    start = today - timedelta(days=today.weekday())
    end = start + timedelta(days=6)
    sessions = ClassSession.query.filter(ClassSession.date>=start, ClassSession.date<=end)        .order_by(ClassSession.date.asc(), ClassSession.start_time.asc()).all()
    days = { (start + timedelta(days=i)): [] for i in range(7) }
    for s in sessions:
        days[s.date].append(s)
    return render_template("calendar.html", days=days, start=start, end=end, brand=BRAND_NAME)

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
        rem = member_remaining_entries(member.id)
        if rem <= 0:
            flash("⚠️ Non hai ingressi disponibili. Contatta la palestra per ricaricare il pacchetto.", "warning")
            db.session.rollback()
            return redirect(url_for("book", class_id=cs.id))
        if len(cs.bookings) >= cs.capacity:
            flash("Capienza raggiunta. Non è possibile prenotare.", "danger")
            db.session.rollback()
            return redirect(url_for("index"))
        booking = Booking(member_id=member.id, class_id=cs.id)
        try:
            db.session.add(booking)
            pkg = Package.query.filter_by(member_id=member.id).order_by(Package.activated_at.desc()).first()
            if pkg and pkg.remaining>0:
                pkg.remaining -= 1
            db.session.commit()
            flash("Prenotazione effettuata!", "success")
            return redirect(url_for("class_detail", class_id=cs.id))
        except Exception as e:
            db.session.rollback()
            if "uq_member_class" in str(e): 
                flash("Già prenotato per questa lezione.", "warning"); 
                return redirect(url_for("class_detail", class_id=cs.id))
            flash(f"Errore: {e}", "danger"); return redirect(url_for("index"))
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

# --- Admin & Auth ---
@app.route("/login", methods=["GET","POST"])
def login():
    if request.method=="POST":
        email = request.form["email"].strip().lower()
        pw = request.form["password"]
        u = User.query.filter_by(email=email, is_active_user=True).first()
        if u and u.check_password(pw):
            login_user(u)
            return redirect(url_for("admin"))
        flash("Credenziali non valide.", "danger")
    return render_template("login.html", brand=BRAND_NAME)

@app.route("/logout")
@login_required
def logout():
    logout_user()
    flash("Logout effettuato.", "info")
    return redirect(url_for("login"))

@app.route("/admin")
@login_required
def admin():
    today = date.today()
    ws = today - timedelta(days=today.weekday())
    we = ws + timedelta(days=6)
    sessions = ClassSession.query.filter(ClassSession.date>=ws, ClassSession.date<=we)        .order_by(ClassSession.date.asc(), ClassSession.start_time.asc()).all()
    total_spots = sum(s.capacity for s in sessions) or 1
    total_booked = sum(len(s.bookings) for s in sessions)
    occ = round((total_booked/total_spots)*100,1)
    return render_template("admin.html", sessions=sessions, occ=occ, total_booked=total_booked, brand=BRAND_NAME)

@app.route("/admin/classes/<int:class_id>")
@login_required
def class_detail(class_id):
    cs = ClassSession.query.get_or_404(class_id)
    spots_left = cs.capacity - len(cs.bookings)
    return render_template("class_detail.html", cs=cs, spots_left=spots_left, brand=BRAND_NAME)

@app.route("/admin/classes/new", methods=["GET","POST"])
@login_required
def new_class():
    if request.method=="POST":
        title = request.form["title"].strip()
        coach = request.form.get("coach","").strip()
        location = request.form.get("location","").strip() or DEFAULT_LOCATION
        date_val = parse_date(request.form["date"])
        start_time = parse_time(request.form["start_time"])
        end_time = parse_time(request.form["end_time"])
        capacity = int(request.form["capacity"])
        if end_time <= start_time:
            flash("L'orario di fine deve essere successivo all'inizio.", "danger")
            return redirect(url_for("new_class"))
        cs = ClassSession(title=title, coach=coach or None, date=date_val, start_time=start_time, end_time=end_time, capacity=capacity, location=location)
        db.session.add(cs); db.session.commit()
        flash("Lezione creata!", "success")
        return redirect(url_for("admin"))
    return render_template("new_class.html", brand=BRAND_NAME)

@app.route("/admin/classes/<int:class_id>/delete", methods=["POST"])
@login_required
def delete_class(class_id):
    cs = ClassSession.query.get_or_404(class_id)
    db.session.delete(cs); db.session.commit()
    flash("Lezione eliminata.", "info")
    return redirect(url_for("admin"))

@app.route("/members/new", methods=["GET","POST"])
def new_member():
    if request.method=="POST":
        name = request.form["name"].strip()
        email = request.form.get("email","").strip() or None
        phone = request.form.get("phone","").strip() or None
        if not name:
            flash("Il nome è obbligatorio.", "danger"); return redirect(url_for("new_member"))
        if email:
            ex = Member.query.filter_by(email=email).first()
            if ex:
                ex.name = name; ex.phone = phone; db.session.commit()
                flash("Utente aggiornato.", "success"); return redirect(url_for("admin"))
        m = Member(name=name, email=email, phone=phone)
        db.session.add(m); db.session.commit()
        flash("Membro creato!", "success"); return redirect(url_for("admin"))
    return render_template("new_member.html", brand=BRAND_NAME)

@app.route("/admin/personal-settings", methods=["GET","POST"])
@login_required
def personal_settings():
    s = ensure_settings()
    if request.method=="POST":
        s.personal_coach = request.form.get("coach","").strip()
        s.personal_capacity = int(request.form.get("capacity","2"))
        s.personal_duration_min = int(request.form.get("duration","60"))
        s.weeks_ahead = int(request.form.get("weeks_ahead","4"))
        db.session.commit()
        upsert_personal_slots()
        flash("Impostazioni Personal aggiornate e slot generati.", "success")
        return redirect(url_for("personal_settings"))
    return render_template("personal_settings.html", settings=s, brand=BRAND_NAME)

@app.route("/cancel/<int:booking_id>", methods=["POST"])
def cancel(booking_id):
    b = Booking.query.get_or_404(booking_id)
    class_id = b.class_id
    db.session.delete(b); db.session.commit()
    flash("Prenotazione cancellata.", "info")
    return redirect(url_for("class_detail", class_id=class_id))

# --- CLI ---
@app.cli.command("init-db")
def init_db():
    db.create_all()
    if not User.query.filter_by(role="admin").first():
        admin_email = os.environ.get("ADMIN_EMAIL","admin@pgym.local").lower()
        admin_pw = os.environ.get("ADMIN_PASSWORD","admin")
        u = User(name="Admin", email=admin_email, role="admin", is_active_user=True)
        u.set_password(admin_pw)
        db.session.add(u)
    ensure_settings()
    db.session.commit()
    upsert_personal_slots()
    print("Database inizializzato, admin creato, slot Personal generati.")

# Init on import (Render/Gunicorn)
try:
    with app.app_context():
        db.create_all()
        if not User.query.filter_by(role="admin").first():
            admin_email = os.environ.get("ADMIN_EMAIL","admin@pgym.local").lower()
            admin_pw = os.environ.get("ADMIN_PASSWORD","admin")
            u = User(name="Admin", email=admin_email, role="admin", is_active_user=True)
            u.set_password(admin_pw)
            db.session.add(u); db.session.commit()
        ensure_settings()
        upsert_personal_slots()
except Exception:
    pass

if __name__ == "__main__":
    with app.app_context():
        db.create_all()
        ensure_settings()
        upsert_personal_slots()
    app.run(host="0.0.0.0", port=5000)
