ARG PYTHON_BASE=python:3.13-slim
FROM ${PYTHON_BASE}
WORKDIR /app
COPY oppy/ oppy/
CMD ["python3", "-m", "oppy"]
