import tkinter as tk
from tkinter import ttk, messagebox
import threading
import json
import re
import tempfile
import urllib.request
from datetime import date
from pathlib import Path

import yt_dlp

# ----------------------------------------------------------------------------
# CONFIGURAÇÃO DO OBSIDIAN
# ----------------------------------------------------------------------------
COFRE_BASE = Path(r"C:\Users\User\Documents\Obsidian\主要\YouTube")

MODELO_WHISPER = "small"
LMSTUDIO_URL = "http://localhost:1234/v1"
TAMANHO_BLOCO = 8000

_whisper = None

# ----------------------------------------------------------------------------
# LÓGICA DE BACKEND (Extração, Transcrição e Resumo)
# ----------------------------------------------------------------------------

def limpar_nome(texto: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "", texto).strip()[:120]

def vtt_para_texto(caminho: Path) -> str:
    linhas, ultima = [], ""
    for linha in caminho.read_text(encoding="utf-8").splitlines():
        linha = linha.strip()
        if not linha or "-->" in linha or linha.startswith(("WEBVTT", "Kind:", "Language:", "NOTE")):
            continue
        linha = re.sub(r"<[^>]+>", "", linha).strip()
        if linha and linha != ultima:
            linhas.append(linha)
            ultima = linha
    paragrafos = [" ".join(linhas[i:i + 6]) for i in range(0, len(linhas), 6)]
    return "\n\n".join(paragrafos)

def opcoes_base():
    return {"quiet": True, "no_warnings": True, "noplaylist": True}

def escolher_idioma(info):
    manuais = info.get("subtitles") or {}
    auto = info.get("automatic_captions") or {}
    for prefixo in ("pt", "en"):
        for lang in manuais:
            if lang.startswith(prefixo): return lang, False
    for lang in auto:
        if lang.endswith("-orig"): return lang, True
    idioma = info.get("language")
    if idioma and idioma in auto: return idioma, True
    for lang in ("pt", "en"):
        if lang in auto: return lang, True
    return None, False

def baixar_legenda(url, tmp: Path, log_func):
    log_func("Buscando metadados do vídeo...")
    with yt_dlp.YoutubeDL(opcoes_base()) as ydl:
        info = ydl.extract_info(url, download=False)
    
    lang, automatica = escolher_idioma(info)
    if not lang:
        log_func("Nenhuma legenda útil encontrada no YouTube.")
        return info, None

    log_func(f"Baixando legenda ({lang})...")
    opts = opcoes_base() | {
        "skip_download": True,
        "writesubtitles": not automatica,
        "writeautomaticsub": automatica,
        "subtitleslangs": [lang],
        "subtitlesformat": "vtt",
        "outtmpl": str(tmp / "%(id)s.%(ext)s"),
    }
    try:
        with yt_dlp.YoutubeDL(opts) as ydl:
            ydl.extract_info(url, download=True)
    except Exception as e:
        log_func(f"Falha ao baixar legenda: {e}")
        return info, None

    arquivos = list(tmp.glob(f"{info['id']}*.vtt"))
    texto = vtt_para_texto(arquivos[0]) if arquivos else None
    return info, texto

def transcrever_com_whisper(url, tmp: Path, log_func) -> str:
    global _whisper
    from faster_whisper import WhisperModel

    log_func("Baixando áudio para o Whisper...")
    opts = opcoes_base() | {
        "format": "bestaudio/best",
        "outtmpl": str(tmp / "%(id)s.%(ext)s"),
    }
    with yt_dlp.YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=True)
        audio = ydl.prepare_filename(info)

    if _whisper is None:
        log_func(f"Carregando Whisper '{MODELO_WHISPER}' na memória...")
        _whisper = WhisperModel(MODELO_WHISPER, device="cpu", compute_type="int8")

    log_func("Transcrevendo áudio... (Isso pode demorar bastante dependendo do vídeo)")
    segmentos, _ = _whisper.transcribe(audio, vad_filter=True)
    linhas = [s.text.strip() for s in segmentos]
    paragrafos = [" ".join(linhas[i:i + 6]) for i in range(0, len(linhas), 6)]
    return "\n\n".join(paragrafos)

