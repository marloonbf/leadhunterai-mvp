from flask import Flask, render_template, request, send_file
from openai import OpenAI
import os, csv, datetime, pathlib, requests, io
from bs4 import BeautifulSoup

app = Flask(__name__, template_folder="templates")

# ========= OpenAI =========
OPENAI_API_KEY = os.getenv("OPENAI_API_KEY", "").strip()
client = OpenAI(api_key=OPENAI_API_KEY) if OPENAI_API_KEY else None

# ========= CSV (histórico) =========
BASE_DIR = pathlib.Path(__file__).parent.resolve()
CSV_PATH = BASE_DIR / "historico.csv"
CSV_HEADERS = ["data_hora", "idioma", "tipo_validacao", "entrada", "urls", "resultado"]

def _garantir_csv():
    if not CSV_PATH.exists():
        with open(CSV_PATH, "w", newline="", encoding="utf-8") as f:
            csv.writer(f).writerow(CSV_HEADERS)

def salvar_historico(lang: str, tipo: str, entrada: str, urls: list[str], resultado: str):
    _garantir_csv()
    with open(CSV_PATH, "a", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow([
            datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            lang, tipo,
            (entrada[:200] + "...") if len(entrada) > 200 else entrada,
            ";".join(urls) if urls else "",
            (resultado[:2000] + "...") if len(resultado) > 2000 else resultado
        ])

# ========= HTTP fetch =========
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) LeadHunterAI/0.2"
def fetch_page(url: str, timeout: int = 12) -> dict:
    try:
        r = requests.get(url, headers={"User-Agent": UA}, timeout=timeout)
        r.raise_for_status()
        soup = BeautifulSoup(r.text, "html.parser")
        title = (soup.title.string.strip() if soup.title and soup.title.string else "")[:180]
        text = " ".join(s.strip() for s in soup.stripped_strings)
        return {"url": url, "ok": True, "title": title, "text": text[:8000]}
    except Exception as e:
        return {"url": url, "ok": False, "title": "", "text": f"ERROR: {type(e).__name__}: {e}"}

# ========= Busca automática GLOBAL (Bing Web Search API) =========
# Opcional: defina BING_API_KEY para ativar.
def buscar_links_automatico(query: str, tipo: str, max_results: int = 6) -> list[str]:
    BING_KEY = os.getenv("BING_API_KEY", "").strip()
    if not BING_KEY or not query:
        return []

    endpoint = "https://api.bing.microsoft.com/v7.0/search"
    headers = {"Ocp-Apim-Subscription-Key": BING_KEY}

    # ajustes por tipo (globais, sem geolocalização)
    if tipo == "vagas":
        # portais globais de vagas
        query = f"({query}) site:linkedin.com/jobs OR site:greenhouse.io OR site:lever.co OR site:workable.com OR site:boards.greenhouse.io"
    elif tipo == "crescimento":
        query = f"({query}) (funding OR investment OR 'series a' OR 'opens new office' OR expansion OR 'hiring surge')"
    elif tipo == "produto":
        query = f"({query}) site:amazon.com OR site:ebay.com OR site:alibaba.com OR site:walmart.com"
    elif tipo == "concorrente":
        query = f"({query}) (press release OR launch OR campaign OR announcement OR roadmap OR changelog)"

    params = {"q": query, "count": max_results, "mkt": "en-US"}  # mkt genérico/global
    try:
        resp = requests.get(endpoint, headers=headers, params=params, timeout=10)
        resp.raise_for_status()
        data = resp.json()
        return [item["url"] for item in data.get("webPages", {}).get("value", [])]
    except Exception as e:
        print("Busca automática falhou:", e)
        return []

# ========= Fallback (sem IA) =========
KEYS = {
    "vagas": ["job", "jobs", "careers", "we are hiring", "hiring", "open roles", "apply", "lever", "greenhouse", "workable", "gupy"],
    "crescimento": ["funding", "series a", "series b", "investment", "expansion", "opens new office", "growth", "hiring surge"],
    "produto": ["buy", "price", "add to cart", "in stock", "sold by", "shipping", "marketplace"],
    "concorrente": ["launch", "release", "campaign", "announcement", "changelog", "new version"],
}
def fallback_avaliar(tipo: str, evidencias: list[dict], entrada: str, lang: str) -> list[dict]:
    rows = []
    keys = KEYS.get(tipo, [])
    for ev in evidencias:
        if not ev["ok"]:
            rows.append({"url": ev["url"], "encontrado": "N/A", "evidencia": ev["text"][:180], "obs": "Falha ao acessar"})
            continue
        txt = ev["text"].lower()
        found = any(k in txt for k in keys) if keys else ("Sim" if entrada else "Talvez")
        rows.append({
            "url": ev["url"],
            "encontrado": "Sim" if found else "Não",
            "evidencia": (ev["title"] or ev["text"][:140]),
            "obs": "Heurística local (demo)"
        })
    return rows

