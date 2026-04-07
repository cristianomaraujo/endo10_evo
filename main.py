from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
import pandas as pd
import os
import json
import re
from io import BytesIO
from pathlib import Path
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
import unicodedata
import textwrap

app = FastAPI()

# =========================
# CONFIG
# =========================
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY")
if not OPENAI_API_KEY:
    raise RuntimeError("OPENAI_API_KEY was not found in environment variables.")

MODEL_EXTRACT = os.getenv("MODEL_EXTRACT", "gpt-4o-mini")
MODEL_TRANSLATE = os.getenv("MODEL_TRANSLATE", "gpt-4o-mini")
MODEL_EXPLAIN = os.getenv("MODEL_EXPLAIN", "gpt-4o")

client = OpenAI(api_key=OPENAI_API_KEY)

BASE_DIR = Path(__file__).resolve().parent
EXCEL_FILE = BASE_DIR / "planilha_endo10.xlsx"
SHEET_NAME = "En"

if not EXCEL_FILE.exists():
    raise RuntimeError(
        f"Spreadsheet not found: {EXCEL_FILE}. "
        "Make sure planilha_endo10.xlsx is inside the project and included in the deploy."
    )

# =========================
# CORS
# =========================
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# =========================
# STATIC
# =========================
STATIC_DIR = BASE_DIR / "static"
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# =========================
# HELPERS
# =========================
def normalize_text(text: str) -> str:
    if text is None:
        return ""
    text = str(text).strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"\s+", " ", text).strip()
    return text


def safe_json_loads(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def safe_chat_completion(messages, model, temperature=0, response_format=None):
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    return client.chat.completions.create(**kwargs)


def wrap_pdf_lines(text: str, width: int = 90):
    if not text:
        return [""]
    lines = []
    for paragraph in str(text).split("\n"):
        wrapped = textwrap.wrap(paragraph, width=width) or [""]
        lines.extend(wrapped)
    return lines


def detect_language(text: str) -> str:
    if not text or not str(text).strip():
        return "English"

    norm = normalize_text(text)
    pt_markers = [
        "oi", "olá", "ola", "não", "nao", "sem dor", "dor", "paciente",
        "sensivel", "sensível", "alterada", "normal", "radiografia",
        "percussão", "palpação", "fístula", "fistula"
    ]
    if any(marker in norm for marker in pt_markers):
        return "Portuguese"

    try:
        prompt = (
            "Detect the language of the following text. "
            "Return only the language name in English, such as English, Portuguese, Spanish, French, Italian, German, Chinese, Arabic.\n\n"
            f"Text: {text}"
        )
        response = safe_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=MODEL_TRANSLATE,
            temperature=0
        )
        language = (response.choices[0].message.content or "").strip()
        return language if language else "English"
    except Exception:
        return "English"


def translate_text(text: str, target_language: str) -> str:
    if not text:
        return text
    if normalize_text(target_language) == "english":
        return text
    try:
        prompt = (
            f"Translate the following text into {target_language}. "
            "Keep the meaning clear, professional, natural, and concise. "
            "Preserve line breaks and list structure. "
            "Do not add commentary.\n\n"
            f"{text}"
        )
        response = safe_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model=MODEL_TRANSLATE,
            temperature=0
        )
        translated = (response.choices[0].message.content or "").strip()
        return translated if translated else text
    except Exception:
        return text


def is_greeting(text: str) -> bool:
    norm = normalize_text(text)
    greetings = {
        "hi", "hello", "hey", "oi", "ola", "olá", "hola", "salut",
        "hallo", "ciao", "bom dia", "boa tarde", "boa noite",
        "good morning", "good afternoon", "good evening"
    }
    return norm in greetings


