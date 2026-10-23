import base64
import io
import json
import math
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi.testclient import TestClient
from PIL import Image

from support_desk.app import create_app
from support_desk.database import Database
from support_desk.ingestion import chunks
from support_desk.models import DemoModels, RemoteModels, service_url, valid_vector
from support_desk.pipeline import gate, reply


def headers(company="acme", role="customer"):
    return {"Authorization": f"Bearer demo-{company}-{role}"}


@pytest.fixture
def client(tmp_path):
    with TestClient(create_app(tmp_path)) as client:
        yield client


def ask(client, question, company="acme", **extra):
    response = client.post("/api/chat", json={"question": question, **extra}, headers=headers(company))
    assert response.status_code == 200
    return response.json()


def test_text_and_visual_page_answers_have_citations(client):
    text = ask(client, "How do I reset my password?")
    assert text["status"] == "answered"
    assert "Security" in text["answer"]
    assert text["citations"][0]["page"] == 1
    visual = ask(client, "How do I fix E-4012?")
    assert visual["status"] == "answered"
    assert "Automatic time" in visual["answer"]
    assert visual["citations"][0]["kind"] == "image"
    assert visual["citations"][0]["page"] == 3
    image_id = visual["citations"][0]["image_id"]
    assert client.get(f"/api/pages/{image_id}", headers=headers()).headers["content-type"] == "image/png"


def test_unknown_question_widens_once_then_escalates_without_generation(client):
    class GuardModels(DemoModels):
        def generate(self, *_):
            pytest.fail("Generation must not run without evidence")
    client.app.state.models.generate = GuardModels().generate
    result = ask(client, "Do you provide interplanetary teleportation insurance?")
    assert result["status"] == "escalated"
    decisions = client.get("/api/decisions", headers=headers(role="rep")).json()
    assert sorted(d["attempt"] for d in decisions) == [1, 2]
    assert {d["outcome"] for d in decisions} == {"widen", "ticket"}
    assert {d["detail"]["limit_per_modality"] for d in decisions} == {2, 8}


def test_tenant_isolation_and_customer_ownership(client):
    result = ask(client, "How do I fix E-4012?", "northstar")
    assert result["status"] == "escalated"
    assert not any(c["title"].startswith("Acme") for c in result["candidates"])
    ticket = result["ticket_id"]
    assert client.post(f"/api/tickets/{ticket}/reply", headers=headers(role="rep"), json={"text":"wrong company"}).status_code == 404
    assert client.get("/api/tickets", headers=headers()).status_code == 403
    assert client.get("/api/tickets").status_code == 401
    own = ask(client, "Reset password")
    assert client.get(f"/api/conversations/{own['conversation_id']}", headers=headers("northstar")).status_code == 404
    assert client.get(f"/api/conversations/{own['conversation_id']}", headers=headers(role="rep")).status_code == 404
    image = own["citations"][0]["image_id"]
    assert client.get(f"/api/pages/{image}", headers=headers("northstar")).status_code == 404
    assert client.get("/api/documents",headers=headers()).status_code==403


def test_rep_reply_becomes_retrievable_knowledge_and_updates_chat(client):
    question = "Can I request an extended warranty?"
    result = ask(client, question)
    assert result["status"] == "escalated"
    response = client.post(f"/api/tickets/{result['ticket_id']}/reply", headers=headers(role="rep"),
                           json={"text":"Extended warranty is available for 12 extra months through your account portal.","save_knowledge":True})
    assert response.json()["knowledge_saved"]
    assert response.json()["delivery"] == "in-app conversation"
    conversation = client.get(f"/api/conversations/{result['conversation_id']}",headers=headers()).json()
    assert conversation["status"] == "resolved"
    assert "12 extra months" in conversation["answer"]
    again = ask(client, question)
    assert again["status"] == "answered"
    assert "12 extra months" in again["answer"]
    assert ask(client, question, "northstar")["status"] == "escalated"


def test_four_thresholds_are_independent_and_persistent(client):
    thresholds = {"text_relevance":.4,"text_answerability":.7,"image_relevance":.6,"image_answerability":.9}
    candidates = [{"id":"t","kind":"text"},{"id":"i","kind":"image"}]
    scores=[{"id":"t","relevance":.5,"answerability":.8},{"id":"i","relevance":.8,"answerability":.8}]
    result={c["id"]:c["passed"] for c in gate(candidates,scores,thresholds)}
    assert result == {"t":True,"i":False}
    assert client.put('/api/thresholds',json=thresholds,headers=headers(role="rep")).status_code==200
    assert client.get('/api/thresholds',headers=headers(role="rep")).json()["image_answerability"]==.9
    assert client.get('/api/thresholds',headers=headers("northstar","rep")).json()["image_answerability"]==.6
    thresholds["image_answerability"]=1.1
    assert client.put('/api/thresholds',json=thresholds,headers=headers(role="rep")).status_code==422


