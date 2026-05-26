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
from difflib import SequenceMatcher

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
    """
    Normalize free-text answers for clinical option matching.

    The goal is to accept natural answers such as "normal", "Normal",
    "não teve", "esta com dor", or "espessamento do ligamento periodontal"
    without allowing the chatbot to fill more than the current clinical field.
    """
    if text is None:
        return ""
    text = str(text).strip().lower()
    text = unicodedata.normalize("NFKD", text)
    text = "".join(ch for ch in text if not unicodedata.combining(ch))
    text = text.replace("\n", " ").replace("\r", " ")
    text = re.sub(r"[^a-z0-9\s]", " ", text)
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
            temperature=0,
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
            temperature=0,
        )
        translated = (response.choices[0].message.content or "").strip()
        return translated if translated else text
    except Exception:
        return text


def is_greeting(text: str) -> bool:
    norm = normalize_text(text)
    greetings = {
        "hi", "hello", "hey", "oi", "ola", "olá", "hola", "salut", "hallo", "ciao",
        "bom dia", "boa tarde", "boa noite", "good morning", "good afternoon", "good evening"
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
            "absent", "no pain", "without pain", "pain absent", "ausente", "sem dor",
            "nao tem dor", "não tem dor", "ele esta sem dor", "ele está sem dor",
            "esta sem dor", "está sem dor", "paciente sem dor", "assintomatico",
            "assintomático", "sem queixa de dor"
        ],
        "spreadsheet_values": ["Absent"],
    },
    "pain_present": {
        "label": "Present",
        "label_pt": "Presente",
        "description": "The patient reports pain or some type of discomfort.",
        "description_pt": "O paciente relata dor ou algum tipo de desconforto.",
        "aliases": [
            "present", "pain", "with pain", "has pain", "pain present", "presente",
            "com dor", "tem dor", "dor presente", "ele esta com dor", "ele está com dor",
            "esta com dor", "está com dor", "paciente com dor", "dolorido", "dolorosa",
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
            "spontaneous", "spontaneously", "espontanea", "espontânea", "espontaneo",
            "espontâneo", "inicia espontaneamente", "dor espontanea", "dor espontânea"
        ],
        "spreadsheet_values": ["Spontaneous", "Spontaneous\n"],
    },
    "onset_provoked": {
        "label": "Provoked",
        "label_pt": "Provocada",
        "description": "The pain starts after a stimulus, such as cold, heat, pressure, or sweets.",
        "description_pt": "A dor se inicia após um estímulo, como frio, calor, pressão ou doce.",
        "aliases": [
            "provoked", "provocada", "provocado", "apos estimulo", "após estímulo",
            "apos frio", "após frio", "apos calor", "após calor", "desencadeada", "induzida"
        ],
        "spreadsheet_values": ["Provoked"],
    },
    "pulp_altered": {
        "label": "Altered",
        "label_pt": "Alterada",
        "description": "There is an exaggerated or persistent painful response to vitality testing.",
        "description_pt": "Há uma resposta dolorosa exagerada ou persistente ao teste de vitalidade.",
        "aliases": [
            "altered", "alterada", "alterado", "resposta exacerbada", "resposta aumentada",
            "resposta persistente", "dor persistente", "hiperreativo", "hyperreactive", "lingering response"
        ],
        "spreadsheet_values": ["Alterad"],
    },
    "pulp_negative": {
        "label": "Negative",
        "label_pt": "Negativa",
        "description": "There is no response to vitality testing.",
        "description_pt": "Não há resposta ao teste de vitalidade.",
        "aliases": [
            "negative", "negativo", "sem resposta", "no response", "nao respondeu", "não respondeu",
            "teste negativo", "sem resposta ao teste", "does not respond", "non-responsive"
        ],
        "spreadsheet_values": ["Negative"],
    },
    "pulp_normal": {
        "label": "Normal",
        "label_pt": "Normal",
        "description": "There is a mild, transient response that disappears shortly after the stimulus is removed.",
        "description_pt": "Há uma resposta leve e transitória que desaparece logo após a remoção do estímulo.",
        "aliases": ["normal", "resposta normal", "transient response", "resposta transitória", "resposta leve"],
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
        "aliases": ["normal", "sem dor", "no pain", "not sensitive", "sem sensibilidade", "indolor", "negative percussion"],
        "spreadsheet_values": ["Normal"],
    },
    "percussion_sensitive": {
        "label": "Sensitive",
        "label_pt": "Sensível",
        "description": "There is pain or sensitivity on percussion.",
        "description_pt": "Há dor ou sensibilidade à percussão.",
        "aliases": [
            "sensitive", "sensivel", "sensível", "painful", "tender", "doloroso",
            "dor a percussao", "dor à percussão", "sensivel a percussao", "sensível à percussão"
        ],
        "spreadsheet_values": ["Sensitive"],
    },
    "palpation_edema": {
        "label": "Edema",
        "label_pt": "Edema",
        "description": "There is swelling of the adjacent tissues.",
        "description_pt": "Há aumento de volume dos tecidos adjacentes.",
        "aliases": ["edema", "swelling", "swollen", "inchaco", "inchaço", "tumefacao", "tumefação"],
        "spreadsheet_values": ["Edema"],
    },
    "palpation_fistula": {
        "label": "Fistula",
        "label_pt": "Fístula",
        "description": "There is a sinus tract or a mucosal/cutaneous opening communicating with the root apex.",
        "description_pt": "Há fístula ou trajeto fistuloso comunicando-se com o ápice radicular.",
        "aliases": ["fistula", "fístula", "sinus tract", "trajeto fistuloso", "fistulous tract", "parulis", "parúlide"],
        "spreadsheet_values": ["Fistula"],
    },
    "palpation_normal": {
        "label": "Normal",
        "label_pt": "Normal",
        "description": "There is no pain on palpation.",
        "description_pt": "Não há dor à palpação.",
        "aliases": ["normal", "sem dor", "no pain", "not sensitive", "indolor", "sem sensibilidade"],
        "spreadsheet_values": ["Normal"],
    },
    "palpation_sensitive": {
        "label": "Sensitive",
        "label_pt": "Sensível",
        "description": "There is pain or sensitivity on palpation.",
        "description_pt": "Há dor ou sensibilidade à palpação.",
        "aliases": [
            "sensitive", "sensivel", "sensível", "painful", "tender", "doloroso",
            "dor a palpacao", "dor à palpação", "sensivel a palpacao", "sensível à palpação"
        ],
        "spreadsheet_values": ["Sensivel"],
    },
    "radiography_circumscribed_radiolucency": {
        "label": "Circumscribed radiolucency lesion",
        "label_pt": "Lesão radiolúcida circunscrita",
        "description": "There is a well-defined radiolucent lesion with relatively distinct borders.",
        "description_pt": "Há uma lesão radiolúcida bem definida com bordas relativamente distintas.",
        "aliases": [
            "circumscribed", "circunscrita", "circumscribed radiolucency", "circumscribed radiolucency lesion",
            "lesao radiolucida circunscrita", "lesão radiolúcida circunscrita", "radiolucidez circunscrita"
        ],
        "spreadsheet_values": ["Circumscribed radiolucency lesion"],
    },
    "radiography_diffuse_apical_radiolucency": {
        "label": "Diffuse apical radiolucency",
        "label_pt": "Radiolucidez apical difusa",
        "description": "There is a diffuse apical radiolucent image or poorly defined radiolucency.",
        "description_pt": "Há uma imagem radiolucente apical difusa ou radiolucidez mal definida.",
        "aliases": [
            "diffuse", "difusa", "diffuse apical radiolucency", "apical diffuse radiolucency",
            "radiolucidez apical difusa", "lesao radiolucida difusa", "lesão radiolúcida difusa"
        ],
        "spreadsheet_values": ["Diffuse apical radiolucency"],
    },
    "radiography_thickening_pdl": {
        "label": "Thickening of the periodontal ligament",
        "label_pt": "Espessamento do ligamento periodontal",
        "description": "There is widening or thickening of the periodontal ligament space.",
        "description_pt": "Há alargamento ou espessamento do espaço do ligamento periodontal.",
        "aliases": [
            "thickening", "widening", "espessamento", "alargamento", "thickening of the periodontal ligament",
            "periodontal ligament thickening", "espessamento do ligamento periodontal",
            "alargamento do ligamento periodontal", "espessamento do espaco periodontal",
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
            "diffuse radiopaque", "radiopaca difusa", "radiopaque diffuse", "radiopaco difuso",
            "diffuse radiopaque lesion", "lesao radiopaca difusa", "lesão radiopaca difusa"
        ],
        "spreadsheet_values": ["Diffuse radiopaque lesion"],
    },
}

