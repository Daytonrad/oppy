ARG PYTHON_BASE=python:3.13-slim
FROM ${PYTHON_BASE}
WORKDIR /app
COPY opportunity_beacon/ opportunity_beacon/
CMD ["python3", "-m", "opportunity_beacon"]
