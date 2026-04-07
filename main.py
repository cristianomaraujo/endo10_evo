from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from openai import OpenAI
import pandas as pd
import os
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
        "Make sure dataset_Abr2026.xlsx is inside the project and included in the Railway deploy."
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

# Compatibilidade caso a coluna antiga exista
if "PULPT VITALITY" in df.columns and "PULP VITALITY" not in df.columns:
    df = df.rename(columns={"PULPT VITALITY": "PULP VITALITY"})

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
    text = " ".join(text.split())
    return text


def detect_language(text: str) -> str:
    try:
        prompt = (
            "Detect the language of the following text. "
            "Respond only with the language name in English, such as: "
            "English, Portuguese, Spanish, French, Italian, German, Chinese, Arabic.\n\n"
            f"Text: {text}"
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        language = response.choices[0].message.content.strip()
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
            "Keep the meaning clear, professional, and natural. "
            "Preserve line breaks and list structure.\n\n"
            f"{text}"
        )
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[{"role": "user", "content": prompt}],
            temperature=0
        )
        translated = response.choices[0].message.content.strip()
        return translated if translated else text
    except Exception:
        return text


def wrap_pdf_lines(text: str, width: int = 90):
    if not text:
        return [""]
    lines = []
    for paragraph in str(text).split("\n"):
        wrapped = textwrap.wrap(paragraph, width=width) or [""]
        lines.extend(wrapped)
    return lines


# =========================
# QUESTIONS + ALIASES
# =========================
questions = [
    {
        "field": "PAIN",
        "question": "Pain",
        "options": [
            {
                "value": "Absent",
                "description": "The patient does not report pain.",
                "aliases": [
                    "absent", "no pain", "without pain", "pain absent",
                    "ausente", "sem dor", "nao tem dor", "não tem dor",
                    "ele esta sem dor", "ele está sem dor",
                    "esta sem dor", "está sem dor",
                    "paciente sem dor", "assintomatico", "assintomático"
                ],
            },
            {
                "value": "Present",
                "description": "The patient reports pain or some type of discomfort.",
                "aliases": [
                    "present", "pain", "with pain", "has pain", "pain present",
                    "presente", "com dor", "tem dor", "dor presente",
                    "ele esta com dor", "ele está com dor",
                    "esta com dor", "está com dor",
                    "paciente com dor", "dor", "dolorido", "dolorosa"
                ],
            },
        ],
    },
    {
        "field": "ONSET",
        "question": "Onset of pain",
        "options": [
            {
                "value": "Not applicable",
                "description": "Use this when the patient does not report pain.",
                "aliases": ["not applicable", "n/a", "nao se aplica", "não se aplica"],
            },
            {
                "value": "Spontaneous",
                "description": "The pain starts spontaneously, without any provoking stimulus.",
                "aliases": ["spontaneous", "spontaneously", "espontanea", "espontânea"],
            },
            {
                "value": "Provoked",
                "description": "The pain starts after a stimulus, such as cold, heat, pressure, or sweets.",
                "aliases": ["provoked", "provocada", "provocado", "apos estimulo", "após estímulo"],
            },
        ],
    },
    {
        "field": "PULP VITALITY",
        "question": "Pulp vitality",
        "options": [
            {
                "value": "Altered",
                "description": "There is an exaggerated or persistent painful response to vitality testing.",
                "aliases": ["altered", "alterada", "alterado"],
            },
            {
                "value": "Negative",
                "description": "There is no response to vitality testing.",
                "aliases": ["negative", "negativo", "sem resposta", "no response"],
            },
            {
                "value": "Normal",
                "description": "There is a mild, transient response that disappears shortly after the stimulus is removed.",
                "aliases": ["normal"],
            },
        ],
    },
    {
        "field": "PERCUSSION",
        "question": "Percussion",
        "options": [
            {
                "value": "Not applicable",
                "description": "Use this when percussion testing is not applicable in the clinical situation.",
                "aliases": ["not applicable", "n/a", "nao se aplica", "não se aplica"],
            },
            {
                "value": "Normal",
                "description": "There is no pain or sensitivity on percussion.",
                "aliases": ["normal", "sem dor", "no pain", "not sensitive"],
            },
            {
                "value": "Sensitive",
                "description": "There is pain or sensitivity on percussion.",
                "aliases": ["sensitive", "sensivel", "sensível", "painful", "tender", "doloroso"],
            },
        ],
    },
    {
        "field": "PALPATION",
        "question": "Palpation",
        "options": [
            {
                "value": "Edema",
                "description": "There is swelling of the adjacent tissues.",
                "aliases": ["edema", "swelling", "swollen", "inchaco", "inchaço"],
            },
            {
                "value": "Fistula",
                "description": "There is a sinus tract or a mucosal/cutaneous opening communicating with the root apex.",
                "aliases": ["fistula", "fístula", "sinus tract", "trajeto fistuloso"],
            },
            {
                "value": "Normal",
                "description": "There is no pain on palpation.",
                "aliases": ["normal", "sem dor", "no pain", "not sensitive"],
            },
            {
                "value": "Sensitive",
                "description": "There is pain or sensitivity on palpation.",
                "aliases": ["sensitive", "sensivel", "sensível", "painful", "tender", "doloroso"],
            },
        ],
    },
    {
        "field": "RADIOGRAPHY",
        "question": "Radiographic finding",
        "options": [
            {
                "value": "Circumscribed",
                "description": "The lesion is well delimited, with relatively distinct borders.",
                "aliases": ["circumscribed", "circunscrita"],
            },
            {
                "value": "Diffuse",
                "description": "The lesion has poorly defined borders or a gradual transition to adjacent tissues.",
                "aliases": ["diffuse", "difusa"],
            },
            {
                "value": "Thickening",
                "description": "There is widening or thickening of the periodontal ligament space.",
                "aliases": ["thickening", "widening", "espessamento", "alargamento"],
            },
            {
                "value": "Normal",
                "description": "The lamina dura is intact and the periodontal ligament space is uniform.",
                "aliases": ["normal"],
            },
            {
                "value": "Diffuse radiopaque",
                "description": "There is a diffuse increase in radiopacity with ill-defined borders and gradual transition to adjacent bone.",
                "aliases": ["diffuse radiopaque", "radiopaca difusa", "radiopaque diffuse", "radiopaco difuso"],
            },
        ],
    },
]

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
            "answers": {},
            "diagnosis_result": {},
        }


