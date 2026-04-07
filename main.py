from fastapi import FastAPI, Form, HTTPException
from fastapi.responses import HTMLResponse, StreamingResponse, JSONResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import pandas as pd
from openai import OpenAI
import os
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas

app = FastAPI()

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # ajuste em produção se necessário
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Pasta static
if os.path.isdir("static"):
    app.mount("/static", StaticFiles(directory="static"), name="static")

# Cliente OpenAI
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# =========================
# FUNÇÕES AUXILIARES
# =========================
def clean_text(value):
    if pd.isna(value):
        return ""
    return str(value).strip().replace("\n", " ").replace("\r", " ")

def normalize_dataframe(dataframe: pd.DataFrame) -> pd.DataFrame:
    dataframe = dataframe.copy()
    dataframe.columns = [clean_text(col) for col in dataframe.columns]

    for col in dataframe.columns:
        if dataframe[col].dtype == "object":
            dataframe[col] = dataframe[col].apply(clean_text)

    return dataframe

def get_existing_column(dataframe: pd.DataFrame, possible_names):
    for name in possible_names:
        if name in dataframe.columns:
            return name
    raise KeyError(f"None of these columns were found in the spreadsheet: {possible_names}")

def translate_text(text: str, target_language: str) -> str:
    if not text:
        return text

    if target_language.lower() == "portuguese":
        return text

    prompt = f"Translate the following text into {target_language}. Keep the meaning exact:\n\n{text}"
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content.strip()

def detect_language(text: str) -> str:
    prompt = (
        f"Detect the language of this text: {text}. "
        f"Only output the language name in English, such as: English, Portuguese, Spanish, Italian."
    )
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}]
    )
    return response.choices[0].message.content.strip()

