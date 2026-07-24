FROM python:3.12-bookworm

# llama-cpp-python builds from source; the toolchain is needed at install time.
RUN apt-get update \
 && apt-get install -y --no-install-recommends build-essential cmake \
 && rm -rf /var/lib/apt/lists/*

RUN --mount=source=requirements.txt,target=/mnt/requirements.txt,type=bind \
    pip3 install --no-cache-dir -r /mnt/requirements.txt

WORKDIR /app
COPY ragsvc /app/ragsvc

# Weights are baked in at build time (or bind-mounted at run time) and never
# fetched at runtime: the container has no egress. Uncomment and point at your
# own artifacts -- they are intentionally not downloaded from a public hub here,
# so an image build is reproducible and offline.
#
#   COPY models/embed  /models/embed
#   COPY models/llm.gguf /models/llm.gguf
#
# ENV RAG_EMBED_MODEL_PATH=/models/embed
# ENV RAG_LLM_MODEL_PATH=/models/llm.gguf

# Belt and braces: even if a library wanted to reach a model hub, it cannot.
ENV HF_HUB_OFFLINE=1 \
    TRANSFORMERS_OFFLINE=1 \
    HF_HUB_DISABLE_TELEMETRY=1 \
    SENTENCE_TRANSFORMERS_HOME=/models \
    RAG_DATA_DIR=/data \
    PYTHONUNBUFFERED=1

VOLUME ["/data"]
EXPOSE 8080

# One worker on purpose: the generation gate (RAG_MAX_CONCURRENCY) is what
# limits concurrency, and a second worker would mean a second copy of the model
# in RAM competing for the same cores.
CMD ["uvicorn", "ragsvc.api:app", "--host", "0.0.0.0", "--port", "8080", "--workers", "1"]
