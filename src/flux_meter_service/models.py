"""The request and response bodies."""

from __future__ import annotations

from pydantic import BaseModel


class CameraInfo(BaseModel):
    model: str
    serial_number: str
    saturation_level: int
    description: str


class Settings(BaseModel):
    exposure_us: int
    gain: int
    black_level: int


class SavedFrame(BaseModel):
    path: str
    sequence: int
