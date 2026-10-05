"""Local API server for frontend development: python -m backend.api (routes without /api prefix)."""

import os

import uvicorn


if __name__ == "__main__":
    uvicorn.run("backend.api.app:app", host="127.0.0.1", port=int(os.environ.get("PORT", "8000")),
                server_header=False, log_level="warning")
