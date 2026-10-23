"""Local application entrypoint; bind to loopback while using demonstration tokens."""

import asyncio
import base64
import binascii
import io
import json
import os
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import BackgroundTasks, Depends, FastAPI, File, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from PIL import Image
from pydantic import BaseModel, Field

from .database import Database, identifier
from .ingestion import index_upload, seed
from .models import DemoModels, RemoteModels
from .pipeline import ask, reply

DEMO_TOKENS = {
    "demo-acme-customer": {"company": "acme", "user": "demo-customer", "role": "customer"},
    "demo-acme-rep": {"company": "acme", "user": "demo-rep", "role": "rep"},
    "demo-northstar-customer": {"company": "northstar", "user": "demo-customer", "role": "customer"},
    "demo-northstar-rep": {"company": "northstar", "user": "demo-rep", "role": "rep"},
}


class Question(BaseModel):
    question: str = Field(min_length=2, max_length=4000)
    screenshot: str | None = Field(default=None, max_length=8_000_000)
    screenshot_caption: str = Field(default="", max_length=4000)


class Reply(BaseModel):
    text: str = Field(min_length=2, max_length=10000)
    save_knowledge: bool = False


class Thresholds(BaseModel):
    text_relevance: float = Field(ge=0, le=1)
    text_answerability: float = Field(ge=0, le=1)
    image_relevance: float = Field(ge=0, le=1)
    image_answerability: float = Field(ge=0, le=1)


