"""SelfVox ランチャー
初回起動時にuv + Python環境を自動構築し、GUIからサーバーを起動する。
PyInstallerでexe化して配布する。
"""

import hashlib
import os
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

# アプリのベースディレクトリ（exe化時は exe の場所）
if getattr(sys, "frozen", False):
    APP_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent

TOOLS_DIR = APP_DIR / "_tools"
UV_EXE = TOOLS_DIR / "uv.exe"
VENV_DIR = APP_DIR / ".venv"
PYTHON_EXE = VENV_DIR / "Scripts" / "python.exe"
HF_CACHE_DIR = APP_DIR / ".cache"

# HuggingFaceモデルのダウンロード先をアプリフォルダ内に固定
os.environ["HF_HOME"] = str(HF_CACHE_DIR)

UV_DOWNLOAD_URL = (
    "https://github.com/astral-sh/uv/releases/latest/download/uv-x86_64-pc-windows-msvc.zip"
)

MODEL_NAME = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"
TORCH_INDEX = "https://download.pytorch.org/whl/cu128"
PACKAGES_TORCH = ["torch", "torchaudio"]
PACKAGES_OTHER = [
    "qwen-tts",
    "soundfile",
    "sounddevice",
    "fastapi",
    "uvicorn[standard]",
    "numpy",
    "pydantic",
]


PKG_STAMP_FILE = VENV_DIR / ".pkg_stamp"


def _pkg_stamp() -> str:
    """パッケージ構成のハッシュ。変わったら再インストールが必要。"""
    blob = "\n".join([TORCH_INDEX] + PACKAGES_TORCH + PACKAGES_OTHER)
    return hashlib.sha256(blob.encode()).hexdigest()[:16]


def _log(callback, msg: str) -> None:
    if callback:
        callback(msg)
    else:
        print(f">>> {msg}")


def is_setup_needed() -> bool:
    return not PYTHON_EXE.exists()


def is_update_needed() -> bool:
    """venvは存在するがパッケージ構成が変わった場合 True。"""
    if not PYTHON_EXE.exists():
        return False
    if not PKG_STAMP_FILE.exists():
        return True
    return PKG_STAMP_FILE.read_text().strip() != _pkg_stamp()


def download_uv(log_callback=None) -> None:
    if UV_EXE.exists():
        return

    _log(log_callback, "uv をダウンロード中...")
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    zip_path = TOOLS_DIR / "uv.zip"
    urllib.request.urlretrieve(UV_DOWNLOAD_URL, zip_path)

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if member.endswith("uv.exe"):
                with zf.open(member) as src, open(UV_EXE, "wb") as dst:
                    dst.write(src.read())
                break

    zip_path.unlink(missing_ok=True)
    _log(log_callback, f"uv.exe を {UV_EXE} に配置しました")


def run_cmd_with_output(args: list[str], desc: str, log_callback=None) -> None:
    _log(log_callback, desc)

    creation_flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation_flags = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        args,
        cwd=str(APP_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        creationflags=creation_flags,
    )
    for line in proc.stdout:
        line = line.rstrip()
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        if line:
            _log(log_callback, line)
    proc.wait()
    if proc.returncode != 0:
        raise RuntimeError(f"コマンドが失敗しました (code={proc.returncode})")


def _write_pkg_stamp() -> None:
    PKG_STAMP_FILE.write_text(_pkg_stamp())


def _install_packages(log_callback=None) -> None:
    """PyTorch + その他パッケージをインストール。"""
    download_uv(log_callback)

    run_cmd_with_output(
        [str(UV_EXE), "pip", "install", "--python", str(PYTHON_EXE)]
        + PACKAGES_TORCH
        + ["--index-url", TORCH_INDEX],
        "PyTorch + CUDA をインストール中（数分かかります）...",
        log_callback,
    )

    run_cmd_with_output(
        [str(UV_EXE), "pip", "install", "--python", str(PYTHON_EXE)]
        + PACKAGES_OTHER,
        "依存パッケージをインストール中...",
        log_callback,
    )

    _write_pkg_stamp()


def is_model_downloaded() -> bool:
    """モデルがキャッシュ済みかどうか"""
    model_cache = HF_CACHE_DIR / "hub" / ("models--" + MODEL_NAME.replace("/", "--"))
    return model_cache.exists()


def download_model(log_callback=None) -> None:
    """HuggingFace モデルを事前ダウンロード"""
    if is_model_downloaded():
        return

    run_cmd_with_output(
        [str(PYTHON_EXE), "-c",
         f"from huggingface_hub import snapshot_download; "
         f"snapshot_download('{MODEL_NAME}')"],
        f"音声合成モデルをダウンロード中 (~4.5GB): {MODEL_NAME}",
        log_callback,
    )


def setup_environment(log_callback=None) -> None:
    if not is_setup_needed():
        return

    _log(log_callback, "=== 初回セットアップ開始 ===")

    download_uv(log_callback)

    run_cmd_with_output(
        [str(UV_EXE), "venv", "-p", "3.12", str(VENV_DIR)],
        "Python 3.12 仮想環境を作成中...",
        log_callback,
    )

    _install_packages(log_callback)

    _log(log_callback, "=== セットアップ完了 ===")


def update_packages(log_callback=None) -> None:
    """パッケージ構成が変わった場合に再インストール。"""
    if not is_update_needed():
        return

    _log(log_callback, "=== パッケージ更新を検出 ===")
    _install_packages(log_callback)
    _log(log_callback, "=== パッケージ更新完了 ===")


def start_server_process(port: int = 50021) -> subprocess.Popen:
    run_py = APP_DIR / "run.py"

    env = os.environ.copy()
    env["PYTHONUNBUFFERED"] = "1"

    creation_flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation_flags = subprocess.CREATE_NO_WINDOW

    proc = subprocess.Popen(
        [str(PYTHON_EXE), str(run_py), "--port", str(port)],
        cwd=str(APP_DIR),
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
        env=env,
        creationflags=creation_flags,
    )
    return proc


def main() -> None:
    from gui import SelfVoxGUI
    app = SelfVoxGUI()
    app.run()


if __name__ == "__main__":
    main()
