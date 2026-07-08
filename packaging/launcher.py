"""NeoCloud OS 데스크톱 런처 — PyInstaller 번들 진입점.

더블클릭(.app) 또는 콘솔 실행 시:
  1. 빈 포트 탐색(8000부터) 후 uvicorn으로 API+웹 서버 기동
  2. 기동 확인 후 기본 브라우저로 대시보드 오픈
  3. 종료: 브라우저에서 http://127.0.0.1:<port>/shutdown 접속
     (콘솔 실행이면 Ctrl+C도 가능)
서버·데이터는 전부 로컬 프로세스(인메모리) — 외부 연결 없음.
"""
import os
import socket
import threading
import webbrowser

import uvicorn
from fastapi.responses import HTMLResponse

from app.main import app


def find_port(start: int = 8000, tries: int = 20) -> int:
    for port in range(start, start + tries):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return port
    raise RuntimeError("사용 가능한 포트가 없습니다 (8000~8019)")


PORT = find_port()


@app.get("/shutdown", include_in_schema=False)
def shutdown() -> HTMLResponse:
    """설치형 .app에는 터미널이 없으므로 브라우저에서 종료를 제공한다."""
    threading.Timer(0.6, lambda: os._exit(0)).start()
    return HTMLResponse(
        "<body style='background:#0f1722;color:#8aa0b4;font-family:sans-serif;"
        "display:grid;place-items:center;height:95vh'>"
        "<div style='text-align:center'><h2 style='color:#76b900'>NeoCloud OS</h2>"
        "서버를 종료했습니다. 이 탭은 닫아도 됩니다.</div></body>")


def open_browser() -> None:
    webbrowser.open(f"http://127.0.0.1:{PORT}/")


if __name__ == "__main__":
    threading.Timer(1.2, open_browser).start()
    try:
        uvicorn.run(app, host="127.0.0.1", port=PORT, log_level="warning")
    except KeyboardInterrupt:
        pass
