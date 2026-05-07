#!/usr/bin/env python3.14
"""
Catsdk 1.0 — a Cursor 3 clone in tkinter, dark UI with blue hue, LM Studio backend.

Features:
  • Cursor Tab     — multi-line ghost-text autocomplete (debounced)
  • Cmd/Ctrl+K     — inline natural-language edit of selection
  • Cmd/Ctrl+L     — focus chat
  • Agent mode     — autonomous coder with native function-calling
  • @-mentions     — @file, @codebase, @selection in chat
  • Bugbot         — scan current file for bugs
  • .acrules / .cursorrules — project-specific persistent instructions
  • Codebase index — keyword-ranked file lookup
  • Diff review    — Apply/Reject popups for agent edits
  • Lint loop      — automatic syntax check after every write

Single file, stdlib only. meow~
"""
from __future__ import annotations
import os, sys, json, re, queue, threading, difflib, ast, fnmatch, subprocess
import urllib.request, urllib.error
import tkinter as tk
from tkinter import ttk, filedialog, messagebox, font as tkfont

# ============================================================================
# DARK THEME — blue hue
# ============================================================================
BG          = "#0B0E13"   # main bg, very dark with blue tint
PANEL       = "#12161E"   # panel bg
PANEL_DARK  = "#080A0F"   # deepest (status, sidebars)
PANEL_HI    = "#1A1F28"   # hover/selected
BORDER      = "#1E2530"   # subtle borders
ACCENT      = "#4A9EFF"   # blue accent
ACCENT_HI   = "#6BB0FF"
ACCENT_LOW  = "#2D6FCC"
FG          = "#D8DEE9"
MUTED       = "#5C6677"
SEL_BG      = "#1F3D66"
LINE_BG     = "#11151D"
GUTTER_FG   = "#3D5A85"
GHOST       = "#3F5577"
ERROR       = "#E5484D"
OK          = "#46A758"
WARN        = "#D29922"
DIFF_ADD_BG = "#0F2317"
DIFF_DEL_BG = "#2A0E12"

BTN_BG, BTN_FG, BTN_HI, BTN_AFG = PANEL_HI, FG, "#222831", ACCENT
PRIMARY_BG, PRIMARY_FG = ACCENT, "#0B0E13"

SYN_KEYWORD = "#79B8FF"
SYN_STRING  = "#9ECBFF"
SYN_COMMENT = "#5C6677"
SYN_NUMBER  = "#79B8FF"
SYN_BUILTIN = "#B392F0"
SYN_DEF     = "#FFAB70"

LM_CHAT_URL   = "http://localhost:1234/v1/chat/completions"
LM_MODELS_URL = "http://localhost:1234/v1/models"

PY_KEYWORDS = {"False","None","True","and","as","assert","async","await","break",
    "class","continue","def","del","elif","else","except","finally","for","from",
    "global","if","import","in","is","lambda","nonlocal","not","or","pass","raise",
    "return","try","while","with","yield","match","case"}
PY_BUILTINS = {"print","len","range","str","int","float","list","dict","set","tuple",
    "bool","open","input","map","filter","zip","enumerate","sorted","reversed","sum",
    "min","max","abs","round","type","isinstance","hasattr","getattr","setattr",
    "self","cls","super","Exception","__init__","__name__","__main__"}
CODE_EXTS = (".py",".pyw",".js",".jsx",".ts",".tsx",".html",".css",".json",
             ".md",".txt",".sh",".c",".cpp",".h",".hpp",".rs",".go",".java",
             ".rb",".php",".lua",".toml",".yml",".yaml",".sql")

FONT_MONO = ("Menlo" if sys.platform=="darwin"
             else "Consolas" if sys.platform=="win32"
             else "DejaVu Sans Mono")
FONT_UI = ("SF Pro Text" if sys.platform=="darwin"
           else "Segoe UI" if sys.platform=="win32" else "Sans")

def F(size=10, weight="normal", mono=False, slant="roman"):
    return (FONT_MONO if mono else FONT_UI, size, weight, slant)

def styled_btn(parent, text, cmd, primary=False, **kw):
    bg = PRIMARY_BG if primary else BTN_BG
    fg = PRIMARY_FG if primary else BTN_FG
    hi = ACCENT_HI if primary else BTN_HI
    afg = PRIMARY_FG if primary else BTN_AFG
    return tk.Button(parent, text=text, bg=bg, fg=fg, bd=0,
        activebackground=hi, activeforeground=afg, relief="flat",
        font=F(9, "bold"), padx=10, pady=4, command=cmd,
        highlightthickness=0, **kw)

# ============================================================================
# LM STUDIO CLIENT
# ============================================================================
class LMStudioClient:
    def __init__(self):
        self.url = LM_CHAT_URL
        self.model = "local-model"

    def list_models(self):
        try:
            with urllib.request.urlopen(LM_MODELS_URL, timeout=4) as r:
                data = json.loads(r.read().decode("utf-8"))
            return [m.get("id","local-model") for m in data.get("data",[])]
        except Exception:
            return []

    def _post(self, body, timeout=300):
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url, data=data,
            headers={"Content-Type":"application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8"))

    def chat(self, messages, temperature=0.4, max_tokens=2048, stream_q=None):
        body = {"model": self.model, "messages": messages,
                "temperature": temperature, "max_tokens": max_tokens,
                "stream": bool(stream_q)}
        data = json.dumps(body).encode("utf-8")
        req = urllib.request.Request(self.url, data=data,
            headers={"Content-Type":"application/json"})
        try:
            with urllib.request.urlopen(req, timeout=300) as resp:
                if not stream_q:
                    out = json.loads(resp.read().decode("utf-8"))
                    return out["choices"][0]["message"]["content"]
                full = []
                for raw in resp:
                    line = raw.decode("utf-8", errors="ignore").strip()
                    if not line.startswith("data:"): continue
                    payload = line[5:].strip()
                    if payload == "[DONE]": break
                    try:
                        chunk = json.loads(payload)
                        tok = chunk["choices"][0].get("delta",{}).get("content","")
                        if tok:
                            full.append(tok); stream_q.put(("tok",tok))
                    except Exception: continue
                stream_q.put(("done","".join(full)))
                return "".join(full)
        except Exception as e:
            msg = f"[LM Studio error: {e}]"
            if stream_q: stream_q.put(("err", msg))
            return msg

    def chat_with_tools(self, messages, tools, temperature=0.2, max_tokens=4096):
        body = {"model": self.model, "messages": messages, "tools": tools,
                "tool_choice": "auto", "temperature": temperature,
                "max_tokens": max_tokens}
        try:
            out = self._post(body)
            return out["choices"][0]["message"], out.get("usage",{})
        except urllib.error.HTTPError as e:
            if e.code == 400:
                return ({"role":"assistant","_tool_unsupported":True,"content":""}, {})
            return ({"role":"assistant","content":f"[LM HTTP {e.code}]"}, {})
        except Exception as e:
            return ({"role":"assistant","content":f"[LM error: {type(e).__name__}: {e}]"}, {})

    def complete(self, prompt, max_tokens=120, temperature=0.1, stop=None):
        body = {"model": self.model,
                "messages":[{"role":"user","content":prompt}],
                "temperature": temperature, "max_tokens": max_tokens}
        if stop: body["stop"] = stop
        try:
            out = self._post(body, timeout=20)
            return out["choices"][0]["message"]["content"]
        except Exception:
            return ""

# ============================================================================
# CODEBASE INDEX
# ============================================================================
class CodebaseIndex:
    SKIP_DIRS = {".git","node_modules","__pycache__","venv",".venv",".idea",
                 "dist","build","target",".next",".cache"}
    def __init__(self):
        self.files, self.tokens, self.root = {}, {}, None

    def build(self, root):
        self.root = root; self.files.clear(); self.tokens.clear()
        for dp, dns, fns in os.walk(root):
            dns[:] = [d for d in dns if d not in self.SKIP_DIRS and not d.startswith(".")]
            for fn in fns:
                if not fn.endswith(CODE_EXTS): continue
                full = os.path.join(dp, fn)
                try:
                    if os.path.getsize(full) > 250_000: continue
                    with open(full,"r",encoding="utf-8",errors="ignore") as f:
                        text = f.read()
                except Exception: continue
                rel = os.path.relpath(full, root)
                self.files[rel] = text
                self.tokens[rel] = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", text.lower()))

    def search(self, query, k=5):
        q = set(re.findall(r"[A-Za-z_][A-Za-z0-9_]{2,}", query.lower()))
        if not q: return []
        scored = []
        for rel, toks in self.tokens.items():
            score = len(q & toks)
            if any(w in rel.lower() for w in q): score += 3
            if score > 0: scored.append((rel, score))
        scored.sort(key=lambda x: -x[1])
        return scored[:k]

    def all_paths(self): return list(self.files.keys())
    def get(self, rel): return self.files.get(rel, "")

def load_project_rules(root):
    for name in (".acrules", ".cursorrules"):
        p = os.path.join(root, name)
        if os.path.isfile(p):
            try:
                with open(p,"r",encoding="utf-8") as f: return f.read().strip()
            except Exception: pass
    return ""

# ============================================================================
# LINE NUMBERS
# ============================================================================
class LineNumbers(tk.Canvas):
    def __init__(self, master, text_widget, **kw):
        super().__init__(master, width=44, bg=BG, highlightthickness=0, bd=0, **kw)
        self.text = text_widget
    def redraw(self):
        self.delete("all")
        i = self.text.index("@0,0")
        while True:
            d = self.text.dlineinfo(i)
            if d is None: break
            ln = str(i).split(".")[0]
            self.create_text(38, d[1]+2, anchor="ne", text=ln,
                             fill=GUTTER_FG, font=F(10, mono=True))
            i = self.text.index(f"{i}+1line")

