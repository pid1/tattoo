"""container entrypoint: uvicorn with the json log config applied.

running `uvicorn` from the shell would install its own plain-text logging
before anything of ours is imported, so the server is started in-process
instead and handed LOG_CONFIG explicitly.
"""

from __future__ import annotations

import os

import uvicorn

from tattoo.logconfig import LOG_CONFIG


def main() -> None:
    uvicorn.run(
        "tattoo.main:app",
        host=os.environ.get("TATTOO_HOST", "0.0.0.0"),  # noqa: S104 - tailnet-only by design
        port=int(os.environ.get("TATTOO_PORT", "8000")),
        log_config=LOG_CONFIG,
    )


if __name__ == "__main__":
    main()
