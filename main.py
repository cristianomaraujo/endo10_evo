# main.py atualizado

from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
from openai import OpenAI
import os
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

# Instancia o cliente OpenAI com a API KEY
client = OpenAI(api_key=os.getenv("OPENAI_API_KEY"))

# Carrega planilha
df = pd.read_excel("planilha_endo10.xlsx", sheet_name="Pt")

# Perguntas da triagem
perguntas = [
    {"campo": "DOR", "pergunta": "O paciente apresenta dor?", "opcoes": ["Ausente", "Presente"]},
    {"campo": "APARECIMENTO", "pergunta": "Como a dor aparece?", "opcoes": ["Não se aplica", "Espontânea", "Provocada"]},
    {"campo": "VITALIDADE PULPAR", "pergunta": "Qual é a condição da vitalidade pulpar do dente?", "opcoes": ["Normal", "Alterado", "Negativo"]},
    {"campo": "PERCUSSÃO", "pergunta": "O dente é sensível à percussão?", "opcoes": ["Não se aplica", "Sensível", "Normal"]},
    {"campo": "PALPAÇÃO", "pergunta": "Durante a palpção, o que foi observado no dente?", "opcoes": ["Sensível", "Edema", "Fístula", "Normal"]},
    {"campo": "RADIOGRAFIA", "pergunta": "O que a radiografia mostra?", "opcoes": ["Normal", "Espessamento", "Difusa", "Circunscrita", "Radiopaca difusa"]}
]

# Sessões em memória temporária
sessions = {}

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1>API do Chatbot Endo10 EVO funcionando!</h1>"

@app.post("/perguntar/")
async def perguntar(session_id: str = Form(...), indice: int = Form(...)):
    if session_id not in sessions:
        sessions[session_id] = {"respostas": {}, "diagnostico": {}}

    if indice < len(perguntas):
        return perguntas[indice]
    else:
        return {"mensagem": "Triagem finalizada. Vamos calcular seu diagnóstico."}

@app.post("/responder/")
async def responder(session_id: str = Form(...), indice: int = Form(...), resposta_usuario: str = Form(...)):
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

    # Salva a resposta interpretada, mas ainda não finaliza
    sessions[session_id]["respostas"][pergunta_info["campo"]] = resposta_interpretada

    return {
        "campo": pergunta_info["campo"],
        "resposta_interpretada": resposta_interpretada
    }

@app.post("/diagnostico/")
async def diagnostico(session_id: str = Form(...)):
    respostas = sessions[session_id]["respostas"]

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

        # Gera explicacao
        explicacao_prompt = f"""
Explique de forma didática para um dentista recém-formado o seguinte diagnóstico:
- Diagnóstico Principal: {diagnostico}
- Diagnóstico Complementar: {diagnostico_complementar}

A explicação deve ser clara, sem termos excessivamente técnicos, e apresentar o raciocínio clínico por trás do diagnóstico.
"""
        explicacao_response = client.chat.completions.create(
            model="gpt-4o",
            messages=[
                {"role": "system", "content": "Você é um professor de endodontia."},
                {"role": "user", "content": explicacao_prompt}
            ],
            temperature=0.5,
            max_tokens=300
        )

        explicacao = explicacao_response.choices[0].message.content.strip()

        sessions[session_id]["diagnostico"] = {
            "principal": diagnostico,
            "complementar": diagnostico_complementar,
            "explicacao": explicacao
        }

        return {
            "diagnostico": diagnostico,
            "diagnostico_complementar": diagnostico_complementar,
            "explicacao": explicacao
        }
    else:
        return {"erro": "Diagnóstico não encontrado."}

@app.get("/pdf/{session_id}")
async def gerar_pdf(session_id: str):
    respostas = sessions[session_id]["respostas"]
    diagnostico = sessions[session_id]["diagnostico"]

    file_path = f"relatorio_{session_id}.pdf"
    c = canvas.Canvas(file_path, pagesize=letter)
    width, height = letter

    c.setFont("Helvetica-Bold", 14)
    c.drawString(100, height - 50, "Relatório de Triagem Endodôntica")

    c.setFont("Helvetica", 12)
    y = height - 100
    for campo, resposta in respostas.items():
        c.drawString(100, y, f"{campo}: {resposta}")
        y -= 20

    y -= 20
    c.setFont("Helvetica-Bold", 12)
    c.drawString(100, y, "Diagnóstico:")
    y -= 20
    c.setFont("Helvetica", 12)
    c.drawString(120, y, f"Principal: {diagnostico['principal']}")
    y -= 20
    c.drawString(120, y, f"Complementar: {diagnostico['complementar']}")

    y -= 40
    c.setFont("Helvetica-Bold", 12)
    c.drawString(100, y, "Explicação:")
    y -= 20
    c.setFont("Helvetica", 12)

    text_object = c.beginText(100, y)
    for line in diagnostico['explicacao'].split("\n"):
        text_object.textLine(line)

    c.drawText(text_object)
    c.showPage()
    c.save()

    return FileResponse(file_path, media_type="application/pdf", filename=file_path)