def create_app(data_dir=None, models=None, auth_tokens=None):
    db = Database(data_dir or os.environ.get("SUPPORT_DATA_DIR", "data"))
    model_mode = os.environ.get("SUPPORT_MODEL_MODE", "demo")
    if model_mode not in ("demo", "remote"):
        raise ValueError("SUPPORT_MODEL_MODE must be demo or remote")
    models = models or (RemoteModels() if model_mode == "remote" else DemoModels())
    token_map = auth_tokens if auth_tokens is not None else json.loads(os.environ.get("SUPPORT_TOKENS_JSON", json.dumps(DEMO_TOKENS)))

    @asynccontextmanager
    async def lifespan(application):
        if models.mode == "demo":
            seed(db, models)
        yield

    app = FastAPI(title="Multimodal support desk", lifespan=lifespan)
    app.state.database = db
    app.state.models = models

    def identity(authorization: str | None = Header(default=None)):
        token = authorization.removeprefix("Bearer ") if authorization else ""
        value = token_map.get(token)
        if not value:
            raise HTTPException(401, "Valid bearer token required")
        return value

    def representative(person=Depends(identity)):
        if person["role"] not in ("rep", "admin"):
            raise HTTPException(403, "Representative access required")
        return person

    @app.get("/api/health")
    def health():
        return {"status": "ok", "mode": models.mode, "demo_tokens": token_map == DEMO_TOKENS}

    @app.get("/api/me")
    def me(person=Depends(identity)):
        return {**person, "mode": models.mode}

    @app.post("/api/chat")
    def chat(body: Question, person=Depends(identity)):
        if body.screenshot:
            try:
                raw = base64.b64decode(body.screenshot, validate=True)
                with Image.open(io.BytesIO(raw)) as image:
                    if image.width * image.height > 16_000_000:
                        raise ValueError("Image too large")
                    image.verify()
            except (ValueError, binascii.Error, OSError, Image.DecompressionBombError):
                raise HTTPException(422, "Screenshot must be a valid image smaller than 16 megapixels")
        return ask(db, models, person, body.question, body.screenshot, body.screenshot_caption)

    @app.get("/api/conversations/{conversation_id}")
    def conversation(conversation_id: str, person=Depends(identity)):
        row = db.one("SELECT * FROM conversations WHERE id=? AND company=? AND user_id=?",
                     (conversation_id, person["company"], person["user"]))
        if not row:
            raise HTTPException(404, "Conversation not found")
        row["citations"] = json.loads(row["citations"])
        return row

    @app.get("/api/tickets")
    def tickets(person=Depends(representative)):
        rows = db.rows("SELECT * FROM tickets WHERE company=? ORDER BY rowid DESC", (person["company"],))
        for row in rows:
            row["rejected"] = json.loads(row["rejected"])
        return rows

    @app.post("/api/tickets/{ticket_id}/reply")
    def answer_ticket(ticket_id: str, body: Reply, person=Depends(representative)):
        try:
            return reply(db, models, person["company"], ticket_id, body.text, body.save_knowledge)
        except LookupError as error:
            raise HTTPException(404, str(error))
        except ValueError as error:
            raise HTTPException(409, str(error))

    @app.get("/api/decisions")
    def decisions(person=Depends(representative)):
        rows = db.rows("SELECT * FROM decisions WHERE company=? ORDER BY rowid DESC LIMIT 100", (person["company"],))
        for row in rows:
            row["detail"] = json.loads(row["detail"])
        return rows

    @app.get("/api/thresholds")
    def get_thresholds(person=Depends(representative)):
        return db.thresholds(person["company"])

    @app.put("/api/thresholds")
    def put_thresholds(body: Thresholds, person=Depends(representative)):
        db.thresholds(person["company"])
        db.execute("UPDATE thresholds SET text_relevance=?,text_answerability=?,image_relevance=?,image_answerability=? WHERE company=?",
                   (*body.model_dump().values(), person["company"]))
        db.event(person["company"], "thresholds_changed", body.model_dump())
        return db.thresholds(person["company"])

    @app.get("/api/documents")
    def documents(person=Depends(representative)):
        return db.rows("""SELECT d.id,d.title,COUNT(c.id) AS chunks,MAX(c.page) AS pages FROM documents d
            LEFT JOIN chunks c ON c.document_id=d.id AND c.company=d.company WHERE d.company=? GROUP BY d.id ORDER BY d.rowid""", (person["company"],))

    @app.get("/api/pages/{chunk_id}")
    def page_image(chunk_id: str, person=Depends(identity)):
        row = db.one("SELECT image_path FROM chunks WHERE id=? AND company=? AND kind='image'", (chunk_id, person["company"]))
        if not row or not row["image_path"]:
            raise HTTPException(404, "Page image not found")
        return FileResponse(row["image_path"], media_type="image/png")

    @app.post("/api/documents", status_code=202)
    async def upload(background: BackgroundTasks, file: UploadFile = File(), person=Depends(representative)):
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in (".pdf", ".docx"):
            raise HTTPException(422, "Upload PDF or DOCX")
        raw = await file.read(20_000_001)
        if len(raw) > 20_000_000:
            raise HTTPException(413, "Upload limit is 20 MB")
        job = identifier()
        directory = db.root / person["company"] / "uploads"
        directory.mkdir(parents=True, exist_ok=True)
        source = directory / (job + suffix)
        source.write_bytes(raw)
        title = Path(file.filename or "Document").name[:160]
        db.execute("INSERT INTO jobs(id,company,title,status) VALUES(?,?,?,'queued')", (job, person["company"], title))
        background.add_task(index_upload, db, models, person["company"], job, source, title)
        return {"job_id": job, "status": "queued"}

    @app.get("/api/jobs")
    def jobs(person=Depends(representative)):
        return db.rows("SELECT * FROM jobs WHERE company=? ORDER BY rowid DESC", (person["company"],))

    @app.get("/api/events")
    async def events(request: Request, after: int = 0, person=Depends(representative)):
        async def stream():
            cursor = after
            while not await request.is_disconnected():
                for row in db.rows("SELECT * FROM events WHERE company=? AND seq>? ORDER BY seq LIMIT 100", (person["company"], cursor)):
                    cursor = row["seq"]
                    yield "id: " + str(cursor) + "\ndata: " + json.dumps(row) + "\n\n"
                yield ": keepalive\n\n"
                await asyncio.sleep(1)
        return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})

    web = Path(__file__).parent.parent / "web"
    app.mount("/static", StaticFiles(directory=web), name="static")

    @app.get("/")
    def index():
        return FileResponse(web / "index.html")

    return app
