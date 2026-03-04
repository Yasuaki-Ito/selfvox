"""SelfVox ランチャー
初回起動時にuv + Python環境を自動構築し、サーバーを起動する。
PyInstallerでexe化して配布する。
"""

import os
import signal
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

# アプリのベースディレクトリ（exe化時は exe の場所、開発時はスクリプトの場所）
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

TORCH_INDEX = "https://download.pytorch.org/whl/cu124"
PACKAGES_TORCH = ["torch", "torchaudio"]
PACKAGES_OTHER = [
    "qwen-tts",
    "soundfile",
    "fastapi",
    "uvicorn[standard]",
    "numpy",
    "pydantic",
]


def print_step(msg: str) -> None:
    print(f"\n>>> {msg}")


def download_uv() -> None:
    """uv.exe をダウンロードして _tools/ に配置"""
    if UV_EXE.exists():
        return

    print_step("uv をダウンロード中...")
    TOOLS_DIR.mkdir(parents=True, exist_ok=True)

    zip_path = TOOLS_DIR / "uv.zip"
    urllib.request.urlretrieve(UV_DOWNLOAD_URL, zip_path)

    with zipfile.ZipFile(zip_path, "r") as zf:
        for member in zf.namelist():
            if member.endswith("uv.exe"):
                # フラットに _tools/uv.exe として展開
                with zf.open(member) as src, open(UV_EXE, "wb") as dst:
                    dst.write(src.read())
                break

    zip_path.unlink(missing_ok=True)
    print(f"    uv.exe を {UV_EXE} に配置しました")


def run_cmd(args: list[str], desc: str) -> None:
    """コマンドを実行して結果を表示"""
    print_step(desc)
    print(f"    実行: {' '.join(args)}")
    result = subprocess.run(args, cwd=str(APP_DIR))
    if result.returncode != 0:
        print(f"    エラー: コマンドが失敗しました (code={result.returncode})")
        sys.exit(1)


def setup_environment() -> None:
    """venv が無ければ環境を構築"""
    if PYTHON_EXE.exists():
        return

    print_step("=== 初回セットアップ開始 ===")

    download_uv()

    # venv 作成
    run_cmd(
        [str(UV_EXE), "venv", "-p", "3.12", str(VENV_DIR)],
        "Python 3.12 仮想環境を作成中...",
    )

    # PyTorch + CUDA インストール
    run_cmd(
        [str(UV_EXE), "pip", "install", "--python", str(PYTHON_EXE)]
        + PACKAGES_TORCH
        + ["--index-url", TORCH_INDEX],
        "PyTorch + CUDA をインストール中（数分かかります）...",
    )

    # その他パッケージ
    run_cmd(
        [str(UV_EXE), "pip", "install", "--python", str(PYTHON_EXE)]
        + PACKAGES_OTHER,
        "依存パッケージをインストール中...",
    )

    print_step("=== セットアップ完了 ===")


def start_server() -> None:
    """サーバーをサブプロセスで起動"""
    print_step("SelfVox サーバーを起動中...")
    print(f"    http://localhost:50021 でアクセスできます")
    print(f"    停止するには Ctrl+C を押してください\n")

    run_py = APP_DIR / "run.py"
    proc = subprocess.Popen(
        [str(PYTHON_EXE), str(run_py)],
        cwd=str(APP_DIR),
    )

    def handle_sigint(sig, frame):
        proc.terminate()
        proc.wait()
        sys.exit(0)

    signal.signal(signal.SIGINT, handle_sigint)

    try:
        proc.wait()
    except KeyboardInterrupt:
        proc.terminate()
        proc.wait()


def main() -> None:
    print("=" * 50)
    print("  SelfVox - Voice Clone TTS Server")
    print("=" * 50)

    setup_environment()
    start_server()


if __name__ == "__main__":
    main()
