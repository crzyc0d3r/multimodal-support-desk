"""Two-modality retrieval, page merging, decision gate, one retry, and human review."""

import json

from .database import identifier


def similarity(a, b):
    return sum(x * y for x, y in zip(a, b, strict=True))


def retrieve(db, company, query, limit):
    rows = db.rows("""SELECT c.*,d.title FROM chunks c JOIN documents d ON d.id=c.document_id AND d.company=c.company
        WHERE c.company=? AND NOT EXISTS (
          SELECT 1 FROM jobs j WHERE j.document_id=c.document_id AND j.status!='complete')""", (company,))
    for row in rows:
        row["similarity"] = similarity(query, json.loads(row.pop("embedding")))
    selected = []
    for kind in ("text", "image"):
        selected.extend(sorted((r for r in rows if r["kind"] == kind), key=lambda r: r["similarity"], reverse=True)[:limit])
    # Keep the strongest text chunk and image per page, then pair their evidence.
    by_page = {}
    for candidate in selected:
        key = (candidate["document_id"], candidate["page"])
        group = by_page.setdefault(key, {})
        if candidate["kind"] not in group:
            group[candidate["kind"]] = candidate
    merged = []
    for group in by_page.values():
        image = group.get("image")
        for candidate in group.values():
            if image:
                candidate["image_path"] = image["image_path"]
                candidate["image_id"] = image["id"]
            merged.append(candidate)
    return merged


def gate(candidates, scores, thresholds):
    lookup = {score["id"]: score for score in scores}
    result = []
    for candidate in candidates:
        score = lookup[candidate["id"]]
        kind = candidate["kind"]
        result.append({**candidate, **score, "passed": (
            score["relevance"] >= thresholds[kind + "_relevance"] and
            score["answerability"] >= thresholds[kind + "_answerability"])})
    return sorted(result, key=lambda c: c["answerability"], reverse=True)


def public_candidate(c):
    return {k: v for k, v in c.items() if k != "image_path"}


def ask(db, models, identity, question, screenshot=None, screenshot_caption=""):
    company, user = identity["company"], identity["user"]
    conversation = identifier()
    db.execute("INSERT INTO conversations(id,company,user_id,question,status) VALUES(?,?,?,?,?)",
               (conversation, company, user, question, "processing"))
    rejected = []
    outcome = "insufficient_evidence"
    try:
        # Avoid temporary screenshot files; remote embedding accepts base64 directly.
        if screenshot and models.mode == "remote":
            from .models import valid_vector
            query = valid_vector(models.post("/embed", {"text": question, "image": screenshot})["embedding"])
        else:
            query = models.embed(question, image_caption=screenshot_caption)
        thresholds = db.thresholds(company)
        for attempt, limit in enumerate((2, 8), 1):
            candidates = retrieve(db, company, query, limit)
            scores = models.rerank(question, candidates, screenshot, screenshot_caption)
            ranked = gate(candidates, scores, thresholds)
            passed = [c for c in ranked if c["passed"]]
            rejected = [public_candidate(c) for c in ranked]
            db.execute("INSERT INTO decisions(id,company,conversation_id,attempt,outcome,detail) VALUES(?,?,?,?,?,?)",
                       (identifier(), company, conversation, attempt, "answer" if passed else "widen" if attempt == 1 else "ticket",
                        json.dumps({"limit_per_modality": limit, "thresholds": thresholds, "candidates": rejected})))
            if passed:
                result = models.generate(question, passed)
                by_id = {c["id"]: c for c in passed}
                citations = [{"id": c["id"], "document_id": c["document_id"], "title": c["title"], "page": c["page"],
                              "kind": c["kind"], "image_id": c.get("image_id")} for c in (by_id[i] for i in result["citations"])]
                db.execute("UPDATE conversations SET answer=?,citations=?,status='answered' WHERE id=? AND company=?",
                           (result["answer"], json.dumps(citations), conversation, company))
                db.event(company, "answered", {"conversation": conversation})
                return {"conversation_id": conversation, "status": "answered", "answer": result["answer"],
                        "citations": citations, "attempts": attempt, "candidates": rejected}
    except Exception as error:
        outcome = "model_error"
        db.execute("INSERT INTO decisions(id,company,conversation_id,attempt,outcome,detail) VALUES(?,?,?,?,?,?)",
                   (identifier(), company, conversation, 0, outcome, json.dumps({"error_type": type(error).__name__})))
    ticket = identifier()
    db.execute("INSERT INTO tickets(id,company,conversation_id,question,rejected,status) VALUES(?,?,?,?,?,?)",
               (ticket, company, conversation, question, json.dumps(rejected), "open"))
    db.execute("UPDATE conversations SET status='escalated' WHERE id=? AND company=?", (conversation, company))
    db.event(company, "ticket_opened", {"ticket": ticket, "conversation": conversation})
    return {"conversation_id": conversation, "ticket_id": ticket, "status": "escalated", "reason": outcome,
            "answer": "A support representative will review your question here.", "citations": [], "candidates": rejected}


def reply(db, models, company, ticket_id, text, save):
    ticket = db.one("SELECT * FROM tickets WHERE id=? AND company=?", (ticket_id, company))
    if not ticket:
        raise LookupError("Ticket not found")
    if ticket["status"] == "resolved":
        raise ValueError("Ticket already resolved")
    document = None
    records = []
    if save:
        document = identifier()
        knowledge = "Question: " + ticket["question"] + "\nAnswer: " + text
        records = [(identifier(), company, document, 1, "text", knowledge, None, json.dumps(models.embed(knowledge)))]
    # Ticket resolution and knowledge insertion form one transaction.
    with db.connect() as connection:
        connection.execute("BEGIN IMMEDIATE")
        current = connection.execute("SELECT status FROM tickets WHERE id=? AND company=?", (ticket_id, company)).fetchone()
        if current["status"] == "resolved":
            raise ValueError("Ticket already resolved")
        if document:
            connection.execute("INSERT INTO documents(id,company,title) VALUES(?,?,?)", (document, company, "Reviewed support answer"))
            connection.executemany("INSERT INTO chunks VALUES(?,?,?,?,?,?,?,?)", records)
        connection.execute("UPDATE tickets SET reply=?,status='resolved',saved_document=? WHERE id=? AND company=?", (text, document, ticket_id, company))
        connection.execute("UPDATE conversations SET answer=?,status='resolved' WHERE id=? AND company=?", (text, ticket["conversation_id"], company))
    db.event(company, "ticket_resolved", {"ticket": ticket_id, "knowledge_saved": bool(document)})
    return {"status": "resolved", "knowledge_saved": bool(document), "delivery": "in-app conversation"}
