from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app
from app.schema import evaluate_branching, load_schema, simple_branch_rule
from app.settings import settings
from app.storage import (
    authenticate,
    connect,
    create_participant,
    dashboard_stats,
    get_admin_by_session,
    get_participant_by_token,
    get_participant_values,
    save_participant_response,
)


def test_automatic_record_id_does_not_collide_with_custom_ids():
    create_participant(record_id="2")
    participant = create_participant()
    assert participant["record_id"] == "3"


def test_create_participant_returns_inserted_row():
    participant = create_participant()
    assert participant is not None
    assert get_participant_by_token(participant["token"])["id"] == participant["id"]


def test_save_does_not_reopen_completed_response():
    participant = create_participant()
    save_participant_response(participant["token"], {"rpps_s_q1": "1"}, submitted=True)
    save_participant_response(participant["token"], {"rpps_s_q1": "2"}, submitted=False)
    saved = get_participant_by_token(participant["token"])
    assert saved["status"] == "complete"
    assert saved["submitted_at"]


def test_dashboard_top_box_and_data_element_stats():
    first = create_participant()
    second = create_participant()
    save_participant_response(
        first["token"],
        {"rpps_s_q1": "4", "rpps_s_q21": "1", "rpps_s_q68": "4", "rpps_s_q60": "2"},
    )
    save_participant_response(second["token"], {"rpps_s_q21": "4", "rpps_s_q68": "5"})

    stats = dashboard_stats()
    top_box = {item["field"]: item["cells"][0] for item in stats["analysis"]["rows"]}
    assert top_box["rpps_s_q1"]["score"] == 100
    assert top_box["rpps_s_q1"]["missing"] == 1
    assert top_box["rpps_s_q21"]["score"] == 50
    assert top_box["rpps_s_q68"]["score"] == 100
    assert top_box["rpps_s_q68"]["not_applicable"] == 1

    fields = {
        field["field"]: field
        for group in stats["data_elements"]
        for field in group["fields"]
    }
    assert fields["rpps_s_q60"]["distribution"][0]["label"] == "35-44"
    assert fields["rpps_s_q60"]["distribution"][0]["count"] == 1


def test_unknown_branching_syntax_fails_closed():
    assert evaluate_branching("contains([field], 'x')", {"field": "x"}) is False


def test_every_current_survey_branch_has_a_browser_safe_rule():
    branching_fields = [field for field in load_schema().survey_fields if field.branching_logic]
    assert branching_fields
    assert all(simple_branch_rule(field.branching_logic) for field in branching_fields)


def test_expired_admin_session_is_rejected(monkeypatch):
    token = authenticate("admin", "test-password")
    expired = (datetime.now(timezone.utc) - timedelta(seconds=settings.session_ttl_seconds + 1)).isoformat()
    with connect() as conn:
        conn.execute("UPDATE admin_sessions SET created_at = ? WHERE token = ?", (expired, token))
    assert get_admin_by_session(token) is None


def test_admin_participant_creation_requires_csrf():
    client = TestClient(app)
    token = authenticate("admin", "test-password")
    client.cookies.set(settings.session_cookie, token)
    response = client.post("/admin/participants", data={"language": "en", "csrf": "wrong"})
    assert response.status_code == 200
    assert "Invalid form token" in response.text


def test_admin_dashboard_renders_reference_metrics():
    client = TestClient(app)
    token = authenticate("admin", "test-password")
    client.cookies.set(settings.session_cookie, token)
    response = client.get("/admin")
    assert response.status_code == 200
    assert "Question type" in response.text
    assert "Load Table" in response.text
    assert "rpps_s_q57" in response.text
    assert "About the research study" in response.text


def test_dashboard_question_type_and_filter_cross_tab():
    first = create_participant()
    second = create_participant()
    save_participant_response(first["token"], {"rpps_s_q2": "1", "rpps_s_q60": "1"})
    save_participant_response(second["token"], {"rpps_s_q2": "2", "rpps_s_q60": "2"})

    stats = dashboard_stats("3", "rpps_s_q60")
    assert stats["analysis"]["title"] == "Reasons for joining a study"
    assert [column["label"] for column in stats["analysis"]["columns"]][:2] == ["18-34", "35-44"]
    q2 = next(row for row in stats["analysis"]["rows"] if row["field"] == "rpps_s_q2")
    assert q2["cells"][0]["score"] == 100
    assert q2["cells"][1]["score"] == 0


def test_dashboard_response_completion_rate():
    complete = create_participant()
    create_participant()
    save_participant_response(complete["token"], {}, submitted=True)
    stats = dashboard_stats("2", "nofilter")
    completion_rate = next(row for row in stats["analysis"]["rows"] if row["label"] == "Completion rate")
    assert completion_rate["cells"][0]["score"] == 50.0


def test_admin_login_requires_csrf():
    response = TestClient(app).post(
        "/admin/login",
        data={"username": "admin", "password": "test-password", "login_csrf": "wrong"},
    )
    assert response.status_code == 403


def test_survey_rejects_unknown_choice_code():
    participant = create_participant()
    client = TestClient(app)
    response = client.post(
        f"/s/{participant['token']}",
        data={"action": "submit", "rpps_s_q1": "not-a-real-code"},
    )
    assert response.status_code == 422
    assert "Invalid response option" in response.text
    values = get_participant_values(get_participant_by_token(participant["token"]))
    assert values.get("rpps_s_q1", "") == ""


def test_survey_renders_hidden_branches_for_live_updates():
    participant = create_participant()
    response = TestClient(app).get(f"/s/{participant['token']}")
    assert 'data-survey-field="rpps_s_q26"' in response.text
    assert 'data-branch-field="rpps_s_q25"' in response.text
    assert "updateBranchFields" in response.text


def test_newly_revealed_branch_is_saved_in_same_request():
    participant = create_participant()
    response = TestClient(app).post(
        f"/s/{participant['token']}",
        data={"action": "save", "rpps_s_q25": "1", "rpps_s_q26": "2"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    values = get_participant_values(get_participant_by_token(participant["token"]))
    assert values["rpps_s_q25"] == "1"
    assert values["rpps_s_q26"] == "2"


def test_spanish_survey_declares_language_and_translates_yes_no():
    participant = create_participant(language="es")
    response = TestClient(app).get(f"/s/{participant['token']}")
    assert '<html lang="es">' in response.text
    assert '"languages": "2"' in response.text