# =========================
# CANONICAL CLINICAL MODEL
# =========================
OPTION_CATALOG = {
    "pain_absent": {
        "label": "Absent",
        "label_pt": "Ausente",
        "description": "The patient does not report pain.",
        "description_pt": "O paciente não relata dor.",
        "aliases": [
            "absent", "no pain", "without pain", "pain absent",
            "ausente", "sem dor", "nao tem dor", "não tem dor",
            "ele esta sem dor", "ele está sem dor",
            "esta sem dor", "está sem dor",
            "paciente sem dor", "assintomatico", "assintomático",
            "sem queixa de dor"
        ],
        "spreadsheet_values": ["Absent"],
    },
    "pain_present": {
        "label": "Present",
        "label_pt": "Presente",
        "description": "The patient reports pain or some type of discomfort.",
        "description_pt": "O paciente relata dor ou algum tipo de desconforto.",
        "aliases": [
            "present", "pain", "with pain", "has pain", "pain present",
            "presente", "com dor", "tem dor", "dor presente",
            "ele esta com dor", "ele está com dor",
            "esta com dor", "está com dor",
            "paciente com dor", "dolorido", "dolorosa",
            "sente dor", "relata dor", "há dor"
        ],
        "spreadsheet_values": ["Present"],
    },
    "onset_na": {
        "label": "Not applicable",
        "label_pt": "Não se aplica",
        "description": "Use this when the patient does not report pain.",
        "description_pt": "Use esta opção quando o paciente não relata dor.",
        "aliases": ["not applicable", "n/a", "nao se aplica", "não se aplica"],
        "spreadsheet_values": ["Not applicable"],
    },
    "onset_spontaneous": {
        "label": "Spontaneous",
        "label_pt": "Espontânea",
        "description": "The pain starts spontaneously, without any provoking stimulus.",
        "description_pt": "A dor se inicia espontaneamente, sem estímulo desencadeante.",
        "aliases": [
            "spontaneous", "spontaneously", "espontanea", "espontânea",
            "espontaneo", "espontâneo", "inicia espontaneamente",
            "dor espontanea", "dor espontânea"
        ],
        "spreadsheet_values": ["Spontaneous", "Spontaneous\n"],
    },
    "onset_provoked": {
        "label": "Provoked",
        "label_pt": "Provocada",
        "description": "The pain starts after a stimulus, such as cold, heat, pressure, or sweets.",
        "description_pt": "A dor se inicia após um estímulo, como frio, calor, pressão ou doce.",
        "aliases": [
            "provoked", "provocada", "provocado", "apos estimulo",
            "após estímulo", "apos frio", "após frio", "apos calor",
            "após calor", "desencadeada", "induzida"
        ],
        "spreadsheet_values": ["Provoked"],
    },
    "pulp_altered": {
        "label": "Altered",
        "label_pt": "Alterada",
        "description": "There is an exaggerated or persistent painful response to vitality testing.",
        "description_pt": "Há uma resposta dolorosa exagerada ou persistente ao teste de vitalidade.",
        "aliases": [
            "altered", "alterada", "alterado", "resposta exacerbada",
            "resposta aumentada", "resposta persistente", "dor persistente",
            "hiperreativo", "hyperreactive", "lingering response"
        ],
        "spreadsheet_values": ["Alterad"],
    },
    "pulp_negative": {
        "label": "Negative",
        "label_pt": "Negativa",
        "description": "There is no response to vitality testing.",
        "description_pt": "Não há resposta ao teste de vitalidade.",
        "aliases": [
            "negative", "negativo", "sem resposta", "no response",
            "nao respondeu", "não respondeu", "teste negativo",
            "sem resposta ao teste", "does not respond", "non-responsive"
        ],
        "spreadsheet_values": ["Negative"],
    },
    "pulp_normal": {
        "label": "Normal",
        "label_pt": "Normal",
        "description": "There is a mild, transient response that disappears shortly after the stimulus is removed.",
        "description_pt": "Há uma resposta leve e transitória que desaparece logo após a remoção do estímulo.",
        "aliases": [
            "normal", "resposta normal", "transient response",
            "resposta transitória", "resposta leve"
        ],
        "spreadsheet_values": ["Normal"],
    },
    "percussion_na": {
        "label": "Not applicable",
        "label_pt": "Não se aplica",
        "description": "Use this when percussion testing is not applicable in the clinical situation.",
        "description_pt": "Use esta opção quando o teste de percussão não se aplica à situação clínica.",
        "aliases": ["not applicable", "n/a", "nao se aplica", "não se aplica"],
        "spreadsheet_values": ["Not applicable"],
    },
    "percussion_normal": {
        "label": "Normal",
        "label_pt": "Normal",
        "description": "There is no pain or sensitivity on percussion.",
        "description_pt": "Não há dor ou sensibilidade à percussão.",
        "aliases": [
            "normal", "sem dor", "no pain", "not sensitive",
            "sem sensibilidade", "indolor", "negative percussion"
        ],
        "spreadsheet_values": ["Normal"],
    },
    "percussion_sensitive": {
        "label": "Sensitive",
        "label_pt": "Sensível",
        "description": "There is pain or sensitivity on percussion.",
        "description_pt": "Há dor ou sensibilidade à percussão.",
        "aliases": [
            "sensitive", "sensivel", "sensível", "painful", "tender",
            "doloroso", "dor a percussao", "dor à percussão",
            "sensivel a percussao", "sensível à percussão"
        ],
        "spreadsheet_values": ["Sensitive"],
    },
    "palpation_edema": {
        "label": "Edema",
        "label_pt": "Edema",
        "description": "There is swelling of the adjacent tissues.",
        "description_pt": "Há aumento de volume dos tecidos adjacentes.",
        "aliases": [
            "edema", "swelling", "swollen", "inchaco", "inchaço",
            "tumefacao", "tumefação"
        ],
        "spreadsheet_values": ["Edema"],
    },
    "palpation_fistula": {
        "label": "Fistula",
        "label_pt": "Fístula",
        "description": "There is a sinus tract or a mucosal/cutaneous opening communicating with the root apex.",
        "description_pt": "Há fístula ou trajeto fistuloso comunicando-se com o ápice radicular.",
        "aliases": [
            "fistula", "fístula", "sinus tract", "trajeto fistuloso",
            "fistulous tract", "parulis", "parúlide"
        ],
        "spreadsheet_values": ["Fistula"],
    },
    "palpation_normal": {
        "label": "Normal",
        "label_pt": "Normal",
        "description": "There is no pain on palpation.",
        "description_pt": "Não há dor à palpação.",
        "aliases": [
            "normal", "sem dor", "no pain", "not sensitive",
            "indolor", "sem sensibilidade"
        ],
        "spreadsheet_values": ["Normal"],
    },
    "palpation_sensitive": {
        "label": "Sensitive",
        "label_pt": "Sensível",
        "description": "There is pain or sensitivity on palpation.",
        "description_pt": "Há dor ou sensibilidade à palpação.",
        "aliases": [
            "sensitive", "sensivel", "sensível", "painful", "tender",
            "doloroso", "dor a palpacao", "dor à palpação",
            "sensivel a palpacao", "sensível à palpação"
        ],
        "spreadsheet_values": ["Sensivel"],
    },
    "radiography_circumscribed_radiolucency": {
        "label": "Circumscribed radiolucency lesion",
        "label_pt": "Lesão radiolúcida circunscrita",
        "description": "There is a well-defined radiolucent lesion with relatively distinct borders.",
        "description_pt": "Há uma lesão radiolúcida bem definida com bordas relativamente distintas.",
        "aliases": [
            "circumscribed", "circunscrita",
            "circumscribed radiolucency", "circumscribed radiolucency lesion",
            "lesao radiolucida circunscrita", "lesão radiolúcida circunscrita",
            "radiolucidez circunscrita"
        ],
        "spreadsheet_values": ["Circumscribed radiolucency lesion"],
    },
    "radiography_diffuse_apical_radiolucency": {
        "label": "Diffuse apical radiolucency",
        "label_pt": "Radiolucidez apical difusa",
        "description": "There is a diffuse apical radiolucent image or poorly defined radiolucency.",
        "description_pt": "Há uma imagem radiolucente apical difusa ou radiolucidez mal definida.",
        "aliases": [
            "diffuse", "difusa", "diffuse apical radiolucency",
            "apical diffuse radiolucency", "radiolucidez apical difusa",
            "lesao radiolucida difusa", "lesão radiolúcida difusa"
        ],
        "spreadsheet_values": ["Diffuse apical radiolucency"],
    },
    "radiography_thickening_pdl": {
        "label": "Thickening of the periodontal ligament",
        "label_pt": "Espessamento do ligamento periodontal",
        "description": "There is widening or thickening of the periodontal ligament space.",
        "description_pt": "Há alargamento ou espessamento do espaço do ligamento periodontal.",
        "aliases": [
            "thickening", "widening", "espessamento", "alargamento",
            "thickening of the periodontal ligament",
            "periodontal ligament thickening",
            "espessamento do ligamento periodontal",
            "alargamento do ligamento periodontal",
            "espessamento do espaco periodontal",
            "espessamento do espaço periodontal"
        ],
        "spreadsheet_values": ["Thickening of the periodontal ligament"],
    },
    "radiography_normal": {
        "label": "Normal",
        "label_pt": "Normal",
        "description": "The lamina dura is intact and the periodontal ligament space is uniform.",
        "description_pt": "A lâmina dura está intacta e o espaço do ligamento periodontal é uniforme.",
        "aliases": ["normal", "sem alteracoes", "sem alterações", "aspecto normal"],
        "spreadsheet_values": ["Normal"],
    },
    "radiography_diffuse_radiopaque": {
        "label": "Diffuse radiopaque lesion",
        "label_pt": "Lesão radiopaca difusa",
        "description": "There is a diffuse radiopaque lesion with ill-defined borders and gradual transition to adjacent bone.",
        "description_pt": "Há uma lesão radiopaca difusa com bordas mal definidas e transição gradual para o osso adjacente.",
        "aliases": [
            "diffuse radiopaque", "radiopaca difusa", "radiopaque diffuse",
            "radiopaco difuso", "diffuse radiopaque lesion",
            "lesao radiopaca difusa", "lesão radiopaca difusa"
        ],
        "spreadsheet_values": ["Diffuse radiopaque lesion"],
    },
}