# ============================================================================
# CODE EDITOR
# ============================================================================
class CodeEditor(tk.Frame):
    def __init__(self, master, on_change=None):
        super().__init__(master, bg=BG)
        self.on_change = on_change
        self.path = None; self.dirty = False
        self.font = tkfont.Font(family=FONT_MONO, size=12)
        self._ghost_text = ""; self._ghost_anchor = None
        self._tab_completer = None

        wrap = tk.Frame(self, bg=BG); wrap.pack(fill="both", expand=True)
        self.text = tk.Text(wrap, wrap="none", undo=True, bg=BG, fg=FG,
            insertbackground=ACCENT, selectbackground=SEL_BG,
            selectforeground=FG, font=self.font, bd=0,
            highlightthickness=0, padx=10, pady=8, tabs=("1c",))
        self.lines = LineNumbers(wrap, self.text)
        self.vbar = ttk.Scrollbar(wrap, orient="vertical", command=self._vscroll)
        self.text.configure(yscrollcommand=self._yset)
        self.lines.pack(side="left", fill="y")
        self.vbar.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

        ff = (self.font.cget("family"), self.font.cget("size"))
        self.text.tag_configure("kw",  foreground=SYN_KEYWORD)
        self.text.tag_configure("str", foreground=SYN_STRING)
        self.text.tag_configure("com", foreground=SYN_COMMENT, font=ff+("italic",))
        self.text.tag_configure("num", foreground=SYN_NUMBER)
        self.text.tag_configure("bi",  foreground=SYN_BUILTIN)
        self.text.tag_configure("def", foreground=SYN_DEF)
        self.text.tag_configure("ghost", foreground=GHOST, font=ff+("italic",))
        self.text.tag_configure("current_line", background=LINE_BG)
        self.text.tag_configure("bug", background="#3A2A0E", underline=True)

        self.text.bind("<<Modified>>", self._modified)
        self.text.bind("<KeyRelease>", self._on_keyrelease)
        self.text.bind("<MouseWheel>", lambda e: self.after(1, self.lines.redraw))
        self.text.bind("<Button-4>",   lambda e: self.after(1, self.lines.redraw))
        self.text.bind("<Button-5>",   lambda e: self.after(1, self.lines.redraw))
        self.text.bind("<Button-1>",   lambda e: self.after(1, self._highlight_line))
        self.text.bind("<KeyPress>",   self._on_keypress)
        self.text.bind("<Configure>",  lambda e: self.lines.redraw())
        self.text.bind("<Return>",     self._auto_indent)
        self.text.bind("<Tab>",        self._handle_tab)
        self.text.bind("<Escape>",     self._dismiss_ghost)
        self._refresh_all()

    def _yset(self, *a): self.vbar.set(*a); self.lines.redraw()
    def _vscroll(self, *a): self.text.yview(*a); self.lines.redraw()

    def _modified(self, _=None):
        if self.text.edit_modified():
            self.dirty = True; self.text.edit_modified(False)
            if self.on_change: self.on_change()

    def _on_keyrelease(self, evt):
        mods = ("Shift_L","Shift_R","Control_L","Control_R","Alt_L","Alt_R","Meta_L","Meta_R")
        if evt.keysym in mods: return
        self._refresh_all()
        if self._tab_completer and evt.char and evt.char.isprintable():
            self._tab_completer.schedule()

    def _on_keypress(self, evt):
        mods = ("Tab","Shift_L","Shift_R","Control_L","Control_R","Alt_L","Alt_R","Meta_L","Meta_R")
        if self._ghost_text and evt.keysym not in mods:
            self._dismiss_ghost()
        self.after(1, self._highlight_line)

    def _refresh_all(self):
        self.lines.redraw(); self._highlight_syntax(); self._highlight_line()

    def _highlight_line(self):
        self.text.tag_remove("current_line","1.0","end")
        idx = self.text.index("insert").split(".")[0]
        self.text.tag_add("current_line", f"{idx}.0", f"{idx}.end+1c")

    def _highlight_syntax(self):
        for t in ("kw","str","com","num","bi","def"):
            self.text.tag_remove(t,"1.0","end")
        c = self.text.get("1.0","end-1c")
        for m in re.finditer(r"#[^\n]*", c):       self._tag("com", m.start(), m.end())
        for m in re.finditer(r"//[^\n]*", c):      self._tag("com", m.start(), m.end())
        for m in re.finditer(r"(\"\"\".*?\"\"\"|'''.*?'''|\"[^\"\n]*\"|'[^'\n]*')",
                             c, re.DOTALL):       self._tag("str", m.start(), m.end())
        for m in re.finditer(r"\b\d+(\.\d+)?\b", c): self._tag("num", m.start(), m.end())
        for m in re.finditer(r"\b[A-Za-z_][A-Za-z0-9_]*\b", c):
            w = m.group()
            if w in PY_KEYWORDS:   self._tag("kw", m.start(), m.end())
            elif w in PY_BUILTINS: self._tag("bi", m.start(), m.end())
        for m in re.finditer(r"\b(?:def|class)\s+([A-Za-z_][A-Za-z0-9_]*)", c):
            self._tag("def", m.start(1), m.end(1))

    def _tag(self, tag, s, e):
        self.text.tag_add(tag, f"1.0+{s}c", f"1.0+{e}c")

    def _auto_indent(self, _e):
        if self._ghost_text: self._dismiss_ghost()
        idx = self.text.index("insert")
        line = self.text.get(f"{idx} linestart", idx)
        indent = re.match(r"[ \t]*", line).group()
        if line.rstrip().endswith(":"): indent += "    "
        self.text.insert("insert", "\n" + indent)
        self.after(1, self._refresh_all)
        return "break"

    def _handle_tab(self, _e):
        if self._ghost_text:
            self._accept_ghost(); return "break"
        self.text.insert("insert", "    "); return "break"

    def show_ghost(self, text):
        self._dismiss_ghost()
        if not text: return
        self._ghost_text = text
        self._ghost_anchor = self.text.index("insert")
        self.text.insert(self._ghost_anchor, text, "ghost")
        self.text.mark_set("insert", self._ghost_anchor)

    def _dismiss_ghost(self, _e=None):
        if not self._ghost_text: return
        try:
            end = self.text.index(f"{self._ghost_anchor}+{len(self._ghost_text)}c")
            self.text.delete(self._ghost_anchor, end)
        except tk.TclError: pass
        self._ghost_text = ""; self._ghost_anchor = None

    def _accept_ghost(self):
        if not self._ghost_text: return
        end = self.text.index(f"{self._ghost_anchor}+{len(self._ghost_text)}c")
        self.text.tag_remove("ghost", self._ghost_anchor, end)
        self.text.mark_set("insert", end)
        self._ghost_text = ""; self._ghost_anchor = None
        self._refresh_all()

    def clear_bugs(self): self.text.tag_remove("bug","1.0","end")
    def mark_bug(self, line):
        self.text.tag_add("bug", f"{line}.0", f"{line}.end")

    def load_file(self, path):
        try:
            with open(path,"r",encoding="utf-8") as f: data = f.read()
        except Exception as e:
            messagebox.showerror("Open failed", str(e)); return False
        self.text.delete("1.0","end"); self.text.insert("1.0", data)
        self.path = path; self.dirty = False
        self.text.edit_modified(False); self._refresh_all()
        return True

    def reload_if_external(self):
        if not self.path: return
        try:
            with open(self.path,"r",encoding="utf-8") as f: disk = f.read()
        except Exception: return
        if disk != self.text.get("1.0","end-1c"):
            cur = self.text.index("insert")
            self.text.delete("1.0","end"); self.text.insert("1.0", disk)
            try: self.text.mark_set("insert", cur)
            except tk.TclError: pass
            self.dirty = False; self.text.edit_modified(False); self._refresh_all()

    def save_file(self, path=None):
        path = path or self.path
        if not path: return False
        try:
            with open(path,"w",encoding="utf-8") as f:
                f.write(self.text.get("1.0","end-1c"))
        except Exception as e:
            messagebox.showerror("Save failed", str(e)); return False
        self.path = path; self.dirty = False; return True

    def get_text(self): return self.text.get("1.0","end-1c")
    def get_selection(self):
        try: return self.text.get("sel.first","sel.last")
        except tk.TclError: return ""
    def replace_selection(self, new):
        try: self.text.delete("sel.first","sel.last")
        except tk.TclError: pass
        self.text.insert("insert", new); self._refresh_all()

# ============================================================================
# CURSOR TAB COMPLETER
# ============================================================================
class TabCompleter:
    DEBOUNCE_MS = 700
    PREFIX_CHARS = 1500
    SUFFIX_CHARS = 500
    def __init__(self, editor, client, enabled_var):
        self.editor = editor; self.client = client; self.enabled = enabled_var
        self._pending = None; self._busy = False; self._gen = 0
        editor._tab_completer = self

    def schedule(self):
        if not self.enabled.get(): return
        if self._pending:
            try: self.editor.after_cancel(self._pending)
            except Exception: pass
        self._gen += 1; gen = self._gen
        self._pending = self.editor.after(self.DEBOUNCE_MS, lambda: self._kick(gen))

    def _kick(self, gen):
        if gen != self._gen or self._busy or self.editor._ghost_text: return
        idx = self.editor.text.index("insert")
        line_end = self.editor.text.get(idx, f"{idx} lineend")
        if line_end.strip(): return
        full = self.editor.get_text()
        offset = self._idx_to_off(idx)
        prefix = full[max(0, offset-self.PREFIX_CHARS):offset]
        suffix = full[offset:offset+self.SUFFIX_CHARS]
        if not prefix.strip(): return
        self._busy = True
        threading.Thread(target=self._fetch, args=(prefix, suffix, gen),
                         daemon=True).start()

    def _idx_to_off(self, idx):
        text = self.editor.get_text()
        line, col = map(int, idx.split("."))
        lines = text.split("\n")
        return sum(len(l)+1 for l in lines[:line-1]) + col

    def _fetch(self, prefix, suffix, gen):
        lang = ""
        if self.editor.path:
            lang = os.path.splitext(self.editor.path)[1].lstrip(".")
        prompt = (f"You are an inline code completion engine. Given the code with "
                  f"the cursor marked <CURSOR>, output ONLY the completion text to "
                  f"insert. Output 1-6 lines max. No fences, no explanation, no "
                  f"repeating existing code. If nothing useful, empty.\n\n"
                  f"Language: {lang or 'auto'}\n\n"
                  f"```\n{prefix}<CURSOR>{suffix}\n```")
        comp = self.client.complete(prompt, max_tokens=120, temperature=0.1,
                                    stop=["```","\n\n\n"])
        comp = self._sanitize(comp)
        def show():
            self._busy = False
            if gen != self._gen or not comp or self.editor._ghost_text: return
            self.editor.show_ghost(comp)
        self.editor.after(0, show)

    @staticmethod
    def _sanitize(s):
        if not s: return ""
        s = s.strip("\n")
        s = re.sub(r"^```\w*\n?", "", s); s = re.sub(r"\n?```$", "", s)
        lines = s.split("\n")
        if len(lines) > 6: s = "\n".join(lines[:6])
        return s

