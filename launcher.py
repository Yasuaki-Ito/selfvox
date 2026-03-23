"""SelfVox ランチャー
初回起動時にuv + Python環境を自動構築し、GUIからサーバーを起動する。
PyInstallerでexe化して配布する。
"""

import hashlib
import os
import ssl
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

# SSL 証明書検証に失敗する環境向けのフォールバック
_ssl_checked = False
_ssl_ok = True


def _check_ssl(log_callback=None) -> None:
    """SSL 証明書の検証が通るかテストし、失敗時は検証を無効化する。"""
    global _ssl_checked, _ssl_ok
    if _ssl_checked:
        return
    _ssl_checked = True

    req = urllib.request.Request("https://github.com", method="HEAD")
    try:
        urllib.request.urlopen(req, timeout=10)
        return
    except urllib.error.URLError as e:
        if "CERTIFICATE_VERIFY_FAILED" not in str(e):
            _log(log_callback, f"ネットワーク接続テスト失敗 (続行します): {e}")
            return
    except Exception as e:
        _log(log_callback, f"ネットワーク接続テスト失敗 (続行します): {e}")
        return

    _ssl_ok = False
    _log(log_callback,
         "SSL 証明書の検証に失敗しました。SSL 検証を無効化して続行します。"
         "（企業プロキシやウイルス対策ソフトが原因の場合があります）")

    # uv: rustls 内蔵証明書を使用 + 全ホストで検証スキップ
    os.environ["UV_NATIVE_TLS"] = "false"
    os.environ["UV_INSECURE_HOST"] = (
        "github.com,objects.githubusercontent.com,"
        "pypi.org,files.pythonhosted.org,"
        "download.pytorch.org,"
        "huggingface.co,cdn-lfs.huggingface.co"
    )
    # pip / requests / huggingface_hub
    os.environ["PIP_TRUSTED_HOST"] = (
        "pypi.org files.pythonhosted.org download.pytorch.org"
    )
    os.environ["REQUESTS_CA_BUNDLE"] = ""
    os.environ["CURL_CA_BUNDLE"] = ""
    os.environ["HF_HUB_DISABLE_SSL_VERIFY"] = "1"
    # Python urllib
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    ssl._create_default_https_context = lambda: ctx

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
    try:
        urllib.request.urlretrieve(UV_DOWNLOAD_URL, zip_path)
    except Exception as e:
        raise RuntimeError(
            f"uv のダウンロードに失敗しました: {e}\n"
            f"URL: {UV_DOWNLOAD_URL}\n"
            "インターネット接続を確認してください。") from e

    try:
        with zipfile.ZipFile(zip_path, "r") as zf:
            for member in zf.namelist():
                if member.endswith("uv.exe"):
                    with zf.open(member) as src, open(UV_EXE, "wb") as dst:
                        dst.write(src.read())
                    break
    except Exception as e:
        raise RuntimeError(
            f"uv.zip の展開に失敗しました: {e}\n"
            "ダウンロードが不完全だった可能性があります。") from e

    if not UV_EXE.exists():
        raise RuntimeError(
            "uv.exe が ZIP 内に見つかりませんでした。\n"
            "ダウンロードされたファイルが破損している可能性があります。")

    zip_path.unlink(missing_ok=True)
    _log(log_callback, f"uv.exe を {UV_EXE} に配置しました")


def run_cmd_with_output(args: list[str], desc: str, log_callback=None) -> None:
    _log(log_callback, desc)

    creation_flags = 0
    if hasattr(subprocess, "CREATE_NO_WINDOW"):
        creation_flags = subprocess.CREATE_NO_WINDOW

    exe_path = Path(args[0])
    if not exe_path.exists():
        raise RuntimeError(
            f"実行ファイルが見つかりません: {exe_path}\n"
            "セットアップが完了していない可能性があります。")

    try:
        proc = subprocess.Popen(
            args,
            cwd=str(APP_DIR),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
            creationflags=creation_flags,
        )
    except OSError as e:
        raise RuntimeError(
            f"プロセスの起動に失敗しました: {e}\n"
            f"コマンド: {' '.join(str(a) for a in args[:3])}") from e
    for line in proc.stdout:
        line = line.rstrip()
        if "\r" in line:
            line = line.rsplit("\r", 1)[-1]
        if line:
            _log(log_callback, line)
    proc.wait()
    if proc.returncode != 0:
        cmd_str = " ".join(args[:3]) + (" ..." if len(args) > 3 else "")
        raise RuntimeError(
            f"コマンドが失敗しました (code={proc.returncode}): {cmd_str}")


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

    _check_ssl(log_callback)
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

    _check_ssl(log_callback)
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

    _check_ssl(log_callback)
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
