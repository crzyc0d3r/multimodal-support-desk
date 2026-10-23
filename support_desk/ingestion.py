"""Full PDF pages plus word chunks; DOCX uses LibreOffice to preserve pagination."""

import json
import shutil
import subprocess
import tempfile
import textwrap
from pathlib import Path

from PIL import Image, ImageDraw

from .database import identifier


def chunks(text, size=220):
    words = text.split()
    return [" ".join(words[i:i + size]) for i in range(0, len(words), size)]


def thumbnail(path, title, text, page):
    image = Image.new("RGB", (760, 980), "#f8faff")
    draw = ImageDraw.Draw(image)
    draw.rectangle((0, 0, 760, 90), fill="#26377f")
    draw.text((35, 35), title, fill="white", font_size=24)
    y = 130
    for line in textwrap.wrap(text, 58):
        draw.text((35, y), line, fill="#172240", font_size=20)
        y += 32
    draw.text((35, 920), f"Page {page} / synthetic support manual", fill="#52648c", font_size=17)
    image.save(path)


def add_page(db, models, company, document, page, text, image_path, caption=""):
    records = []
    for passage in chunks(text):
        records.append((identifier(), company, document, page, "text", passage, None,
                        json.dumps(models.embed(passage))))
    # Remote mode embeds the actual image. Demo mode embeds only supplied caption metadata.
    records.append((identifier(), company, document, page, "image", caption or text,
                    str(image_path), json.dumps(models.embed("", str(image_path), caption or text))))
    with db.connect() as connection:
        connection.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)", records)


def seed(db, models):
    if db.one("SELECT id FROM documents LIMIT 1"):
        return
    for company, title, pages in [
        ("acme", "Acme device handbook", [
            ("To reset your password, open Settings, select Security, then Reset password. A reset link is displayed in your account portal.", ""),
            ("Returns are accepted within 30 days of purchase. Open Orders and select Request return. Keep your order number handy.", ""),
            ("", "Error E-4012 means the device clock is out of sync. Open Device settings, enable Automatic time, then restart the device. The diagram shows Settings > Time > Automatic time.")]),
        ("northstar", "Northstar service guide", [
            ("Northstar warranty coverage lasts 18 months from activation. Contact your account representative to request a warranty assessment.", ""),
            ("", "Northstar error N-200 means the cable is unplugged. Reconnect the blue cable to port B and restart the hub.")])]:
        document = identifier()
        db.execute("INSERT INTO documents(id,company,title) VALUES(?,?,?)", (document, company, title))
        directory = db.root / company / document
        directory.mkdir(parents=True, exist_ok=True)
        for page, (text, caption) in enumerate(pages, 1):
            path = directory / f"page-{page}.png"
            thumbnail(path, title, text or caption, page)
            add_page(db, models, company, document, page, text, path, caption)


def index_upload(db, models, company, job_id, source, title):
    document = identifier()
    try:
        import pymupdf

        file_path = Path(source)
        # Conversion must succeed before the job creates an indexed document.
        with tempfile.TemporaryDirectory(prefix="support-convert-", dir=db.root) as conversion:
            if file_path.suffix.lower() == ".docx":
                binary = shutil.which("soffice") or shutil.which("libreoffice")
                if not binary:
                    raise ValueError("DOCX requires LibreOffice (soffice) on PATH; export to PDF and upload that instead")
                subprocess.run([binary, "-env:UserInstallation=file:///" + Path(conversion).as_posix().lstrip("/"),
                                "--headless", "--convert-to", "pdf", "--outdir", conversion, str(file_path)],
                               check=True, capture_output=True, timeout=90)
                file_path = Path(conversion) / (file_path.stem + ".pdf")
            with pymupdf.open(file_path) as pdf:
                if len(pdf) > 200:
                    raise ValueError("Limit uploads to 200 pages")
                db.execute("UPDATE jobs SET total=?,status='indexing',document_id=? WHERE id=? AND company=?",
                           (len(pdf), document, job_id, company))
                db.execute("INSERT INTO documents(id,company,title) VALUES(?,?,?)", (document, company, title))
                directory = db.root / company / document
                directory.mkdir(parents=True, exist_ok=True)
                for index, page in enumerate(pdf, 1):
                    image_path = directory / f"page-{index}.png"
                    page.get_pixmap(matrix=pymupdf.Matrix(1.1, 1.1), alpha=False).save(image_path)
                    text = page.get_text()
                    add_page(db, models, company, document, index, text, image_path)
                    db.execute("UPDATE jobs SET completed=? WHERE id=? AND company=?", (index, job_id, company))
                    db.event(company, "index_progress", {"job": job_id, "completed": index, "total": len(pdf)})
        db.execute("UPDATE jobs SET status='complete' WHERE id=? AND company=?", (job_id, company))
        db.event(company, "index_complete", {"job": job_id})
    except Exception as error:
        # Partial pages stay private and excluded from retrieval until the job completes.
        message = str(error) if isinstance(error, ValueError) else type(error).__name__ + ": indexing failed; check local service logs"
        db.execute("UPDATE jobs SET status='failed',error=? WHERE id=? AND company=?", (message, job_id, company))
        db.event(company, "index_failed", {"job": job_id, "error": message})