# ============================================================================
# DIFF VIEWER
# ============================================================================
class DiffViewer(tk.Toplevel):
    def __init__(self, master, path, old, new, on_apply):
        super().__init__(master)
        self.title(f"Catsdk — review edit: {os.path.basename(path)}")
        self.configure(bg=BG); self.geometry("900x600")
        self.on_apply = on_apply
        hdr = tk.Frame(self, bg=PANEL_DARK); hdr.pack(fill="x")
        tk.Label(hdr, text=f"  {path}", bg=PANEL_DARK, fg=ACCENT,
                 font=F(11, "bold")).pack(side="left", pady=6)
        body = tk.Text(self, wrap="none", bg=BG, fg=FG, font=F(11, mono=True),
                       bd=0, highlightthickness=0, padx=6, pady=4)
        body.pack(fill="both", expand=True)
        body.tag_configure("add", background=DIFF_ADD_BG, foreground="#7EE7A0")
        body.tag_configure("del", background=DIFF_DEL_BG, foreground="#F08F94")
        body.tag_configure("hd",  foreground=ACCENT, font=F(10, "bold", mono=True))
        diff = difflib.unified_diff(old.splitlines(keepends=True),
                                    new.splitlines(keepends=True),
                                    fromfile="before", tofile="after", n=3)
        for line in diff:
            if line.startswith("+++") or line.startswith("---"):
                body.insert("end", line, "hd")
            elif line.startswith("@@"):
                body.insert("end", line, "hd")
                if not line.endswith("\n"): body.insert("end","\n")
            elif line.startswith("+"): body.insert("end", line, "add")
            elif line.startswith("-"): body.insert("end", line, "del")
            else: body.insert("end", line)
        body.config(state="disabled")
        btns = tk.Frame(self, bg=PANEL_DARK); btns.pack(fill="x")
        tk.Button(btns, text="Reject", bg=ERROR, fg="white",
            activebackground="#F0666B", bd=0, padx=12, pady=6,
            font=F(10, "bold"), command=self._reject).pack(side="right", padx=4, pady=4)
        tk.Button(btns, text="Apply", bg=OK, fg="white",
            activebackground="#5BC76E", bd=0, padx=12, pady=6,
            font=F(10, "bold"), command=self._apply).pack(side="right", padx=4, pady=4)
        self.transient(master); self.grab_set()
    def _apply(self):  self.on_apply(True);  self.destroy()
    def _reject(self): self.on_apply(False); self.destroy()

# ============================================================================
# FILE TREE
# ============================================================================
class FileTree(tk.Frame):
    def __init__(self, master, on_open, on_root_change=None):
        super().__init__(master, bg=PANEL_DARK)
        self.on_open = on_open; self.on_root_change = on_root_change
        self.root_path = os.getcwd()

        hdr = tk.Frame(self, bg=PANEL_DARK, height=32); hdr.pack(fill="x")
        tk.Label(hdr, text=" EXPLORER", bg=PANEL_DARK, fg=MUTED,
                 font=F(8, "bold")).pack(side="left", pady=8)
        tk.Button(hdr, text="📁", bg=PANEL_DARK, fg=MUTED, bd=0,
            activebackground=PANEL_HI, activeforeground=ACCENT, relief="flat",
            command=self.choose_folder).pack(side="right", padx=4)

        s = ttk.Style()
        try: s.theme_use("clam")
        except Exception: pass
        s.configure("Cat.Treeview", background=PANEL_DARK, foreground=FG,
                    fieldbackground=PANEL_DARK, borderwidth=0,
                    font=F(10), rowheight=22)
        s.map("Cat.Treeview",
              background=[("selected", PANEL_HI)],
              foreground=[("selected", ACCENT)])
        s.layout("Cat.Treeview.Item", [
            ("Treeitem.padding", {"sticky":"nswe", "children":[
                ("Treeitem.indicator", {"side":"left","sticky":""}),
                ("Treeitem.text",      {"side":"left","sticky":""}),
            ]})
        ])
        self.tree = ttk.Treeview(self, style="Cat.Treeview", show="tree")
        self.tree.pack(fill="both", expand=True, padx=2)
        self.tree.bind("<Double-1>", self._on_double)
        self.tree.bind("<<TreeviewOpen>>", self._on_expand)
        self.refresh()

    def choose_folder(self):
        d = filedialog.askdirectory(initialdir=self.root_path)
        if d:
            self.root_path = d; self.refresh()
            if self.on_root_change: self.on_root_change(d)

    def refresh(self):
        for i in self.tree.get_children(): self.tree.delete(i)
        rid = self.tree.insert("","end", text="📂 "+os.path.basename(self.root_path),
            open=True, values=(self.root_path,))
        self._populate(rid, self.root_path)

    def _populate(self, parent, path):
        try:
            entries = sorted(os.listdir(path),
                key=lambda n: (not os.path.isdir(os.path.join(path,n)), n.lower()))
        except PermissionError: return
        for name in entries:
            if name.startswith("."): continue
            full = os.path.join(path, name)
            if os.path.isdir(full):
                node = self.tree.insert(parent,"end",text="▸ "+name,values=(full,))
                self.tree.insert(node,"end",text="…",values=("",))
            else:
                icon = "•"
                self.tree.insert(parent,"end",text=f"{icon}  {name}",values=(full,))

    def _on_expand(self, _e):
        node = self.tree.focus(); kids = self.tree.get_children(node)
        if len(kids)==1 and self.tree.item(kids[0],"text")=="…":
            self.tree.delete(kids[0])
            path = self.tree.item(node,"values")[0]
            if path and os.path.isdir(path): self._populate(node, path)
            cur = self.tree.item(node,"text")
            if cur.startswith("▸ "): self.tree.item(node, text="▾ "+cur[2:])

    def _on_double(self, _e):
        node = self.tree.focus()
        path = self.tree.item(node,"values")[0]
        if path and os.path.isfile(path): self.on_open(path)

# ============================================================================
# AGENT — Cursor-style autonomous coder
# ============================================================================
AGENT_SYSTEM_PROMPT = """You are Catsdk Agent, an elite autonomous software engineer in Catsdk IDE.
You can read, search, write, and edit files in the user's project via the function-calling tools.

# WORKFLOW
1. UNDERSTAND. Read .acrules / project conventions if relevant.
2. EXPLORE first: list_dir, glob_files, file_outline, codebase_search, grep.
3. PLAN with update_todos: atomic steps; mark in_progress as you start, completed when done.
4. READ files BEFORE editing. Use offset/limit for files >500 lines.
5. EDIT precisely:
   • edit_file for surgical changes
   • multi_edit for several changes to ONE file in one shot
   • write_file only for NEW files or full rewrites
   • `old` MUST appear EXACTLY ONCE — include 2-3 lines of surrounding context
6. VERIFY: bash to run tests / the file. Read failures. Fix. Iterate.
7. FINISH with a clear summary of all changes.

# TOOL USE
- Call MULTIPLE tools in PARALLEL when independent (read 4 files at once).
- Lint feedback is automatic after every write — fix syntax errors before continuing.
- Bash requires user approval. Keep commands short.

# RULES
- NEVER fabricate file contents — always read.
- NEVER stop early. Iterate until the task is genuinely done.
- Be concise; tools speak louder than words.
- Match existing code style.
- Don't apologize, don't repeat the user's request — just work."""

def _tool(name, desc, props, required=None):
    return {"type":"function","function":{"name":name,"description":desc,
        "parameters":{"type":"object","properties":props,"required":required or []}}}

AGENT_TOOL_SCHEMAS = [
    _tool("list_dir","List files & folders in project-relative dir. '' = root.",
        {"path":{"type":"string"}}),
    _tool("read_file","Read file. For large files use offset (1-indexed) + limit.",
        {"path":{"type":"string"},
         "offset":{"type":"integer"},"limit":{"type":"integer"}},
        required=["path"]),
    _tool("file_outline","Structural outline (Python: classes/functions; other: head).",
        {"path":{"type":"string"}}, required=["path"]),
    _tool("write_file","Create new file or fully overwrite. Lints automatically.",
        {"path":{"type":"string"},"content":{"type":"string"}},
        required=["path","content"]),
    _tool("edit_file","Replace EXACTLY ONE unique occurrence of `old` with `new`.",
        {"path":{"type":"string"},"old":{"type":"string"},"new":{"type":"string"}},
        required=["path","old","new"]),
    _tool("multi_edit","Apply MULTIPLE sequential edits to ONE file in one call.",
        {"path":{"type":"string"},
         "edits":{"type":"array","items":{"type":"object",
            "properties":{"old":{"type":"string"},"new":{"type":"string"}},
            "required":["old","new"]}}},
        required=["path","edits"]),
    _tool("grep","Regex search across project. Optional file glob.",
        {"pattern":{"type":"string"},"glob":{"type":"string"},"path":{"type":"string"}},
        required=["pattern"]),
    _tool("glob_files","Find files by name pattern, e.g. '**/*.py'.",
        {"pattern":{"type":"string"}}, required=["pattern"]),
    _tool("codebase_search","Natural-language search across codebase index.",
        {"query":{"type":"string"}}, required=["query"]),
    _tool("bash","Run shell command in project root. USER MUST APPROVE.",
        {"command":{"type":"string"},"timeout":{"type":"integer"}},
        required=["command"]),
    _tool("update_todos","Set/update task plan. status: pending|in_progress|completed.",
        {"items":{"type":"array","items":{"type":"object",
            "properties":{"content":{"type":"string"},"status":{"type":"string"}},
            "required":["content","status"]}}}, required=["items"]),
    _tool("open_in_editor","Open file in user's editor pane.",
        {"path":{"type":"string"}}, required=["path"]),
    _tool("finish","Mark task complete with summary for the user.",
        {"answer":{"type":"string"}}, required=["answer"]),
]