QUESTION_DEFS = [
    {
        "field": "PAIN",
        "question": "Is the patient in pain?",
        "question_pt": "O paciente está com dor?",
        "codes": ["pain_absent", "pain_present"],
    },
    {
        "field": "ONSET",
        "question": "How did the pain start?",
        "question_pt": "Como a dor começou?",
        "codes": ["onset_na", "onset_spontaneous", "onset_provoked"],
    },
    {
        "field": "PULP VITALITY",
        "question": "What was the response to the pulp vitality test?",
        "question_pt": "Qual foi a resposta ao teste de vitalidade pulpar?",
        "codes": ["pulp_altered", "pulp_negative", "pulp_normal"],
    },
    {
        "field": "PERCUSSION",
        "question": "What was the finding on percussion?",
        "question_pt": "Qual foi o achado no teste de percussão?",
        "codes": ["percussion_na", "percussion_normal", "percussion_sensitive"],
    },
    {
        "field": "PALPATION",
        "question": "What was the finding on palpation?",
        "question_pt": "Qual foi o achado à palpação?",
        "codes": ["palpation_edema", "palpation_fistula", "palpation_normal", "palpation_sensitive"],
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
            "radiography_diffuse_radiopaque",
        ],
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
    raise RuntimeError(f"Error loading spreadsheet '{EXCEL_FILE}' / sheet '{SHEET_NAME}': {e}")

df.columns = [str(col).strip() for col in df.columns]
for col in df.columns:
    if df[col].dtype == "object":
        df[col] = df[col].fillna("").astype(str).str.strip()

if "PULPT VITALITY" in df.columns and "PULP VITALITY" not in df.columns:
    df = df.rename(columns={"PULPT VITALITY": "PULP VITALITY"})

REQUIRED_SPREADSHEET_COLS = [
    "PAIN",
    "ONSET",
    "PULP VITALITY",
    "PERCUSSION",
    "PALPATION",
    "RADIOGRAPHY",
    "DIAGNOSIS (AAE NOMENCLATURE 2009/2013)",
    "DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)",
    "COMPLEMENTARY DIAGNOSIS",
]

for required_col in REQUIRED_SPREADSHEET_COLS:
    if required_col not in df.columns:
        raise RuntimeError(f"Required column '{required_col}' not found in spreadsheet.")

# =========================
# CANONICALIZATION
# =========================
def term_similarity(a: str, b: str) -> float:
    a = normalize_text(a)
    b = normalize_text(b)
    if not a or not b:
        return 0.0
    return SequenceMatcher(None, a, b).ratio()


def build_terms_for_code(code: str):
    meta = OPTION_CATALOG[code]
    raw_terms = (
        [meta.get("label", ""), meta.get("label_pt", "")] +
        meta.get("aliases", []) +
        meta.get("spreadsheet_values", [])
    )
    terms = []
    seen = set()
    for term in raw_terms:
        nterm = normalize_text(term)
        if nterm and nterm not in seen:
            seen.add(nterm)
            terms.append(nterm)
    return terms


def local_semantic_shortcuts(field: str, norm: str):
    """
    Deterministic clinical shortcuts for common natural answers.
    These are limited to the current field and therefore do not make the flow loose.
    """
    if not norm:
        return None

    # Generic confirmations/negations are only safe in the PAIN question.
    if field == "PAIN":
        no_patterns = [
            "nao", "não", "sem", "ausente", "sem dor", "nao tem dor", "nao teve dor",
            "não tem dor", "não teve dor", "nao doi", "não dói", "nao sente dor",
            "não sente dor", "assintomatico", "assintomático", "indolor"
        ]
        yes_patterns = [
            "sim", "presente", "com dor", "tem dor", "teve dor", "esta com dor",
            "está com dor", "sente dor", "relata dor", "dor", "dolorido", "desconforto"
        ]
        if any(p in norm for p in map(normalize_text, no_patterns)):
            return "pain_absent"
        if any(p in norm for p in map(normalize_text, yes_patterns)):
            return "pain_present"

    if field == "ONSET":
        na_patterns = ["nao se aplica", "não se aplica", "n a", "sem dor", "ausente"]
        spontaneous_patterns = ["espontanea", "espontaneo", "espontaneamente", "do nada", "sem estimulo", "sem estímulo"]
        provoked_patterns = ["provocada", "provocado", "com estimulo", "com estímulo", "apos estimulo", "após estímulo", "frio", "calor", "mastigacao", "mastigação", "doce", "pressao", "pressão"]
        if any(p in norm for p in map(normalize_text, na_patterns)):
            return "onset_na"
        if any(p in norm for p in map(normalize_text, spontaneous_patterns)):
            return "onset_spontaneous"
        if any(p in norm for p in map(normalize_text, provoked_patterns)):
            return "onset_provoked"

    if field == "PULP VITALITY":
        negative_patterns = [
            "negativa", "negativo", "sem resposta", "nao respondeu", "não respondeu",
            "nao teve", "não teve", "nao houve resposta", "não houve resposta",
            "ausente", "sem reacao", "sem reação", "zero resposta"
        ]
        altered_patterns = [
            "alterada", "alterado", "exagerada", "exacerbada", "persistente",
            "demorada", "dor persistente", "resposta dolorosa", "resposta aumentada"
        ]
        normal_patterns = ["normal", "resposta normal", "leve", "transitoria", "transitória"]
        if any(p in norm for p in map(normalize_text, negative_patterns)):
            return "pulp_negative"
        if any(p in norm for p in map(normalize_text, altered_patterns)):
            return "pulp_altered"
        if any(p in norm for p in map(normalize_text, normal_patterns)):
            return "pulp_normal"

    if field == "PERCUSSION":
        if any(p in norm for p in map(normalize_text, ["nao se aplica", "não se aplica", "n a"])):
            return "percussion_na"
        if any(p in norm for p in map(normalize_text, ["sensivel", "sensível", "dor", "doloroso", "positivo", "tender"])):
            return "percussion_sensitive"
        if any(p in norm for p in map(normalize_text, ["normal", "sem dor", "negativo", "sem sensibilidade", "indolor"])):
            return "percussion_normal"

    if field == "PALPATION":
        if any(p in norm for p in map(normalize_text, ["edema", "inchaco", "inchaço", "aumento de volume", "tumefacao", "tumefação"])):
            return "palpation_edema"
        if any(p in norm for p in map(normalize_text, ["fistula", "fístula", "trajeto fistuloso", "parulis", "parúlide"])):
            return "palpation_fistula"
        if any(p in norm for p in map(normalize_text, ["sensivel", "sensível", "dor", "doloroso", "positivo", "tender"])):
            return "palpation_sensitive"
        if any(p in norm for p in map(normalize_text, ["normal", "sem dor", "negativo", "sem sensibilidade", "indolor"])):
            return "palpation_normal"

    if field == "RADIOGRAPHY":
        if any(p in norm for p in map(normalize_text, ["espessamento", "alargamento", "ligamento periodontal", "periodontal ligament", "pdl"])):
            return "radiography_thickening_pdl"
        if any(p in norm for p in map(normalize_text, ["radiolucida circunscrita", "radiolúcida circunscrita", "circunscrita", "bem definida"])):
            return "radiography_circumscribed_radiolucency"
        if any(p in norm for p in map(normalize_text, ["radiolucidez apical difusa", "radiolucida difusa", "radiolúcida difusa", "apical difusa", "mal definida"])):
            return "radiography_diffuse_apical_radiolucency"
        if any(p in norm for p in map(normalize_text, ["radiopaca difusa", "radiopaco difuso", "radiopaque diffuse"])):
            return "radiography_diffuse_radiopaque"
        if any(p in norm for p in map(normalize_text, ["normal", "sem alteracoes", "sem alterações", "lamina dura intacta", "lâmina dura intacta"])):
            return "radiography_normal"

    return None


def canonicalize_value(field: str, value: str):
    norm = normalize_text(value)
    if not norm or field not in FIELD_TO_CODES:
        return None

    field_codes = FIELD_TO_CODES[field]

    # 1) Exact match against official labels, Portuguese labels, aliases, and spreadsheet values.
    for code in field_codes:
        for term in build_terms_for_code(code):
            if term == norm:
                return code

    # 2) Deterministic clinical shortcuts for common natural-language answers.
    shortcut = local_semantic_shortcuts(field, norm)
    if shortcut in field_codes:
        return shortcut

    # 3) Longest-term containment. This lets phrases like
    #    "espessamento do ligamento periodontal" match the correct radiographic option.
    candidates = []
    for code in field_codes:
        for term in build_terms_for_code(code):
            if len(term) >= 3:
                candidates.append((len(term), term, code))

    candidates.sort(reverse=True)
    for _, term, code in candidates:
        if term in norm or norm in term:
            return code

    # 4) Conservative fuzzy matching for short/typed answers.
    #    This accepts minor typos but avoids forcing very ambiguous answers.
    best_code = None
    best_score = 0.0
    for code in field_codes:
        for term in build_terms_for_code(code):
            score = term_similarity(norm, term)
            if score > best_score:
                best_score = score
                best_code = code

    if best_score >= 0.88:
        return best_code

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
    # If pain is absent, pain onset is not applicable and should not be asked.
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
    elif session["stage"] != "greeting":
        session["stage"] = "triage"


def build_question_text(index: int, language: str) -> str:
    q = get_question_by_index(index)
    is_pt = normalize_text(language) == "portuguese"
    question_line = q.get("question_pt", q["question"]) if is_pt else q["question"]

    base_text = f"{question_line}\n\n"
    for code in q["codes"]:
        # Hide "Not applicable" only in the percussion question.
        # The diagnostic logic remains unchanged: PERCUSSION is still collected
        # and still used in the spreadsheet matching.
        if q["field"] == "PERCUSSION" and code == "percussion_na":
            continue

        meta = OPTION_CATALOG[code]
        label = meta.get("label_pt", meta["label"]) if is_pt else meta["label"]
        desc = meta.get("description_pt", meta["description"]) if is_pt else meta["description"]
        base_text += f"{label} - {desc}\n"
    return base_text.strip()


def build_intro(language: str) -> str:
    intro_text = """
Hello! I am Endo10 EVO, a virtual assistant developed to support diagnostic reasoning in Endodontics.
This system conducts a structured clinical screening based on signs, symptoms, and complementary examination findings. At the end of the process, a diagnostic suggestion will be presented according to the reference nomenclature adopted by the system.
Please answer one item at a time, according to the option currently requested.
""".strip()
    return translate_text(intro_text, language)


def build_intro_and_first_question(language: str) -> str:
    return f"{build_intro(language)}\n\n{build_question_text(0, language)}"


def build_inconsistent_message(language: str) -> str:
    return translate_text(
        "I could not find a diagnosis for this exact combination of findings. Please review the selected clinical information.",
        language,
    )


def build_incomplete_message(language: str) -> str:
    return translate_text(
        "The screening is incomplete. Please answer all required items before requesting the diagnosis.",
        language,
    )


def build_invalid_answer_message(index: int, language: str) -> str:
    if normalize_text(language) == "portuguese":
        text = "Não consegui identificar essa resposta com segurança. Responda usando uma das opções mostradas."
    else:
        text = "I could not identify that response safely. Please answer using one of the listed options."
    return f"{text}\n\n{build_question_text(index, language)}"


def format_captured_fields(extracted: dict, language: str):
    return {field: label_for_code(code, language) for field, code in extracted.items()}

# =========================
# EXTRACTION - STRICT SEQUENTIAL FLOW
# =========================
def extract_answers_fallback(user_text: str, session: dict):
    """
    Rule-based extraction limited to the current question only.
    This prevents generic answers such as 'normal' or 'sem dor' from filling multiple fields.
    """
    extracted = {}
    sync_current_question(session)

    if session["stage"] == "completed":
        return extracted

    current_field = QUESTION_DEFS[session["current_question"]]["field"]
    current_code = canonicalize_value(current_field, user_text)

    if current_code:
        extracted[current_field] = current_code

    if current_field == "PAIN" and extracted.get("PAIN") == "pain_absent":
        extracted["ONSET"] = "onset_na"

    return extracted


def extract_answers_with_llm(user_text: str, session: dict):
    """
    LLM extraction limited to the current question only.
    The model is not allowed to infer or fill future fields.
    """
    sync_current_question(session)

    if session["stage"] == "completed":
        return {}

    current_field = QUESTION_DEFS[session["current_question"]]["field"]

    options = []
    for code in FIELD_TO_CODES[current_field]:
        meta = OPTION_CATALOG[code]
        options.append({
            "code": code,
            "label": meta["label"],
            "label_pt": meta.get("label_pt", meta["label"]),
            "aliases": meta.get("aliases", [])[:12],
        })

    prompt = f"""
You are extracting ONE structured answer for an endodontic triage chatbot.

Current field:
{current_field}

User message:
{user_text}

Allowed options:
{json.dumps(options, ensure_ascii=False, indent=2)}

Return ONLY valid JSON:
{{
  "code": null,
  "confidence": 0.0
}}

Rules:
- Extract only the current field.
- Do not extract other clinical fields.
- Do not infer unstated findings.
- If the answer is ambiguous, return null.
- Confidence must be between 0 and 1.
""".strip()

    try:
        response = safe_chat_completion(
            messages=[
                {"role": "system", "content": "You extract one clinical answer and return only JSON."},
                {"role": "user", "content": prompt},
            ],
            model=MODEL_EXTRACT,
            temperature=0,
            response_format={"type": "json_object"},
        )

        raw = response.choices[0].message.content or "{}"
        data = safe_json_loads(raw) or {}

        code = data.get("code")
        confidence = data.get("confidence", 0)

        if code in FIELD_TO_CODES[current_field] and isinstance(confidence, (int, float)) and confidence >= 0.80:
            extracted = {current_field: code}
            if current_field == "PAIN" and code == "pain_absent":
                extracted["ONSET"] = "onset_na"
            return extracted

        return {}
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
        (temp_df["__code_PAIN"] == answers.get("PAIN"))
        & (temp_df["__code_ONSET"] == answers.get("ONSET"))
        & (temp_df["__code_PULP VITALITY"] == answers.get("PULP VITALITY"))
        & (temp_df["__code_PERCUSSION"] == answers.get("PERCUSSION"))
        & (temp_df["__code_PALPATION"] == answers.get("PALPATION"))
        & (temp_df["__code_RADIOGRAPHY"] == answers.get("RADIOGRAPHY"))
    )
    result = temp_df[conditions]
    if result.empty:
        return None
    return result.iloc[0]