# ========= Prompt de análise (IA) =========
def prompt_for(lang: str, tipo: str, entrada: str, evidencias: list[dict]) -> str:
    lang_map = {"pt":"Português", "en":"English", "es":"Español", "fr":"Français"}
    idioma = lang_map.get(lang, "Português")
    objetivo = {
        "vagas": "Dizer se há vagas abertas (Sim/Não) e citar evidência curta.",
        "crescimento": "Apontar sinais de crescimento (funding, headcount, expansão, novas vagas) com evidência.",
        "produto": "Verificar se a marca/produto está à venda (Sim/Não), onde, e citar evidência.",
        "concorrente": "Checar atividade do concorrente (lançamentos, campanhas, anúncios) com evidência.",
        "livre": "Executar a validação solicitada com evidências."
    }.get(tipo, "Executar a validação solicitada com evidências.")

    blocos = []
    for ev in evidencias:
        blocos.append(
            f"- URL: {ev['url']}\n  TITLE: {ev.get('title','')}\n  TEXT: {ev.get('text','')[:1200]}"
        )
    evid = "\n\n".join(blocos) if blocos else "Sem URLs fornecidas."

    return f"""
Responda em {idioma}.

Você é um analista de validação. Objetivo: {objetivo}
Contexto:
{entrada}

Evidências por URL:
{evid}

Entregue TABELA com colunas:
URL | Encontrado (Sim/Não/N/A) | Evidência (curta) | Observações (próximo passo)
Se não houver URLs, entregue análise estruturada com passos práticos.
Se a evidência for fraca, diga como reforçar.
"""

def chamar_openai(lang: str, tipo: str, entrada: str, evidencias: list[dict]):
    # Se não houver chave, avisa o caller para usar fallback
    if not client:
        return None

    prompt = prompt_for(lang, tipo, entrada, evidencias)
    try:
        resp = client.chat.completions.create(
            model="gpt-3.5-turbo",
            messages=[
                {"role": "system", "content": "Seja objetivo, baseado em evidências, e útil para decisões comerciais."},
                {"role": "user", "content": prompt},
            ],
            temperature=0.2,
        )
        content = resp.choices[0].message.content.strip()

        # tentar extrair linhas da tabela "URL | Encontrado | Evidência | Observações"
        linhas = []
        for line in content.splitlines():
            if "http" in line and "|" in line:
                parts = [p.strip() for p in line.split("|")]
                if len(parts) >= 4:
                    linhas.append({
                        "url": parts[0],
                        "encontrado": parts[1],
                        "evidencia": parts[2],
                        "obs": parts[3],
                    })
        return linhas if linhas else content
    except Exception as e:
        print("OpenAI erro:", e)
        return None

# ========= Rotas =========
@app.route("/", methods=["GET", "POST"])
def home():
    linhas, resultado, erro = None, None, None

    if request.method == "POST":
        lang = request.form.get("lang", "pt")
        tipo = request.form.get("tipo", "livre")
        entrada = request.form.get("entrada", "").strip()

        # URLs manuais
        urls_text = request.form.get("urls", "").strip()
        urls = [u.strip() for u in urls_text.splitlines() if u.strip()]

        # CSV opcional
        if "csv_urls" in request.files and request.files["csv_urls"]:
            try:
                file = request.files["csv_urls"]
                data = file.read().decode("utf-8", errors="ignore")
                reader = csv.DictReader(io.StringIO(data))
                for row in reader:
                    u = (row.get("url") or "").strip()
                    if u:
                        urls.append(u)
            except Exception as e:
                erro = f"Falha ao ler CSV: {e}"

        # Busca automática global (opcional)
        auto_query = request.form.get("auto_search", "").strip()
        if auto_query:
            urls.extend(buscar_links_automatico(auto_query, tipo))

        # Deduplicar e limitar
        urls = list(dict.fromkeys(urls))[:12]

        # Coletar páginas
        evidencias = [fetch_page(u) for u in urls] if urls else []

        # IA se disponível, senão fallback
        saida = chamar_openai(lang, tipo, entrada, evidencias)
        if isinstance(saida, list):
            linhas = saida
            salvar_historico(lang, tipo, entrada, urls, "\n".join([str(r) for r in linhas]))
        elif isinstance(saida, str):
            resultado = saida
            salvar_historico(lang, tipo, entrada, urls, resultado)
        else:
            # fallback simples
            if evidencias:
                linhas = fallback_avaliar(tipo, evidencias, entrada, lang)
                salvar_historico(lang, tipo, entrada, urls, "\n".join([str(r) for r in linhas]))
            else:
                resultado = ("Modo demonstração: sem IA e sem URLs para validar.\n"
                             "Forneça URLs, use CSV, ou ative a busca automática.")
                salvar_historico(lang, tipo, entrada, urls, resultado)

    return render_template("index.html", linhas=linhas, resultado=resultado, erro=erro)

@app.route("/download")
def download():
    _garantir_csv()
    return send_file(CSV_PATH, as_attachment=True, download_name="historico_leadhunterai.csv")

if __name__ == "__main__":
    print("Servidor iniciado… http://127.0.0.1:5000/")
    if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)