class AgentTools:
    def __init__(self, root_getter, opener, approve_shell, approve_edit, index):
        self.get_root = root_getter
        self.open_in_editor = opener
        self.approve_shell = approve_shell
        self.approve_edit = approve_edit
        self.index = index
        self.todos = []; self.todos_dirty = False
        self.files_modified = set()

    def _resolve(self, path):
        root = os.path.normpath(self.get_root())
        full = os.path.normpath(os.path.join(root, path or ""))
        if not (full == root or full.startswith(root + os.sep)):
            raise ValueError(f"path escapes project root: {path!r}")
        return full

    def _lint(self, path, content):
        if path.endswith((".py",".pyw")):
            try: ast.parse(content)
            except SyntaxError as e:
                return f"⚠ LINT FAIL: SyntaxError: {e.msg} (line {e.lineno})"
        if path.endswith(".json"):
            try: json.loads(content)
            except Exception as e: return f"⚠ LINT FAIL: invalid JSON: {e}"
        return ""

    def list_dir(self, path=""):
        full = self._resolve(path)
        if not os.path.isdir(full): return f"[error] not a directory: {path or '.'}"
        items = []
        for name in sorted(os.listdir(full)):
            if name.startswith("."): continue
            kind = "📁" if os.path.isdir(os.path.join(full,name)) else "📄"
            items.append(f"{kind} {name}")
        return "\n".join(items) if items else "(empty)"

    def read_file(self, path, offset=1, limit=None):
        full = self._resolve(path)
        if not os.path.isfile(full): return f"[error] not a file: {path}"
        try:
            with open(full,"r",encoding="utf-8",errors="replace") as f:
                lines = f.readlines()
        except Exception as e: return f"[error] {e}"
        total = len(lines)
        offset = max(1, int(offset or 1))
        end = total if not limit else min(total, offset - 1 + int(limit))
        chunk = lines[offset-1:end]
        out = "".join(f"{offset+i:5d}│{ln}" for i, ln in enumerate(chunk))
        if len(out) > 30000: out = out[:30000] + "\n…[truncated]"
        return out + (f"\n[lines {offset}-{end} of {total}]"
                      if (offset!=1 or end!=total) else f"\n[{total} lines]")

    def file_outline(self, path):
        full = self._resolve(path)
        if not full.endswith((".py",".pyw")):
            try:
                with open(full,"r",encoding="utf-8",errors="replace") as f:
                    out = []
                    for i, line in enumerate(f, 1):
                        if line.strip(): out.append(f"{i:4d}: {line.rstrip()[:160]}")
                        if len(out) >= 40: break
                return "\n".join(out) if out else "(empty)"
            except Exception as e: return f"[error] {e}"
        try:
            with open(full,"r",encoding="utf-8") as f: tree = ast.parse(f.read())
        except Exception as e: return f"[error] {e}"
        items = []
        for node in tree.body:
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                items.append(f"L{node.lineno}: def {node.name}({', '.join(a.arg for a in node.args.args)})")
            elif isinstance(node, ast.ClassDef):
                items.append(f"L{node.lineno}: class {node.name}")
                for sub in node.body:
                    if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        items.append(f"  L{sub.lineno}:   def {sub.name}({', '.join(a.arg for a in sub.args.args)})")
            elif isinstance(node, ast.Import):
                items.append(f"L{node.lineno}: import {', '.join(a.name for a in node.names)}")
            elif isinstance(node, ast.ImportFrom):
                items.append(f"L{node.lineno}: from {node.module or ''} import {', '.join(a.name for a in node.names)}")
        return "\n".join(items) if items else "(empty)"

    def write_file(self, path, content):
        full = self._resolve(path)
        old = ""
        if os.path.isfile(full):
            try:
                with open(full,"r",encoding="utf-8") as f: old = f.read()
            except Exception: old = ""
        if not self.approve_edit(path, old, content): return "[rejected by user]"
        os.makedirs(os.path.dirname(full) or ".", exist_ok=True)
        try:
            with open(full,"w",encoding="utf-8") as f: f.write(content)
        except Exception as e: return f"[error] {e}"
        self.files_modified.add(path); self.open_in_editor(full)
        lint = self._lint(full, content)
        return f"✓ wrote {len(content)} chars to {path}" + (f"\n{lint}" if lint else "")

    def edit_file(self, path, old, new):
        full = self._resolve(path)
        try:
            with open(full,"r",encoding="utf-8") as f: data = f.read()
        except Exception as e: return f"[error] {e}"
        if old not in data:
            return ("[error] `old` not found. Re-read the file and copy exact text.")
        if data.count(old) > 1:
            return ("[error] `old` not unique — include surrounding context.")
        new_data = data.replace(old, new, 1)
        if not self.approve_edit(path, data, new_data): return "[rejected by user]"
        try:
            with open(full,"w",encoding="utf-8") as f: f.write(new_data)
        except Exception as e: return f"[error] {e}"
        self.files_modified.add(path); self.open_in_editor(full)
        lint = self._lint(full, new_data)
        return f"✓ edited {path} ({len(new_data)-len(data):+d} chars)" + (f"\n{lint}" if lint else "")

    def multi_edit(self, path, edits):
        full = self._resolve(path)
        try:
            with open(full,"r",encoding="utf-8") as f: data = f.read()
        except Exception as e: return f"[error] {e}"
        original = data
        for i, e in enumerate(edits, 1):
            old = e.get("old",""); new = e.get("new","")
            if old not in data: return f"[error] edit #{i}: `old` not found"
            if data.count(old) > 1: return f"[error] edit #{i}: `old` not unique"
            data = data.replace(old, new, 1)
        if not self.approve_edit(path, original, data): return "[rejected by user]"
        try:
            with open(full,"w",encoding="utf-8") as f: f.write(data)
        except Exception as e: return f"[error] {e}"
        self.files_modified.add(path); self.open_in_editor(full)
        lint = self._lint(full, data)
        return f"✓ applied {len(edits)} edits to {path}" + (f"\n{lint}" if lint else "")

    def grep(self, pattern, glob="**/*", path=""):
        root = self.get_root(); base = self._resolve(path or "")
        try: regex = re.compile(pattern)
        except re.error as e: return f"[error] bad regex: {e}"
        hits = []
        for dp, dns, fns in os.walk(base):
            dns[:] = [d for d in dns if not d.startswith(".")
                and d not in ("node_modules","__pycache__","venv",".venv","dist","build")]
            for fn in fns:
                full = os.path.join(dp, fn)
                rel = os.path.relpath(full, root)
                if not (fnmatch.fnmatch(rel, glob) or fnmatch.fnmatch(fn, glob)): continue
                try:
                    with open(full,"r",encoding="utf-8",errors="ignore") as f:
                        for i, line in enumerate(f, 1):
                            if regex.search(line):
                                hits.append(f"{rel}:{i}: {line.rstrip()[:200]}")
                                if len(hits) >= 100:
                                    return "\n".join(hits) + "\n[truncated]"
                except Exception: continue
        return "\n".join(hits) if hits else "(no matches)"

    def glob_files(self, pattern):
        import glob as g
        root = self.get_root(); hits = []
        for full in g.glob(os.path.join(root, pattern), recursive=True):
            rel = os.path.relpath(full, root)
            if any(p in rel.split(os.sep) for p in
                   (".git","node_modules","__pycache__","venv",".venv")):
                continue
            hits.append(rel)
            if len(hits) >= 200: hits.append("[truncated]"); break
        return "\n".join(sorted(hits)) if hits else "(no matches)"

    def codebase_search(self, query):
        results = self.index.search(query, k=8)
        if not results: return "(no matches)"
        return "\n".join(f"{rel}  (score {s})" for rel, s in results)

    def bash(self, command, timeout=60):
        if not self.approve_shell(command): return "[denied by user]"
        try:
            p = subprocess.run(command, shell=True, capture_output=True,
                text=True, timeout=timeout, cwd=self.get_root())
            out = p.stdout or ""
            if p.stderr: out += "\n[stderr]\n" + p.stderr
            return f"[exit {p.returncode}]\n{out[:8000]}"
        except subprocess.TimeoutExpired: return f"[timed out after {timeout}s]"
        except Exception as e: return f"[error] {e}"

    def update_todos(self, items):
        clean = []
        for it in items:
            st = it.get("status","pending")
            if st not in ("pending","in_progress","completed"): st = "pending"
            clean.append({"content": it.get("content",""), "status": st})
        self.todos = clean; self.todos_dirty = True
        n = sum(1 for t in clean if t["status"]=="completed")
        return f"✓ todos updated ({n}/{len(clean)} done)"

    def open_in_editor_tool(self, path):
        full = self._resolve(path); self.open_in_editor(full)
        return f"opened {path}"


class AgentRunner:
    MAX_STEPS = 40
    def __init__(self, client, tools, ui_callback, project_rules=""):
        self.client = client; self.tools = tools; self.ui = ui_callback
        self.project_rules = project_rules
        self._stop = threading.Event()
        self.total_tokens = 0

    def stop(self): self._stop.set()

    def run(self, user_task, prior=None, attached_files=None):
        self._stop.clear()
        sys_msg = AGENT_SYSTEM_PROMPT
        if self.project_rules:
            sys_msg += f"\n\n# PROJECT RULES (.acrules)\n{self.project_rules}"
        messages = [{"role":"system","content":sys_msg}]
        try: tree = self.tools.list_dir("")
        except Exception: tree = "(unavailable)"
        ctx = (f"<project_root>{self.tools.get_root()}</project_root>\n"
               f"<initial_listing>\n{tree[:2000]}\n</initial_listing>")
        if attached_files:
            ctx += "\n\n<attached_files>\n"
            for p in attached_files:
                content = self.tools.index.get(p) or ""
                ctx += f"\n=== {p} ===\n{content[:5000]}\n"
            ctx += "</attached_files>"
        messages.append({"role":"system","content":ctx})
        if prior: messages.extend(prior[-6:])
        messages.append({"role":"user","content":user_task})

        for step in range(1, self.MAX_STEPS + 1):
            if self._stop.is_set(): self.ui({"type":"stopped"}); return
            self.ui({"type":"thinking","step":step})
            msg, usage = self._call(messages)
            self.total_tokens += usage.get("total_tokens", 0)
            messages.append(self._sanitize(msg))
            tool_calls = msg.get("tool_calls") or []

            if not tool_calls and msg.get("content"):
                obj = self._extract_json(msg["content"])
                if obj:
                    if obj.get("done") or obj.get("tool")=="finish":
                        ans = obj.get("answer") or obj.get("args",{}).get("answer","")
                        self.ui({"type":"final","answer":ans,"tokens":self.total_tokens})
                        return
                    if obj.get("tool"):
                        tool_calls = [{"id":f"call_{step}_0","type":"function",
                            "function":{"name":obj["tool"],
                                "arguments":json.dumps(obj.get("args",{}))}}]

            if not tool_calls:
                self.ui({"type":"final","answer":msg.get("content","(no response)"),
                         "tokens":self.total_tokens})
                return

            for tc in tool_calls:
                if self._stop.is_set(): self.ui({"type":"stopped"}); return
                name = tc["function"]["name"]
                try: args = json.loads(tc["function"]["arguments"] or "{}")
                except Exception: args = {}
                self.ui({"type":"tool_call","step":step,"tool":name,
                         "args":args,"id":tc.get("id","")})
                if name == "finish":
                    ans = args.get("answer","(done)")
                    self.ui({"type":"final","answer":ans,"tokens":self.total_tokens})
                    return
                result = self._dispatch(name, args)
                self.ui({"type":"tool_result","step":step,"tool":name,
                         "result":result,"id":tc.get("id","")})
                messages.append({"role":"tool",
                    "tool_call_id":tc.get("id",f"call_{step}"),"name":name,
                    "content": result if len(result)<60000 else result[:60000]+"\n[truncated]"})
                if self.tools.todos_dirty:
                    self.tools.todos_dirty = False
                    self.ui({"type":"todos","items":list(self.tools.todos)})

        self.ui({"type":"error","msg":f"reached max steps ({self.MAX_STEPS})"})

    def _call(self, messages):
        msg, usage = self.client.chat_with_tools(messages, AGENT_TOOL_SCHEMAS)
        if msg.get("_tool_unsupported"):
            sys_extra = ("\n\n[FALLBACK MODE — model lacks tool calling] Output ONE "
                "```json fenced block per turn: "
                '{"thought":"...","tool":"<name>","args":{...}} '
                'or {"done":true,"answer":"..."}.')
            alt = [dict(messages[0])]
            alt[0]["content"] = messages[0]["content"] + sys_extra
            alt.extend(messages[1:])
            content = self.client.chat(alt, temperature=0.2, max_tokens=4096)
            msg = {"role":"assistant","content":content}; usage = {}
        return msg, usage

    @staticmethod
    def _sanitize(msg):
        out = {"role":msg.get("role","assistant")}
        if msg.get("content") is not None: out["content"] = msg["content"]
        if msg.get("tool_calls"): out["tool_calls"] = msg["tool_calls"]
        return out

    def _dispatch(self, tool, args):
        try:
            t = self.tools
            if tool=="list_dir":       return t.list_dir(args.get("path",""))
            if tool=="read_file":      return t.read_file(args["path"], args.get("offset",1), args.get("limit"))
            if tool=="file_outline":   return t.file_outline(args["path"])
            if tool=="write_file":     return t.write_file(args["path"], args.get("content",""))
            if tool=="edit_file":      return t.edit_file(args["path"], args["old"], args["new"])
            if tool=="multi_edit":     return t.multi_edit(args["path"], args["edits"])
            if tool=="grep":           return t.grep(args["pattern"], args.get("glob","**/*"), args.get("path",""))
            if tool=="glob_files":     return t.glob_files(args["pattern"])
            if tool=="codebase_search":return t.codebase_search(args["query"])
            if tool=="bash":           return t.bash(args["command"], args.get("timeout",60))
            if tool=="update_todos":   return t.update_todos(args["items"])
            if tool=="open_in_editor": return t.open_in_editor_tool(args["path"])
            return f"[error] unknown tool: {tool}"
        except KeyError as e: return f"[error] missing arg: {e}"
        except Exception as e: return f"[error] {type(e).__name__}: {e}"

    @staticmethod
    def _extract_json(text):
        if not text: return None
        m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
        if m:
            try: return json.loads(m.group(1))
            except Exception: pass
        m = re.search(r"(\{.*\})", text, re.DOTALL)
        if m:
            try: return json.loads(m.group(1))
            except Exception: pass
        return None

