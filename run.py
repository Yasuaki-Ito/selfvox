import sys
import uvicorn

port = 50021
if "--port" in sys.argv:
    idx = sys.argv.index("--port")
    if idx + 1 < len(sys.argv):
        port = int(sys.argv[idx + 1])

uvicorn.run("server:app", host="0.0.0.0", port=port, log_level="info")
