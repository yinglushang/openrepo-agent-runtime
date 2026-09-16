from __future__ import annotations

import argparse

import uvicorn


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the OpenRepo Agent Runtime API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", default=8000, type=int)
    parser.add_argument("--reload", action="store_true")
    arguments = parser.parse_args()
    uvicorn.run(
        "app.api:app",
        host=arguments.host,
        port=arguments.port,
        reload=arguments.reload,
        workers=1,
    )


if __name__ == "__main__":
    main()