def test_pdf_ingestion_renders_pages_and_retrieves_content(client):
    import pymupdf
    pdf=pymupdf.open()
    page=pdf.new_page()
    page.insert_text((72,72),"The calibration interval is seventeen days. Recalibrate sensors every seventeen days.")
    content=pdf.tobytes()
    response=client.post('/api/documents',headers=headers(role="rep"),files={"file":("calibration.pdf",content,"application/pdf")})
    assert response.status_code==202
    jobs=client.get('/api/jobs',headers=headers(role="rep")).json()
    assert jobs[0]["status"]=="complete"
    assert jobs[0]["completed"]==1
    result=ask(client,"What is the calibration interval?")
    assert result["status"]=="answered"
    assert "seventeen" in result["answer"]
    assert result["citations"][0]["title"]=="calibration.pdf"


def test_failed_ingestion_is_visible_and_not_retrievable(client):
    response=client.post('/api/documents',headers=headers(role="rep"),files={"file":("broken.pdf",b"broken pdf","application/pdf")})
    assert response.status_code==202
    jobs=client.get('/api/jobs',headers=headers(role="rep")).json()
    assert jobs[0]["status"]=="failed"
    assert jobs[0]["error"]
    assert client.post('/api/documents',headers=headers(),files={"file":("a.pdf",b"a","application/pdf")}).status_code==403
    assert client.post('/api/documents',headers=headers(role="rep"),files={"file":("a.exe",b"a","application/octet-stream")}).status_code==422


def test_screenshot_input_and_explicit_demo_caption(client):
    image=Image.new('RGB',(30,30),'white'); output=io.BytesIO();image.save(output,format='PNG')
    result=ask(client,"Can you help?",screenshot=base64.b64encode(output.getvalue()).decode(),screenshot_caption="E-4012")
    assert result["status"]=="answered"
    assert client.post('/api/chat',headers=headers(),json={"question":"Help me","screenshot":"not base64"}).status_code==422


def test_model_failure_escalates_and_logs_sanitized_type(client):
    def fail(*_,**__):
        raise RuntimeError("sensitive-secret")
    client.app.state.models.embed=fail
    result=ask(client,"Reset password")
    assert result["status"]=="escalated"
    assert result["reason"]=="model_error"
    decisions=client.get('/api/decisions',headers=headers(role="rep")).json()
    assert decisions[0]["detail"]=={"error_type":"RuntimeError"}
    assert 'sensitive-secret' not in json.dumps(decisions)


def test_duplicate_ticket_resolution_is_atomic(client):
    result=ask(client,"Can I request an extended warranty?")
    db=client.app.state.database
    def resolve(_):
        try:
            return reply(db,DemoModels(),'acme',result['ticket_id'],'Extended warranty lasts twelve months.',True)["status"]
        except ValueError:
            return "already resolved"
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes=list(pool.map(resolve,range(2)))
    assert sorted(outcomes)==["already resolved","resolved"]
    assert len(db.rows("SELECT * FROM documents WHERE title='Reviewed support answer' AND company='acme'"))==1


def test_remote_contract_rejects_corrupt_vectors_scores_and_untrusted_urls():
    for value in ([True]*768,[float('nan')]*768,[0]*768,[1]*767,None,[10**1000]*768):
        with pytest.raises(ValueError):valid_vector(value)
    extreme = valid_vector([1e308] * 768)
    assert math.isclose(sum(x*x for x in extreme),1.0,abs_tol=1e-12)
    for url in ('https://user:secret@example.com','http://example.com','https://example.com/?key=secret'):
        with pytest.raises(ValueError):service_url(url)
    def response(request):
        return httpx.Response(200,json={"scores":[{"id":"a","relevance":True,"answerability":.9}]})
    remote=RemoteModels('https://example.com','not-a-real-key',httpx.Client(transport=httpx.MockTransport(response)))
    with pytest.raises(ValueError): remote.rerank('question',[{"id":"a","text":"passage"}])


def test_insforge_gateway_uses_raw_text_and_checks_citations(monkeypatch):
    for key,value in {'INSFORGE_BASE_URL':'https://example.com','INSFORGE_ACCESS_TOKEN':'not-a-real-token','GENERATION_MODEL':'configured-vision-model'}.items():monkeypatch.setenv(key,value)
    def response(request):
        assert request.url.path=='/api/ai/chat/completion'
        assert request.headers['authorization']=='Bearer not-a-real-token'
        return httpx.Response(200,json={'text':json.dumps({'answer':'An evidence-backed answer','citations':['a']})})
    remote=RemoteModels('https://example.com','not-a-real-key',httpx.Client(transport=httpx.MockTransport(response)))
    assert remote.generate('question',[{'id':'a','title':'manual','page':3,'text':'evidence'}])['citations']==['a']


def test_chunk_boundaries_preserve_all_words():
    words=' '.join(str(i) for i in range(441))
    result=chunks(words)
    assert [len(c.split()) for c in result]==[220,220,1]
    assert ' '.join(result)==words