# ============================================================================
# @-MENTION POPUP
# ============================================================================
class AtMentionPopup(tk.Toplevel):
    SPECIALS = ["@codebase","@selection","@file"]
    def __init__(self, master, all_paths, on_select):
        super().__init__(master)
        self.overrideredirect(True); self.configure(bg=ACCENT)
        self.all_paths = all_paths; self.on_select = on_select
        self.lb = tk.Listbox(self, bg=PANEL, fg=FG, selectbackground=ACCENT,
            selectforeground=PRIMARY_FG, font=F(10, mono=True),
            bd=0, highlightthickness=1, highlightbackground=ACCENT,
            height=8, width=50)
        self.lb.pack(padx=1, pady=1)
        self.lb.bind("<Return>", self._pick)
        self.lb.bind("<Double-1>", self._pick)
        self.lb.bind("<Escape>", lambda e: self.destroy())
        self.refresh("")
    def refresh(self, query):
        self.lb.delete(0,"end"); q = query.lower()
        for s in self.SPECIALS:
            if q in s.lower(): self.lb.insert("end", s)
        for p in self.all_paths:
            if q in p.lower(): self.lb.insert("end", p)
            if self.lb.size() > 60: break
        if self.lb.size(): self.lb.selection_set(0)
    def _pick(self, _e=None):
        if not self.lb.curselection(): return
        val = self.lb.get(self.lb.curselection()[0])
        self.on_select(val.split()[0]); self.destroy()

