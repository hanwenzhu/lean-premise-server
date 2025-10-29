# Using Python 3.11 due to https://github.com/facebookresearch/faiss/issues/3936
# Once faiss-cpu is updated to include this fix, we can update to newest Python
# and faiss-cpu versions
FROM python:3.11-slim

RUN apt-get update && \
    apt-get install -y --no-install-recommends build-essential && \
    rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY . .

CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
