import pytest
from starlette.testclient import TestClient

from app import config
from app.agent import Agent
from app.main import create_app
from app.memory import Extraction
from app.session import SessionRegistry
from tests.fakes import FakeLLM

NADIA_FULL = Extraction(
    caller_role="policyholder", claimed_name="Nadia Okonkwo", policy_number="POL-3318",
    dob="1987-06-09", id_last4="2907", case_type="healthcare", status="denied", month=7,
    intent="denial_question",
)


@pytest.fixture
def client(store):
    llm = FakeLLM([NADIA_FULL, Extraction(wants_to_wrap_up=True)], replies=["Verified, and here is why...", "Shall I email a summary?"])
    app = create_app(agent=Agent(store, llm), registry=SessionRegistry(store))
    with TestClient(app) as c:
        yield c


def test_health(client):
    r = client.get("/health")
    assert r.status_code == 200
    assert r.json()["ok"] is True
    assert "extractor_model" in r.json()


def test_index_serves_html(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert config.BRAND_NAME in r.text


def test_session_create_and_scenario_validation(client):
    r = client.post("/api/session", params={"scenario": "timeout"})
    assert r.status_code == 200
    body = r.json()
    assert body["phase"] == "VERIFY_ID"
    from app import config
    assert body["greeting"].startswith(f"Hi, this is {config.AGENT_NAME}")
    assert client.post("/api/session", params={"scenario": "bogus"}).status_code == 422
    assert client.post("/api/session").status_code == 200  # default scenario


def test_message_flow_and_state(client):
    sid = client.post("/api/session").json()["session_id"]
    r = client.post(f"/api/session/{sid}/message", json={"text": "I'm Nadia Okonkwo, POL-3318..."})
    assert r.status_code == 200
    assert r.json()["reply"].startswith("Verified")
    assert r.json()["phase"] == "PROCESS_CASE"
    assert r.json()["turn"] == 1
    state = client.get(f"/api/session/{sid}/state").json()
    assert state["phase"] == "PROCESS_CASE"
    assert state["memory"]["verified_party_id"] == "PH-4021"
    assert state["memory"]["resolved_case_id"] == "CLM-7710"
    assert any(d["kind"] == "VERIFIED" for d in state["last_directives"])
    assert "Denial reason" in state["facts"]
    assert len(state["transcript"]) == 2
    r = client.post(f"/api/session/{sid}/message", json={"text": "that's all"})
    assert r.json()["phase"] == "POST_PROCESS"
    assert r.json()["sub_state"] == "awaiting_email_decision"


def test_unknown_session_and_bad_body(client):
    assert client.post("/api/session/nope/message", json={"text": "hi"}).status_code == 404
    assert client.get("/api/session/nope/state").status_code == 404
    sid = client.post("/api/session").json()["session_id"]
    assert client.post(f"/api/session/{sid}/message", json={"text": ""}).status_code == 422
    assert client.post(f"/api/session/{sid}/message", json={}).status_code == 422