# ============================================================================
# AI CHAT PANEL  (Cursor-style: mode dropdown, attachments, agent steps)
# ============================================================================
class AIChatPanel(tk.Frame):
    def __init__(self, master, get_editor, get_project_root, open_path_cb,
                 reload_editor_cb, codebase_index, project_rules_getter):
        super().__init__(master, bg=PANEL)
        self.get_editor = get_editor
        self.get_project_root = get_project_root
        self.open_path_cb = open_path_cb
        self.reload_editor_cb = reload_editor_cb
        self.index = codebase_index
        self.get_rules = project_rules_getter

        self.client = LMStudioClient()
        self.history = []
        self.q = queue.Queue()
        self.streaming = False
        self.mode = tk.StringVar(value="Agent")
        self.review_mode = tk.BooleanVar(value=False)
        self.tab_enabled = tk.BooleanVar(value=True)
        self.agent = None
        self._mention_popup = None
        self._attached_files = []

        # Header — minimal Cursor-style
        hdr = tk.Frame(self, bg=PANEL, height=36); hdr.pack(fill="x")
        tk.Label(hdr, text="  New Chat", bg=PANEL, fg=FG,
                 font=F(11, "bold")).pack(side="left", pady=8)
        tk.Button(hdr, text="✕", bg=PANEL, fg=MUTED, bd=0,
            activebackground=PANEL_HI, activeforeground=ACCENT, relief="flat",
            font=F(11), command=self._new_chat).pack(side="right", padx=8)
        tk.Button(hdr, text="↻", bg=PANEL, fg=MUTED, bd=0,
            activebackground=PANEL_HI, activeforeground=ACCENT, relief="flat",
            font=F(11), command=self.refresh_models).pack(side="right")

        # Mode dropdown row (like Cursor's @ Agent selector)
        moderow = tk.Frame(self, bg=PANEL); moderow.pack(fill="x", padx=8, pady=4)
        self.mode_btn = tk.Menubutton(moderow, text="◆ Agent ▾",
            bg=PANEL_HI, fg=ACCENT, activebackground=BTN_HI,
            activeforeground=ACCENT_HI, bd=0, relief="flat",
            font=F(10, "bold"), padx=10, pady=4)
        m = tk.Menu(self.mode_btn, tearoff=0, bg=PANEL_HI, fg=FG,
                    activebackground=ACCENT, activeforeground=PRIMARY_FG, bd=0)
        m.add_command(label="◆ Agent",  command=lambda: self._set_mode("Agent"))
        m.add_command(label="● Ask",    command=lambda: self._set_mode("Ask"))
        self.mode_btn["menu"] = m
        self.mode_btn.pack(side="left")

        # Model picker (compact)
        self.model_var = tk.StringVar(value="local-model")
        self.model_box = ttk.Combobox(moderow, textvariable=self.model_var,
            values=["local-model"], width=20, font=F(9, mono=True),
            state="readonly")
        self.model_box.pack(side="left", padx=6)

        self.status_dot = tk.Label(moderow, text="●", bg=PANEL,
                                   fg=WARN, font=F(11))
        self.status_dot.pack(side="right", padx=4)
        self.stop_btn = tk.Button(moderow, text="■", bg=PANEL, fg=MUTED, bd=0,
            activebackground=ERROR, activeforeground="white", relief="flat",
            font=F(11), command=self.stop_agent, state="disabled")
        self.stop_btn.pack(side="right", padx=2)

        # Conversation
        self.conv = tk.Text(self, wrap="word", bg=BG, fg=FG, font=F(11),
            bd=0, highlightthickness=0, padx=12, pady=10, state="disabled")
        self.conv.pack(fill="both", expand=True, padx=8, pady=4)
        c = self.conv
        c.tag_configure("user",   foreground=ACCENT, font=F(10, "bold"))
        c.tag_configure("ai",     foreground=FG)
        c.tag_configure("ailbl",  foreground=ACCENT_HI, font=F(10, "bold"))
        c.tag_configure("code",   background=PANEL_DARK, foreground="#9ECBFF",
                        font=F(10, mono=True))
        c.tag_configure("err",    foreground=ERROR, font=F(10, slant="italic"))
        c.tag_configure("step",   foreground=MUTED, font=F(8, "bold"))
        c.tag_configure("tool",   foreground=ACCENT, font=F(10, "bold", mono=True))
        c.tag_configure("toolargs",foreground=MUTED, font=F(9, mono=True))
        c.tag_configure("toolres",foreground=FG, background=PANEL_DARK,
                        font=F(9, mono=True))
        c.tag_configure("final",  foreground=OK, font=F(11, "bold"))
        c.tag_configure("todoH",  foreground=ACCENT, font=F(10, "bold"))
        c.tag_configure("todoP",  foreground=MUTED)
        c.tag_configure("todoI",  foreground=ACCENT_HI, font=F(10, "bold"))
        c.tag_configure("todoC",  foreground=OK, overstrike=True)
        c.tag_configure("attach", foreground=ACCENT, background=PANEL_HI,
                        font=F(9, slant="italic"))
        c.tag_configure("sys",    foreground=MUTED, font=F(9, slant="italic"))

        # Attached chips row
        self.attach_row = tk.Frame(self, bg=PANEL)
        self.attach_row.pack(fill="x", padx=8)

        # Input area
        ic = tk.Frame(self, bg=PANEL); ic.pack(fill="x", padx=8, pady=(4,8))
        in_wrap = tk.Frame(ic, bg=PANEL_HI, highlightthickness=1,
                           highlightbackground=BORDER)
        in_wrap.pack(fill="both", expand=True)
        self.input = tk.Text(in_wrap, height=3, bg=PANEL_HI, fg=FG, font=F(11),
            bd=0, highlightthickness=0, insertbackground=ACCENT,
            padx=8, pady=6)
        self.input.pack(fill="both", expand=True, padx=2, pady=2)
        self.input.bind("<Control-Return>", lambda e: self.send())
        self.input.bind("<Command-Return>", lambda e: self.send())
        self.input.bind("<KeyRelease>", self._on_input_key)
        self.input.bind("<FocusOut>", lambda e: self._close_mention())

        # Action row below input
        act = tk.Frame(self, bg=PANEL); act.pack(fill="x", padx=8, pady=(0,8))
        for label, cmd in [
            ("Explain",  lambda: self.quick("Explain this code:")),
            ("Refactor", lambda: self.quick("Refactor, keep behavior identical:")),
            ("Bugbot",   lambda: self.run_bugbot()),
        ]:
            tk.Button(act, text=label, bg=BTN_BG, fg=ACCENT, bd=0,
                activebackground=BTN_HI, activeforeground=ACCENT_HI,
                relief="flat", font=F(9, "bold"), padx=10, pady=4,
                highlightthickness=0,
                command=cmd).pack(side="left", padx=2)

        right = tk.Frame(act, bg=PANEL); right.pack(side="right")
        tk.Checkbutton(right, text="Tab", variable=self.tab_enabled,
            bg=PANEL, fg=MUTED, activebackground=PANEL,
            selectcolor=PANEL_HI, font=F(9), bd=0).pack(side="left")
        tk.Checkbutton(right, text="Review", variable=self.review_mode,
            bg=PANEL, fg=MUTED, activebackground=PANEL,
            selectcolor=PANEL_HI, font=F(9), bd=0).pack(side="left", padx=4)
        styled_btn(right, "Send  ⌘⏎", self.send, primary=True).pack(side="left", padx=2)

        self._sys("◆ Agent mode. Type @ for files.  Press Tab to accept ghost text.")
        self.after(200, self.refresh_models)
        self.after(50, self._drain_queue)

    def _new_chat(self):
        self.conv.config(state="normal"); self.conv.delete("1.0","end")
        self.conv.config(state="disabled")
        self.history = []
        self._sys("New chat started.")

    def _set_mode(self, m):
        self.mode.set(m)
        icon = "◆" if m == "Agent" else "●"
        self.mode_btn.config(text=f"{icon} {m} ▾")
        self._sys(f"Mode: {m}")

    def _sys(self, text):
        self.conv.config(state="normal")
        self.conv.insert("end", f"\n{text}\n", "sys")
        self.conv.see("end"); self.conv.config(state="disabled")

    def refresh_models(self):
        def work():
            models = self.client.list_models()
            def done():
                if models:
                    self.model_box["values"] = models
                    self.model_var.set(models[0])
                    self.client.model = models[0]
                    self.status_dot.config(fg=OK)
                else:
                    self.status_dot.config(fg=ERROR)
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    # @-mentions
    def _on_input_key(self, evt):
        cur = self.input.index("insert")
        before = self.input.get("1.0", cur)
        m = re.search(r"@([\w./-]*)$", before)
        if m: self._open_mention(m.group(1))
        else: self._close_mention()

    def _open_mention(self, query):
        if self._mention_popup is None:
            paths = self.index.all_paths()
            self._mention_popup = AtMentionPopup(self, paths, self._insert_mention)
            x = self.input.winfo_rootx()
            y = self.input.winfo_rooty() - 8 - 180
            self._mention_popup.geometry(f"+{x}+{y}")
        self._mention_popup.refresh(query)

    def _close_mention(self):
        if self._mention_popup is not None:
            try: self._mention_popup.destroy()
            except Exception: pass
            self._mention_popup = None

    def _insert_mention(self, token):
        cur = self.input.index("insert")
        before = self.input.get("1.0", cur)
        m = re.search(r"@([\w./-]*)$", before)
        if m:
            start = self.input.index(f"{cur} - {len(m.group(0))}c")
            self.input.delete(start, cur)
            self.input.insert(start, token + " ")
        if token == "@codebase":   self._attach("@codebase")
        elif token == "@selection":self._attach("@selection")
        elif token == "@file":
            ed = self.get_editor()
            if ed.path:
                self._attach(os.path.relpath(ed.path, self.get_project_root()))
        else: self._attach(token)
        self._close_mention(); self.input.focus_set()

    def _attach(self, label):
        if label not in self._attached_files:
            self._attached_files.append(label); self._render_attachments()

    def _render_attachments(self):
        for w in self.attach_row.winfo_children(): w.destroy()
        for label in self._attached_files:
            chip = tk.Frame(self.attach_row, bg=PANEL_HI,
                            highlightthickness=1, highlightbackground=BORDER)
            chip.pack(side="left", padx=2, pady=2)
            tk.Label(chip, text=" "+label+" ", bg=PANEL_HI, fg=ACCENT,
                     font=F(9, "bold")).pack(side="left")
            tk.Button(chip, text="×", bg=PANEL_HI, fg=MUTED, bd=0,
                activebackground=ERROR, activeforeground="white",
                font=F(9, "bold"), relief="flat",
                command=lambda l=label: self._unattach(l)).pack(side="left")

    def _unattach(self, label):
        if label in self._attached_files:
            self._attached_files.remove(label); self._render_attachments()

    def quick(self, prefix):
        ed = self.get_editor()
        sel = ed.get_selection() or ed.get_text()
        if not sel.strip():
            messagebox.showinfo("Catsdk","No code selected/open"); return
        self.input.delete("1.0","end")
        self.input.insert("1.0", f"{prefix}\n\n```\n{sel}\n```")
        self.send()

    # BUGBOT
    def run_bugbot(self):
        ed = self.get_editor(); code = ed.get_text()
        if not code.strip():
            messagebox.showinfo("Bugbot","No code in editor"); return
        self._sys("🐛 Bugbot scanning…"); ed.clear_bugs()
        prompt = ("Find bugs and anti-patterns. Output ONLY a JSON array, no fences. "
                  "Items: {line:int, severity:'error'|'warning'|'info', message:str}. "
                  "Limit 12. If none, [].\n\n```\n" + code + "\n```")
        def work():
            self.client.model = self.model_var.get() or "local-model"
            out = self.client.chat([{"role":"user","content":prompt}],
                                   temperature=0.1, max_tokens=2048)
            issues = self._parse_bug_json(out)
            def done():
                if not issues:
                    self._sys("🐛 Bugbot: no issues found ✓"); return
                self.conv.config(state="normal")
                self.conv.insert("end","\n🐛 BUGBOT REPORT\n","todoH")
                for it in issues:
                    line = it.get("line",1); sev = it.get("severity","warning")
                    msg  = it.get("message","")
                    icon = {"error":"❌","warning":"⚠","info":"ℹ"}.get(sev,"•")
                    self.conv.insert("end", f"  {icon} L{line}: {msg}\n", "ai")
                    try: ed.mark_bug(int(line))
                    except Exception: pass
                self.conv.see("end"); self.conv.config(state="disabled")
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    @staticmethod
    def _parse_bug_json(text):
        if not text: return []
        m = re.search(r"\[.*\]", text, re.DOTALL)
        if not m: return []
        try: return json.loads(m.group(0))
        except Exception: return []

    # SEND
    def send(self):
        if self.streaming: return "break"
        msg = self.input.get("1.0","end-1c").strip()
        if not msg: return "break"
        self.input.delete("1.0","end")
        self.client.model = self.model_var.get() or "local-model"
        if self.mode.get() == "Agent": self._start_agent(msg)
        else: self._send_chat(msg)
        return "break"

    def _send_chat(self, msg):
        attached = []
        for label in self._attached_files:
            if label == "@codebase":
                for path,_ in self.index.search(msg, k=4):
                    content = self.index.get(path)
                    if content: attached.append(f"=== {path} ===\n{content[:5000]}")
            elif label == "@selection":
                sel = self.get_editor().get_selection()
                if sel: attached.append(f"=== @selection ===\n{sel}")
            else:
                content = self.index.get(label)
                if content: attached.append(f"=== {label} ===\n{content[:5000]}")
        ctx = ""
        if attached: ctx = "\n\n[CONTEXT]\n" + "\n\n".join(attached)
        self.history.append({"role":"user","content": msg + ctx})
        self._append_msg("You", msg, "user")
        if self._attached_files:
            self.conv.config(state="normal")
            self.conv.insert("end","  attached: "+", ".join(self._attached_files)+"\n","attach")
            self.conv.config(state="disabled")
        self._append_msg("Catsdk", "", "ailbl")
        self._attached_files = []; self._render_attachments()
        self.streaming = True
        rules = self.get_rules()
        sys_p = "You are Catsdk, a precise coding assistant. Be concise. Use code fences."
        if rules: sys_p += f"\n\n# PROJECT RULES\n{rules}"
        full = [{"role":"system","content":sys_p}] + self.history
        threading.Thread(target=lambda:
            self.client.chat(full, stream_q=self.q), daemon=True).start()

    def _start_agent(self, task):
        self._append_msg("You", task, "user")
        if self._attached_files:
            self.conv.config(state="normal")
            self.conv.insert("end","  attached: "+", ".join(self._attached_files)+"\n","attach")
            self.conv.config(state="disabled")
        attached_paths = []
        for label in self._attached_files:
            if label == "@codebase":
                attached_paths.extend(p for p,_ in self.index.search(task, k=4))
            elif label == "@selection":
                pass
            elif not label.startswith("@"):
                attached_paths.append(label)
        if "@selection" in self._attached_files:
            sel = self.get_editor().get_selection()
            if sel: task += f"\n\n[SELECTION]\n```\n{sel}\n```"
        self._attached_files = []; self._render_attachments()

        tools = AgentTools(
            root_getter=self.get_project_root,
            opener=lambda p: self.after(0, lambda: (self.open_path_cb(p),
                                                    self.reload_editor_cb())),
            approve_shell=self._approve_shell,
            approve_edit=self._approve_edit,
            index=self.index)
        self.agent = AgentRunner(self.client, tools, self._agent_event,
                                 project_rules=self.get_rules())
        self.streaming = True
        self.stop_btn.config(state="normal", fg=ERROR)
        history_for_agent = [m for m in self.history if m["role"] != "system"]
        def work():
            try:
                self.agent.run(task, history_for_agent, attached_paths)
            finally:
                self.after(0, lambda: (
                    setattr(self,"streaming",False),
                    self.stop_btn.config(state="disabled", fg=MUTED)))
        threading.Thread(target=work, daemon=True).start()

    def stop_agent(self):
        if self.agent: self.agent.stop()
        self._sys("⏹ stopping after current step…")

    def _agent_event(self, evt):
        self.after(0, lambda: self._render_agent_event(evt))

    def _render_agent_event(self, evt):
        self.conv.config(state="normal"); t = evt["type"]
        if t == "thinking":
            self.conv.insert("end", f"\nstep {evt['step']}\n", "step")
        elif t == "tool_call":
            self.conv.insert("end", f"  → {evt['tool']}", "tool")
            args = json.dumps(evt["args"], ensure_ascii=False)
            if len(args) > 240: args = args[:240] + "…"
            self.conv.insert("end", f"  {args}\n", "toolargs")
        elif t == "tool_result":
            res = evt["result"]
            if len(res) > 1200: res = res[:1200] + "\n…[truncated]"
            self.conv.insert("end", res + "\n", "toolres")
        elif t == "todos":
            self.conv.insert("end","\nTASK PLAN\n","todoH")
            for it in evt["items"]:
                st = it["status"]
                if st == "completed":
                    self.conv.insert("end", f"  ☑ {it['content']}\n", "todoC")
                elif st == "in_progress":
                    self.conv.insert("end", f"  ⟳ {it['content']}\n", "todoI")
                else:
                    self.conv.insert("end", f"  ☐ {it['content']}\n", "todoP")
        elif t == "final":
            self.conv.insert("end","\n✓ ","final")
            self.conv.insert("end", evt.get("answer","(done)") + "\n", "ai")
            tk_used = evt.get("tokens", 0)
            if tk_used: self.conv.insert("end", f"  [{tk_used} tokens]\n", "step")
            self.history.append({"role":"user","content":"[agent task]"})
            self.history.append({"role":"assistant","content":evt.get("answer","")})
        elif t == "stopped":
            self.conv.insert("end","\n⏹ stopped\n","err")
        elif t == "error":
            self.conv.insert("end","\n⚠ "+evt["msg"]+"\n","err")
        self.conv.see("end"); self.conv.config(state="disabled")

    def _approve_shell(self, command):
        result = {"ok":False,"ev":threading.Event()}
        def ask():
            result["ok"] = messagebox.askyesno("Catsdk Agent: run shell?",
                f"Command:\n\n  {command}\n\nAllow it?", icon="warning")
            result["ev"].set()
        self.after(0, ask)
        result["ev"].wait(timeout=300)
        return result["ok"]

    def _approve_edit(self, path, old, new):
        if not self.review_mode.get(): return True
        result = {"ok":False,"ev":threading.Event()}
        def ask():
            DiffViewer(self, path, old, new,
                on_apply=lambda ok: (result.__setitem__("ok",ok),
                                     result["ev"].set()))
        self.after(0, ask)
        result["ev"].wait(timeout=600)
        return result["ok"]

    def _append_msg(self, who, text, tag):
        self.conv.config(state="normal")
        self.conv.insert("end", f"\n{who}\n", tag)
        if text: self._insert_with_code(text)
        self.conv.see("end"); self.conv.config(state="disabled")

    def _insert_with_code(self, text):
        parts = re.split(r"```([\w+-]*)\n?(.*?)```", text, flags=re.DOTALL)
        i = 0
        while i < len(parts):
            chunk = parts[i]
            if i % 3 == 0:
                if chunk: self.conv.insert("end", chunk, "ai")
                i += 1
            else:
                code = parts[i+1] if i+1 < len(parts) else ""
                self.conv.insert("end", "\n"+code+"\n", "code"); i += 2

    def _drain_queue(self):
        try:
            while True:
                kind, payload = self.q.get_nowait()
                if kind == "tok":
                    self.conv.config(state="normal")
                    self.conv.insert("end", payload, "ai")
                    self.conv.see("end"); self.conv.config(state="disabled")
                elif kind == "done":
                    self.history.append({"role":"assistant","content":payload})
                    self.streaming = False
                    self.conv.config(state="normal")
                    self.conv.insert("end","\n"); self.conv.config(state="disabled")
                elif kind == "err":
                    self.conv.config(state="normal")
                    self.conv.insert("end","\n"+payload+"\n","err")
                    self.conv.config(state="disabled"); self.streaming = False
        except queue.Empty: pass
        self.after(40, self._drain_queue)

