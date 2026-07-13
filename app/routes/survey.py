from datetime import datetime

from fastapi import APIRouter, Form, Request

from ..schema import (
    evaluate_branching,
    load_schema,
    localized_choices,
    localized_label,
    simple_branch_rule,
    visible_survey_fields,
)
from ..storage import get_participant_by_token, get_participant_values, save_participant_response
from .deps import redirect, render


router = APIRouter()


def other_field_for(parent_name: str, choice_code: str, language: str):
    target = f"[{parent_name}({choice_code})]"
    for field in load_schema().survey_fields:
        if target in field.branching_logic and field.field_type in ("text", "notes"):
            return {
                "name": field.name,
                "label": localized_label(field, language),
                "field_type": field.field_type,
            }
    return None


def field_view(field, language: str, values: dict | None = None) -> dict:
    choices = []
    localized = localized_choices(field, language)
    for source_choice, choice in zip(field.choices, localized):
        choices.append(
            {
                "code": choice.code,
                "label": choice.label,
                "other_field": other_field_for(field.name, source_choice.code, language)
                if "other (please specify)" in source_choice.label.lower()
                else None,
            }
        )
    return {
        "name": field.name,
        "field_type": field.field_type,
        "label": localized_label(field, language),
        "choices": choices,
        "validation": field.validation,
        "required": field.required,
        "section": field.section,
        "branch": simple_branch_rule(field.branching_logic),
        "visible": evaluate_branching(field.branching_logic, values or {}),
    }


def survey_context(request: Request, participant, values: dict, errors: dict | None = None) -> dict:
    language = participant["language"]
    survey_fields = [field for field in load_schema().survey_fields if not field.is_hidden and field.field_type != "calc"]
    inline_other_names = {
        other_field["name"]
        for field in survey_fields
        for choice in field_view(field, language)["choices"]
        if (other_field := choice.get("other_field"))
    }
    return {
        "title": "Research Participant Perception Survey",
        "participant": participant,
        "fields": [field_view(field, language, values) for field in survey_fields if field.name not in inline_other_names],
        "values": values,
        "language": language,
        "errors": errors or {},
    }


def validate_value(field, value) -> str | None:
    if field.field_type in ("radio", "dropdown", "checkbox"):
        allowed = {choice.code for choice in field.choices}
        submitted = value if isinstance(value, list) else ([value] if value else [])
        if any(item not in allowed for item in submitted):
            return "Invalid response option."
    if not value or field.validation != "datetime_seconds_ymd":
        return None
    try:
        datetime.strptime(str(value), "%Y-%m-%d %H:%M:%S")
    except ValueError:
        return "Use YYYY-MM-DD HH:MM:SS format."
    return None


@router.get("/s/{token}")
def survey_form(request: Request, token: str):
    participant = get_participant_by_token(token)
    if not participant:
        return render(request, "survey_missing.html", {"title": "Survey link not found"})
    return render(request, "survey_form.html", survey_context(request, participant, get_participant_values(participant)))


@router.post("/s/{token}")
async def survey_save(request: Request, token: str):
    participant = get_participant_by_token(token)
    if not participant:
        return render(request, "survey_missing.html", {"title": "Survey link not found"})
    form = await request.form()
    action = form.get("action", "save")
    schema = load_schema()
    values = get_participant_values(participant)
    values["record_id"] = participant["record_id"]
    values["languages"] = "2" if participant["language"] == "es" else "1"
    values["survey_datetime"] = values.get("survey_datetime") or datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    # Apply submitted parent values first so fields revealed in the browser are
    # treated as visible during this same request.
    for field in schema.survey_fields:
        if field.field_type == "checkbox":
            values[field.name] = list(form.getlist(field.name))
        elif field.name in form and field.field_type not in ("descriptive", "calc"):
            values[field.name] = str(form.get(field.name, "")).strip()

    visible_names = {field.name for field in visible_survey_fields(values, participant["language"])}
    errors: dict[str, str] = {}
    for field in schema.survey_fields:
        if field.name not in visible_names:
            continue
        if field.field_type == "checkbox":
            values[field.name] = list(form.getlist(field.name))
        elif field.field_type in ("descriptive", "calc"):
            continue
        else:
            values[field.name] = str(form.get(field.name, "")).strip()
        if error := validate_value(field, values.get(field.name)):
            errors[field.name] = error

    # Re-evaluate branching with the submitted parent values, then discard stale
    # answers for fields that are no longer visible.
    final_visible = {field.name for field in visible_survey_fields(values, participant["language"])}
    for field in schema.survey_fields:
        if field.name not in final_visible and field.field_type not in ("descriptive", "calc"):
            values[field.name] = [] if field.field_type == "checkbox" else ""

    if action == "submit":
        for field in schema.survey_fields:
            if field.name in final_visible and field.required and values.get(field.name) in (None, "", []):
                errors[field.name] = "This question is required."

    for field in schema.survey_fields:
        if field.field_type not in ("checkbox", "radio"):
            continue
        selected = values.get(field.name)
        selected_values = set(selected or []) if isinstance(selected, list) else {selected}
        for choice in field.choices:
            other_field = other_field_for(field.name, choice.code, participant["language"])
            if other_field and choice.code not in selected_values:
                values[other_field["name"]] = ""

    if errors:
        return render(
            request,
            "survey_form.html",
            survey_context(request, participant, values, errors),
            status_code=422,
        )

    submitted = action == "submit"
    save_participant_response(token, values, submitted=submitted)
    if submitted:
        return redirect(f"/s/{token}/thanks")
    return redirect(f"/s/{token}?saved=1")


@router.get("/s/{token}/thanks")
def survey_thanks(request: Request, token: str):
    participant = get_participant_by_token(token)
    if not participant:
        return render(request, "survey_missing.html", {"title": "Survey link not found"})
    return render(
        request,
        "survey_thanks.html",
        {"title": "Thank you", "participant": participant, "language": participant["language"]},
    )