QUESTION_DEFS = [
    {
        "field": "PAIN",
        "question": "Is the patient in pain?",
        "question_pt": "O paciente está com dor?",
        "codes": ["pain_absent", "pain_present"]
    },
    {
        "field": "ONSET",
        "question": "How did the pain start?",
        "question_pt": "Como a dor começou?",
        "codes": ["onset_na", "onset_spontaneous", "onset_provoked"]
    },
    {
        "field": "PULP VITALITY",
        "question": "What was the response to the pulp vitality test?",
        "question_pt": "Qual foi a resposta ao teste de vitalidade pulpar?",
        "codes": ["pulp_altered", "pulp_negative", "pulp_normal"]
    },
    {
        "field": "PERCUSSION",
        "question": "What was the finding on percussion?",
        "question_pt": "Qual foi o achado no teste de percussão?",
        "codes": ["percussion_na", "percussion_normal", "percussion_sensitive"]
    },
    {
        "field": "PALPATION",
        "question": "What was the finding on palpation?",
        "question_pt": "Qual foi o achado à palpação?",
        "codes": ["palpation_edema", "palpation_fistula", "palpation_normal", "palpation_sensitive"]
    },
    {
        "field": "RADIOGRAPHY",
        "question": "What is the main radiographic finding?",
        "question_pt": "Qual é o principal achado radiográfico?",
        "codes": [
            "radiography_circumscribed_radiolucency",
            "radiography_diffuse_apical_radiolucency",
            "radiography_thickening_pdl",
            "radiography_normal",
            "radiography_diffuse_radiopaque"
        ]
    },
]

FIELD_TO_CODES = {q["field"]: q["codes"] for q in QUESTION_DEFS}
FIELD_ORDER = [q["field"] for q in QUESTION_DEFS]

# =========================
# LOAD DATA
# =========================
try:
    df = pd.read_excel(EXCEL_FILE, sheet_name=SHEET_NAME)
