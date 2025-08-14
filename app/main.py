import os
from datetime import datetime
from typing import Generator, List, Optional

from fastapi import FastAPI, Request, Depends, Form, UploadFile, File
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware

from sqlalchemy import (
    create_engine, Column, Integer, String, DateTime, Float, Boolean,
    ForeignKey, UniqueConstraint, text
)
from sqlalchemy.orm import declarative_base, sessionmaker, Session, relationship

# --------------------------------------------------------------------------------------
# Basic app + storage
# --------------------------------------------------------------------------------------
app = FastAPI()
app.add_middleware(SessionMiddleware, secret_key=os.getenv("SESSION_SECRET", "dev-secret"))
app.mount("/static", StaticFiles(directory="static"), name="static")
templates = Jinja2Templates(directory="templates")

DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///./app.db")
engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False} if DATABASE_URL.startswith("sqlite") else {})
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db() -> Generator[Session, None, None]:
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

# --------------------------------------------------------------------------------------
# Models
# --------------------------------------------------------------------------------------
class Instructor(Base):
    __tablename__ = "instructors"
    id = Column(Integer, primary_key=True)
    name = Column(String, nullable=False, default="Coach")
    login_code = Column(String, nullable=False, unique=True)  # simple code auth

class Player(Base):
    __tablename__ = "players"
    id = Column(Integer, primary_key=True)
    first_name = Column(String, nullable=False)
    last_name = Column(String, nullable=False)
    age_group = Column(String, nullable=False)  # 7-9, 10-12, 13-15, 16-18, 18+
    email = Column(String)
    phone = Column(String)
    login_code = Column(String, unique=True, index=True)
    image_url = Column(String)
    created_at = Column(DateTime, default=datetime.utcnow)

    metrics = relationship("Metric", back_populates="player", cascade="all, delete-orphan")

class Metric(Base):
    __tablename__ = "metrics"
    id = Column(Integer, primary_key=True)
    player_id = Column(Integer, ForeignKey("players.id"), index=True, nullable=False)
    taken_at = Column(DateTime, default=datetime.utcnow, index=True)
    exit_velocity = Column(Float)
    spin_rate = Column(Float)
    launch_angle = Column(Float)

    player = relationship("Player", back_populates="metrics")

