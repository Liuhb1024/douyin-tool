import sys
from pathlib import Path

import uvicorn

from app.douyin import DouyinError, save_text


def main() -> None:
    if len(sys.argv) > 1:
        text = " ".join(sys.argv[1:])
        try:
            path = save_text(text, Path("downloads"))
        except DouyinError as exc:
            print(exc, file=sys.stderr)
            raise SystemExit(1) from exc
        print(path)
        return
    uvicorn.run("app.main:app", host="127.0.0.1", port=8000, reload=False)


if __name__ == "__main__":
    main()
