import csv
import re
from dataclasses import dataclass, field
from functools import lru_cache
from html import unescape

from .settings import settings


CHOICE_SPLIT_RE = re.compile(r"\s*\|\s*")
CHOICE_RE = re.compile(r"\s*([^,]+),\s*(.*)")
FIELD_RE = re.compile(r"\[([A-Za-z0-9_]+)(?:\(([^)]+)\))?\]")
SIMPLE_BRANCH_RE = re.compile(
    r"^\s*\[([A-Za-z0-9_]+)(?:\(([^)]+)\))?\]\s*(=|<>|!=|<=|>=|<|>)\s*['\"]([^'\"]*)['\"]\s*$"
)


@dataclass
class Choice:
    code: str
    label: str


@dataclass
class SurveyField:
    name: str
    form: str
    section: str
    field_type: str
    label: str
    choices: list[Choice] = field(default_factory=list)
    validation: str = ""
    validation_min: str = ""
    validation_max: str = ""
    required: bool = False
    branching_logic: str = ""
    annotation: str = ""
    matrix_group: str = ""

    @property
    def is_hidden(self) -> bool:
        return "@HIDDEN" in self.annotation or "@HIDDEN-SURVEY" in self.annotation

    @property
    def default(self) -> str | None:
        match = re.search(r"@DEFAULT=['\"]?([^'\"]+)['\"]?", self.annotation)
        return match.group(1) if match else None


@dataclass
class SurveySchema:
    fields: list[SurveyField]
    by_name: dict[str, SurveyField]
    survey_fields: list[SurveyField]
    export_columns: list[str]
    translations: dict[tuple[str, str, str], str]


def parse_choices(raw: str) -> list[Choice]:
    if not raw:
        return []
    choices: list[Choice] = []
    for part in CHOICE_SPLIT_RE.split(raw):
        match = CHOICE_RE.match(part)
        if match:
            choices.append(Choice(match.group(1).strip(), match.group(2).strip()))
    return choices


def clean_label(value: str) -> str:
    value = unescape(value or "")
    value = re.sub(r"<br\s*/?>", " ", value, flags=re.I)
    value = re.sub(r"</p>\s*<p[^>]*>", " ", value, flags=re.I)
    value = re.sub(r"<[^>]+>", "", value)
    value = value.replace("\xa0", " ")
    return re.sub(r"\s+", " ", value).strip()


def load_translations() -> dict[tuple[str, str, str], str]:
    translations: dict[tuple[str, str, str], str] = {}
    with settings.spanish_translation.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    header_index = next(
        i for i, row in enumerate(rows) if row[:6] == ["section", "type", "name", "index", "kind", "text"]
    )
    for row in rows[header_index + 1 :]:
        if len(row) < 6:
            continue
        section, typ, name, index, kind, text = row[:6]
        if kind not in ("translation", "value") or not text:
            continue
        if section == "fieldTranslations" and typ in ("label", "enum"):
            translations[(typ, name, index)] = clean_label(text)
        elif section == "matrixTranslations" and typ == "enum":
            translations[(typ, name, index)] = clean_label(text)
        elif section == "surveyTranslations" and typ == "survey-title":
            translations[(typ, name, index)] = clean_label(text)
    return translations


@lru_cache(maxsize=1)
def load_schema() -> SurveySchema:
    with settings.data_dictionary.open(newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        fields = [
            SurveyField(
                name=row["Variable / Field Name"],
                form=row["Form Name"],
                section=clean_label(row["Section Header"]),
                field_type=row["Field Type"],
                label=clean_label(row["Field Label"]),
                choices=parse_choices(row["Choices, Calculations, OR Slider Labels"]),
                validation=row["Text Validation Type OR Show Slider Number"],
                validation_min=row["Text Validation Min"],
                validation_max=row["Text Validation Max"],
                required=row["Required Field?"] == "y",
                branching_logic=row["Branching Logic (Show field only if...)"],
                annotation=row["Field Annotation"],
                matrix_group=row["Matrix Group Name"],
            )
            for row in reader
        ]
    by_name = {field.name: field for field in fields}
    translations = load_translations()
    export_columns: list[str] = []
    for field in fields:
        if field.field_type == "checkbox":
            export_columns.extend(f"{field.name}___{choice.code}" for choice in field.choices)
        else:
            export_columns.append(field.name)
    return SurveySchema(
        fields=fields,
        by_name=by_name,
        survey_fields=[f for f in fields if f.form == "research_participant_perception_survey_epv_version"],
        export_columns=export_columns,
        translations=translations,
    )


def localized_label(field: SurveyField, language: str) -> str:
    if language != "es":
        return field.label
    return load_schema().translations.get(("label", field.name, ""), field.label)


def localized_choices(field: SurveyField, language: str) -> list[Choice]:
    if language != "es":
        return field.choices
    translations = load_schema().translations
    translated: list[Choice] = []
    for choice in field.choices:
        label = translations.get(("enum", field.name, choice.code), choice.label)
        if field.matrix_group:
            label = translations.get(("enum", field.matrix_group, choice.code), label)
        translated.append(Choice(choice.code, label))
    return translated


def get_field_value(values: dict, name: str, checkbox_code: str | None = None):
    value = values.get(name)
    if checkbox_code is None:
        return value
    return "1" if checkbox_code in (value or []) else "0"


def evaluate_branching(logic: str, values: dict) -> bool:
    if not logic:
        return True
    expression = logic

    def repl_field(match: re.Match) -> str:
        name, checkbox_code = match.group(1), match.group(2)
        return repr(str(get_field_value(values, name, checkbox_code) or ""))

    expression = FIELD_RE.sub(repl_field, expression)
    expression = expression.replace("<>", "!=")
    expression = re.sub(r"(?<![!<>=])=(?!=)", "==", expression)
    if not re.fullmatch(r"[\s'\"A-Za-z0-9_!=<>()&|.-]+", expression):
        return False
    try:
        return bool(eval(expression, {"__builtins__": {}}, {}))
    except Exception:
        return False


def simple_branch_rule(logic: str) -> dict[str, str] | None:
    """Return a browser-safe representation of the dictionary's simple conditions."""
    if not logic:
        return None
    match = SIMPLE_BRANCH_RE.fullmatch(logic)
    if not match:
        return None
    field_name, checkbox_code, operator, expected = match.groups()
    return {
        "field": field_name,
        "checkbox": checkbox_code or "",
        "operator": "!=" if operator == "<>" else operator,
        "value": expected,
    }


def visible_survey_fields(values: dict, language: str) -> list[SurveyField]:
    visible: list[SurveyField] = []
    for field in load_schema().survey_fields:
        if field.is_hidden:
            continue
        if field.field_type == "calc":
            continue
        if evaluate_branching(field.branching_logic, values):
            visible.append(field)
    return visible


def defaults() -> dict:
    data = {}
    for field in load_schema().fields:
        if field.default is not None:
            data[field.name] = field.default
    data.setdefault("dd_version", "1.5.1")
    return data
