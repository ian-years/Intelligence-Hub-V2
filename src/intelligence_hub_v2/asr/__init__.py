"""本地 ASR（sherpa-onnx + SenseVoice）。契约：`models.transcript.Transcript`。

引擎与切句算法在 `engine.py`；不写盘、不碰 SQLite（那是 `tasks/postprocess.py` 的活）。
"""

from intelligence_hub_v2.asr.engine import (
    AsrUnavailable,
    EngineStatus,
    detect,
    detect_model_dir,
    transcribe_wav,
)

__all__ = ["AsrUnavailable", "EngineStatus", "detect", "detect_model_dir", "transcribe_wav"]
