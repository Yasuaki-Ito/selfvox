"""SelfVox - Voice Clone TTS Server (VOICEVOX互換API)"""

from __future__ import annotations

import base64
import io
import json
import logging
import shutil
import zipfile
from pathlib import Path

import numpy as np
import soundfile as sf
import uvicorn
from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, Response

from models import (
    AccentPhrase,
    AudioQuery,
    Mora,
    Speaker,
    SpeakerStyle,
    SpeakerSupportedFeatures,
    SupportedDevices,
)
from tts_engine import TTSEngine
from version import VERSION

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(
    title="SelfVox",
    description="Voice Clone TTS Server - VOICEVOX互換API",
    version=VERSION,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

engine = TTSEngine()


@app.on_event("startup")
async def startup():
    engine.load()
    logger.info("Server ready: http://localhost:50021")


# --- VOICEVOX互換エンドポイント ---


@app.post("/audio_query", response_model=AudioQuery)
async def audio_query(
    text: str = Query(..., description="合成するテキスト"),
    speaker: int = Query(..., description="スタイルID"),
    core_version: str | None = Query(None),
):
    """テキストからAudioQueryを生成"""
    # Qwen3-TTSはEnd-to-Endモデルなので、accent_phrasesは簡略化
    # 実際の合成はテキストをそのまま使う
    moras = []
    for char in text:
        moras.append(
            Mora(
                text=char,
                consonant=None,
                consonant_length=None,
                vowel="a",
                vowel_length=0.1,
                pitch=5.0,
            )
        )

    accent_phrases = []
    if moras:
        accent_phrases.append(
            AccentPhrase(
                moras=moras,
                accent=1,
                pause_mora=None,
                is_interrogative=text.endswith("？") or text.endswith("?"),
            )
        )

    query = AudioQuery(
        accent_phrases=accent_phrases,
        speedScale=1.0,
        pitchScale=0.0,
        intonationScale=1.0,
        volumeScale=1.0,
        prePhonemeLength=0.1,
        postPhonemeLength=0.8,
        outputSamplingRate=24000,
        outputStereo=False,
        kana=text,
    )
    # 内部テキストをセット（JSONシリアライズで_textフィールドとして保持）
    query._text = text
    return query


def _extract_text(aq: AudioQuery) -> str:
    """AudioQueryから合成用テキストを抽出"""
    text = aq._text if aq._text else aq.kana
    if not text:
        text = "".join(
            mora.text
            for phrase in aq.accent_phrases
            for mora in phrase.moras
        )
    return text


def _synthesize_wav(aq: AudioQuery, speaker_id: int) -> bytes:
    """AudioQueryからWAVバイナリを生成"""
    text = _extract_text(aq)
    if not text:
        raise HTTPException(status_code=400, detail="合成テキストが空です")

    try:
        wav, sr = engine.synthesize(
            text=text,
            speaker_id=speaker_id,
            speed=aq.speedScale,
            volume=aq.volumeScale,
            output_sr=aq.outputSamplingRate,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("音声合成エラー")
        raise HTTPException(status_code=500, detail=f"合成エラー: {e}")

    # 前後の無音を追加
    pre_silence = np.zeros(int(sr * aq.prePhonemeLength))
    post_silence = np.zeros(int(sr * aq.postPhonemeLength))
    wav = np.concatenate([pre_silence, wav, post_silence])

    # ステレオ変換
    if aq.outputStereo:
        wav = np.stack([wav, wav], axis=-1)

    buf = io.BytesIO()
    sf.write(buf, wav, sr, format="WAV", subtype="PCM_16")
    return buf.getvalue()


@app.post("/synthesis")
async def synthesis(
    speaker: int = Query(..., description="スタイルID"),
    enable_interrogative_upspeak: bool = Query(True),
    core_version: str | None = Query(None),
    audio_query: AudioQuery = ...,
):
    """AudioQueryから音声を合成してWAVを返す"""
    wav_bytes = _synthesize_wav(audio_query, speaker)
    return Response(content=wav_bytes, media_type="audio/wav")


@app.post("/multi_synthesis")
async def multi_synthesis(
    speaker: int = Query(..., description="スタイルID"),
    core_version: str | None = Query(None),
    audio_queries: list[AudioQuery] = ...,
):
    """複数AudioQueryから音声を合成してZIPで返す"""
    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for i, aq in enumerate(audio_queries):
            wav_bytes = _synthesize_wav(aq, speaker)
            zf.writestr(f"{i:03d}.wav", wav_bytes)
    zip_buf.seek(0)
    return Response(content=zip_buf.read(), media_type="application/zip")


@app.post("/cancellable_synthesis")
async def cancellable_synthesis(
    speaker: int = Query(..., description="スタイルID"),
    core_version: str | None = Query(None),
    audio_query: AudioQuery = ...,
):
    """キャンセル可能な合成（synthesisと同じ動作）"""
    wav_bytes = _synthesize_wav(audio_query, speaker)
    return Response(content=wav_bytes, media_type="audio/wav")


@app.get("/speakers", response_model=list[Speaker])
async def speakers(core_version: str | None = Query(None)):
    """利用可能なスピーカー一覧"""
    result = []
    for profile in engine.voices.values():
        result.append(
            Speaker(
                name=profile.name,
                speaker_uuid=profile.speaker_uuid,
                styles=[
                    SpeakerStyle(
                        name=s.get("name", "ノーマル"),
                        id=s.get("id", profile.speaker_id),
                        type=s.get("type", "talk"),
                    )
                    for s in profile.styles
                ],
                version=VERSION,
                supported_features=SpeakerSupportedFeatures(),
            )
        )
    return result


@app.post("/initialize_speaker")
async def initialize_speaker(
    speaker: int = Query(...),
    skip_reinit: bool = Query(False),
    core_version: str | None = Query(None),
):
    """スピーカー初期化（互換性のため実装、実質no-op）"""
    return {}


@app.get("/is_initialized_speaker")
async def is_initialized_speaker(
    speaker: int = Query(...),
    core_version: str | None = Query(None),
):
    """スピーカー初期化状態確認"""
    return True


@app.get("/version")
async def version():
    """エンジンバージョン"""
    return VERSION


@app.get("/supported_devices")
async def supported_devices():
    """対応デバイス一覧"""
    return SupportedDevices(cpu=False, cuda=True, dml=False)


@app.get("/engine_manifest")
async def engine_manifest():
    """エンジンマニフェスト"""
    return {
        "manifest_version": "0.13.1",
        "name": "SelfVox",
        "brand_name": "SelfVox",
        "uuid": "a1b2c3d4-e5f6-7890-abcd-ef1234567890",
        "url": "https://github.com/QwenLM/Qwen3-TTS",
        "default_sampling_rate": 24000,
        "frame_rate": 93.75,
        "icon": "",
        "terms_of_service": "",
        "update_infos": [],
        "supported_features": {
            "adjust_mora_pitch": False,
            "adjust_phoneme_length": False,
            "adjust_speed_scale": True,
            "adjust_pitch_scale": False,
            "adjust_intonation_scale": False,
            "adjust_volume_scale": True,
            "manage_library": False,
        },
    }


@app.get("/speaker_info")
async def speaker_info(
    speaker_uuid: str = Query(...),
    resource_format: str = Query("base64"),
    core_version: str | None = Query(None),
):
    """スピーカー詳細情報"""
    for profile in engine.voices.values():
        if profile.speaker_uuid == speaker_uuid:
            return {
                "policy": "",
                "portrait": "",
                "style_infos": [
                    {
                        "id": s.get("id", profile.speaker_id),
                        "icon": "",
                        "portrait": "",
                        "voice_samples": [],
                    }
                    for s in profile.styles
                ],
            }
    raise HTTPException(status_code=404, detail="スピーカーが見つかりません")


@app.get("/user_dict")
async def get_user_dict():
    """ユーザー辞書取得（空）"""
    return {}


@app.post("/user_dict_word")
async def add_user_dict_word():
    """ユーザー辞書追加（互換スタブ）"""
    return "00000000-0000-0000-0000-000000000000"


@app.get("/presets")
async def get_presets():
    """プリセット一覧（空）"""
    return []


@app.post("/mora_data")
async def mora_data(
    speaker: int = Query(...),
    core_version: str | None = Query(None),
    accent_phrases: list[AccentPhrase] = ...,
):
    """モーラデータ更新（そのまま返す）"""
    return accent_phrases


@app.post("/mora_length")
async def mora_length(
    speaker: int = Query(...),
    core_version: str | None = Query(None),
    accent_phrases: list[AccentPhrase] = ...,
):
    """モーラ長更新（そのまま返す）"""
    return accent_phrases


@app.post("/mora_pitch")
async def mora_pitch(
    speaker: int = Query(...),
    core_version: str | None = Query(None),
    accent_phrases: list[AccentPhrase] = ...,
):
    """モーラピッチ更新（そのまま返す）"""
    return accent_phrases


@app.post("/accent_phrases")
async def accent_phrases(
    text: str = Query(...),
    speaker: int = Query(...),
    is_kana: bool = Query(False),
    core_version: str | None = Query(None),
):
    """アクセント句生成（簡略版）"""
    moras = []
    for char in text:
        moras.append(
            Mora(
                text=char,
                consonant=None,
                consonant_length=None,
                vowel="a",
                vowel_length=0.1,
                pitch=5.0,
            )
        )
    result = []
    if moras:
        result.append(
            AccentPhrase(
                moras=moras,
                accent=1,
                is_interrogative=text.endswith("？") or text.endswith("?"),
            )
        )
    return result


# --- Voice Management Web UI ---

def _app_dir() -> Path:
    d = Path(__file__).resolve().parent
    if d.name == "__pycache__":
        d = d.parent
    return d

VOICES_DIR = _app_dir() / "voices"


@app.get("/", response_class=HTMLResponse)
async def web_ui(request: Request):
    """Voice management UI"""
    host = request.headers.get("host", "localhost:50021")
    api_url = f"http://{host}"
    # favicon を Base64 埋め込み
    icon_path = _app_dir() / "selfvox.png"
    if icon_path.exists():
        icon_b64 = base64.b64encode(icon_path.read_bytes()).decode()
        favicon_tag = f'<link rel="icon" type="image/png" href="data:image/png;base64,{icon_b64}">'
    else:
        favicon_tag = ""
    return f"""<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
{favicon_tag}
<title>SelfVox</title>
<style>
  * {{ box-sizing: border-box; margin: 0; padding: 0; }}
  body {{ font-family: 'Segoe UI', sans-serif; background: #f0f2f5; color: #333; }}
  .container {{ max-width: 800px; margin: 0 auto; padding: 20px; }}

  /* Header */
  .header {{ background: linear-gradient(135deg, #1a237e, #4a148c); color: #fff;
             padding: 28px 24px; border-radius: 12px; margin-bottom: 20px; }}
  .header-top {{ display: flex; align-items: center; gap: 14px; margin-bottom: 8px; }}
  .header-right {{ margin-left: auto; }}
  .github-link {{ display: inline-flex; align-items: center; gap: 6px; color: #fff; text-decoration: none;
                  background: rgba(255,255,255,.15); padding: 6px 12px; border-radius: 6px;
                  font-size: 13px; transition: background .15s; }}
  .github-link:hover {{ background: rgba(255,255,255,.3); }}
  .github-link svg {{ fill: #fff; }}
  .header-icon {{ width: 52px; height: 52px; border-radius: 10px; }}
  .header h1 {{ font-size: 24px; }}
  .header p {{ font-size: 14px; opacity: .85; line-height: 1.6; }}
  .badge {{ display: inline-block; background: rgba(255,255,255,.2); border-radius: 4px;
            padding: 2px 8px; font-size: 12px; margin-right: 6px; }}

  /* API Info */
  .api-info {{ background: #e8eaf6; border-left: 4px solid #3f51b5; border-radius: 0 8px 8px 0;
               padding: 14px 18px; margin-bottom: 20px; font-size: 13px; line-height: 1.7; }}
  .api-info code {{ background: #fff; padding: 2px 6px; border-radius: 3px; font-size: 13px; }}

  /* Cards */
  .card {{ background: #fff; border-radius: 10px; padding: 22px; margin-bottom: 16px;
           box-shadow: 0 1px 4px rgba(0,0,0,.08); }}
  .card h2 {{ font-size: 17px; margin-bottom: 4px; }}
  .card .desc {{ font-size: 13px; color: #777; margin-bottom: 14px; }}
  label {{ display: block; font-weight: 600; margin: 12px 0 4px; font-size: 14px; }}
  input[type=text], textarea, select {{
    width: 100%; padding: 9px 10px; border: 1px solid #d0d0d0;
    border-radius: 6px; font-size: 14px; font-family: inherit; }}
  input[type=text]:focus, textarea:focus, select:focus {{
    outline: none; border-color: #5c6bc0; box-shadow: 0 0 0 2px rgba(92,107,192,.15); }}
  textarea {{ resize: vertical; min-height: 80px; }}
  input[type=file] {{ font-size: 14px; }}

  /* Buttons */
  button {{ padding: 9px 18px; border: none; border-radius: 6px; cursor: pointer;
            font-size: 14px; font-weight: 600; transition: background .15s; }}
  .btn-primary {{ background: #4CAF50; color: #fff; }}
  .btn-primary:hover {{ background: #43a047; }}
  .btn-synth {{ background: #ff9800; color: #fff; }}
  .btn-synth:hover {{ background: #f57c00; }}
  .btn-synth:disabled {{ background: #ccc; cursor: not-allowed; }}
  .btn-dl {{ background: #2196F3; color: #fff; }}
  .btn-dl:hover {{ background: #1976D2; }}
  .btn-delete {{ background: #ef5350; color: #fff; font-size: 12px; padding: 5px 12px; }}
  .btn-delete:hover {{ background: #e53935; }}

  /* Status */
  .status {{ margin-top: 10px; padding: 8px 12px; border-radius: 6px; display: none; font-size: 13px; }}
  .status.ok {{ display: block; background: #e8f5e9; color: #2e7d32; }}
  .status.err {{ display: block; background: #ffebee; color: #c62828; }}
  .synth-status {{ margin-top: 8px; font-size: 13px; color: #666; }}

  /* Voice list */
  .voice-list {{ display: grid; gap: 10px; }}
  .voice-item {{ display: flex; justify-content: space-between; align-items: flex-start;
                 background: #fafafa; border-radius: 8px; padding: 14px; }}
  .voice-info {{ flex: 1; }}
  .voice-info strong {{ font-size: 15px; }}
  .voice-info .meta {{ font-size: 12px; color: #888; margin-top: 2px; }}
  .voice-info audio {{ width: 100%; margin-top: 8px; }}

  /* Range slider */
  .slider-row {{ display: flex; gap: 24px; margin-top: 10px; }}
  .slider-row > div {{ flex: 1; }}
  input[type=range] {{ width: 100%; accent-color: #ff9800; }}
  .slider-label {{ font-size: 13px; color: #555; }}

  /* Drop zone */
  .drop-zone {{ border: 2px dashed #bbb; border-radius: 8px; padding: 20px; text-align: center;
                color: #888; cursor: pointer; transition: border-color .2s, background .2s; }}
  .drop-zone.dragover {{ border-color: #5c6bc0; background: #e8eaf6; color: #333; }}
  .drop-zone input[type=file] {{ display: none; }}
  .drop-zone .drop-text {{ font-size: 14px; }}

  /* Spinner */
  .synth-progress {{ display: flex; align-items: center; gap: 10px; margin-top: 8px; font-size: 13px; color: #666; }}
  .spinner {{ width: 18px; height: 18px; border: 2.5px solid #ddd; border-top-color: #ff9800;
              border-radius: 50%; animation: spin .7s linear infinite; }}
  @keyframes spin {{ to {{ transform: rotate(360deg); }} }}

  /* History */
  .history-list {{ display: grid; gap: 8px; margin-top: 10px; }}
  .history-item {{ display: flex; align-items: center; gap: 10px; background: #fafafa;
                   border-radius: 8px; padding: 10px 14px; font-size: 13px; }}
  .history-item .hist-text {{ flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .history-item .hist-time {{ color: #999; font-size: 11px; white-space: nowrap; }}
  .history-item audio {{ height: 30px; }}
  .history-item a {{ text-decoration: none; }}
  .history-empty {{ color: #999; font-size: 13px; font-style: italic; }}

  /* Batch */
  .batch-progress {{ margin-top: 10px; }}
  .batch-bar {{ width: 100%; height: 6px; background: #eee; border-radius: 3px; overflow: hidden; }}
  .batch-bar-fill {{ height: 100%; background: #ff9800; transition: width .3s; }}
  .batch-results {{ display: grid; gap: 6px; margin-top: 10px; }}
  .batch-item {{ display: flex; align-items: center; gap: 8px; background: #fafafa;
                 border-radius: 6px; padding: 8px 12px; font-size: 13px; }}
  .batch-item .batch-text {{ flex: 1; overflow: hidden; text-overflow: ellipsis; white-space: nowrap; }}
  .batch-item audio {{ height: 28px; }}

  /* Footer */
  .footer {{ text-align: center; font-size: 12px; color: #aaa; margin-top: 24px; padding: 12px; }}
</style>
</head>
<body>
<div class="container">

<div class="header">
  <div class="header-top">
    <img class="header-icon" src="data:image/png;base64,{icon_b64}" alt="SelfVox">
    <h1>SelfVox <span style="font-size:13px;font-weight:400;opacity:.7">v{VERSION}</span></h1>
    <div class="header-right">
      <a class="github-link" href="https://github.com/Yasuaki-Ito/selfvox" target="_blank">
        <svg width="18" height="18" viewBox="0 0 16 16"><path d="M8 0C3.58 0 0 3.58 0 8c0 3.54 2.29 6.53 5.47 7.59.4.07.55-.17.55-.38 0-.19-.01-.82-.01-1.49-2.01.37-2.53-.49-2.69-.94-.09-.23-.48-.94-.82-1.13-.28-.15-.68-.52-.01-.53.63-.01 1.08.58 1.23.82.72 1.21 1.87.87 2.33.66.07-.52.28-.87.51-1.07-1.78-.2-3.64-.89-3.64-3.95 0-.87.31-1.59.82-2.15-.08-.2-.36-1.02.08-2.12 0 0 .67-.21 2.2.82.64-.18 1.32-.27 2-.27s1.36.09 2 .27c1.53-1.04 2.2-.82 2.2-.82.44 1.1.16 1.92.08 2.12.51.56.82 1.27.82 2.15 0 3.07-1.87 3.75-3.65 3.95.29.25.54.73.54 1.48 0 1.07-.01 1.93-.01 2.2 0 .21.15.46.55.38A8.01 8.01 0 0016 8c0-4.42-3.58-8-8-8z"/></svg>
        GitHub
      </a>
    </div>
  </div>
  <p>
    <span class="badge">VOICEVOX Compatible</span>
    <span class="badge">Voice Clone</span>
    <span class="badge">Qwen3-TTS 1.7B</span>
    <br>
    あなたの声をAIで再現する音声クローン合成サーバーです。<br>
    VOICEVOX互換APIを提供しており、VOICEVOX対応アプリからそのまま利用できます。
  </p>
</div>

<div class="api-info">
  <strong>API Endpoint:</strong> <code>{api_url}</code><br>
  VOICEVOX対応アプリの接続先URLにこのアドレスを設定してください。<br>
  <strong>Speakers:</strong> <code>GET {api_url}/speakers</code> &nbsp;
  <strong>Synthesis:</strong> <code>POST {api_url}/audio_query</code> &rarr; <code>POST {api_url}/synthesis</code>
</div>

<!-- Registered Voices -->
<div class="card">
  <h2>登録済みボイス</h2>
  <p class="desc">登録済みのボイスプロファイル一覧です。VOICEVOX対応アプリからスピーカーとして選択できます。</p>
  <div id="voiceList" class="voice-list"><em>読み込み中...</em></div>
</div>

<!-- Voice Registration -->
<div class="card">
  <h2>ボイス登録</h2>
  <p class="desc">クローンしたい声の短いサンプル音声（5〜15秒程度のWAV）と、その音声の書き起こしテキストを登録します。</p>
  <form id="voiceForm">
    <label>ボイス名</label>
    <input type="text" id="name" value="" placeholder="(例) ナレーター1号">

    <label>リファレンス音声</label>
    <div id="dropZone" class="drop-zone">
      <input type="file" id="audio" accept=".wav,.mp3,.ogg,.flac">
      <div class="drop-text" id="dropText">ここにファイルをドラッグ&ドロップ<br>またはクリックして選択</div>
    </div>
    <audio id="preview" controls style="display:none; margin-top:6px; width:100%"></audio>

    <label>リファレンステキスト</label>
    <textarea id="refText" placeholder="リファレンス音声の書き起こしテキストを正確に入力してください"></textarea>

    <label>言語</label>
    <select id="lang">
      <option value="Japanese" selected>Japanese</option>
      <option value="Chinese">Chinese</option>
      <option value="English">English</option>
      <option value="Korean">Korean</option>
      <option value="French">French</option>
      <option value="Spanish">Spanish</option>
      <option value="German">German</option>
      <option value="Italian">Italian</option>
      <option value="Portuguese">Portuguese</option>
      <option value="Russian">Russian</option>
    </select>

    <div style="margin-top: 16px;">
      <button type="submit" id="saveBtn" class="btn-primary">ボイスを登録</button>
    </div>
  </form>
  <div id="status" class="status"></div>
</div>

<!-- Speech Synthesis -->
<div class="card">
  <h2>音声合成</h2>
  <p class="desc">登録済みボイスでテキスト音声合成をテストできます。</p>

  <label>スピーカー</label>
  <select id="synthSpeaker"><option value="">-- 読み込み中 --</option></select>

  <label>テキスト</label>
  <textarea id="synthText">こんにちは。音声合成のテストです。</textarea>

  <div class="slider-row">
    <div>
      <label class="slider-label">速度: <span id="speedVal">1.0</span>x</label>
      <input type="range" id="synthSpeed" min="0.5" max="2.0" step="0.1" value="1.0"
        oninput="document.getElementById('speedVal').textContent=this.value">
    </div>
    <div>
      <label class="slider-label">音量: <span id="volVal">1.0</span>x</label>
      <input type="range" id="synthVolume" min="0.1" max="2.0" step="0.1" value="1.0"
        oninput="document.getElementById('volVal').textContent=this.value">
    </div>
  </div>

  <div style="margin-top: 16px; display:flex; gap:8px; align-items:center;">
    <button id="synthBtn" class="btn-synth" onclick="synthesize()">合成</button>
    <a id="synthDownload" download="synthesis.wav" style="display:none;">
      <button type="button" class="btn-dl">WAVダウンロード</button>
    </a>
  </div>
  <div id="synthStatus" class="synth-progress" style="display:none;">
    <div class="spinner"></div>
    <span id="synthStatusText">生成中...</span>
    <span id="synthElapsed">0.0秒</span>
  </div>
  <div id="synthError" style="margin-top:8px; font-size:13px; color:#c62828;"></div>
  <audio id="synthAudio" controls style="display:none; margin-top:10px; width:100%"></audio>
</div>

<!-- Batch Synthesis -->
<div class="card">
  <h2>一括合成</h2>
  <p class="desc">複数行のテキストを1行ずつ順番に合成します。</p>

  <label>スピーカー</label>
  <select id="batchSpeaker"><option value="">-- 読み込み中 --</option></select>

  <label>テキスト（1行 = 1音声）</label>
  <textarea id="batchText" rows="6" placeholder="こんにちは。&#10;今日はいい天気ですね。&#10;よろしくお願いします。"></textarea>

  <div class="slider-row">
    <div>
      <label class="slider-label">速度: <span id="batchSpeedVal">1.0</span>x</label>
      <input type="range" id="batchSpeed" min="0.5" max="2.0" step="0.1" value="1.0"
        oninput="document.getElementById('batchSpeedVal').textContent=this.value">
    </div>
    <div>
      <label class="slider-label">音量: <span id="batchVolVal">1.0</span>x</label>
      <input type="range" id="batchVolume" min="0.1" max="2.0" step="0.1" value="1.0"
        oninput="document.getElementById('batchVolVal').textContent=this.value">
    </div>
  </div>

  <div style="margin-top: 16px;">
    <button id="batchBtn" class="btn-synth" onclick="batchSynthesize()">一括合成</button>
  </div>
  <div class="batch-progress" id="batchProgress" style="display:none;">
    <div style="display:flex; align-items:center; gap:10px; margin-bottom:6px;">
      <div class="spinner"></div>
      <span id="batchStatusText">合成中... 0/0</span>
    </div>
    <div class="batch-bar"><div class="batch-bar-fill" id="batchBarFill" style="width:0%"></div></div>
  </div>
  <div class="batch-results" id="batchResults"></div>
</div>

<!-- Synthesis History -->
<div class="card">
  <h2>合成履歴</h2>
  <p class="desc">最近の合成結果を再生・ダウンロードできます（最大20件、ページを閉じるとクリアされます）。</p>
  <div id="historyList"><em class="history-empty">まだ合成履歴がありません</em></div>
  <div style="margin-top:10px;">
    <button class="btn-delete" id="clearHistoryBtn" onclick="clearHistory()" style="display:none;">履歴をクリア</button>
  </div>
</div>

<div class="footer">
  SelfVox - Voice Clone TTS Server (VOICEVOX Compatible)<br>
  <a href="https://github.com/Yasuaki-Ito/selfvox" target="_blank" style="color:#888;">github.com/Yasuaki-Ito/selfvox</a>
</div>

</div><!-- /container -->

<script>
const audioInput = document.getElementById('audio');
const preview = document.getElementById('preview');
const dropZone = document.getElementById('dropZone');
const dropText = document.getElementById('dropText');

// --- Drag & Drop ---
function setFile(file) {{
  const dt = new DataTransfer();
  dt.items.add(file);
  audioInput.files = dt.files;
  preview.src = URL.createObjectURL(file);
  preview.style.display = 'block';
  dropText.textContent = file.name;
}}

dropZone.addEventListener('click', () => audioInput.click());
dropZone.addEventListener('dragover', (e) => {{ e.preventDefault(); dropZone.classList.add('dragover'); }});
dropZone.addEventListener('dragleave', () => dropZone.classList.remove('dragover'));
dropZone.addEventListener('drop', (e) => {{
  e.preventDefault();
  dropZone.classList.remove('dragover');
  const f = e.dataTransfer.files[0];
  if (f && /\.(wav|mp3|ogg|flac)$/i.test(f.name)) setFile(f);
}});
audioInput.addEventListener('change', () => {{
  const f = audioInput.files[0];
  if (f) setFile(f);
}});

// --- Voice Registration ---
document.getElementById('voiceForm').addEventListener('submit', async (e) => {{
  e.preventDefault();
  const st = document.getElementById('status');
  const btn = document.getElementById('saveBtn');
  const nameVal = document.getElementById('name').value.trim();
  const refTextVal = document.getElementById('refText').value.trim();
  const file = audioInput.files[0];

  if (!nameVal) {{ st.className = 'status err'; st.textContent = 'ボイス名を入力してください'; return; }}
  if (!file) {{ st.className = 'status err'; st.textContent = 'リファレンス音声を選択してください'; return; }}
  if (!refTextVal) {{ st.className = 'status err'; st.textContent = 'リファレンステキストを入力してください'; return; }}

  btn.disabled = true;
  btn.textContent = '保存中...';
  st.className = 'status';
  st.style.display = 'none';

  const fd = new FormData();
  fd.append('name', nameVal);
  fd.append('ref_text', refTextVal);
  fd.append('language', document.getElementById('lang').value);
  fd.append('audio', file);

  try {{
    const res = await fetch('/manage/voice', {{ method: 'POST', body: fd }});
    const data = await res.json();
    if (res.ok) {{
      st.className = 'status ok';
      st.textContent = data.message;
      document.getElementById('voiceForm').reset();
      preview.style.display = 'none';
      dropText.innerHTML = 'ここにファイルをドラッグ&ドロップ<br>またはクリックして選択';
      loadVoices();
    }} else {{
      st.className = 'status err';
      st.textContent = typeof data.detail === 'string' ? data.detail : JSON.stringify(data.detail) || 'エラーが発生しました';
    }}
  }} catch (err) {{
    st.className = 'status err';
    st.textContent = 'エラー: ' + err.message;
  }} finally {{
    btn.disabled = false;
    btn.textContent = 'ボイスを登録';
  }}
}});

// --- Voice List ---
async function loadVoices() {{
  const el = document.getElementById('voiceList');
  try {{
    const res = await fetch('/manage/voices');
    const voices = await res.json();
    updateSpeakerSelect(voices);
    if (voices.length === 0) {{
      el.innerHTML = '<em style="color:#999">ボイスが未登録です。下の「ボイス登録」から登録してください。</em>';
      return;
    }}
    el.innerHTML = voices.map(v => `
      <div class="voice-item">
        <div class="voice-info">
          <strong>${{v.name}}</strong>
          <div class="meta">Speaker ID: ${{v.speaker_id}} / ${{v.ref_text || '(テキストなし)'}}</div>
          ${{v.has_audio ? '<audio controls src="/manage/voice/' + v.dir_name + '/audio"></audio>' : ''}}
        </div>
        <button class="btn-delete" onclick="deleteVoice('${{v.dir_name}}')">削除</button>
      </div>
    `).join('');
  }} catch (err) {{
    el.innerHTML = '<em>読み込みに失敗しました</em>';
  }}
}}

async function deleteVoice(dirName) {{
  if (!confirm('このボイスを削除しますか？')) return;
  await fetch('/manage/voice/' + dirName, {{ method: 'DELETE' }});
  loadVoices();
}}

// --- Synthesis History ---
const history = [];
const MAX_HISTORY = 20;

function addHistory(text, speakerName, blobUrl) {{
  const now = new Date();
  const time = now.getHours().toString().padStart(2,'0') + ':' + now.getMinutes().toString().padStart(2,'0');
  history.unshift({{ text, speakerName, blobUrl, time }});
  if (history.length > MAX_HISTORY) {{
    const old = history.pop();
    URL.revokeObjectURL(old.blobUrl);
  }}
  renderHistory();
}}

function renderHistory() {{
  const el = document.getElementById('historyList');
  const clearBtn = document.getElementById('clearHistoryBtn');
  if (history.length === 0) {{
    el.innerHTML = '<em class="history-empty">まだ合成履歴がありません</em>';
    clearBtn.style.display = 'none';
    return;
  }}
  clearBtn.style.display = 'inline-block';
  el.innerHTML = '<div class="history-list">' + history.map((h, i) => `
    <div class="history-item">
      <span class="hist-time">${{h.time}}</span>
      <span class="hist-text" title="${{h.text}}">${{h.speakerName}}: ${{h.text}}</span>
      <audio controls src="${{h.blobUrl}}"></audio>
      <a href="${{h.blobUrl}}" download="synth_${{i}}.wav" title="ダウンロード" style="font-size:18px;">⬇</a>
    </div>
  `).join('') + '</div>';
}}

function clearHistory() {{
  history.forEach(h => URL.revokeObjectURL(h.blobUrl));
  history.length = 0;
  renderHistory();
}}

function getSpeakerName(speakerId) {{
  const sel = document.getElementById('synthSpeaker');
  const opt = sel.querySelector('option[value="' + speakerId + '"]');
  return opt ? opt.textContent : 'ID:' + speakerId;
}}

// --- Synthesis with progress ---
let synthTimer = null;

async function synthesize() {{
  const speaker = document.getElementById('synthSpeaker').value;
  const text = document.getElementById('synthText').value.trim();
  const btn = document.getElementById('synthBtn');
  const statusEl = document.getElementById('synthStatus');
  const elapsedEl = document.getElementById('synthElapsed');
  const errEl = document.getElementById('synthError');
  const audio = document.getElementById('synthAudio');

  errEl.textContent = '';
  if (!speaker) {{ errEl.textContent = 'スピーカーを選択してください'; return; }}
  if (!text) {{ errEl.textContent = 'テキストを入力してください'; return; }}

  btn.disabled = true;
  statusEl.style.display = 'flex';
  audio.style.display = 'none';

  const startTime = performance.now();
  synthTimer = setInterval(() => {{
    const sec = ((performance.now() - startTime) / 1000).toFixed(1);
    elapsedEl.textContent = sec + '秒';
  }}, 100);

  try {{
    const qRes = await fetch('/audio_query?text=' + encodeURIComponent(text) + '&speaker=' + speaker, {{ method: 'POST' }});
    if (!qRes.ok) throw new Error('audio_query に失敗しました (HTTP ' + qRes.status + ')');
    const query = await qRes.json();
    query.speedScale = parseFloat(document.getElementById('synthSpeed').value);
    query.volumeScale = parseFloat(document.getElementById('synthVolume').value);

    const sRes = await fetch('/synthesis?speaker=' + speaker, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(query),
    }});
    if (!sRes.ok) throw new Error('音声合成に失敗しました (HTTP ' + sRes.status + ')');

    const blob = await sRes.blob();
    const url = URL.createObjectURL(blob);
    audio.src = url;
    audio.style.display = 'block';
    audio.play();
    const dl = document.getElementById('synthDownload');
    dl.href = url;
    dl.style.display = 'inline-flex';

    addHistory(text, getSpeakerName(speaker), url);
  }} catch (err) {{
    errEl.textContent = 'エラー: ' + err.message;
  }} finally {{
    clearInterval(synthTimer);
    statusEl.style.display = 'none';
    btn.disabled = false;
  }}
}}

// --- Batch Synthesis (1件ずつ順次合成) ---
async function batchSynthesize() {{
  const speaker = document.getElementById('batchSpeaker').value;
  const rawText = document.getElementById('batchText').value.trim();
  const btn = document.getElementById('batchBtn');
  const progressEl = document.getElementById('batchProgress');
  const statusText = document.getElementById('batchStatusText');
  const barFill = document.getElementById('batchBarFill');
  const resultsEl = document.getElementById('batchResults');

  if (!speaker) {{ alert('スピーカーを選択してください'); return; }}
  if (!rawText) {{ alert('テキストを入力してください'); return; }}

  const lines = rawText.split('\\n').map(l => l.trim()).filter(l => l);
  if (lines.length === 0) return;

  btn.disabled = true;
  progressEl.style.display = 'block';
  resultsEl.innerHTML = '';
  barFill.style.width = '0%';
  statusText.textContent = '一括合成中... (0/' + lines.length + ')';

  const speed = parseFloat(document.getElementById('batchSpeed').value);
  const volume = parseFloat(document.getElementById('batchVolume').value);
  const speakerName = getSpeakerName(speaker);

  let completed = 0;
  for (let i = 0; i < lines.length; i++) {{
    const text = lines[i];
    statusText.textContent = '合成中... (' + (i + 1) + '/' + lines.length + ')';
    barFill.style.width = (i / lines.length * 100) + '%';
    try {{
      const qRes = await fetch('/audio_query?text=' + encodeURIComponent(text) + '&speaker=' + speaker, {{ method: 'POST' }});
      if (!qRes.ok) throw new Error('audio_query failed');
      const query = await qRes.json();
      query.speedScale = speed;
      query.volumeScale = volume;

      const sRes = await fetch('/synthesis?speaker=' + speaker, {{
        method: 'POST',
        headers: {{ 'Content-Type': 'application/json' }},
        body: JSON.stringify(query),
      }});
      if (!sRes.ok) throw new Error('synthesis failed');
      const blob = await sRes.blob();
      const url = URL.createObjectURL(blob);

      const itemId = 'batch-item-' + i;
      resultsEl.innerHTML += `
        <div class="batch-item" id="${{itemId}}">
          <span class="batch-text">${{text}}</span>
          <audio controls src="${{url}}"></audio>
          <button onclick="batchRegenerate('${{itemId}}', ${{speaker}}, ${{i}})" title="再生成" style="background:none;border:1px solid #ccc;border-radius:4px;cursor:pointer;font-size:16px;padding:2px 6px;">&#x21bb;</button>
          <a href="${{url}}" download="batch_${{i + 1}}.wav" title="ダウンロード" style="font-size:16px;">⬇</a>
        </div>`;

      addHistory(text, speakerName, url);
      completed++;
    }} catch (err) {{
      resultsEl.innerHTML += '<div style="color:#c62828;font-size:13px;">' + text + ': ' + err.message + '</div>';
    }}
  }}

  barFill.style.width = '100%';
  statusText.textContent = '完了 (' + completed + '/' + lines.length + '件)';
  setTimeout(() => {{ progressEl.style.display = 'none'; }}, 3000);
  btn.disabled = false;
}}

// --- Batch Regenerate (1件再生成) ---
async function batchRegenerate(itemId, speaker, index) {{
  const el = document.getElementById(itemId);
  if (!el) return;
  const textEl = el.querySelector('.batch-text');
  const text = textEl ? textEl.textContent : '';
  if (!text) return;

  const regenBtn = el.querySelector('button');
  if (regenBtn) regenBtn.disabled = true;

  const speed = parseFloat(document.getElementById('batchSpeed').value);
  const volume = parseFloat(document.getElementById('batchVolume').value);
  const speakerName = getSpeakerName(speaker);

  try {{
    const qRes = await fetch('/audio_query?text=' + encodeURIComponent(text) + '&speaker=' + speaker, {{ method: 'POST' }});
    if (!qRes.ok) throw new Error('audio_query failed');
    const query = await qRes.json();
    query.speedScale = speed;
    query.volumeScale = volume;

    const sRes = await fetch('/synthesis?speaker=' + speaker, {{
      method: 'POST',
      headers: {{ 'Content-Type': 'application/json' }},
      body: JSON.stringify(query),
    }});
    if (!sRes.ok) throw new Error('synthesis failed');
    const blob = await sRes.blob();
    const url = URL.createObjectURL(blob);

    // audio要素を更新
    const audio = el.querySelector('audio');
    if (audio) audio.src = url;
    // ダウンロードリンクを更新
    const dl = el.querySelector('a');
    if (dl) dl.href = url;

    addHistory(text, speakerName, url);
  }} catch (err) {{
    alert('再生成エラー: ' + err.message);
  }} finally {{
    if (regenBtn) regenBtn.disabled = false;
  }}
}}

// --- Speaker Select ---
function updateSpeakerSelect(voices) {{
  const selIds = ['synthSpeaker', 'batchSpeaker'];
  selIds.forEach(id => {{
    const sel = document.getElementById(id);
    if (voices.length === 0) {{
      sel.innerHTML = '<option value="">-- ボイス未登録 --</option>';
      return;
    }}
    sel.innerHTML = voices.map(v =>
      '<option value="' + v.speaker_id + '">' + v.name + ' (ID: ' + v.speaker_id + ')</option>'
    ).join('');
  }});
}}

loadVoices();
</script>
</body>
</html>"""


@app.get("/manage/voices")
async def manage_list_voices():
    """Register voice list for management UI"""
    result = []
    if not VOICES_DIR.exists():
        return result
    for d in sorted(VOICES_DIR.iterdir()):
        if not d.is_dir():
            continue
        meta_path = d / "meta.json"
        if not meta_path.exists():
            continue
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
        result.append({
            "dir_name": d.name,
            "name": meta.get("name", d.name),
            "speaker_id": meta.get("speaker_id", 0),
            "ref_text": meta.get("ref_text", ""),
            "language": meta.get("language", "Japanese"),
            "has_audio": (d / meta.get("ref_audio", "reference.wav")).exists(),
        })
    return result


@app.post("/manage/voice")
async def manage_save_voice(
    name: str = Form(...),
    ref_text: str = Form(...),
    language: str = Form("Japanese"),
    audio: UploadFile | None = File(None),
):
    """Save or update a voice profile"""
    dir_name = name.replace(" ", "_").replace("/", "_")
    voice_dir = VOICES_DIR / dir_name
    voice_dir.mkdir(parents=True, exist_ok=True)

    meta_path = voice_dir / "meta.json"

    # Load existing meta or create new
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    else:
        used_ids = set()
        for d in VOICES_DIR.iterdir():
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
            "styles": [{"name": "\u30ce\u30fc\u30de\u30eb", "id": new_id, "type": "talk"}],
            "ref_audio": "reference.wav",
        }

    meta["name"] = name
    meta["ref_text"] = ref_text
    meta["language"] = language

    # Save audio file
    if audio and audio.filename:
        audio_path = voice_dir / "reference.wav"
        content = await audio.read()
        with open(audio_path, "wb") as f:
            f.write(content)

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=2)

    # Reload voices in engine
    engine.voices.clear()
    engine._voice_prompts.clear()
    engine._load_voices()

    return {"message": f"ボイス '{name}' を登録しました。"}