# ============================================================================
# INLINE PROMPT (Cmd+K)
# ============================================================================
class InlinePrompt(tk.Toplevel):
    def __init__(self, master, editor, client, project_rules=""):
        super().__init__(master)
        self.editor = editor; self.client = client; self.rules = project_rules
        self.title("Catsdk: edit selection")
        self.configure(bg=PANEL_DARK); self.geometry("560x130")
        self.resizable(False, False)
        tk.Label(self, text="  Tell Catsdk how to change the selection:",
                 bg=PANEL_DARK, fg=ACCENT,
                 font=F(10, "bold")).pack(anchor="w", pady=(8,2))
        self.entry = tk.Text(self, height=2, bg=PANEL_HI, fg=FG, font=F(11),
            bd=0, padx=6, pady=4, insertbackground=ACCENT,
            highlightthickness=1, highlightbackground=BORDER)
        self.entry.pack(fill="both", expand=True, padx=8)
        self.entry.focus_set()
        btns = tk.Frame(self, bg=PANEL_DARK); btns.pack(fill="x", pady=6)
        styled_btn(btns, "Cancel (Esc)", self.destroy).pack(side="right", padx=4)
        styled_btn(btns, "Apply (⏎)", self.apply, primary=True).pack(side="right")
        self.bind("<Escape>", lambda e: self.destroy())
        self.entry.bind("<Return>", lambda e: self.apply())

    def apply(self):
        instr = self.entry.get("1.0","end-1c").strip()
        if not instr: return "break"
        sel = self.editor.get_selection()
        if not sel:
            messagebox.showinfo("Catsdk","Select code first.")
            self.destroy(); return "break"
        sys_p = "You output ONLY code, no fences, no explanations."
        if self.rules: sys_p += f"\n\n# PROJECT RULES\n{self.rules}"
        prompt = (f"Rewrite per instruction. Return ONLY new code, no fences, "
                  f"no commentary.\n\nINSTRUCTION: {instr}\n\nCODE:\n{sel}")
        self.config(cursor="watch"); self.update_idletasks()
        def work():
            out = self.client.chat(
                [{"role":"system","content":sys_p},
                 {"role":"user","content":prompt}],
                temperature=0.2, max_tokens=2048)
            out = re.sub(r"^```\w*\n?","", out.strip())
            out = re.sub(r"\n?```$","", out)
            def done():
                self.editor.replace_selection(out); self.destroy()
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()
        return "break"

# ============================================================================
# WELCOME (no file open)
# ============================================================================
class Welcome(tk.Frame):
    def __init__(self, master, on_open, on_open_folder, on_new):
        super().__init__(master, bg=BG)
        wrap = tk.Frame(self, bg=BG); wrap.place(relx=.5, rely=.45, anchor="center")
        # 3D-ish cube logo (made with text)
        cube = tk.Canvas(wrap, width=120, height=120, bg=BG, highlightthickness=0)
        cube.pack(pady=(0,16))
        # draw an isometric cube
        cx, cy, s = 60, 60, 38
        # top face
        cube.create_polygon(cx, cy-s, cx+s, cy-s/2, cx, cy, cx-s, cy-s/2,
                            fill=ACCENT, outline=ACCENT_HI)
        # left face
        cube.create_polygon(cx-s, cy-s/2, cx, cy, cx, cy+s, cx-s, cy+s/2,
                            fill=ACCENT_LOW, outline=ACCENT)
        # right face
        cube.create_polygon(cx+s, cy-s/2, cx, cy, cx, cy+s, cx+s, cy+s/2,
                            fill="#1A4A85", outline=ACCENT)

        tk.Label(wrap, text="Catsdk", bg=BG, fg=FG,
                 font=F(28, "bold")).pack()
        tk.Label(wrap, text="An AI-powered IDE  •  meow~",
                 bg=BG, fg=MUTED, font=F(11)).pack(pady=(2,18))

        for label, cmd, key in [
            ("New File",      on_new,         "⌘N"),
            ("Open File…",    on_open,        "⌘O"),
            ("Open Folder…",  on_open_folder, ""),
        ]:
            row = tk.Frame(wrap, bg=BG); row.pack(fill="x", pady=2)
            b = tk.Button(row, text=label, bg=BG, fg=ACCENT, bd=0,
                activebackground=PANEL_HI, activeforeground=ACCENT_HI,
                relief="flat", anchor="w", font=F(11, "bold"),
                padx=12, pady=4, command=cmd, width=20)
            b.pack(side="left")
            if key:
                tk.Label(row, text=key, bg=BG, fg=MUTED,
                         font=F(10, mono=True)).pack(side="left", padx=8)

