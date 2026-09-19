from fastapi import FastAPI

app = FastAPI(title="Distributed AI Task Scheduler")


@app.get("/health")
def health():
    return {"status": "ok"}