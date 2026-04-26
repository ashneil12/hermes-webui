FROM python:3.12-slim

LABEL maintainer="nesquena"
LABEL description="Hermes Web UI — browser interface for Hermes Agent"

# Install system packages
ENV DEBIAN_FRONTEND=noninteractive

# Make use of apt-cacher-ng if available
RUN if [ "A${BUILD_APT_PROXY:-}" != "A" ]; then \
        echo "Using APT proxy: ${BUILD_APT_PROXY}"; \
        printf 'Acquire::http::Proxy "%s";\n' "$BUILD_APT_PROXY" > /etc/apt/apt.conf.d/01proxy; \
    fi \
    && apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates wget gnupg \
    && rm -rf /var/lib/apt/lists/* \
    && apt-get clean

RUN apt-get update -y --fix-missing --no-install-recommends \
    && apt-get install -y --no-install-recommends \
    apt-utils \
    locales \
    ca-certificates \
    sudo \
    curl \
    gh \
    rsync \
    openssh-client \
    && apt-get upgrade -y \
    && apt-get clean \
    && rm -rf /var/lib/apt/lists/*

# UTF-8
RUN localedef -i en_US -c -f UTF-8 -A /usr/share/locale/locale.alias en_US.UTF-8
ENV LANG=en_US.utf8
ENV LC_ALL=C

# Set environment variables
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8

WORKDIR /apptoo

# Every sudo group user does not need a password
RUN echo '%sudo ALL=(ALL) NOPASSWD:ALL' >> /etc/sudoers

# Create a new group for the hermeswebui and hermeswebuitoo users
RUN groupadd -g 1024 hermeswebui \ 
    && groupadd -g 1025 hermeswebuitoo

# The hermeswebui (resp. hermeswebuitoo) user will have UID 1024 (resp. 1025), 
# be part of the hermeswebui (resp. hermeswebuitoo) and users groups and be sudo capable (passwordless) 
RUN useradd -u 1024 -d /home/hermeswebui -g hermeswebui -s /bin/bash -m hermeswebui \
    && usermod -G users hermeswebui \
    && adduser hermeswebui sudo
RUN useradd -u 1025 -d /home/hermeswebuitoo -g hermeswebuitoo -s /bin/bash -m hermeswebuitoo \
    && usermod -G users hermeswebuitoo \
    && adduser hermeswebuitoo sudo
RUN chown -R hermeswebuitoo:hermeswebuitoo /apptoo

USER root

COPY --chmod=555 docker_init.bash /hermeswebui_init.bash

RUN touch /.within_container

# Remove APT proxy configuration and clean up APT downloaded files
RUN rm -rf /var/lib/apt/lists/* /etc/apt/apt.conf.d/01proxy \
    && apt-get clean

USER root

# Pre-install uv system-wide so the container doesn't need internet access at runtime.
# Installing as root places uv in /usr/local/bin, available to all users.
# The init script will skip the download when uv is already on PATH.
RUN curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin sh

USER hermeswebuitoo

COPY --chown=hermeswebuitoo:hermeswebuitoo . /apptoo

# Bake the git version tag into the image so the settings badge works even
# when .git is not present (it is excluded by .dockerignore).
# CI passes: --build-arg HERMES_VERSION=$(git describe --tags --always)
# Local builds that omit the arg get "unknown" as the fallback.
ARG HERMES_VERSION=unknown
RUN echo "__version__ = '${HERMES_VERSION}'" > /apptoo/api/_version.py

# Default to binding all interfaces (required for container networking)
ENV HERMES_WEBUI_HOST=0.0.0.0
ENV HERMES_WEBUI_PORT=8787
ENV HERMES_HOME=/home/hermeswebui/.hermes
ENV GH_CONFIG_DIR=/home/hermeswebui/.hermes/gh
ENV XDG_CONFIG_HOME=/home/hermeswebui/.hermes/.config
ENV PYTHONUSERBASE=/home/hermeswebui/.hermes/python
ENV PIP_CACHE_DIR=/home/hermeswebui/.hermes/cache/pip
ENV PIPX_HOME=/home/hermeswebui/.hermes/pipx
ENV PIPX_BIN_DIR=/home/hermeswebui/.hermes/bin
ENV UV_CACHE_DIR=/home/hermeswebui/.hermes/cache/uv
ENV UV_TOOL_DIR=/home/hermeswebui/.hermes/uv/tools
ENV UV_TOOL_BIN_DIR=/home/hermeswebui/.hermes/bin
ENV NPM_CONFIG_PREFIX=/home/hermeswebui/.hermes/npm
ENV NPM_CONFIG_CACHE=/home/hermeswebui/.hermes/cache/npm
ENV PNPM_HOME=/home/hermeswebui/.hermes/pnpm
ENV YARN_GLOBAL_FOLDER=/home/hermeswebui/.hermes/yarn/global
ENV YARN_CACHE_FOLDER=/home/hermeswebui/.hermes/cache/yarn
ENV COREPACK_HOME=/home/hermeswebui/.hermes/corepack
ENV CARGO_HOME=/home/hermeswebui/.hermes/cargo
ENV RUSTUP_HOME=/home/hermeswebui/.hermes/rustup
ENV GOPATH=/home/hermeswebui/.hermes/go
ENV GOBIN=/home/hermeswebui/.hermes/bin
ENV BUN_INSTALL=/home/hermeswebui/.hermes/bun
ENV DENO_INSTALL=/home/hermeswebui/.hermes/deno
ENV GEM_HOME=/home/hermeswebui/.hermes/gem
ENV GEM_PATH=/home/hermeswebui/.hermes/gem
ENV COMPOSER_HOME=/home/hermeswebui/.hermes/composer
ENV DOTNET_CLI_HOME=/home/hermeswebui/.hermes/dotnet
ENV PATH=/home/hermeswebui/.hermes/bin:/home/hermeswebui/.hermes/python/bin:/home/hermeswebui/.hermes/npm/bin:/home/hermeswebui/.hermes/pnpm:/home/hermeswebui/.hermes/cargo/bin:/home/hermeswebui/.hermes/go/bin:/home/hermeswebui/.hermes/bun/bin:/home/hermeswebui/.hermes/deno/bin:/home/hermeswebui/.hermes/gem/bin:/home/hermeswebui/.hermes/composer/vendor/bin:/home/hermeswebui/.local/bin:${PATH}

EXPOSE 8787

CMD ["/hermeswebui_init.bash"]
