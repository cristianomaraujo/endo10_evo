from fastapi import FastAPI, Form
from fastapi.responses import HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
import pandas as pd
from openai import OpenAI
import os

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
    {"campo": "PALPAÇÃO", "pergunta": "Durante a palpação, o que foi observado no dente?", "opcoes": ["Sensível", "Edema", "Fístula", "Normal"]},
    {"campo": "RADIOGRAFIA", "pergunta": "O que a radiografia mostra?", "opcoes": ["Normal", "Espessamento", "Difusa", "Circunscrita", "Radiopaca difusa"]}
]

@app.get("/", response_class=HTMLResponse)
async def root():
    return "<h1>API do Chatbot Endo10 EVO funcionando!</h1>"

@app.post("/perguntar/")
async def perguntar(indice: int = Form(...)):
    if indice < len(perguntas):
        return perguntas[indice]
    else:
        return {"mensagem": "Triagem finalizada. Vamos calcular seu diagnóstico."}

@app.post("/responder/")
async def responder(indice: int = Form(...), resposta_usuario: str = Form(...)):
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
    return {
        "campo": pergunta_info["campo"],
        "resposta_interpretada": resposta_interpretada
    }

@app.post("/diagnostico/")
async def diagnostico(respostas: dict):
    filtro = (
        (df["DOR"] == respostas["DOR"]) &
        (df["APARECIMENTO"] == respostas["APARECIMENTO"]) &
        (df["VITALIDADE PULPAR"] == respostas["VITALIDADE PULPAR"]) &
        (df["PERCUSSÃO"] == respostas["PERCUSSÃO"]) &
        (df["PALPAÇÃO"] == respostas["PALPAÇÃO"]) &
        (df["RADIOGRAFIA"] == respostas["RADIOGRAFIA"])
    )
    resultado = df[filtro]
    if not resultado.empty:
        return {
            "diagnostico": resultado.iloc[0]["DIAGNÓSTICO"],
            "diagnostico_complementar": resultado.iloc[0]["DIAGNÓSTICO COMPLEMENTAR"]
        }
    else:
        return {"erro": "Diagnóstico não encontrado."}
