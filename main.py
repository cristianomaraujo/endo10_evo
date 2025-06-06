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
import httpx

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

# Instancia o cliente OpenAI com a API KEY
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Carrega planilha
df = pd.read_excel("planilha_endo10.xlsx", sheet_name="Pt")

# Perguntas da triagem
perguntas = [
    {"campo": "DOR", "pergunta": "Does the patient have pain?", "opcoes": ["Absent", "Present"]},
    {"campo": "APARECIMENTO", "pergunta": "How does the pain appear?", "opcoes": ["Not applicable", "Spontaneous", "Provoked"]},
    {"campo": "VITALIDADE PULPAR", "pergunta": "What is the condition of the pulp vitality?", "opcoes": ["Normal", "Altered", "Negative"]},
    {"campo": "PERCUSSÃO", "pergunta": "Is the tooth sensitive to percussion?", "opcoes": ["Not applicable", "Sensitive", "Normal"]},
    {"campo": "PALPAÇÃO", "pergunta": "What was observed during palpation?", "opcoes": ["Sensitive", "Swelling", "Fistula", "Normal"]},
    {"campo": "RADIOGRAFIA", "pergunta": "What does the radiograph show?", "opcoes": ["Normal", "Thickening", "Diffuse", "Circumscribed", "Radiopaque diffuse"]},
]

sessions = {}

async def detectar_idioma(texto):
    url = "https://libretranslate.de/detect"
    payload = {"q": texto}
    async with httpx.AsyncClient() as client:
        response = await client.post(url, data=payload)
        detections = response.json()
        return detections[0]['language'] if detections else 'en'

async def traduzir(texto, target_lang):
    url = "https://libretranslate.de/translate"
    payload = {"q": texto, "source": "en", "target": target_lang}
    async with httpx.AsyncClient() as client:
        response = await client.post(url, data=payload)
        translated = response.json()
        return translated['translatedText'] if translated else texto

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/perguntar/")
async def perguntar(indice: int = Form(...), session_id: str = Form(...)):
    session = sessions.get(session_id, {})

    # Se idioma não detectado ainda
    if "idioma" not in session:
        return {"mensagem": "Please say hi in your preferred language."}

    if indice < len(perguntas):
        pergunta_en = perguntas[indice]["pergunta"]
        pergunta_traduzida = await traduzir(pergunta_en, session["idioma"])
        return {"pergunta": pergunta_traduzida}
    else:
        return {"mensagem": "Triagem finalizada. Vamos calcular seu diagnóstico."}

@app.post("/responder/")
async def responder(indice: int = Form(...), resposta_usuario: str = Form(...), session_id: str = Form(...)):
    if session_id not in sessions:
        sessions[session_id] = {}

    session = sessions[session_id]

    # Se idioma ainda não foi detectado
    if "idioma" not in session:
        idioma_detectado = await detectar_idioma(resposta_usuario)
        session["idioma"] = idioma_detectado
        return {"campo": "idioma_detectado", "resposta_interpretada": idioma_detectado}

    pergunta_info = perguntas[indice]

    prompt = f"""
You are an endodontic diagnosis assistant.

Your task is to map the user's answer to one of the possible options.

Question: {pergunta_info['pergunta']}
Possible options: {', '.join(pergunta_info['opcoes'])}
User's answer: {resposta_usuario}

Reply only with the most appropriate option from the list.
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "You are a specialist in endodontic diagnosis."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0,
        max_tokens=50
    )
    resposta_interpretada = response.choices[0].message.content.strip()

    # Armazena a resposta
    session[pergunta_info["campo"]] = resposta_interpretada

    return {
        "campo": pergunta_info["campo"],
        "resposta_interpretada": resposta_interpretada
    }

@app.post("/diagnostico/")
async def diagnostico(session_id: str = Form(...)):
    respostas = sessions.get(session_id, {})
    idioma = respostas.get("idioma", "en")

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
        diagnostico_complementar = resultado.iloc[0]["DIAGNÓSTICO COMPLEMENTAR"]

        explicacao = f"The diagnosis \"{diagnostico}\" refers to {diagnostico_complementar}."
        explicacao_traduzida = await traduzir(explicacao, idioma)

        return {
            "diagnostico": diagnostico,
            "diagnostico_complementar": diagnostico_complementar,
            "explicacao": explicacao_traduzida
        }
    else:
        return {"erro": "Diagnosis not found."}

@app.get("/pdf/{session_id}")
async def gerar_pdf(session_id: str):
    respostas = sessions.get(session_id, {})
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    p.setFont("Helvetica", 12)

    y = 750
    p.drawString(100, y, "Endodontic Triage Report - Endo10 EVO")
    y -= 40

    for campo, resposta in respostas.items():
        if campo != "idioma":
            p.drawString(100, y, f"{campo}: {resposta}")
            y -= 20

    p.save()
    buffer.seek(0)

    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": "attachment;filename=triage_report_endo10evo.pdf"})
