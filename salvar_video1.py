"""
salvar_video.py
Transforma vídeos do YouTube em notas Markdown dentro do seu vault do Obsidian.

Fluxo para cada vídeo:
  1. tenta baixar a legenda (manual ou automática) -> rápido
  2. se não houver legenda, baixa só o áudio e transcreve com faster-whisper (offline)
  3. (opcional) gera um resumo usando o servidor local do LM Studio

Uso:
  python salvar_video.py URL1 URL2
  python salvar_video.py --lista links.txt
  python salvar_video.py URL --resumir
  python salvar_video.py URL --cookies chrome      (se o YouTube pedir "confirme que não é um robô")
"""

import argparse
import json
import re
import tempfile
import urllib.request
from datetime import date
from pathlib import Path

import yt_dlp

# ----------------------------------------------------------------------------
# CONFIGURAÇÃO: troque pelo caminho do SEU vault do Obsidian
# ----------------------------------------------------------------------------
COFRE = Path(r"C:\Users\User\Documents\Obsidian\主要")
PASTA_NOTAS = "YouTube"            # subpasta dentro do vault
MODELO_WHISPER = "small"           # tiny, base, small, medium
LMSTUDIO_URL = "http://localhost:1234/v1"
TAMANHO_BLOCO = 8000               # caracteres por bloco na hora de resumir

_whisper = None


