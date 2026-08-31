FROM python:3.10-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Install a pre-built CPU wheel for llama-cpp-python instead of compiling it --
# compiling from source needs far more memory than Render's build step allows.
RUN pip install --no-cache-dir llama-cpp-python --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu

COPY . .

EXPOSE 7860

# Render assigns the port dynamically via $PORT -- do not hardcode it
CMD ["sh", "-c", "uvicorn app:app --host 0.0.0.0 --port ${PORT:-8000}"]
