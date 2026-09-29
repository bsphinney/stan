FROM python:3.11-slim

WORKDIR /app

# Pinned (v1.2.0): unpinned installs meant every rebuild took the newest
# release, and huggingface_hub 2.0 would have crashed the relay at import.
# These are the versions tests/test_relay_peg.py passes against; move them
# only together with a test run. Deploy with scripts/deploy_hf_space.py.
RUN pip install --no-cache-dir \
    fastapi==0.141.1 \
    uvicorn[standard]==0.54.0 \
    pyarrow==25.0.1 \
    polars==1.44.2 \
    huggingface_hub==2.0.0 \
    pydantic==2.13.5 \
    requests==2.34.2

COPY app.py .
COPY static/ ./static/

EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
