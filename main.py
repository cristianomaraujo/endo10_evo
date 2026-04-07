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


def wrap_pdf_lines(text: str, width: int = 90):
    if not text:
        return [""]
    lines = []
    for paragraph in str(text).split("\n"):
        wrapped = textwrap.wrap(paragraph, width=width) or [""]
        lines.extend(wrapped)
    return lines


def safe_json_loads(text: str):
    try:
        return json.loads(text)
    except Exception:
        return None


def safe_chat_completion(messages, model="gpt-4o-mini", temperature=0, response_format=None):
    kwargs = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
    }
    if response_format is not None:
        kwargs["response_format"] = response_format
    return client.chat.completions.create(**kwargs)


def detect_language(text: str) -> str:
    """
    Detecta idioma de forma robusta, com fallback simples.
    """
    if not text or not str(text).strip():
        return "English"

    # Fallback rápido para português por padrões comuns
    quick_pt_markers = [
        "oi", "olá", "ola", "não", "nao", "dor", "sem dor",
        "paciente", "sensível", "sensivel", "fístula", "fistula"
    ]
    norm = normalize_text(text)
    if any(marker in norm for marker in quick_pt_markers):
        return "Portuguese"

    try:
        prompt = (
            "Detect the language of the text below. "
            "Return only one language name in English, such as: "
            "English, Portuguese, Spanish, French, Italian, German, Chinese, Arabic.\n\n"
            f"Text: {text}"
        )
        response = safe_chat_completion(
            messages=[{"role": "user", "content": prompt}],
            model="gpt-4o-mini",
            temperature=0
        )
        language = (response.choices[0].message.content or "").strip()
        return language if language else "English"
    except Exception:
        return "English"


def translate_text(text: str, target_language: str) -> str:
    """
    Traduz apenas o necessário para exibição.
    A lógica interna sempre permanece em códigos canônicos.
    """
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
            model="gpt-4o-mini",
            temperature=0
        )
        translated = (response.choices[0].message.content or "").strip()
        return translated if translated else text
    except Exception:
        return text