def get_question_by_index(index: int):
    if index < 0 or index >= len(questions):
        raise HTTPException(status_code=400, detail="Invalid question index.")
    return questions[index]


def build_question_text(index: int, language: str) -> str:
    q = get_question_by_index(index)
    base_text = f"{q['question']}\n\n"
    for opt in q["options"]:
        base_text += f"{opt['value']} - {opt['description']}\n"
    return translate_text(base_text.strip(), language)


def build_intro_and_first_question(language: str) -> str:
    intro_text = """
Hello! I am Endo10 EVO, a virtual assistant developed to support diagnostic reasoning in Endodontics.

This system conducts a structured clinical screening based on signs, symptoms, and complementary examination findings. At the end of the process, a diagnostic suggestion will be presented according to the reference nomenclature adopted by the system.

The variables will be presented sequentially. At each step, provide the option that best represents the clinical findings of the case under evaluation.

We will begin with the first variable.
""".strip()

    intro_text = translate_text(intro_text, language)
    first_question = build_question_text(0, language)
    return f"{intro_text}\n\n{first_question}"


def build_final_message(language: str) -> str:
    return translate_text("Screening completed. We can now calculate the diagnosis.", language)


def build_inconsistent_message(language: str) -> str:
    return translate_text(
        "I could not find a diagnosis for this exact combination of answers. Please review the selected options.",
        language
    )


def build_incomplete_message(language: str) -> str:
    return translate_text(
        "The screening is incomplete. Please answer all questions before requesting the diagnosis.",
        language
    )


def build_invalid_answer_message(index: int, language: str) -> str:
    text = translate_text(
        "I could not identify a valid option for this item. Please answer using one of the available options.",
        language
    )
    question_text = build_question_text(index, language)
    return f"{text}\n\n{question_text}"


def find_matching_option(question_index: int, user_text: str):
    q = get_question_by_index(question_index)
    norm_user = normalize_text(user_text)

    # 1. match exato com value
    for opt in q["options"]:
        if normalize_text(opt["value"]) == norm_user:
            return opt["value"]

    # 2. match exato com aliases
    for opt in q["options"]:
        for alias in opt.get("aliases", []):
            if normalize_text(alias) == norm_user:
                return opt["value"]

    # 3. match por termo contido na resposta do usuário
    candidates = []
    for opt in q["options"]:
        for term in [opt["value"]] + opt.get("aliases", []):
            norm_term = normalize_text(term)
            if norm_term:
                candidates.append((len(norm_term), norm_term, opt["value"]))

    candidates.sort(reverse=True)

    for _, norm_term, opt_value in candidates:
        if norm_term in norm_user:
            return opt_value

    # 4. regras extras por campo
    if q["field"] == "PAIN":
        if any(term in norm_user for term in [
            "sem dor", "nao tem dor", "não tem dor", "assintomatico", "assintomático"
        ]):
            return "Absent"
        if any(term in norm_user for term in [
            "com dor", "tem dor", "dor presente", "dolorido", "dolorosa"
        ]):
            return "Present"

    return None


