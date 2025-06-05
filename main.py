from fastapi import FastAPI, Form
import pandas as pd
import openai
import os

app = FastAPI()

# Pega a chave da variável de ambiente
openai.api_key = os.getenv("OPENAI_API_KEY")

# Carrega o banco de dados
df = pd.read_excel("planilha_endo10.xlsx", sheet_name="Pt")

perguntas = [
    {"campo": "DOR", "pergunta": "O paciente apresenta dor?", "opcoes": ["Ausente", "Presente"]},
    {"campo": "APARECIMENTO", "pergunta": "Como a dor aparece?", "opcoes": ["Não se aplica", "Espontânea", "Provocada"]},
    {"campo": "VITALIDADE PULPAR", "pergunta": "Qual é a condição da vitalidade pulpar do dente?", "opcoes": ["Normal", "Alterado", "Negativo"]},
    {"campo": "PERCUSSÃO", "pergunta": "O dente é sensível à percussão?", "opcoes": ["Não se aplica", "Sensível", "Normal"]},
    {"campo": "PALPAÇÃO", "pergunta": "Durante a palpação, o que foi observado no dente?", "opcoes": ["Sensível", "Edema", "Fístula", "Normal"]},
    {"campo": "RADIOGRAFIA", "pergunta": "O que a radiografia mostra?", "opcoes": ["Normal", "Espessamento", "Difusa", "Circunscrita", "Radiopaca difusa"]}
]

def interpretar_resposta(pergunta, resposta_usuario, opcoes):
    prompt = f"""
Você é um assistente de diagnóstico endodôntico. Mapeie a resposta do usuário para uma das opções:

Pergunta: {pergunta}
Opções: {', '.join(opcoes)}
Resposta do usuário: {resposta_usuario}

Responda apenas com a melhor opção.
"""
    response = openai.ChatCompletion.create(
        model="gpt-4o",
        messages=[{"role": "user", "content": prompt}],
        temperature=0
    )
    return response["choices"][0]["message"]["content"].strip()

def buscar_diagnostico(respostas):
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

@app.post("/perguntar/")
async def perguntar(indice: int = Form(...)):
    if indice < len(perguntas):
        return perguntas[indice]
    else:
        return {"mensagem": "Todas as perguntas foram feitas."}

@app.post("/responder/")
async def responder(indice: int = Form(...), resposta_usuario: str = Form(...)):
    pergunta_info = perguntas[indice]
    interpretado = interpretar_resposta(
        pergunta_info["pergunta"],
        resposta_usuario,
        pergunta_info["opcoes"]
    )
    return {"campo": pergunta_info["campo"], "resposta_interpretada": interpretado}

@app.post("/diagnostico/")
async def diagnostico(respostas: dict):
    return buscar_diagnostico(respostas)