def limpar_nome(texto: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", texto).strip()[:120]


def vtt_para_texto(caminho: Path) -> str:
    """Converte .vtt em texto corrido, removendo tempos, tags e repetições."""
    linhas, ultima = [], ""
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or "-->" in linha or linha.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        linha = re.sub(r"<[^>]+>", "", linha).strip()
        if linha and linha != ultima:  # legendas automáticas repetem linhas
            linhas.append(linha)
            ultima = linha
    paragrafos = [" ".join(linhas[i:i + 6]) for i in range(0, len(linhas), 6)]
    return "\n\n".join(paragrafos)


def opcoes_base(cookies):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    return opts


def escolher_idioma(info):
    """Escolhe UMA legenda: manual pt/en, senão a automática no idioma original."""
    manuais = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for prefixo in ("pt", "en"):
        for lang in manuais:
            if lang.startswith(prefixo):
                return lang, False
    for lang in auto:
        if lang.endswith("-orig"):
            return lang, True
    idioma = info.get("language")
    if idioma and idioma in auto:
        return idioma, True
    for lang in ("pt", "en"):
        if lang in auto:
            return lang, True
    return None, False


def baixar_legenda(url, tmp: Path, cookies):
    # 1) só consulta os metadados (sem baixar nada)
    with yt_dlp.YoutubeDL(opcoes_base(cookies)) as ydl:
        info = ydl.extract_info(url, download=False)

    lang, automatica = escolher_idioma(info)
    if not lang:
        return info, None

    # 2) baixa somente a legenda escolhida, com pausa entre requisições
    opts = opcoes_base(cookies) | {
        "skip_download": True,
        "writesubtitles": not automatica,
        "writeautomaticsub": automatica,
        "subtitleslangs": [lang],
        "subtitlesformat": "vtt",
        "sleep_interval_subtitles": 2,
        "retries": 3,
        "outtmpl": str(tmp / "%(id)s.%(ext)s"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        print(f"  não consegui baixar a legenda ({e})")
        return info, None

    arquivos = list(tmp.glob(f"{info['id']}*.vtt"))
    texto = vtt_para_texto(arquivos[0]) if arquivos else None
    return info, texto


def transcrever_com_whisper(url, tmp: Path, cookies) -> str:
    global _whisper
    from faster_whisper import WhisperModel

    opts = opcoes_base(cookies) | {
        "format": "bestaudio/best",
        "outtmpl": str(tmp / "%(id)s.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        audio = ydl.prepare_filename(info)

    if _whisper is None:
        print(f"  carregando Whisper '{MODELO_WHISPER}' (CPU, int8)...")
        _whisper = WhisperModel(MODELO_WHISPER, device="cpu", compute_type="int8")

    print("  transcrevendo (pode demorar)...")
    segmentos, _ = _whisper.transcribe(audio, vad_filter=True)
    linhas = [s.text.strip() for s in segmentos]
    paragrafos = [" ".join(linhas[i:i + 6]) for i in range(0, len(linhas), 6)]
    return "\n\n".join(paragrafos)


# ---------------------------- resumo via LM Studio ---------------------------

def perguntar_lmstudio(mensagens) -> str:
    with urllib.request.urlopen(f"{LMSTUDIO_URL}/models", timeout=10) as r:
        modelo = json.load(r)["data"][0]["id"]
    corpo = json.dumps({"model": modelo, "messages": mensagens, "temperature": 0.2}).encode()
    req = urllib.request.Request(f"{LMSTUDIO_URL}/chat/completions", data=corpo,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def resumir(texto: str) -> str:
    blocos = [texto[i:i + TAMANHO_BLOCO] for i in range(0, len(texto), TAMANHO_BLOCO)]
    parciais = []
    for n, bloco in enumerate(blocos, 1):
        print(f"  resumindo bloco {n}/{len(blocos)}...")
        parciais.append(perguntar_lmstudio([
            {"role": "system", "content": "Você resume transcrições de vídeos em português do Brasil. "
             "A transcrição pode ter erros de reconhecimento. Não invente nada que não esteja no texto."},
            {"role": "user", "content": f"Resuma em tópicos curtos os pontos principais deste trecho:\n\n{bloco}"},
        ]))
    if len(parciais) == 1:
        return parciais[0]
    return perguntar_lmstudio([
        {"role": "system", "content": "Você organiza resumos em português do Brasil. Não invente nada."},
        {"role": "user", "content": "Junte estes resumos parciais em um único resumo com: "
         "1) ideias principais, 2) ações práticas que vale aplicar.\n\n" + "\n\n".join(parciais)},
    ])


# ---------------------------------- principal --------------------------------

def processar(url, args):
    destino = COFRE / PASTA_NOTAS
    destino.mkdir(parents=True, exist_ok=True)

    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        print(f"\n> {url}")
        info, texto = baixar_legenda(url, tmp, args.cookies)
        fonte = "legenda"
        if not texto:
            print("  sem legenda; usando Whisper")
            texto = transcrever_com_whisper(url, tmp, args.cookies)
            fonte = "whisper"

    titulo = info.get("title", info["id"])
    arquivo = destino / f"{limpar_nome(titulo)}.md"
    if arquivo.exists():
        print("  já existe, pulando.")
        return

    data_up = info.get("upload_date", "")
    if len(data_up) == 8:
        data_up = f"{data_up[:4]}-{data_up[4:6]}-{data_up[6:]}"

    partes = [
        "---",
        f'titulo: "{titulo.replace(chr(34), chr(39))}"',
        f'canal: "{info.get("uploader", "")}"',
        f"url: {info.get('webpage_url', url)}",
        f"publicado: {data_up}",
        f"salvo_em: {date.today().isoformat()}",
        f"fonte: {fonte}",
        "tags: [youtube]",
        "---",
        f"# {titulo}",
        "",
    ]
    if args.resumir:
        try:
            partes += ["## Resumo", "", resumir(texto), ""]
        except Exception as e:
            print(f"  não consegui resumir (o servidor do LM Studio está ligado?): {e}")
    partes += ["## Transcrição", "", texto, ""]

    arquivo.write_text("\n".join(partes), encoding="utf-8")
    print(f"  salvo: {arquivo}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--lista", help="arquivo .txt com um link por linha")
    ap.add_argument("--resumir", action="store_true", help="gera resumo com o LM Studio")
    ap.add_argument("--cookies", help="navegador para usar os cookies (chrome, firefox, edge)")
    args = ap.parse_args()

    urls = list(args.urls)
    if args.lista:
        urls += [l.strip() for l in Path(args.lista).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not urls:
        ap.error("passe pelo menos um link")

    for u in urls:
        try:
            processar(u, args)
        except Exception as e:
            print(f"  ERRO em {u}: {e}")


if __name__ == "__main__":
    main()