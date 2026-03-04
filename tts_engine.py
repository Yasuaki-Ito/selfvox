"""Qwen3-TTS エンジンラッパー"""

from __future__ import annotations

import json
import logging
import os
import uuid
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import soundfile as sf
import torch
import torchaudio

logger = logging.getLogger(__name__)


def _app_dir() -> Path:
    """アプリのベースディレクトリ（__pycache__から実行時も正しく解決）"""
    d = Path(__file__).resolve().parent
    if d.name == "__pycache__":
        d = d.parent
    return d


VOICES_DIR = _app_dir() / "voices"


@dataclass
class VoiceProfile:
    """音声プロファイル"""

    name: str
    speaker_id: int
    styles: list[dict]
    ref_audio_path: Path
    ref_text: str
    language: str = "Japanese"
    speaker_uuid: str = field(default_factory=lambda: str(uuid.uuid4()))


class TTSEngine:
    """Qwen3-TTS Voice Clone エンジン"""

    def __init__(self, model_name: str = "Qwen/Qwen3-TTS-12Hz-1.7B-Base"):
        self.model_name = model_name
        self.model = None
        self.voices: dict[int, VoiceProfile] = {}  # speaker_id -> VoiceProfile
        self._voice_prompts: dict[int, dict] = {}  # speaker_id -> cached prompt
        self._loaded = False

    def load(self) -> None:
        """モデルとボイスプロファイルをロード"""
        if self._loaded:
            return

        from qwen_tts import Qwen3TTSModel

        # 初回ダウンロードかどうかを判定
        hf_home = os.environ.get("HF_HOME", Path.home() / ".cache" / "huggingface")
        model_cache = Path(hf_home) / "hub" / ("models--" + self.model_name.replace("/", "--"))
        first_download = not model_cache.exists()

        if first_download:
            logger.info(
                "=== 初回起動: モデルをダウンロードします (~4.5GB) ==="
            )
            logger.info(
                "ダウンロード先: %s", hf_home
            )
            logger.info(
                "インターネット接続が必要です。完了まで数分〜十数分かかります。"
            )
        else:
            logger.info("Qwen3-TTS モデルをロード中: %s", self.model_name)

        device = "cuda:0" if torch.cuda.is_available() else "cpu"
        logger.info("デバイス: %s", device)

        self.model = Qwen3TTSModel.from_pretrained(
            self.model_name,
            device_map=device,
            dtype=torch.float16,
            low_cpu_mem_usage=True,
            attn_implementation="sdpa",
        )
        logger.info("モデルロード完了")

        self._load_voices()
        self._loaded = True

    def _load_voices(self) -> None:
        """voices/ ディレクトリからボイスプロファイルを読み込み"""
        if not VOICES_DIR.exists():
            VOICES_DIR.mkdir(parents=True, exist_ok=True)
            logger.warning("voices/ ディレクトリが空です。ボイスプロファイルを追加してください。")
            return

        for voice_dir in sorted(VOICES_DIR.iterdir()):
            if not voice_dir.is_dir():
                continue
            meta_path = voice_dir / "meta.json"
            if not meta_path.exists():
                logger.warning("meta.json が見つかりません: %s", voice_dir)
                continue

            with open(meta_path, encoding="utf-8") as f:
                meta = json.load(f)

            ref_audio_path = voice_dir / meta.get("ref_audio", "reference.wav")
            if not ref_audio_path.exists():
                logger.warning("リファレンス音声が見つかりません: %s", ref_audio_path)
                continue

            profile = VoiceProfile(
                name=meta.get("name", voice_dir.name),
                speaker_id=meta.get("speaker_id", len(self.voices)),
                styles=meta.get("styles", [{"name": "ノーマル", "id": meta.get("speaker_id", len(self.voices))}]),
                ref_audio_path=ref_audio_path,
                ref_text=meta.get("ref_text", ""),
                language=meta.get("language", "Japanese"),
                speaker_uuid=meta.get("speaker_uuid", str(uuid.uuid4())),
            )

            for style in profile.styles:
                if "id" not in style:
                    style["id"] = profile.speaker_id

            self.voices[profile.speaker_id] = profile
            logger.info("ボイスプロファイル読み込み: %s (ID=%d)", profile.name, profile.speaker_id)

        # リファレンス音声を事前エンコードしてキャッシュ
        for sid, profile in self.voices.items():
            logger.info("ボイスプロンプトを事前計算中: %s (ID=%d)", profile.name, sid)
            self._voice_prompts[sid] = self.model.create_voice_clone_prompt(
                ref_audio=str(profile.ref_audio_path),
                ref_text=profile.ref_text,
            )
            logger.info("ボイスプロンプト計算完了: %s", profile.name)

    def get_speaker_id_from_style(self, style_id: int) -> int | None:
        """style_id から対応する speaker_id を検索"""
        for sid, profile in self.voices.items():
            for style in profile.styles:
                if style.get("id") == style_id:
                    return sid
        return None

    def synthesize(
        self,
        text: str,
        speaker_id: int,
        speed: float = 1.0,
        volume: float = 1.0,
        output_sr: int = 24000,
    ) -> tuple[np.ndarray, int]:
        """テキストから音声を合成

        Returns:
            (wav_data, sample_rate) のタプル
        """
        if self.model is None:
            raise RuntimeError("モデルが未ロードです。load() を呼んでください。")

        # speaker_id に対応するボイスを取得
        profile = self.voices.get(speaker_id)
        if profile is None:
            real_sid = self.get_speaker_id_from_style(speaker_id)
            if real_sid is not None:
                profile = self.voices[real_sid]
            else:
                raise ValueError(f"スピーカーID {speaker_id} が見つかりません")

        # テキスト末尾に句読点がなければ追加（モデルが文末を適切に生成するため）
        end_puncts = ("。", "！", "？", ".", "!", "?", "…", "」", "』", "）", ")")
        if text and not text.rstrip().endswith(end_puncts):
            text = text.rstrip() + "。"

        # キャッシュ済みプロンプトを取得（なければリアルタイム計算）
        prompt = self._voice_prompts.get(profile.speaker_id)

        if prompt is not None:
            wavs, sr = self.model.generate_voice_clone(
                text=text,
                language=profile.language,
                voice_clone_prompt=prompt,
                max_new_tokens=4096,
            )
        else:
            wavs, sr = self.model.generate_voice_clone(
                text=text,
                language=profile.language,
                ref_audio=str(profile.ref_audio_path),
                ref_text=profile.ref_text,
                max_new_tokens=4096,
            )

        wav = wavs[0]

        if isinstance(wav, torch.Tensor):
            wav = wav.cpu().numpy()

        # 速度調整（リサンプリング）
        if abs(speed - 1.0) > 0.01:
            wav_tensor = torch.from_numpy(wav).unsqueeze(0).float()
            wav_tensor = torchaudio.functional.resample(
                wav_tensor, orig_freq=sr, new_freq=int(sr * speed)
            )
            wav = wav_tensor.squeeze(0).numpy()

        # 音量調整
        if abs(volume - 1.0) > 0.01:
            wav = wav * volume

        # サンプリングレート変換
        if sr != output_sr:
            wav_tensor = torch.from_numpy(wav).unsqueeze(0).float()
            wav_tensor = torchaudio.functional.resample(
                wav_tensor, orig_freq=sr, new_freq=output_sr
            )
            wav = wav_tensor.squeeze(0).numpy()
            sr = output_sr

        # クリッピング防止
        wav = np.clip(wav, -1.0, 1.0)

        return wav, sr