except Exception as e:
    raise RuntimeError(
        f"Error loading spreadsheet '{EXCEL_FILE}' / sheet '{SHEET_NAME}': {e}"
    )

df.columns = [str(col).strip() for col in df.columns]
for col in df.columns:
    if df[col].dtype == "object":
        df[col] = df[col].fillna("").astype(str).str.strip()

if "PULPT VITALITY" in df.columns and "PULP VITALITY" not in df.columns:
    df = df.rename(columns={"PULPT VITALITY": "PULP VITALITY"})

REQUIRED_SPREADSHEET_COLS = [
    "PAIN", "ONSET", "PULP VITALITY", "PERCUSSION", "PALPATION", "RADIOGRAPHY",
    "DIAGNOSIS (AAE NOMENCLATURE 2009/2013)",
    "DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)",
    "COMPLEMENTARY DIAGNOSIS"
]
for required_col in REQUIRED_SPREADSHEET_COLS:
    if required_col not in df.columns:
        raise RuntimeError(f"Required column '{required_col}' not found in spreadsheet.")


# =========================
# CANONICALIZATION
# =========================
def canonicalize_value(field: str, value: str):
    norm = normalize_text(value)
    if not norm:
        return None

    field_codes = FIELD_TO_CODES[field]

    for code in field_codes:
        meta = OPTION_CATALOG[code]
        terms = [meta["label"], meta.get("label_pt", "")] + meta.get("aliases", []) + meta.get("spreadsheet_values", [])
        for term in terms:
            if normalize_text(term) == norm:
                return code

    candidates = []
    for code in field_codes:
        meta = OPTION_CATALOG[code]
        terms = [meta["label"], meta.get("label_pt", "")] + meta.get("aliases", []) + meta.get("spreadsheet_values", [])
        for term in terms:
            nterm = normalize_text(term)
            if nterm:
                candidates.append((len(nterm), nterm, code))
    candidates.sort(reverse=True)

    for _, nterm, code in candidates:
        if nterm in norm:
            return code

    if field == "PAIN":
        if any(term in norm for term in [
            "sem dor", "nao tem dor", "não tem dor",
            "esta sem dor", "está sem dor",
            "ele esta sem dor", "ele está sem dor",
            "paciente sem dor", "assintomatico", "assintomático"
        ]):
            return "pain_absent"
        if any(term in norm for term in [
            "com dor", "tem dor", "dor presente",
            "esta com dor", "está com dor",
            "ele esta com dor", "ele está com dor",
            "paciente com dor", "relata dor"
        ]):
            return "pain_present"

    if field == "PULP VITALITY":
        if any(term in norm for term in [
            "sem resposta", "nao respondeu", "não respondeu",
            "teste negativo", "negativo", "negative"
        ]):
            return "pulp_negative"
        if any(term in norm for term in [
            "resposta persistente", "resposta exacerbada",
            "resposta aumentada", "dor persistente"
        ]):
            return "pulp_altered"

    if field == "PERCUSSION":
        if any(term in norm for term in [
            "dor a percussao", "dor à percussão",
            "sensivel a percussao", "sensível à percussão"
        ]):
            return "percussion_sensitive"

    if field == "PALPATION":
        if any(term in norm for term in [
            "dor a palpacao", "dor à palpação",
            "sensivel a palpacao", "sensível à palpação"
        ]):
            return "palpation_sensitive"

    if field == "RADIOGRAPHY":
        if norm == "circunscrita":
            return "radiography_circumscribed_radiolucency"
        if norm == "difusa":
            return "radiography_diffuse_apical_radiolucency"

    return None


def label_for_code(code: str, language: str = "English"):
    if not code:
        return ""
    meta = OPTION_CATALOG.get(code, {})
    if normalize_text(language) == "portuguese":
        return meta.get("label_pt", meta.get("label", code))
    return meta.get("label", code)


for field in FIELD_ORDER:
    df[f"__code_{field}"] = df[field].apply(lambda x: canonicalize_value(field, x))

unmapped_rows = df[[f"__code_{field}" for field in FIELD_ORDER]].isna().any(axis=1)
if unmapped_rows.any():
    bad_indices = df[unmapped_rows].index.tolist()
    raise RuntimeError(f"Some spreadsheet rows could not be canonicalized. Row indices: {bad_indices}")


# =========================
# SESSIONS
# =========================
sessions = {}


def empty_session():
    return {
        "language": None,
        "stage": "greeting",
        "current_question": 0,
        "answers": {},
        "diagnosis_result": {},
        "history": [],
        "last_bot_payload": None,
    }


def create_session_if_needed(session_id: str):
    if session_id not in sessions:
        sessions[session_id] = empty_session()


def cache_payload(session: dict, payload: dict):
    session["last_bot_payload"] = payload
    return payload


def get_question_by_index(index: int):
    if index < 0 or index >= len(QUESTION_DEFS):
        raise HTTPException(status_code=400, detail="Invalid question index.")
    return QUESTION_DEFS[index]


def apply_business_rules(session: dict):
    if session["answers"].get("PAIN") == "pain_absent":
        session["answers"]["ONSET"] = "onset_na"


def get_next_unanswered_index(session: dict):
    for idx, q in enumerate(QUESTION_DEFS):
        if q["field"] not in session["answers"]:
            return idx
    return len(QUESTION_DEFS)


def sync_current_question(session: dict):
    apply_business_rules(session)
    session["current_question"] = get_next_unanswered_index(session)
    if session["current_question"] >= len(QUESTION_DEFS):
        session["stage"] = "completed"


