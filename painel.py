"""
painel.py  -  painel gráfico para salvar vídeos do YouTube no Obsidian.

Uso:   python painel.py
(precisa estar na mesma pasta que salvar_video.py)
"""

import os
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

import salvar_video as sv

try:  # deixa a janela nítida em telas com escala > 100% no Windows
    import ctypes
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass


class Painel(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Memória YouTube → Obsidian")
        self.geometry("900x760")
        self.minsize(780, 640)

        self.cfg = sv.carregar_config()
        self.fila = queue.Queue()   # mensagens vindas da thread de trabalho
        self.caminhos = {}          # id do item na árvore -> Path da pasta
        self.ocupado = False

        self._montar()
        self._garantir_cofre()
        self._recarregar_arvore()
        self.after(100, self._ler_fila)

    # ------------------------------------------------------------------ layout
    def _montar(self):
        pad = {"padx": 10, "pady": 4}

        # --- vault
        topo = ttk.Frame(self)
        topo.pack(fill="x", **pad)
        ttk.Label(topo, text="Vault do Obsidian:").pack(side="left")
        self.lbl_cofre = ttk.Label(topo, text="", foreground="#555555")
        self.lbl_cofre.pack(side="left", padx=6)
        ttk.Button(topo, text="Alterar…", command=self._escolher_cofre).pack(side="right")

        # --- links
        quadro = ttk.LabelFrame(self, text="Links dos vídeos (um por linha)")
        quadro.pack(fill="x", **pad)
        self.txt_urls = tk.Text(quadro, height=4, wrap="none")
        self.txt_urls.pack(fill="x", padx=6, pady=6)

        # --- como obter o texto
        opc = ttk.LabelFrame(self, text="Como obter o texto")
        opc.pack(fill="x", **pad)
        self.var_modo = tk.StringVar(value=self.cfg["modo"])
        for valor, texto in (
            ("auto", "Automático: legenda do YouTube e, se não houver, Whisper"),
            ("legenda", "Só a legenda do YouTube (rápido)"),
            ("whisper", "Só Whisper: transcreve o áudio (mais lento, funciona offline)"),
        ):
            ttk.Radiobutton(opc, text=texto, value=valor, variable=self.var_modo).pack(anchor="w", padx=8, pady=1)

        linha = ttk.Frame(opc)
        linha.pack(fill="x", padx=8, pady=4)
        ttk.Label(linha, text="Modelo Whisper:").pack(side="left")
        self.var_modelo = tk.StringVar(value=self.cfg["modelo_whisper"])
        ttk.Combobox(linha, textvariable=self.var_modelo, state="readonly", width=8,
                     values=["tiny", "base", "small", "medium"]).pack(side="left", padx=6)
        ttk.Label(linha, text="Cookies do navegador:").pack(side="left", padx=(16, 0))
        self.var_cookies = tk.StringVar(value=self.cfg["cookies"] or "(nenhum)")
        ttk.Combobox(linha, textvariable=self.var_cookies, state="readonly", width=10,
                     values=["(nenhum)", "firefox", "chrome", "edge"]).pack(side="left", padx=6)

        self.var_resumir = tk.BooleanVar(value=self.cfg["resumir"])
        ttk.Checkbutton(opc, text="Gerar resumo com o LM Studio (o servidor local precisa estar ligado)",
                        variable=self.var_resumir).pack(anchor="w", padx=8, pady=(0, 6))

        # --- destino
        dest = ttk.LabelFrame(self, text="Destino no Obsidian (clique na pasta onde a nota será salva)")
        dest.pack(fill="both", expand=True, **pad)
        corpo = ttk.Frame(dest)
        corpo.pack(fill="both", expand=True, padx=6, pady=6)

        self.arvore = ttk.Treeview(corpo, show="tree", selectmode="browse", height=8)
        rolagem = ttk.Scrollbar(corpo, orient="vertical", command=self.arvore.yview)
        self.arvore.configure(yscrollcommand=rolagem.set)
        self.arvore.pack(side="left", fill="both", expand=True)
        rolagem.pack(side="left", fill="y")

        botoes = ttk.Frame(corpo)
        botoes.pack(side="left", fill="y", padx=(8, 0))
        ttk.Button(botoes, text="Nova seção", command=lambda: self._nova_pasta(True)).pack(fill="x", pady=2)
        ttk.Button(botoes, text="Nova subseção", command=lambda: self._nova_pasta(False)).pack(fill="x", pady=2)
        ttk.Button(botoes, text="Atualizar lista", command=self._recarregar_arvore).pack(fill="x", pady=2)
        ttk.Button(botoes, text="Abrir pasta", command=self._abrir_pasta).pack(fill="x", pady=2)

        # --- ação
        acao = ttk.Frame(self)
        acao.pack(fill="x", **pad)
        self.btn = ttk.Button(acao, text="Salvar no Obsidian", command=self._iniciar)
        self.btn.pack(side="left")
        self.barra = ttk.Progressbar(acao, mode="indeterminate", length=220)
        self.barra.pack(side="left", padx=10)

        # --- log
        quadro_log = ttk.LabelFrame(self, text="Andamento")
        quadro_log.pack(fill="both", expand=True, **pad)
        self.log = tk.Text(quadro_log, height=8, state="disabled", wrap="word")
        rol_log = ttk.Scrollbar(quadro_log, orient="vertical", command=self.log.yview)
        self.log.configure(yscrollcommand=rol_log.set)
        self.log.pack(side="left", fill="both", expand=True, padx=(6, 0), pady=6)
        rol_log.pack(side="left", fill="y", pady=6)

    # ------------------------------------------------------------------- vault
    def _raiz(self):
        cofre = self.cfg["cofre"]
        if not cofre or not Path(cofre).is_dir():
            return None
        return Path(cofre) / self.cfg["pasta_raiz"]

    def _garantir_cofre(self):
        if self._raiz() is None:
            messagebox.showinfo(
                "Primeiro uso",
                "Escolha a pasta do seu vault do Obsidian\n(a pasta que contém a subpasta '.obsidian').")
            self._escolher_cofre()
        else:
            self._preparar_raiz()
        self._atualizar_label()

    def _escolher_cofre(self):
        pasta = filedialog.askdirectory(title="Escolha a pasta do seu vault do Obsidian",
                                        initialdir=str(Path.home() / "Documents"))
        if not pasta:
            return
        if not (Path(pasta) / ".obsidian").exists():
            if not messagebox.askyesno(
                    "É um vault?",
                    "Essa pasta não tem a subpasta '.obsidian', então talvez não seja um vault.\n"
                    "Usar mesmo assim?"):
                return
        self.cfg["cofre"] = str(Path(pasta))
        sv.salvar_config(self.cfg)
        self._preparar_raiz()
        self._atualizar_label()
        self._recarregar_arvore()

    def _preparar_raiz(self):
        """Cria a pasta YouTube e, se estiver vazia, as seções iniciais."""
        raiz = self._raiz()
        raiz.mkdir(parents=True, exist_ok=True)
        if not any(p.is_dir() for p in raiz.iterdir()):
            for nome in sv.SECOES_INICIAIS:
                (raiz / nome).mkdir(exist_ok=True)

    def _atualizar_label(self):
        self.lbl_cofre.config(text=self.cfg["cofre"] or "(não escolhido)")

    # ------------------------------------------------------------------ árvore
    def _recarregar_arvore(self, selecionar=None):
        self.arvore.delete(*self.arvore.get_children())
        self.caminhos.clear()
        raiz = self._raiz()
        if raiz is None:
            return
        no_raiz = self.arvore.insert("", "end", text=f"{raiz.name}  (raiz)", open=True)
        self.caminhos[no_raiz] = raiz
        self._preencher(no_raiz, raiz)

        if selecionar is None:
            rel = self.cfg.get("ultimo_destino", "")
            selecionar = raiz / rel if rel and rel != "." else raiz
        alvo = next((i for i, p in self.caminhos.items() if p == selecionar), no_raiz)
        self.arvore.selection_set(alvo)
        self.arvore.see(alvo)

    def _preencher(self, no_pai, pasta: Path):
        try:
            subs = sorted((p for p in pasta.iterdir() if p.is_dir() and not p.name.startswith(".")),
                          key=lambda p: p.name.lower())
        except OSError:
            return
        for sub in subs:
            item = self.arvore.insert(no_pai, "end", text=sub.name, open=True)
            self.caminhos[item] = sub
            self._preencher(item, sub)

    def _destino_selecionado(self):
        sel = self.arvore.selection()
        return self.caminhos.get(sel[0]) if sel else None

    def _nova_pasta(self, na_raiz: bool):
        raiz = self._raiz()
        if raiz is None:
            messagebox.showwarning("Vault", "Escolha primeiro o vault do Obsidian.")
            return
        pai = raiz if na_raiz else (self._destino_selecionado() or raiz)
        titulo = "Nova seção" if na_raiz else "Nova subseção"
        nome = simpledialog.askstring(titulo, f"Nome (será criada dentro de '{pai.name}'):", parent=self)
        if not nome:
            return
        nome = sv.limpar_nome(nome)
        if not nome:
            return
        nova = pai / nome
        nova.mkdir(parents=True, exist_ok=True)
        self._recarregar_arvore(selecionar=nova)

    def _abrir_pasta(self):
        pasta = self._destino_selecionado() or self._raiz()
        if pasta is None:
            return
        try:
            os.startfile(pasta)  # só existe no Windows
        except Exception as e:
            messagebox.showinfo("Pasta", f"{pasta}\n\n({e})")

    # --------------------------------------------------------------- execução
    def _escrever_log(self, msg):
        self.log.configure(state="normal")
        self.log.insert("end", msg + "\n")
        self.log.see("end")
        self.log.configure(state="disabled")

    def _iniciar(self):
        if self.ocupado:
            return
        raiz = self._raiz()
        if raiz is None:
            messagebox.showwarning("Vault", "Escolha primeiro o vault do Obsidian.")
            return
        urls = [l.strip() for l in self.txt_urls.get("1.0", "end").splitlines() if l.strip()]
        if not urls:
            messagebox.showwarning("Links", "Cole pelo menos um link.")
            return
        destino = self._destino_selecionado()
        if destino is None:
            messagebox.showwarning("Destino", "Clique na pasta onde a nota deve ser salva.")
            return

        rel = destino.relative_to(raiz).as_posix()
        categoria = "" if rel == "." else rel
        cookies = self.var_cookies.get()
        cookies = None if cookies == "(nenhum)" else cookies
        params = dict(
            modo=self.var_modo.get(),
            resumir_texto=self.var_resumir.get(),
            cookies=cookies,
            modelo_whisper=self.var_modelo.get(),
            categoria=categoria,
            lmstudio_url=self.cfg["lmstudio_url"],
        )

        # lembra as escolhas para a próxima vez
        self.cfg.update(modo=params["modo"], resumir=params["resumir_texto"],
                        cookies=cookies or "", modelo_whisper=params["modelo_whisper"],
                        ultimo_destino=rel)
        sv.salvar_config(self.cfg)

        self.ocupado = True
        self.btn.config(state="disabled")
        self.barra.start(12)
        threading.Thread(target=self._trabalho, args=(urls, destino, params), daemon=True).start()

    def _trabalho(self, urls, destino, params):
        """Roda em outra thread (para a janela não travar). Só se comunica pela fila."""
        def log(msg):
            self.fila.put(("log", msg))

        for u in urls:
            try:
                sv.processar_video(u, destino, log=log, **params)
            except Exception as e:
                log(f"  ERRO: {e}")
        self.fila.put(("fim", None))

    def _ler_fila(self):
        try:
            while True:
                tipo, msg = self.fila.get_nowait()
                if tipo == "log":
                    self._escrever_log(msg)
                elif tipo == "fim":
                    self.barra.stop()
                    self.btn.config(state="normal")
                    self.ocupado = False
                    self._escrever_log("Concluído.\n")
        except queue.Empty:
            pass
        self.after(100, self._ler_fila)


if __name__ == "__main__":
    Painel().mainloop()
