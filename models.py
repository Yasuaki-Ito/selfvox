"""VOICEVOX互換データモデル定義"""

from __future__ import annotations

from typing import Optional

from version import VERSION

from pydantic import BaseModel, Field


class Mora(BaseModel):
    text: str
    consonant: Optional[str] = None
    consonant_length: Optional[float] = None
    vowel: str
    vowel_length: float
    pitch: float


class AccentPhrase(BaseModel):
    moras: list[Mora]
    accent: int
    pause_mora: Optional[Mora] = None
    is_interrogative: bool = False


class AudioQuery(BaseModel):
    accent_phrases: list[AccentPhrase] = Field(default_factory=list)
    speedScale: float = 1.0
    pitchScale: float = 0.0
    intonationScale: float = 1.0
    volumeScale: float = 1.0
    prePhonemeLength: float = 0.1
    postPhonemeLength: float = 0.1
    pauseLength: Optional[float] = None
    pauseLengthScale: float = 1.0
    outputSamplingRate: int = 24000
    outputStereo: bool = False
    kana: str = ""

    # 内部用: Qwen3-TTSに渡す元テキスト
    _text: str = ""

    class Config:
        # _text をJSON出力に含める
        json_schema_extra = {
            "example": {
                "accent_phrases": [],
                "speedScale": 1.0,
                "pitchScale": 0.0,
                "intonationScale": 1.0,
                "volumeScale": 1.0,
                "prePhonemeLength": 0.1,
                "postPhonemeLength": 0.1,
                "pauseLength": None,
                "pauseLengthScale": 1.0,
                "outputSamplingRate": 24000,
                "outputStereo": False,
                "kana": "",
            }
        }


class SpeakerStyle(BaseModel):
    name: str
    id: int
    type: str = "talk"


class SpeakerSupportedFeatures(BaseModel):
    permitted_synthesis_morphing: str = "NOTHING"


class Speaker(BaseModel):
    name: str
    speaker_uuid: str
    styles: list[SpeakerStyle]
    version: str = VERSION
    supported_features: SpeakerSupportedFeatures = Field(
        default_factory=SpeakerSupportedFeatures
    )


class SupportedDevices(BaseModel):
    cpu: bool = False
    cuda: bool = True
    dml: bool = False