def build_question_text(index: int, language: str) -> str:
    q = get_question_by_index(index)
    is_pt = normalize_text(language) == "portuguese"
    question_line = q.get("question_pt", q["question"]) if is_pt else q["question"]

    base_text = f"{question_line}\n\n"
    for code in q["codes"]:
        meta = OPTION_CATALOG[code]
        label = meta.get("label_pt", meta["label"]) if is_pt else meta["label"]
        desc = meta.get("description_pt", meta["description"]) if is_pt else meta["description"]
        base_text += f"{label} - {desc}\n"
    return base_text.strip()


def build_intro(language: str) -> str:
    intro_text = """
Hello! I am Endo10 EVO, a virtual assistant developed to support diagnostic reasoning in Endodontics.

This system conducts a structured clinical screening based on signs, symptoms, and complementary examination findings. At the end of the process, a diagnostic suggestion will be presented according to the reference nomenclature adopted by the system.

You may answer briefly or in natural language. If your message contains more than one clinical finding, I will try to identify them automatically.
""".strip()
    return translate_text(intro_text, language)


def build_intro_and_first_question(language: str) -> str:
    return f"{build_intro(language)}\n\n{build_question_text(0, language)}"


def build_inconsistent_message(language: str) -> str:
    return translate_text(
        "I could not find a diagnosis for this exact combination of findings. Please review the selected clinical information.",
        language
    )


def build_incomplete_message(language: str) -> str:
    return translate_text(
        "The screening is incomplete. Please answer all required items before requesting the diagnosis.",
        language
    )


def build_invalid_answer_message(index: int, language: str) -> str:
    if normalize_text(language) == "portuguese":
        text = "Não consegui identificar essa resposta com segurança. Responda usando uma das opções mostradas."
    else:
        text = "I could not identify that response safely. Please answer using one of the listed options."
    return f"{text}\n\n{build_question_text(index, language)}"


def format_captured_fields(extracted: dict, language: str):
    return {field: label_for_code(code, language) for field, code in extracted.items()}


def summarize_recent_context(session: dict, language: str = "English", max_items: int = 6):
    items = []
    for field in FIELD_ORDER:
        if field in session["answers"]:
            items.append(f"{field}: {label_for_code(session['answers'][field], language)}")
    return "; ".join(items[:max_items])


# =========================
# EXTRACTION
# =========================
def extract_answers_fallback(user_text: str, session: dict):
    extracted = {}

    sync_current_question(session)
    if session["stage"] == "completed":
        return extracted

    current_field = QUESTION_DEFS[session["current_question"]]["field"]
    norm = normalize_text(user_text)

    current_code = canonicalize_value(current_field, user_text)
    if current_code:
        extracted[current_field] = current_code

    multi_signal = any(sep in norm for sep in [";", ",", " e ", " and ", " além disso", "também", "also"])
    explicit_field_cues = any(term in norm for term in [
        "vitalidade", "teste de vitalidade", "percuss", "palpa", "radiogra", "rx", "radiograf"
    ])

    if multi_signal or explicit_field_cues:
        remaining_fields = [field for field in FIELD_ORDER if field not in session["answers"] and field != current_field]
        for field in remaining_fields:
            code = canonicalize_value(field, user_text)
            if code:
                if normalize_text(user_text) in {"sem dor", "normal", "sensivel", "sensível", "circunscrita"}:
                    continue
                extracted[field] = code

    if extracted.get("PAIN") == "pain_absent":
        extracted["ONSET"] = "onset_na"

    return extracted


def extract_answers_with_llm(user_text: str, session: dict):
    remaining_fields = [field for field in FIELD_ORDER if field not in session["answers"]]
    if not remaining_fields:
        return {}

    current_field = QUESTION_DEFS[session["current_question"]]["field"]
    language = session.get("language") or "English"
    clinical_context = summarize_recent_context(session, language)

    options_by_field = {}
    for field in remaining_fields:
        options_by_field[field] = []
        for code in FIELD_TO_CODES[field]:
            meta = OPTION_CATALOG[code]
            options_by_field[field].append({
                "code": code,
                "label": meta["label"],
                "label_pt": meta.get("label_pt", meta["label"]),
                "aliases": meta["aliases"][:10]
            })

    prompt = f"""
You are extracting structured endodontic triage data.

Current field being asked:
{current_field}

Return ONLY a valid JSON object in this format:
{{
  "answers": {{
    "PAIN": null,
    "ONSET": null,
    "PULP VITALITY": null,
    "PERCUSSION": null,
    "PALPATION": null,
    "RADIOGRAPHY": null
  }}
}}

If a field is identified, use:
{{"code": "...", "evidence": "...", "confidence": 0.0}}

Strict rules:
- Prefer extracting ONLY the current field.
- Extract additional fields only if the message explicitly contains more than one clinical finding.
- Do not infer percussion, palpation, or radiography from generic terms such as "normal", "sem dor", "sensitive", or "circumscribed" unless the message explicitly refers to that exam.
- Never invent findings.
- Confidence must be between 0 and 1.
- If PAIN is clearly absent, ONSET may be set to onset_na.

Known clinical context:
{clinical_context if clinical_context else "None"}

Allowed options:
{json.dumps(options_by_field, ensure_ascii=False, indent=2)}

User message:
{user_text}
""".strip()

    try:
        response = safe_chat_completion(
            messages=[
                {"role": "system", "content": "You extract structured clinical data and return only JSON."},
                {"role": "user", "content": prompt}
            ],
            model=MODEL_EXTRACT,
            temperature=0,
            response_format={"type": "json_object"}
        )
        raw = response.choices[0].message.content or "{}"
        data = safe_json_loads(raw) or {}

        extracted = {}
        for field, payload in (data.get("answers") or {}).items():
            if field not in remaining_fields or not payload:
                continue
            code = payload.get("code")
            confidence = payload.get("confidence", 0)
            if code in FIELD_TO_CODES[field] and isinstance(confidence, (int, float)) and confidence >= 0.80:
                extracted[field] = code

        return extracted
    except Exception:
        return {}


