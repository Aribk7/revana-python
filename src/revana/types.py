"""Response and request shapes. Every one is a plain dict at runtime."""

import os
from typing import Any, Dict, List, Literal, Mapping, Optional, TypedDict, Union

__all__ = [
    "GenerationStatus",
    "GenerationInput",
    "GenerationOutput",
    "GenerationError",
    "Generation",
    "Model",
    "Balance",
    "Upload",
    "FileInput",
    "InputLike",
]

GenerationStatus = Literal["queued", "running", "succeeded", "failed"]


class GenerationInput(TypedDict, total=False):
    """The input of any model. Which fields a model takes is listed by ``models.list()``.

    Models may take more fields than listed here; any extra key is passed through.
    """

    prompt: str
    negative_prompt: str
    #: Start frame (image to video) or the picture to edit (image to image).
    image_url: str
    #: Last frame, on models with first and last frame. Needs image_url.
    end_image_url: str
    #: Reference pictures.
    image_urls: List[str]
    #: A clip to transform or extend.
    video_url: str
    #: Reference clips (Seedance 2.5).
    video_urls: List[str]
    #: Sound that drives the video (lip sync, talking avatars).
    audio_url: str
    #: Reference sound (Seedance 2.5).
    audio_urls: List[str]
    #: e.g. "480p", "720p", "1080p", "1K", "2K", "4K".
    resolution: str
    #: e.g. "16:9", "9:16", "1:1".
    aspect_ratio: str
    #: Video length in seconds.
    duration: float
    #: Generate sound, on models that offer it.
    audio: bool
    quality: str
    seed: int


class _GenerationOutputRequired(TypedDict):
    urls: List[str]


class GenerationOutput(_GenerationOutputRequired, total=False):
    duration_s: float
    width: int
    height: int
    has_audio: bool


class _GenerationErrorRequired(TypedDict):
    code: str
    message: str


class GenerationError(_GenerationErrorRequired, total=False):
    #: True: the same request can work later. False: the request has to change.
    retryable: bool
    reason: str


class _GenerationRequired(TypedDict):
    id: str
    object: Literal["generation"]
    model: str
    status: GenerationStatus
    output: Optional[GenerationOutput]
    quoted_usd: float
    #: The charge once succeeded, 0 once failed, else None.
    cost_usd: Optional[float]
    attempts: int
    error: Optional[GenerationError]
    created_at: str
    completed_at: Optional[str]


class Generation(_GenerationRequired, total=False):
    message: str
    reserved_usd: Optional[float]


class _ModelRequired(TypedDict):
    id: str
    object: Literal["model"]


class Model(_ModelRequired, total=False):
    name: str
    maker: Optional[str]
    #: "image", "video", "audio" or "text" (chat models).
    kind: str
    tasks: List[str]
    options: Dict[str, Any]
    price: Dict[str, Any]


class _BalanceRequired(TypedDict):
    object: Literal["balance"]
    currency: Literal["usd"]
    available_usd: float
    held_usd: float
    balance_usd: float
    available_credits: int
    held_credits: int
    balance_credits: int


class Balance(_BalanceRequired, total=False):
    spent_usd: float


class _UploadRequired(TypedDict):
    id: str
    object: Literal["upload"]
    #: Pass this as image_url / video_url / audio_url. Good for 14 days.
    url: str
    expires_at: str
    upload_url: str
    upload_method: Literal["PUT"]
    upload_headers: Dict[str, str]
    upload_expires_at: str
    content_type: str
    bytes: int


class Upload(_UploadRequired, total=False):
    filename: str


#: A GenerationInput, or any mapping for model-specific fields.
InputLike = Union[GenerationInput, Mapping[str, Any]]

#: Raw bytes, or the path of a local file.
FileInput = Union[bytes, bytearray, memoryview, str, "os.PathLike[str]"]
