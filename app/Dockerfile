FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN apt-get update && apt-get install -y --no-install-recommends \
        ca-certificates \
        curl \
        git \
        python3 \
        python3-pip \
        python3-venv \
        skopeo \
        jq \
        tar \
        gzip \
        xz-utils \
        unzip \
        rsync \
        iproute2 \
        docker.io \
        apt-utils \
        dpkg-dev \
    && rm -rf /var/lib/apt/lists/*

ARG HELM_VERSION=3.16.4
RUN curl -fsSL "https://get.helm.sh/helm-v${HELM_VERSION}-linux-amd64.tar.gz" \
      | tar -xz -C /tmp \
    && mv /tmp/linux-amd64/helm /usr/local/bin/helm \
    && chmod +x /usr/local/bin/helm \
    && rm -rf /tmp/linux-amd64

WORKDIR /app

COPY requirements.txt .
RUN pip3 install --break-system-packages -r requirements.txt \
    && pip3 install --break-system-packages python-pypi-mirror twine

COPY . .

RUN mkdir -p /data/outputs /data/cache /data/logs \
    && chmod +x /app/entrypoint.sh

EXPOSE 8000
ENTRYPOINT ["/app/entrypoint.sh"]