def perguntar_lmstudio(mensagens) -> str:
    with urllib.request.urlopen(f"{LMSTUDIO_URL}/models", timeout=10) as r:
        modelo = json.load(r)["data"][0]["id"]
    corpo = json.dumps({"model": modelo, "messages": mensagens, "temperature": 0.2}).encode()
    req = urllib.request.Request(f"{LMSTUDIO_URL}/chat/completions", data=corpo,
                                 headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=1800) as r:
        return json.load(r)["choices"][0]["message"]["content"].strip()

def resumir(texto: str, log_func) -> str:
    blocos = [texto[i:i + TAMANHO_BLOCO] for i in range(0, len(texto), TAMANHO_BLOCO)]
    parciais = []
    for n, bloco in enumerate(blocos, 1):
        log_func(f"Resumindo bloco {n}/{len(blocos)} no LM Studio...")
        parciais.append(perguntar_lmstudio([
            {"role": "system", "content": "Você resume transcrições de vídeos. Não invente fatos."},
            {"role": "user", "content": f"Resuma em tópicos os pontos principais:\n\n{bloco}"},
        ]))
    if len(parciais) == 1:
        return parciais[0]
    log_func("Consolidando resumos parciais...")
    return perguntar_lmstudio([
        {"role": "system", "content": "Você organiza resumos em português. Não invente nada."},
        {"role": "user", "content": "Junte estes resumos parciais:\n\n" + "\n\n".join(parciais)},
    ])

# ----------------------------------------------------------------------------
# INTERFACE GRÁFICA (GUI)
# ----------------------------------------------------------------------------

