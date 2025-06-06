from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
import pandas as pd
import openai
import os
from io import BytesIO
from reportlab.lib.pagesizes import letter
from reportlab.pdfgen import canvas
from langdetect import detect
from googletrans import Translator

app = FastAPI()

# CORS Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Montar a pasta static
app.mount("/static", StaticFiles(directory="static"), name="static")

# OpenAI API Key
openai.api_key = os.getenv("OPENAI_API_KEY")

# Carrega planilha
df = pd.read_excel("planilha_endo10.xlsx", sheet_name="Pt")

# Perguntas da triagem
perguntas = [
    {"campo": "DOR", "pergunta": "Does the patient have pain?", "opcoes": ["Absent", "Present"]},
    {"campo": "APARECIMENTO", "pergunta": "How does the pain appear?", "opcoes": ["Not applicable", "Spontaneous", "Provoked"]},
    {"campo": "VITALIDADE PULPAR", "pergunta": "What is the condition of the pulp vitality?", "opcoes": ["Normal", "Altered", "Negative"]},
    {"campo": "PERCUSSÃO", "pergunta": "Is the tooth sensitive to percussion?", "opcoes": ["Not applicable", "Sensitive", "Normal"]},
    {"campo": "PALPAÇÃO", "pergunta": "What was observed during palpation?", "opcoes": ["Sensitive", "Edema", "Fistula", "Normal"]},
    {"campo": "RADIOGRAFIA", "pergunta": "What does the radiograph show?", "opcoes": ["Normal", "Thickening", "Diffuse", "Circumscribed", "Diffuse radiopaque"]}
]

sessions = {}
translator = Translator()

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/perguntar/")
async def perguntar(indice: int = Form(...), session_id: str = Form(...)):
    if session_id not in sessions:
        return {"mensagem": "Sorry, I didn't understand. Please say Hello (Olá, Ciao, Hallo, etc.) in your preferred language."}
    language = sessions[session_id]["language"]
    if indice < len(perguntas):
        pergunta_original = perguntas[indice]["pergunta"]
        pergunta_traduzida = translator.translate(pergunta_original, dest=language).text
        return {"pergunta": pergunta_traduzida}
    else:
        final_message = "Triage completed. Let's check your diagnosis."
        final_translated = translator.translate(final_message, dest=language).text
        return {"mensagem": final_translated}

@app.post("/responder/")
async def responder(indice: int = Form(...), resposta_usuario: str = Form(...), session_id: str = Form(...)):
    # Se for o primeiro input, detectar o idioma
    if session_id not in sessions:
        try:
            lang_detected = detect(resposta_usuario)
        except:
            lang_detected = 'en'
        sessions[session_id] = {"language": lang_detected, "answers": {}}
        welcome_message = "Language detected. Let's start the triage!"
        welcome_translated = translator.translate(welcome_message, dest=lang_detected).text
        return {"campo": "language", "resposta_interpretada": welcome_translated}

    language = sessions[session_id]["language"]
    pergunta_info = perguntas[indice]

    # Tradução da resposta do usuário para inglês para o modelo
    resposta_traduzida_en = translator.translate(resposta_usuario, dest='en').text

    # Monta o prompt para interpretar
    prompt = f"""
You are an assistant specialized in endodontic diagnosis.

Your task is to interpret the user's answer and map it to one of the possible options.

Question: {pergunta_info['pergunta']}
Possible options: {', '.join(pergunta_info['opcoes'])}
User's answer: {resposta_traduzida_en}

Respond only with the most appropriate option from the list.
"""

    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are an expert in endodontic diagnosis."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0,
        max_tokens=50
    )
    resposta_interpretada = response['choices'][0]['message']['content'].strip()

    # Double check
    double_check_message = f"You said: '{resposta_interpretada}'. Is this correct? (Yes/No)"
    double_check_translated = translator.translate(double_check_message, dest=language).text

    # Guarda as respostas por sessão
    sessions[session_id]["answers"][pergunta_info["campo"]] = resposta_interpretada

    return {
        "campo": pergunta_info["campo"],
        "resposta_interpretada": double_check_translated
    }

@app.post("/diagnostico/")
async def diagnostico(session_id: str = Form(...)):
    respostas = sessions.get(session_id, {}).get("answers", {})
    language = sessions[session_id]["language"]

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
        diagnostico = resultado.iloc[0]["DIAGNÓSTICO"]
        explicacao = resultado.iloc[0]["DIAGNÓSTICO COMPLEMENTAR"]

        # Traduzir para o idioma do usuário
        diagnostico_traduzido = translator.translate(f"Diagnosis: {diagnostico}", dest=language).text
        explicacao_traduzida = translator.translate(f"Explanation: {explicacao}", dest=language).text

        return {
            "diagnostico": diagnostico_traduzido,
            "diagnostico_complementar": explicacao_traduzida
        }
    else:
        not_found = translator.translate("No diagnosis found with the provided information.", dest=language).text
        return {"erro": not_found}

@app.get("/pdf/{session_id}")
async def gerar_pdf(session_id: str):
    respostas = sessions.get(session_id, {}).get("answers", {})
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    p.setFont("Helvetica", 12)

    y = 750
    p.drawString(100, y, "Endodontic Triage Report - Endo10 EVO")
    y -= 40

    for campo, resposta in respostas.items():
        p.drawString(100, y, f"{campo}: {resposta}")
        y -= 20

    p.save()
    buffer.seek(0)

    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": "attachment;filename=triage_report_endo10evo.pdf"})
