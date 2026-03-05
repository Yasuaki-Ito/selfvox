"""SelfVox GUI ランチャー - customtkinter ベースのグラフィカルインターフェース"""

import ctypes
import json
import math
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import urllib.parse
import urllib.request
import webbrowser
import winsound
from pathlib import Path
from tkinter import filedialog, messagebox

from PIL import Image

# Windows 高DPI対応 (GUI生成前に呼ぶ)
try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)  # Per-Monitor DPI Aware V2
except Exception:
    try:
        ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass

import customtkinter as ctk

from version import VERSION

# ---------------------------------------------------------------------------
# Font helpers — 全体を "Yu Gothic UI" で統一
# ---------------------------------------------------------------------------

_FONT = "Yu Gothic UI"
_MONO = "Consolas"


def _ui_font(size: int, bold: bool = False):
    return ctk.CTkFont(family=_FONT, size=size,
                        weight="bold" if bold else "normal")


def _mono_font(size: int):
    return ctk.CTkFont(family=_MONO, size=size)


# ---------------------------------------------------------------------------
# HTTP Client
# ---------------------------------------------------------------------------

class ServerAPIClient:
    """ローカルSelfVoxサーバーとのHTTP通信"""

    def __init__(self, port: int = 50021):
        self.base_url = f"http://localhost:{port}"

    def set_port(self, port: int):
        self.base_url = f"http://localhost:{port}"

    # -- Synthesis --

    def audio_query(self, text: str, speaker: int) -> dict:
        params = urllib.parse.urlencode({"text": text, "speaker": speaker})
        url = f"{self.base_url}/audio_query?{params}"
        req = urllib.request.Request(url, method="POST", data=b"")
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())

    def synthesize(self, speaker: int, query: dict) -> bytes:
        url = f"{self.base_url}/synthesis?speaker={speaker}"
        data = json.dumps(query).encode("utf-8")
        req = urllib.request.Request(
            url, data=data, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.read()


# ---------------------------------------------------------------------------
# GUI
# ---------------------------------------------------------------------------

class SelfVoxGUI:
    """メインGUIアプリケーション"""

    # --- 状態定数 ---
    STATE_IDLE = "idle"
    STATE_SETTING_UP = "setting_up"
    STATE_LOADING = "loading_model"
    STATE_RUNNING = "running"
    STATE_STOPPED = "stopped"

    STATE_CONFIG = {
        "idle":          {"color": "#9E9E9E", "text": "待機中",              "start": True,  "stop": False, "tabs": False},
        "setting_up":    {"color": "#FFC107", "text": "セットアップ中...",    "start": False, "stop": False, "tabs": False},
        "loading_model": {"color": "#FF9800", "text": "モデル読み込み中...", "start": False, "stop": True,  "tabs": False},
        "running":       {"color": "#4CAF50", "text": "サーバー稼働中",     "start": False, "stop": True,  "tabs": True},
        "stopped":       {"color": "#F44336", "text": "停止",               "start": True,  "stop": False, "tabs": False},
    }

    LANGUAGES = [
        "Japanese", "Chinese", "English", "Korean", "French",
        "Spanish", "German", "Italian", "Portuguese", "Russian",
    ]

    def __init__(self):
        ctk.set_appearance_mode("light")
        ctk.set_default_color_theme("blue")

        self.root = ctk.CTk()
        self.state = self.STATE_IDLE
        self.server_proc: subprocess.Popen | None = None
        self.worker_thread: threading.Thread | None = None
        self.config = self._load_config()

        self.api = ServerAPIClient(self.config.get("port", 50021))
        self._selected_audio_path: str | None = None
        self._last_synth_wav: bytes | None = None
        self._last_synth_path: str | None = None
        self._voices_data: list[dict] = []
        self._playing_btn = None

        self._build_ui()
        self._apply_state()
        self._refresh_voice_list()
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

    def run(self):
        self.root.mainloop()

    # ===================================================================
    # UI 構築
    # ===================================================================

    def _build_ui(self):
        self.root.title("SelfVox")
        self.root.geometry("900x700")
        self.root.minsize(700, 550)

        ico_path = self._app_dir() / "selfvox.ico"
        if ico_path.exists():
            self.root.iconbitmap(str(ico_path))

        self._build_header()
        self._build_buttons()
        self._build_tabs()

    def _build_header(self):
        self.header = ctk.CTkFrame(self.root, fg_color="#1a237e", corner_radius=0)
        self.header.pack(fill="x")
        header = self.header

        inner = ctk.CTkFrame(header, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=14)

        logo_path = self._app_dir() / "selfvox.png"
        if logo_path.exists():
            logo_img = ctk.CTkImage(
                light_image=Image.open(logo_path), size=(36, 36))
            ctk.CTkLabel(inner, image=logo_img, text="").pack(
                side="left", padx=(0, 10))

        ctk.CTkLabel(
            inner, text=f"SelfVox v{VERSION}",
            font=_ui_font(20, True),
            text_color="white",
        ).pack(side="left")

        status_frame = ctk.CTkFrame(inner, fg_color="transparent")
        status_frame.pack(side="right")

        self.status_dot = ctk.CTkLabel(
            status_frame, text="\u25cf", width=20,
            font=_ui_font(16), text_color="#9E9E9E",
        )
        self.status_dot.pack(side="left", padx=(0, 6))

        self.status_label = ctk.CTkLabel(
            status_frame, text="待機中",
            font=_ui_font(13), text_color="white",
        )
        self.status_label.pack(side="left")

    def _build_buttons(self):
        btn_frame = ctk.CTkFrame(self.root, fg_color="transparent")
        btn_frame.pack(fill="x", padx=20, pady=10)

        self.start_btn = ctk.CTkButton(
            btn_frame, text="Start Server", command=self._on_start,
            fg_color="#4CAF50", hover_color="#43a047",
            font=_ui_font(13, True),
            width=140, height=36,
        )
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ctk.CTkButton(
            btn_frame, text="Stop Server", command=self._on_stop,
            fg_color="#F44336", hover_color="#e53935",
            font=_ui_font(13, True),
            width=140, height=36,
        )
        self.stop_btn.pack(side="left", padx=(0, 8))

        # サーバーURL (起動後に表示)
        self.url_entry = ctk.CTkEntry(
            btn_frame, width=260, height=36, font=_ui_font(13),
            state="readonly", fg_color="#f0f0f0", border_width=1,
            border_color="#cccccc", text_color="#1a237e",
        )
        self._url_var = ctk.StringVar()
        self.url_entry.configure(textvariable=self._url_var)

        self.copy_url_btn = ctk.CTkButton(
            btn_frame, text="Copy", width=56, height=36,
            font=_ui_font(12), fg_color="#1a237e", hover_color="#283593",
            command=self._copy_url,
        )

        self.open_browser_btn = ctk.CTkButton(
            btn_frame, text="Browser", width=72, height=36,
            font=_ui_font(12), fg_color="#1a237e", hover_color="#283593",
            command=self._open_browser,
        )

        ctk.CTkButton(
            btn_frame, text="\u2699 Settings", command=self._open_settings,
            fg_color="transparent", hover_color="#e0e0e0",
            text_color="#222222",
            font=_ui_font(12),
            width=100, height=36, border_width=1, border_color="#999999",
        ).pack(side="right")

    def _build_tabs(self):
        self.tabview = ctk.CTkTabview(self.root)
        self.tabview.pack(fill="both", expand=True, padx=20, pady=(0, 16))

        self.tab_voices = self.tabview.add("ボイス管理")
        self.tab_synth = self.tabview.add("音声合成")
        self.tab_log = self.tabview.add("ログ")

        self._build_voice_tab()
        self._build_synth_tab()
        self._build_log_tab()

        # 起動時はログタブを表示
        self.tabview.set("ログ")

    # --- ボイス管理タブ ---

    def _build_voice_tab(self):
        # サーバー未起動時のオーバーレイ
        container = ctk.CTkFrame(self.tab_voices, fg_color="transparent")
        container.pack(fill="both", expand=True)
        container.grid_columnconfigure(0, weight=2)
        container.grid_columnconfigure(1, weight=3)
        container.grid_rowconfigure(0, weight=1)

        # 左: 登録フォーム
        left = ctk.CTkFrame(container)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 5), pady=0)

        ctk.CTkLabel(
            left, text="ボイス登録",
            font=_ui_font(15, True),
        ).pack(anchor="w", padx=14, pady=(14, 8))

        form = ctk.CTkFrame(left, fg_color="transparent")
        form.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        # ボイス名
        ctk.CTkLabel(form, text="ボイス名:", font=_ui_font(13)).pack(
            anchor="w", pady=(0, 2))
        self.voice_name_entry = ctk.CTkEntry(
            form, placeholder_text="(例) ナレーター1号",
            font=_ui_font(13),
        )
        self.voice_name_entry.pack(fill="x", pady=(0, 8))

        # リファレンス音声
        ctk.CTkLabel(form, text="リファレンス音声:", font=_ui_font(13)).pack(
            anchor="w", pady=(0, 2))
        audio_row = ctk.CTkFrame(form, fg_color="transparent")
        audio_row.pack(fill="x", pady=(0, 4))

        self.browse_btn = ctk.CTkButton(
            audio_row, text="ファイル選択...",
            command=self._browse_audio_file, width=120,
            font=_ui_font(12),
        )
        self.browse_btn.pack(side="left", padx=(0, 8))

        self.preview_btn = ctk.CTkButton(
            audio_row, text="\u25b6 再生",
            command=self._play_audio_preview, width=70,
            font=_ui_font(12), state="disabled",
        )
        self.preview_btn.pack(side="left")

        self.audio_path_label = ctk.CTkLabel(
            form, text="未選択", text_color="#888888",
            font=_ui_font(11),
        )
        self.audio_path_label.pack(anchor="w", pady=(0, 8))

        # リファレンステキスト
        ctk.CTkLabel(form, text="リファレンスのテキスト:", font=_ui_font(13)).pack(
            anchor="w", pady=(0, 2))
        self.ref_text_entry = ctk.CTkTextbox(
            form, height=80, font=_ui_font(13),
        )
        self.ref_text_entry.pack(fill="x", pady=(0, 8))

        # 言語
        ctk.CTkLabel(form, text="言語:", font=_ui_font(13)).pack(
            anchor="w", pady=(0, 2))
        self.lang_dropdown = ctk.CTkComboBox(
            form, values=self.LANGUAGES, state="readonly",
            font=_ui_font(13),
        )
        self.lang_dropdown.set("Japanese")
        self.lang_dropdown.pack(fill="x", pady=(0, 12))

        # 保存ボタン
        self.save_voice_btn = ctk.CTkButton(
            form, text="ボイスを保存", command=self._save_voice,
            fg_color="#4CAF50", hover_color="#43a047",
            font=_ui_font(13, True), height=36,
        )
        self.save_voice_btn.pack(fill="x", pady=(0, 6))

        self.voice_status = ctk.CTkLabel(
            form, text="", text_color="#888888",
            font=_ui_font(12), wraplength=240,
        )
        self.voice_status.pack(anchor="w")

        # 右: ボイス一覧
        right = ctk.CTkFrame(container)
        right.grid(row=0, column=1, sticky="nsew", padx=(5, 0), pady=0)

        title_row = ctk.CTkFrame(right, fg_color="transparent")
        title_row.pack(fill="x", padx=14, pady=(14, 8))
        ctk.CTkLabel(
            title_row, text="登録済みボイス",
            font=_ui_font(15, True),
        ).pack(side="left")
        ctk.CTkButton(
            title_row, text="↻ 更新", width=60, height=26,
            font=_ui_font(11), fg_color="transparent",
            hover_color="#e0e0e0", text_color="#333333",
            border_width=1, border_color="#999999",
            command=self._refresh_voice_list,
        ).pack(side="right")

        self.voice_list_frame = ctk.CTkScrollableFrame(right)
        self.voice_list_frame.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        self.voice_list_placeholder = ctk.CTkLabel(
            self.voice_list_frame,
            text="ボイスが未登録です。\n左のフォームから登録してください。",
            text_color="#999999", font=_ui_font(12),
        )
        self.voice_list_placeholder.pack(pady=20)

    # --- 音声合成タブ ---

    def _build_synth_tab(self):
        # サーバー未起動時のオーバーレイ
        self._synth_overlay = ctk.CTkFrame(self.tab_synth, fg_color="transparent")
        self._synth_overlay.pack(fill="both", expand=True)
        ctk.CTkLabel(
            self._synth_overlay,
            text="サーバーを起動すると音声合成が利用できます",
            font=_ui_font(14), text_color="#888888",
        ).place(relx=0.5, rely=0.45, anchor="center")

        self._synth_content = ctk.CTkFrame(self.tab_synth, fg_color="transparent")
        frame = self._synth_content

        # スピーカー選択
        ctk.CTkLabel(frame, text="スピーカー:", font=_ui_font(13)).pack(
            anchor="w", pady=(0, 2))
        self.speaker_dropdown = ctk.CTkComboBox(
            frame, values=["-- サーバー起動後に選択 --"],
            state="readonly", width=350, font=_ui_font(13),
        )
        self.speaker_dropdown.pack(anchor="w", pady=(0, 12))

        # テキスト入力
        ctk.CTkLabel(frame, text="テキスト:", font=_ui_font(13)).pack(
            anchor="w", pady=(0, 2))
        self.synth_text = ctk.CTkTextbox(
            frame, height=100, font=_ui_font(13),
        )
        self.synth_text.insert("0.0", "こんにちは。音声合成のテストです。")
        self.synth_text.pack(fill="x", pady=(0, 12))

        # スライダー
        slider_frame = ctk.CTkFrame(frame, fg_color="transparent")
        slider_frame.pack(fill="x", pady=(0, 12))
        slider_frame.grid_columnconfigure(0, weight=1)
        slider_frame.grid_columnconfigure(1, weight=1)

        # 速度
        speed_box = ctk.CTkFrame(slider_frame, fg_color="transparent")
        speed_box.grid(row=0, column=0, sticky="ew", padx=(0, 12))

        speed_label_row = ctk.CTkFrame(speed_box, fg_color="transparent")
        speed_label_row.pack(fill="x")
        ctk.CTkLabel(speed_label_row, text="速度:", font=_ui_font(13)).pack(
            side="left")
        self.speed_val_label = ctk.CTkLabel(
            speed_label_row, text="1.0x", font=_ui_font(13),
        )
        self.speed_val_label.pack(side="right")

        self.speed_slider = ctk.CTkSlider(
            speed_box, from_=0.5, to=2.0, number_of_steps=15,
            command=self._on_speed_change,
        )
        self.speed_slider.set(1.0)
        self.speed_slider.pack(fill="x", pady=(2, 0))

        # 音量
        vol_box = ctk.CTkFrame(slider_frame, fg_color="transparent")
        vol_box.grid(row=0, column=1, sticky="ew", padx=(12, 0))

        vol_label_row = ctk.CTkFrame(vol_box, fg_color="transparent")
        vol_label_row.pack(fill="x")
        ctk.CTkLabel(vol_label_row, text="音量:", font=_ui_font(13)).pack(
            side="left")
        self.volume_val_label = ctk.CTkLabel(
            vol_label_row, text="1.0x", font=_ui_font(13),
        )
        self.volume_val_label.pack(side="right")

        self.volume_slider = ctk.CTkSlider(
            vol_box, from_=0.1, to=2.0, number_of_steps=19,
            command=self._on_volume_change,
        )
        self.volume_slider.set(1.0)
        self.volume_slider.pack(fill="x", pady=(2, 0))

        # ボタン行
        btn_row = ctk.CTkFrame(frame, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, 8))

        self.synthesize_btn = ctk.CTkButton(
            btn_row, text="合成", command=self._synthesize,
            fg_color="#e65100", hover_color="#bf360c",
            font=_ui_font(13, True),
            width=120, height=36,
        )
        self.synthesize_btn.pack(side="left", padx=(0, 8))

        self.play_synth_btn = ctk.CTkButton(
            btn_row, text="\u25b6 再生", command=self._play_synthesized,
            font=_ui_font(13), width=80, height=36,
            state="disabled",
        )
        self.play_synth_btn.pack(side="left", padx=(0, 8))

        self.save_wav_btn = ctk.CTkButton(
            btn_row, text="WAV保存", command=self._save_synthesized_wav,
            fg_color="#2196F3", hover_color="#1976D2",
            font=_ui_font(13), width=100, height=36,
            state="disabled",
        )
        self.save_wav_btn.pack(side="left")

        # ステータス
        self.synth_status = ctk.CTkLabel(
            frame, text="", text_color="#666666",
            font=_ui_font(12),
        )
        self.synth_status.pack(anchor="w")

    # --- ログタブ ---

    def _build_log_tab(self):
        self.log_text = ctk.CTkTextbox(
            self.tab_log, state="disabled",
            font=_mono_font(12),
            fg_color="#1e1e1e", text_color="#d4d4d4",
            wrap="word", corner_radius=8,
        )
        self.log_text.pack(fill="both", expand=True)

    # ===================================================================
    # 状態遷移
    # ===================================================================

    def _set_state(self, new_state: str):
        self.state = new_state
        self._apply_state()

    def _apply_state(self):
        cfg = self.STATE_CONFIG[self.state]
        self.status_dot.configure(text_color=cfg["color"])
        self.status_label.configure(text=cfg["text"])
        self.start_btn.configure(state="normal" if cfg["start"] else "disabled")
        self.stop_btn.configure(state="normal" if cfg["stop"] else "disabled")

        # 音声合成タブの表示切り替え
        if cfg["tabs"]:
            self._synth_overlay.pack_forget()
            self._synth_content.pack(fill="both", expand=True, padx=20, pady=10)
        else:
            self._synth_content.pack_forget()
            self._synth_overlay.pack(fill="both", expand=True)

        # サーバーURL表示
        if self.state == self.STATE_RUNNING:
            port = self.config.get("port", 50021)
            self._url_var.set(f"http://localhost:{port}")
            self.url_entry.pack(side="left", padx=(0, 4))
            self.copy_url_btn.pack(side="left", padx=(0, 4))
            self.open_browser_btn.pack(side="left", padx=(0, 8))
        else:
            self.url_entry.pack_forget()
            self.copy_url_btn.pack_forget()
            self.open_browser_btn.pack_forget()
            self._url_var.set("")

        # サーバー起動完了時
        if self.state == self.STATE_RUNNING:
            self._refresh_voice_list()
            self._refresh_speaker_dropdown()
            self.tabview.set("音声合成")

    # ===================================================================
    # ログ出力 (スレッドセーフ)
    # ===================================================================

    def _append_log(self, text: str):
        self.root.after(0, self._append_log_main, text)

    def _append_log_main(self, text: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    # ===================================================================
    # サーバー Start / Stop
    # ===================================================================

    def _on_start(self):
        self.tabview.set("ログ")
        self._open_progress()
        self.worker_thread = threading.Thread(target=self._worker_run, daemon=True)
        self.worker_thread.start()

    def _on_stop(self):
        if self.server_proc and self.server_proc.poll() is None:
            self._append_log("サーバーを停止中...")
            self.server_proc.terminate()
            try:
                self.server_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.server_proc.kill()
                self.server_proc.wait()
            self._append_log("サーバーを停止しました")
            self._set_state(self.STATE_STOPPED)

    def _on_close(self):
        if self.server_proc and self.server_proc.poll() is None:
            if messagebox.askyesno("SelfVox", "サーバーが稼働中です。終了しますか？"):
                self._on_stop()
                self.root.destroy()
        else:
            self.root.destroy()

    def _on_server_ready(self):
        port = self.config.get("port", 50021)
        self.api.set_port(port)

    # ===================================================================
    # 起動プログレスポップアップ
    # ===================================================================

    _SPINNER_CHARS = [
        "\u280b", "\u2819", "\u2839", "\u2838",
        "\u283c", "\u2834", "\u2826", "\u2827",
        "\u2807", "\u280f",
    ]  # ⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏

    _STEPS_WITH_SETUP = [
        ("setup", "環境セットアップ"),
        ("start", "サーバー起動"),
        ("model", "モデル読み込み"),
        ("ready", "準備完了"),
    ]
    _STEPS_NO_SETUP = [
        ("start", "サーバー起動"),
        ("model", "モデル読み込み"),
        ("ready", "準備完了"),
    ]

    def _open_progress(self):
        from launcher import is_setup_needed
        self._progress_steps = (
            self._STEPS_WITH_SETUP if is_setup_needed()
            else self._STEPS_NO_SETUP
        )
        self._progress_current = None
        self._progress_animating = True
        self._spinner_idx = 0

        dlg = ctk.CTkToplevel(self.root)
        dlg.title("")
        dlg.overrideredirect(True)  # タイトルバーなし
        dlg.attributes("-topmost", True)
        self._progress_dlg = dlg

        # ウィンドウを親の中央に配置
        w, h = 460, 340
        dlg.geometry(f"{w}x{h}")
        dlg.update_idletasks()
        px = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        py = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        dlg.geometry(f"+{px}+{py}")

        # 外枠 (影風)
        outer = ctk.CTkFrame(dlg, fg_color="#e0e0e0", corner_radius=16)
        outer.pack(fill="both", expand=True, padx=2, pady=2)

        inner = ctk.CTkFrame(outer, fg_color="#ffffff", corner_radius=14)
        inner.pack(fill="both", expand=True, padx=1, pady=1)

        # ヘッダー帯
        hdr = ctk.CTkFrame(inner, fg_color="#1a237e", corner_radius=0,
                            height=56)
        hdr.pack(fill="x")
        hdr.pack_propagate(False)

        hdr_inner = ctk.CTkFrame(hdr, fg_color="transparent")
        hdr_inner.pack(fill="x", padx=20)
        hdr_inner.pack_propagate(False)
        hdr_inner.configure(height=56)

        ctk.CTkLabel(
            hdr_inner, text="SelfVox", text_color="white",
            font=_ui_font(17, True),
        ).place(x=0, rely=0.5, anchor="w")

        self._progress_spinner_label = ctk.CTkLabel(
            hdr_inner, text="", text_color="#80cbc4",
            font=_mono_font(20),
        )
        self._progress_spinner_label.place(relx=1.0, rely=0.5, anchor="e")

        # ステップリスト
        body = ctk.CTkFrame(inner, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=28, pady=(20, 10))

        self._progress_labels: dict[str, tuple] = {}
        self._progress_bars: dict[str, ctk.CTkFrame] = {}

        for i, (key, label_text) in enumerate(self._progress_steps):
            row = ctk.CTkFrame(body, fg_color="transparent")
            row.pack(fill="x", pady=(0, 12))

            # 番号バッジ
            badge = ctk.CTkFrame(row, width=32, height=32,
                                 corner_radius=16, fg_color="#e8eaf6")
            badge.pack(side="left")
            badge.pack_propagate(False)
            badge_lbl = ctk.CTkLabel(
                badge, text=str(i + 1), width=0, height=0,
                font=_ui_font(13, True),
                text_color="#5c6bc0",
            )
            badge_lbl.place(relx=0.5, rely=0.5, anchor="center")

            # テキスト + プログレスバー
            text_col = ctk.CTkFrame(row, fg_color="transparent")
            text_col.pack(side="left", fill="x", expand=True, padx=(12, 0))

            lbl = ctk.CTkLabel(
                text_col, text=label_text, anchor="w",
                font=_ui_font(14), text_color="#aaaaaa",
            )
            lbl.pack(anchor="w")

            # 小さなプログレスバー
            bar_bg = ctk.CTkFrame(text_col, height=4, fg_color="#eeeeee",
                                  corner_radius=2)
            bar_bg.pack(fill="x", pady=(3, 0))
            bar_fill = ctk.CTkFrame(bar_bg, height=4, fg_color="#eeeeee",
                                    corner_radius=2, width=0)
            bar_fill.place(x=0, y=0, relheight=1.0)

            self._progress_labels[key] = (badge, badge_lbl, lbl)
            self._progress_bars[key] = (bar_bg, bar_fill)

        # ステータスメッセージ
        self._progress_status = ctk.CTkLabel(
            inner, text="", font=_ui_font(12),
            text_color="#888888", wraplength=400,
        )
        self._progress_status.pack(padx=20, pady=(0, 16))

        # アニメーション開始
        self._animate_spinner()

    def _animate_spinner(self):
        if not hasattr(self, "_progress_dlg"):
            return
        if not self._progress_dlg.winfo_exists():
            return
        if not self._progress_animating:
            return

        char = self._SPINNER_CHARS[self._spinner_idx % len(self._SPINNER_CHARS)]
        self._progress_spinner_label.configure(text=char)
        self._spinner_idx += 1
        self._progress_dlg.after(100, self._animate_spinner)

    def _animate_bar(self, step_key: str, phase: float = 0.0):
        """実行中ステップのバーを左→右にスイープ"""
        if not hasattr(self, "_progress_dlg"):
            return
        if not self._progress_dlg.winfo_exists():
            return
        if self._progress_current != step_key:
            return

        bar_bg, bar_fill = self._progress_bars[step_key]
        total_w = bar_bg.winfo_width()
        if total_w > 1:
            bar_w = int(total_w * 0.35)
            x = int((total_w + bar_w) * phase) - bar_w
            x = max(0, min(x, total_w))
            w = min(bar_w, total_w - x)
            bar_fill.place_configure(x=x, width=w)

        phase += 0.02
        if phase > 1.0:
            phase = 0.0
        self._progress_dlg.after(30, self._animate_bar, step_key, phase)

    def _update_progress(self, step_key: str):
        self.root.after(0, self._update_progress_main, step_key)

    def _update_progress_main(self, step_key: str):
        if not hasattr(self, "_progress_dlg") or not self._progress_dlg.winfo_exists():
            return

        self._progress_current = step_key
        found_current = False

        for key, _label_text in self._progress_steps:
            badge, badge_lbl, text_lbl = self._progress_labels[key]
            bar_bg, bar_fill = self._progress_bars[key]

            if key == step_key:
                found_current = True
                badge.configure(fg_color="#ff9800")
                badge_lbl.configure(text_color="#ffffff")
                text_lbl.configure(
                    text_color="#333333",
                    font=_ui_font(14, True),
                )
                bar_fill.configure(fg_color="#ff9800")
                # バーアニメーション開始
                self._progress_dlg.after(100, self._animate_bar, key)

            elif not found_current:
                badge.configure(fg_color="#4CAF50")
                badge_lbl.configure(text_color="#ffffff",
                                    text="\u2713",
                                    font=_ui_font(12, True))
                text_lbl.configure(
                    text_color="#4CAF50",
                    font=_ui_font(14),
                )
                bar_fill.configure(fg_color="#4CAF50")
                bar_fill.place_configure(relwidth=1.0, width=0)

            else:
                badge.configure(fg_color="#e8eaf6")
                badge_lbl.configure(text_color="#5c6bc0")
                text_lbl.configure(
                    text_color="#aaaaaa",
                    font=_ui_font(14),
                )
                bar_fill.configure(fg_color="#eeeeee")
                bar_fill.place_configure(width=0)

    def _complete_progress(self):
        self.root.after(0, self._complete_progress_main)

    def _complete_progress_main(self):
        if not hasattr(self, "_progress_dlg") or not self._progress_dlg.winfo_exists():
            return

        self._progress_animating = False
        self._progress_current = None
        self._progress_spinner_label.configure(text="\u2713")

        for key, _label_text in self._progress_steps:
            badge, badge_lbl, text_lbl = self._progress_labels[key]
            bar_bg, bar_fill = self._progress_bars[key]
            badge.configure(fg_color="#4CAF50")
            badge_lbl.configure(text_color="#ffffff", text="\u2713",
                                font=_ui_font(12, True))
            text_lbl.configure(text_color="#4CAF50", font=_ui_font(14))
            bar_fill.configure(fg_color="#4CAF50")
            bar_fill.place_configure(relwidth=1.0, width=0)

        self._progress_status.configure(
            text="サーバーの準備ができました!", text_color="#1b5e20",
            font=_ui_font(14, True),
        )

        self._progress_dlg.after(2500, self._close_progress)

    def _close_progress(self):
        if hasattr(self, "_progress_dlg") and self._progress_dlg.winfo_exists():
            self._progress_dlg.destroy()

    def _fail_progress(self, err: str):
        self.root.after(0, self._fail_progress_main, err)

    _ERROR_HINTS = [
        ("CUDA error",             "GPUドライバを最新版に更新してください。またはGPUが非対応の可能性があります。"),
        ("no kernel image",        "PyTorchがこのGPUに対応していません。.venv を削除して再セットアップしてください。"),
        ("out of memory",          "GPUメモリが不足しています。他のアプリを閉じてから再試行してください。"),
        ("Address already in use", "ポートが既に使用されています。Settingsでポート番号を変更するか、既存のプロセスを終了してください。"),
        ("ModuleNotFoundError",    "必要なライブラリが見つかりません。.venv を削除して再セットアップしてください。"),
        ("Connection",             "ネットワーク接続を確認してください。初回セットアップにはインターネット接続が必要です。"),
        ("disk",                   "ディスク容量を確認してください。モデルのダウンロードに約5GBの空き容量が必要です。"),
    ]

    def _get_error_hint(self, err: str) -> str:
        err_lower = err.lower()
        for keyword, hint in self._ERROR_HINTS:
            if keyword.lower() in err_lower:
                return hint
        return "ログタブで詳細を確認してください。"

    def _fail_progress_main(self, err: str):
        if not hasattr(self, "_progress_dlg") or not self._progress_dlg.winfo_exists():
            return

        self._progress_animating = False
        self._progress_current = None
        self._progress_spinner_label.configure(text="\u2717", text_color="#ef5350")

        # 失敗ステップをマーク
        found_fail = False
        for key, _label_text in self._progress_steps:
            badge, badge_lbl, text_lbl = self._progress_labels[key]
            bar_bg, bar_fill = self._progress_bars[key]
            if not found_fail and badge_lbl.cget("text_color") != "#ffffff":
                # まだ完了してないステップ = 失敗箇所
                found_fail = True
                badge.configure(fg_color="#F44336")
                badge_lbl.configure(text_color="#ffffff", text="\u2717",
                                    font=_ui_font(12, True))
                text_lbl.configure(text_color="#F44336")
                bar_fill.configure(fg_color="#F44336")
                bar_fill.place_configure(relwidth=1.0, width=0)

        hint = self._get_error_hint(err)
        self._progress_status.configure(
            text=f"{hint}", text_color="#c62828",
            font=_ui_font(13),
        )

        # エラー時はウィンドウを広げて閉じるボタンを表示
        w, h = 460, 380
        px = self.root.winfo_x() + (self.root.winfo_width() - w) // 2
        py = self.root.winfo_y() + (self.root.winfo_height() - h) // 2
        self._progress_dlg.geometry(f"{w}x{h}+{px}+{py}")

        ctk.CTkButton(
            self._progress_dlg.winfo_children()[0].winfo_children()[0],
            text="閉じる", command=self._close_progress,
            fg_color="#c62828", hover_color="#b71c1c",
            font=_ui_font(13), width=100, height=32,
        ).pack(pady=(0, 12))

    # ===================================================================
    # ワーカースレッド
    # ===================================================================

    def _worker_run(self):
        from launcher import is_setup_needed, setup_environment, start_server_process

        try:
            if is_setup_needed():
                self._update_progress("setup")
                self.root.after(0, self._set_state, self.STATE_SETTING_UP)
                setup_environment(log_callback=self._append_log)

            self._update_progress("start")
            self.root.after(0, self._set_state, self.STATE_LOADING)
            self._append_log("SelfVox サーバーを起動中...")

            port = self.config.get("port", 50021)
            self.server_proc = start_server_process(port=port)

            model_step_done = False
            for line in self.server_proc.stdout:
                line = line.rstrip()
                if "\r" in line:
                    line = line.rsplit("\r", 1)[-1]
                if line:
                    self._append_log(line)
                # モデル読み込み開始を検知
                if not model_step_done and ("loading" in line.lower()
                        or "model" in line.lower()
                        or "Qwen" in line):
                    self._update_progress("model")
                    model_step_done = True
                if "Server ready" in line:
                    self._update_progress("ready")
                    self._complete_progress()
                    self.root.after(0, self._set_state, self.STATE_RUNNING)
                    self.root.after(0, self._on_server_ready)

            self.server_proc.wait()
            rc = self.server_proc.returncode
            self._append_log(f"サーバープロセス終了 (code={rc})")
            if rc != 0:
                self._fail_progress(f"サーバーが異常終了しました (code={rc})")
            self.root.after(0, self._set_state, self.STATE_STOPPED)

        except Exception as e:
            self._append_log(f"エラー: {e}")
            self._fail_progress(str(e))
            self.root.after(0, self._set_state, self.STATE_STOPPED)

    # ===================================================================
    # ボイス管理
    # ===================================================================

    def _browse_audio_file(self):
        path = filedialog.askopenfilename(
            title="リファレンス音声を選択",
            filetypes=[
                ("Audio Files", "*.wav *.mp3 *.ogg *.flac"),
                ("WAV", "*.wav"), ("MP3", "*.mp3"),
                ("OGG", "*.ogg"), ("FLAC", "*.flac"),
                ("All Files", "*.*"),
            ],
        )
        if path:
            self._selected_audio_path = path
            self.audio_path_label.configure(
                text=Path(path).name, text_color="#333333")
            is_wav = path.lower().endswith(".wav")
            self.preview_btn.configure(
                state="normal" if is_wav else "disabled")

    def _play_audio_preview(self):
        if self._selected_audio_path and os.path.exists(self._selected_audio_path):
            self._play_wav(self._selected_audio_path, toggle_btn=self.preview_btn)

    def _save_voice(self):
        name = self.voice_name_entry.get().strip()
        ref_text = self.ref_text_entry.get("0.0", "end").strip()
        language = self.lang_dropdown.get()
        audio_path = self._selected_audio_path

        if not name:
            self.voice_status.configure(
                text="ボイス名を入力してください", text_color="#c62828")
            return
        if not audio_path:
            self.voice_status.configure(
                text="音声ファイルを選択してください", text_color="#c62828")
            return
        if not ref_text:
            self.voice_status.configure(
                text="リファレンステキストを入力してください", text_color="#c62828")
            return

        self.save_voice_btn.configure(state="disabled", text="保存中...")
        self.voice_status.configure(text="", text_color="#888888")

        try:
            dir_name = name.replace(" ", "_").replace("/", "_")
            voice_dir = self._voices_dir() / dir_name
            voice_dir.mkdir(parents=True, exist_ok=True)

            meta_path = voice_dir / "meta.json"
            if meta_path.exists():
                with open(meta_path, encoding="utf-8") as f:
                    meta = json.load(f)
            else:
                # 既存IDと衝突しない最小IDを採番
                used_ids = set()
                for d in self._voices_dir().iterdir():
                    if not d.is_dir():
                        continue
                    mp = d / "meta.json"
                    if mp.exists():
                        try:
                            with open(mp, encoding="utf-8") as mf:
                                used_ids.add(json.load(mf).get("speaker_id", 0))
                        except Exception:
                            pass
                new_id = 0
                while new_id in used_ids:
                    new_id += 1
                meta = {
                    "speaker_id": new_id,
                    "speaker_uuid": f"00000000-0000-0000-0000-{new_id:012d}",
                    "styles": [{"name": "ノーマル", "id": new_id, "type": "talk"}],
                    "ref_audio": "reference.wav",
                }

            meta["name"] = name
            meta["ref_text"] = ref_text
            meta["language"] = language

            shutil.copy2(audio_path, voice_dir / "reference.wav")

            with open(meta_path, "w", encoding="utf-8") as f:
                json.dump(meta, f, ensure_ascii=False, indent=2)

            self._on_voice_saved("保存しました")
            # サーバー起動中なら engine にリロードを通知
            if self.state == self.STATE_RUNNING:
                self._notify_server_reload_voices()
        except Exception as e:
            self._on_voice_save_error(str(e))

    def _notify_server_reload_voices(self):
        """サーバー起動中にボイス変更があった場合、エンジンをリロード"""
        def worker():
            try:
                req = urllib.request.Request(
                    f"{self.api.base_url}/manage/reload",
                    method="POST", data=b"")
                with urllib.request.urlopen(req, timeout=10):
                    pass
                self.root.after(0, self._refresh_speaker_dropdown)
            except Exception:
                pass
        threading.Thread(target=worker, daemon=True).start()

    def _on_voice_saved(self, msg: str):
        self.voice_status.configure(text=msg, text_color="#2e7d32")
        self.save_voice_btn.configure(state="normal", text="ボイスを保存")
        # フォームリセット
        self.voice_name_entry.delete(0, "end")
        self.ref_text_entry.delete("0.0", "end")
        self._selected_audio_path = None
        self.audio_path_label.configure(text="未選択", text_color="#888888")
        self.preview_btn.configure(state="disabled")
        self.lang_dropdown.set("Japanese")
        # 一覧更新
        self._refresh_voice_list()

    def _on_voice_save_error(self, err: str):
        self.voice_status.configure(text=f"エラー: {err}", text_color="#c62828")
        self.save_voice_btn.configure(state="normal", text="ボイスを保存")

    def _refresh_voice_list(self):
        voices = []
        vdir = self._voices_dir()
        if vdir.exists():
            for d in sorted(vdir.iterdir()):
                if not d.is_dir():
                    continue
                meta_path = d / "meta.json"
                if not meta_path.exists():
                    continue
                try:
                    with open(meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                    voices.append({
                        "dir_name": d.name,
                        "name": meta.get("name", d.name),
                        "speaker_id": meta.get("speaker_id", 0),
                        "ref_text": meta.get("ref_text", ""),
                        "language": meta.get("language", "Japanese"),
                        "has_audio": (d / meta.get(
                            "ref_audio", "reference.wav")).exists(),
                    })
                except Exception:
                    continue
        self._update_voice_list_ui(voices)

    def _update_voice_list_ui(self, voices: list[dict]):
        self._voices_data = voices

        # 既存ウィジェットをクリア
        for w in self.voice_list_frame.winfo_children():
            w.destroy()

        if not voices:
            ctk.CTkLabel(
                self.voice_list_frame,
                text="ボイスが未登録です。\n左のフォームから登録してください。",
                text_color="#999999", font=_ui_font(12),
            ).pack(pady=20)
            return

        for v in voices:
            self._create_voice_item(v)

    def _create_voice_item(self, voice: dict):
        item = ctk.CTkFrame(
            self.voice_list_frame, fg_color="#f5f5f5", corner_radius=8)
        item.pack(fill="x", pady=3)

        # ボタンを先にpackして領域を確保
        btn_frame = ctk.CTkFrame(item, fg_color="transparent")
        btn_frame.pack(side="right", padx=10, pady=8)

        dir_name = voice["dir_name"]
        if voice.get("has_audio"):
            play_btn = ctk.CTkButton(
                btn_frame, text="\u25b6 再生", width=70, height=30,
                font=_ui_font(12),
            )
            play_btn.configure(
                command=lambda d=dir_name, b=play_btn:
                    self._play_reference_audio(d, b))
            play_btn.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            btn_frame, text="削除", width=60, height=30,
            fg_color="#c62828", hover_color="#b71c1c",
            font=_ui_font(12),
            command=lambda d=dir_name: self._delete_voice(d),
        ).pack(side="left")

        # 情報は残りの領域を使う
        info = ctk.CTkFrame(item, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True, padx=10, pady=8)

        ctk.CTkLabel(
            info, text=voice["name"],
            font=_ui_font(14, True),
        ).pack(anchor="w")

        meta_text = f"Speaker ID: {voice['speaker_id']}"
        lang = voice.get("language", "")
        if lang:
            meta_text += f" / {lang}"
        ctk.CTkLabel(
            info, text=meta_text,
            font=_ui_font(11), text_color="#888888",
        ).pack(anchor="w")

        ref_text = voice.get("ref_text", "")
        if ref_text:
            display = ref_text[:50] + "..." if len(ref_text) > 50 else ref_text
            ctk.CTkLabel(
                info, text=display,
                font=_ui_font(11), text_color="#aaaaaa",
            ).pack(anchor="w")

    def _play_reference_audio(self, dir_name: str, btn=None):
        voice_dir = self._voices_dir() / dir_name
        meta_path = voice_dir / "meta.json"
        try:
            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)
            audio_path = voice_dir / meta.get("ref_audio", "reference.wav")
            if audio_path.exists():
                self._play_wav(str(audio_path), toggle_btn=btn)
            else:
                messagebox.showerror("再生エラー", "音声ファイルが見つかりません")
        except Exception as e:
            messagebox.showerror("再生エラー", str(e))

    def _delete_voice(self, dir_name: str):
        if not messagebox.askyesno("確認", f"ボイス '{dir_name}' を削除しますか？"):
            return
        try:
            voice_dir = self._voices_dir() / dir_name
            if voice_dir.exists():
                shutil.rmtree(voice_dir)
            self._refresh_voice_list()
            if self.state == self.STATE_RUNNING:
                self._notify_server_reload_voices()
        except Exception as e:
            messagebox.showerror("削除エラー", str(e))

    # ===================================================================
    # 音声合成
    # ===================================================================

    def _on_speed_change(self, value):
        self.speed_val_label.configure(text=f"{value:.1f}x")

    def _on_volume_change(self, value):
        self.volume_val_label.configure(text=f"{value:.1f}x")

    def _refresh_speaker_dropdown(self):
        voices = []
        vdir = self._voices_dir()
        if vdir.exists():
            for d in sorted(vdir.iterdir()):
                if not d.is_dir():
                    continue
                meta_path = d / "meta.json"
                if not meta_path.exists():
                    continue
                try:
                    with open(meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                    voices.append({
                        "name": meta.get("name", d.name),
                        "speaker_id": meta.get("speaker_id", 0),
                    })
                except Exception:
                    continue
        self._update_speaker_dropdown(voices)

    def _update_speaker_dropdown(self, voices: list[dict]):
        if not voices:
            self.speaker_dropdown.configure(values=["-- ボイス未登録 --"])
            self.speaker_dropdown.set("-- ボイス未登録 --")
            return

        values = [f"{v['name']} (ID: {v['speaker_id']})" for v in voices]
        self.speaker_dropdown.configure(values=values)
        self.speaker_dropdown.set(values[0])
        self._speaker_id_map = {
            f"{v['name']} (ID: {v['speaker_id']})": v["speaker_id"]
            for v in voices
        }

    def _get_selected_speaker_id(self) -> int | None:
        selected = self.speaker_dropdown.get()
        if hasattr(self, "_speaker_id_map"):
            return self._speaker_id_map.get(selected)
        return None

    def _synthesize(self):
        speaker_id = self._get_selected_speaker_id()
        text = self.synth_text.get("0.0", "end").strip()

        if speaker_id is None:
            self.synth_status.configure(
                text="スピーカーを選択してください", text_color="#c62828")
            return
        if not text:
            self.synth_status.configure(
                text="テキストを入力してください", text_color="#c62828")
            return

        speed = self.speed_slider.get()
        volume = self.volume_slider.get()

        self.synthesize_btn.configure(state="disabled", text="合成中...")
        self.synth_status.configure(text="生成中...", text_color="#666666")
        self.play_synth_btn.configure(state="disabled")
        self.save_wav_btn.configure(state="disabled")

        def worker():
            try:
                query = self.api.audio_query(text, speaker_id)
                query["speedScale"] = speed
                query["volumeScale"] = volume
                wav_bytes = self.api.synthesize(speaker_id, query)
                self.root.after(0, self._on_synthesis_complete, wav_bytes)
            except Exception as e:
                self.root.after(0, self._on_synthesis_error, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _on_synthesis_complete(self, wav_bytes: bytes):
        self._last_synth_wav = wav_bytes
        temp_path = os.path.join(tempfile.gettempdir(), "selfvox_synth.wav")
        with open(temp_path, "wb") as f:
            f.write(wav_bytes)
        self._last_synth_path = temp_path

        self.synthesize_btn.configure(state="normal", text="合成")
        self.play_synth_btn.configure(state="normal")
        self.save_wav_btn.configure(state="normal")
        self.synth_status.configure(text="合成完了", text_color="#2e7d32")

        self._play_wav(temp_path)

    def _on_synthesis_error(self, err: str):
        self.synthesize_btn.configure(state="normal", text="合成")
        self.synth_status.configure(text=f"エラー: {err}", text_color="#c62828")

    def _play_synthesized(self):
        if self._last_synth_path and os.path.exists(self._last_synth_path):
            self._play_wav(self._last_synth_path, toggle_btn=self.play_synth_btn)

    def _save_synthesized_wav(self):
        if not self._last_synth_wav:
            return
        path = filedialog.asksaveasfilename(
            title="WAVファイルを保存",
            defaultextension=".wav",
            filetypes=[("WAV File", "*.wav")],
            initialfile="synthesis.wav",
        )
        if path:
            with open(path, "wb") as f:
                f.write(self._last_synth_wav)
            self.synth_status.configure(
                text=f"保存しました: {Path(path).name}", text_color="#2e7d32")

    # ===================================================================
    # 音声再生
    # ===================================================================

    def _open_browser(self):
        url = self._url_var.get()
        if url:
            webbrowser.open(url)

    def _copy_url(self):
        url = self._url_var.get()
        if url:
            self.root.clipboard_clear()
            self.root.clipboard_append(url)
            self.copy_url_btn.configure(text="Copied!")
            self.root.after(1500, lambda: self.copy_url_btn.configure(text="Copy"))

    def _play_wav(self, path: str, toggle_btn=None):
        """WAV再生/停止トグル。toggle_btn を渡すとボタン表示を切り替える"""
        if self._playing_btn is not None:
            was_same = self._playing_btn is toggle_btn
            # 何か再生中 → 停止
            winsound.PlaySound(None, winsound.SND_PURGE)
            self._stop_playing()
            if was_same:
                return  # 同じボタン = 停止のみ
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            if toggle_btn is not None:
                self._playing_btn = toggle_btn
                toggle_btn.configure(text="\u25a0")  # ■ 停止アイコン
        except Exception as e:
            messagebox.showerror("再生エラー", str(e))

    def _stop_playing(self):
        if self._playing_btn is not None:
            try:
                self._playing_btn.configure(text="\u25b6")  # ▶ に戻す
            except Exception:
                pass
            self._playing_btn = None

    # ===================================================================
    # 設定
    # ===================================================================

    def _load_config(self) -> dict:
        config_path = self._app_dir() / "config.json"
        defaults = {"port": 50021}
        if config_path.exists():
            try:
                with open(config_path, encoding="utf-8") as f:
                    defaults.update(json.load(f))
            except Exception:
                pass
        return defaults

    def _save_config(self):
        config_path = self._app_dir() / "config.json"
        with open(config_path, "w", encoding="utf-8") as f:
            json.dump(self.config, f, indent=2)

    def _open_settings(self):
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("Settings")
        dlg.geometry("400x180")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="ポート番号:", font=_ui_font(13)).place(
            x=20, y=24)
        port_var = ctk.StringVar(value=str(self.config.get("port", 50021)))
        port_entry = ctk.CTkEntry(
            dlg, textvariable=port_var, width=120, font=_ui_font(13))
        port_entry.place(x=120, y=20)

        ctk.CTkLabel(
            dlg, text="※ ポート番号の変更はサーバー再起動後に反映されます",
            font=_ui_font(11), text_color="#888888",
        ).place(x=20, y=72)

        def save_and_close():
            try:
                port = int(port_var.get())
                if not (1 <= port <= 65535):
                    raise ValueError
            except ValueError:
                messagebox.showerror(
                    "エラー", "ポート番号は1-65535の整数を入力してください",
                    parent=dlg,
                )
                return
            self.config["port"] = port
            self._save_config()
            dlg.destroy()

        ctk.CTkButton(
            dlg, text="保存", command=save_and_close,
            fg_color="#4CAF50", hover_color="#43a047",
            font=_ui_font(13, True),
            width=120, height=36,
        ).place(x=140, y=120)

        dlg.wait_window()

    # ===================================================================
    # ユーティリティ
    # ===================================================================

    @staticmethod
    def _app_dir() -> Path:
        if getattr(sys, "frozen", False):
            return Path(sys.executable).parent
        return Path(__file__).parent

    @classmethod
    def _voices_dir(cls) -> Path:
        d = cls._app_dir() / "voices"
        d.mkdir(exist_ok=True)
        return d
