import uvicorn
uvicorn.run("server:app", host="0.0.0.0", port=50021, log_level="info")