def merge_extracted_answers(session: dict, extracted: dict):
    for field, code in extracted.items():
        if field in FIELD_ORDER and code in FIELD_TO_CODES[field]:
            session["answers"][field] = code
    sync_current_question(session)


# =========================
# DIAGNOSIS ENGINE
# =========================
def find_diagnosis_row(answers: dict):
    temp_df = df.copy()

    conditions = (
        (temp_df["__code_PAIN"] == answers.get("PAIN")) &
        (temp_df["__code_ONSET"] == answers.get("ONSET")) &
        (temp_df["__code_PULP VITALITY"] == answers.get("PULP VITALITY")) &
        (temp_df["__code_PERCUSSION"] == answers.get("PERCUSSION")) &
        (temp_df["__code_PALPATION"] == answers.get("PALPATION")) &
        (temp_df["__code_RADIOGRAPHY"] == answers.get("RADIOGRAPHY"))
    )

    result = temp_df[conditions]
    if result.empty:
        return None
    return result.iloc[0]


def run_diagnosis_from_session(session: dict):
    missing_fields = [field for field in FIELD_ORDER if field not in session["answers"]]
    if missing_fields:
        return {
            "ok": False,
            "type": "incomplete",
            "missing_fields": missing_fields
        }

    row = find_diagnosis_row(session["answers"])
    if row is None:
        return {
            "ok": False,
            "type": "not_found"
        }

    col_2009 = "DIAGNOSIS (AAE NOMENCLATURE 2009/2013)"
    col_2025 = "DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)"
    col_comp = "COMPLEMENTARY DIAGNOSIS"

    diagnosis_aae_2009_2013 = str(row[col_2009]).strip() if col_2009 in row.index else ""
    diagnosis_aae_ese_2025 = str(row[col_2025]).strip() if col_2025 in row.index else ""
    complementary_diagnosis = str(row[col_comp]).strip() if col_comp in row.index else ""

    session["diagnosis_result"] = {
        "DIAGNOSIS (AAE NOMENCLATURE 2009/2013)": diagnosis_aae_2009_2013,
        "DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)": diagnosis_aae_ese_2025,
        "COMPLEMENTARY DIAGNOSIS": complementary_diagnosis
    }

    return {
        "ok": True,
        "diagnosis_aae_2009_2013": diagnosis_aae_2009_2013,
        "diagnosis_aae_ese_2025": diagnosis_aae_ese_2025,
        "complementary_diagnosis": complementary_diagnosis
    }


def build_final_message(language: str, diagnosis_payload=None) -> str:
    if not diagnosis_payload or not diagnosis_payload.get("ok"):
        return translate_text("Screening completed. We can now calculate the diagnosis.", language)

    text = f"""
Screening completed.

Diagnostic result:
- Diagnosis (AAE nomenclature 2009/2013): {diagnosis_payload.get("diagnosis_aae_2009_2013", "")}
- Diagnosis (AAE/ESE nomenclature 2025): {diagnosis_payload.get("diagnosis_aae_ese_2025", "")}
- Complementary diagnosis: {diagnosis_payload.get("complementary_diagnosis", "")}
""".strip()
    return translate_text(text, language)


def build_response_after_processing(session: dict, extracted: dict, primary_field: str, primary_code: str):
    language = session["language"] or "English"

    if session["stage"] == "completed":
        diagnosis_payload = run_diagnosis_from_session(session)
        final_message = build_final_message(language, diagnosis_payload if diagnosis_payload.get("ok") else None)

        payload = {
            "campo": primary_field,
            "resposta_interpretada": label_for_code(primary_code, language),
            "mensagem": final_message,
            "captured_fields": format_captured_fields(extracted, language)
        }

        if diagnosis_payload.get("ok"):
            payload["diagnosis"] = diagnosis_payload
        elif diagnosis_payload.get("type") == "not_found":
            payload["mensagem"] += "\n\n" + build_inconsistent_message(language)

        return cache_payload(session, payload)

    next_index = session["current_question"]
    next_question = build_question_text(next_index, language)
    payload = {
        "campo": primary_field,
        "resposta_interpretada": label_for_code(primary_code, language),
        "mensagem": next_question,
        "pergunta": next_question,
        "captured_fields": format_captured_fields(extracted, language)
    }
    return cache_payload(session, payload)


# =========================
# ROOT / HEALTH
# =========================
@app.get("/", response_class=HTMLResponse)
async def root():
    index_file = STATIC_DIR / "index.html"
    if index_file.exists():
        return index_file.read_text(encoding="utf-8")
    return """
    <html>
        <body>
            <h2>Endo10 EVO API is running.</h2>
        </body>
    </html>
    """


