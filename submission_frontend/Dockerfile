FROM python:3.11-slim

WORKDIR /app

COPY pyproject.toml .

# Install dependencies using pip
RUN pip install --no-cache-dir fastapi uvicorn google-adk[gcp] google-cloud-aiplatform pydantic

COPY main.py .

EXPOSE 8080

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8080"]
