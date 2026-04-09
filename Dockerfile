FROM 1password/op:2@sha256:57d7d6a2bb2b74b2cf8111f6afb2973c74772198f82ea30359a53faae9fff5b1 AS op

FROM python:3.11-slim

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates dumb-init gosu \
    && rm -rf /var/lib/apt/lists/*

RUN groupadd --gid 10001 app \
    && useradd --uid 10001 --gid 10001 --create-home --shell /usr/sbin/nologin app

WORKDIR /app

COPY --from=op /usr/local/bin/op /usr/local/bin/op

COPY requirements.txt /app/requirements.txt
RUN python -m pip install --no-cache-dir --upgrade pip \
    && python -m pip install --no-cache-dir -r /app/requirements.txt

COPY op-entrypoint.sh /usr/local/bin/op-entrypoint
RUN chmod 0755 /usr/local/bin/op-entrypoint \
    && chown app:app /usr/local/bin/op-entrypoint /usr/local/bin/op

COPY --chown=app:app app.py models.py storage.py measurement_aggregator.py /app/
COPY --chown=app:app deployment /app/deployment

RUN mkdir -p /app/logs \
    && chown -R app:app /app

EXPOSE 8084

ENTRYPOINT ["dumb-init", "--", "/usr/local/bin/op-entrypoint"]
CMD ["python", "app.py"]
