ARG JELLYFIN_VERSION=10.11.11
FROM jellyfin/jellyfin:${JELLYFIN_VERSION}
USER root
RUN apt-get update && apt-get install -y --no-install-recommends python3 python3-requests python3-bs4 && rm -rf /var/lib/apt/lists/*
COPY sitecustomize.py /opt/finamp-smoke/sitecustomize.py
COPY python /opt/finamp-smoke/python
RUN chmod 755 /opt/finamp-smoke/python
