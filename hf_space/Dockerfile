FROM python:3.11-slim

WORKDIR /app

RUN pip install --no-cache-dir \
    fastapi \
    uvicorn[standard] \
    pyarrow \
    polars \
    huggingface_hub \
    pydantic \
    requests

COPY app.py .
COPY static/ ./static/

EXPOSE 7860

CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "7860"]