# ============================================================================
# MAIN APPLICATION
# ============================================================================
class Catsdk(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Catsdk 1.0")
        self.geometry("1380x840")
        self.configure(bg=BG)
        try: self.tk.call("tk","scaling",1.2)
        except Exception: pass

        # Dark ttk theme tweaks
        s = ttk.Style()
        try: s.theme_use("clam")
        except Exception: pass
        s.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG,
                    bordercolor=BG, arrowcolor=MUTED, lightcolor=PANEL,
                    darkcolor=PANEL)
        s.configure("Horizontal.TScrollbar", background=PANEL, troughcolor=BG,
                    bordercolor=BG, arrowcolor=MUTED)
        s.configure("TCombobox", fieldbackground=PANEL_HI, background=PANEL_HI,
                    foreground=FG, arrowcolor=ACCENT, bordercolor=BORDER,
                    lightcolor=PANEL_HI, darkcolor=PANEL_HI)
        s.map("TCombobox", fieldbackground=[("readonly", PANEL_HI)],
              foreground=[("readonly", FG)])

        self.index = CodebaseIndex()
        self.project_rules = ""

        self._build_menu()
        self._build_titlebar()
        self._build_layout()
        self._build_statusbar()
        self._bind_keys()

        self.tab_completer = TabCompleter(self.editor, self.chat.client,
                                          self.chat.tab_enabled)
        self._show_welcome()
        self.after(150, lambda: self._on_root_change(self.tree.root_path))
        self.after(150, self._tick_status)

    def _build_menu(self):
        mb = tk.Menu(self, bg=PANEL, fg=FG, activebackground=ACCENT,
                     activeforeground=PRIMARY_FG, bd=0)
        m_file = tk.Menu(mb, tearoff=0, bg=PANEL, fg=FG,
                         activebackground=ACCENT, activeforeground=PRIMARY_FG)
        m_file.add_command(label="New        Ctrl+N", command=self.new_file)
        m_file.add_command(label="Open…      Ctrl+O", command=self.open_file)
        m_file.add_command(label="Open Folder…",     command=lambda: self.tree.choose_folder())
        m_file.add_separator()
        m_file.add_command(label="Save       Ctrl+S", command=self.save_file)
        m_file.add_command(label="Save As…",          command=self.save_as)
        m_file.add_separator()
        m_file.add_command(label="Exit", command=self.destroy)
        mb.add_cascade(label="File", menu=m_file)

        m_ai = tk.Menu(mb, tearoff=0, bg=PANEL, fg=FG,
                       activebackground=ACCENT, activeforeground=PRIMARY_FG)
        m_ai.add_command(label="Edit Selection   Ctrl+K", command=self.inline_edit)
        m_ai.add_command(label="Focus Chat       Ctrl+L", command=self._focus_chat)
        m_ai.add_separator()
        m_ai.add_command(label="Mode: Agent",  command=lambda: self.chat._set_mode("Agent"))
        m_ai.add_command(label="Mode: Ask",    command=lambda: self.chat._set_mode("Ask"))
        m_ai.add_command(label="Stop Agent",   command=lambda: self.chat.stop_agent())
        m_ai.add_separator()
        m_ai.add_command(label="Toggle Cursor Tab",
            command=lambda: self.chat.tab_enabled.set(not self.chat.tab_enabled.get()))
        m_ai.add_command(label="Toggle Review-mode edits",
            command=lambda: self.chat.review_mode.set(not self.chat.review_mode.get()))
        m_ai.add_command(label="Run Bugbot", command=lambda: self.chat.run_bugbot())
        m_ai.add_separator()
        m_ai.add_command(label="Reindex codebase",
            command=lambda: self._on_root_change(self.tree.root_path))
        m_ai.add_command(label="Reconnect LM Studio",
            command=lambda: self.chat.refresh_models())
        mb.add_cascade(label="Catsdk AI", menu=m_ai)

        m_help = tk.Menu(mb, tearoff=0, bg=PANEL, fg=FG,
                         activebackground=ACCENT, activeforeground=PRIMARY_FG)
        m_help.add_command(label="About", command=self._about)
        mb.add_cascade(label="Help", menu=m_help)
        self.config(menu=mb)

    def _about(self):
        messagebox.showinfo("About Catsdk",
            "Catsdk 1.0\n"
            "A Cursor 3 clone in tkinter, powered by LM Studio.\n\n"
            "• Cursor Tab autocomplete\n"
            "• Cmd/Ctrl+K inline edit\n"
            "• Cmd/Ctrl+L chat\n"
            "• Agent mode with native tool calling\n"
            "• @-mentions  •  Bugbot  •  .acrules\n\n"
            "meow~")

    def _build_titlebar(self):
        # Minimal toolbar
        bar = tk.Frame(self, bg=PANEL_DARK, height=32); bar.pack(fill="x")
        bar.pack_propagate(False)
        # Left: file actions
        left = tk.Frame(bar, bg=PANEL_DARK); left.pack(side="left", padx=4)
        for label, cmd in [("New", self.new_file),
                           ("Open", self.open_file),
                           ("Save", self.save_file)]:
            tk.Button(left, text=label, bg=PANEL_DARK, fg=MUTED, bd=0,
                activebackground=PANEL_HI, activeforeground=ACCENT, relief="flat",
                font=F(9, "bold"), padx=8, pady=4,
                command=cmd).pack(side="left", padx=1, pady=4)

        # Center: ai actions
        mid = tk.Frame(bar, bg=PANEL_DARK); mid.pack(side="left", padx=20)
        for label, cmd in [("✨ Edit ⌘K", self.inline_edit),
                           ("💬 Chat ⌘L", self._focus_chat),
                           ("◆ Agent",    self._focus_agent),
                           ("🐛 Bugbot",  lambda: self.chat.run_bugbot()),
                           ("▶ Run",      self.run_file)]:
            tk.Button(mid, text=label, bg=PANEL_DARK, fg=ACCENT, bd=0,
                activebackground=PANEL_HI, activeforeground=ACCENT_HI, relief="flat",
                font=F(9, "bold"), padx=8, pady=4,
                command=cmd).pack(side="left", padx=1, pady=4)

        # Right: title
        tk.Label(bar, text="Catsdk 1.0", bg=PANEL_DARK, fg=MUTED,
                 font=F(9, slant="italic")).pack(side="right", padx=12)

    def _build_layout(self):
        self.paned = tk.PanedWindow(self, orient="horizontal", bg=BORDER,
                                    sashwidth=2, bd=0)
        self.paned.pack(fill="both", expand=True)

        self.tree = FileTree(self.paned, on_open=self.open_path,
                             on_root_change=self._on_root_change)
        self.paned.add(self.tree, minsize=180, width=240)

        self.center = tk.Frame(self.paned, bg=BG)
        self.paned.add(self.center, minsize=400)

        # Tab bar (just current file)
        self.tabbar = tk.Frame(self.center, bg=PANEL_DARK, height=28)
        self.tabbar.pack(fill="x")
        self.tab_label = tk.Label(self.tabbar, text="", bg=BG, fg=ACCENT,
                                  font=F(10, "bold"), padx=14, pady=4)
        self.tab_label.pack(side="left")

        # Editor (hidden initially, welcome shown)
        self.editor_wrap = tk.Frame(self.center, bg=BG)
        self.editor_wrap.pack(fill="both", expand=True)
        self.editor = CodeEditor(self.editor_wrap, on_change=self._on_editor_change)
        self.welcome = Welcome(self.editor_wrap,
            on_open=self.open_file,
            on_open_folder=lambda: self.tree.choose_folder(),
            on_new=self.new_file)

        # Output pane
        outwrap = tk.Frame(self.center, bg=PANEL); outwrap.pack(fill="x")
        tk.Label(outwrap, text=" OUTPUT", bg=PANEL_DARK, fg=MUTED,
                 font=F(8, "bold"), anchor="w").pack(fill="x")
        self.output = tk.Text(outwrap, height=6, bg=BG, fg=FG,
            font=F(10, mono=True), bd=0, highlightthickness=0,
            padx=8, pady=4, insertbackground=ACCENT)
        self.output.pack(fill="x")

        self.chat = AIChatPanel(self.paned,
            get_editor=lambda: self.editor,
            get_project_root=lambda: self.tree.root_path,
            open_path_cb=self.open_path,
            reload_editor_cb=lambda: self.editor.reload_if_external(),
            codebase_index=self.index,
            project_rules_getter=lambda: self.project_rules)
        self.paned.add(self.chat, minsize=320, width=420)

    def _show_welcome(self):
        self.editor.pack_forget()
        self.welcome.pack(fill="both", expand=True)
        self.tab_label.config(text="")

    def _show_editor(self):
        self.welcome.pack_forget()
        self.editor.pack(fill="both", expand=True)

    def _build_statusbar(self):
        self.status = tk.Frame(self, bg=PANEL_DARK, height=22)
        self.status.pack(fill="x", side="bottom")
        self.status_left = tk.Label(self.status, text="Ready", bg=PANEL_DARK,
            fg=MUTED, font=F(8))
        self.status_left.pack(side="left", padx=8)
        self.status_lm = tk.Label(self.status, text="LM Studio: …",
            bg=PANEL_DARK, fg=MUTED, font=F(8))
        self.status_lm.pack(side="right", padx=8)
        self.status_idx = tk.Label(self.status, text="Index: —",
            bg=PANEL_DARK, fg=MUTED, font=F(8))
        self.status_idx.pack(side="right", padx=8)
        self.status_pos = tk.Label(self.status, text="Ln 1, Col 1",
            bg=PANEL_DARK, fg=MUTED, font=F(8))
        self.status_pos.pack(side="right", padx=8)

    def _bind_keys(self):
        for seq, fn in [
            ("<Control-n>", self.new_file), ("<Control-o>", self.open_file),
            ("<Control-s>", self.save_file),
            ("<Control-k>", self.inline_edit),
            ("<Control-l>", self._focus_chat),
            ("<Control-f>", self.find_dialog),
            ("<F5>",        self.run_file),
            ("<Command-n>", self.new_file), ("<Command-o>", self.open_file),
            ("<Command-s>", self.save_file),
            ("<Command-k>", self.inline_edit),
            ("<Command-l>", self._focus_chat),
            ("<Command-f>", self.find_dialog),
        ]:
            self.bind_all(seq, lambda e, f=fn: f())

    def _focus_chat(self): self.chat.input.focus_set()
    def _focus_agent(self):
        self.chat._set_mode("Agent"); self.chat.input.focus_set()

    def _on_root_change(self, root):
        self.status_idx.config(text="Index: building…")
        def work():
            self.index.build(root)
            self.project_rules = load_project_rules(root)
            def done():
                self.status_idx.config(text=f"Index: {len(self.index.files)} files")
                if self.project_rules:
                    self.chat._sys(f"📜 Loaded .acrules ({len(self.project_rules)} chars)")
            self.after(0, done)
        threading.Thread(target=work, daemon=True).start()

    def _tick_status(self):
        try:
            ln, col = self.editor.text.index("insert").split(".")
            self.status_pos.config(text=f"Ln {ln}, Col {int(col)+1}")
        except Exception: pass
        ok = self.chat.status_dot.cget("fg") == OK
        self.status_lm.config(text=f"LM Studio: {'connected' if ok else 'offline'}")
        self.after(400, self._tick_status)

    def _on_editor_change(self):
        if self.editor.path:
            name = os.path.basename(self.editor.path)
            self.tab_label.config(text=f" {'● ' if self.editor.dirty else ''}{name} ")

    def open_path(self, path):
        if self.editor.load_file(path):
            self._show_editor()
            self._on_editor_change()
            self.status_left.config(text=f"Opened {path}")

    def new_file(self):
        self._show_editor()
        self.editor.text.delete("1.0","end")
        self.editor.path = None; self.editor.dirty = False
        self.tab_label.config(text=" untitled ")
        self.status_left.config(text="New file")
        self.editor.text.focus_set()

    def open_file(self):
        path = filedialog.askopenfilename()
        if path: self.open_path(path)

    def save_file(self):
        if not self.editor.path: return self.save_as()
        if self.editor.save_file():
            self._on_editor_change()
            self.status_left.config(text=f"Saved {self.editor.path}")
            try:
                rel = os.path.relpath(self.editor.path, self.tree.root_path)
                self.index.files[rel] = self.editor.get_text()
                self.index.tokens[rel] = set(re.findall(
                    r"[A-Za-z_][A-Za-z0-9_]{2,}",
                    self.index.files[rel].lower()))
            except Exception: pass

    def save_as(self):
        path = filedialog.asksaveasfilename(defaultextension=".py")
        if path:
            if self.editor.save_file(path):
                self._on_editor_change()
                self.status_left.config(text=f"Saved {path}")

    def find_dialog(self):
        dlg = tk.Toplevel(self); dlg.title("Find")
        dlg.configure(bg=PANEL_DARK); dlg.geometry("360x70")
        tk.Label(dlg, text="Find:", bg=PANEL_DARK, fg=ACCENT,
                 font=F(10, "bold")).pack(side="left", padx=6)
        e = tk.Entry(dlg, bg=PANEL_HI, fg=FG, insertbackground=ACCENT,
            font=F(11, mono=True), bd=0, highlightthickness=1,
            highlightbackground=BORDER)
        e.pack(side="left", fill="x", expand=True, padx=4, pady=8); e.focus_set()
        def do_find():
            self.editor.text.tag_remove("find","1.0","end")
            self.editor.text.tag_configure("find", background="#664400")
            q = e.get()
            if not q: return
            start = "1.0"; count = 0
            while True:
                pos = self.editor.text.search(q, start, stopindex="end")
                if not pos: break
                end = f"{pos}+{len(q)}c"
                self.editor.text.tag_add("find", pos, end)
                start = end; count += 1
            self.status_left.config(text=f"Found {count} match(es)")
        styled_btn(dlg, "Go", do_find, primary=True).pack(side="right", padx=6)
        e.bind("<Return>", lambda ev: do_find())

    def inline_edit(self):
        if not self.editor.get_selection():
            messagebox.showinfo("Catsdk","Select code first, then ⌘K"); return
        InlinePrompt(self, self.editor, self.chat.client, self.project_rules)

    def run_file(self):
        if not self.editor.path:
            messagebox.showinfo("Run","Save file first"); return
        self.save_file()
        self.output.delete("1.0","end")
        self.output.insert("end", f"$ python {self.editor.path}\n")
        def work():
            try:
                p = subprocess.run([sys.executable, self.editor.path],
                    capture_output=True, text=True, timeout=30)
                out = p.stdout + (("\n"+p.stderr) if p.stderr else "")
            except Exception as e:
                out = f"[run error] {e}"
            self.after(0, lambda: self.output.insert("end", out + "\n"))
        threading.Thread(target=work, daemon=True).start()


if __name__ == "__main__":
    Catsdk().mainloop()