def run_diagnosis_from_session(session: dict):
    missing_fields = [field for field in FIELD_ORDER if field not in session["answers"]]
    if missing_fields:
        return {"ok": False, "type": "incomplete", "missing_fields": missing_fields}

    row = find_diagnosis_row(session["answers"])
    if row is None:
        return {"ok": False, "type": "not_found"}

    col_2009 = "DIAGNOSIS (AAE NOMENCLATURE 2009/2013)"
    col_2025 = "DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)"
    col_comp = "COMPLEMENTARY DIAGNOSIS"

    diagnosis_aae_2009_2013 = str(row[col_2009]).strip() if col_2009 in row.index else ""
    diagnosis_aae_ese_2025 = str(row[col_2025]).strip() if col_2025 in row.index else ""
    complementary_diagnosis = str(row[col_comp]).strip() if col_comp in row.index else ""

    session["diagnosis_result"] = {
        "DIAGNOSIS (AAE NOMENCLATURE 2009/2013)": diagnosis_aae_2009_2013,
        "DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)": diagnosis_aae_ese_2025,
        "COMPLEMENTARY DIAGNOSIS": complementary_diagnosis,
    }

    return {
        "ok": True,
        "diagnosis_aae_2009_2013": diagnosis_aae_2009_2013,
        "diagnosis_aae_ese_2025": diagnosis_aae_ese_2025,
        "complementary_diagnosis": complementary_diagnosis,
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
            "captured_fields": format_captured_fields(extracted, language),
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
        "captured_fields": format_captured_fields(extracted, language),
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

    if session["stage"] == "greeting":
        session["stage"] = "triage"
        session["current_question"] = 0
        texto = build_intro_and_first_question(language)
        payload = {"pergunta": texto, "mensagem": texto}
        return cache_payload(session, payload)

    sync_current_question(session)

    if session["stage"] == "completed":
        diagnosis_payload = run_diagnosis_from_session(session)
        payload = {"mensagem": build_final_message(language, diagnosis_payload if diagnosis_payload.get("ok") else None)}
        if diagnosis_payload.get("ok"):
            payload["diagnosis"] = diagnosis_payload
        return cache_payload(session, payload)

    current_index = session["current_question"]
    texto = build_question_text(current_index, language)
    payload = {"pergunta": texto, "mensagem": texto}
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
                "pergunta": intro_first,
            }
            return cache_payload(session, payload)

    sync_current_question(session)

    if session["stage"] == "completed":
        diagnosis_payload = run_diagnosis_from_session(session)
        final_message = build_final_message(language, diagnosis_payload if diagnosis_payload.get("ok") else None)
        payload = {
            "campo": "__FLOW__",
            "resposta_interpretada": "READY_FOR_DIAGNOSIS",
            "mensagem": final_message,
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

    # Security lock: accept only the current field, plus automatic ONSET = not applicable when PAIN is absent.
    allowed_fields = {current_field}
    if current_field == "PAIN" and extracted.get("PAIN") == "pain_absent":
        allowed_fields.add("ONSET")
    extracted = {field: code for field, code in extracted.items() if field in allowed_fields}

    if current_field not in extracted:
        invalid = build_invalid_answer_message(current_index, language)
        payload = {
            "campo": "__FLOW__",
            "resposta_interpretada": "REASK_CURRENT",
            "mensagem": invalid,
            "pergunta": invalid,
        }
        return cache_payload(session, payload)

    merge_extracted_answers(session, extracted)
    primary_code = extracted[current_field]
    return build_response_after_processing(session, extracted, current_field, primary_code)

# =========================
# CONFIRMAR
# =========================
@app.post("/confirmar/")
async def confirmar(indice: int = Form(...), resposta_interpretada: str = Form(...), session_id: str = Form(...)):
    create_session_if_needed(session_id)
    session = sessions[session_id]

    # Compatibility with the old frontend: returns the last payload already processed.
    if session.get("last_bot_payload"):
        return session["last_bot_payload"]

    language = session["language"] or "English"
    sync_current_question(session)

    if session["stage"] == "completed":
        diagnosis_payload = run_diagnosis_from_session(session)
        payload = {"mensagem": build_final_message(language, diagnosis_payload if diagnosis_payload.get("ok") else None)}
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
                "missing_fields": diagnosis_payload.get("missing_fields", []),
            }
        return {"status": "not_found", "mensagem": build_inconsistent_message(language)}

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
        },
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
    diagnostico_complementar: str = Form(None),
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
            content={"mensagem": "No diagnosis is available yet. Run /diagnostico/ first."},
        )

    prompt = f"""
Explain clearly to a dentist the following endodontic diagnostic result.
Write the entire answer in {language}. Do not switch languages.
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
                {"role": "user", "content": prompt},
            ],
            model=MODEL_EXPLAIN,
            temperature=0.2,
        )
        explanation_text = (response.choices[0].message.content or "").strip()
    except Exception as e:
        return JSONResponse(status_code=500, content={"mensagem": f"Error generating explanation: {str(e)}"})

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
        headers={"Content-Disposition": "attachment; filename=endodontic_screening_report.pdf"},
    )
