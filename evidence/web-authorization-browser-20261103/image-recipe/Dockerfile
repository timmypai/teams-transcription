FROM python@sha256:7753c33391fc9f01d1984375bf375eb6686d52ba10db6043a86634a5ccf90dcf
COPY requirements.txt /tmp/browser-test-requirements.txt
RUN --mount=type=secret,id=ca_bundle,mode=0444 \
    if [ -f /run/secrets/ca_bundle ]; then \
      export SSL_CERT_FILE=/run/secrets/ca_bundle PIP_CERT=/run/secrets/ca_bundle; \
      printf 'Acquire::https::CaInfo "/run/secrets/ca_bundle";\n' > /etc/apt/apt.conf.d/99-build-ca; \
    fi; \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources \
    && apt-get update --error-on=any \
    && apt-get install -y --no-install-recommends chromium chromium-sandbox fonts-noto-cjk ca-certificates \
    && python -m pip install --no-cache-dir -r /tmp/browser-test-requirements.txt \
    && rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/99-build-ca /tmp/browser-test-requirements.txt \
    && useradd --uid 10001 --create-home recorder
ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1 XDG_CACHE_HOME=/tmp/meeting-cache XDG_CONFIG_HOME=/tmp/meeting-config
USER 10001:10001
WORKDIR /tmp
ENTRYPOINT ["python"]
