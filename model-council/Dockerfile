FROM python:3.12-slim

# Pinned on purpose: entrypoint.py monkey-patches model_council.server
# (live config reload, no gateway restart — see entrypoint.py), so an
# unnoticed upstream major bump must not land silently.
RUN pip install --no-cache-dir model-council-mcp==0.14.0

COPY entrypoint.py /entrypoint.py

ENTRYPOINT ["python3", "/entrypoint.py"]
