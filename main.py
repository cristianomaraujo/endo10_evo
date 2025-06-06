from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, StreamingResponse
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
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.mount("/static", StaticFiles(directory="static"), name="static")

client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Load Excel
df = pd.read_excel("planilha_endo10.xlsx", sheet_name="Pt")

perguntas = [
    {"campo": "DOR", "pergunta": "Does the patient have pain?", "opcoes": ["Absent", "Present"]},
    {"campo": "APARECIMENTO", "pergunta": "How does the pain appear?", "opcoes": ["Not applicable", "Spontaneous", "Provoked"]},
    {"campo": "VITALIDADE PULPAR", "pergunta": "What is the condition of the pulp vitality?", "opcoes": ["Normal", "Altered", "Negative"]},
    {"campo": "PERCUSSÃO", "pergunta": "Is the tooth sensitive to percussion?", "opcoes": ["Not applicable", "Sensitive", "Normal"]},
    {"campo": "PALPAÇÃO", "pergunta": "What was observed during palpation?", "opcoes": ["Sensitive", "Edema", "Fistula", "Normal"]},
    {"campo": "RADIOGRAFIA", "pergunta": "What does the radiograph show?", "opcoes": ["Normal", "Thickening", "Diffuse", "Circumscribed", "Diffuse radiopacity"]}
]

sessions = {}

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/set_language/")
async def set_language(session_id: str = Form(...), user_input: str = Form(...)):
    # Detect language
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Detect the language of the following text. Respond only with the language name (e.g., English, Portuguese, Spanish)."},
            {"role": "user", "content": user_input}
        ],
        temperature=0.0,
        max_tokens=5
    )
    language = response.choices[0].message.content.strip()
    sessions[session_id] = {"language": language, "respostas": {}, "awaiting_confirmation": False, "last_answer": None}
    return {"language": language}

@app.post("/perguntar/")
async def perguntar(session_id: str = Form(...), indice: int = Form(...)):
    session = sessions.get(session_id)
    language = session.get("language")

    if indice < len(perguntas):
        pergunta_ingles = perguntas[indice]["pergunta"]

        # Translate the question
        translation = translate_text(pergunta_ingles, language)

        return {"pergunta": translation}
    else:
        return {"mensagem": translate_text("Screening completed. Let's calculate your diagnosis.", language)}

@app.post("/responder/")
async def responder(session_id: str = Form(...), indice: int = Form(...), resposta_usuario: str = Form(...)):
    session = sessions.get(session_id)

    if session.get("awaiting_confirmation"):
        confirmation = resposta_usuario.lower()
        if confirmation in ["yes", "sim"]:
            session["respostas"][session["campo_atual"]] = session["last_answer"]
            session["awaiting_confirmation"] = False
            return {"confirmed": True}
        else:
            session["awaiting_confirmation"] = False
            return {"retry": True}

    pergunta_info = perguntas[indice]

    prompt = f"""
You are an endodontics assistant.
Map the user's answer to one of these options:
Options: {', '.join(pergunta_info['opcoes'])}
User answer: {resposta_usuario}
Respond with only the most appropriate option.
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an expert in dental diagnosis."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0,
        max_tokens=20
    )
    resposta_interpretada = response.choices[0].message.content.strip()

    session["campo_atual"] = pergunta_info["campo"]
    session["last_answer"] = resposta_interpretada
    session["awaiting_confirmation"] = True

    language = session.get("language")
    double_check_msg = translate_text(f"Did you mean: {resposta_interpretada}? Please confirm (Yes/No).", language)

    return {"double_check": double_check_msg}

@app.post("/diagnostico/")
async def diagnostico(session_id: str = Form(...)):
    session = sessions.get(session_id)
    respostas = session.get("respostas", {})

    filtro = (
        (df["DOR"] == respostas.get("DOR")) &
        (df["APARECIMENTO"] == respostas.get("APARECIMENTO")) &
        (df["VITALIDADE PULPAR"] == respostas.get("VITALIDADE PULPAR")) &
        (df["PERCUSSÃO"] == respostas.get("PERCUSSÃO")) &
        (df["PALPAÇÃO"] == respostas.get("PALPAÇÃO")) &
        (df["RADIOGRAFIA"] == respostas.get("RADIOGRAFIA"))
    )

    resultado = df[filtro]

    if not resultado.empty:
        return {
            "diagnostico": resultado.iloc[0]["DIAGNÓSTICO"],
            "diagnostico_complementar": resultado.iloc[0]["DIAGNÓSTICO COMPLEMENTAR"]
        }
    else:
        return {"erro": "Diagnosis not found."}

def translate_text(text, target_language):
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": f"Translate the following text into {target_language}:"},
            {"role": "user", "content": text}
        ],
        temperature=0.0,
        max_tokens=100
    )
    return response.choices[0].message.content.strip()
