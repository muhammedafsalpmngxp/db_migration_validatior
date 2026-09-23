# DBCompare - Streamlit UI + PocketFlow agent against Microsoft SQL Server.
#
# Alpine. Two things had to be true for that to work, and both were checked rather than
# assumed:
#
#   1. Every compiled dependency ships a musllinux wheel for cp312 - pyodbc 5.3.0,
#      pandas 3.0.6, pyarrow 25.0.1, numpy 2.5.3 and uv itself. So there is no compiler
#      in this image and no source build: `apk add build-base` is not needed.
#   2. Microsoft publishes the ODBC driver for Alpine as a signed .apk (17.5+ only).
#      pyodbc is just a binding; the driver is a separate install, and on Alpine it comes
#      from download.microsoft.com rather than a repository.
#
# ARG-driven arch so the same file builds for x86_64 and Graviton:
#   docker build -t dbcompare .                              (host arch)
#   docker build --platform linux/amd64 -t dbcompare .        (EC2 x86, from a Mac)
FROM python:3.12-alpine

# Set by BuildKit: amd64 or arm64, which is exactly how Microsoft names the .apk files.
ARG TARGETARCH=amd64
ARG MSODBC_VERSION=18.7.1.1-1

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy

# unixodbc is the driver manager pyodbc talks to; the rest are what the Microsoft driver
# links against on musl. gnupg is only here to verify the download and is removed after.
RUN apk add --no-cache \
        curl ca-certificates unixodbc krb5-libs libstdc++ openssl \
    && apk add --no-cache --virtual .verify gnupg \
    && cd /tmp \
    && curl -fsSLO "https://download.microsoft.com/download/ade174b7-8cea-4543-91a6-c33ae320c2f0/msodbcsql18_${MSODBC_VERSION}_${TARGETARCH}.apk" \
    && curl -fsSLO "https://download.microsoft.com/download/ade174b7-8cea-4543-91a6-c33ae320c2f0/msodbcsql18_${MSODBC_VERSION}_${TARGETARCH}.sig" \
    && curl -fsSL https://packages.microsoft.com/keys/microsoft.asc | gpg --import - \
    # A driver that talks to the database on our behalf is worth verifying: without this
    # --allow-untrusted would install whatever the network handed back.
    && gpg --verify "msodbcsql18_${MSODBC_VERSION}_${TARGETARCH}.sig" \
                    "msodbcsql18_${MSODBC_VERSION}_${TARGETARCH}.apk" \
    # --allow-untrusted is required because the package is signed with Microsoft's GPG
    # key, not an apk repository key. The signature above is what makes that acceptable.
    && apk add --allow-untrusted "msodbcsql18_${MSODBC_VERSION}_${TARGETARCH}.apk" \
    && apk del .verify \
    && rm -rf /tmp/* /root/.gnupg

RUN pip install --no-cache-dir uv==0.11.26

WORKDIR /app

# Dependencies first, from the lockfile, so a code change does not reinstall them.
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY app.py main.py config.py flow.py nodes.py tools.py check_setup.py ./
COPY utils/ ./utils/
COPY .streamlit/ ./.streamlit/

# The app writes only its log file; it runs as a non-root user that owns that directory.
RUN addgroup -g 10001 dbcompare \
    && adduser -D -u 10001 -G dbcompare dbcompare \
    && mkdir -p /app/logs \
    && chown -R dbcompare:dbcompare /app
USER dbcompare

ENV PATH="/app/.venv/bin:$PATH"

# Fail fast and loudly if the driver ever goes missing from a future base image, rather
# than at the first connection attempt in front of a user.
RUN python -c "import pyodbc; ds = pyodbc.drivers(); \
print('ODBC drivers:', ds); \
assert any('SQL Server' in d for d in ds), 'no SQL Server ODBC driver in the image'"

EXPOSE 8501

# Streamlit's own health endpoint: the container is unhealthy until the app can serve.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD curl -fsS http://localhost:8501/_stcore/health || exit 1

CMD ["streamlit", "run", "app.py"]
