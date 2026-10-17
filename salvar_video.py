"""
salvar_video.py  -  o "motor" do projeto.

Transforma vídeos do YouTube em notas Markdown dentro do seu vault do Obsidian.
É usado pelo painel gráfico (painel.py), mas também funciona sozinho:

  python salvar_video.py URL --pasta "Programação/Python"
  python salvar_video.py --lista links.txt --modo whisper --resumir
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
# Configuração (guardada em config.json, na mesma pasta deste arquivo)
# ----------------------------------------------------------------------------
CONFIG_PATH = Path(__file__).with_name("config.json")
CONFIG_PADRAO = {
    "cofre": "",                     # pasta do vault do Obsidian
    "pasta_raiz": "YouTube",         # pasta dentro do vault onde tudo é guardado
    "modo": "auto",                  # auto | legenda | whisper
    "modelo_whisper": "small",       # tiny | base | small | medium
    "resumir": False,
    "cookies": "",                   # firefox | chrome | edge | "" (nenhum)
    "ultimo_destino": "",
    "lmstudio_url": "http://localhost:1234/v1",
}
SECOES_INICIAIS = ["Medicina", "Programação", "Teologia", "Filosofia"]
TAMANHO_BLOCO = 8000  # caracteres por bloco na hora de resumir


def carregar_config() -> dict:
    cfg = dict(CONFIG_PADRAO)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


def salvar_config(cfg: dict) -> None:
    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")


# ----------------------------------------------------------------------------
# Utilidades
# ----------------------------------------------------------------------------

def limpar_nome(texto: str) -> str:
    """Remove caracteres proibidos em nomes de arquivo/pasta do Windows."""
    return re.sub(r'[\\/:*?"<>|]', "", texto).strip().lstrip(".")[:120].strip()


def agrupar(linhas, por_paragrafo=6) -> str:
    paragrafos = [" ".join(linhas[i:i + por_paragrafo]) for i in range(0, len(linhas), por_paragrafo)]
    return "\n\n".join(paragrafos)


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
    return agrupar(linhas)


def slug_tag(texto: str) -> str:
    return re.sub(r"[^\w-]+", "-", texto.lower()).strip("-")


def opcoes_base(cookies):
    opts = {"quiet": True, "no_warnings": True, "noplaylist": True}
    if cookies:
        opts["cookiesfrombrowser"] = (cookies,)
    return opts


# ----------------------------------------------------------------------------
# Obtenção do texto
# ----------------------------------------------------------------------------

def obter_info(url, cookies):
    """Só consulta os metadados do vídeo (título, canal, legendas disponíveis...)."""
    with yt_dlp.YoutubeDL(opcoes_base(cookies)) as ydl:
        return ydl.extract_info(url, download=False)


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


def baixar_legenda(url, info, tmp: Path, cookies, log=print):
    lang, automatica = escolher_idioma(info)
    if not lang:
        log("  este vídeo não tem legenda.")
        return None
    log(f"  baixando legenda: {lang} ({'automática' if automatica else 'manual'})")
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
        log(f"  não consegui baixar a legenda ({e})")
        return None
    arquivos = list(tmp.glob(f"{info['id']}*.vtt"))
    return vtt_para_texto(arquivos[0]) if arquivos else None


_whisper = {"nome": None, "modelo": None}


def obter_whisper(nome, log=print):
    if _whisper["nome"] != nome:
        from faster_whisper import WhisperModel
        log(f"  carregando Whisper '{nome}' (CPU, int8)...")
        _whisper["modelo"] = WhisperModel(nome, device="cpu", compute_type="int8")
        _whisper["nome"] = nome
    return _whisper["modelo"]


def transcrever_com_whisper(url, tmp: Path, cookies, nome_modelo, log=print) -> str:
    opts = opcoes_base(cookies) | {
        "format": "bestaudio/best",
        "outtmpl": str(tmp / "%(id)s.%(ext)s"),
    }
    log("  baixando o áudio...")
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        audio = ydl.prepare_filename(info)
    modelo = obter_whisper(nome_modelo, log)
    log("  transcrevendo (pode demorar bastante)...")
    segmentos, _ = modelo.transcribe(audio, vad_filter=True)
    return agrupar([s.text.strip() for s in segmentos])


# ----------------------------------------------------------------------------
# Resumo via LM Studio (servidor local)
# ----------------------------------------------------------------------------

def perguntar_lmstudio(mensagens, base_url) -> str:
    with urllib.request.urlopen(f"{base_url}/models", timeout=10) as r:
        modelo = json.load(r)["data"][0]["id"]
    corpo = json.dumps({"model": modelo, "messages": mensagens, "temperature": 0.2}).encode()
    req = urllib.request.Request(f"{base_url}/chat/completions", data=corpo,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()


def resumir(texto: str, base_url, log=print) -> str:
    blocos = [texto[i:i + TAMANHO_BLOCO] for i in range(0, len(texto), TAMANHO_BLOCO)]
    parciais = []
    for n, bloco in enumerate(blocos, 1):
        log(f"  resumindo bloco {n}/{len(blocos)}...")
        parciais.append(perguntar_lmstudio([
            {"role": "system", "content": "Você resume transcrições de vídeos em português do Brasil. "
             "A transcrição pode ter erros de reconhecimento. Não invente nada que não esteja no texto."},
            {"role": "user", "content": f"Resuma em tópicos curtos os pontos principais deste trecho:\n\n{bloco}"},
        ], base_url))
    if len(parciais) == 1:
        return parciais[0]
    return perguntar_lmstudio([
        {"role": "system", "content": "Você organiza resumos em português do Brasil. Não invente nada."},
        {"role": "user", "content": "Junte estes resumos parciais em um único resumo com: "
         "1) ideias principais, 2) ações práticas que vale aplicar.\n\n" + "\n\n".join(parciais)},
    ], base_url)


# ----------------------------------------------------------------------------
# Função principal de processamento de UM vídeo
# ----------------------------------------------------------------------------

def processar_video(url, destino: Path, modo="auto", resumir_texto=False, cookies=None,
                    modelo_whisper="small", categoria="",
                    lmstudio_url=CONFIG_PADRAO["lmstudio_url"], log=print):
    """
    modo: "legenda" (só YouTube), "whisper" (só Whisper) ou "auto" (legenda, senão Whisper).
    categoria: caminho relativo, ex. "Programação/Python" (vira tags na nota).
    Retorna o caminho da nota criada (ou já existente).
    """
    destino.mkdir(parents=True, exist_ok=True)
    log(f"> {url}")

    info = obter_info(url, cookies)
    titulo = info.get("title", info["id"])
    arquivo = destino / f"{limpar_nome(titulo)}.md"
    if arquivo.exists():
        log(f"  já existe, pulando: {arquivo.name}")
        return arquivo

    texto, fonte = None, ""
    with tempfile.TemporaryDirectory() as t:
        tmp = Path(t)
        if modo in ("legenda", "auto"):
            texto = baixar_legenda(url, info, tmp, cookies, log)
            fonte = "legenda"
            if not texto and modo == "legenda":
                raise RuntimeError("não foi possível obter a legenda. Tente o modo Whisper ou o Automático.")
        if not texto:
            if modo == "auto":
                log("  usando Whisper...")
            texto = transcrever_com_whisper(url, tmp, cookies, modelo_whisper, log)
            fonte = "whisper"

    data_up = info.get("upload_date", "") or ""
    if len(data_up) == 8:
        data_up = f"{data_up[:4]}-{data_up[4:6]}-{data_up[6:]}"

    tags = ["youtube"] + [slug_tag(p) for p in categoria.split("/") if p]
    partes = [
        "---",
        f'titulo: "{titulo.replace(chr(34), chr(39))}"',
        f'canal: "{(info.get("uploader") or "").replace(chr(34), chr(39))}"',
        f"url: {info.get('webpage_url', url)}",
        f"publicado: {data_up}",
        f"salvo_em: {date.today().isoformat()}",
        f"fonte: {fonte}",
        f'categoria: "{categoria}"',
        f"tags: [{', '.join(tags)}]",
        "---",
        f"# {titulo}",
        "",
    ]
    if resumir_texto:
        try:
            partes += ["## Resumo", "", resumir(texto, lmstudio_url, log), ""]
        except Exception as e:
            log(f"  não consegui resumir (o servidor do LM Studio está ligado?): {e}")
    partes += ["## Transcrição", "", texto, ""]

    arquivo.write_text("\n".join(partes), encoding="utf-8")
    log(f"  salvo: {arquivo}")
    return arquivo


# ----------------------------------------------------------------------------
# Uso pela linha de comando (opcional; o painel é o jeito principal)
# ----------------------------------------------------------------------------

def main():
    cfg = carregar_config()
    ap = argparse.ArgumentParser()
    ap.add_argument("urls", nargs="*")
    ap.add_argument("--lista", help="arquivo .txt com um link por linha")
    ap.add_argument("--pasta", default="", help='subpasta de destino, ex.: "Programação/Python"')
    ap.add_argument("--modo", choices=["auto", "legenda", "whisper"], default=cfg["modo"])
    ap.add_argument("--resumir", action="store_true", help="gera resumo com o LM Studio")
    ap.add_argument("--cookies", default=cfg["cookies"] or None,
                    help="navegador para usar os cookies (chrome, firefox, edge)")
    args = ap.parse_args()

    if not cfg["cofre"]:
        ap.error("vault não configurado. Abra o painel (python painel.py) uma vez para escolher.")

    urls = list(args.urls)
    if args.lista:
        urls += [l.strip() for l in Path(args.lista).read_text(encoding="utf-8").splitlines() if l.strip()]
    if not urls:
        ap.error("passe pelo menos um link")

    destino = Path(cfg["cofre"]) / cfg["pasta_raiz"] / args.pasta
    for u in urls:
        try:
            processar_video(u, destino, modo=args.modo, resumir_texto=args.resumir,
                            cookies=args.cookies, modelo_whisper=cfg["modelo_whisper"],
                            categoria=args.pasta.replace("\\", "/"), lmstudio_url=cfg["lmstudio_url"])
        except Exception as e:
            print(f"  ERRO em {u}: {e}")


if __name__ == "__main__":
    main()