@app.get("/health")
async def health():
    return {"status": "ok"}


# =========================
# PERGUNTAR
# =========================
@app.post("/perguntar/")
async def perguntar(indice: int = Form(...), session_id: str = Form(...)):
    create_session_if_needed(session_id)
    session = sessions[session_id]
    language = session["language"] or "English"

    sync_current_question(session)

    if session["stage"] == "greeting":
        texto = translate_text(
            "Hello! I am Endo10 EVO, a virtual assistant developed to support diagnostic reasoning in Endodontics.",
            language
        )
        payload = {"pergunta": texto, "mensagem": texto}
        return cache_payload(session, payload)

    current_index = session["current_question"]
    if current_index < len(QUESTION_DEFS):
        texto = build_question_text(current_index, language)
        payload = {"pergunta": texto, "mensagem": texto}
        return cache_payload(session, payload)

    payload = {"mensagem": build_final_message(language)}
    return cache_payload(session, payload)


# =========================
# RESPONDER
# =========================
@app.post("/responder/")
async def responder(indice: int = Form(...), resposta_usuario: str = Form(...), session_id: str = Form(...)):
    create_session_if_needed(session_id)
    session = sessions[session_id]

    user_text = (resposta_usuario or "").strip()

    if not session["language"]:
        session["language"] = detect_language(user_text)

    language = session["language"]

    if user_text:
        session["history"].append({"role": "user", "content": user_text})

    if session["stage"] == "greeting":
        session["stage"] = "triage"
        session["current_question"] = 0

        if is_greeting(user_text):
            intro_first = build_intro_and_first_question(language)
            payload = {
                "campo": "__FLOW__",
                "resposta_interpretada": "START_SCREENING",
                "mensagem": intro_first,
                "pergunta": intro_first
            }
            return cache_payload(session, payload)

        extracted = extract_answers_fallback(user_text, session)
        if not extracted:
            extracted = extract_answers_with_llm(user_text, session)

        if extracted:
            merge_extracted_answers(session, extracted)
            primary_field = list(extracted.keys())[0]
            primary_code = extracted[primary_field]
            return build_response_after_processing(session, extracted, primary_field, primary_code)

        intro_first = build_intro_and_first_question(language)
        payload = {
            "campo": "__FLOW__",
            "resposta_interpretada": "START_SCREENING",
            "mensagem": intro_first,
            "pergunta": intro_first
        }
        return cache_payload(session, payload)

    sync_current_question(session)

    if session["stage"] == "completed":
        diagnosis_payload = run_diagnosis_from_session(session)
        final_message = build_final_message(language, diagnosis_payload if diagnosis_payload.get("ok") else None)
        payload = {
            "campo": "__FLOW__",
            "resposta_interpretada": "READY_FOR_DIAGNOSIS",
            "mensagem": final_message
        }
        if diagnosis_payload.get("ok"):
            payload["diagnosis"] = diagnosis_payload
        elif diagnosis_payload.get("type") == "not_found":
            payload["mensagem"] += "\n\n" + build_inconsistent_message(language)
        return cache_payload(session, payload)

    current_index = session["current_question"]
    current_field = QUESTION_DEFS[current_index]["field"]

    extracted = extract_answers_fallback(user_text, session)
    if not extracted:
        extracted = extract_answers_with_llm(user_text, session)

    if current_field not in extracted:
        direct_current = canonicalize_value(current_field, user_text)
        if direct_current:
            extracted[current_field] = direct_current

    if not extracted:
        invalid = build_invalid_answer_message(current_index, language)
        payload = {
            "campo": "__FLOW__",
            "resposta_interpretada": "REASK_CURRENT",
            "mensagem": invalid,
            "pergunta": invalid
        }
        return cache_payload(session, payload)

    merge_extracted_answers(session, extracted)
    primary_field = current_field if current_field in extracted else list(extracted.keys())[0]
    primary_code = extracted[primary_field]
    return build_response_after_processing(session, extracted, primary_field, primary_code)


# =========================
# CONFIRMAR
# =========================
@app.post("/confirmar/")
async def confirmar(indice: int = Form(...), resposta_interpretada: str = Form(...), session_id: str = Form(...)):
    create_session_if_needed(session_id)
    session = sessions[session_id]

    # compatibilidade com frontend antigo:
    # apenas devolve o último payload já processado
    if session.get("last_bot_payload"):
        return session["last_bot_payload"]

    language = session["language"] or "English"
    sync_current_question(session)

    if session["stage"] == "completed":
        diagnosis_payload = run_diagnosis_from_session(session)
        payload = {
            "mensagem": build_final_message(language, diagnosis_payload if diagnosis_payload.get("ok") else None)
        }
        if diagnosis_payload.get("ok"):
            payload["diagnosis"] = diagnosis_payload
        return cache_payload(session, payload)

    current_index = session["current_question"]
    texto = build_question_text(current_index, language)
    payload = {"mensagem": texto, "pergunta": texto}
    return cache_payload(session, payload)