class InstructorFavorite(Base):
    __tablename__ = "instructor_favorites"
    id = Column(Integer, primary_key=True)
    instructor_id = Column(Integer, ForeignKey("instructors.id"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    __table_args__ = (UniqueConstraint("instructor_id", "player_id", name="uq_instructor_player"),)

class CoachNote(Base):
    __tablename__ = "coach_notes"
    id = Column(Integer, primary_key=True)
    instructor_id = Column(Integer, ForeignKey("instructors.id"), nullable=False)
    player_id = Column(Integer, ForeignKey("players.id"), nullable=False)
    text = Column(String, nullable=False)
    shared_with_player = Column(Boolean, default=False)
    created_at = Column(DateTime, default=datetime.utcnow)

Base.metadata.create_all(bind=engine)

# Ensure favorites table exists in SQLite even if created externally
with SessionLocal() as s:
    s.execute(text("""
        CREATE TABLE IF NOT EXISTS instructor_favorites (
          id INTEGER PRIMARY KEY AUTOINCREMENT,
          instructor_id INTEGER NOT NULL,
          player_id INTEGER NOT NULL,
          UNIQUE(instructor_id, player_id)
        );
    """))
    s.commit()

# --------------------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------------------
AGE_GROUPS = ["7-9", "10-12", "13-15", "16-18", "18+"]

def seed_demo_instructor(db: Session) -> Instructor:
    """Create a default instructor if none exists; login code from env or '999999'."""
    inst = db.query(Instructor).first()
    if not inst:
        code = os.getenv("INSTRUCTOR_DEFAULT_CODE", "999999")
        inst = Instructor(name="Coach", login_code=code)
        db.add(inst)
        db.commit()
        db.refresh(inst)
    return inst

def get_favorite_ids(db: Session, instructor_id: int) -> set[int]:
    rows = db.query(InstructorFavorite.player_id)\
             .filter(InstructorFavorite.instructor_id == instructor_id).all()
    return {r[0] for r in rows}

# --------------------------------------------------------------------------------------
# Public / Auth
# --------------------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
def home(request: Request):
    return templates.TemplateResponse("landing.html", {"request": request})

# ---------- Instructor auth ----------
@app.get("/instructor", response_class=HTMLResponse)
def instructor_login(request: Request, db: Session = Depends(get_db)):
    seed_demo_instructor(db)
    return templates.TemplateResponse("instructor_login.html", {"request": request})

@app.post("/instructor", response_class=HTMLResponse)
def instructor_do_login(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    coach = db.query(Instructor).filter(Instructor.login_code == code.strip()).first()
    if not coach:
        # show error
        return templates.TemplateResponse("instructor_login.html", {"request": request, "error": "Invalid login code."})
    request.session["instructor_id"] = coach.id
    return RedirectResponse("/instructor/clients", status_code=303)

@app.get("/logout")
def logout(request: Request):
    request.session.clear()
    return RedirectResponse("/", status_code=303)

# ---------- Player auth ----------
@app.get("/player", response_class=HTMLResponse)
def player_login(request: Request):
    return templates.TemplateResponse("player_login.html", {"request": request})

@app.post("/player", response_class=HTMLResponse)
def player_do_login(request: Request, code: str = Form(...), db: Session = Depends(get_db)):
    p = db.query(Player).filter(Player.login_code == code.strip()).first()
    if not p:
        return templates.TemplateResponse("player_login.html", {"request": request, "error": "Invalid login code."})
    request.session["player_id"] = p.id
    return RedirectResponse("/player/dashboard", status_code=303)

# --------------------------------------------------------------------------------------
# Instructor – Clients page + toggle favorites
# --------------------------------------------------------------------------------------
def group_players_by_age(players: List[Player]):
    grouped = {g: [] for g in AGE_GROUPS}
    for p in players:
        grouped.get(p.age_group, grouped["18+"]).append(p)
    return grouped

@app.get("/instructor/clients", response_class=HTMLResponse)
def instructor_clients(request: Request, db: Session = Depends(get_db)):
    inst_id = request.session.get("instructor_id")
    if not inst_id:
        return RedirectResponse("/instructor", status_code=303)

    players = db.query(Player).order_by(Player.last_name, Player.first_name).all()
    grouped = group_players_by_age(players)
    fav_ids = get_favorite_ids(db, inst_id)
    my_clients = (
        db.query(Player).join(InstructorFavorite, InstructorFavorite.player_id == Player.id)
        .filter(InstructorFavorite.instructor_id == inst_id)
        .order_by(Player.last_name, Player.first_name)
        .all()
    )
    return templates.TemplateResponse(
        "instructor_players.html",
        {
            "request": request,
            "groups": grouped,
            "fav_ids": fav_ids,
            "my_clients": my_clients
        }
    )

@app.post("/instructor/favorite/{player_id}")
def toggle_favorite(player_id: int, request: Request, db: Session = Depends(get_db)):
    inst_id = request.session.get("instructor_id")
    if not inst_id:
        return JSONResponse({"ok": False, "error": "not_logged_in"}, status_code=401)

    fav = db.query(InstructorFavorite).filter_by(instructor_id=inst_id, player_id=player_id).first()
    if fav:
        db.delete(fav)
        db.commit()
        favorited = False
    else:
        db.add(InstructorFavorite(instructor_id=inst_id, player_id=player_id))
        db.commit()
        favorited = True

    my = (
        db.query(Player.id, Player.first_name, Player.last_name, Player.age_group)
        .join(InstructorFavorite, InstructorFavorite.player_id == Player.id)
        .filter(InstructorFavorite.instructor_id == inst_id)
        .order_by(Player.last_name, Player.first_name)
        .all()
    )
    my_list = [{"id": pid, "name": f"{fn} {ln}".strip(), "age_group": ag} for (pid, fn, ln, ag) in my]
    return {"ok": True, "favorited": favorited, "my_clients": my_list}

# --------------------------------------------------------------------------------------
# Player – Dashboard
# --------------------------------------------------------------------------------------
@app.get("/player/dashboard", response_class=HTMLResponse)
def player_dashboard(request: Request, db: Session = Depends(get_db)):
    player_id = request.session.get("player_id")
    if not player_id:
        return RedirectResponse("/player", status_code=303)

    player = db.query(Player).get(player_id)
    if not player:
        return RedirectResponse("/player", status_code=303)

    # last 20 metrics (newest last so line moves left→right)
    pts = (
        db.query(Metric)
        .filter(Metric.player_id == player.id)
        .order_by(Metric.taken_at.desc())
        .limit(20)
        .all()
    )
    pts = list(reversed(pts))
    labels = [m.taken_at.strftime("%b %d") for m in pts]
    ev_vals = [round((m.exit_velocity or 0.0), 1) for m in pts]

    latest = pts[-1] if pts else None
    return templates.TemplateResponse(
        "player_dashboard.html",
        {
            "request": request,
            "player": player,
            "labels": labels,
            "ev_vals": ev_vals,
            "latest": latest,
        },
    )

# --------------------------------------------------------------------------------------
# Simple utilities for creating data (optional quick helpers)
# --------------------------------------------------------------------------------------
@app.post("/instructor/create-player")
def create_player(
    request: Request,
    first_name: str = Form(...),
    last_name: str = Form(...),
    age_group: str = Form(...),
    email: Optional[str] = Form(None),
    phone: Optional[str] = Form(None),
    image: Optional[UploadFile] = File(None),
    db: Session = Depends(get_db),
):
    inst_id = request.session.get("instructor_id")
    if not inst_id:
        return RedirectResponse("/instructor", status_code=303)

    if age_group not in AGE_GROUPS:
        age_group = "13-15"

    # Save a bare-minimum file (to /static/uploads)
    image_url = None
    if image and image.filename:
        os.makedirs("static/uploads", exist_ok=True)
        dest = f"static/uploads/{int(datetime.utcnow().timestamp())}_{image.filename}"
        with open(dest, "wb") as f:
            f.write(image.file.read())
        image_url = "/" + dest

    code = str(int(datetime.utcnow().timestamp()))[-6:]  # quick unique code
    p = Player(
        first_name=first_name.strip(),
        last_name=last_name.strip(),
        age_group=age_group,
        email=email,
        phone=phone,
        login_code=code,
        image_url=image_url
    )
    db.add(p)
    db.commit()

    # show a green banner with their login code
    request.session["create_success"] = f"Player created. Login code: {code}"
    return RedirectResponse("/instructor/clients", status_code=303)
