"""SelfVox GUI ランチャー - customtkinter ベースのグラフィカルインターフェース"""

import ctypes
import io
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
import wave
import winsound
import zipfile
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

    def batch_synthesize(
        self, texts: list[str], speaker: int,
        speed: float = 1.0, volume: float = 1.0,
    ) -> list[bytes]:
        """バッチ合成API呼び出し。ZIPレスポンスを展開してWAVバイト列のリストを返す"""
        url = f"{self.base_url}/batch_synthesis?speaker={speaker}"
        body = json.dumps({
            "texts": texts,
            "speedScale": speed,
            "volumeScale": volume,
        }).encode("utf-8")
        req = urllib.request.Request(
            url, data=body, method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=600) as resp:
            zip_data = resp.read()
        wav_list: list[bytes] = []
        with zipfile.ZipFile(io.BytesIO(zip_data)) as zf:
            for name in sorted(zf.namelist()):
                wav_list.append(zf.read(name))
        return wav_list


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

        # 起動時にセットアップ/パッケージ更新が必要なら自動実行
        self.root.after(800, self._check_auto_setup)

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
            text_color="white",
            font=_ui_font(13, True),
            width=140, height=36,
        )
        self.start_btn.pack(side="left", padx=(0, 8))

        self.stop_btn = ctk.CTkButton(
            btn_frame, text="Stop Server", command=self._on_stop,
            fg_color="#F44336", hover_color="#e53935",
            text_color="white",
            font=_ui_font(13, True),
            width=140, height=36,
        )
        self.stop_btn.pack(side="left", padx=(0, 8))

        self._url_var = ctk.StringVar()

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
        self.tab_voicevox = self.tabview.add("VOICEVOX")
        self.tab_log = self.tabview.add("ログ")
        self.tab_about = self.tabview.add("About")

        self._build_voice_tab()
        self._build_synth_tab()
        self._build_voicevox_tab()
        self._build_log_tab()
        self._build_about_tab()

        # タブ切り替え時に再生停止
        self.tabview.configure(command=lambda: self._stop_all_audio())

        # 起動時はログタブを表示
        self.tabview.set("ログ")

    # --- About タブ ---

    def _build_about_tab(self):
        scroll = ctk.CTkScrollableFrame(
            self.tab_about, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=10)

        # --- SelfVox ---
        hero_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color="#1a237e")
        hero_card.pack(fill="x", pady=(0, 12))
        hero_inner = ctk.CTkFrame(hero_card, fg_color="transparent")
        hero_inner.pack(fill="x", padx=24, pady=20)

        # ロゴ + タイトル行
        hero_top = ctk.CTkFrame(hero_inner, fg_color="transparent")
        hero_top.pack(fill="x", pady=(0, 8))

        logo_path = self._app_dir() / "selfvox.png"
        if logo_path.exists():
            logo_img = ctk.CTkImage(
                light_image=Image.open(logo_path), size=(52, 52))
            ctk.CTkLabel(
                hero_top, image=logo_img, text="",
            ).pack(side="left", padx=(0, 14))

        title_block = ctk.CTkFrame(hero_top, fg_color="transparent")
        title_block.pack(side="left")
        ctk.CTkLabel(
            title_block, text="SelfVox",
            font=_ui_font(24, True), text_color="white",
        ).pack(anchor="w")
        ctk.CTkLabel(
            title_block, text=f"Version {VERSION}",
            font=_ui_font(13), text_color="#b0b8d0",
        ).pack(anchor="w")

        ctk.CTkLabel(
            hero_inner,
            text="Qwen3-TTS ベースの Voice Clone デスクトップアプリ\n"
                 "VOICEVOX互換APIでお好みのアプリから利用できます",
            font=_ui_font(13), text_color="white",
            justify="left",
        ).pack(anchor="w", pady=(4, 10))

        # GitHub ボタン
        link_row = ctk.CTkFrame(hero_inner, fg_color="transparent")
        link_row.pack(anchor="w")
        ctk.CTkButton(
            link_row, text="GitHub",
            command=lambda: webbrowser.open(
                "https://github.com/Yasuaki-Ito/selfvox"),
            fg_color="#3949ab",
            hover_color="#5c6bc0",
            text_color="white",
            font=_ui_font(12), corner_radius=6,
            width=90, height=32,
        ).pack(side="left", padx=(0, 8))

        # --- 概要 ---
        overview_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        overview_card.pack(fill="x", pady=(0, 12))
        ov_inner = ctk.CTkFrame(overview_card, fg_color="transparent")
        ov_inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            ov_inner, text="特徴", font=_ui_font(16, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 8))

        features = [
            ("Voice Clone", "あなたの声をリファレンス音声から再現"),
            ("VOICEVOX互換", "PPVoice 等の対応アプリからそのまま利用可能"),
            ("Web UI", "ブラウザから音声合成・ボイス管理"),
            ("デスクトップアプリ", "GUIから簡単セットアップ・操作"),
            ("Qwen3-TTS", "Alibaba Cloud の高品質TTSモデル搭載"),
        ]

        for title, desc in features:
            feat_row = ctk.CTkFrame(
                ov_inner, fg_color=self._CLR_ITEM_BG, corner_radius=8)
            feat_row.pack(fill="x", pady=(0, 4))
            feat_inner = ctk.CTkFrame(feat_row, fg_color="transparent")
            feat_inner.pack(fill="x", padx=14, pady=8)
            ctk.CTkLabel(
                feat_inner, text=title, font=_ui_font(13, True),
                text_color=self._CLR_PRIMARY,
            ).pack(side="left", padx=(0, 10))
            ctk.CTkLabel(
                feat_inner, text=desc, font=_ui_font(12),
                text_color=self._CLR_TEXT,
            ).pack(side="left")

        # --- PPVoice ---
        pp_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        pp_card.pack(fill="x", pady=(0, 12))
        pp_inner = ctk.CTkFrame(pp_card, fg_color="transparent")
        pp_inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            pp_inner, text="PPVoice", font=_ui_font(16, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkLabel(
            pp_inner,
            text="PowerPoint スライドに音声ナレーションを自動付与するツール",
            font=_ui_font(12), text_color=self._CLR_TEXT_SUB,
        ).pack(anchor="w", pady=(0, 10))

        pp_features = [
            "PowerPoint のノートに書いたテキストを自動で音声合成",
            "SelfVox / VOICEVOX 対応 — Voice Clone 音声でナレーション",
            "スライドごとに異なるスピーカーを指定可能",
            "一括処理でプレゼン動画の作成に最適",
        ]

        pp_list = ctk.CTkFrame(
            pp_inner, fg_color=self._CLR_ITEM_BG, corner_radius=8)
        pp_list.pack(fill="x", pady=(0, 10))
        pp_list_inner = ctk.CTkFrame(pp_list, fg_color="transparent")
        pp_list_inner.pack(fill="x", padx=14, pady=10)
        for feat in pp_features:
            ctk.CTkLabel(
                pp_list_inner, text=f"\u2022 {feat}",
                font=_ui_font(12), text_color=self._CLR_TEXT,
                justify="left",
            ).pack(anchor="w", pady=1)

        ctk.CTkButton(
            pp_inner, text="PPVoice GitHub",
            command=lambda: webbrowser.open(
                "https://github.com/Yasuaki-Ito/PPVoice"),
            fg_color=self._CLR_BLUE, hover_color=self._CLR_BLUE_HOVER,
            text_color="white",
            font=_ui_font(12), corner_radius=6,
            width=130, height=32,
        ).pack(anchor="w")

        # --- 技術情報 ---
        tech_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        tech_card.pack(fill="x", pady=(0, 12))
        tech_inner = ctk.CTkFrame(tech_card, fg_color="transparent")
        tech_inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            tech_inner, text="技術情報", font=_ui_font(16, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 8))

        tech_items = [
            ("TTSモデル", "Qwen3-TTS-12Hz-1.7B-Base"),
            ("フレームワーク", "PyTorch + Transformers"),
            ("APIサーバー", "FastAPI (Uvicorn)"),
            ("GUI", "customtkinter"),
            ("ライセンス", "MIT License"),
        ]

        for label, value in tech_items:
            tech_row = ctk.CTkFrame(tech_inner, fg_color="transparent")
            tech_row.pack(fill="x", pady=2)
            ctk.CTkLabel(
                tech_row, text=label, font=_ui_font(12, True),
                text_color=self._CLR_TEXT_SUB, width=100, anchor="w",
            ).pack(side="left")
            ctk.CTkLabel(
                tech_row, text=value, font=_ui_font(12),
                text_color=self._CLR_TEXT,
            ).pack(side="left", padx=(8, 0))

        # フッター
        ctk.CTkLabel(
            scroll,
            text="Made by Yasuaki Ito",
            font=_ui_font(11), text_color=self._CLR_TEXT_HINT,
        ).pack(pady=(4, 8))

    # --- VOICEVOX タブ ---

    def _build_voicevox_tab(self):
        scroll = ctk.CTkScrollableFrame(
            self.tab_voicevox, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=10)

        # --- VOICEVOX互換API 概要 ---
        overview_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        overview_card.pack(fill="x", pady=(0, 12))
        overview_inner = ctk.CTkFrame(overview_card, fg_color="transparent")
        overview_inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            overview_inner, text="VOICEVOX互換API",
            font=_ui_font(16, True), text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 4))

        ctk.CTkLabel(
            overview_inner,
            text="SelfVoxはVOICEVOX互換APIを提供しています。\n"
                 "VOICEVOX対応アプリの接続先URLに\n"
                 "サーバーURLを設定するだけで利用できます。",
            font=_ui_font(12), text_color=self._CLR_TEXT_SUB,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        # バッジ風ラベル
        badge_row = ctk.CTkFrame(overview_inner, fg_color="transparent")
        badge_row.pack(anchor="w", pady=(0, 8))
        for badge_text, bg in [
            ("VOICEVOX Compatible", "#1a237e"),
            ("Voice Clone", self._CLR_PRIMARY),
            ("Qwen3-TTS", "#7B1FA2"),
        ]:
            ctk.CTkLabel(
                badge_row, text=f" {badge_text} ",
                font=_ui_font(11, True), text_color="white",
                fg_color=bg, corner_radius=4, height=24,
            ).pack(side="left", padx=(0, 6))

        # --- サーバー稼働状況 ---
        status_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        status_card.pack(fill="x", pady=(0, 12))
        status_inner = ctk.CTkFrame(status_card, fg_color="transparent")
        status_inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            status_inner, text="サーバー稼働状況",
            font=_ui_font(16, True), text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 8))

        status_row = ctk.CTkFrame(status_inner, fg_color="transparent")
        status_row.pack(fill="x", pady=(0, 4))

        self._vv_status_dot = ctk.CTkLabel(
            status_row, text="\u25cf", width=20,
            font=_ui_font(18), text_color="#9E9E9E",
        )
        self._vv_status_dot.pack(side="left", padx=(0, 8))

        self._vv_status_label = ctk.CTkLabel(
            status_row, text="停止中",
            font=_ui_font(14), text_color=self._CLR_TEXT,
        )
        self._vv_status_label.pack(side="left")

        self._vv_url_label = ctk.CTkLabel(
            status_inner, text="",
            font=_mono_font(14), text_color="#1a237e",
        )
        self._vv_url_label.pack(anchor="w", pady=(4, 0))

        # URL コピー行
        url_row = ctk.CTkFrame(status_inner, fg_color="transparent")
        url_row.pack(fill="x", pady=(6, 0))

        self.url_entry = ctk.CTkEntry(
            url_row, height=34, font=_ui_font(13),
            state="readonly", fg_color="#f0f0f0", border_width=1,
            border_color="#cccccc", text_color="#1a237e",
        )
        self.url_entry.configure(textvariable=self._url_var)
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))

        self.copy_url_btn = ctk.CTkButton(
            url_row, text="Copy", width=64, height=34,
            font=_ui_font(12), fg_color="#1a237e", hover_color="#283593",
            text_color="white",
            command=self._copy_url,
        )
        self.copy_url_btn.pack(side="left")

        self._vv_url_hint = ctk.CTkLabel(
            status_inner,
            text="VOICEVOX対応アプリの接続先URLにこのアドレスを設定してください。",
            font=_ui_font(11), text_color=self._CLR_TEXT_HINT,
        )
        self._vv_url_hint.pack(anchor="w", pady=(4, 0))

        # 初期状態では非表示
        url_row.pack_forget()
        self._vv_url_hint.pack_forget()
        self._vv_url_row = url_row

        # --- 使い方 ---
        usage_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        usage_card.pack(fill="x", pady=(0, 12))
        usage_inner = ctk.CTkFrame(usage_card, fg_color="transparent")
        usage_inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            usage_inner, text="使い方",
            font=_ui_font(16, True), text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 8))

        steps = [
            ("1", "ボイス管理タブでリファレンス音声を登録"),
            ("2", "サーバーを起動（Start Server）"),
            ("3", "音声合成タブで合成テスト、または外部アプリから接続"),
        ]
        for num, text in steps:
            step_row = ctk.CTkFrame(
                usage_inner, fg_color=self._CLR_ITEM_BG, corner_radius=6)
            step_row.pack(fill="x", pady=2)
            step_inner = ctk.CTkFrame(step_row, fg_color="transparent")
            step_inner.pack(fill="x", padx=10, pady=6)
            ctk.CTkLabel(
                step_inner, text=num, width=26, height=26,
                font=_ui_font(12, True), text_color="white",
                fg_color=self._CLR_PRIMARY, corner_radius=13,
            ).pack(side="left", padx=(0, 10))
            ctk.CTkLabel(
                step_inner, text=text,
                font=_ui_font(12), text_color=self._CLR_TEXT,
            ).pack(side="left")

        # --- API エンドポイント一覧 ---
        api_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        api_card.pack(fill="x", pady=(0, 12))
        api_inner = ctk.CTkFrame(api_card, fg_color="transparent")
        api_inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            api_inner, text="API エンドポイント一覧",
            font=_ui_font(16, True), text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 4))

        ctk.CTkLabel(
            api_inner,
            text="基本フロー:  POST /audio_query → POST /synthesis",
            font=_mono_font(11), text_color=self._CLR_TEXT_SUB,
        ).pack(anchor="w", pady=(0, 10))

        endpoints = [
            ("POST", "/audio_query?text=...&speaker=ID", "テキストからAudioQueryを生成"),
            ("POST", "/synthesis?speaker=ID", "AudioQueryから音声(WAV)を合成"),
            ("GET",  "/speakers", "利用可能なスピーカー一覧"),
            ("GET",  "/version", "エンジンバージョン"),
            ("POST", "/multi_synthesis?speaker=ID", "複数テキストを一括合成(ZIP)"),
            ("POST", "/cancellable_synthesis?speaker=ID", "キャンセル可能な合成"),
            ("GET",  "/speaker_info?speaker_uuid=...", "スピーカー詳細情報"),
            ("POST", "/accent_phrases?text=...&speaker=ID", "アクセント句生成"),
            ("GET",  "/supported_devices", "対応デバイス一覧"),
            ("GET",  "/engine_manifest", "エンジンマニフェスト"),
            ("GET",  "/presets", "プリセット一覧"),
            ("GET",  "/user_dict", "ユーザー辞書"),
        ]

        self._vv_endpoint_labels = []
        for method, path, desc in endpoints:
            row = ctk.CTkFrame(
                api_inner, fg_color=self._CLR_ITEM_BG, corner_radius=6)
            row.pack(fill="x", pady=2)
            row_inner = ctk.CTkFrame(row, fg_color="transparent")
            row_inner.pack(fill="x", padx=10, pady=5)

            method_bg = "#e8f5e9" if method == "GET" else "#fff3e0"
            method_fg = "#2e7d32" if method == "GET" else "#e65100"
            method_label = ctk.CTkLabel(
                row_inner, text=method, width=48,
                font=_mono_font(11), text_color=method_fg,
            )
            method_label.pack(side="left")

            path_label = ctk.CTkLabel(
                row_inner, text=path,
                font=_mono_font(11), text_color=self._CLR_TEXT,
            )
            path_label.pack(side="left", padx=(8, 10))
            self._vv_endpoint_labels.append(path_label)

            ctk.CTkLabel(
                row_inner, text=desc,
                font=_ui_font(11), text_color=self._CLR_TEXT_SUB,
            ).pack(side="left")

        # --- Web UI ---
        webui_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        webui_card.pack(fill="x", pady=(0, 12))
        webui_inner = ctk.CTkFrame(webui_card, fg_color="transparent")
        webui_inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            webui_inner, text="Web UI",
            font=_ui_font(16, True), text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 4))

        ctk.CTkLabel(
            webui_inner,
            text="サーバー起動中にブラウザでアクセスすると、\n"
                 "GUIと同等の機能が使えるWeb UIが表示されます。",
            font=_ui_font(12), text_color=self._CLR_TEXT_SUB,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        # Web UI 機能一覧
        webui_features = [
            ("ボイス登録", "音声ファイルのドラッグ&ドロップで簡単登録"),
            ("音声合成", "テキスト入力→即再生・ダウンロード"),
            ("一括合成", "複数行テキストを1行ずつ順番に合成"),
            ("合成履歴", "最近の合成結果を再生・ダウンロード"),
        ]
        for feat_title, feat_desc in webui_features:
            feat_row = ctk.CTkFrame(
                webui_inner, fg_color=self._CLR_ITEM_BG, corner_radius=6)
            feat_row.pack(fill="x", pady=2)
            feat_inner = ctk.CTkFrame(feat_row, fg_color="transparent")
            feat_inner.pack(fill="x", padx=10, pady=5)
            ctk.CTkLabel(
                feat_inner, text=feat_title,
                font=_ui_font(12, True), text_color=self._CLR_PRIMARY,
            ).pack(side="left", padx=(0, 10))
            ctk.CTkLabel(
                feat_inner, text=feat_desc,
                font=_ui_font(11), text_color=self._CLR_TEXT_SUB,
            ).pack(side="left")

        self.open_browser_btn = ctk.CTkButton(
            webui_inner, text="🌐 ブラウザで開く", width=160, height=36,
            font=_ui_font(13, True), fg_color="#1a237e", hover_color="#283593",
            text_color="white",
            command=self._open_browser,
            state="disabled",
        )
        self.open_browser_btn.pack(anchor="w", pady=(10, 0))

        # --- 互換性情報 ---
        desc_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        desc_card.pack(fill="x", pady=(0, 12))
        desc_inner = ctk.CTkFrame(desc_card, fg_color="transparent")
        desc_inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            desc_inner, text="互換性情報",
            font=_ui_font(16, True), text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 4))

        ctk.CTkLabel(
            desc_inner,
            text="SelfVoxはEnd-to-End音声合成モデル（Qwen3-TTS）を使用しているため、\n"
                 "VOICEVOXの全機能には対応していません。\n"
                 "テキスト→音声の基本的な合成フローは互換性があります。",
            font=_ui_font(12), text_color=self._CLR_TEXT_SUB,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        # 対応機能
        compat_frame = ctk.CTkFrame(
            desc_inner, fg_color=self._CLR_SUCCESS_BG, corner_radius=8)
        compat_frame.pack(fill="x", pady=(0, 8))
        compat_inner = ctk.CTkFrame(compat_frame, fg_color="transparent")
        compat_inner.pack(fill="x", padx=14, pady=10)
        ctk.CTkLabel(
            compat_inner, text="対応機能",
            font=_ui_font(13, True), text_color=self._CLR_SUCCESS_FG,
        ).pack(anchor="w", pady=(0, 4))
        for item in [
            "テキスト→音声合成 (audio_query + synthesis)",
            "スピーカー一覧・詳細情報取得",
            "速度・音量・ピッチ調整",
            "複数テキスト一括合成 (multi_synthesis)",
            "キャンセル可能な合成 (cancellable_synthesis)",
        ]:
            ctk.CTkLabel(
                compat_inner, text=f"  \u2713  {item}",
                font=_ui_font(12), text_color=self._CLR_SUCCESS_FG,
            ).pack(anchor="w")

        # 非対応機能
        incompat_frame = ctk.CTkFrame(
            desc_inner, fg_color=self._CLR_ERROR_BG, corner_radius=8)
        incompat_frame.pack(fill="x", pady=(0, 8))
        incompat_inner = ctk.CTkFrame(incompat_frame, fg_color="transparent")
        incompat_inner.pack(fill="x", padx=14, pady=10)
        ctk.CTkLabel(
            incompat_inner, text="非対応（End-to-Endモデルのため）",
            font=_ui_font(13, True), text_color=self._CLR_ERROR_FG,
        ).pack(anchor="w", pady=(0, 4))
        for item in [
            "モーラ単位のピッチ・長さ編集",
            "イントネーション詳細調整",
            "ユーザー辞書（スタブのみ実装）",
        ]:
            ctk.CTkLabel(
                incompat_inner, text=f"  \u2717  {item}",
                font=_ui_font(12), text_color=self._CLR_ERROR_FG,
            ).pack(anchor="w")

        # 動作確認済みアプリ
        apps_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        apps_card.pack(fill="x", pady=(0, 12))
        apps_inner = ctk.CTkFrame(apps_card, fg_color="transparent")
        apps_inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            apps_inner, text="動作確認済みアプリ",
            font=_ui_font(16, True), text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 4))

        ctk.CTkLabel(
            apps_inner,
            text="以下のアプリで動作確認を行っています。\n"
                 "接続先URLにサーバーアドレスを設定するだけで利用できます。",
            font=_ui_font(12), text_color=self._CLR_TEXT_SUB,
            justify="left",
        ).pack(anchor="w", pady=(0, 10))

        # PPVoice カード
        ppv_frame = ctk.CTkFrame(
            apps_inner, fg_color=self._CLR_ITEM_BG, corner_radius=8)
        ppv_frame.pack(fill="x", pady=(0, 4))
        ppv_inner = ctk.CTkFrame(ppv_frame, fg_color="transparent")
        ppv_inner.pack(fill="x", padx=14, pady=10)
        ctk.CTkLabel(
            ppv_inner, text="PPVoice",
            font=_ui_font(14, True), text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(
            ppv_inner,
            text="PowerPointのノートを自動で読み上げるVOICEVOX連携ツール。\n"
                 "プレゼン資料のナレーション作成に最適です。",
            font=_ui_font(11), text_color=self._CLR_TEXT_SUB,
            justify="left",
        ).pack(anchor="w", pady=(0, 4))
        ctk.CTkButton(
            ppv_inner, text="GitHub で見る",
            command=lambda: webbrowser.open(
                "https://github.com/Yasuaki-Ito/PPVoice"),
            fg_color=self._CLR_BLUE, hover_color=self._CLR_BLUE_HOVER,
            text_color="white",
            font=_ui_font(11), corner_radius=6,
            width=110, height=28,
        ).pack(anchor="w")

        ctk.CTkLabel(
            apps_inner,
            text="その他のVOICEVOX互換アプリでもAPIエンドポイントを\n"
                 "指定できるものであれば利用可能です。",
            font=_ui_font(11), text_color=self._CLR_TEXT_HINT,
            justify="left",
        ).pack(anchor="w", pady=(8, 0))

    # --- ボイス管理タブ ---

    def _build_voice_tab(self):
        scroll = ctk.CTkScrollableFrame(
            self.tab_voices, fg_color="transparent")
        scroll.pack(fill="both", expand=True, padx=10, pady=10)

        # ========== 上: 登録済みボイス一覧 ==========
        list_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        list_card.pack(fill="x", pady=(0, 12))

        title_row = ctk.CTkFrame(list_card, fg_color="transparent")
        title_row.pack(fill="x", padx=20, pady=(16, 4))
        ctk.CTkLabel(
            title_row, text="登録済みボイス",
            font=_ui_font(16, True), text_color=self._CLR_TEXT,
        ).pack(side="left")
        ctk.CTkButton(
            title_row, text="\u21bb 更新", width=68, height=28,
            font=_ui_font(11), fg_color="transparent",
            hover_color="#e0e0e0", text_color=self._CLR_TEXT,
            border_width=1, border_color=self._CLR_SEPARATOR,
            corner_radius=6,
            command=self._refresh_voice_list,
        ).pack(side="right")

        ctk.CTkLabel(
            list_card,
            text="登録済みのボイスプロファイル一覧です。"
                 "VOICEVOX対応アプリからスピーカーとして選択できます。",
            font=_ui_font(11), text_color=self._CLR_TEXT_HINT,
        ).pack(anchor="w", padx=20, pady=(0, 8))

        self.voice_list_frame = ctk.CTkFrame(
            list_card, fg_color="transparent")
        self.voice_list_frame.pack(
            fill="x", padx=16, pady=(0, 16))

        self.voice_list_placeholder = ctk.CTkLabel(
            self.voice_list_frame,
            text="ボイスが未登録です。下のフォームから登録してください。",
            text_color=self._CLR_TEXT_HINT, font=_ui_font(12),
        )
        self.voice_list_placeholder.pack(pady=20)

        # ========== 下: ボイス登録フォーム ==========
        reg_card = ctk.CTkFrame(
            scroll, corner_radius=10, fg_color=self._CLR_CARD_BG)
        reg_card.pack(fill="x", pady=(0, 12))

        ctk.CTkLabel(
            reg_card, text="ボイス登録",
            font=_ui_font(16, True), text_color=self._CLR_TEXT,
        ).pack(anchor="w", padx=20, pady=(16, 4))

        ctk.CTkLabel(
            reg_card,
            text="あなたの声のサンプル音声を登録して、AIによる音声クローンを作成します。",
            font=_ui_font(12), text_color=self._CLR_TEXT_SUB,
        ).pack(anchor="w", padx=20, pady=(0, 10))

        # --- リファレンス音声の作り方ガイド ---
        guide_frame = ctk.CTkFrame(
            reg_card, fg_color="#fff8e1", corner_radius=8)
        guide_frame.pack(fill="x", padx=16, pady=(0, 12))
        guide_inner = ctk.CTkFrame(guide_frame, fg_color="transparent")
        guide_inner.pack(fill="x", padx=12, pady=10)

        ctk.CTkLabel(
            guide_inner, text="リファレンス音声の作り方",
            font=_ui_font(12, True), text_color="#e65100",
        ).pack(anchor="w", pady=(0, 4))

        tips = [
            "5〜15秒程度の短い音声を用意",
            "ノイズが少なく高音質なもの",
            "先頭・末尾が途切れていないもの",
            "テキストは音声の内容と正確に一致させる",
            "自分の声または許諾を得た音声を使用",
        ]
        for tip in tips:
            ctk.CTkLabel(
                guide_inner, text=f"  \u2022 {tip}",
                font=_ui_font(11), text_color="#bf360c",
                justify="left",
            ).pack(anchor="w")

        # --- フォーム本体 (2カラム) ---
        form = ctk.CTkFrame(reg_card, fg_color="transparent")
        form.pack(fill="x", padx=20, pady=(0, 16))
        form.grid_columnconfigure(0, weight=1)
        form.grid_columnconfigure(1, weight=1)

        # 左カラム
        left_col = ctk.CTkFrame(form, fg_color="transparent")
        left_col.grid(row=0, column=0, sticky="nsew", padx=(0, 10))

        ctk.CTkLabel(
            left_col, text="ボイス名", font=_ui_font(13, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 4))
        self.voice_name_entry = ctk.CTkEntry(
            left_col, placeholder_text="(例) ナレーター1号",
            font=_ui_font(13), corner_radius=6, height=34,
        )
        self.voice_name_entry.pack(fill="x", pady=(0, 10))

        ctk.CTkLabel(
            left_col, text="リファレンス音声", font=_ui_font(13, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(
            left_col,
            text="クローンしたい声の短いサンプル音声（WAV推奨）",
            font=_ui_font(11), text_color=self._CLR_TEXT_HINT,
        ).pack(anchor="w", pady=(0, 4))
        audio_row = ctk.CTkFrame(left_col, fg_color="transparent")
        audio_row.pack(fill="x", pady=(0, 4))

        self.browse_btn = ctk.CTkButton(
            audio_row, text="ファイル選択...",
            command=self._browse_audio_file, width=120, height=32,
            font=_ui_font(12), corner_radius=6,
            fg_color=self._CLR_BLUE, hover_color=self._CLR_BLUE_HOVER,
            text_color="white",
        )
        self.browse_btn.pack(side="left", padx=(0, 8))

        self.preview_btn = ctk.CTkButton(
            audio_row, text="\u25b6 再生",
            command=self._play_audio_preview, width=76, height=32,
            font=_ui_font(12), corner_radius=6,
            state="disabled",
        )
        self.preview_btn.pack(side="left")

        self.audio_path_label = ctk.CTkLabel(
            left_col, text="未選択", text_color=self._CLR_TEXT_HINT,
            font=_ui_font(11),
        )
        self.audio_path_label.pack(anchor="w", pady=(0, 6))

        ctk.CTkLabel(
            left_col, text="言語", font=_ui_font(13, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 4))
        self.lang_dropdown = ctk.CTkComboBox(
            left_col, values=self.LANGUAGES, state="readonly",
            font=_ui_font(13), corner_radius=6, height=34,
        )
        self.lang_dropdown.set("Japanese")
        self.lang_dropdown.pack(fill="x")

        # 右カラム
        right_col = ctk.CTkFrame(form, fg_color="transparent")
        right_col.grid(row=0, column=1, sticky="nsew", padx=(10, 0))

        ctk.CTkLabel(
            right_col, text="リファレンステキスト", font=_ui_font(13, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(
            right_col,
            text="上の音声で話している内容を正確に書き起こしてください",
            font=_ui_font(11), text_color=self._CLR_TEXT_HINT,
        ).pack(anchor="w", pady=(0, 4))
        self.ref_text_entry = ctk.CTkTextbox(
            right_col, height=140, font=_ui_font(13), corner_radius=6,
        )
        self.ref_text_entry.pack(fill="x", pady=(0, 10))

        # 保存ボタン + ステータス (フォーム下部に横幅フル)
        bottom_row = ctk.CTkFrame(reg_card, fg_color="transparent")
        bottom_row.pack(fill="x", padx=20, pady=(0, 16))

        self.save_voice_btn = ctk.CTkButton(
            bottom_row, text="ボイスを保存", command=self._save_voice,
            fg_color="#4CAF50", hover_color="#43a047",
            text_color="white",
            font=_ui_font(14, True), height=40, corner_radius=6,
            width=200,
        )
        self.save_voice_btn.pack(side="left", padx=(0, 12))

        self._voice_status_frame = ctk.CTkFrame(
            bottom_row, fg_color="transparent", corner_radius=6)
        self._voice_status_frame.pack(side="left", fill="x", expand=True)
        self.voice_status = ctk.CTkLabel(
            self._voice_status_frame, text="",
            text_color=self._CLR_TEXT_SUB,
            font=_ui_font(12), wraplength=400,
        )

    # --- 音声合成タブ ---

    # -- UI色定数 (Web UIに合わせた統一カラー) --
    _CLR_CARD_BG = "#ffffff"           # カード背景
    _CLR_ITEM_BG = "#f5f5f5"           # 結果行・履歴行の背景
    _CLR_ITEM_HOVER = "#eeeeee"
    _CLR_PRIMARY = "#ff9800"           # 合成ボタン (オレンジ)
    _CLR_PRIMARY_HOVER = "#f57c00"
    _CLR_BLUE = "#2196F3"              # 保存ボタン
    _CLR_BLUE_HOVER = "#1976D2"
    _CLR_RED = "#ef5350"               # 削除・クリア
    _CLR_RED_HOVER = "#e53935"
    _CLR_SUCCESS_BG = "#e8f5e9"
    _CLR_SUCCESS_FG = "#2e7d32"
    _CLR_ERROR_BG = "#ffebee"
    _CLR_ERROR_FG = "#c62828"
    _CLR_TEXT = "#333333"
    _CLR_TEXT_SUB = "#777777"
    _CLR_TEXT_HINT = "#999999"
    _CLR_PROGRESS = "#ff9800"
    _CLR_SEPARATOR = "#e0e0e0"

    def _build_synth_tab(self):
        # サーバー未起動時のオーバーレイ
        self._synth_overlay = ctk.CTkFrame(self.tab_synth, fg_color="transparent")
        self._synth_overlay.pack(fill="both", expand=True)
        ctk.CTkLabel(
            self._synth_overlay,
            text="サーバーを起動すると音声合成が利用できます",
            font=_ui_font(14), text_color=self._CLR_TEXT_HINT,
        ).place(relx=0.5, rely=0.45, anchor="center")

        self._synth_content = ctk.CTkScrollableFrame(
            self.tab_synth, fg_color="transparent")

        frame = self._synth_content

        # ========== 音声合成セクション ==========
        synth_card = ctk.CTkFrame(
            frame, corner_radius=10, fg_color=self._CLR_CARD_BG)
        synth_card.pack(fill="x", padx=6, pady=(6, 10))
        inner = ctk.CTkFrame(synth_card, fg_color="transparent")
        inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            inner, text="音声合成", font=_ui_font(16, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(
            inner,
            text="登録済みボイスでテキスト音声合成をテストできます。",
            font=_ui_font(12), text_color=self._CLR_TEXT_SUB,
        ).pack(anchor="w", pady=(0, 10))

        # スピーカー選択
        ctk.CTkLabel(
            inner, text="スピーカー", font=_ui_font(13, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 4))
        self.speaker_dropdown = ctk.CTkComboBox(
            inner, values=["-- サーバー起動後に選択 --"],
            state="readonly", font=_ui_font(13),
            height=34, corner_radius=6,
        )
        self.speaker_dropdown.pack(fill="x", pady=(0, 12))

        # テキスト入力
        ctk.CTkLabel(
            inner, text="テキスト", font=_ui_font(13, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 4))
        self.synth_text = ctk.CTkTextbox(
            inner, height=80, font=_ui_font(13), corner_radius=6,
        )
        self.synth_text.insert("0.0", "こんにちは。音声合成のテストです。")
        self.synth_text.pack(fill="x", pady=(0, 14))

        # スライダー
        slider_frame = ctk.CTkFrame(inner, fg_color="transparent")
        slider_frame.pack(fill="x", pady=(0, 14))
        slider_frame.grid_columnconfigure(0, weight=1)
        slider_frame.grid_columnconfigure(1, weight=1)

        # 速度
        speed_box = ctk.CTkFrame(slider_frame, fg_color="transparent")
        speed_box.grid(row=0, column=0, sticky="ew", padx=(0, 16))

        speed_label_row = ctk.CTkFrame(speed_box, fg_color="transparent")
        speed_label_row.pack(fill="x")
        ctk.CTkLabel(
            speed_label_row, text="速度", font=_ui_font(13),
            text_color=self._CLR_TEXT_SUB,
        ).pack(side="left")
        self.speed_val_label = ctk.CTkLabel(
            speed_label_row, text="1.0x", font=_ui_font(13, True),
            text_color=self._CLR_PRIMARY,
        )
        self.speed_val_label.pack(side="right")

        self.speed_slider = ctk.CTkSlider(
            speed_box, from_=0.5, to=2.0, number_of_steps=15,
            command=self._on_speed_change,
            button_color=self._CLR_PRIMARY,
            button_hover_color=self._CLR_PRIMARY_HOVER,
            progress_color=self._CLR_PRIMARY,
        )
        self.speed_slider.set(1.0)
        self.speed_slider.pack(fill="x", pady=(4, 0))

        # 音量
        vol_box = ctk.CTkFrame(slider_frame, fg_color="transparent")
        vol_box.grid(row=0, column=1, sticky="ew", padx=(16, 0))

        vol_label_row = ctk.CTkFrame(vol_box, fg_color="transparent")
        vol_label_row.pack(fill="x")
        ctk.CTkLabel(
            vol_label_row, text="音量", font=_ui_font(13),
            text_color=self._CLR_TEXT_SUB,
        ).pack(side="left")
        self.volume_val_label = ctk.CTkLabel(
            vol_label_row, text="1.0x", font=_ui_font(13, True),
            text_color=self._CLR_PRIMARY,
        )
        self.volume_val_label.pack(side="right")

        self.volume_slider = ctk.CTkSlider(
            vol_box, from_=0.1, to=2.0, number_of_steps=19,
            command=self._on_volume_change,
            button_color=self._CLR_PRIMARY,
            button_hover_color=self._CLR_PRIMARY_HOVER,
            progress_color=self._CLR_PRIMARY,
        )
        self.volume_slider.set(1.0)
        self.volume_slider.pack(fill="x", pady=(4, 0))

        # ボタン行
        btn_row = ctk.CTkFrame(inner, fg_color="transparent")
        btn_row.pack(fill="x", pady=(0, 6))

        self.synthesize_btn = ctk.CTkButton(
            btn_row, text="合成", command=self._synthesize,
            fg_color=self._CLR_PRIMARY, hover_color=self._CLR_PRIMARY_HOVER,
            text_color="white",
            font=_ui_font(14, True), corner_radius=6,
            width=130, height=38,
        )
        self.synthesize_btn.pack(side="left", padx=(0, 8))

        self.play_synth_btn = ctk.CTkButton(
            btn_row, text="\u25b6 再生", command=self._play_synthesized,
            font=_ui_font(13), corner_radius=6,
            width=90, height=38, state="disabled",
        )
        self.play_synth_btn.pack(side="left", padx=(0, 8))

        self.save_wav_btn = ctk.CTkButton(
            btn_row, text="\u2b07 WAV保存", command=self._save_synthesized_wav,
            fg_color=self._CLR_BLUE, hover_color=self._CLR_BLUE_HOVER,
            text_color="white",
            font=_ui_font(13), corner_radius=6,
            width=120, height=38, state="disabled",
        )
        self.save_wav_btn.pack(side="left")

        # ステータス（経過時間表示付き）
        self._synth_status_frame = ctk.CTkFrame(
            inner, fg_color="transparent", corner_radius=6, height=0)
        self._synth_status_frame.pack(fill="x", pady=(2, 0))
        self.synth_status = ctk.CTkLabel(
            self._synth_status_frame, text="",
            text_color=self._CLR_TEXT_SUB, font=_ui_font(12),
        )

        # ========== 一括合成セクション ==========
        batch_card = ctk.CTkFrame(
            frame, corner_radius=10, fg_color=self._CLR_CARD_BG)
        batch_card.pack(fill="x", padx=6, pady=(0, 10))
        batch_inner = ctk.CTkFrame(batch_card, fg_color="transparent")
        batch_inner.pack(fill="x", padx=20, pady=16)

        ctk.CTkLabel(
            batch_inner, text="一括合成", font=_ui_font(16, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 2))
        ctk.CTkLabel(
            batch_inner, text="複数行のテキストを1行ずつ順番に合成します",
            font=_ui_font(12), text_color=self._CLR_TEXT_SUB,
        ).pack(anchor="w", pady=(0, 10))

        ctk.CTkLabel(
            batch_inner, text="テキスト (1行 = 1音声)", font=_ui_font(13, True),
            text_color=self._CLR_TEXT,
        ).pack(anchor="w", pady=(0, 4))
        self.batch_text = ctk.CTkTextbox(
            batch_inner, height=100, font=_ui_font(13), corner_radius=6,
        )
        self.batch_text.insert("0.0", "こんにちは。\n今日はいい天気ですね。\nよろしくお願いします。")
        self.batch_text.pack(fill="x", pady=(0, 14))

        batch_btn_row = ctk.CTkFrame(batch_inner, fg_color="transparent")
        batch_btn_row.pack(fill="x", pady=(0, 6))

        self.batch_synth_btn = ctk.CTkButton(
            batch_btn_row, text="一括合成", command=self._batch_synthesize,
            fg_color=self._CLR_PRIMARY, hover_color=self._CLR_PRIMARY_HOVER,
            text_color="white",
            font=_ui_font(14, True), corner_radius=6,
            width=130, height=38,
        )
        self.batch_synth_btn.pack(side="left", padx=(0, 8))

        self.batch_save_all_btn = ctk.CTkButton(
            batch_btn_row, text="\u2b07 全てWAV保存", command=self._batch_save_all,
            fg_color=self._CLR_BLUE, hover_color=self._CLR_BLUE_HOVER,
            text_color="white",
            font=_ui_font(13), corner_radius=6,
            width=140, height=38, state="disabled",
        )
        self.batch_save_all_btn.pack(side="left")

        # プログレスバー
        self._batch_progress_frame = ctk.CTkFrame(
            batch_inner, fg_color="transparent")
        self.batch_progress = ctk.CTkProgressBar(
            self._batch_progress_frame,
            progress_color=self._CLR_PROGRESS, corner_radius=3, height=6)
        self.batch_progress.set(0)
        self.batch_progress.pack(side="left", fill="x", expand=True, padx=(0, 10))
        self.batch_progress_label = ctk.CTkLabel(
            self._batch_progress_frame, text="",
            font=_ui_font(12), text_color=self._CLR_TEXT_SUB,
        )
        self.batch_progress_label.pack(side="left")

        # 一括合成結果リスト
        self._batch_results_frame = ctk.CTkFrame(
            batch_inner, fg_color="transparent")
        self._batch_results: list[dict] = []

        # ========== 合成履歴セクション ==========
        hist_card = ctk.CTkFrame(
            frame, corner_radius=10, fg_color=self._CLR_CARD_BG)
        hist_card.pack(fill="x", padx=6, pady=(0, 10))
        hist_inner = ctk.CTkFrame(hist_card, fg_color="transparent")
        hist_inner.pack(fill="x", padx=20, pady=16)

        hist_title_row = ctk.CTkFrame(hist_inner, fg_color="transparent")
        hist_title_row.pack(fill="x", pady=(0, 2))
        ctk.CTkLabel(
            hist_title_row, text="合成履歴", font=_ui_font(16, True),
            text_color=self._CLR_TEXT,
        ).pack(side="left")
        self.clear_history_btn = ctk.CTkButton(
            hist_title_row, text="クリア", command=self._clear_history,
            fg_color=self._CLR_RED, hover_color=self._CLR_RED_HOVER,
            text_color="white",
            font=_ui_font(11), corner_radius=6,
            width=64, height=28,
        )
        self.clear_history_btn.pack(side="right")

        ctk.CTkLabel(
            hist_inner, text="最新20件を表示（アプリ終了でクリアされます）",
            font=_ui_font(12), text_color=self._CLR_TEXT_SUB,
        ).pack(anchor="w", pady=(0, 8))

        self._history_frame = ctk.CTkFrame(hist_inner, fg_color="transparent")
        self._history_frame.pack(fill="x")
        self._synth_history: list[dict] = []

        self._history_empty_label = ctk.CTkLabel(
            self._history_frame, text="まだ合成履歴がありません",
            font=_ui_font(12), text_color=self._CLR_TEXT_HINT,
        )
        self._history_empty_label.pack(anchor="w")

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
            self._synth_content.pack(fill="both", expand=True, padx=4, pady=4)
        else:
            self._synth_content.pack_forget()
            self._synth_overlay.pack(fill="both", expand=True)

        # VOICEVOX タブの稼働状況更新
        self._vv_status_dot.configure(text_color=cfg["color"])
        if self.state == self.STATE_RUNNING:
            port = self.config.get("port", 50021)
            url = f"http://localhost:{port}"
            self._url_var.set(url)
            self._vv_status_label.configure(text=f"稼働中 — {url}")
            self._vv_url_label.configure(text=url)
            self._vv_url_row.pack(fill="x", pady=(6, 0))
            self._vv_url_hint.pack(anchor="w", pady=(4, 0))
            self.open_browser_btn.configure(state="normal")
        elif self.state == self.STATE_LOADING:
            self._vv_status_label.configure(text="モデル読み込み中...")
            self._vv_url_label.configure(text="")
            self._url_var.set("")
            self._vv_url_row.pack_forget()
            self._vv_url_hint.pack_forget()
            self.open_browser_btn.configure(state="disabled")
        else:
            self._vv_status_label.configure(text="停止中")
            self._vv_url_label.configure(text="")
            self._url_var.set("")
            self._vv_url_row.pack_forget()
            self._vv_url_hint.pack_forget()
            self.open_browser_btn.configure(state="disabled")

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

    _STEPS_FIRST_SETUP = [
        ("setup", "環境セットアップ"),
        ("model_dl", "モデルダウンロード (~4.5GB)"),
    ]
    _STEP_PKG_UPDATE = ("pkg_update", "パッケージ更新")
    _STEP_MODEL_DL = ("model_dl", "モデルダウンロード (~4.5GB)")
    _STEPS_SERVER = [
        ("start", "サーバー起動"),
        ("model", "モデル読み込み"),
        ("ready", "準備完了"),
    ]

    def _open_progress(self, steps=None):
        self._progress_steps = steps or self._STEPS_SERVER
        self._progress_current = None
        self._progress_animating = True
        self._spinner_idx = 0

        dlg = ctk.CTkToplevel(self.root)
        dlg.title("")
        dlg.overrideredirect(True)  # タイトルバーなし
        dlg.attributes("-topmost", True)
        self._progress_dlg = dlg

        # ウィンドウを親の中央に配置 (ステップ数で高さ調整)
        n = len(self._progress_steps)
        w, h = 460, max(200, 140 + n * 50)
        self.root.update()
        root_x = self.root.winfo_x()
        root_y = self.root.winfo_y()
        root_w = self.root.winfo_width()
        root_h = self.root.winfo_height()
        px = root_x + (root_w - w) // 2
        py = root_y + (root_h - h) // 2
        # 画面外にはみ出さないようクランプ
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        px = max(0, min(px, sw - w))
        py = max(0, min(py, sh - h))
        dlg.geometry(f"{w}x{h}+{px}+{py}")

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
        self.root.update()
        rx, ry = self.root.winfo_x(), self.root.winfo_y()
        rw, rh = self.root.winfo_width(), self.root.winfo_height()
        px = rx + (rw - w) // 2
        py = ry + (rh - h) // 2
        sw = self.root.winfo_screenwidth()
        sh = self.root.winfo_screenheight()
        px = max(0, min(px, sw - w))
        py = max(0, min(py, sh - h))
        self._progress_dlg.geometry(f"{w}x{h}+{px}+{py}")

        ctk.CTkButton(
            self._progress_dlg.winfo_children()[0].winfo_children()[0],
            text="閉じる", command=self._close_progress,
            fg_color="#c62828", hover_color="#b71c1c",
            text_color="white",
            font=_ui_font(13), width=100, height=32,
        ).pack(pady=(0, 12))

    # ===================================================================
    # 自動セットアップ
    # ===================================================================

    def _check_auto_setup(self):
        from launcher import is_setup_needed, is_update_needed, is_model_downloaded
        if is_setup_needed():
            # 初回: 確認ダイアログ → 環境構築 + モデルDL
            ok = messagebox.askyesno(
                "SelfVox - 初回セットアップ",
                "初回起動のため、以下を自動でセットアップします。\n\n"
                "  - Python 環境の構築\n"
                "  - PyTorch + CUDA のインストール (~5GB)\n"
                "  - 音声合成モデルのダウンロード (~4.5GB)\n\n"
                "合計 約10GB のダウンロードが必要です。\n"
                "インターネット接続が必要です。\n"
                "数分〜十数分かかります。\n\n"
                "セットアップを開始しますか？\n"
                "「いいえ」を選択するとアプリを終了します。")
            if ok:
                self._run_auto_setup(first_setup=True)
            else:
                self.root.destroy()
                return
        else:
            # 2回目以降: パッケージ・モデル更新チェック
            need_pkg = is_update_needed()
            need_model = not is_model_downloaded()
            if need_pkg or need_model:
                items = []
                if need_pkg:
                    items.append("  - パッケージの更新")
                if need_model:
                    items.append("  - 音声合成モデルのダウンロード (~4.5GB)")
                detail = "\n".join(items)
                ok = messagebox.askyesno(
                    "SelfVox - 更新",
                    f"以下の更新が必要です。\n\n{detail}\n\n"
                    "インターネット接続が必要です。\n\n"
                    "更新を開始しますか？")
                if ok:
                    self._run_auto_setup(first_setup=False,
                                         need_pkg=need_pkg,
                                         need_model=need_model)
            elif self.config.get("auto_start", True):
                self._on_start()

    def _run_auto_setup(self, first_setup: bool, *,
                        need_pkg: bool = False,
                        need_model: bool = False):
        self.tabview.set("ログ")
        if first_setup:
            steps = self._STEPS_FIRST_SETUP
        else:
            steps = []
            if need_pkg:
                steps.append(self._STEP_PKG_UPDATE)
            if need_model:
                steps.append(self._STEP_MODEL_DL)
        self._open_progress(steps=steps)
        threading.Thread(
            target=self._setup_worker,
            args=(first_setup, need_pkg, need_model),
            daemon=True).start()

    def _setup_worker(self, first_setup: bool,
                      need_pkg: bool, need_model: bool):
        from launcher import (setup_environment, update_packages,
                               download_model)
        try:
            self.root.after(0, self._set_state, self.STATE_SETTING_UP)
            if first_setup:
                self._update_progress("setup")
                setup_environment(log_callback=self._append_log)
                self._update_progress("model_dl")
                download_model(log_callback=self._append_log)
            else:
                if need_pkg:
                    self._update_progress("pkg_update")
                    update_packages(log_callback=self._append_log)
                if need_model:
                    self._update_progress("model_dl")
                    download_model(log_callback=self._append_log)

            self._complete_progress()
            self.root.after(0, self._set_state, self.STATE_IDLE)
        except Exception as e:
            self._append_log(f"エラー: {e}")
            self._fail_progress(str(e))
            self.root.after(0, self._set_state, self.STATE_IDLE)

    # ===================================================================
    # ワーカースレッド
    # ===================================================================

    def _worker_run(self):
        from launcher import start_server_process

        try:
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

    # -- 録音ポップアップ ------------------------------------------------

    _RECORD_SR = 44100   # サンプルレート
    _RECORD_CH = 1       # モノラル

    def _open_record_dialog(self):
        """録音ポップアップダイアログを開く"""
        dlg = ctk.CTkToplevel(self.root)
        dlg.title("ボイス録音")
        dlg.geometry("500x440")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        # ダイアログローカル状態
        dlg._recording = False
        dlg._record_stream = None
        dlg._record_frames: list = []
        dlg._recorded_path: str | None = None
        dlg._record_start_time: float = 0.0

        pad = 24

        # ボイス名
        ctk.CTkLabel(dlg, text="ボイス名:", font=_ui_font(13)).place(
            x=pad, y=pad)
        name_entry = ctk.CTkEntry(
            dlg, placeholder_text="(例) ナレーター1号",
            font=_ui_font(13), width=300)
        name_entry.place(x=pad + 80, y=pad - 2)

        # 言語
        ctk.CTkLabel(dlg, text="言語:", font=_ui_font(13)).place(
            x=pad, y=pad + 44)
        lang_cb = ctk.CTkComboBox(
            dlg, values=self.LANGUAGES, state="readonly",
            font=_ui_font(13), width=200)
        lang_cb.set("Japanese")
        lang_cb.place(x=pad + 80, y=pad + 42)

        # 読み上げテキスト
        ctk.CTkLabel(dlg, text="読み上げテキスト:", font=_ui_font(13)).place(
            x=pad, y=pad + 92)
        script_box = ctk.CTkTextbox(
            dlg, height=80, font=_ui_font(13), width=452,
            border_width=1, border_color="#cccccc")
        script_box.place(x=pad, y=pad + 116)
        script_box.insert(
            "0.0",
            "吾輩は猫である。名前はまだ無い。"
            "どこで生まれたかとんと見当がつかぬ。")

        # 録音 / 再生 ボタン行
        btn_y = pad + 210

        rec_btn = ctk.CTkButton(
            dlg, text="\u25cf 録音", width=100, height=36,
            fg_color="#c62828", hover_color="#b71c1c",
            text_color="white",
            font=_ui_font(13, True))
        rec_btn.place(x=pad, y=btn_y)

        play_btn = ctk.CTkButton(
            dlg, text="\u25b6 再生", width=80, height=36,
            font=_ui_font(13), state="disabled")
        play_btn.place(x=pad + 112, y=btn_y)

        time_label = ctk.CTkLabel(
            dlg, text="", font=_mono_font(13), text_color="#888888")
        time_label.place(x=pad + 210, y=btn_y + 6)

        # ステータス
        status_label = ctk.CTkLabel(
            dlg, text="録音してください", font=_ui_font(12),
            text_color="#888888")
        status_label.place(x=pad, y=btn_y + 52)

        # 登録 / キャンセル ボタン
        bottom_y = 390

        register_btn = ctk.CTkButton(
            dlg, text="登録", width=100, height=36,
            fg_color="#4CAF50", hover_color="#43a047",
            text_color="white",
            font=_ui_font(13, True), state="disabled")
        register_btn.place(x=500 - pad - 100 - 8 - 100, y=bottom_y)

        ctk.CTkButton(
            dlg, text="キャンセル", width=100, height=36,
            fg_color="transparent", hover_color="#e0e0e0",
            text_color="#333333", border_width=1, border_color="#999999",
            font=_ui_font(13),
            command=lambda: _on_cancel(),
        ).place(x=500 - pad - 100, y=bottom_y)

        # --- 録音タイマー ---
        def _update_timer():
            if not dlg.winfo_exists():
                return
            if not dlg._recording:
                return
            import time as _time
            elapsed = _time.time() - dlg._record_start_time
            mins = int(elapsed) // 60
            secs = int(elapsed) % 60
            time_label.configure(text=f"{mins:02d}:{secs:02d}")
            dlg.after(200, _update_timer)

        # --- 録音開始/停止 ---
        def _toggle_rec():
            if dlg._recording:
                _stop_rec()
            else:
                _start_rec()

        def _start_rec():
            try:
                import sounddevice as sd
            except ImportError:
                messagebox.showerror(
                    "録音エラー",
                    "sounddevice がインストールされていません。",
                    parent=dlg)
                return

            dlg._record_frames.clear()

            def cb(indata, frames, time_info, st):
                dlg._record_frames.append(indata.copy())

            try:
                dlg._record_stream = sd.InputStream(
                    samplerate=self._RECORD_SR,
                    channels=self._RECORD_CH,
                    dtype="int16",
                    callback=cb)
                dlg._record_stream.start()
                dlg._recording = True
                import time as _time
                dlg._record_start_time = _time.time()
                rec_btn.configure(
                    text="\u25a0 停止", fg_color="#333333",
                    hover_color="#555555")
                play_btn.configure(state="disabled")
                register_btn.configure(state="disabled")
                status_label.configure(
                    text="録音中...", text_color="#c62828")
                _update_timer()
            except Exception as e:
                messagebox.showerror("録音エラー", str(e), parent=dlg)

        def _stop_rec():
            import numpy as np

            if dlg._record_stream is not None:
                dlg._record_stream.stop()
                dlg._record_stream.close()
                dlg._record_stream = None
            dlg._recording = False
            rec_btn.configure(
                text="\u25cf 録音", fg_color="#c62828",
                hover_color="#b71c1c")

            if not dlg._record_frames:
                status_label.configure(
                    text="録音データがありません", text_color="#c62828")
                return

            audio = np.concatenate(dlg._record_frames, axis=0)
            dlg._record_frames.clear()

            tmp = tempfile.NamedTemporaryFile(
                suffix=".wav", delete=False, dir=tempfile.gettempdir())
            tmp.close()
            with wave.open(tmp.name, "wb") as wf:
                wf.setnchannels(self._RECORD_CH)
                wf.setsampwidth(2)
                wf.setframerate(self._RECORD_SR)
                wf.writeframes(audio.tobytes())

            dlg._recorded_path = tmp.name
            play_btn.configure(state="normal")
            register_btn.configure(state="normal")

            duration = len(audio) / self._RECORD_SR
            mins = int(duration) // 60
            secs = int(duration) % 60
            time_label.configure(text=f"{mins:02d}:{secs:02d}")
            status_label.configure(
                text="録音完了 — 再生で確認してください",
                text_color="#2e7d32")

        rec_btn.configure(command=_toggle_rec)

        # --- 再生 ---
        def _play_rec():
            if dlg._recorded_path and os.path.exists(dlg._recorded_path):
                self._play_wav(dlg._recorded_path, toggle_btn=play_btn)

        play_btn.configure(command=_play_rec)

        # --- 登録 ---
        def _register():
            name = name_entry.get().strip()
            language = lang_cb.get()
            ref_text = script_box.get("0.0", "end").strip()
            audio_path = dlg._recorded_path

            if not name:
                status_label.configure(
                    text="ボイス名を入力してください", text_color="#c62828")
                return
            if not audio_path:
                status_label.configure(
                    text="録音してください", text_color="#c62828")
                return
            if not ref_text:
                status_label.configure(
                    text="読み上げテキストを入力してください",
                    text_color="#c62828")
                return

            register_btn.configure(state="disabled", text="登録中...")
            try:
                dir_name = name.replace(" ", "_").replace("/", "_")
                voice_dir = self._voices_dir() / dir_name
                voice_dir.mkdir(parents=True, exist_ok=True)

                meta_path = voice_dir / "meta.json"
                if meta_path.exists():
                    with open(meta_path, encoding="utf-8") as f:
                        meta = json.load(f)
                else:
                    used_ids = set()
                    for d in self._voices_dir().iterdir():
                        if not d.is_dir():
                            continue
                        mp = d / "meta.json"
                        if mp.exists():
                            try:
                                with open(mp, encoding="utf-8") as mf:
                                    used_ids.add(
                                        json.load(mf).get("speaker_id", 0))
                            except Exception:
                                pass
                    new_id = 0
                    while new_id in used_ids:
                        new_id += 1
                    meta = {
                        "speaker_id": new_id,
                        "speaker_uuid":
                            f"00000000-0000-0000-0000-{new_id:012d}",
                        "styles": [{"name": "ノーマル", "id": new_id,
                                    "type": "talk"}],
                        "ref_audio": "reference.wav",
                    }

                meta["name"] = name
                meta["ref_text"] = ref_text
                meta["language"] = language

                shutil.copy2(audio_path, voice_dir / "reference.wav")
                with open(meta_path, "w", encoding="utf-8") as f:
                    json.dump(meta, f, ensure_ascii=False, indent=2)

                dlg.destroy()
                self._refresh_voice_list()
                if self.state == self.STATE_RUNNING:
                    self._notify_server_reload_voices()
            except Exception as e:
                status_label.configure(
                    text=f"エラー: {e}", text_color="#c62828")
                register_btn.configure(state="normal", text="登録")

        register_btn.configure(command=_register)

        # --- キャンセル ---
        def _on_cancel():
            if dlg._recording and dlg._record_stream is not None:
                dlg._record_stream.stop()
                dlg._record_stream.close()
                dlg._recording = False
            dlg.destroy()

        dlg.protocol("WM_DELETE_WINDOW", _on_cancel)

    def _save_voice(self):
        name = self.voice_name_entry.get().strip()
        ref_text = self.ref_text_entry.get("0.0", "end").strip()
        language = self.lang_dropdown.get()
        audio_path = self._selected_audio_path

        if not name:
            self._show_voice_status("ボイス名を入力してください", error=True)
            return
        if not audio_path:
            self._show_voice_status("音声ファイルを選択してください", error=True)
            return
        if not ref_text:
            self._show_voice_status("リファレンステキストを入力してください", error=True)
            return

        self.save_voice_btn.configure(state="disabled", text="保存中...")
        self._show_voice_status("")

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
        self._show_voice_status(msg, success=True)
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
        self._show_voice_status(f"エラー: {err}", error=True)
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

        for w in self.voice_list_frame.winfo_children():
            w.destroy()

        if not voices:
            ctk.CTkLabel(
                self.voice_list_frame,
                text="ボイスが未登録です。\n下のフォームから登録してください。",
                text_color=self._CLR_TEXT_HINT, font=_ui_font(12),
            ).pack(pady=20)
            return

        for v in voices:
            self._create_voice_item(v)

    def _create_voice_item(self, voice: dict):
        item = ctk.CTkFrame(
            self.voice_list_frame,
            fg_color=self._CLR_ITEM_BG, corner_radius=8)
        item.pack(fill="x", pady=(0, 6))

        item_inner = ctk.CTkFrame(item, fg_color="transparent")
        item_inner.pack(fill="x", padx=14, pady=10)

        # ボタンを先にpackして領域を確保
        btn_frame = ctk.CTkFrame(item_inner, fg_color="transparent")
        btn_frame.pack(side="right")

        dir_name = voice["dir_name"]
        if voice.get("has_audio"):
            play_btn = ctk.CTkButton(
                btn_frame, text="\u25b6 再生", width=72, height=30,
                font=_ui_font(11), corner_radius=6,
            )
            play_btn.configure(
                command=lambda d=dir_name, b=play_btn:
                    self._play_reference_audio(d, b))
            play_btn.pack(side="left", padx=(0, 6))

        ctk.CTkButton(
            btn_frame, text="削除", width=60, height=30,
            fg_color=self._CLR_RED, hover_color=self._CLR_RED_HOVER,
            text_color="white",
            font=_ui_font(11), corner_radius=6,
            command=lambda d=dir_name: self._delete_voice(d),
        ).pack(side="left")

        # 情報は残りの領域を使う
        info = ctk.CTkFrame(item_inner, fg_color="transparent")
        info.pack(side="left", fill="x", expand=True)

        ctk.CTkLabel(
            info, text=voice["name"],
            font=_ui_font(14, True), text_color=self._CLR_TEXT,
        ).pack(anchor="w")

        meta_text = f"Speaker ID: {voice['speaker_id']}"
        lang = voice.get("language", "")
        if lang:
            meta_text += f" / {lang}"
        ctk.CTkLabel(
            info, text=meta_text,
            font=_ui_font(11), text_color=self._CLR_TEXT_SUB,
        ).pack(anchor="w")

        ref_text = voice.get("ref_text", "")
        if ref_text:
            display = ref_text[:50] + "..." if len(ref_text) > 50 else ref_text
            ctk.CTkLabel(
                info, text=display,
                font=_ui_font(11), text_color=self._CLR_TEXT_HINT,
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
            self._show_synth_status("スピーカーを選択してください", error=True)
            return
        if not text:
            self._show_synth_status("テキストを入力してください", error=True)
            return

        speed = self.speed_slider.get()
        volume = self.volume_slider.get()

        self.synthesize_btn.configure(state="disabled", text="合成中...")
        self.play_synth_btn.configure(state="disabled")
        self.save_wav_btn.configure(state="disabled")

        # 経過時間タイマー
        import time as _time
        self._synth_start_time = _time.time()
        self._synth_timer_id = None

        def update_elapsed():
            elapsed = _time.time() - self._synth_start_time
            self._show_synth_status(f"生成中... {elapsed:.1f}秒")
            self._synth_timer_id = self.root.after(100, update_elapsed)

        update_elapsed()

        speaker_name = self.speaker_dropdown.get()

        def worker():
            try:
                query = self.api.audio_query(text, speaker_id)
                query["speedScale"] = speed
                query["volumeScale"] = volume
                wav_bytes = self.api.synthesize(speaker_id, query)
                self.root.after(0, self._on_synthesis_complete,
                                wav_bytes, text, speaker_name)
            except Exception as e:
                self.root.after(0, self._on_synthesis_error, str(e))

        threading.Thread(target=worker, daemon=True).start()

    def _stop_synth_timer(self):
        if hasattr(self, "_synth_timer_id") and self._synth_timer_id:
            self.root.after_cancel(self._synth_timer_id)
            self._synth_timer_id = None

    def _on_synthesis_complete(self, wav_bytes: bytes,
                               text: str = "", speaker_name: str = ""):
        self._stop_synth_timer()
        import time as _time
        elapsed = _time.time() - self._synth_start_time

        self._last_synth_wav = wav_bytes
        temp_path = os.path.join(tempfile.gettempdir(), "selfvox_synth.wav")
        with open(temp_path, "wb") as f:
            f.write(wav_bytes)
        self._last_synth_path = temp_path

        self.synthesize_btn.configure(state="normal", text="合成")
        self.play_synth_btn.configure(state="normal")
        self.save_wav_btn.configure(state="normal")
        self._show_synth_status(f"合成完了 ({elapsed:.1f}秒)", success=True)

        self._play_wav(temp_path, toggle_btn=self.play_synth_btn)

        # 履歴に追加
        if text:
            self._add_history(text, speaker_name, wav_bytes)

    def _on_synthesis_error(self, err: str):
        self._stop_synth_timer()
        self.synthesize_btn.configure(state="normal", text="合成")
        self._show_synth_status(f"エラー: {err}", error=True)

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
            self._show_synth_status(
                f"保存しました: {Path(path).name}", success=True)

    # --- 一括合成 ---

    def _batch_synthesize(self):
        speaker_id = self._get_selected_speaker_id()
        raw = self.batch_text.get("0.0", "end").strip()
        if speaker_id is None:
            return
        lines = [l.strip() for l in raw.split("\n") if l.strip()]
        if not lines:
            return

        speed = self.speed_slider.get()
        volume = self.volume_slider.get()
        speaker_name = self.speaker_dropdown.get()

        self.batch_synth_btn.configure(state="disabled", text="合成中...")
        self.batch_save_all_btn.configure(state="disabled")
        self._batch_progress_frame.pack(fill="x", pady=(4, 4))
        self.batch_progress.set(0)
        self.batch_progress_label.configure(text=f"0/{len(lines)}")

        # 結果リストクリア
        for w in self._batch_results_frame.winfo_children():
            w.destroy()
        self._batch_results_frame.pack(fill="x", pady=(4, 0))
        self._batch_results.clear()

        def worker():
            for i, text in enumerate(lines):
                self.root.after(0, self._batch_update_progress,
                                i, len(lines))
                try:
                    query = self.api.audio_query(text, speaker_id)
                    query["speedScale"] = speed
                    query["volumeScale"] = volume
                    wav_bytes = self.api.synthesize(speaker_id, query)
                    self.root.after(0, self._batch_add_result,
                                    text, wav_bytes, speaker_name, i)
                except Exception as e:
                    self.root.after(0, self._batch_add_error, text, str(e))
            self.root.after(0, self._batch_complete, len(lines))

        threading.Thread(target=worker, daemon=True).start()

    def _batch_update_progress(self, current: int, total: int):
        self.batch_progress.set(current / total)
        self.batch_progress_label.configure(text=f"{current + 1}/{total}")

    def _build_batch_row(self, text: str, index: int,
                         speaker_name: str, temp_path: str) -> ctk.CTkFrame:
        """一括合成の結果行をカード風に構築"""
        row = ctk.CTkFrame(
            self._batch_results_frame,
            fg_color=self._CLR_ITEM_BG, corner_radius=8)
        row.pack(fill="x", pady=(0, 4))

        row_inner = ctk.CTkFrame(row, fg_color="transparent")
        row_inner.pack(fill="x", padx=12, pady=8)

        # 番号ラベル
        ctk.CTkLabel(
            row_inner, text=f"{index + 1}.",
            font=_ui_font(12, True), text_color=self._CLR_PRIMARY,
            width=24,
        ).pack(side="left")

        # テキスト
        text_display = text if len(text) <= 40 else text[:40] + "..."
        ctk.CTkLabel(
            row_inner, text=text_display,
            font=_ui_font(12), text_color=self._CLR_TEXT,
        ).pack(side="left", padx=(4, 8), fill="x", expand=True)

        # 再生ボタン
        batch_play_btn = ctk.CTkButton(
            row_inner, text="\u25b6 再生", width=64, height=28,
            font=_ui_font(11), corner_radius=6,
        )
        batch_play_btn.configure(
            command=lambda p=temp_path, b=batch_play_btn:
                self._play_wav(p, toggle_btn=b))
        batch_play_btn.pack(side="right", padx=(4, 0))

        # 再生成ボタン
        ctk.CTkButton(
            row_inner, text="\u21bb 再生成", width=76, height=28,
            font=_ui_font(11), corner_radius=6,
            fg_color=self._CLR_ITEM_BG, hover_color=self._CLR_ITEM_HOVER,
            text_color=self._CLR_TEXT_SUB,
            border_width=1, border_color=self._CLR_SEPARATOR,
            command=lambda t=text, idx=index, sn=speaker_name, r=row:
                self._batch_regenerate(t, idx, sn, r),
        ).pack(side="right", padx=(4, 0))

        return row

    def _batch_add_result(self, text: str, wav_bytes: bytes,
                          speaker_name: str, index: int):
        temp_path = os.path.join(
            tempfile.gettempdir(), f"selfvox_batch_{index}.wav")
        with open(temp_path, "wb") as f:
            f.write(wav_bytes)
        self._batch_results.append({
            "text": text, "wav_bytes": wav_bytes, "path": temp_path,
            "index": index, "speaker_name": speaker_name})

        self._build_batch_row(text, index, speaker_name, temp_path)

        # 履歴にも追加
        self._add_history(text, speaker_name, wav_bytes)

    def _batch_regenerate(self, text: str, index: int,
                          speaker_name: str, row: ctk.CTkFrame):
        """一括合成の1件を再生成"""
        speaker_id = self._get_selected_speaker_id()
        if speaker_id is None:
            return
        speed = self.speed_slider.get()
        volume = self.volume_slider.get()

        # ボタン無効化
        for w in row.winfo_children():
            for btn in w.winfo_children():
                if isinstance(btn, ctk.CTkButton):
                    btn.configure(state="disabled")

        def worker():
            try:
                query = self.api.audio_query(text, speaker_id)
                query["speedScale"] = speed
                query["volumeScale"] = volume
                wav_bytes = self.api.synthesize(speaker_id, query)
                self.root.after(0, _on_done, wav_bytes)
            except Exception as e:
                self.root.after(0, _on_error, str(e))

        def _on_done(wav_bytes: bytes):
            temp_path = os.path.join(
                tempfile.gettempdir(), f"selfvox_batch_{index}.wav")
            with open(temp_path, "wb") as f:
                f.write(wav_bytes)
            for item in self._batch_results:
                if item.get("index") == index:
                    item["wav_bytes"] = wav_bytes
                    item["path"] = temp_path
                    break
            # 行を再描画
            row.destroy()
            self._build_batch_row(text, index, speaker_name, temp_path)
            self._add_history(text, speaker_name, wav_bytes)

        def _on_error(err: str):
            for w in row.winfo_children():
                for btn in w.winfo_children():
                    if isinstance(btn, ctk.CTkButton):
                        btn.configure(state="normal")
            self._show_synth_status(f"再生成エラー: {err}", error=True)

        threading.Thread(target=worker, daemon=True).start()

    def _batch_add_error(self, text: str, err: str):
        row = ctk.CTkFrame(
            self._batch_results_frame,
            fg_color=self._CLR_ERROR_BG, corner_radius=8)
        row.pack(fill="x", pady=(0, 4))
        text_display = text if len(text) <= 35 else text[:35] + "..."
        ctk.CTkLabel(
            row, text=f"  {text_display}: {err}",
            font=_ui_font(12), text_color=self._CLR_ERROR_FG,
        ).pack(anchor="w", padx=12, pady=8)

    def _batch_complete(self, total: int):
        self.batch_progress.set(1.0)
        self.batch_progress_label.configure(text=f"完了 ({total}件)")
        self.batch_synth_btn.configure(state="normal", text="一括合成")
        if self._batch_results:
            self.batch_save_all_btn.configure(state="normal")

    def _show_synth_status(self, text: str, error: bool = False,
                           success: bool = False):
        """ステータスメッセージをカード風に表示"""
        if not text:
            self.synth_status.pack_forget()
            self._synth_status_frame.configure(fg_color="transparent")
            return
        if error:
            self._synth_status_frame.configure(fg_color=self._CLR_ERROR_BG)
            self.synth_status.configure(
                text=text, text_color=self._CLR_ERROR_FG)
        elif success:
            self._synth_status_frame.configure(fg_color=self._CLR_SUCCESS_BG)
            self.synth_status.configure(
                text=text, text_color=self._CLR_SUCCESS_FG)
        else:
            self._synth_status_frame.configure(fg_color="transparent")
            self.synth_status.configure(
                text=text, text_color=self._CLR_TEXT_SUB)
        self.synth_status.pack(anchor="w", padx=10, pady=6)

    def _show_voice_status(self, text: str, error: bool = False,
                           success: bool = False):
        """ボイス登録フォームのステータスメッセージ表示"""
        if not text:
            self.voice_status.pack_forget()
            self._voice_status_frame.configure(fg_color="transparent")
            return
        if error:
            self._voice_status_frame.configure(fg_color=self._CLR_ERROR_BG)
            self.voice_status.configure(
                text=text, text_color=self._CLR_ERROR_FG)
        elif success:
            self._voice_status_frame.configure(fg_color=self._CLR_SUCCESS_BG)
            self.voice_status.configure(
                text=text, text_color=self._CLR_SUCCESS_FG)
        else:
            self._voice_status_frame.configure(fg_color="transparent")
            self.voice_status.configure(
                text=text, text_color=self._CLR_TEXT_SUB)
        self.voice_status.pack(anchor="w", padx=10, pady=6)

    def _batch_save_all(self):
        if not self._batch_results:
            return
        dir_path = filedialog.askdirectory(title="保存先フォルダを選択")
        if not dir_path:
            return
        for i, item in enumerate(self._batch_results):
            out = os.path.join(dir_path, f"batch_{i + 1:03d}.wav")
            with open(out, "wb") as f:
                f.write(item["wav_bytes"])
        self._show_synth_status(
            f"{len(self._batch_results)}件を保存しました", success=True)

    # --- 合成履歴 ---

    MAX_HISTORY = 20

    def _add_history(self, text: str, speaker_name: str, wav_bytes: bytes):
        import time as _time
        t = _time.strftime("%H:%M")
        temp_path = os.path.join(
            tempfile.gettempdir(),
            f"selfvox_hist_{len(self._synth_history)}.wav")
        with open(temp_path, "wb") as f:
            f.write(wav_bytes)

        entry = {"text": text, "speaker": speaker_name,
                 "wav_bytes": wav_bytes, "path": temp_path, "time": t}
        self._synth_history.insert(0, entry)

        if len(self._synth_history) > self.MAX_HISTORY:
            self._synth_history.pop()

        self._render_history()

    def _render_history(self):
        for w in self._history_frame.winfo_children():
            w.destroy()

        if not self._synth_history:
            self._history_empty_label = ctk.CTkLabel(
                self._history_frame, text="まだ合成履歴がありません",
                font=_ui_font(12), text_color=self._CLR_TEXT_HINT,
            )
            self._history_empty_label.pack(anchor="w")
            return

        for i, entry in enumerate(self._synth_history):
            row = ctk.CTkFrame(
                self._history_frame,
                fg_color=self._CLR_ITEM_BG, corner_radius=8)
            row.pack(fill="x", pady=(0, 4))

            row_inner = ctk.CTkFrame(row, fg_color="transparent")
            row_inner.pack(fill="x", padx=12, pady=8)

            # 時刻
            ctk.CTkLabel(
                row_inner, text=entry["time"], width=40,
                font=_ui_font(11), text_color=self._CLR_TEXT_HINT,
            ).pack(side="left", padx=(0, 8))

            # スピーカー名 + テキスト
            text_display = entry["text"]
            if len(text_display) > 35:
                text_display = text_display[:35] + "..."
            label_text = f"{entry['speaker']}: {text_display}"
            ctk.CTkLabel(
                row_inner, text=label_text,
                font=_ui_font(12), text_color=self._CLR_TEXT,
            ).pack(side="left", fill="x", expand=True)

            # 再生ボタン
            hist_play_btn = ctk.CTkButton(
                row_inner, text="\u25b6 再生", width=64, height=28,
                font=_ui_font(11), corner_radius=6,
            )
            hist_play_btn.configure(
                command=lambda p=entry["path"], b=hist_play_btn:
                    self._play_wav(p, toggle_btn=b))
            hist_play_btn.pack(side="right", padx=(4, 0))

            # 保存ボタン
            def _save_one(wb=entry["wav_bytes"], idx=i):
                path = filedialog.asksaveasfilename(
                    defaultextension=".wav",
                    filetypes=[("WAV File", "*.wav")],
                    initialfile=f"synth_{idx + 1}.wav",
                )
                if path:
                    with open(path, "wb") as f:
                        f.write(wb)

            ctk.CTkButton(
                row_inner, text="\u2b07 保存", width=64, height=28,
                font=_ui_font(11), corner_radius=6,
                fg_color=self._CLR_BLUE, hover_color=self._CLR_BLUE_HOVER,
                command=_save_one,
            ).pack(side="right", padx=(4, 0))

    def _clear_history(self):
        self._synth_history.clear()
        self._render_history()

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
            winsound.PlaySound(None, winsound.SND_PURGE)
            self._stop_playing()
            if was_same:
                return  # 同じボタン = 停止のみ
        try:
            winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC)
            if toggle_btn is not None:
                self._playing_btn = toggle_btn
                self._playing_btn_orig_text = toggle_btn.cget("text")
                toggle_btn.configure(text="\u25a0 停止")
                # WAV の再生時間を取得してタイマーでボタンを戻す
                try:
                    with wave.open(path, "rb") as wf:
                        duration_ms = int(
                            wf.getnframes() / wf.getframerate() * 1000) + 200
                    self._play_after_id = self.root.after(
                        duration_ms, self._on_play_finished, toggle_btn)
                except Exception:
                    pass
        except Exception as e:
            messagebox.showerror("再生エラー", str(e))

    def _on_play_finished(self, btn):
        """再生終了タイマーによるボタン復元"""
        if self._playing_btn is btn:
            self._stop_playing()

    def _stop_playing(self):
        # タイマーキャンセル
        after_id = getattr(self, "_play_after_id", None)
        if after_id is not None:
            self.root.after_cancel(after_id)
            self._play_after_id = None
        if self._playing_btn is not None:
            try:
                self._playing_btn.configure(
                    text=getattr(self, "_playing_btn_orig_text", "\u25b6 再生"))
            except Exception:
                pass
            self._playing_btn = None
            self._playing_btn_orig_text = None

    def _stop_all_audio(self):
        """再生中の音声をすべて停止する"""
        winsound.PlaySound(None, winsound.SND_PURGE)
        self._stop_playing()

    # ===================================================================
    # 設定
    # ===================================================================

    def _load_config(self) -> dict:
        config_path = self._app_dir() / "config.json"
        defaults = {"port": 50021, "auto_start": True}
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
        dlg.geometry("400x220")
        dlg.resizable(False, False)
        dlg.transient(self.root)
        dlg.grab_set()

        ctk.CTkLabel(dlg, text="ポート番号:", font=_ui_font(13)).place(
            x=20, y=24)
        port_var = ctk.StringVar(value=str(self.config.get("port", 50021)))
        port_entry = ctk.CTkEntry(
            dlg, textvariable=port_var, width=120, font=_ui_font(13))
        port_entry.place(x=120, y=20)

        auto_start_var = ctk.BooleanVar(
            value=self.config.get("auto_start", True))
        ctk.CTkCheckBox(
            dlg, text="起動時にサーバーを自動起動",
            variable=auto_start_var, font=_ui_font(13),
        ).place(x=20, y=72)

        ctk.CTkLabel(
            dlg, text="※ ポート番号の変更はサーバー再起動後に反映されます",
            font=_ui_font(11), text_color="#888888",
        ).place(x=20, y=112)

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
            self.config["auto_start"] = auto_start_var.get()
            self._save_config()
            dlg.destroy()

        ctk.CTkButton(
            dlg, text="保存", command=save_and_close,
            fg_color="#4CAF50", hover_color="#43a047",
            text_color="white",
            font=_ui_font(13, True),
            width=120, height=36,
        ).place(x=140, y=160)

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
