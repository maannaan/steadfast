# Open-weight STT -> LLM -> TTS server. Weights are not in the image: they are fetched from
# S3 at startup (see model/weights.py).
#
#   docker buildx build -f model/Dockerfile --build-arg COMPUTE=cpu .   (default)
#   docker buildx build -f model/Dockerfile --build-arg COMPUTE=gpu .
#
# CPU: python:3.12-slim. GPU: CUDA 12 + cuDNN 9 runtime on Ubuntu 24.04 (Python 3.12, what
# CTranslate2 4.5+ and the cu124 llama-cpp-python wheels need); the ECS GPU AMI provides
# the driver.
#
# Built in AWS by scripts/build_model.sh, behind a NAT gateway whose shared address can
# hit Docker Hub's anonymous pull limit, so the CPU base is the same official image via
# ECR Public.
ARG COMPUTE=cpu

# llama-cpp-python for CPU is compiled here rather than taken from the prebuilt CPU wheel
# index, whose wheels link against musl and fail to load on Debian (glibc). GGML_NATIVE
# off + AVX2: the binary must not depend on the build host's exact CPU. Only the wheel is
# copied on, so the compilers stay out of the final image.
FROM public.ecr.aws/docker/library/python:3.12-slim AS llama-cpu
RUN apt-get update \
    && apt-get install -y --no-install-recommends build-essential cmake \
    && rm -rf /var/lib/apt/lists/*
ENV CMAKE_ARGS="-DGGML_NATIVE=OFF -DGGML_AVX2=ON -DGGML_FMA=ON -DGGML_F16C=ON"
RUN CMAKE_BUILD_PARALLEL_LEVEL="$(nproc)" \
    pip wheel --no-cache-dir --no-deps --wheel-dir /wheels llama-cpp-python==0.3.16

FROM public.ecr.aws/docker/library/python:3.12-slim AS base-cpu
COPY --from=llama-cpu /wheels /wheels

# GPU (untested: needs a GPU quota): the prebuilt CUDA 12.4 wheel.
FROM nvidia/cuda:12.6.3-cudnn-runtime-ubuntu24.04 AS base-gpu
RUN apt-get update \
    && apt-get install -y --no-install-recommends python3 python3-venv \
    && rm -rf /var/lib/apt/lists/* \
    && python3 -m venv /opt/venv
ENV PATH=/opt/venv/bin:$PATH
RUN pip download --no-cache-dir --no-deps --only-binary=:all: --dest /wheels \
    --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cu124 \
    llama-cpp-python==0.3.4

FROM base-${COMPUTE}
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 PIP_NO_CACHE_DIR=1
WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates curl libgomp1 \
    && rm -rf /var/lib/apt/lists/*

# Piper TTS, the MIT-licensed 2023.11.14-2 release (not the later GPL piper1-gpl), as a
# standalone binary. model/stages.py runs it as a separate process.
RUN curl -fsSL --retry 5 \
      https://github.com/rhasspy/piper/releases/download/2023.11.14-2/piper_linux_x86_64.tar.gz \
    | tar -xz -C /opt

COPY model/requirements.txt model-requirements.txt
RUN pip install --retries 10 --timeout 60 -r model-requirements.txt /wheels/*.whl \
    && rm -rf /wheels \
    && useradd --create-home --uid 10001 app \
    && mkdir -p /models && chown app /models
# On ECS /models is a host directory (weights survive task restarts); the instance's user
# data creates it owned by uid 10001.

COPY model/ model/
# Fail the build, not the deployed task, on a missing module or an unresolved Piper library.
RUN python -c "import faster_whisper, llama_cpp, model.app" \
    && ! ldd /opt/piper/piper | grep -q 'not found'
USER app

EXPOSE 8080
CMD ["python", "-m", "model.app"]