# =========================
# DIAGNOSTICO
# =========================
@app.post("/diagnostico/")
async def diagnostico(session_id: str = Form(...)):
    create_session_if_needed(session_id)
    session = sessions[session_id]
    language = session["language"] or "English"

    sync_current_question(session)
    diagnosis_payload = run_diagnosis_from_session(session)

    if not diagnosis_payload.get("ok"):
        if diagnosis_payload.get("type") == "incomplete":
            return {
                "status": "incomplete",
                "mensagem": build_incomplete_message(language),
                "missing_fields": diagnosis_payload.get("missing_fields", [])
            }

        return {
            "status": "not_found",
            "mensagem": build_inconsistent_message(language)
        }

    return {
        "status": "ok",
        "diagnosis_aae_2009_2013": diagnosis_payload["diagnosis_aae_2009_2013"],
        "diagnosis_aae_ese_2025": diagnosis_payload["diagnosis_aae_ese_2025"],
        "complementary_diagnosis": diagnosis_payload["complementary_diagnosis"],
        "diagnostico": diagnosis_payload["diagnosis_aae_2009_2013"],
        "diagnostico_complementar": diagnosis_payload["complementary_diagnosis"],
        "answers_interpreted": {
            field: label_for_code(session["answers"].get(field), language)
            for field in FIELD_ORDER
        }
    }


# =========================
# EXPLICACAO
# =========================
@app.post("/explicacao/")
async def explicacao(
    session_id: str = Form(...),
    diagnosis_aae_2009_2013: str = Form(None),
    diagnosis_aae_ese_2025: str = Form(None),
    complementary_diagnosis: str = Form(None),
    diagnostico: str = Form(None),
    diagnostico_complementar: str = Form(None)
):
    create_session_if_needed(session_id)
    session = sessions[session_id]
    language = session["language"] or "English"

    stored = session.get("diagnosis_result", {})

    diag_2009 = diagnosis_aae_2009_2013 or stored.get("DIAGNOSIS (AAE NOMENCLATURE 2009/2013)") or diagnostico or ""
    diag_2025 = diagnosis_aae_ese_2025 or stored.get("DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)") or ""
    comp_diag = complementary_diagnosis or stored.get("COMPLEMENTARY DIAGNOSIS") or diagnostico_complementar or ""

    if not diag_2009 and not diag_2025 and not comp_diag:
        return JSONResponse(
            status_code=400,
            content={"mensagem": "No diagnosis is available yet. Run /diagnostico/ first."}
        )

    prompt = f"""
Explain clearly to a dentist the following endodontic diagnostic result.

Write the entire answer in {language}.
Do not switch languages.
Use a professional and clinically coherent tone.
If there are two nomenclatures, explain that they correspond to different diagnostic classification systems.
If there is a complementary diagnosis, explain its practical clinical meaning.
Do not mention that you are an AI model.

Diagnostic result:
- Diagnosis according to AAE nomenclature 2009/2013: {diag_2009}
- Diagnosis according to AAE/ESE nomenclature 2025: {diag_2025}
- Complementary diagnosis: {comp_diag}
""".strip()

    try:
        response = safe_chat_completion(
            messages=[
                {"role": "system", "content": "You are an endodontics professor."},
                {"role": "user", "content": prompt}
            ],
            model=MODEL_EXPLAIN,
            temperature=0.2
        )
        explanation_text = (response.choices[0].message.content or "").strip()
    except Exception as e:
        return JSONResponse(
            status_code=500,
            content={"mensagem": f"Error generating explanation: {str(e)}"}
        )

    return JSONResponse(content={"explicacao": explanation_text})


# =========================
# RESET
# =========================
@app.post("/reset/")
async def reset_session(session_id: str = Form(...)):
    sessions[session_id] = empty_session()
    return {"mensagem": "Session reset successfully."}


# =========================
# PDF
# =========================
@app.get("/pdf/{session_id}")
async def gerar_pdf(session_id: str):
    create_session_if_needed(session_id)
    session = sessions[session_id]

    answers = session.get("answers", {})
    diagnosis_result = session.get("diagnosis_result", {})
    language = session.get("language") or "English"

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    width, height = letter
    y = height - 50
    left = 50

    def write_lines(lines, step=16):
        nonlocal y
        for line in lines:
            if y < 50:
                p.showPage()
                p.setFont("Helvetica", 11)
                y = height - 50
            p.drawString(left, y, line)
            y -= step

    p.setFont("Helvetica-Bold", 13)
    write_lines(["Endo10 EVO - Endodontic Screening Report"], step=20)

    p.setFont("Helvetica", 11)
    write_lines([f"Session ID: {session_id}"], step=18)
    write_lines([""], step=10)

    write_lines(["Answers:"], step=18)
    for q in QUESTION_DEFS:
        field = q["field"]
        value = label_for_code(answers.get(field), language) if answers.get(field) else "Not answered"
        for line in wrap_pdf_lines(f"- {field}: {value}", width=95):
            write_lines([line])

    write_lines([""], step=10)
    write_lines(["Diagnostic result:"], step=18)

    if diagnosis_result:
        lines = [
            f"- Diagnosis (AAE Nomenclature 2009/2013): {diagnosis_result.get('DIAGNOSIS (AAE NOMENCLATURE 2009/2013)', '')}",
            f"- Diagnosis (AAE/ESE Nomenclature 2025): {diagnosis_result.get('DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)', '')}",
            f"- Complementary diagnosis: {diagnosis_result.get('COMPLEMENTARY DIAGNOSIS', '')}",
        ]
        for item in lines:
            for line in wrap_pdf_lines(item, width=95):
                write_lines([line])
    else:
        write_lines(["No diagnosis has been generated yet."])

    p.save()
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=endodontic_screening_report.pdf"}
    )
