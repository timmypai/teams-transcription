# syntax=docker/dockerfile:1@sha256:4edf897a3ffa55b89f906fc8cc78afdb3f1834cc9c7083565e611a8a7d5fe99e
# Supply a verified immutable base, e.g. python@sha256:<pulled RepoDigest>.
ARG PYTHON_BASE=python@sha256:7753c33391fc9f01d1984375bf375eb6686d52ba10db6043a86634a5ccf90dcf
FROM ${PYTHON_BASE}
ARG TARGETARCH
ARG CODEX_VERSION=0.159.0-alpha.3
WORKDIR /opt/meeting
# Optional combined trust bundle is mounted only during networked build steps.
COPY tools/prefetch_codex_npm.py /tmp/prefetch_codex_npm.py
RUN --mount=type=secret,id=ca_bundle,mode=0444 \
    if [ -f /run/secrets/ca_bundle ]; then \
      export SSL_CERT_FILE=/run/secrets/ca_bundle PIP_CERT=/run/secrets/ca_bundle; \
      printf 'Acquire::https::CaInfo "/run/secrets/ca_bundle";\n' > /etc/apt/apt.conf.d/99-build-ca; \
    fi; \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update --error-on=any && apt-get install -y --no-install-recommends \
      chromium chromium-sandbox ffmpeg espeak-ng nodejs npm ca-certificates tzdata \
    && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99-build-ca
RUN --network=none \
    --mount=type=bind,from=codex_npm,target=/workspace/codex-bundle \
    case "${TARGETARCH}" in amd64) platform=linux-x64 ;; arm64) platform=linux-arm64 ;; *) exit 1 ;; esac \
    && python /tmp/prefetch_codex_npm.py /workspace/codex-bundle --verify --platform "${platform}" \
    && mkdir /tmp/codex-bundle \
    && cp /workspace/codex-bundle/package.json /workspace/codex-bundle/empty-global.npmrc /tmp/codex-bundle/ \
    && for version in "${CODEX_VERSION}" "${CODEX_VERSION}-${platform}"; do \
      cp "/workspace/codex-bundle/metadata-${version}.json" \
         "/workspace/codex-bundle/codex-${version}.tgz" /tmp/codex-bundle/ || exit 1; \
    done \
    && npm install --prefix /tmp/codex-bundle --offline --omit=optional \
      --ignore-scripts --no-audit --no-fund --userconfig=/dev/null \
      --globalconfig=/tmp/codex-bundle/empty-global.npmrc --cache=/tmp/codex-npm-cache \
    && mkdir -p /opt/codex \
    && mv /tmp/codex-bundle/node_modules /opt/codex/node_modules \
    && ln -s /opt/codex/node_modules/.bin/codex /usr/local/bin/codex \
    && test "$(codex --version)" = "codex-cli ${CODEX_VERSION}" \
    && rm -rf /tmp/codex-bundle /tmp/codex-npm-cache /tmp/prefetch_codex_npm.py /root/.npm
COPY pyproject.toml ./
COPY teams_transcription ./teams_transcription
COPY runtime.example.yml ./runtime.example.yml
RUN --network=none \
    --mount=type=bind,from=python_wheels,target=/workspace/python-wheels \
    python -m pip install --no-index --no-cache-dir --find-links=/workspace/python-wheels . \
    && useradd --uid 10001 --create-home recorder \
    && install -d -m 0700 -o 10001 -g 10001 /data
COPY docker/entrypoint.py /opt/meeting/entrypoint.py
# Source files may be created with umask 077 in the development workspace.
# Runtime UID is operator-selected, so shipped code must be readable by it.
RUN chmod -R a+rX /opt/meeting
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 \
    XDG_CACHE_HOME=/tmp/meeting-cache XDG_CONFIG_HOME=/tmp/meeting-config \
    ASSISTANT_HOST=0.0.0.0 ASSISTANT_CHROMIUM=/usr/bin/chromium
USER 10001:10001
ENTRYPOINT ["python", "/opt/meeting/entrypoint.py"]
CMD ["serve", "--config", "/data/config/runtime.yml"]
