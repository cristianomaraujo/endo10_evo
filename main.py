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

# Instancia o cliente OpenAI com a API KEY
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))
translator = Translator()

# Carrega planilha
df = pd.read_excel("planilha_endo10.xlsx", sheet_name="Pt")

# Perguntas da triagem
perguntas = [
    {"campo": "DOR", "pergunta": "O paciente apresenta dor?", "opcoes": ["Ausente", "Presente"]},
    {"campo": "APARECIMENTO", "pergunta": "Como a dor aparece?", "opcoes": ["Não se aplica", "Espontânea", "Provocada"]},
    {"campo": "VITALIDADE PULPAR", "pergunta": "Qual é a condição da vitalidade pulpar do dente?", "opcoes": ["Normal", "Alterado", "Negativo"]},
    {"campo": "PERCUSSÃO", "pergunta": "O dente é sensível à percussão?", "opcoes": ["Não se aplica", "Sensível", "Normal"]},
    {"campo": "PALPAÇÃO", "pergunta": "Durante a palpação, o que foi observado no dente?", "opcoes": ["Sensível", "Edema", "Fístula", "Normal"]},
    {"campo": "RADIOGRAFIA", "pergunta": "O que a radiografia mostra?", "opcoes": ["Normal", "Espessamento", "Difusa", "Circunscrita", "Radiopaca difusa"]}
]

# Armazenamento de sessão
sessions = {}

# Saudações conhecidas para mapeamento manual
saudacoes = {
    "oi": "pt",
    "olá": "pt",
    "hello": "en",
    "hola": "es",
    "hallo": "de",
    "ciao": "it",
    "salut": "fr",
    "こんにちは": "ja",
    "안녕하세요": "ko",
    "مرحبا": "ar"
}

def traduzir_texto(texto, destino):
    traducao = translator.translate(texto, dest=destino)
    return traducao.text

@app.get("/", response_class=HTMLResponse)
async def root():
    with open("static/index.html", "r", encoding="utf-8") as f:
        return f.read()

@app.post("/responder/idioma/")
async def responder_idioma(mensagem_usuario: str = Form(...), session_id: str = Form(...)):
    if len(mensagem_usuario.strip()) <= 6:
        idioma_detectado = saudacoes.get(mensagem_usuario.strip().lower())
    else:
        try:
            idioma_detectado = detect(mensagem_usuario)
        except:
            idioma_detectado = None

    if idioma_detectado:
        sessions[session_id] = {"idioma": idioma_detectado}
        return {"mensagem": traduzir_texto("Ótimo! Vamos começar a triagem!", idioma_detectado)}
    else:
        return {"mensagem": "Sorry, I didn't understand. Please say Hello (Olá, Ciao, Hallo, etc.) in your preferred language."}

@app.post("/perguntar/")
async def perguntar(indice: int = Form(...), session_id: str = Form(...)):
    idioma = sessions.get(session_id, {}).get("idioma", "en")
    if indice < len(perguntas):
        pergunta_traduzida = traduzir_texto(perguntas[indice]['pergunta'], idioma)
        return {"pergunta": pergunta_traduzida}
    else:
        return {"mensagem": traduzir_texto("Triagem finalizada. Vamos calcular seu diagnóstico.", idioma)}

@app.post("/responder/")
async def responder(indice: int = Form(...), resposta_usuario: str = Form(...), session_id: str = Form(...)):
    pergunta_info = perguntas[indice]
    prompt = f"""
Você é um assistente de endodontia.

Sua tarefa é interpretar a resposta do usuário e mapear para uma das opções possíveis.

Pergunta: {pergunta_info['pergunta']}
Opções possíveis: {', '.join(pergunta_info['opcoes'])}
Resposta do usuário: {resposta_usuario}

Responda apenas com a opção mais adequada da lista.
"""
    response = client.chat.completions.create(
        model="gpt-4o",
        messages=[
            {"role": "system", "content": "Você é um especialista em diagnóstico endodôntico."},
            {"role": "user", "content": prompt}
        ],
        temperature=0.0,
        max_tokens=50
    )
    resposta_interpretada = response.choices[0].message.content.strip()

    if session_id not in sessions:
        sessions[session_id] = {}
    sessions[session_id][pergunta_info["campo"]] = resposta_interpretada

    idioma = sessions[session_id].get("idioma", "en")
    return {
        "campo": pergunta_info["campo"],
        "resposta_interpretada": traduzir_texto(resposta_interpretada, idioma)
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
        return {
            "diagnostico": traduzir_texto(diagnostico, idioma),
            "diagnostico_complementar": traduzir_texto(diagnostico_complementar, idioma)
        }
    else:
        return {"erro": traduzir_texto("Não consegui encontrar um diagnóstico com essas informações.", idioma)}

@app.get("/pdf/{session_id}")
async def gerar_pdf(session_id: str):
    respostas = sessions.get(session_id, {})
    buffer = BytesIO()
    p = canvas.Canvas(buffer, pagesize=letter)
    p.setFont("Helvetica", 12)

    y = 750
    p.drawString(100, y, "Relatório da Triagem Endodôntica - Endo10 EVO")
    y -= 40

    for campo, resposta in respostas.items():
        if campo != "idioma":
            p.drawString(100, y, f"{campo}: {resposta}")
            y -= 20

    p.save()
    buffer.seek(0)

    return StreamingResponse(buffer, media_type="application/pdf", headers={"Content-Disposition": "attachment;filename=relatorio_triagem_endo10evo.pdf"})