def safe_match_option(question_text: str, options: list, user_answer: str) -> str:
    prompt = f"""
You are an endodontic assistant.

Interpret the user's answer and map it to exactly one of the possible options below.

Question: {question_text}
Possible options: {', '.join(options)}
User's answer: {user_answer}

Rules:
- Return only one exact option from the list.
- Do not explain.
- Do not create new options.
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a specialist in endodontic diagnosis."},
            {"role": "user", "content": prompt}
        ]
    )
    interpreted = response.choices[0].message.content.strip()

    if interpreted not in options:
        raise ValueError(
            f"The interpreted answer '{interpreted}' is not one of the allowed options: {options}"
        )

    return interpreted

# =========================
# CARREGAMENTO DA PLANILHA
# =========================
try:
    df = pd.read_excel("dataset_Abr2026.xlsx", sheet_name="En")
    df = normalize_dataframe(df)
except Exception as e:
    raise RuntimeError(f"Error loading spreadsheet: {e}")

# Mapeamento robusto de colunas
COL_PAIN = get_existing_column(df, ["PAIN"])
COL_ONSET = get_existing_column(df, ["ONSET"])
COL_PULP_VITALITY = get_existing_column(df, ["PULP VITALITY", "PULPT VITALITY"])
COL_PERCUSSION = get_existing_column(df, ["PERCUSSION"])
COL_PALPATION = get_existing_column(df, ["PALPATION"])
COL_RADIOGRAPHY = get_existing_column(df, ["RADIOGRAPHY"])

COL_DIAG_2009 = get_existing_column(df, ["DIAGNOSIS (AAE NOMENCLATURE 2009/2013)"])
COL_DIAG_2025 = get_existing_column(df, ["DIAGNOSIS (AAE/ESE NOMENCLATURE 2025)"])
COL_COMP_DIAG = get_existing_column(df, ["COMPLEMENTARY DIAGNOSIS"])

# Perguntas da triagem
questions = [
    {
        "field": COL_PAIN,
        "question": "Does the patient have pain?",
        "options": ["Absent", "Present"]
    },
    {
        "field": COL_ONSET,
        "question": "How does the pain start?",
        "options": ["Not applicable", "Spontaneous", "Provoked"]
    },
    {
        "field": COL_PULP_VITALITY,
        "question": "What is the pulp vitality condition of the tooth?",
        "options": ["Normal", "Altered", "Alterad", "Negative"]
    },
    {
        "field": COL_PERCUSSION,
        "question": "Is the tooth sensitive to percussion?",
        "options": ["Not applicable", "Sensitive", "Normal"]
    },
    {
        "field": COL_PALPATION,
        "question": "What was observed on palpation?",
        "options": ["Sensitive", "Sensivel", "Edema", "Fistula", "Normal"]
    },
    {
        "field": COL_RADIOGRAPHY,
        "question": "What does the radiograph show?",
        "options": [
            "Normal",
            "Thickening",
            "Thickening of the periodontal ligament",
            "Diffuse",
            "Diffuse apical radiolucency",
            "Circumscribed",
            "Circumscribed radiolucency lesion",
            "Diffuse radiopaque lesion",
            "Radiopaque diffuse"
        ]
    }
]

sessions = {}

# =========================
# ROTAS
# =========================
@app.get("/", response_class=HTMLResponse)
async def root():
    if not os.path.exists("static/index.html"):
        return HTMLResponse("<h1>API is running</h1>")
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/ask/")
async def ask(index: int = Form(...), session_id: str = Form(...)):
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required.")

    language = sessions.get(session_id, {}).get("language", "English")

    if index < len(questions):
        question_info = questions[index]
        question_text = question_info["question"]

        if language.lower() != "english":
            question_text = translate_text(question_text, language)

        return {"question": question_text}
    else:
        final_message = "Screening completed. Let's calculate the diagnosis."
        if language.lower() != "english":
            final_message = translate_text(final_message, language)
        return {"message": final_message}

@app.post("/answer/")
async def answer(index: int = Form(...), user_answer: str = Form(...), session_id: str = Form(...)):
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required.")

    if index < 0 or index >= len(questions):
        raise HTTPException(status_code=400, detail="Invalid question index.")

    if session_id not in sessions:
        detected_language = detect_language(user_answer)
        sessions[session_id] = {"language": detected_language}

    question_info = questions[index]

    try:
        interpreted_answer = safe_match_option(
            question_text=question_info["question"],
            options=question_info["options"],
            user_answer=user_answer
        )
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error interpreting answer: {e}")

    return {
        "field": question_info["field"],
        "interpreted_answer": interpreted_answer
    }

@app.post("/confirm/")
async def confirm(index: int = Form(...), interpreted_answer: str = Form(...), session_id: str = Form(...)):
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required.")

    if index < 0 or index >= len(questions):
        raise HTTPException(status_code=400, detail="Invalid question index.")

    question_info = questions[index]
    language = sessions.get(session_id, {}).get("language", "English")

    if session_id not in sessions:
        sessions[session_id] = {"language": "English"}

    sessions[session_id][question_info["field"]] = clean_text(interpreted_answer)

    confirmation_text = f"Based on your answer, may I record **{interpreted_answer}**? (Type: Yes or No)"
    if language.lower() != "english":
        confirmation_text = translate_text(confirmation_text, language)

    return {"message": confirmation_text}

@app.post("/diagnosis/")
async def diagnosis(session_id: str = Form(...)):
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required.")

    answers = sessions.get(session_id, {})
    language = answers.get("language", "English")

    pain = clean_text(answers.get(COL_PAIN, ""))
    onset = clean_text(answers.get(COL_ONSET, ""))
    pulp_vitality = clean_text(answers.get(COL_PULP_VITALITY, ""))
    percussion = clean_text(answers.get(COL_PERCUSSION, ""))
    palpation = clean_text(answers.get(COL_PALPATION, ""))
    radiography = clean_text(answers.get(COL_RADIOGRAPHY, ""))

    result = df[
        (df[COL_PAIN] == pain) &
        (df[COL_ONSET] == onset) &
        (df[COL_PULP_VITALITY] == pulp_vitality) &
        (df[COL_PERCUSSION] == percussion) &
        (df[COL_PALPATION] == palpation) &
        (df[COL_RADIOGRAPHY] == radiography)
    ]

    if result.empty:
        error_message = "I could not find a diagnosis with the provided information."
        if language.lower() != "english":
            error_message = translate_text(error_message, language)
        return {"message": error_message}

    diagnosis_aae_2009_2013 = clean_text(result.iloc[0][COL_DIAG_2009])
    diagnosis_aae_ese_2025 = clean_text(result.iloc[0][COL_DIAG_2025])
    complementary_diagnosis = clean_text(result.iloc[0][COL_COMP_DIAG])

    # Salvar na sessão para PDF e outros usos
    sessions[session_id][COL_DIAG_2009] = diagnosis_aae_2009_2013
    sessions[session_id][COL_DIAG_2025] = diagnosis_aae_ese_2025
    sessions[session_id][COL_COMP_DIAG] = complementary_diagnosis

    if language.lower() != "english":
        label_2009 = translate_text("Diagnosis (AAE nomenclature 2009/2013)", language)
        label_2025 = translate_text("Diagnosis (AAE/ESE nomenclature 2025)", language)
        label_comp = translate_text("Complementary diagnosis", language)

        value_2009 = translate_text(diagnosis_aae_2009_2013, language)
        value_2025 = translate_text(diagnosis_aae_ese_2025, language)
        value_comp = translate_text(complementary_diagnosis, language)

        return {
            "diagnosis_aae_2009_2013_label": label_2009,
            "diagnosis_aae_2009_2013": value_2009,
            "diagnosis_aae_ese_2025_label": label_2025,
            "diagnosis_aae_ese_2025": value_2025,
            "complementary_diagnosis_label": label_comp,
            "complementary_diagnosis": value_comp
        }

    return {
        "diagnosis_aae_2009_2013": diagnosis_aae_2009_2013,
        "diagnosis_aae_ese_2025": diagnosis_aae_ese_2025,
        "complementary_diagnosis": complementary_diagnosis
    }

@app.post("/explanation/")
async def explanation(
    diagnosis_aae_2009_2013: str = Form(...),
    diagnosis_aae_ese_2025: str = Form(...),
    complementary_diagnosis: str = Form(...),
    session_id: str = Form(...)
):
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required.")

    language = sessions.get(session_id, {}).get("language", "English")

    prompt = f"""