class AppYouTubeObsidian(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Extrator YouTube -> Obsidian")
        self.geometry("600x550")
        self.configure(padx=20, pady=20)
        
        COFRE_BASE.mkdir(parents=True, exist_ok=True)
        
        # --- Linha 1: URL ---
        tk.Label(self, text="Link do YouTube:", font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.entry_url = ttk.Entry(self, width=80)
        self.entry_url.pack(fill="x", pady=(0, 15))

        # --- Linha 2: Categoria (Pasta) ---
        frame_cat = tk.Frame(self)
        frame_cat.pack(fill="x", pady=(0, 15))
        
        tk.Label(frame_cat, text="Salvar na Seção:", font=("Segoe UI", 10, "bold")).pack(side="left", padx=(0, 10))
        self.combo_categorias = ttk.Combobox(frame_cat, state="readonly", width=25)
        self.combo_categorias.pack(side="left", padx=(0, 10))
        self.atualizar_categorias()
        
        ttk.Button(frame_cat, text="Nova Seção...", command=self.criar_categoria).pack(side="left")

        # --- Linha 3: Opções de Processamento ---
        frame_opts = tk.LabelFrame(self, text="Opções de Captura", padx=10, pady=10)
        frame_opts.pack(fill="x", pady=(0, 15))

        self.var_metodo = tk.StringVar(value="auto")
        ttk.Radiobutton(frame_opts, text="Priorizar Legenda do YouTube (Rápido)", variable=self.var_metodo, value="auto").pack(anchor="w")
        ttk.Radiobutton(frame_opts, text="Forçar Whisper (Ignorar legenda nativa)", variable=self.var_metodo, value="whisper").pack(anchor="w", pady=(5, 0))

        self.var_resumir = tk.BooleanVar(value=False)
        ttk.Checkbutton(frame_opts, text="Gerar Resumo com LM Studio (IA Local)", variable=self.var_resumir).pack(anchor="w", pady=(10, 0))

        # --- Linha 4: Botão de Ação ---
        self.btn_processar = ttk.Button(self, text="Processar Vídeo", command=self.iniciar_processamento)
        self.btn_processar.pack(pady=(5, 15), ipadx=10, ipady=5)

        # --- Linha 5: Console/Logs ---
        tk.Label(self, text="Progresso:", font=("Segoe UI", 9)).pack(anchor="w")
        self.text_log = tk.Text(self, height=10, state="disabled", bg="#f4f4f4", font=("Consolas", 9))
        self.text_log.pack(fill="both", expand=True)

    def log(self, mensagem):
        self.text_log.config(state="normal")
        self.text_log.insert("end", mensagem + "\n")
        self.text_log.see("end")
        self.text_log.config(state="disabled")
        self.update_idletasks()

    def atualizar_categorias(self):
        pastas = [p.name for p in COFRE_BASE.iterdir() if p.is_dir()]
        if not pastas:
            (COFRE_BASE / "Geral").mkdir()
            pastas = ["Geral"]
        self.combo_categorias["values"] = sorted(pastas)
        self.combo_categorias.current(0)

    def criar_categoria(self):
        nova = tk.simpledialog.askstring("Nova Seção", "Digite o nome da nova pasta (ex: Medicina):")
        if nova and nova.strip():
            nova = limpar_nome(nova)
            (COFRE_BASE / nova).mkdir(exist_ok=True)
            self.atualizar_categorias()
            self.combo_categorias.set(nova)

    def iniciar_processamento(self):
        url = self.entry_url.get().strip()
        if not url:
            messagebox.showwarning("Aviso", "Cole o link do YouTube primeiro.")
            return

        categoria = self.combo_categorias.get()
        destino = COFRE_BASE / categoria
        forcar_whisper = (self.var_metodo.get() == "whisper")
        gerar_resumo = self.var_resumir.get()

        # Desabilita o botão para evitar cliques duplos
        self.btn_processar.config(state="disabled")
        self.log(f"\n--- Iniciando: {url} ---")
        
        # Roda o processamento pesado em uma Thread separada (evita travar a janela)
        thread = threading.Thread(
            target=self.processar_backend,
            args=(url, destino, forcar_whisper, gerar_resumo)
        )
        thread.start()

    def processar_backend(self, url, destino, forcar_whisper, gerar_resumo):
        try:
            with tempfile.TemporaryDirectory() as t:
                tmp = Path(t)
                fonte = "whisper"
                
                if forcar_whisper:
                    self.log("Modo Whisper forçado ativado.")
                    texto = transcrever_com_whisper(url, tmp, self.log)
                    with yt_dlp.YoutubeDL(opcoes_base()) as ydl:
                        info = ydl.extract_info(url, download=False)
                else:
                    info, texto = baixar_legenda(url, tmp, self.log)
                    if texto:
                        fonte = "legenda_youtube"
                    else:
                        self.log("Fallback: Iniciando Whisper...")
                        texto = transcrever_com_whisper(url, tmp, self.log)

                titulo = info.get("title", info.get("id", "Video_Desconhecido"))
                arquivo = destino / f"{limpar_nome(titulo)}.md"
                
                if arquivo.exists():
                    self.log(f"O arquivo já existe na pasta '{destino.name}'. Processo cancelado.")
                    return

                data_up = info.get("upload_date", "")
                if len(data_up) == 8: data_up = f"{data_up[:4]}-{data_up[4:6]}-{data_up[6:]}"

                partes = [
                    "---",
                    f'titulo: "{titulo.replace(chr(34), chr(39))}"',
                    f'canal: "{info.get("uploader", "")}"',
                    f"url: {info.get('webpage_url', url)}",
                    f"publicado: {data_up}",
                    f"salvo_em: {date.today().isoformat()}",
                    f"fonte: {fonte}",
                    f"secao: {destino.name}",
                    "tags: [youtube, video_nota]",
                    "---",
                    f"# {titulo}",
                    "",
                ]

                if gerar_resumo:
                    try:
                        resumo = resumir(texto, self.log)
                        partes += ["## Resumo", "", resumo, ""]
                        self.log("Resumo gerado com sucesso.")
                    except Exception as e:
                        self.log(f"Erro ao resumir (LM Studio ligado?): {e}")

                partes += ["## Transcrição", "", texto, ""]
                arquivo.write_text("\n".join(partes), encoding="utf-8")
                self.log(f"✅ Sucesso! Nota salva em: {destino.name}/{arquivo.name}")

        except Exception as e:
            self.log(f"❌ ERRO CRÍTICO: {e}")
        finally:
            # Reativa o botão na interface principal
            self.after(0, lambda: self.btn_processar.config(state="normal"))

if __name__ == "__main__":
    app = AppYouTubeObsidian()
    app.mainloop()