# =========================
# CANONICAL CLINICAL MODEL
# =========================
OPTION_CATALOG = {
    "pain_absent": {
        "label": "Absent",
        "description": "The patient does not report pain.",
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
        "description": "The patient reports pain or some type of discomfort.",
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
        "description": "Use this when the patient does not report pain.",
        "aliases": ["not applicable", "n/a", "nao se aplica", "não se aplica"],
        "spreadsheet_values": ["Not applicable"],
    },
    "onset_spontaneous": {
        "label": "Spontaneous",
        "description": "The pain starts spontaneously, without any provoking stimulus.",
        "aliases": [
            "spontaneous", "spontaneously", "espontanea", "espontânea",
            "espontaneo", "espontâneo", "inicia espontaneamente",
            "dor espontanea", "dor espontânea"
        ],
        "spreadsheet_values": ["Spontaneous", "Spontaneous\n"],
    },
    "onset_provoked": {
        "label": "Provoked",
        "description": "The pain starts after a stimulus, such as cold, heat, pressure, or sweets.",
        "aliases": [
            "provoked", "provocada", "provocado", "apos estimulo",
            "após estímulo", "apos frio", "após frio", "apos calor",
            "após calor", "desencadeada", "induzida"
        ],
        "spreadsheet_values": ["Provoked"],
    },
    "pulp_altered": {
        "label": "Altered",
        "description": "There is an exaggerated or persistent painful response to vitality testing.",
        "aliases": [
            "altered", "alterada", "alterado", "resposta exacerbada",
            "resposta aumentada", "resposta persistente", "dor persistente",
            "hiper-reativo", "hiperreativo", "positive lingering response"
        ],
        "spreadsheet_values": ["Alterad"],
    },
    "pulp_negative": {
        "label": "Negative",
        "description": "There is no response to vitality testing.",
        "aliases": [
            "negative", "negativo", "sem resposta", "no response",
            "nao respondeu", "não respondeu", "teste negativo",
            "sem resposta ao teste", "does not respond", "non-responsive"
        ],
        "spreadsheet_values": ["Negative"],
    },
    "pulp_normal": {
        "label": "Normal",
        "description": "There is a mild, transient response that disappears shortly after the stimulus is removed.",
        "aliases": [
            "normal", "resposta normal", "transient response",
            "resposta transitória", "resposta leve"
        ],
        "spreadsheet_values": ["Normal"],
    },
    "percussion_na": {
        "label": "Not applicable",
        "description": "Use this when percussion testing is not applicable in the clinical situation.",
        "aliases": ["not applicable", "n/a", "nao se aplica", "não se aplica"],
        "spreadsheet_values": ["Not applicable"],
    },
    "percussion_normal": {
        "label": "Normal",
        "description": "There is no pain or sensitivity on percussion.",
        "aliases": [
            "normal", "sem dor", "no pain", "not sensitive",
            "sem sensibilidade", "indolor", "negative percussion"
        ],
        "spreadsheet_values": ["Normal"],
    },
    "percussion_sensitive": {
        "label": "Sensitive",
        "description": "There is pain or sensitivity on percussion.",
        "aliases": [
            "sensitive", "sensivel", "sensível", "painful", "tender",
            "doloroso", "dor a percussao", "dor à percussão",
            "sensivel a percussao", "sensível à percussão"
        ],
        "spreadsheet_values": ["Sensitive"],
    },
    "palpation_edema": {
        "label": "Edema",
        "description": "There is swelling of the adjacent tissues.",
        "aliases": [
            "edema", "swelling", "swollen", "inchaco", "inchaço",
            "tumefacao", "tumefação"
        ],
        "spreadsheet_values": ["Edema"],
    },
    "palpation_fistula": {
        "label": "Fistula",
        "description": "There is a sinus tract or a mucosal/cutaneous opening communicating with the root apex.",
        "aliases": [
            "fistula", "fístula", "sinus tract", "trajeto fistuloso",
            "fistulous tract", "parulis", "parúlide"
        ],
        "spreadsheet_values": ["Fistula"],
    },
    "palpation_normal": {
        "label": "Normal",
        "description": "There is no pain on palpation.",
        "aliases": [
            "normal", "sem dor", "no pain", "not sensitive",
            "indolor", "sem sensibilidade"
        ],
        "spreadsheet_values": ["Normal"],
    },
    "palpation_sensitive": {
        "label": "Sensitive",
        "description": "There is pain or sensitivity on palpation.",
        "aliases": [
            "sensitive", "sensivel", "sensível", "painful", "tender",
            "doloroso", "dor a palpacao", "dor à palpação",
            "sensivel a palpacao", "sensível à palpação"
        ],
        "spreadsheet_values": ["Sensivel"],
    },
    "radiography_circumscribed_radiolucency": {
        "label": "Circumscribed radiolucency lesion",
        "description": "There is a well-defined radiolucent lesion with relatively distinct borders.",
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
        "description": "There is a diffuse apical radiolucent image or poorly defined radiolucency.",
        "aliases": [
            "diffuse", "difusa", "diffuse apical radiolucency",
            "apical diffuse radiolucency", "radiolucidez apical difusa",
            "lesao radiolucida difusa", "lesão radiolúcida difusa"
        ],
        "spreadsheet_values": ["Diffuse apical radiolucency"],
    },
    "radiography_thickening_pdl": {
        "label": "Thickening of the periodontal ligament",
        "description": "There is widening or thickening of the periodontal ligament space.",
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
        "description": "The lamina dura is intact and the periodontal ligament space is uniform.",
        "aliases": ["normal", "sem alteracoes", "sem alterações", "aspecto normal"],
        "spreadsheet_values": ["Normal"],
    },
    "radiography_diffuse_radiopaque": {
        "label": "Diffuse radiopaque lesion",
        "description": "There is a diffuse radiopaque lesion with ill-defined borders and gradual transition to adjacent bone.",
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
        "question": "Pain",
        "codes": ["pain_absent", "pain_present"]
    },
    {
        "field": "ONSET",
        "question": "Onset of pain",
        "codes": ["onset_na", "onset_spontaneous", "onset_provoked"]
    },
    {
        "field": "PULP VITALITY",
        "question": "Pulp vitality",
        "codes": ["pulp_altered", "pulp_negative", "pulp_normal"]
    },
    {
        "field": "PERCUSSION",
        "question": "Percussion",
        "codes": ["percussion_na", "percussion_normal", "percussion_sensitive"]
    },
    {
        "field": "PALPATION",
        "question": "Palpation",
        "codes": ["palpation_edema", "palpation_fistula", "palpation_normal", "palpation_sensitive"]
    },
    {
        "field": "RADIOGRAPHY",
        "question": "Radiographic finding",
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
FIELD_TO_QUESTION = {q["field"]: q["question"] for q in QUESTION_DEFS}

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

# Compatibilidade com nome antigo
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
# CANONICALIZATION OF SPREADSHEET
# =========================
def build_value_to_code_map():
    value_to_code = {}
    for code, meta in OPTION_CATALOG.items():
        for val in [meta["label"]] + meta.get("aliases", []) + meta.get("spreadsheet_values", []):
            value_to_code[normalize_text(val)] = code
    return value_to_code


VALUE_TO_CODE = build_value_to_code_map()


def canonicalize_value(field: str, value: str):
    """
    Converte valores livres ou da planilha para um código canônico.
    """
    norm = normalize_text(value)
    if not norm:
        return None

    direct = VALUE_TO_CODE.get(norm)
    if direct and direct in FIELD_TO_CODES[field]:
        return direct

    # Busca por alias contido no texto
    candidates = []
    for code in FIELD_TO_CODES[field]:
        meta = OPTION_CATALOG[code]
        terms = [meta["label"]] + meta.get("aliases", []) + meta.get("spreadsheet_values", [])
        for term in terms:
            nterm = normalize_text(term)
            if nterm:
                candidates.append((len(nterm), nterm, code))

    # Termos maiores primeiro para reduzir colisões
    candidates.sort(reverse=True)

    for _, nterm, code in candidates:
        if nterm in norm:
            return code

    # Regras específicas por campo
    if field == "PAIN":
        if any(term in norm for term in ["sem dor", "nao tem dor", "não tem dor", "assintomatico", "assintomático"]):
            return "pain_absent"
        if any(term in norm for term in ["com dor", "tem dor", "dor presente", "dolorido", "dolorosa", "relata dor"]):
            return "pain_present"

    if field == "PULP VITALITY":
        if any(term in norm for term in ["sem resposta", "nao respondeu", "não respondeu", "teste negativo"]):
            return "pulp_negative"
        if any(term in norm for term in ["resposta persistente", "resposta exacerbada", "aumentada"]):
            return "pulp_altered"

    return None


def label_for_code(code: str):
    if not code:
        return ""
    return OPTION_CATALOG.get(code, {}).get("label", code)


def spreadsheet_value_for_code(code: str):
    if not code:
        return ""
    values = OPTION_CATALOG.get(code, {}).get("spreadsheet_values", [])
    return values[0] if values else OPTION_CATALOG.get(code, {}).get("label", "")


# Adiciona colunas canônicas ao dataframe
for field in FIELD_ORDER:
    df[f"__code_{field}"] = df[field].apply(lambda x: canonicalize_value(field, x))

# Validação opcional para detectar valores não mapeados
unmapped_rows = df[[f"__code_{field}" for field in FIELD_ORDER]].isna().any(axis=1)
if unmapped_rows.any():
    bad_indices = df[unmapped_rows].index.tolist()
    raise RuntimeError(
        f"Some spreadsheet rows could not be canonicalized. Row indices: {bad_indices}"
    )

# =========================
# SESSIONS
# =========================
sessions = {}


def create_session_if_needed(session_id: str):
    if session_id not in sessions:
        sessions[session_id] = {
            "language": None,
            "stage": "greeting",   # greeting -> triage -> completed
            "current_question": 0,
            "answers": {},         # field -> canonical code
            "diagnosis_result": {},
            "history": []
        }


def get_question_by_index(index: int):
    if index < 0 or index >= len(QUESTION_DEFS):
        raise HTTPException(status_code=400, detail="Invalid question index.")
    return QUESTION_DEFS[index]


def get_next_unanswered_index(session: dict):
    """
    Retorna o próximo índice não respondido.
    """
    for idx, q in enumerate(QUESTION_DEFS):
        if q["field"] not in session["answers"]:
            return idx
    return len(QUESTION_DEFS)


def apply_business_rules(session: dict):
    """
    Regras clínicas/operacionais determinísticas.
    """
    if session["answers"].get("PAIN") == "pain_absent":
        session["answers"]["ONSET"] = "onset_na"


def sync_current_question(session: dict):
    apply_business_rules(session)
    session["current_question"] = get_next_unanswered_index(session)
    if session["current_question"] >= len(QUESTION_DEFS):
        session["stage"] = "completed"


def build_question_text(index: int, language: str) -> str:
    q = get_question_by_index(index)
    base_text = f"{q['question']}\n\n"
    for code in q["codes"]:
        meta = OPTION_CATALOG[code]
        base_text += f"{meta['label']} - {meta['description']}\n"
    return translate_text(base_text.strip(), language)


def build_intro_and_first_question(language: str) -> str:
    intro_text = """
Hello! I am Endo10 EVO, a virtual assistant developed to support diagnostic reasoning in Endodontics.

This system conducts a structured clinical screening based on signs, symptoms, and complementary examination findings. At the end of the process, a diagnostic suggestion will be presented according to the reference nomenclature adopted by the system.

You may answer briefly or in natural language. If your message contains more than one clinical finding, I will try to identify them automatically.

We will begin with the first variable.
""".strip()

    intro_text = translate_text(intro_text, language)
    first_question = build_question_text(0, language)
    return f"{intro_text}\n\n{first_question}"


def build_final_message(language: str) -> str:
    return translate_text("Screening completed. We can now calculate the diagnosis.", language)


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
    text = translate_text(
        "I could not identify a valid option for this item. Please answer using one of the available options or describe the clinical finding more clearly.",
        language
    )
    question_text = build_question_text(index, language)
    return f"{text}\n\n{question_text}"


def summarize_recent_context(session: dict, max_items: int = 6):
    items = []
    for field in FIELD_ORDER:
        if field in session["answers"]:
            items.append(f"{field}: {label_for_code(session['answers'][field])}")
    return "; ".join(items[:max_items])


def extract_answers_with_llm(user_text: str, session: dict):
    """
    Usa a API para extrair múltiplos campos da mensagem do usuário.
    A decisão final continua estruturada em códigos canônicos.
    """
    remaining_fields = [field for field in FIELD_ORDER if field not in session["answers"]]

    if not remaining_fields:
        return {}

    options_by_field = {}
    for field in remaining_fields:
        options_by_field[field] = []
        for code in FIELD_TO_CODES[field]:
            meta = OPTION_CATALOG[code]
            options_by_field[field].append({
                "code": code,
                "label": meta["label"],
                "description": meta["description"],
                "aliases": meta["aliases"][:12],  # reduz prompt
            })

    clinical_context = summarize_recent_context(session)

    prompt = f"""
You are extracting structured endodontic triage information from a clinician's message.

Return ONLY a valid JSON object with this structure:
{{
  "answers": {{
    "PAIN": {{"code": "...", "evidence": "...", "confidence": 0.0}},
    "ONSET": {{"code": "...", "evidence": "...", "confidence": 0.0}},
    "PULP VITALITY": {{"code": "...", "evidence": "...", "confidence": 0.0}},
    "PERCUSSION": {{"code": "...", "evidence": "...", "confidence": 0.0}},
    "PALPATION": {{"code": "...", "evidence": "...", "confidence": 0.0}},
    "RADIOGRAPHY": {{"code": "...", "evidence": "...", "confidence": 0.0}}
  }}
}}

Rules:
- Only fill fields that are explicitly or strongly implied in the message.
- Use null for fields not supported by the message.
- Never invent findings.
- Respect the allowed codes for each field.
- If pain is clearly absent, ONSET may be set to "onset_na".
- Confidence must be between 0 and 1.

Already known clinical context:
{clinical_context if clinical_context else "None"}

Allowed options by field:
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
            model="gpt-4o-mini",
            temperature=0,
            response_format={"type": "json_object"}
        )
        raw = response.choices[0].message.content or "{}"
        data = safe_json_loads(raw) or {}
        extracted = {}

        for field, payload in (data.get("answers") or {}).items():
            if field not in remaining_fields:
                continue
            if not payload:
                continue
            code = payload.get("code")
            confidence = payload.get("confidence", 0)
            if code in FIELD_TO_CODES[field] and isinstance(confidence, (int, float)) and confidence >= 0.60:
                extracted[field] = code

        return extracted
    except Exception:
        return {}


def extract_answers_fallback(user_text: str, session: dict):
    """
    Fallback local por alias, inclusive tentando identificar mais de um campo na mesma mensagem.
    """
    extracted = {}
    remaining_fields = [field for field in FIELD_ORDER if field not in session["answers"]]

    for field in remaining_fields:
        code = canonicalize_value(field, user_text)
        if code:
            extracted[field] = code

    # Regra segura: se sem dor, onset = N/A
    if extracted.get("PAIN") == "pain_absent":
        extracted["ONSET"] = "onset_na"

    return extracted


def merge_extracted_answers(session: dict, extracted: dict):
    for field, code in extracted.items():
        if field in FIELD_ORDER and code in FIELD_TO_CODES[field]:
            session["answers"][field] = code
    sync_current_question(session)


def find_diagnosis_row(answers: dict):
    """
    Busca a linha na planilha com base nos códigos canônicos, e não no texto literal.
    """
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
        return {"pergunta": texto, "mensagem": texto}

    current_index = session["current_question"]

    if current_index < len(QUESTION_DEFS):
        return {"pergunta": build_question_text(current_index, language)}

    return {"mensagem": build_final_message(language)}


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

    # Entrada inicial: saudação
    if session["stage"] == "greeting":
        session["stage"] = "triage"
        session["current_question"] = 0
        intro_and_question = build_intro_and_first_question(language)
        return {
            "campo": "__FLOW__",
            "resposta_interpretada": "START_SCREENING",
            "mensagem": intro_and_question,
            "pergunta": intro_and_question
        }

    sync_current_question(session)

    # Se já terminou
    if session["stage"] == "completed":
        final_message = build_final_message(language)
        return {
            "campo": "__FLOW__",
            "resposta_interpretada": "READY_FOR_DIAGNOSIS",
            "mensagem": final_message
        }

    current_index = session["current_question"]
    current_field = QUESTION_DEFS[current_index]["field"]

    # 1) tenta extrair vários campos com LLM
    extracted = extract_answers_with_llm(user_text, session)

    # 2) fallback local se necessário
    if not extracted:
        extracted = extract_answers_fallback(user_text, session)

    # 3) se ainda não capturou nada, tenta ao menos o campo atual
    if current_field not in extracted:
        direct_current = canonicalize_value(current_field, user_text)
        if direct_current:
            extracted[current_field] = direct_current

    if not extracted:
        invalid_message = build_invalid_answer_message(current_index, language)
        return {
            "campo": "__FLOW__",
            "resposta_interpretada": "REASK_CURRENT",
            "mensagem": invalid_message,
            "pergunta": invalid_message
        }

    # Aplica respostas extraídas
    merge_extracted_answers(session, extracted)

    # Campo principal interpretado = o campo atual, se foi preenchido;
    # senão, usa o primeiro campo capturado
    primary_field = current_field if current_field in extracted else list(extracted.keys())[0]
    primary_code = extracted[primary_field]

    if session["stage"] == "completed":
        final_message = build_final_message(language)
        return {
            "campo": primary_field,
            "resposta_interpretada": label_for_code(primary_code),
            "mensagem": final_message,
            "captured_fields": {
                field: label_for_code(code) for field, code in extracted.items()
            }
        }

    next_index = session["current_question"]
    next_question = build_question_text(next_index, language)

    return {
        "campo": primary_field,
        "resposta_interpretada": label_for_code(primary_code),
        "mensagem": next_question,
        "pergunta": next_question,
        "captured_fields": {
            field: label_for_code(code) for field, code in extracted.items()
        }
    }


# =========================
# CONFIRMAR
# Compatibilidade com frontend antigo
# =========================
@app.post("/confirmar/")
async def confirmar(indice: int = Form(...), resposta_interpretada: str = Form(...), session_id: str = Form(...)):
    create_session_if_needed(session_id)
    session = sessions[session_id]
    language = session["language"] or "English"

    interpreted = (resposta_interpretada or "").strip()
    sync_current_question(session)

    if interpreted in {"START_SCREENING", "REASK_CURRENT"}:
        current_index = session["current_question"]
        if current_index == 0 and session["stage"] != "completed":
            texto = build_intro_and_first_question(language)
        elif current_index < len(QUESTION_DEFS):
            texto = build_question_text(current_index, language)
        else:
            texto = build_final_message(language)
        return {"mensagem": texto, "pergunta": texto}

    if interpreted == "READY_FOR_DIAGNOSIS":
        return {"mensagem": build_final_message(language)}

    current_index = session["current_question"]
    if current_index < len(QUESTION_DEFS):
        texto = build_question_text(current_index, language)
        return {"mensagem": texto, "pergunta": texto}

    return {"mensagem": build_final_message(language)}


# =========================
# DIAGNOSTICO
# =========================
@app.post("/diagnostico/")
async def diagnostico(session_id: str = Form(...)):
    create_session_if_needed(session_id)
    session = sessions[session_id]
    language = session["language"] or "English"

    sync_current_question(session)

    required_fields = FIELD_ORDER
    missing_fields = [field for field in required_fields if field not in session["answers"]]

    if missing_fields:
        return JSONResponse(
            status_code=400,
            content={
                "mensagem": build_incomplete_message(language),
                "missing_fields": missing_fields
            }
        )

    row = find_diagnosis_row(session["answers"])

    if row is None:
        return {"mensagem": build_inconsistent_message(language)}

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
        "diagnosis_aae_2009_2013": diagnosis_aae_2009_2013,
        "diagnosis_aae_ese_2025": diagnosis_aae_ese_2025,
        "complementary_diagnosis": complementary_diagnosis,
        "diagnostico": diagnosis_aae_2009_2013,
        "diagnostico_complementar": complementary_diagnosis,
        "answers_interpreted": {
            field: label_for_code(session["answers"].get(field)) for field in FIELD_ORDER
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
            model="gpt-4o",
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
    sessions[session_id] = {
        "language": None,
        "stage": "greeting",
        "current_question": 0,
        "answers": {},
        "diagnosis_result": {},
        "history": []
    }
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
        value = label_for_code(answers.get(field)) if answers.get(field) else "Not answered"
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
