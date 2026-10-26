"""Chooses the Pipeline implementation from PIPELINE_BACKEND, so swapping is config-only."""

import httpx

from service.config import Settings
from service.pipeline.base import Pipeline
from service.pipeline.http_mock import HttpMockPipeline
from service.pipeline.open_weight import OpenWeightPipeline


def build_pipeline(settings: Settings, client: httpx.AsyncClient) -> Pipeline:
    if settings.pipeline_backend == "mock":
        return HttpMockPipeline(client, settings.pipeline_url, settings.attempt_timeout_s)
    if settings.pipeline_backend == "open_weight":
        return OpenWeightPipeline(client, settings.pipeline_url, settings.attempt_timeout_s)
    raise ValueError(f"unknown PIPELINE_BACKEND: {settings.pipeline_backend}")
