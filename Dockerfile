# The Press: the paper server, the fetcher, the builder, and the sources it
# writes its own paper from, in one small image.
# The same image also runs the optional staging server and the gadget scaffold;
# only the command differs.

FROM python:3.13-slim AS build
COPY --from=ghcr.io/astral-sh/uv:0.9 /uv /usr/local/bin/uv
WORKDIR /app
# Dependencies only (Pillow, ebooklib and the Anthropic SDK). All ship binary wheels for amd64
# and arm64, so nothing is compiled and no build tools are needed.
COPY pyproject.toml uv.lock ./
RUN UV_PROJECT_ENVIRONMENT=/venv UV_LINK_MODE=copy UV_COMPILE_BYTECODE=1 \
    uv sync --frozen --no-dev --no-install-project
# The front page is set in Noto Serif. The package carries hundreds of other
# faces, so only the four serif files are carried into the final image.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fonts-noto-core \
    && mkdir /fonts \
    && cp /usr/share/fonts/truetype/noto/NotoSerif-Regular.ttf \
          /usr/share/fonts/truetype/noto/NotoSerif-Bold.ttf \
          /usr/share/fonts/truetype/noto/NotoSerif-Italic.ttf \
          /usr/share/fonts/truetype/noto/NotoSerif-BoldItalic.ttf /fonts/

FROM python:3.13-slim
# fontconfig lets the builder find the serif face; tzdata makes TZ work, so
# "06:40" means 06:40 where you live.
RUN apt-get update \
    && apt-get install -y --no-install-recommends fontconfig tzdata \
    && rm -rf /var/lib/apt/lists/* \
    && useradd --uid 1000 --create-home --shell /usr/sbin/nologin press \
    && mkdir /data /config /inbox && chown press:press /data /inbox
COPY --from=build /fonts /usr/share/fonts/truetype/noto-serif
COPY --from=build /venv /venv
RUN fc-cache -f
WORKDIR /app
# The package is run from here, not installed: PYTHONPATH below points at it.
COPY src ./src
# The default paper. A sources.toml mounted at /config replaces it.
COPY sources.toml ./
COPY integrations/muse/gadget ./integrations/muse/gadget
# Served to the Kindle, which installs and updates itself from the Press.
COPY kindle ./kindle
ENV PATH=/venv/bin:$PATH \
    PYTHONPATH=/app/src \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PAPER_DATA_DIR=/data \
    PRESS_CONFIG_DIR=/config \
    PRESS_INBOX_DIR=/inbox \
    PRESS_IN_CONTAINER=1 \
    STAGE_DIR=/data \
    MORNING_PAPER_REPO=/app \
    MORNING_PAPER_RUNNER="python -m mnn"
USER press
VOLUME /data
EXPOSE 8484
CMD ["python", "-m", "mnn", "server"]
