FROM postgis/postgis:15-3.3

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
       build-essential \
       postgresql-server-dev-15 \
       git \
       clang-16 \
    && ln -sf /usr/bin/clang-16 /usr/bin/clang \
    && git clone --branch v0.7.4 https://github.com/pgvector/pgvector.git /tmp/pgvector \
    && cd /tmp/pgvector && make && make install \
    && rm -rf /tmp/pgvector \
    && apt-get purge -y build-essential postgresql-server-dev-15 git \
    && apt-get autoremove -y \
    && rm -rf /var/lib/apt/lists/*