@app.get("/manage/voice/{dir_name}/audio")
async def manage_get_audio(dir_name: str):
    """Serve reference audio for playback"""
    voice_dir = VOICES_DIR / dir_name
    meta_path = voice_dir / "meta.json"
    if not meta_path.exists():
        raise HTTPException(status_code=404)
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    audio_path = voice_dir / meta.get("ref_audio", "reference.wav")
    if not audio_path.exists():
        raise HTTPException(status_code=404)
    return Response(content=audio_path.read_bytes(), media_type="audio/wav")


@app.delete("/manage/voice/{dir_name}")
async def manage_delete_voice(dir_name: str):
    """Delete a voice profile"""
    voice_dir = VOICES_DIR / dir_name
    if voice_dir.exists():
        shutil.rmtree(voice_dir)
        engine.voices.clear()
        engine._voice_prompts.clear()
        engine._load_voices()
    return {"message": "削除しました"}


@app.post("/manage/reload")
async def manage_reload_voices():
    """Reload voice profiles from disk"""
    engine.voices.clear()
    engine._voice_prompts.clear()
    engine._load_voices()
    return {"message": f"{len(engine.voices)} 件のボイスを再読み込みしました"}


@app.post("/batch_synthesis")
async def batch_synthesis(
    speaker: int = Query(..., description="スタイルID"),
    request: Request = ...,
):
    """複数テキストをバッチ合成してZIPで返す（モデル内部で一括処理）

    Body: {"texts": ["テキスト1", "テキスト2", ...],
           "speedScale": 1.0, "volumeScale": 1.0}
    """
    body = await request.json()
    texts = body.get("texts", [])
    if not texts:
        raise HTTPException(status_code=400, detail="テキストが空です")

    speed = float(body.get("speedScale", 1.0))
    volume = float(body.get("volumeScale", 1.0))
    output_sr = int(body.get("outputSamplingRate", 24000))

    try:
        results = engine.synthesize_batch(
            texts=texts,
            speaker_id=speaker,
            speed=speed,
            volume=volume,
            output_sr=output_sr,
        )
    except ValueError as e:
        raise HTTPException(status_code=404, detail=str(e))
    except Exception as e:
        logger.exception("バッチ合成エラー")
        raise HTTPException(status_code=500, detail=f"合成エラー: {e}")

    zip_buf = io.BytesIO()
    with zipfile.ZipFile(zip_buf, "w", zipfile.ZIP_STORED) as zf:
        for i, (wav, sr) in enumerate(results):
            buf = io.BytesIO()
            sf.write(buf, wav, sr, format="WAV", subtype="PCM_16")
            zf.writestr(f"{i:03d}.wav", buf.getvalue())
    zip_buf.seek(0)
    return Response(content=zip_buf.read(), media_type="application/zip")


if __name__ == "__main__":
    uvicorn.run(
        "server:app",
        host="0.0.0.0",
        port=50021,
        log_level="info",
    )