def advance_question_index(session: dict, matched_field: str, matched_value: str):
    # Regra clínica: se não há dor, onset = not applicable automaticamente
    if matched_field == "PAIN" and matched_value == "Absent":
        session["answers"]["ONSET"] = "Not applicable"
        session["current_question"] += 2  # pula ONSET
    else:
        session["current_question"] += 1


def find_diagnosis_row(answers: dict):
    temp_df = df.copy()

    required_cols = ["PAIN", "ONSET", "PULP VITALITY", "PERCUSSION", "PALPATION", "RADIOGRAPHY"]
    for col in required_cols:
        if col not in temp_df.columns:
            raise RuntimeError(f"Required column '{col}' not found in spreadsheet.")

    for col in required_cols:
        temp_df[f"__norm_{col}"] = temp_df[col].apply(normalize_text)

    conditions = (
        (temp_df["__norm_PAIN"] == normalize_text(answers.get("PAIN", ""))) &
        (temp_df["__norm_ONSET"] == normalize_text(answers.get("ONSET", ""))) &
        (temp_df["__norm_PULP VITALITY"] == normalize_text(answers.get("PULP VITALITY", ""))) &
        (temp_df["__norm_PERCUSSION"] == normalize_text(answers.get("PERCUSSION", ""))) &
        (temp_df["__norm_PALPATION"] == normalize_text(answers.get("PALPATION", ""))) &
        (temp_df["__norm_RADIOGRAPHY"] == normalize_text(answers.get("RADIOGRAPHY", "")))
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

    if session["stage"] == "greeting":
        texto = translate_text(
            "Hello! I am Endo10 EVO, a virtual assistant developed to support diagnostic reasoning in Endodontics.",
            language
        )
        return {"pergunta": texto, "mensagem": texto}

    current_index = session["current_question"]

    if current_index < len(questions):
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

    # Se já terminou
    if session["stage"] == "completed":
        final_message = build_final_message(language)
        return {
            "campo": "__FLOW__",
            "resposta_interpretada": "READY_FOR_DIAGNOSIS",
            "mensagem": final_message
        }

    current_index = session["current_question"]

    # Proteção extra: se PAIN = Absent e por algum motivo ficou em ONSET, pula
    if session["answers"].get("PAIN") == "Absent" and current_index == 1:
        session["answers"]["ONSET"] = "Not applicable"
        session["current_question"] = 2
        current_index = 2

    if current_index >= len(questions):
        session["stage"] = "completed"
        final_message = build_final_message(language)
        return {
            "campo": "__FLOW__",
            "resposta_interpretada": "READY_FOR_DIAGNOSIS",
            "mensagem": final_message
        }

    matched_option = find_matching_option(current_index, user_text)

    if not matched_option:
        invalid_message = build_invalid_answer_message(current_index, language)
        return {
            "campo": "__FLOW__",
            "resposta_interpretada": "REASK_CURRENT",
            "mensagem": invalid_message,
            "pergunta": invalid_message
        }

    field = questions[current_index]["field"]
    session["answers"][field] = matched_option

    advance_question_index(session, field, matched_option)

    next_index = session["current_question"]

    if next_index < len(questions):
        next_question = build_question_text(next_index, language)
        return {
            "campo": field,
            "resposta_interpretada": matched_option,
            "mensagem": next_question,
            "pergunta": next_question
        }

    session["stage"] = "completed"
    final_message = build_final_message(language)
    return {
        "campo": field,
        "resposta_interpretada": matched_option,
        "mensagem": final_message
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

    if interpreted in {"START_SCREENING", "REASK_CURRENT"}:
        current_index = session["current_question"]
        if current_index == 0:
            texto = build_intro_and_first_question(language)
        elif current_index < len(questions):
            texto = build_question_text(current_index, language)
        else:
            texto = build_final_message(language)
        return {"mensagem": texto, "pergunta": texto}

    if interpreted == "READY_FOR_DIAGNOSIS":
        return {"mensagem": build_final_message(language)}

    current_index = session["current_question"]
    if current_index < len(questions):
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

    required_fields = [q["field"] for q in questions]
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
        "diagnostico_complementar": complementary_diagnosis
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

Diagnostic result:
- Diagnosis according to AAE nomenclature 2009/2013: {diag_2009}
- Diagnosis according to AAE/ESE nomenclature 2025: {diag_2025}
- Complementary diagnosis: {comp_diag}
"""

    try:
        response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "You are an endodontics professor."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.3
        )
        explanation_text = response.choices[0].message.content.strip()
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
        "diagnosis_result": {}
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
    for q in questions:
        field = q["field"]
        value = answers.get(field, "Not answered")
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