Explain clearly to a newly graduated dentist the following diagnostic result:

- Diagnosis according to AAE nomenclature 2009/2013: {diagnosis_aae_2009_2013}
- Diagnosis according to AAE/ESE nomenclature 2025: {diagnosis_aae_ese_2025}
- Complementary diagnosis: {complementary_diagnosis}

The explanation should be clear, without overly technical terms, and should present the clinical reasoning behind the diagnosis.
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an endodontics professor."},
            {"role": "user", "content": prompt}
        ]
    )
    explanation_text = response.choices[0].message.content.strip()

    if language.lower() != "english":
        explanation_text = translate_text(explanation_text, language)

    return JSONResponse(content={"explanation": explanation_text})

@app.get("/pdf/{session_id}")
async def generate_pdf(session_id: str):
    if not session_id:
        raise HTTPException(status_code=400, detail="session_id is required.")

    answers = sessions.get(session_id, {})
    if not answers:
        raise HTTPException(status_code=404, detail="Session not found.")

    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    p.setFont("Helvetica", 12)

    y = 750
    p.drawString(50, y, "Endodontic Screening Report")
    y -= 30

    ordered_fields = [
        COL_PAIN,
        COL_ONSET,
        COL_PULP_VITALITY,
        COL_PERCUSSION,
        COL_PALPATION,
        COL_RADIOGRAPHY,
        COL_DIAG_2009,
        COL_DIAG_2025,
        COL_COMP_DIAG
    ]

    for field in ordered_fields:
        value = clean_text(answers.get(field, ""))
        if value:
            line = f"{field}: {value}"

            if len(line) > 100:
                chunks = [line[i:i+100] for i in range(0, len(line), 100)]
                for chunk in chunks:
                    p.drawString(50, y, chunk)
                    y -= 18
                    if y < 50:
                        p.showPage()
                        p.setFont("Helvetica", 12)
                        y = 750
            else:
                p.drawString(50, y, line)
                y -= 18
                if y < 50:
                    p.showPage()
                    p.setFont("Helvetica", 12)
                    y = 750

    p.save()
    buffer.seek(0)

    return StreamingResponse(
        buffer,
        media_type="application/pdf",
        headers={"Content-Disposition": "attachment; filename=endodontic_screening_report.pdf"}
    )
